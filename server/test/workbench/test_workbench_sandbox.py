import asyncio
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from config import tagentic_config
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_identity import CoreWorkbenchIdentity, WorkbenchIdentityError
from core.workbench_runtime import RuntimeLease
from core.workbench_sandbox.contracts import (
    CodeResult,
    CommandChunk,
    CommandResult,
    ProviderInstance,
    SandboxProviderError,
)
from core.workbench_sandbox.service import WorkbenchSandboxError, WorkbenchSandboxService
from core.workbench_sandbox.pty import (
    WorkbenchPtyConnection,
    WorkbenchSandboxPtyError,
    WorkbenchSandboxPtyService,
)
from core.workbench_sandbox.provider import TencentAGSXProvider
from core.workbench_sandbox import code_worker
from core.workbench_sandbox import secrets as sandbox_secrets
from core.workbench_sandbox.secrets import read_secret_file
from model.chat import ChatConversation
from model.workbench import WorkbenchConversationWorkspace
from model.workbench_sandbox import (
    WorkbenchSandbox,
    WorkbenchSandboxAudit,
    WorkbenchSandboxPty,
)
from model.workbench_sandbox_acceptance import WorkbenchSandboxAcceptanceEvent


ACCOUNT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONVERSATION_1 = "11111111-1111-4111-8111-111111111111"
CONVERSATION_2 = "22222222-2222-4222-8222-222222222222"
CONVERSATION_3 = "33333333-3333-4333-8333-333333333333"


class _AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    def add(self, value):
        self.session.add(value)

    def get_bind(self):
        return self.session.get_bind()

    async def execute(self, statement, params=None):
        return self.session.execute(statement, params or {})

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()


def _identity(*, customer=7, user=9, epoch=3, binding="binding-7"):
    return WorkbenchIdentityContext(
        binding_id=binding,
        canonical_subject=f"napi:prod:customer:{customer}:user:{user}",
        customer_id=customer,
        new_api_user_id=user,
        auth_epoch=epoch,
        display_name="User",
        application_id="customer-app-7",
        app_profile_id="17",
        access_mode="active",
        config_version=5,
    )


def _app_context(
    *,
    epoch=3,
    capabilities=("chat", "sandbox", "files"),
    customer_concurrency=5,
    user_concurrency=1,
):
    return WorkbenchAppContext(
        application_id="customer-app-7",
        app_profile_id="17",
        config_version=5,
        auth_epoch=epoch,
        vendor="Tencent",
        service_vendor="ChinaTencentCloud",
        app_id="provider-app-7",
        app_key="provider-key",
        space_id="space-7",
        template_agent_id="template-7",
        secret_id="secret-id",
        secret_key="secret-key",
        capabilities=capabilities,
        limits={
            "customer_concurrency": customer_concurrency,
            "user_concurrency": user_concurrency,
            "max_runtime_seconds": 120,
            "max_reasoning_rounds": 20,
            "max_output_tokens": 8192,
            "web_search_per_turn": 0,
            "max_file_bytes": 1024,
        },
    )


class _FakeProvider:
    def __init__(self):
        self.start_tokens = []
        self.stop_calls = []
        self.stopped_instances = set()
        self.file_data = b"file"
        self.code_result = CodeResult("out", "", ("result",), None)
        self.command_result = CommandResult("out", "", 0)
        self.start_error = None
        self.read_kwargs = None
        self.write_kwargs = None

    async def start(self, *, client_token, timeout_seconds, metadata):
        del timeout_seconds, metadata
        self.start_tokens.append(client_token)
        if self.start_error:
            raise self.start_error
        return ProviderInstance(
            f"provider-instance-{len(self.start_tokens)}",
            "running",
            "SANDBOX",
            "TOKEN",
            datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=10),
        )

    async def describe(self, instance_id):
        status = "stopped" if instance_id in self.stopped_instances else "running"
        return ProviderInstance(instance_id, status, "SANDBOX", "TOKEN")

    async def pause(self, instance_id):
        return ProviderInstance(instance_id, "paused", "SANDBOX", "TOKEN")

    async def resume(self, instance_id, *, timeout_seconds):
        del timeout_seconds
        return ProviderInstance(instance_id, "running", "SANDBOX", "TOKEN")

    async def stop(self, instance_id):
        self.stop_calls.append(instance_id)
        self.stopped_instances.add(instance_id)

    async def execute_code(self, instance_id, **kwargs):
        del instance_id, kwargs
        return self.code_result

    async def run_command(self, instance_id, **kwargs):
        del instance_id, kwargs
        return self.command_result

    async def _stream(self):
        yield CommandChunk("stdout", "first")
        await __import__("asyncio").Event().wait()

    def stream_command(self, instance_id, **kwargs):
        del instance_id, kwargs
        return self._stream()

    async def read_file(self, instance_id, **kwargs):
        del instance_id
        self.read_kwargs = kwargs
        return self.file_data

    async def write_file(self, instance_id, **kwargs):
        del instance_id
        self.write_kwargs = kwargs

class _RuntimeGuard:
    released = []
    acquired = []

    @staticmethod
    async def acquire(**kwargs):
        _RuntimeGuard.acquired.append(kwargs)
        return RuntimeLease("runtime-1", 120)

    @classmethod
    async def release(cls, lease):
        cls.released.append(lease)


class _BlockingProvider(_FakeProvider):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release_start = asyncio.Event()

    async def start(self, **kwargs):
        result = await super().start(**kwargs)
        self.started.set()
        await self.release_start.wait()
        return result


@pytest.fixture
def sandbox_store(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def register_uuid(dbapi_connection, _connection_record):
        dbapi_connection.create_function("uuid_generate_v4", 0, lambda: uuid.uuid4().hex)

    ChatConversation.__table__.create(engine)
    WorkbenchConversationWorkspace.__table__.create(engine)
    WorkbenchSandbox.__table__.create(engine)
    WorkbenchSandboxAudit.__table__.create(engine)
    WorkbenchSandboxPty.__table__.create(engine)
    WorkbenchSandboxAcceptanceEvent.__table__.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as session:
        for conversation_id in (CONVERSATION_1, CONVERSATION_2):
            session.add(
                ChatConversation(
                    Id=uuid.UUID(conversation_id),
                    AccountId=ACCOUNT_ID,
                    ApplicationId="customer-app-7",
                    Title="Sandbox test",
                )
            )
            session.add(
                WorkbenchConversationWorkspace(
                    ConversationId=uuid.UUID(conversation_id),
                    BindingId="binding-7",
                    AccountId=ACCOUNT_ID,
                    CustomerId=7,
                    ApplicationId="customer-app-7",
                    ProviderAppId="provider-app-7",
                    AppProfileId="17",
                    ConfigVersion=5,
                    AgentId="agent-7",
                    Status="active",
                )
            )
        session.add(
            ChatConversation(
                Id=uuid.UUID(CONVERSATION_3),
                AccountId=ACCOUNT_ID,
                ApplicationId="customer-app-7",
                Title="Other user sandbox test",
            )
        )
        session.add(
            WorkbenchConversationWorkspace(
                ConversationId=uuid.UUID(CONVERSATION_3),
                BindingId="binding-10",
                AccountId=ACCOUNT_ID,
                CustomerId=7,
                ApplicationId="customer-app-7",
                ProviderAppId="provider-app-7",
                AppProfileId="17",
                ConfigVersion=5,
                AgentId="agent-10",
                Status="active",
            )
        )
        session.commit()
    key_file = tmp_path / "client-token-key"
    key_file.write_bytes(b"k" * 32)
    if os.name != "nt":
        key_file.chmod(0o600)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_ENABLED", True)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_CODE_ENABLED", True)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_PROVIDER", "tencent_agsx")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_INSTANCE_ID", "blue")
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_SANDBOX_CLIENT_TOKEN_KEY_FILE",
        str(key_file),
    )
    monkeypatch.setattr(WorkbenchSandboxService, "runtime_guard", _RuntimeGuard)
    _RuntimeGuard.acquired = []
    _RuntimeGuard.released = []
    @asynccontextmanager
    async def state_session_factory():
        with sessions() as session:
            yield _AsyncSessionAdapter(session)

    monkeypatch.setattr(
        WorkbenchSandboxService,
        "state_session_factory",
        state_session_factory,
    )
    yield sessions
    engine.dispose()


async def _create(sessions, provider, *, payload=None, identity=None, app_context=None):
    with sessions() as session:
        return await WorkbenchSandboxService.create_or_reuse(
            _AsyncSessionAdapter(session),
            account_id=ACCOUNT_ID,
            identity=identity or _identity(),
            app_context=app_context or _app_context(),
            payload=payload or {"conversation_id": CONVERSATION_1, "timeout_seconds": 60},
            provider=provider,
        )


@pytest.mark.asyncio
async def test_create_reuses_owner_scope_and_never_projects_provider_secrets(sandbox_store):
    provider = _FakeProvider()
    first = await _create(sandbox_store, provider)
    second = await _create(sandbox_store, provider)

    assert first == second
    assert len(provider.start_tokens) == 1
    assert set(first) == {
        "sandbox_id",
        "conversation_id",
        "status",
        "timeout_seconds",
        "expires_at",
        "created_at",
        "updated_at",
    }
    assert not any("provider" in key or "token" in key or "url" in key for key in first)
    with sandbox_store() as session:
        row = session.execute(select(WorkbenchSandbox)).scalar_one()
        assert row.ClientTokenHash != provider.start_tokens[0]
        assert len(row.ClientTokenHash) == 64


