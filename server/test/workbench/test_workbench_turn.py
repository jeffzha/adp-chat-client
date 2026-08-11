import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.workbench_turn as turn_module
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_control_events import CACHE_INVALIDATE, SESSION_REVOKE, WorkbenchControlEvent
from core.workbench_metrics import WorkbenchMetrics
from core.workbench_runtime import RuntimeLease
from core.workbench_turn import WorkbenchTurnError, WorkbenchTurnManager
from model.workbench import (
    WorkbenchAgentBinding,
    WorkbenchTurn,
    WorkbenchTurnCancellation,
    WorkbenchTurnEvent,
)


ACCOUNT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
FOREIGN_ACCOUNT_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


class _AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    def add(self, value):
        self.session.add(value)

    async def execute(self, statement, params=None):
        return self.session.execute(statement, params or {})

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()


@pytest.fixture
def turn_store(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def register_uuid(dbapi_connection, _connection_record):
        dbapi_connection.create_function("uuid_generate_v4", 0, lambda: uuid.uuid4().hex)

    WorkbenchAgentBinding.__table__.create(engine)
    WorkbenchTurn.__table__.create(engine)
    WorkbenchTurnEvent.__table__.create(engine)
    WorkbenchTurnCancellation.__table__.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def connection():
        session = sessions()
        try:
            yield _AsyncSessionAdapter(session)
        finally:
            session.close()

    monkeypatch.setattr(turn_module, "db_connection", connection)
    monkeypatch.setattr(WorkbenchTurnManager, "instance_id", "instance-a")
    monkeypatch.setattr(WorkbenchTurnManager, "lifecycle_id", "lifecycle-new")
    yield sessions, connection
    engine.dispose()


def _identity(binding_id="binding-7", customer_id=7):
    return WorkbenchIdentityContext(
        binding_id=binding_id,
        canonical_subject=f"napi:prod:customer:{customer_id}:user:9",
        customer_id=customer_id,
        new_api_user_id=9,
        auth_epoch=1,
        display_name="User 9",
        application_id="customer-app-7",
        app_profile_id="17",
        access_mode="active",
        config_version=3,
    )


def _app_context():
    return WorkbenchAppContext(
        application_id="customer-app-7",
        app_profile_id="17",
        config_version=3,
        auth_epoch=1,
        vendor="Tencent",
        service_vendor="ChinaTencentCloud",
        app_id="provider-app-7",
        app_key="provider-key",
        space_id="",
        template_agent_id="template-7",
        secret_id="secret-id",
        secret_key="secret-key",
        capabilities=("chat",),
        limits={
            "customer_concurrency": 5,
            "user_concurrency": 1,
            "max_runtime_seconds": 60,
            "max_reasoning_rounds": 20,
            "max_output_tokens": 8192,
            "web_search_per_turn": 0,
            "max_file_bytes": 1024,
        },
    )


async def _submit(connection, client_request_id=None, digest=None):
    client_request_id = client_request_id or str(uuid.uuid4())
    digest = digest or WorkbenchTurnManager.request_digest(
        {"Contents": [{"Type": "text", "Text": "hello"}]}
    )
    async with connection() as db:
        result = await WorkbenchTurnManager.create_or_get(
            db,
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            client_request_id=client_request_id,
            request_digest=digest,
            conversation_id=None,
        )
    return result, client_request_id, digest


@pytest.mark.asyncio
async def test_same_client_request_is_reused_and_different_payload_conflicts(turn_store):
    sessions, connection = turn_store
    first, request_id, digest = await _submit(connection)
    second, _, _ = await _submit(connection, request_id, digest)

    assert first.created is True
    assert second.created is False
    assert second.turn_id == first.turn_id
    with sessions() as session:
        assert session.execute(select(func.count(WorkbenchTurn.Id))).scalar_one() == 1

    with pytest.raises(WorkbenchTurnError, match="different request") as error:
        await _submit(
            connection,
            request_id,
            WorkbenchTurnManager.request_digest(
                {"Contents": [{"Type": "text", "Text": "different"}]}
            ),
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_turn_submission_rejects_agent_while_skill_mutation_epoch_is_open(
    turn_store,
):
    sessions, connection = turn_store
    with sessions() as session:
        session.add(
            WorkbenchAgentBinding(
                BindingId="binding-7",
                AccountId=ACCOUNT_ID,
                ApplicationId="customer-app-7",
                AgentId="agent-7",
                Status="configuring_integrations",
                AttemptId="mutation-epoch-2",
            )
        )
        session.commit()

    with pytest.raises(WorkbenchTurnError, match="being reconfigured") as exc_info:
        await _submit(connection)

    assert exc_info.value.status_code == 409
    with sessions() as session:
        assert session.execute(select(func.count(WorkbenchTurn.Id))).scalar_one() == 0


@pytest.mark.asyncio
async def test_cross_owner_turn_lookup_returns_not_found(turn_store):
    _, connection = turn_store
    submission, _, _ = await _submit(connection)
    async with connection() as db:
        owned = await WorkbenchTurnManager.get_owned(
            db,
            turn_id=submission.turn_id,
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
        )
        foreign = await WorkbenchTurnManager.get_owned(
            db,
            turn_id=submission.turn_id,
            account_id=FOREIGN_ACCOUNT_ID,
            identity=_identity("binding-other", 8),
            app_context=_app_context(),
        )

    assert owned is not None
    assert foreign is None


@pytest.mark.asyncio
async def test_create_provider_and_terminal_event_commits_are_observed(turn_store, monkeypatch):
    _, connection = turn_store
    metrics = WorkbenchMetrics()
    monkeypatch.setattr(turn_module, "WORKBENCH_METRICS", metrics)

    submission, _, _ = await _submit(connection)
    await WorkbenchTurnManager._append_provider_event(
        submission.turn_id,
        b'data: {"Type":"text.delta","Text":"a"}\n\n',
    )
    await WorkbenchTurnManager._finalize(submission.turn_id, "completed")

    output = metrics.render()
    assert "workbench_turn_events_persisted_total 3" in output
    assert "workbench_turn_event_persist_db_seconds_count 3" in output
    assert 'workbench_turns_total{status="completed"} 1' in output


@pytest.mark.asyncio
async def test_events_are_ordered_replayable_and_bounded(turn_store, monkeypatch):
    sessions, connection = turn_store
    submission, _, _ = await _submit(connection)
    metrics = WorkbenchMetrics()
    monkeypatch.setattr(turn_module, "WORKBENCH_METRICS", metrics)
    monkeypatch.setattr(WorkbenchTurnManager, "MAX_PERSISTED_EVENTS", 4)

    await WorkbenchTurnManager._append_provider_event(
        submission.turn_id,
        b'data: {"Type":"text.delta","Text":"a"}\n\n',
    )
    await WorkbenchTurnManager._append_provider_event(
        submission.turn_id,
        b'data: {"Type":"text.delta","Text":"b"}\n\n',
    )
    with pytest.raises(WorkbenchTurnError, match="count limit"):
        await WorkbenchTurnManager._append_provider_event(
            submission.turn_id,
            b'data: {"Type":"text.delta","Text":"c"}\n\n',
        )
    assert "workbench_turn_events_persisted_total 2" in metrics.render()
    assert "workbench_turn_event_persist_db_seconds_count 2" in metrics.render()
    await WorkbenchTurnManager._finalize(submission.turn_id, "completed")

    written = []
    async def write(chunk):
        written.append(chunk)

    await WorkbenchTurnManager.replay(
        write,
        turn_id=submission.turn_id,
        account_id=ACCOUNT_ID,
        identity=_identity(),
        app_context=_app_context(),
        after_sequence=1,
    )
    assert [chunk.split(b"\n", 1)[0] for chunk in written] == [b"id: 2", b"id: 3", b"id: 4"]
    with sessions() as session:
        sequences = session.execute(
            select(WorkbenchTurnEvent.Sequence)
            .where(WorkbenchTurnEvent.TurnId == submission.turn_id)
            .order_by(WorkbenchTurnEvent.Sequence)
        ).scalars().all()
    assert sequences == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_failed_event_commit_does_not_change_persistence_metrics(turn_store, monkeypatch):
    sessions, connection = turn_store
    submission, _, _ = await _submit(connection)
    metrics = WorkbenchMetrics()
    monkeypatch.setattr(turn_module, "WORKBENCH_METRICS", metrics)

    @asynccontextmanager
    async def failing_connection():
        session = sessions()
        adapter = _AsyncSessionAdapter(session)

        async def fail_commit():
            raise RuntimeError("database commit failed")

        adapter.commit = fail_commit
        try:
            yield adapter
        finally:
            session.rollback()
            session.close()

    monkeypatch.setattr(turn_module, "db_connection", failing_connection)
    with pytest.raises(RuntimeError, match="database commit failed"):
        await WorkbenchTurnManager._append_provider_event(
            submission.turn_id,
            b'data: {"Type":"text.delta","Text":"not committed"}\n\n',
        )

    output = metrics.render()
    assert "workbench_turn_events_persisted_total 0" in output
    assert "workbench_turn_event_persist_db_seconds_count 0" in output
    with sessions() as session:
        assert session.execute(select(func.count(WorkbenchTurnEvent.Id))).scalar_one() == 1


@pytest.mark.asyncio
async def test_replay_sequence_id_cannot_be_overridden_by_provider(turn_store):
    sessions, connection = turn_store
    submission, _, _ = await _submit(connection)
    await WorkbenchTurnManager._append_provider_event(
        submission.turn_id,
        'id: provider-cursor\ndata: {"Type":"text.delta","Text":"a"}\n\n',
    )
    await WorkbenchTurnManager._finalize(submission.turn_id, "completed")

    written = []

    async def write(chunk):
        written.append(chunk)

    await WorkbenchTurnManager.replay(
        write,
        turn_id=submission.turn_id,
        account_id=ACCOUNT_ID,
        identity=_identity(),
        app_context=_app_context(),
        after_sequence=1,
    )

    assert written[0].startswith(b"id: 2\n")
    assert b"provider-cursor" not in written[0]
    with sessions() as session:
        stored = session.execute(
            select(WorkbenchTurnEvent).where(
                WorkbenchTurnEvent.TurnId == submission.turn_id,
                WorkbenchTurnEvent.Sequence == 2,
            )
        ).scalar_one()
    assert "id:" not in stored.EventData


@pytest.mark.asyncio
async def test_cancel_intent_is_owned_idempotent_and_does_not_claim_provider_cancel(turn_store):
    sessions, connection = turn_store
    submission, _, _ = await _submit(connection)

    async with connection() as db:
        result = await WorkbenchTurnManager.request_cancel(
            db,
            turn_id=submission.turn_id,
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
        )
    assert result.status == "cancel_requested"
    assert result.provider_cancel_supported is False

    async with connection() as db:
        duplicate = await WorkbenchTurnManager.request_cancel(
            db,
            turn_id=submission.turn_id,
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
        )
    assert duplicate.status == "cancel_requested"

    with sessions() as session:
        turn = session.execute(
            select(WorkbenchTurn).where(WorkbenchTurn.TurnId == submission.turn_id)
        ).scalar_one()
        cancellation = session.execute(
            select(WorkbenchTurnCancellation).where(
                WorkbenchTurnCancellation.TurnId == submission.turn_id
            )
        ).scalar_one()
        assert turn.Status == "cancel_requested"
        assert turn.EventCount == 2
        assert cancellation.Status == "cancel_requested"
        assert cancellation.ProviderEvidenceSha256 is None

    await WorkbenchTurnManager._append_provider_event(
        submission.turn_id,
        'data: {"Type":"text.delta","Text":"provider continues"}\n\n',
    )
    with sessions() as session:
        turn = session.execute(
            select(WorkbenchTurn).where(WorkbenchTurn.TurnId == submission.turn_id)
        ).scalar_one()
        assert turn.Status == "cancel_requested"

    await WorkbenchTurnManager._finalize(submission.turn_id, "completed")
    with sessions() as session:
        turn = session.execute(
            select(WorkbenchTurn).where(WorkbenchTurn.TurnId == submission.turn_id)
        ).scalar_one()
        cancellation = session.execute(
            select(WorkbenchTurnCancellation).where(
                WorkbenchTurnCancellation.TurnId == submission.turn_id
            )
        ).scalar_one()
        assert turn.Status == "completed"
        assert cancellation.Status == "cancel_requested"


@pytest.mark.asyncio
async def test_cancel_intent_rejects_foreign_owner(turn_store):
    _, connection = turn_store
    submission, _, _ = await _submit(connection)

    async with connection() as db:
        with pytest.raises(WorkbenchTurnError, match="not found") as error:
            await WorkbenchTurnManager.request_cancel(
                db,
                turn_id=submission.turn_id,
                account_id=FOREIGN_ACCOUNT_ID,
                identity=_identity(binding_id="foreign-binding", customer_id=8),
                app_context=_app_context(),
            )
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_confirmation_requires_provider_evidence_and_is_terminal(turn_store, monkeypatch):
    sessions, connection = turn_store
    metrics = WorkbenchMetrics()
    monkeypatch.setattr(turn_module, "WORKBENCH_METRICS", metrics)
    submission, _, _ = await _submit(connection)
    evidence_hash = "b" * 64

    async with connection() as db:
        await WorkbenchTurnManager.request_cancel(
            db,
            turn_id=submission.turn_id,
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
        )
    async with connection() as db:
        result = await WorkbenchTurnManager.confirm_cancel(
            db,
            turn_id=submission.turn_id,
            provider_evidence_sha256=evidence_hash,
        )
        duplicate = await WorkbenchTurnManager.confirm_cancel(
            db,
            turn_id=submission.turn_id,
            provider_evidence_sha256=evidence_hash,
        )
        with pytest.raises(WorkbenchTurnError, match="conflicts"):
            await WorkbenchTurnManager.confirm_cancel(
                db,
                turn_id=submission.turn_id,
                provider_evidence_sha256="c" * 64,
            )
    assert result.status == "cancel_confirmed"
    assert duplicate.status == "cancel_confirmed"
    output = metrics.render()
    assert "workbench_turn_events_persisted_total 3" in output
    assert "workbench_turn_event_persist_db_seconds_count 3" in output

    with sessions() as session:
        turn = session.execute(
            select(WorkbenchTurn).where(WorkbenchTurn.TurnId == submission.turn_id)
        ).scalar_one()
        cancellation = session.execute(
            select(WorkbenchTurnCancellation).where(
                WorkbenchTurnCancellation.TurnId == submission.turn_id
            )
        ).scalar_one()
        assert turn.Status == "cancel_confirmed"
        assert cancellation.Status == "cancel_confirmed"
        assert cancellation.ProviderEvidenceSha256 == evidence_hash
        assert cancellation.ConfirmedAt is not None


@pytest.mark.asyncio
async def test_startup_marks_only_previous_local_lifecycle_provider_unknown(turn_store, monkeypatch):
    sessions, connection = turn_store
    metrics = WorkbenchMetrics()
    monkeypatch.setattr(turn_module, "WORKBENCH_METRICS", metrics)
    local, _, _ = await _submit(connection)
    second_local, _, _ = await _submit(connection)
    foreign, _, _ = await _submit(connection)
    with sessions() as session:
        local_turn = session.execute(
            select(WorkbenchTurn).where(WorkbenchTurn.TurnId == local.turn_id)
        ).scalar_one()
        second_local_turn = session.execute(
            select(WorkbenchTurn).where(WorkbenchTurn.TurnId == second_local.turn_id)
        ).scalar_one()
        foreign_turn = session.execute(
            select(WorkbenchTurn).where(WorkbenchTurn.TurnId == foreign.turn_id)
        ).scalar_one()
        local_turn.OwnerLifecycleId = "lifecycle-old"
        second_local_turn.OwnerLifecycleId = "lifecycle-old"
        foreign_turn.OwnerInstanceId = "instance-b"
        foreign_turn.OwnerLifecycleId = "lifecycle-old"
        session.commit()

    assert await WorkbenchTurnManager.recover_previous_lifecycle() == 2
    with sessions() as session:
        local_turn = session.execute(
            select(WorkbenchTurn).where(WorkbenchTurn.TurnId == local.turn_id)
        ).scalar_one()
        second_local_turn = session.execute(
            select(WorkbenchTurn).where(WorkbenchTurn.TurnId == second_local.turn_id)
        ).scalar_one()
        foreign_turn = session.execute(
            select(WorkbenchTurn).where(WorkbenchTurn.TurnId == foreign.turn_id)
        ).scalar_one()
    assert local_turn.Status == "provider_unknown"
    assert second_local_turn.Status == "provider_unknown"
    assert local_turn.ErrorCode == "PreviousLifecycleEnded"
    assert foreign_turn.Status == "submitted"
    output = metrics.render()
    assert "workbench_turn_events_persisted_total 5" in output
    assert "workbench_turn_event_persist_db_seconds_count 4" in output
    assert 'workbench_turns_total{status="provider_unknown"} 2' in output


@pytest.mark.asyncio
async def test_subscriber_disconnect_does_not_cancel_background_turn(monkeypatch):
    started = asyncio.Event()
    finish = asyncio.Event()

    async def execute(**_kwargs):
        started.set()
        await finish.wait()

    async def replay(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(WorkbenchTurnManager, "execute", execute)
    monkeypatch.setattr(WorkbenchTurnManager, "replay", replay)
    background = WorkbenchTurnManager.schedule(
        turn_id="turn-1",
        identity=_identity(),
        app_context=_app_context(),
        claims={"exp": 9999999999},
    )
    subscriber = asyncio.create_task(WorkbenchTurnManager.replay(None))
    await started.wait()
    subscriber.cancel()
    await asyncio.gather(subscriber, return_exceptions=True)

    assert background.done() is False
    finish.set()
    await background


@pytest.mark.asyncio
async def test_execute_releases_lease_and_never_resubmits_provider(monkeypatch):
    lease = RuntimeLease(lease_id="lease-1", max_runtime_seconds=60)
    release = AsyncMock()
    finalize = AsyncMock()
    append = AsyncMock()
    provider_calls = []

    async def acquire(**_kwargs):
        return lease

    @asynccontextmanager
    async def connection():
        yield object()

    async def ensure(*_args, **_kwargs):
        return SimpleNamespace(ownership_id="agent-9", provider_agent_id="agent-9")

    async def stream():
        provider_calls.append("submitted")
        yield b'data: {"Type":"response.completed"}\n\n'

    async def message(*_args, **_kwargs):
        return stream()

    async def pump(upstream, write, **_kwargs):
        async for item in upstream:
            await write(item)
        await release(lease)

    monkeypatch.setattr(turn_module, "db_connection", connection)
    monkeypatch.setattr(turn_module.WorkbenchRuntimeGuard, "acquire", acquire)
    monkeypatch.setattr(turn_module.WorkbenchRuntimeGuard, "release", release)
    monkeypatch.setattr(turn_module.CoreAgent, "ensure_runtime_principal", ensure)
    monkeypatch.setattr(turn_module.CoreChat, "message", lambda *_args, **_kwargs: stream())
    monkeypatch.setattr(turn_module.WorkbenchStreamGuard, "pump", pump)
    monkeypatch.setattr(
        turn_module.WorkbenchUsageTelemetry,
        "capture_completed_event",
        AsyncMock(return_value="wte-test"),
    )
    monkeypatch.setattr(WorkbenchTurnManager, "_set_running", AsyncMock())
    monkeypatch.setattr(WorkbenchTurnManager, "_append_provider_event", append)
    monkeypatch.setattr(WorkbenchTurnManager, "_finalize", finalize)

    await WorkbenchTurnManager.execute(
        turn_id="turn-1",
        vendor_app=SimpleNamespace(),
        account_id="account-9",
        identity=_identity(),
        app_context=_app_context(),
        claims={"exp": 9999999999},
        contents=[{"Type": "text", "Text": "hello"}],
        conversation_id=None,
        search_network=False,
        custom_variables={},
        limits=_app_context().limits,
    )

    assert provider_calls == ["submitted"]
    append.assert_awaited_once()
    finalize.assert_awaited_once_with("turn-1", "completed")
    release.assert_any_await(lease)


@pytest.mark.asyncio
async def test_private_file_url_is_redacted_before_turn_event_persistence(monkeypatch):
    lease = RuntimeLease(lease_id="lease-file", max_runtime_seconds=60)
    append = AsyncMock()
    private_url = (
        "https://private-workbench-1250000000.cos.ap-guangzhou.myqcloud.com/"
        "workbench/customer-7/binding-9/private.pdf?q-sign-algorithm=sha1"
    )

    @asynccontextmanager
    async def connection():
        yield object()

    async def stream():
        yield (
            'data: {"Type":"debug","AppKey":"provider-key",'
            '"AuthConfig":{"SecretKey":"secret-key"},"Nested":{"FileUrl":"'
            + private_url
            + '"}}\n\n'
        ).encode()
        yield b'data: {"Type":"response.completed","Response":{}}\n\n'

    async def pump(upstream, write, **_kwargs):
        async for item in upstream:
            await write(item)

    monkeypatch.setattr(turn_module, "db_connection", connection)
    monkeypatch.setattr(turn_module.WorkbenchRuntimeGuard, "acquire", AsyncMock(return_value=lease))
    monkeypatch.setattr(turn_module.WorkbenchRuntimeGuard, "release", AsyncMock())
    monkeypatch.setattr(
        turn_module.CoreAgent,
        "ensure_runtime_principal",
        AsyncMock(
            return_value=SimpleNamespace(
                ownership_id="agent-9", provider_agent_id="agent-9"
            )
        ),
    )
    monkeypatch.setattr(turn_module.CoreChat, "message", lambda *_args, **_kwargs: stream())
    monkeypatch.setattr(turn_module.WorkbenchStreamGuard, "pump", pump)
    monkeypatch.setattr(
        turn_module.WorkbenchUsageTelemetry,
        "capture_completed_event",
        AsyncMock(return_value="wte-test"),
    )
    monkeypatch.setattr(WorkbenchTurnManager, "_set_running", AsyncMock())
    monkeypatch.setattr(WorkbenchTurnManager, "_append_provider_event", append)
    monkeypatch.setattr(WorkbenchTurnManager, "_finalize", AsyncMock())

    await WorkbenchTurnManager.execute(
        turn_id="turn-file",
        vendor_app=SimpleNamespace(),
        account_id="account-9",
        identity=_identity(),
        app_context=_app_context(),
        claims={"exp": 9999999999},
        contents=[
            {
                "Type": "file",
                "File": {"FileUrl": private_url, "Url": private_url},
            }
        ],
        conversation_id=None,
        search_network=False,
        custom_variables={},
        limits=_app_context().limits,
    )

    persisted = append.await_args_list[0].args[1]
    assert "private-workbench-1250000000" not in persisted
    assert "workbench/customer-" not in persisted
    assert "q-sign" not in persisted
    assert "provider-key" not in persisted
    assert "secret-key" not in persisted


@pytest.mark.asyncio
async def test_stream_without_completion_evidence_cannot_finalize_as_completed(monkeypatch):
    lease = RuntimeLease(lease_id="lease-no-completion", max_runtime_seconds=60)
    finalize = AsyncMock()
    capture = AsyncMock(return_value="wte-unused")

    @asynccontextmanager
    async def connection():
        yield object()

    async def stream():
        yield b'data: {"Type":"text.delta","Text":"partial"}\n\n'

    async def pump(upstream, write, **_kwargs):
        async for item in upstream:
            await write(item)

    monkeypatch.setattr(turn_module, "db_connection", connection)
    monkeypatch.setattr(
        turn_module.WorkbenchRuntimeGuard,
        "acquire",
        AsyncMock(return_value=lease),
    )
    monkeypatch.setattr(turn_module.WorkbenchRuntimeGuard, "release", AsyncMock())
    monkeypatch.setattr(
        turn_module.CoreAgent,
        "ensure_runtime_principal",
        AsyncMock(
            return_value=SimpleNamespace(
                ownership_id="agent-9", provider_agent_id="agent-9"
            )
        ),
    )
    monkeypatch.setattr(turn_module.CoreChat, "message", lambda *_args, **_kwargs: stream())
    monkeypatch.setattr(turn_module.WorkbenchStreamGuard, "pump", pump)
    monkeypatch.setattr(turn_module.WorkbenchUsageTelemetry, "capture_completed_event", capture)
    monkeypatch.setattr(WorkbenchTurnManager, "_set_running", AsyncMock())
    monkeypatch.setattr(WorkbenchTurnManager, "_append_provider_event", AsyncMock())
    monkeypatch.setattr(WorkbenchTurnManager, "_finalize", finalize)

    await WorkbenchTurnManager.execute(
        turn_id="turn-no-completion",
        vendor_app=SimpleNamespace(),
        account_id="account-9",
        identity=_identity(),
        app_context=_app_context(),
        claims={"exp": 9999999999},
        contents=[{"Type": "text", "Text": "hello"}],
        conversation_id=None,
        search_network=False,
        custom_variables={},
        limits=_app_context().limits,
    )

    capture.assert_not_awaited()
    finalize.assert_awaited_once_with(
        "turn-no-completion",
        "failed_after_accept",
        "WorkbenchTurnError",
    )


@pytest.mark.asyncio
async def test_timeout_before_first_provider_event_is_unknown_and_releases_lease(monkeypatch):
    lease = RuntimeLease(lease_id="lease-timeout", max_runtime_seconds=60)
    release = AsyncMock()
    finalize = AsyncMock()

    @asynccontextmanager
    async def connection():
        yield object()

    async def acquire(**_kwargs):
        return lease

    async def ensure(*_args, **_kwargs):
        return SimpleNamespace(ownership_id="agent-9", provider_agent_id="agent-9")

    async def stream():
        yield b'data: {"Type":"response.completed"}\n\n'

    async def timeout(*_args, **_kwargs):
        raise TimeoutError("provider did not produce a verifiable event")

    monkeypatch.setattr(turn_module, "db_connection", connection)
    monkeypatch.setattr(turn_module.WorkbenchRuntimeGuard, "acquire", acquire)
    monkeypatch.setattr(turn_module.WorkbenchRuntimeGuard, "release", release)
    monkeypatch.setattr(turn_module.CoreAgent, "ensure_runtime_principal", ensure)
    monkeypatch.setattr(turn_module.CoreChat, "message", lambda *_args, **_kwargs: stream())
    monkeypatch.setattr(turn_module.WorkbenchStreamGuard, "pump", timeout)
    monkeypatch.setattr(WorkbenchTurnManager, "_set_running", AsyncMock())
    monkeypatch.setattr(WorkbenchTurnManager, "_finalize", finalize)

    await WorkbenchTurnManager.execute(
        turn_id="turn-timeout",
        vendor_app=SimpleNamespace(),
        account_id="account-9",
        identity=_identity(),
        app_context=_app_context(),
        claims={"exp": 9999999999},
        contents=[{"Type": "text", "Text": "hello"}],
        conversation_id=None,
        search_network=False,
        custom_variables={},
        limits=_app_context().limits,
    )

    finalize.assert_awaited_once_with("turn-timeout", "provider_unknown", "TimeoutError")
    release.assert_awaited_once_with(lease)


def test_migration_registers_durable_turn_tables():
    from unittest.mock import patch

    with patch("app_factory.TAgenticApp.get_app", return_value=SimpleNamespace()):
        from core.migration import Migration

    assert WorkbenchTurn in Migration.tables()
    assert WorkbenchTurnEvent in Migration.tables()
    assert WorkbenchTurnCancellation in Migration.tables()


@pytest.mark.asyncio
async def test_session_revoke_reintrospects_effective_context_without_epoch_comparison(monkeypatch):
    finish = asyncio.Event()

    async def execute(**_kwargs):
        await finish.wait()

    reauthorize = AsyncMock()
    monkeypatch.setattr(WorkbenchTurnManager, "execute", execute)
    monkeypatch.setattr(turn_module.WorkbenchStreamGuard, "reauthorize", reauthorize)
    task = WorkbenchTurnManager.schedule(
        turn_id="turn-session-event",
        identity=_identity(),
        app_context=_app_context(),
        claims={"AuthEpoch": 41, "exp": 9999999999},
    )
    event = WorkbenchControlEvent(
        event_key="session-event-1",
        event_type=SESSION_REVOKE,
        customer_id=7,
        payload={
            "customer_id": 7,
            "new_api_user_id": 9,
            "auth_epoch": 999999,
        },
        created_at=datetime.now(UTC),
    )

    await WorkbenchTurnManager.handle_control_event(event)

    reauthorize.assert_awaited_once()
    assert task.cancelled() is False
    finish.set()
    await task


@pytest.mark.asyncio
async def test_cache_invalidate_reauthorization_failure_closes_matching_subscriber(monkeypatch):
    task = asyncio.create_task(asyncio.Event().wait())
    context = turn_module._LiveTurnContext(
        task=task,
        identity=_identity(),
        app_context=_app_context(),
        claims={"AuthEpoch": 41, "exp": 9999999999},
        method="GET",
        resource_path="/chat/turn/events",
    )
    WorkbenchTurnManager._subscribers[task] = context
    monkeypatch.setattr(
        turn_module.WorkbenchStreamGuard,
        "reauthorize",
        AsyncMock(side_effect=RuntimeError("control introspection unavailable")),
    )
    event = WorkbenchControlEvent(
        event_key="cache-event-1",
        event_type=CACHE_INVALIDATE,
        customer_id=7,
        payload={
            "customer_id": 7,
            "customer_app_id": 17,
            "application_id": "customer-app-7",
            "auth_epoch": 123,
        },
        created_at=datetime.now(UTC),
    )

    try:
        await WorkbenchTurnManager.handle_control_event(event)
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() is True
    finally:
        WorkbenchTurnManager._subscribers.pop(task, None)


@pytest.mark.asyncio
async def test_cache_invalidate_does_not_cancel_an_accepted_provider_consumer(monkeypatch):
    finish = asyncio.Event()

    async def execute(**_kwargs):
        await finish.wait()

    monkeypatch.setattr(WorkbenchTurnManager, "execute", execute)
    monkeypatch.setattr(
        turn_module.WorkbenchStreamGuard,
        "reauthorize",
        AsyncMock(side_effect=RuntimeError("revoked")),
    )
    task = WorkbenchTurnManager.schedule(
        turn_id="turn-provider-drain",
        identity=_identity(),
        app_context=_app_context(),
        claims={"AuthEpoch": 41, "exp": 9999999999},
    )
    event = WorkbenchControlEvent(
        event_key="cache-event-drain",
        event_type=CACHE_INVALIDATE,
        customer_id=7,
        payload={
            "customer_id": 7,
            "application_id": "customer-app-7",
            "auth_epoch": 123,
        },
        created_at=datetime.now(UTC),
    )

    await WorkbenchTurnManager.handle_control_event(event)

    assert task.cancelled() is False
    finish.set()
    await task
