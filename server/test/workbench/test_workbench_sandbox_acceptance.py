import json
import os
import uuid
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from config import tagentic_config
from core.workbench_identity import CoreWorkbenchIdentity
from core.workbench_sandbox.acceptance import (
    WorkbenchSandboxAcceptance,
    WorkbenchSandboxAcceptanceContext,
)
from core.workbench_sandbox.service import WorkbenchSandboxError, WorkbenchSandboxService
from model.workbench_sandbox import WorkbenchSandbox
from model.workbench_sandbox_acceptance import WorkbenchSandboxAcceptanceEvent
from test.workbench.test_workbench_sandbox import (
    ACCOUNT_ID,
    CONVERSATION_1,
    CONVERSATION_2,
    _AsyncSessionAdapter,
    _FakeProvider,
    _app_context,
    _identity,
    sandbox_store,
)


RUN_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _context():
    return WorkbenchSandboxAcceptanceContext(
        acceptance_run_id=RUN_ID,
        conversation_id=CONVERSATION_1,
        account_id=ACCOUNT_ID,
        identity=_identity(),
        app_context=_app_context(),
    )


@pytest.mark.asyncio
async def test_response_loss_evidence_proves_replay_does_not_repeat_provider_start(
    sandbox_store,
):
    provider = _FakeProvider()
    context = _context()
    with sandbox_store() as session:
        with pytest.raises(WorkbenchSandboxError) as raised:
            await WorkbenchSandboxService.create_or_reuse(
                _AsyncSessionAdapter(session),
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                payload={"conversation_id": CONVERSATION_1, "timeout_seconds": 60},
                provider=provider,
                provider_start_observer=context.observe_provider_start,
            )
    assert raised.value.code == "sandbox_provider_failed"
    assert len(provider.start_tokens) == 1

    with sandbox_store() as session:
        row = session.execute(select(WorkbenchSandbox)).scalar_one()
        assert row.Status == "provider_unknown"
        replay = await WorkbenchSandboxService.create_or_reuse(
            _AsyncSessionAdapter(session),
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            payload={"conversation_id": CONVERSATION_1, "timeout_seconds": 60},
            provider=provider,
            provider_start_observer=context.observe_provider_start,
        )
        report = await WorkbenchSandboxAcceptance.report(
            _AsyncSessionAdapter(session), context
        )
        revoked_report = await WorkbenchSandboxAcceptance.report(
            _AsyncSessionAdapter(session),
            replace(
                context,
                identity=_identity(epoch=4),
                app_context=_app_context(epoch=4),
            ),
        )

    assert replay["status"] == "provider_unknown"
    assert len(provider.start_tokens) == 1
    assert report["provider_start_count"] == 1
    assert revoked_report["provider_start_count"] == 0
    assert len(report["instances"]) == 1
    serialized = json.dumps(report)
    assert "provider-instance" not in serialized
    assert "ProviderInstanceId" not in serialized
    assert "client_token" not in serialized


@pytest.mark.asyncio
async def test_observer_failure_still_fences_replay_after_provider_started(sandbox_store):
    provider = _FakeProvider()

    async def failed_observer(_db, _sandbox, _instance):
        raise RuntimeError("evidence store unavailable")

    with sandbox_store() as session:
        with pytest.raises(WorkbenchSandboxError) as raised:
            await WorkbenchSandboxService.create_or_reuse(
                _AsyncSessionAdapter(session),
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                payload={"conversation_id": CONVERSATION_1, "timeout_seconds": 60},
                provider=provider,
                provider_start_observer=failed_observer,
            )
    assert raised.value.code == "sandbox_provider_failed"

    with sandbox_store() as session:
        row = session.execute(select(WorkbenchSandbox)).scalar_one()
        replay = await WorkbenchSandboxService.create_or_reuse(
            _AsyncSessionAdapter(session),
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            payload={"conversation_id": CONVERSATION_1, "timeout_seconds": 60},
            provider=provider,
            provider_start_observer=failed_observer,
        )

    assert row.Status == "provider_unknown"
    assert replay["status"] == "provider_unknown"
    assert len(provider.start_tokens) == 1


@pytest.mark.asyncio
async def test_acceptance_cleanup_stops_every_recorded_instance_without_projecting_locator(
    sandbox_store,
):
    provider = _FakeProvider()
    context = _context()
    with sandbox_store() as session:
        with pytest.raises(WorkbenchSandboxError):
            await WorkbenchSandboxService.create_or_reuse(
                _AsyncSessionAdapter(session),
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                payload={"conversation_id": CONVERSATION_1, "timeout_seconds": 60},
                provider=provider,
                provider_start_observer=context.observe_provider_start,
            )
        sandbox = session.execute(select(WorkbenchSandbox)).scalar_one()
        session.add(
            WorkbenchSandboxAcceptanceEvent(
                AcceptanceRunId=RUN_ID,
                AccountId=ACCOUNT_ID,
                BindingId="binding-7",
                CustomerId=7,
                NewApiUserId=9,
                ApplicationId="customer-app-7",
                AppProfileId="17",
                ConfigVersion=5,
                AuthEpoch=3,
                ConversationId=CONVERSATION_1,
                SandboxId=sandbox.SandboxId,
                Generation=sandbox.Generation,
                ProviderInstanceId="provider-instance-orphan-2",
                FaultMode="provider_start_response_lost",
                CleanupStatus="pending",
                CleanupAttempts=0,
            )
        )
        session.commit()

    with sandbox_store() as session:
        result = await WorkbenchSandboxAcceptance.cleanup(
            _AsyncSessionAdapter(session), context, provider=provider
        )
        events = session.execute(
            select(WorkbenchSandboxAcceptanceEvent)
        ).scalars().all()
        sandbox = session.execute(select(WorkbenchSandbox)).scalar_one()

    assert len(provider.stop_calls) == 2
    assert set(provider.stop_calls) == {
        "provider-instance-1",
        "provider-instance-orphan-2",
    }
    assert result["provider_start_count"] == 2
    assert result["cleanup_requested"] == 2
    assert result["cleanup_succeeded"] == 2
    assert result["cleanup_failed"] == 0
    assert result["complete"] is True
    assert {event.CleanupStatus for event in events} == {"stopped"}
    assert sandbox.Status == "stopped"
    assert "provider-instance" not in json.dumps(result)