@pytest.mark.asyncio
async def test_expired_blue_green_lease_retries_the_same_client_token(sandbox_store):
    provider = _FakeProvider()
    failed = await _create(sandbox_store, provider)
    with sandbox_store() as session:
        row = session.execute(select(WorkbenchSandbox)).scalar_one()
        row.Status = "provisioning"
        row.ProviderInstanceId = None
        row.LeaseOwner = "green"
        row.LeaseUntil = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        session.commit()

    retried = await _create(sandbox_store, provider)
    assert retried["sandbox_id"] == failed["sandbox_id"]
    assert len(provider.start_tokens) == 2
    assert provider.start_tokens[0] == provider.start_tokens[1]


@pytest.mark.asyncio
async def test_same_color_concurrent_create_does_not_repeat_start(sandbox_store):
    provider = _BlockingProvider()
    first = asyncio.create_task(_create(sandbox_store, provider))
    await provider.started.wait()

    with pytest.raises(WorkbenchSandboxError) as raised:
        await _create(sandbox_store, provider)
    assert raised.value.code == "sandbox_provisioning"
    assert len(provider.start_tokens) == 1
    provider.release_start.set()
    await first


@pytest.mark.asyncio
async def test_active_provisioning_consumes_capacity_for_other_conversations(
    sandbox_store,
):
    provider = _BlockingProvider()
    limits = _app_context(customer_concurrency=1, user_concurrency=2)
    first = asyncio.create_task(
        _create(sandbox_store, provider, app_context=limits)
    )
    await provider.started.wait()

    with pytest.raises(WorkbenchSandboxError) as raised:
        await _create(
            sandbox_store,
            provider,
            payload={"conversation_id": CONVERSATION_2, "timeout_seconds": 60},
            app_context=limits,
        )
    assert raised.value.code == "sandbox_customer_capacity"
    assert len(provider.start_tokens) == 1
    provider.release_start.set()
    await first


@pytest.mark.asyncio
async def test_expired_provisioning_lease_still_consumes_capacity_for_other_conversation(
    sandbox_store,
):
    provider = _BlockingProvider()
    limits = _app_context(customer_concurrency=1, user_concurrency=1)
    first = asyncio.create_task(_create(sandbox_store, provider, app_context=limits))
    await provider.started.wait()
    with sandbox_store() as session:
        row = session.execute(select(WorkbenchSandbox)).scalar_one()
        row.LeaseUntil = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        session.commit()

    with pytest.raises(WorkbenchSandboxError) as raised:
        await _create(
            sandbox_store,
            provider,
            payload={"conversation_id": CONVERSATION_2, "timeout_seconds": 60},
            app_context=limits,
        )
    assert raised.value.code == "sandbox_customer_capacity"
    assert len(provider.start_tokens) == 1
    provider.release_start.set()
    await first


@pytest.mark.asyncio
async def test_user_capacity_blocks_a_second_conversation_without_calling_provider(
    sandbox_store,
):
    provider = _FakeProvider()
    await _create(sandbox_store, provider)

    with pytest.raises(WorkbenchSandboxError) as raised:
        await _create(
            sandbox_store,
            provider,
            payload={"conversation_id": CONVERSATION_2, "timeout_seconds": 60},
        )
    assert raised.value.status_code == 429
    assert raised.value.code == "sandbox_user_capacity"
    assert len(provider.start_tokens) == 1


@pytest.mark.asyncio
async def test_customer_capacity_is_shared_by_different_users(sandbox_store):
    provider = _FakeProvider()
    limits = _app_context(customer_concurrency=1, user_concurrency=2)
    await _create(sandbox_store, provider, app_context=limits)

    with pytest.raises(WorkbenchSandboxError) as raised:
        await _create(
            sandbox_store,
            provider,
            payload={"conversation_id": CONVERSATION_3, "timeout_seconds": 60},
            identity=_identity(user=10, binding="binding-10"),
            app_context=limits,
        )
    assert raised.value.status_code == 429
    assert raised.value.code == "sandbox_customer_capacity"
    assert len(provider.start_tokens) == 1


@pytest.mark.asyncio
async def test_expired_instances_are_marked_terminal_before_capacity_count(sandbox_store):
    provider = _FakeProvider()
    await _create(sandbox_store, provider)
    with sandbox_store() as session:
        row = session.execute(select(WorkbenchSandbox)).scalar_one()
        version_before_expiry = row.Version
        row.ExpiresAt = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        session.commit()

    created = await _create(
        sandbox_store,
        provider,
        payload={"conversation_id": CONVERSATION_2, "timeout_seconds": 60},
    )
    assert created["conversation_id"] == CONVERSATION_2
    assert len(provider.start_tokens) == 2
    with sandbox_store() as session:
        rows = session.execute(
            select(WorkbenchSandbox).order_by(WorkbenchSandbox.ConversationId)
        ).scalars().all()
        assert [row.Status for row in rows] == ["stopped", "running"]
        assert rows[0].Version == version_before_expiry + 1
        expiry = session.execute(
            select(WorkbenchSandboxAudit).where(
                WorkbenchSandboxAudit.SandboxId == rows[0].SandboxId,
                WorkbenchSandboxAudit.EventType == "instance_expired",
            )
        ).scalar_one()
        assert expiry.Outcome == "accepted"


@pytest.mark.asyncio
async def test_stopped_instance_starts_a_new_generation_and_client_token(sandbox_store):
    provider = _FakeProvider()
    first = await _create(sandbox_store, provider)
    with sandbox_store() as session:
        first_generation = session.execute(select(WorkbenchSandbox)).scalar_one().Generation
        await WorkbenchSandboxService.lifecycle(
            _AsyncSessionAdapter(session),
            action="stop",
            sandbox_id=first["sandbox_id"],
            conversation_id=CONVERSATION_1,
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            provider=provider,
        )
    second = await _create(sandbox_store, provider)

    assert second["sandbox_id"] == first["sandbox_id"]
    assert provider.start_tokens[0] != provider.start_tokens[1]
    with sandbox_store() as session:
        row = session.execute(select(WorkbenchSandbox)).scalar_one()
        assert row.Generation == first_generation + 1
        assert row.ProviderInstanceId == "provider-instance-2"


def test_scope_serialization_prevents_separator_collisions():
    identity = _identity()
    first = WorkbenchSandboxService._scope(
        identity,
        SimpleNamespace(application_id="app:a"),
        "b",
    )
    second = WorkbenchSandboxService._scope(
        identity,
        SimpleNamespace(application_id="app"),
        "a:b",
    )
    assert first != second


@pytest.mark.asyncio
async def test_postgres_capacity_and_scope_locks_use_one_stable_order():
    statements = []

    class Db:
        @staticmethod
        def get_bind():
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        @staticmethod
        async def execute(statement, params):
            del statement
            statements.append(params["lock_key"])

    await WorkbenchSandboxService._provisioning_locks(
        Db(),
        identity=_identity(),
        scope=b"canonical-scope",
    )
    assert statements == sorted(statements)
    assert any(":customer:" in key for key in statements)
    assert any(":user:" in key for key in statements)
    assert any(":scope:" in key for key in statements)


@pytest.mark.asyncio
async def test_cross_tenant_lookup_is_indistinguishable_from_missing(sandbox_store):
    provider = _FakeProvider()
    created = await _create(sandbox_store, provider)
    with sandbox_store() as session:
        with pytest.raises(WorkbenchSandboxError) as raised:
            await WorkbenchSandboxService.query(
                _AsyncSessionAdapter(session),
                sandbox_id=created["sandbox_id"],
                conversation_id=CONVERSATION_1,
                account_id=ACCOUNT_ID,
                identity=_identity(customer=8, binding="binding-8"),
                app_context=_app_context(),
                provider=provider,
            )
    assert raised.value.status_code == 404
    assert raised.value.code == "sandbox_not_found"


@pytest.mark.asyncio
async def test_epoch_change_revokes_existing_sandbox(sandbox_store):
    provider = _FakeProvider()
    created = await _create(sandbox_store, provider)
    with sandbox_store() as session:
        with pytest.raises(WorkbenchSandboxError) as raised:
            await WorkbenchSandboxService.query(
                _AsyncSessionAdapter(session),
                sandbox_id=created["sandbox_id"],
                conversation_id=CONVERSATION_1,
                account_id=ACCOUNT_ID,
                identity=_identity(epoch=4),
                app_context=_app_context(epoch=4),
                provider=provider,
            )
    assert raised.value.code == "sandbox_authorization_changed"


@pytest.mark.asyncio
async def test_provider_rate_limit_is_projected_without_original_message(sandbox_store):
    provider = _FakeProvider()
    provider.start_error = SandboxProviderError(
        "provider_rate_limited",
        status_code=429,
        retryable=True,
    )
    with pytest.raises(WorkbenchSandboxError) as raised:
        await _create(sandbox_store, provider)
    assert raised.value.code == "provider_rate_limited"
    assert raised.value.status_code == 429

    provider.start_error = None
    recovered = await _create(sandbox_store, provider)
    assert recovered["status"] == "running"
    assert provider.start_tokens[0] == provider.start_tokens[1]
    with sandbox_store() as session:
        row = session.execute(select(WorkbenchSandbox)).scalar_one()
        assert row.Generation > 0


@pytest.mark.asyncio
async def test_unknown_start_is_owned_audited_and_cannot_be_replaced(sandbox_store):
    provider = _FakeProvider()
    provider.start_error = SandboxProviderError(
        "provider_status_unknown",
        instance_id="provider-instance-unknown",
    )
    with pytest.raises(WorkbenchSandboxError):
        await _create(sandbox_store, provider)

    with sandbox_store() as session:
        row = session.execute(select(WorkbenchSandbox)).scalar_one()
        assert row.Status == "provider_unknown"
        assert row.ProviderInstanceId == "provider-instance-unknown"
        assert session.execute(
            select(WorkbenchSandboxAudit).where(
                WorkbenchSandboxAudit.EventType == "provider_unknown"
            )
        ).scalar_one().Outcome == "unknown"

    provider.start_error = None
    reused = await _create(sandbox_store, provider)
    assert reused["status"] == "provider_unknown"
    assert len(provider.start_tokens) == 1


@pytest.mark.asyncio
async def test_accepted_stop_remains_stopping_until_describe_confirms_terminal(
    sandbox_store,
):
    class DelayedStopProvider(_FakeProvider):
        async def stop(self, instance_id):
            self.stop_calls.append(instance_id)

    provider = DelayedStopProvider()
    created = await _create(sandbox_store, provider)
    with sandbox_store() as session:
        result = await WorkbenchSandboxService.lifecycle(
            _AsyncSessionAdapter(session),
            action="stop",
            sandbox_id=created["sandbox_id"],
            conversation_id=CONVERSATION_1,
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            provider=provider,
        )
    assert result["status"] == "stopping"
    with sandbox_store() as session:
        row = session.execute(select(WorkbenchSandbox)).scalar_one()
        assert row.Status == "stopping"
        events = session.execute(select(WorkbenchSandboxAudit.EventType)).scalars().all()
        assert "stop_requested" in events
        assert "lifecycle_stop" in events


@pytest.mark.asyncio
async def test_concurrent_stop_returns_active_intent_without_stealing_version(
    sandbox_store,
):
    class BlockingStopProvider(_FakeProvider):
        def __init__(self):
            super().__init__()
            self.stop_started = asyncio.Event()
            self.release_stop = asyncio.Event()
            self.describe_calls = 0

        async def stop(self, instance_id):
            self.stop_calls.append(instance_id)
            self.stop_started.set()
            await self.release_stop.wait()

        async def describe(self, instance_id):
            self.describe_calls += 1
            return ProviderInstance(instance_id, "running", "SANDBOX", "TOKEN")

    provider = BlockingStopProvider()
    created = await _create(sandbox_store, provider)
    with sandbox_store() as first_session:
        first = asyncio.create_task(
            WorkbenchSandboxService.lifecycle(
                _AsyncSessionAdapter(first_session),
                action="stop",
                sandbox_id=created["sandbox_id"],
                conversation_id=CONVERSATION_1,
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                provider=provider,
            )
        )
        await provider.stop_started.wait()
        with sandbox_store() as observer:
            version_during_first = observer.execute(
                select(WorkbenchSandbox.Version)
            ).scalar_one()
        with sandbox_store() as second_session:
            second = await WorkbenchSandboxService.lifecycle(
                _AsyncSessionAdapter(second_session),
                action="stop",
                sandbox_id=created["sandbox_id"],
                conversation_id=CONVERSATION_1,
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                provider=provider,
            )
        assert second["status"] == "stopping"
        assert provider.describe_calls == 0
        with sandbox_store() as observer:
            assert observer.execute(select(WorkbenchSandbox.Version)).scalar_one() == version_during_first
        provider.release_stop.set()
        completed = await first
    assert completed["status"] == "stopping"
    assert provider.stop_calls == ["provider-instance-1"]
    assert provider.describe_calls == 1


@pytest.mark.asyncio
async def test_expired_stop_intent_recovers_by_describing_before_resending_stop(
    sandbox_store,
):
    class RecoveryProvider(_FakeProvider):
        def __init__(self):
            super().__init__()
            self.describe_statuses = ["running", "stopped"]

        async def describe(self, instance_id):
            status = self.describe_statuses.pop(0)
            return ProviderInstance(instance_id, status, "SANDBOX", "TOKEN")

    provider = RecoveryProvider()
    created = await _create(sandbox_store, provider)
    with sandbox_store() as session:
        row = session.execute(select(WorkbenchSandbox)).scalar_one()
        row.Status = "stopping"
        row.LeaseOwner = "crashed-blue"
        row.LeaseUntil = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        row.Version += 1
        session.commit()

    with sandbox_store() as session:
        recovered = await WorkbenchSandboxService.lifecycle(
            _AsyncSessionAdapter(session),
            action="stop",
            sandbox_id=created["sandbox_id"],
            conversation_id=CONVERSATION_1,
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            provider=provider,
        )
    assert recovered["status"] == "stopped"
    assert provider.stop_calls == ["provider-instance-1"]
    assert provider.describe_statuses == []
    with sandbox_store() as session:
        events = session.execute(select(WorkbenchSandboxAudit.EventType)).scalars().all()
        assert "stop_recovery_requested" in events
        assert "lifecycle_stop" in events


@pytest.mark.asyncio
async def test_query_failure_persists_provider_unknown_and_audit(sandbox_store):
    provider = _FakeProvider()
    created = await _create(sandbox_store, provider)

    async def unavailable(_instance_id):
        raise SandboxProviderError("provider_control_unavailable", retryable=True)

    provider.describe = unavailable
    with sandbox_store() as session:
        with pytest.raises(WorkbenchSandboxError):
            await WorkbenchSandboxService.query(
                _AsyncSessionAdapter(session),
                sandbox_id=created["sandbox_id"],
                conversation_id=CONVERSATION_1,
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                provider=provider,
            )
    with sandbox_store() as session:
        assert session.execute(select(WorkbenchSandbox)).scalar_one().Status == "provider_unknown"
        assert session.execute(
            select(WorkbenchSandboxAudit).where(
                WorkbenchSandboxAudit.EventType == "query_provider_unknown"
            )
        ).scalar_one().Outcome == "unknown"


@pytest.mark.asyncio
async def test_query_does_not_steal_an_active_lifecycle_transition(sandbox_store):
    class BlockingPauseProvider(_FakeProvider):
        def __init__(self):
            super().__init__()
            self.pause_started = asyncio.Event()
            self.release_pause = asyncio.Event()
            self.describe_calls = 0

        async def pause(self, instance_id):
            self.pause_started.set()
            await self.release_pause.wait()
            return ProviderInstance(instance_id, "paused", "SANDBOX", "TOKEN")

        async def describe(self, instance_id):
            self.describe_calls += 1
            return await super().describe(instance_id)

    provider = BlockingPauseProvider()
    created = await _create(sandbox_store, provider)
    with sandbox_store() as lifecycle_session:
        operation = asyncio.create_task(
            WorkbenchSandboxService.lifecycle(
                _AsyncSessionAdapter(lifecycle_session),
                action="pause",
                sandbox_id=created["sandbox_id"],
                conversation_id=CONVERSATION_1,
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                provider=provider,
            )
        )
        await provider.pause_started.wait()
        with sandbox_store() as query_session:
            queried = await WorkbenchSandboxService.query(
                _AsyncSessionAdapter(query_session),
                sandbox_id=created["sandbox_id"],
                conversation_id=CONVERSATION_1,
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                provider=provider,
            )
        assert queried["status"] == "pausing"
        assert provider.describe_calls == 0
        provider.release_pause.set()
        result = await operation
    assert result["status"] == "paused"


@pytest.mark.asyncio
async def test_automatic_stop_persists_fenced_intent_before_provider_call(
    sandbox_store,
):
    class BlockingStopProvider(_FakeProvider):
        def __init__(self):
            super().__init__()
            self.stop_started = asyncio.Event()
            self.release_stop = asyncio.Event()

        async def stop(self, instance_id):
            self.stop_calls.append(instance_id)
            self.stop_started.set()
            await self.release_stop.wait()

    provider = BlockingStopProvider()
    created = await _create(sandbox_store, provider)
    with sandbox_store() as operation_session:
        sandbox = operation_session.execute(select(WorkbenchSandbox)).scalar_one()
        operation = asyncio.create_task(
            WorkbenchSandboxService._bounded_stop(
                _AsyncSessionAdapter(operation_session),
                sandbox,
                provider,
                event_type="shell_timeout",
                error_code="sandbox_runtime_timeout",
            )
        )
        await provider.stop_started.wait()
        with sandbox_store() as observer_session:
            observed = observer_session.execute(select(WorkbenchSandbox)).scalar_one()
            assert observed.Status == "stopping"
            assert observed.LeaseOwner == "blue"
            assert observed.LeaseUntil is not None
            requested = observer_session.execute(
                select(WorkbenchSandboxAudit).where(
                    WorkbenchSandboxAudit.EventType == "shell_timeout_requested"
                )
            ).scalar_one()
            assert requested.Outcome == "accepted"
        provider.release_stop.set()
        await operation
    with sandbox_store() as session:
        stopped = session.execute(select(WorkbenchSandbox)).scalar_one()
        assert stopped.Status == "stopping"
        assert stopped.LeaseOwner is None
        assert session.execute(
            select(WorkbenchSandboxAudit).where(
                WorkbenchSandboxAudit.EventType == "shell_timeout"
            )
        ).scalar_one().Outcome == "accepted"