@pytest.mark.asyncio
async def test_local_recovery_lookup_is_owner_scoped_and_never_calls_provider(
    sandbox_store, monkeypatch
):
    provider = _FakeProvider()
    with sandbox_store() as session:
        await WorkbenchSandboxService.create_or_reuse(
            _AsyncSessionAdapter(session),
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            payload={"conversation_id": CONVERSATION_1, "timeout_seconds": 60},
            provider=provider,
        )
    assert len(provider.start_tokens) == 1
    provider_factory = Mock(side_effect=AssertionError("provider must not be created"))
    monkeypatch.setattr(WorkbenchSandboxService, "provider_factory", provider_factory)

    cases = [
        (CONVERSATION_1, _identity(customer=8), _app_context()),
        (CONVERSATION_1, _identity(user=10), _app_context()),
        (
            CONVERSATION_1,
            replace(_identity(), application_id="customer-app-other"),
            replace(_app_context(), application_id="customer-app-other"),
        ),
        (CONVERSATION_2, _identity(), _app_context()),
    ]
    with sandbox_store() as session:
        projected = await WorkbenchSandboxService.find_local(
            _AsyncSessionAdapter(session),
            conversation_id=CONVERSATION_1,
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
        )
        for conversation_id, identity, app_context in cases:
            with pytest.raises(WorkbenchSandboxError) as raised:
                await WorkbenchSandboxService.find_local(
                    _AsyncSessionAdapter(session),
                    conversation_id=conversation_id,
                    account_id=ACCOUNT_ID,
                    identity=identity,
                    app_context=app_context,
                )
            assert raised.value.code == "sandbox_not_found"
            assert raised.value.status_code == 404

    assert projected["conversation_id"] == CONVERSATION_1
    assert projected["status"] == "running"
    assert len(provider.start_tokens) == 1
    assert provider.stop_calls == []
    provider_factory.assert_not_called()


def _acceptance_configuration(monkeypatch, tmp_path):
    token_file = tmp_path / "acceptance-token"
    token_file.write_text("a" * 48, encoding="ascii")
    if os.name != "nt":
        token_file.chmod(0o600)
    monkeypatch.setattr(
        tagentic_config, "WORKBENCH_SANDBOX_ACCEPTANCE_FAULTS_ENABLED", True
    )
    monkeypatch.setattr(tagentic_config, "WORKBENCH_DEPLOYMENT_TIER", "acceptance")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SANDBOX_ENABLED", True)
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_SANDBOX_ACCEPTANCE_TOKEN_FILE",
        str(token_file),
    )
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_PUBLIC_BASE_URL",
        "https://gateway.example/workbench",
    )
    return "a" * 48


def test_acceptance_readiness_is_fail_closed_outside_acceptance(
    monkeypatch, tmp_path
):
    _acceptance_configuration(monkeypatch, tmp_path)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_DEPLOYMENT_TIER", "production")
    with pytest.raises(RuntimeError):
        WorkbenchSandboxAcceptance.validate_readiness()

    monkeypatch.setattr(tagentic_config, "WORKBENCH_DEPLOYMENT_TIER", "acceptance")
    WorkbenchSandboxAcceptance.validate_readiness()


@pytest.mark.asyncio
async def test_acceptance_authorization_requires_same_origin_token_and_recent_session(
    sandbox_store, monkeypatch, tmp_path
):
    token = _acceptance_configuration(monkeypatch, tmp_path)
    recent_session = AsyncMock()
    monkeypatch.setattr(
        CoreWorkbenchIdentity, "require_browser_session", recent_session
    )
    headers = {
        "Origin": "https://gateway.example",
        "X-Workbench-Acceptance-Token": token,
    }
    with sandbox_store() as session:
        context = await WorkbenchSandboxAcceptance.authorize(
            _AsyncSessionAdapter(session),
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            session_claims={"sid": "s" * 40, "auth_time": 1},
            headers=headers,
            request_host="gateway.example",
            acceptance_run_id=RUN_ID,
            conversation_id=CONVERSATION_1,
        )
        with pytest.raises(WorkbenchSandboxError) as bad_origin:
            await WorkbenchSandboxAcceptance.authorize(
                _AsyncSessionAdapter(session),
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                session_claims={},
                headers={**headers, "Origin": "https://evil.example"},
                request_host="gateway.example",
                acceptance_run_id=RUN_ID,
                conversation_id=CONVERSATION_1,
            )
        with pytest.raises(WorkbenchSandboxError) as bad_token:
            await WorkbenchSandboxAcceptance.authorize(
                _AsyncSessionAdapter(session),
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                session_claims={},
                headers={**headers, "X-Workbench-Acceptance-Token": "b" * 48},
                request_host="gateway.example",
                acceptance_run_id=RUN_ID,
                conversation_id=CONVERSATION_1,
            )

    assert context.acceptance_run_id == str(uuid.UUID(RUN_ID))
    assert bad_origin.value.status_code == 403
    assert bad_token.value.status_code == 404
    recent_session.assert_awaited_once()