@pytest.mark.asyncio
async def test_start_rate_limit_is_database_backed_and_precedes_provider_start(
    sandbox_store,
    monkeypatch,
):
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_SANDBOX_STARTS_PER_USER_MINUTE",
        1,
    )
    provider = _FakeProvider()
    limits = _app_context(customer_concurrency=2, user_concurrency=2)
    first = await _create(sandbox_store, provider, app_context=limits)
    with sandbox_store() as session:
        await WorkbenchSandboxService.lifecycle(
            _AsyncSessionAdapter(session),
            action="stop",
            sandbox_id=first["sandbox_id"],
            conversation_id=CONVERSATION_1,
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=limits,
            provider=provider,
        )
    with pytest.raises(WorkbenchSandboxError) as raised:
        await _create(
            sandbox_store,
            provider,
            payload={"conversation_id": CONVERSATION_2, "timeout_seconds": 60},
            app_context=limits,
        )
    assert raised.value.code == "sandbox_start_rate_limited"
    assert len(provider.start_tokens) == 1


@pytest.mark.asyncio
async def test_random_unowned_conversation_cannot_create_row_or_provider_instance(
    sandbox_store,
):
    provider = _FakeProvider()
    with pytest.raises(WorkbenchSandboxError) as raised:
        await _create(
            sandbox_store,
            provider,
            payload={
                "conversation_id": "44444444-4444-4444-8444-444444444444",
                "timeout_seconds": 60,
            },
        )
    assert raised.value.code == "conversation_not_owned"
    assert raised.value.status_code == 404
    assert provider.start_tokens == []
    with sandbox_store() as session:
        assert session.execute(select(WorkbenchSandbox)).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_output_limit_stops_instance(sandbox_store, monkeypatch):
    provider = _FakeProvider()
    created = await _create(sandbox_store, provider)
    provider.code_result = CodeResult("x" * 20, "")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_MAX_OUTPUT_BYTES", 8)
    with sandbox_store() as session:
        with pytest.raises(WorkbenchSandboxError) as raised:
            await WorkbenchSandboxService.execute_code(
                _AsyncSessionAdapter(session),
                sandbox_id=created["sandbox_id"],
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                payload={
                    "conversation_id": CONVERSATION_1,
                    "code": "print('x')",
                    "language": "python",
                    "timeout_seconds": 10,
                },
                provider=provider,
            )
    assert raised.value.status_code == 413
    assert provider.stop_calls == ["provider-instance-1"]


@pytest.mark.asyncio
async def test_safety_stop_reloads_and_retries_after_a_cas_race(
    sandbox_store,
    monkeypatch,
):
    provider = _FakeProvider()
    created = await _create(sandbox_store, provider)
    original = WorkbenchSandboxService._cas_transition.__func__
    raced = False

    async def conflict_once(cls, db, sandbox, **kwargs):
        nonlocal raced
        if not raced and kwargs["event_type"].endswith("_requested"):
            raced = True
            await db.execute(
                update(WorkbenchSandbox)
                .where(WorkbenchSandbox.Id == sandbox.Id)
                .values(Version=int(sandbox.Version) + 1)
            )
            await db.commit()
            return False
        return await original(cls, db, sandbox, **kwargs)

    monkeypatch.setattr(
        WorkbenchSandboxService,
        "_cas_transition",
        classmethod(conflict_once),
    )
    with sandbox_store() as session:
        row = session.execute(
            select(WorkbenchSandbox).where(
                WorkbenchSandbox.SandboxId == created["sandbox_id"]
            )
        ).scalar_one()
        await WorkbenchSandboxService._bounded_stop(
            _AsyncSessionAdapter(session),
            row,
            provider,
            event_type="output_limit",
            error_code="output_limit_exceeded",
        )

    assert raced is True
    assert provider.stop_calls == ["provider-instance-1"]


@pytest.mark.asyncio
async def test_code_execution_is_separately_disabled_before_provider_use(
    sandbox_store,
    monkeypatch,
):
    provider = _FakeProvider()
    created = await _create(sandbox_store, provider)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_CODE_ENABLED", False)
    with sandbox_store() as session:
        with pytest.raises(WorkbenchSandboxError) as raised:
            await WorkbenchSandboxService.execute_code(
                _AsyncSessionAdapter(session),
                sandbox_id=created["sandbox_id"],
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                payload={
                    "conversation_id": CONVERSATION_1,
                    "code": "print('closed')",
                },
                provider=provider,
            )
    assert raised.value.code == "sandbox_code_disabled"


def test_code_feature_requires_global_flag_and_exact_sandbox_capability(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_ENABLED", True)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_CODE_ENABLED", False)
    assert WorkbenchSandboxService.code_execution_available(_app_context()) is False
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_CODE_ENABLED", True)
    assert WorkbenchSandboxService.code_execution_available(_app_context()) is True
    assert WorkbenchSandboxService.code_execution_available(
        _app_context(capabilities=("chat", "files"))
    ) is False


@pytest.mark.asyncio
async def test_code_execution_requires_recent_browser_reauthentication(monkeypatch):
    recent = AsyncMock()
    monkeypatch.setattr(CoreWorkbenchIdentity, "require_browser_session", recent)
    await WorkbenchSandboxService.require_recent_reauthentication(
        SimpleNamespace(),
        account_id=ACCOUNT_ID,
        identity=_identity(),
        session_claims={"jti": "session-1"},
    )
    recent.assert_awaited_once_with(
        ANY,
        claims={"jti": "session-1"},
        account_id=ACCOUNT_ID,
        identity=ANY,
        maximum_age_seconds=tagentic_config.WORKBENCH_SANDBOX_REAUTH_SECONDS,
    )

    recent.side_effect = WorkbenchIdentityError("stale", 401)
    with pytest.raises(WorkbenchSandboxError) as raised:
        await WorkbenchSandboxService.require_recent_reauthentication(
            SimpleNamespace(),
            account_id=ACCOUNT_ID,
            identity=_identity(),
            session_claims={"jti": "session-1"},
        )
    assert raised.value.code == "sandbox_reauth_required"
    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_stream_cancellation_stops_instance(sandbox_store):
    provider = _FakeProvider()
    created = await _create(sandbox_store, provider)
    with sandbox_store() as session:
        stream, _lease, _timeout, _instance_id = await WorkbenchSandboxService.stream_command(
            _AsyncSessionAdapter(session),
            sandbox_id=created["sandbox_id"],
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            payload={
                "conversation_id": CONVERSATION_1,
                "command": "echo test",
                "timeout_seconds": 10,
            },
            provider=provider,
        )
        assert (await anext(stream)).data == "first"
        await stream.aclose()
    assert provider.stop_calls == ["provider-instance-1"]
    with sandbox_store() as session:
        row = session.execute(select(WorkbenchSandbox)).scalar_one()
        assert row.Status == "stopping"
        audit = session.execute(
            select(WorkbenchSandboxAudit).where(
                WorkbenchSandboxAudit.EventType == "stream_cancelled"
            )
        ).scalar_one()
        assert audit.Outcome == "accepted"


@pytest.mark.asyncio
async def test_stream_close_failure_still_stops_instance(sandbox_store):
    class FailingCloseStream:
        def __init__(self):
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return CommandChunk("stdout", "first")

        async def aclose(self):
            raise RuntimeError("provider close failed")

    provider = _FakeProvider()
    provider.stream_command = lambda *_args, **_kwargs: FailingCloseStream()
    created = await _create(sandbox_store, provider)
    with sandbox_store() as session:
        stream, _lease, _timeout, _instance_id = await WorkbenchSandboxService.stream_command(
            _AsyncSessionAdapter(session),
            sandbox_id=created["sandbox_id"],
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            payload={
                "conversation_id": CONVERSATION_1,
                "command": "echo test",
                "timeout_seconds": 10,
            },
            provider=provider,
        )
        assert (await anext(stream)).data == "first"
        await stream.aclose()

    assert provider.stop_calls == ["provider-instance-1"]


@pytest.mark.asyncio
async def test_cancelled_sync_shell_is_stopped_with_cas_and_audited(sandbox_store):
    class BlockingCommandProvider(_FakeProvider):
        def __init__(self):
            super().__init__()
            self.command_started = asyncio.Event()

        async def run_command(self, instance_id, **kwargs):
            del instance_id, kwargs
            self.command_started.set()
            await asyncio.Event().wait()

    provider = BlockingCommandProvider()
    created = await _create(sandbox_store, provider)
    with sandbox_store() as session:
        operation = asyncio.create_task(
            WorkbenchSandboxService.run_command(
                _AsyncSessionAdapter(session),
                sandbox_id=created["sandbox_id"],
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                payload={
                    "conversation_id": CONVERSATION_1,
                    "command": "sleep 60",
                    "timeout_seconds": 60,
                },
                provider=provider,
            )
        )
        await provider.command_started.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
    with sandbox_store() as session:
        row = session.execute(select(WorkbenchSandbox)).scalar_one()
        assert row.Status == "stopping"
        assert session.execute(
            select(WorkbenchSandboxAudit).where(
                WorkbenchSandboxAudit.EventType == "shell_cancelled"
            )
        ).scalar_one().Outcome == "accepted"


@pytest.mark.asyncio
async def test_cancelled_code_execution_stops_and_records_unknown_provider_state(
    sandbox_store,
):
    class BlockingCodeProvider(_FakeProvider):
        def __init__(self):
            super().__init__()
            self.code_started = asyncio.Event()

        async def execute_code(self, instance_id, **kwargs):
            del instance_id, kwargs
            self.code_started.set()
            await asyncio.Event().wait()

        async def stop(self, instance_id):
            self.stop_calls.append(instance_id)
            raise SandboxProviderError("provider_control_unavailable")

    provider = BlockingCodeProvider()
    created = await _create(sandbox_store, provider)
    with sandbox_store() as session:
        operation = asyncio.create_task(
            WorkbenchSandboxService.execute_code(
                _AsyncSessionAdapter(session),
                sandbox_id=created["sandbox_id"],
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                payload={
                    "conversation_id": CONVERSATION_1,
                    "code": "while True: pass",
                    "language": "python",
                    "timeout_seconds": 60,
                },
                provider=provider,
            )
        )
        await provider.code_started.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
    assert provider.stop_calls == ["provider-instance-1"]
    with sandbox_store() as session:
        row = session.execute(select(WorkbenchSandbox)).scalar_one()
        assert row.Status == "provider_unknown"
        audit = session.execute(
            select(WorkbenchSandboxAudit).where(
                WorkbenchSandboxAudit.EventType == "code_cancelled"
            )
        ).scalar_one()
        assert audit.Outcome == "unknown"


@pytest.mark.asyncio
async def test_code_response_loss_stops_instead_of_reusing_uncertain_instance(
    sandbox_store,
):
    class ResponseLossProvider(_FakeProvider):
        async def execute_code(self, instance_id, **kwargs):
            del instance_id, kwargs
            raise SandboxProviderError("sandbox_runtime_unknown")

        async def stop(self, instance_id):
            self.stop_calls.append(instance_id)
            raise SandboxProviderError("provider_control_unavailable")

    provider = ResponseLossProvider()
    created = await _create(sandbox_store, provider)
    with sandbox_store() as session:
        with pytest.raises(WorkbenchSandboxError) as raised:
            await WorkbenchSandboxService.execute_code(
                _AsyncSessionAdapter(session),
                sandbox_id=created["sandbox_id"],
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                payload={
                    "conversation_id": CONVERSATION_1,
                    "code": "side_effect()",
                    "language": "python",
                    "timeout_seconds": 10,
                },
                provider=provider,
            )
    assert raised.value.code == "sandbox_runtime_unknown"
    assert provider.stop_calls == ["provider-instance-1"]
    with sandbox_store() as session:
        row = session.execute(select(WorkbenchSandbox)).scalar_one()
        assert row.Status == "provider_unknown"
        assert session.execute(
            select(WorkbenchSandboxAudit).where(
                WorkbenchSandboxAudit.EventType == "code_unknown"
            )
        ).scalar_one().Outcome == "unknown"


@pytest.mark.asyncio
async def test_file_operations_use_plan_limit_and_independent_runtime_leases(
    sandbox_store,
):
    provider = _FakeProvider()
    created = await _create(sandbox_store, provider)
    with sandbox_store() as session:
        data = await WorkbenchSandboxService.read_file(
            _AsyncSessionAdapter(session),
            sandbox_id=created["sandbox_id"],
            conversation_id=CONVERSATION_1,
            path="notes/read.txt",
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            provider=provider,
        )
    assert data == b"file"
    assert provider.read_kwargs["max_bytes"] == 1024
    with sandbox_store() as session:
        await WorkbenchSandboxService.write_file(
            _AsyncSessionAdapter(session),
            sandbox_id=created["sandbox_id"],
            conversation_id=CONVERSATION_1,
            path="notes/write.txt",
            data=b"bounded",
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            provider=provider,
        )
    assert [entry["operation"] for entry in _RuntimeGuard.acquired[-2:]] == [
        "sandbox_file_read",
        "sandbox_file_write",
    ]
    assert len(_RuntimeGuard.released) >= 2
    with sandbox_store() as session:
        audit_types = set(
            session.execute(select(WorkbenchSandboxAudit.EventType)).scalars().all()
        )
        assert {"file_read", "file_write"}.issubset(audit_types)


@pytest.mark.asyncio
async def test_successful_shell_code_and_stream_operations_are_audited(sandbox_store):
    class FiniteStreamProvider(_FakeProvider):
        async def _stream(self):
            yield CommandChunk("stdout", "done")
            yield CommandChunk("exit", "", 0)

    provider = FiniteStreamProvider()
    created = await _create(sandbox_store, provider)
    with sandbox_store() as session:
        await WorkbenchSandboxService.execute_code(
            _AsyncSessionAdapter(session),
            sandbox_id=created["sandbox_id"],
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            payload={
                "conversation_id": CONVERSATION_1,
                "code": "print('bounded')",
                "language": "python",
                "timeout_seconds": 10,
            },
            provider=provider,
        )
    with sandbox_store() as session:
        await WorkbenchSandboxService.run_command(
            _AsyncSessionAdapter(session),
            sandbox_id=created["sandbox_id"],
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            payload={
                "conversation_id": CONVERSATION_1,
                "command": "printf bounded",
                "timeout_seconds": 10,
            },
            provider=provider,
        )
    with sandbox_store() as session:
        stream, lease, _, _ = await WorkbenchSandboxService.stream_command(
            _AsyncSessionAdapter(session),
            sandbox_id=created["sandbox_id"],
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            payload={
                "conversation_id": CONVERSATION_1,
                "command": "printf bounded",
                "timeout_seconds": 10,
            },
            provider=provider,
        )
        assert [chunk.type if hasattr(chunk, "type") else chunk.stream async for chunk in stream]
        await WorkbenchSandboxService.runtime_guard.release(lease)
    with sandbox_store() as session:
        audit_types = set(
            session.execute(select(WorkbenchSandboxAudit.EventType)).scalars().all()
        )
        assert {"code_execute", "shell_execute", "shell_stream"}.issubset(audit_types)


def test_paths_are_workspace_relative(sandbox_store):
    assert WorkbenchSandboxService.sandbox_path("folder/file.txt") == "/workspace/folder/file.txt"
    for value in ("/etc/passwd", "../secret", "folder\\file", "folder/../secret"):
        with pytest.raises(WorkbenchSandboxError):
            WorkbenchSandboxService.sandbox_path(value)

@pytest.mark.asyncio
async def test_pty_ticket_is_one_use_hash_only_and_owner_scoped(
    sandbox_store,
    monkeypatch,
):
    created = await _create(sandbox_store, _FakeProvider())
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_PTY_ENABLED", True)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_PTY_GLOBAL_CAPACITY", 1)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_PTY_CUSTOMER_CAPACITY", 1)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_PTY_USER_CAPACITY", 1)
    claims = {"sid": "s" * 48}
    payload = {
        "conversation_id": CONVERSATION_1,
        "rows": 24,
        "cols": 80,
        "timeout_seconds": 60,
    }
    with sandbox_store() as session:
        first = await WorkbenchSandboxPtyService.mint_ticket(
            _AsyncSessionAdapter(session),
            sandbox_id=created["sandbox_id"],
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            claims=claims,
            payload=payload,
        )
    assert set(first) == {"pty_session_id", "ticket", "expires_at", "protocol"}
    assert first["protocol"] == "claw-workbench-pty-v1"
    with sandbox_store() as session:
        row = session.execute(
            select(WorkbenchSandboxPty).where(
                WorkbenchSandboxPty.PtySessionId == first["pty_session_id"]
            )
        ).scalar_one()
        assert row.TicketHash == __import__("hashlib").sha256(
            first["ticket"].encode("ascii")
        ).hexdigest()
        assert first["ticket"] not in repr(row.__dict__)
        assert row.ProviderPid is None

    @asynccontextmanager
    async def state_db():
        with sandbox_store() as session:
            yield _AsyncSessionAdapter(session)

    monkeypatch.setattr("core.workbench_sandbox.pty.db_connection", state_db)
    monkeypatch.setattr(
        "core.workbench_sandbox.pty.WorkbenchRuntimeGuard.acquire",
        AsyncMock(return_value=RuntimeLease("pty-lease", 120)),
    )
    with sandbox_store() as session:
        connection = await WorkbenchSandboxPtyService.consume_ticket(
            _AsyncSessionAdapter(session),
            sandbox_id=created["sandbox_id"],
            ticket=first["ticket"],
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            claims=claims,
        )
    assert connection.pty_session_id == first["pty_session_id"]

    with sandbox_store() as session:
        second = await WorkbenchSandboxPtyService.mint_ticket(
            _AsyncSessionAdapter(session),
            sandbox_id=created["sandbox_id"],
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            claims=claims,
            payload=payload,
        )
    with sandbox_store() as session:
        with pytest.raises(WorkbenchSandboxPtyError) as at_capacity:
            await WorkbenchSandboxPtyService.consume_ticket(
                _AsyncSessionAdapter(session),
                sandbox_id=created["sandbox_id"],
                ticket=second["ticket"],
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                claims=claims,
            )
    assert at_capacity.value.code == "pty_capacity"

    with sandbox_store() as session:
        with pytest.raises(WorkbenchSandboxPtyError) as replayed:
            await WorkbenchSandboxPtyService.consume_ticket(
                _AsyncSessionAdapter(session),
                sandbox_id=created["sandbox_id"],
                ticket=first["ticket"],
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                claims=claims,
            )
    assert replayed.value.code == "pty_ticket_replayed"


def test_pty_subprotocol_never_accepts_ticket_from_an_ambiguous_header():
    ticket = "a" * 43
    assert WorkbenchSandboxPtyService.parse_protocol_header(
        f"claw-workbench-pty-v1, ticket.{ticket}"
    ) == ticket
    for header in (
        None,
        f"ticket.{ticket}",
        f"claw-workbench-pty-v1, ticket.{ticket}, extra",
        "claw-workbench-pty-v1, ticket.short",
    ):
        with pytest.raises(WorkbenchSandboxPtyError):
            WorkbenchSandboxPtyService.parse_protocol_header(header)


@pytest.mark.asyncio
async def test_pty_provider_queue_overflow_becomes_one_terminal_sentinel():
    queue = asyncio.Queue(maxsize=1)
    callback = TencentAGSXProvider._pty_callback(queue)
    await callback(b"first")
    await callback(b"overflow")
    sentinel = queue.get_nowait()
    assert isinstance(sentinel, SandboxProviderError)
    assert sentinel.code == "pty_output_queue_exceeded"
    await callback(b"ignored")
    assert queue.empty()


@pytest.mark.asyncio
async def test_pty_provider_rejects_an_oversized_callback_before_copy(monkeypatch):
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_SANDBOX_PTY_MAX_OUTPUT_FRAME_BYTES",
        8,
    )
    queue = asyncio.Queue(maxsize=2)
    callback = TencentAGSXProvider._pty_callback(queue)

    await callback(bytearray(b"x" * 9))

    sentinel = queue.get_nowait()
    assert isinstance(sentinel, SandboxProviderError)
    assert sentinel.code == "pty_output_frame_exceeded"


@pytest.mark.asyncio
async def test_normal_pty_close_kills_only_the_pty_and_releases_the_lease(monkeypatch):
    class Handle:
        pid = 17
        killed = False

        async def _output(self):
            await asyncio.Event().wait()
            yield b""

        def output(self):
            return self._output()

        async def wait(self):
            await asyncio.Event().wait()

        async def send_input(self, _data):
            return None

        async def resize(self, **_kwargs):
            return None

        async def kill(self):
            self.killed = True
            return True

    class Provider:
        async def create_pty(self, *_args, **_kwargs):
            return handle

    class WebSocket:
        sent = []

        async def send(self, value):
            self.sent.append(value)

        async def recv(self):
            return '{"type":"close"}'

    handle = Handle()
    websocket = WebSocket()
    monkeypatch.setattr(WorkbenchSandboxPtyService, "_set_state", AsyncMock(return_value=True))
    abnormal = AsyncMock()
    monkeypatch.setattr(WorkbenchSandboxPtyService, "_abnormal_cleanup", abnormal)
    release = AsyncMock()
    monkeypatch.setattr("core.workbench_sandbox.pty.WorkbenchRuntimeGuard.release", release)
    connection = WorkbenchPtyConnection(
        pty_session_id="pty_" + "1" * 32,
        sandbox_id="sbx_" + "2" * 32,
        sandbox_generation=1,
        provider_instance_id="provider-1",
        conversation_id=CONVERSATION_1,
        application_id="customer-app-7",
        app_profile_id="17",
        config_version=5,
        deadline_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=30),
        rows=24,
        cols=80,
        lease=RuntimeLease("lease-1", 30),
    )
    await WorkbenchSandboxPtyService.serve(
        websocket,
        connection=connection,
        claims={},
        provider=Provider(),
    )
    assert handle.killed is True
    abnormal.assert_not_awaited()
    release.assert_awaited_once_with(connection.lease)
    assert any('"type": "ready"' in item for item in websocket.sent if isinstance(item, str))
    assert any('"type": "exit"' in item for item in websocket.sent if isinstance(item, str))


@pytest.mark.asyncio
async def test_pty_cleanup_failure_cannot_leak_runtime_lease(monkeypatch):
    connection = WorkbenchPtyConnection(
        pty_session_id="pty_" + "a" * 32,
        sandbox_id="sbx_" + "b" * 32,
        sandbox_generation=1,
        provider_instance_id="provider-instance-1",
        conversation_id=CONVERSATION_1,
        application_id="customer-app-7",
        app_profile_id="17",
        config_version=5,
        deadline_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=60),
        rows=24,
        cols=80,
        lease=RuntimeLease("pty-lease-release", 60),
    )
    provider = _FakeProvider()
    provider.create_pty = AsyncMock(
        side_effect=SandboxProviderError("provider_failed")
    )
    cleanup = AsyncMock(side_effect=RuntimeError("cleanup failed"))
    release = AsyncMock()
    monkeypatch.setattr(WorkbenchSandboxPtyService, "_abnormal_cleanup", cleanup)
    monkeypatch.setattr(
        "core.workbench_sandbox.pty.WorkbenchRuntimeGuard.release",
        release,
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await WorkbenchSandboxPtyService.serve(
            SimpleNamespace(send=AsyncMock()),
            connection=connection,
            claims={"sid": "s" * 48},
            provider=provider,
        )

    cleanup.assert_awaited_once()
    release.assert_awaited_once_with(connection.lease)


@pytest.mark.asyncio
async def test_official_control_sdk_request_is_token_authenticated_and_metadata_is_opaque(
    monkeypatch,
):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_AGSX_REGION", "ap-guangzhou")
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_AGSX_DOMAIN",
        "ap-guangzhou.tencentags.com",
    )
    monkeypatch.setattr(tagentic_config, "WORKBENCH_AGSX_TOOL_ID", "tool-1")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_AGSX_TOOL_NAME", "sandbox-tool")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_NETWORK_MODE", "SANDBOX")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_AUTH_MODE", "TOKEN")
    requests = []

    class Client:
        def StartSandboxInstance(self, request):
            requests.append(request)
            return SimpleNamespace(
                Instance=SimpleNamespace(
                    InstanceId="provider-instance-1",
                    Status="RUNNING",
                    NetworkMode="SANDBOX",
                        AuthMode="TOKEN",
                        ToolId="tool-1",
                        ToolName="sandbox-tool",
                        Persistent=False,
                        ExpiresAt=None,
                )
            )

    async def control_call(operation, **kwargs):
        del kwargs
        return operation()

    monkeypatch.setattr(
        TencentAGSXProvider,
        "_cloud_client",
        classmethod(lambda cls: Client()),
    )
    monkeypatch.setattr(
        TencentAGSXProvider,
        "_control_call",
        classmethod(
            lambda cls, operation, **kwargs: control_call(operation, **kwargs)
        ),
    )
    result = await TencentAGSXProvider().start(
        client_token="wb_token",
        timeout_seconds=60,
        metadata={"scope_hash": "a" * 64},
    )

    assert result.instance_id == "provider-instance-1"
    assert requests[0].AuthMode == "TOKEN"
    assert requests[0].Timeout == "60s"
    assert requests[0].ClientToken == "wb_token"
    assert requests[0].ToolId == "tool-1"
    assert requests[0].ToolName == "sandbox-tool"
    assert [(item.Name, item.Value) for item in requests[0].Metadata] == [
        ("scope_hash", "a" * 64)
    ]


@pytest.mark.asyncio
async def test_unknown_provider_status_is_failed_closed_and_start_is_cleaned_up(monkeypatch):
    _configure_provider(monkeypatch)

    class Client:
        def StartSandboxInstance(self, request):
            del request
            return SimpleNamespace(
                Instance=SimpleNamespace(
                    InstanceId="provider-instance-unknown",
                    Status="NEW_UNDOCUMENTED_STATE",
                    NetworkMode="SANDBOX",
                    AuthMode="TOKEN",
                    ExpiresAt=None,
                )
            )

    async def control_call(operation, **kwargs):
        del kwargs
        return operation()

    monkeypatch.setattr(
        TencentAGSXProvider,
        "_cloud_client",
        classmethod(lambda cls: Client()),
    )
    monkeypatch.setattr(
        TencentAGSXProvider,
        "_control_call",
        classmethod(
            lambda cls, operation, **kwargs: control_call(operation, **kwargs)
        ),
    )
    provider = TencentAGSXProvider()
    provider.stop = AsyncMock()
    with pytest.raises(SandboxProviderError) as raised:
        await provider.start(
            client_token="wb_token",
            timeout_seconds=60,
            metadata={},
        )
    assert raised.value.code == "provider_status_unknown"
    assert raised.value.instance_id == "provider-instance-unknown"
    provider.stop.assert_awaited_once_with("provider-instance-unknown")


@pytest.mark.asyncio
async def test_describe_rejects_a_different_instance_than_requested(monkeypatch):
    _configure_provider(monkeypatch)

    class Client:
        def DescribeSandboxInstanceList(self, request):
            del request
            return SimpleNamespace(
                InstanceSet=[SimpleNamespace(InstanceId="provider-instance-other")]
            )

    async def control_call(operation, **kwargs):
        del kwargs
        return operation()

    monkeypatch.setattr(
        TencentAGSXProvider,
        "_cloud_client",
        classmethod(lambda cls: Client()),
    )
    monkeypatch.setattr(
        TencentAGSXProvider,
        "_control_call",
        classmethod(lambda cls, operation, **kwargs: control_call(operation, **kwargs)),
    )
    with pytest.raises(SandboxProviderError) as raised:
        await TencentAGSXProvider().describe("provider-instance-requested")
    assert raised.value.code == "provider_instance_mismatch"


def test_provider_instance_requires_exact_tool_nonpersistent_and_safe_identifier(monkeypatch):
    _configure_provider(monkeypatch)
    valid = ProviderInstance(
        "provider-instance-1",
        "running",
        "SANDBOX",
        "TOKEN",
        tool_id="tool-1",
        tool_name="sandbox-tool",
        persistent=False,
    )
    TencentAGSXProvider._validate_provider_instance(valid)
    invalid = (
        ProviderInstance(
            "provider instance with spaces",
            "running",
            "SANDBOX",
            "TOKEN",
            tool_id="tool-1",
            tool_name="sandbox-tool",
            persistent=False,
        ),
        ProviderInstance(
            "provider-instance-1",
            "running",
            "SANDBOX",
            "TOKEN",
            tool_id="other-tool",
            tool_name="sandbox-tool",
            persistent=False,
        ),
        ProviderInstance(
            "provider-instance-1",
            "running",
            "SANDBOX",
            "TOKEN",
            tool_id="tool-1",
            tool_name="sandbox-tool",
            persistent=True,
        ),
    )
    for instance in invalid:
        with pytest.raises(SandboxProviderError):
            TencentAGSXProvider._validate_provider_instance(instance)


def test_provider_accepts_either_tool_selector_but_checks_configured_values(monkeypatch):
    _configure_provider(monkeypatch)
    returned = ProviderInstance(
        "provider-instance-1",
        "running",
        "SANDBOX",
        "TOKEN",
        tool_id="tool-1",
        tool_name="sandbox-tool",
        persistent=False,
    )

    monkeypatch.setattr(tagentic_config, "WORKBENCH_AGSX_TOOL_NAME", "")
    TencentAGSXProvider.validate_configuration()
    TencentAGSXProvider._validate_provider_instance(returned)

    monkeypatch.setattr(tagentic_config, "WORKBENCH_AGSX_TOOL_ID", "")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_AGSX_TOOL_NAME", "sandbox-tool")
    TencentAGSXProvider.validate_configuration()
    TencentAGSXProvider._validate_provider_instance(returned)

    monkeypatch.setattr(tagentic_config, "WORKBENCH_AGSX_TOOL_NAME", "")
    with pytest.raises(SandboxProviderError) as raised:
        TencentAGSXProvider.validate_configuration()
    assert raised.value.code == "provider_tool_not_configured"


@pytest.mark.asyncio
async def test_stream_queue_backpressures_and_cancels_the_sdk_producer(monkeypatch):
    _configure_provider(monkeypatch)

    class Commands:
        def __init__(self):
            self.produced = 0
            self.cancelled = asyncio.Event()
            self.command = ""

        async def run(self, command, *, on_stdout, on_stderr, **kwargs):
            del on_stderr, kwargs
            self.command = command
            try:
                for index in range(TencentAGSXProvider._STREAM_QUEUE_SIZE + 20):
                    await on_stdout(f"chunk-{index}")
                    self.produced += 1
                return SimpleNamespace(exit_code=0)
            finally:
                self.cancelled.set()

    commands = Commands()

    async def connect(cls, instance_id):
        del cls, instance_id
        return SimpleNamespace(commands=commands)

    monkeypatch.setattr(TencentAGSXProvider, "_connect", classmethod(connect))
    provider = TencentAGSXProvider()
    stream = provider.stream_command(
        "provider-instance-1",
        command="printf bounded-output",
        cwd="/workspace",
        timeout_seconds=60,
    )
    assert (await anext(stream)).data == "chunk-0"
    await asyncio.sleep(0.05)
    assert commands.produced <= TencentAGSXProvider._STREAM_QUEUE_SIZE + 1
    assert "printf bounded-output" not in commands.command
    assert commands.command.startswith("python3 -c ")
    await stream.aclose()
    await asyncio.wait_for(commands.cancelled.wait(), timeout=1)


@pytest.mark.asyncio
async def test_code_worker_uses_official_run_code_and_projects_text_only(monkeypatch):
    calls = {}
    rich_result = SimpleNamespace(text="42", html="<b>42</b>", png="image-data")

    class Sandbox:
        async def run_code(self, code, **kwargs):
            calls["run_code"] = (code, kwargs)
            kwargs["on_stdout"](SimpleNamespace(line="hello\n"))
            kwargs["on_stderr"](SimpleNamespace(line="warning\n"))
            kwargs["on_result"](rich_result)
            return SimpleNamespace()

    connect = AsyncMock(return_value=Sandbox())
    monkeypatch.setattr("e2b_code_interpreter.AsyncSandbox.connect", connect)
    monkeypatch.setenv(code_worker._API_KEY_ENV, "ark_0123456789abcdef")
    request = {
        "instance_id": "provider-instance-1",
        "code": "print('hello')\n21 * 2",
        "language": "python",
        "domain": "ap-guangzhou.tencentags.com",
        "timeout_seconds": 60,
        "request_timeout_seconds": 30,
        "max_output_bytes": 1024,
    }
    response = await code_worker._execute(request)

    assert response == {
        "version": 1,
        "ok": True,
        "stdout": "hello\n",
        "stderr": "warning\n",
        "results": ["42"],
        "error": None,
    }
    connect.assert_awaited_once_with(
        sandbox_id="provider-instance-1",
        api_key="ark_0123456789abcdef",
        validate_api_key=False,
        domain="ap-guangzhou.tencentags.com",
        request_timeout=30,
    )
    code, kwargs = calls["run_code"]
    assert code == "print('hello')\n21 * 2"
    assert kwargs["language"] == "python"
    assert kwargs["timeout"] == 60
    assert kwargs["request_timeout"] == 30
    assert rich_result.html is None
    assert rich_result.png is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "language",
    ["python", "javascript", "typescript", "r", "java", "bash"],
)
async def test_code_worker_accepts_exact_official_languages(monkeypatch, language):
    class Sandbox:
        async def run_code(self, code, **kwargs):
            del code, kwargs

    monkeypatch.setattr(
        "e2b_code_interpreter.AsyncSandbox.connect",
        AsyncMock(return_value=Sandbox()),
    )
    monkeypatch.setenv(code_worker._API_KEY_ENV, "ark_0123456789abcdef")
    response = await code_worker._execute(
        {
            "instance_id": "provider-instance-1",
            "code": "1",
            "language": language,
            "domain": "ap-guangzhou.tencentags.com",
            "timeout_seconds": 10,
            "request_timeout_seconds": 10,
            "max_output_bytes": 1024,
        }
    )
    assert response["ok"] is True


@pytest.mark.asyncio
async def test_code_worker_rejects_single_frame_and_cumulative_output(monkeypatch):
    class SingleFrameSandbox:
        async def run_code(self, code, **kwargs):
            del code
            kwargs["on_result"](SimpleNamespace(text=None, png="x" * 9))

    connect = AsyncMock(return_value=SingleFrameSandbox())
    monkeypatch.setattr("e2b_code_interpreter.AsyncSandbox.connect", connect)
    monkeypatch.setenv(code_worker._API_KEY_ENV, "ark_0123456789abcdef")
    base = {
        "instance_id": "provider-instance-1",
        "code": "1",
        "language": "python",
        "domain": "ap-guangzhou.tencentags.com",
        "timeout_seconds": 10,
        "request_timeout_seconds": 10,
        "max_output_bytes": 8,
    }
    assert (await code_worker._execute(base))["code"] == "output_limit_exceeded"

    class CumulativeSandbox:
        async def run_code(self, code, **kwargs):
            del code
            kwargs["on_stdout"](SimpleNamespace(line="12345"))
            kwargs["on_stderr"](SimpleNamespace(line="6789"))

    connect.return_value = CumulativeSandbox()
    monkeypatch.setenv(code_worker._API_KEY_ENV, "ark_0123456789abcdef")
    assert (await code_worker._execute(base))["code"] == "output_limit_exceeded"


@pytest.mark.asyncio
async def test_code_worker_distinguishes_pre_submit_connect_failure_from_response_loss(
    monkeypatch,
):
    connect = AsyncMock(side_effect=RuntimeError("connect failed"))
    monkeypatch.setattr("e2b_code_interpreter.AsyncSandbox.connect", connect)
    monkeypatch.setenv(code_worker._API_KEY_ENV, "ark_0123456789abcdef")
    request = {
        "instance_id": "provider-instance-1",
        "code": "side_effect()",
        "language": "python",
        "domain": "ap-guangzhou.tencentags.com",
        "timeout_seconds": 10,
        "request_timeout_seconds": 10,
        "max_output_bytes": 1024,
    }
    assert (await code_worker._execute(request))["code"] == "provider_data_unavailable"

    class ResponseLossSandbox:
        async def run_code(self, code, **kwargs):
            del code, kwargs
            raise RuntimeError("response lost after submit")

    connect.side_effect = None
    connect.return_value = ResponseLossSandbox()
    monkeypatch.setenv(code_worker._API_KEY_ENV, "ark_0123456789abcdef")
    assert (await code_worker._execute(request))["code"] == "sandbox_runtime_unknown"


@pytest.mark.asyncio
async def test_provider_passes_only_minimal_environment_to_code_worker(monkeypatch):
    _configure_provider(monkeypatch)
    monkeypatch.setattr("core.workbench_sandbox.provider.os.name", "posix")
    monkeypatch.setenv("DATABASE_URL", "postgresql://sensitive")
    monkeypatch.setenv("WORKBENCH_SESSION_KEY", "sensitive-session")
    monkeypatch.setenv("OAUTH_CLIENT_SECRET", "sensitive-oauth")
    monkeypatch.setattr(
        TencentAGSXProvider,
        "_secret_file",
        staticmethod(lambda path, name: "ark_0123456789abcdef"),
    )
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_MAX_OUTPUT_BYTES", 1024)
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_SANDBOX_CODE_WORKER_MEMORY_BYTES",
        512 * 1024 * 1024,
    )

    class Reader:
        def __init__(self, data=b""):
            self.data = data

        async def read(self, size):
            del size
            data, self.data = self.data, b""
            return data

    class Writer:
        def __init__(self):
            self.data = b""

        def write(self, data):
            self.data += data

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

    class Process:
        def __init__(self):
            self.stdin = Writer()
            self.stdout = Reader(
                b'{"version":1,"ok":true,"stdout":"ok","stderr":"",'
                b'"results":[],"error":null}'
            )
            self.stderr = Reader(b"provider stderr must never be returned")
            self.returncode = 0

        async def wait(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    process = Process()
    create_subprocess = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)

    result = await TencentAGSXProvider().execute_code(
        "provider-instance-1",
        code="print('ok')",
        language="python",
        timeout_seconds=10,
    )
    assert result == CodeResult("ok", "", (), None)
    environment = create_subprocess.await_args.kwargs["env"]
    assert set(environment) == {
        code_worker._API_KEY_ENV,
        "PYTHONNOUSERSITE",
        "PYTHONUNBUFFERED",
        "PYTHONPATH",
        "LANG",
        "LC_ALL",
    }
    assert environment[code_worker._API_KEY_ENV] == "ark_0123456789abcdef"
    assert environment["PYTHONPATH"].endswith("server")
    assert "DATABASE_URL" not in environment
    assert "WORKBENCH_SESSION_KEY" not in environment
    assert "OAUTH_CLIENT_SECRET" not in environment
    assert "provider stderr" not in result.stdout
    assert "provider stderr" not in result.stderr

def test_code_worker_applies_memory_cpu_core_and_fd_limits(monkeypatch):
    calls = []
    resource = SimpleNamespace(
        RLIMIT_AS=1,
        RLIMIT_CPU=2,
        RLIMIT_CORE=3,
        RLIMIT_NOFILE=4,
        setrlimit=lambda kind, value: calls.append((kind, value)),
    )
    monkeypatch.setattr(code_worker.os, "name", "posix")
    monkeypatch.setitem(sys.modules, "resource", resource)
    code_worker._apply_process_limits(512 * 1024 * 1024, 65)
    assert calls == [
        (1, (512 * 1024 * 1024, 512 * 1024 * 1024)),
        (2, (65, 65)),
        (3, (0, 0)),
        (4, (64, 64)),
    ]


def _configure_provider(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_AGSX_REGION", "ap-guangzhou")
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_AGSX_DOMAIN",
        "ap-guangzhou.tencentags.com",
    )
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_AGSX_CONTROL_ENDPOINT",
        "ags.tencentcloudapi.com",
    )
    monkeypatch.setattr(tagentic_config, "WORKBENCH_AGSX_TOOL_ID", "tool-1")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_AGSX_TOOL_NAME", "sandbox-tool")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_NETWORK_MODE", "SANDBOX")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_AUTH_MODE", "TOKEN")


def test_control_endpoint_must_exactly_match_the_official_tencent_endpoint(monkeypatch):
    _configure_provider(monkeypatch)
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_AGSX_CONTROL_ENDPOINT",
        "attacker.example.com",
    )
    with pytest.raises(SandboxProviderError) as raised:
        TencentAGSXProvider()
    assert raised.value.code == "provider_control_endpoint_invalid"


@pytest.mark.asyncio
async def test_e2b_api_key_requires_the_official_ark_prefix(monkeypatch, tmp_path):
    _configure_provider(monkeypatch)
    key_file = tmp_path / "api-key"
    key_file.write_text("e2b_not_a_tencent_key", encoding="utf-8")
    if os.name != "nt":
        key_file.chmod(0o600)
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_AGSX_API_KEY_FILE",
        str(key_file),
    )
    with pytest.raises(SandboxProviderError) as raised:
        await TencentAGSXProvider._connect("provider-instance-1")
    assert raised.value.code == "agsx_api_key_invalid"


def test_enabled_provider_readiness_validates_all_mounted_secrets(monkeypatch, tmp_path):
    _configure_provider(monkeypatch)
    values = {
        "WORKBENCH_AGSX_API_KEY_FILE": b"ark_ready-provider-key",
        "WORKBENCH_AGSX_CAM_SECRET_ID_FILE": b"cam-secret-id",
        "WORKBENCH_AGSX_CAM_SECRET_KEY_FILE": b"cam-secret-key-value",
        "WORKBENCH_SANDBOX_CLIENT_TOKEN_KEY_FILE": b"h" * 32,
    }
    for setting, value in values.items():
        path = tmp_path / setting.lower()
        path.write_bytes(value)
        if os.name != "nt":
            path.chmod(0o600)
        monkeypatch.setattr(tagentic_config, setting, str(path))
    TencentAGSXProvider.validate_readiness()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are unavailable")
def test_secret_reader_rejects_group_or_world_readable_files(tmp_path):
    path = tmp_path / "insecure-secret"
    path.write_bytes(b"secret")
    path.chmod(0o644)
    with pytest.raises(OSError):
        read_secret_file(str(path))


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership is unavailable")
def test_secret_reader_rejects_files_owned_by_another_uid(monkeypatch, tmp_path):
    path = tmp_path / "wrong-owner-secret"
    path.write_bytes(b"secret")
    path.chmod(0o600)
    monkeypatch.setattr(sandbox_secrets.os, "geteuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(OSError):
        read_secret_file(str(path))


def test_secret_reader_rejects_symlinked_files(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"secret")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    with pytest.raises(OSError):
        read_secret_file(str(link))


def test_retry_backoff_has_bounded_jitter(monkeypatch):
    monkeypatch.setattr("core.workbench_sandbox.provider.secrets.randbelow", lambda _: 49)
    assert TencentAGSXProvider._retry_delay(0) == pytest.approx(0.149)
    assert TencentAGSXProvider._retry_delay(1) == pytest.approx(0.249)
    assert TencentAGSXProvider._retry_delay(10) == pytest.approx(0.5)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ("pause", "resume", "stop"))
async def test_non_idempotent_lifecycle_control_calls_are_never_blindly_retried(
    monkeypatch,
    action,
):
    _configure_provider(monkeypatch)
    retry_counts = []

    async def control_call(operation, *, retry_count=3):
        del operation
        retry_counts.append(retry_count)
        raise SandboxProviderError("provider_control_unavailable", retryable=True)

    monkeypatch.setattr(
        TencentAGSXProvider,
        "_cloud_client",
        classmethod(lambda cls: SimpleNamespace()),
    )
    monkeypatch.setattr(
        TencentAGSXProvider,
        "_control_call",
        classmethod(
            lambda cls, operation, **kwargs: control_call(operation, **kwargs)
        ),
    )
    provider = TencentAGSXProvider()
    with pytest.raises(SandboxProviderError):
        if action == "resume":
            await provider.resume("provider-instance-1", timeout_seconds=60)
        else:
            await getattr(provider, action)("provider-instance-1")
    assert retry_counts == [1]
