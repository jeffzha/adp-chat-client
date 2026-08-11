import importlib
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from unittest.mock import patch

import pytest

from config import tagentic_config
from core.workbench_app_migration import (
    WorkbenchAppMigrationError,
    WorkbenchAppMigrationWorker,
)
from core.workbench_control import (
    WorkbenchAppContext,
    WorkbenchAppMigrationTask,
    WorkbenchControlClient,
    WorkbenchControlError,
)
from core.workbench_runtime_profile import (
    CLAW_STATIC_V2,
    MULTI_AGENT_V2,
    STANDARD_V2,
    WORKFLOW_V2,
)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


def _task(
    *,
    provider_app_mode: int = 4,
    runtime_profile: str = "claw_dynamic_v2",
    execution_enabled: bool = True,
) -> WorkbenchAppMigrationTask:
    provider = WorkbenchAppContext(
        application_id="target-app",
        app_profile_id="22",
        config_version=4,
        auth_epoch=1,
        vendor="Tencent",
        service_vendor="ChinaTencentADP",
        app_id="target-app",
        app_key="provider-app-key",
        space_id="space-1",
        template_agent_id="template-1",
        secret_id="provider-secret-id",
        secret_key="provider-secret-key",
        provider_app_mode=provider_app_mode,
        runtime_profile=runtime_profile,
        execution_enabled=execution_enabled,
    )
    return WorkbenchAppMigrationTask(
        migration_job_id="amj_1",
        migration_member_id="amm_1",
        attempt_id="ama_1",
        lease_token="a" * 64,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
        binding_id="binding-1",
        adp_account_id="11111111-1111-1111-1111-111111111111",
        customer_id=7,
        source_application_id="source-app",
        target_application_id="target-app",
        target_app_profile_id=22,
        target_config_version=4,
        target_config_fingerprint="sha256:" + "b" * 64,
        mode="copy",
        known_target_agent_id="",
        provider=provider,
        provider_app_mode=provider_app_mode,
        runtime_profile=runtime_profile,
        execution_enabled=execution_enabled,
    )


def _claim_envelope(task: WorkbenchAppMigrationTask) -> dict:
    envelope = {
        "task": {
            "migration_job_id": task.migration_job_id,
            "migration_member_id": task.migration_member_id,
            "attempt_id": task.attempt_id,
            "lease_token": task.lease_token,
            "lease_expires_at": task.lease_expires_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "binding_id": task.binding_id,
            "adp_account_id": task.adp_account_id,
            "customer_id": task.customer_id,
            "source_application_id": task.source_application_id,
            "target_application_id": task.target_application_id,
            "target_app_profile_id": task.target_app_profile_id,
            "target_config_version": task.target_config_version,
            "target_config_fingerprint": task.target_config_fingerprint,
            "mode": task.mode,
            "provider_app_mode": task.provider_app_mode,
            "runtime_profile": task.runtime_profile,
            "execution_enabled": task.execution_enabled,
            "provider": {
                "vendor": task.provider.vendor,
                "service_vendor": task.provider.service_vendor,
                "app_id": task.provider.app_id,
                "app_key": task.provider.app_key,
                "space_id": task.provider.space_id,
                "template_agent_id": task.provider.template_agent_id,
                "secret_id": task.provider.secret_id,
                "secret_key": task.provider.secret_key,
                "provider_app_mode": task.provider.provider_app_mode,
                "runtime_profile": task.provider.runtime_profile,
                "execution_enabled": task.provider.execution_enabled,
            },
        }
    }
    if task.known_target_agent_id:
        envelope["task"]["known_target_agent_id"] = task.known_target_agent_id
    return envelope


@pytest.mark.asyncio
async def test_signed_claim_contract_is_strict_and_task_repr_hides_credentials(
    monkeypatch,
):
    expected = _task()
    monkeypatch.setattr(
        WorkbenchControlClient,
        "_request",
        AsyncMock(return_value=_claim_envelope(expected)),
    )
    task = await WorkbenchControlClient.claim_app_migration_task(
        worker_id="adp-blue",
        lease_seconds=60,
    )
    assert task == expected
    assert "provider-app-key" not in repr(task)
    assert "provider-secret-key" not in repr(task)

    invalid = _claim_envelope(expected)
    invalid["task"]["unexpected"] = True
    monkeypatch.setattr(
        WorkbenchControlClient,
        "_request",
        AsyncMock(return_value=invalid),
    )
    with pytest.raises(WorkbenchControlError, match="response is invalid"):
        await WorkbenchControlClient.claim_app_migration_task(
            worker_id="adp-blue",
            lease_seconds=60,
        )

    mismatched = _claim_envelope(expected)
    mismatched["task"]["provider"]["runtime_profile"] = CLAW_STATIC_V2
    mismatched["task"]["provider"]["execution_enabled"] = False
    monkeypatch.setattr(
        WorkbenchControlClient,
        "_request",
        AsyncMock(return_value=mismatched),
    )
    with pytest.raises(WorkbenchControlError, match="failed validation"):
        await WorkbenchControlClient.claim_app_migration_task(
            worker_id="adp-blue",
            lease_seconds=60,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_app_mode", "runtime_profile"),
    (
        (1, STANDARD_V2),
        (2, MULTI_AGENT_V2),
        (3, WORKFLOW_V2),
        (4, CLAW_STATIC_V2),
    ),
)
async def test_non_dynamic_migration_establishes_only_local_readiness(
    monkeypatch,
    provider_app_mode,
    runtime_profile,
):
    task = _task(
        provider_app_mode=provider_app_mode,
        runtime_profile=runtime_profile,
        execution_enabled=False,
    )
    binding = SimpleNamespace(
        Status="provisioning",
        AttemptId=task.attempt_id,
        AgentId=None,
        ErrorCode=None,
    )
    db = SimpleNamespace(
        add=Mock(),
        commit=AsyncMock(),
        execute=AsyncMock(return_value=_ScalarResult(None)),
    )
    ensure_vendor = AsyncMock()
    monkeypatch.setattr(
        "core.workbench_app_migration.WorkbenchAppResolver.ensure_vendor",
        ensure_vendor,
    )
    revoke = Mock()
    monkeypatch.setattr(
        "core.workbench_app_migration.WorkbenchAppResolver.revoke",
        revoke,
    )
    provider_factory = Mock(side_effect=AssertionError("provider must not be used"))
    monkeypatch.setattr(
        "core.workbench_app_migration.TAgenticApp.get_app",
        provider_factory,
    )
    monkeypatch.setattr(
        WorkbenchAppMigrationWorker,
        "_prepare_local_binding",
        AsyncMock(return_value=(binding, None)),
    )
    monkeypatch.setattr(
        WorkbenchAppMigrationWorker,
        "_locked_binding",
        AsyncMock(return_value=binding),
    )
    report = AsyncMock(return_value={})
    monkeypatch.setattr(
        WorkbenchControlClient,
        "report_app_migration_task",
        report,
    )

    await WorkbenchAppMigrationWorker.process_one(db, task)

    provider_factory.assert_not_called()
    ensure_vendor.assert_not_awaited()
    assert binding.Status == "active"
    assert binding.AgentId.startswith("wrp_")
    assert len(binding.AgentId) == 52
    assert report.await_args.kwargs == {
        "status": "succeeded",
        "target_agent_id": "",
        "target_readback_hash": "",
    }
    revoke.assert_not_called()


@pytest.mark.asyncio
async def test_worker_copies_describes_commits_then_reports_exact_target(monkeypatch):
    task = _task()
    binding = SimpleNamespace(
        Status="provisioning",
        AttemptId=task.attempt_id,
        AgentId=None,
        ErrorCode=None,
    )
    db = SimpleNamespace(
        add=lambda _value: None,
        commit=AsyncMock(),
        execute=AsyncMock(return_value=_ScalarResult(None)),
    )
    vendor = SimpleNamespace(
        forward_request=AsyncMock(
            side_effect=[
                {"ParentAgentId": "agent-target-1"},
                {"Agent": {"AgentId": "agent-target-1", "Name": "copied"}},
            ]
        )
    )
    monkeypatch.setattr(
        "core.workbench_app_migration.WorkbenchAppResolver.ensure_vendor",
        AsyncMock(),
    )
    revoke = Mock()
    monkeypatch.setattr(
        "core.workbench_app_migration.WorkbenchAppResolver.revoke",
        revoke,
    )
    monkeypatch.setattr(
        "core.workbench_app_migration.TAgenticApp.get_app",
        lambda: SimpleNamespace(get_vendor_app=lambda _application_id: vendor),
    )
    monkeypatch.setattr(
        WorkbenchAppMigrationWorker,
        "_prepare_local_binding",
        AsyncMock(return_value=(binding, None)),
    )
    monkeypatch.setattr(
        WorkbenchAppMigrationWorker,
        "_locked_binding",
        AsyncMock(return_value=binding),
    )
    report = AsyncMock(return_value={})
    monkeypatch.setattr(
        WorkbenchControlClient,
        "report_app_migration_task",
        report,
    )

    await WorkbenchAppMigrationWorker.process_one(db, task)

    assert binding.Status == "active"
    assert binding.AgentId == "agent-target-1"
    assert db.commit.await_count == 2
    assert vendor.forward_request.await_args_list[0].args == (
        "CopyAgentFromApp",
        {"AppId": "target-app", "Kind": 1},
    )
    assert vendor.forward_request.await_args_list[1].args == (
        "DescribeAgentDetail",
        {"AppId": "target-app", "AgentId": "agent-target-1"},
    )
    kwargs = report.await_args.kwargs
    assert kwargs["status"] == "succeeded"
    assert kwargs["target_agent_id"] == "agent-target-1"
    assert kwargs["target_readback_hash"].startswith("sha256:")
    revoke.assert_called_once_with("target-app")


@pytest.mark.asyncio
async def test_worker_marks_unknown_and_reports_failure_on_readback_mismatch(
    monkeypatch,
):
    task = _task()
    binding = SimpleNamespace(
        Status="provisioning",
        AttemptId=task.attempt_id,
        AgentId=None,
        ErrorCode=None,
    )
    db = SimpleNamespace(
        add=lambda _value: None,
        commit=AsyncMock(),
        execute=AsyncMock(return_value=_ScalarResult(None)),
    )
    vendor = SimpleNamespace(
        forward_request=AsyncMock(
            side_effect=[
                {"ParentAgentId": "agent-target-1"},
                {"Agent": {"AgentId": "another-agent"}},
            ]
        )
    )
    monkeypatch.setattr(
        "core.workbench_app_migration.WorkbenchAppResolver.ensure_vendor",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "core.workbench_app_migration.WorkbenchAppResolver.revoke",
        lambda _application_id: None,
    )
    monkeypatch.setattr(
        "core.workbench_app_migration.TAgenticApp.get_app",
        lambda: SimpleNamespace(get_vendor_app=lambda _application_id: vendor),
    )
    monkeypatch.setattr(
        WorkbenchAppMigrationWorker,
        "_prepare_local_binding",
        AsyncMock(return_value=(binding, None)),
    )
    monkeypatch.setattr(
        WorkbenchAppMigrationWorker,
        "_locked_binding",
        AsyncMock(return_value=binding),
    )
    report = AsyncMock(return_value={})
    monkeypatch.setattr(
        WorkbenchControlClient,
        "report_app_migration_task",
        report,
    )

    with pytest.raises(WorkbenchAppMigrationError, match="readback_mismatch"):
        await WorkbenchAppMigrationWorker.process_one(db, task)

    assert binding.Status == "provider_unknown"
    assert report.await_args.kwargs == {
        "status": "failed",
        "target_agent_id": "agent-target-1",
        "error_code": "provider_outcome_unknown",
    }


@pytest.mark.asyncio
async def test_readback_recovery_never_calls_copy_agent(monkeypatch):
    task = replace(
        _task(),
        mode="readback",
        known_target_agent_id="known-target-agent",
    )
    binding = SimpleNamespace(
        Status="verifying",
        AttemptId=task.attempt_id,
        AgentId="known-target-agent",
        ErrorCode=None,
    )
    db = SimpleNamespace(
        add=lambda _value: None,
        commit=AsyncMock(),
        execute=AsyncMock(return_value=_ScalarResult(None)),
    )
    vendor = SimpleNamespace(
        forward_request=AsyncMock(
            return_value={"Agent": {"AgentId": "known-target-agent"}}
        )
    )
    monkeypatch.setattr(
        "core.workbench_app_migration.WorkbenchAppResolver.ensure_vendor",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "core.workbench_app_migration.WorkbenchAppResolver.revoke",
        lambda _application_id: None,
    )
    monkeypatch.setattr(
        "core.workbench_app_migration.TAgenticApp.get_app",
        lambda: SimpleNamespace(get_vendor_app=lambda _application_id: vendor),
    )
    monkeypatch.setattr(
        WorkbenchAppMigrationWorker,
        "_prepare_local_binding",
        AsyncMock(return_value=(binding, "known-target-agent")),
    )
    monkeypatch.setattr(
        WorkbenchAppMigrationWorker,
        "_locked_binding",
        AsyncMock(return_value=binding),
    )
    report = AsyncMock(return_value={})
    monkeypatch.setattr(
        WorkbenchControlClient,
        "report_app_migration_task",
        report,
    )

    await WorkbenchAppMigrationWorker.process_one(db, task)

    vendor.forward_request.assert_awaited_once_with(
        "DescribeAgentDetail",
        {"AppId": "target-app", "AgentId": "known-target-agent"},
    )
    assert binding.Status == "active"
    assert report.await_args.kwargs["status"] == "succeeded"


@pytest.mark.asyncio
async def test_migration_worker_feature_off_starts_no_background_task(monkeypatch):
    class _FakeApp:
        def listener(self, *_args, **_kwargs):
            return lambda function: function

    sys.modules.pop("middleware.workbench_app_migration", None)
    with patch("app_factory.TAgenticApp.get_app", return_value=_FakeApp()):
        module = importlib.import_module("middleware.workbench_app_migration")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_APP_MIGRATION_WORKER_ENABLED",
        False,
    )
    start = Mock()
    monkeypatch.setattr(module.WorkbenchAppMigrationWorker, "start", start)

    await module.start_workbench_app_migration_worker(SimpleNamespace(), None)

    start.assert_not_called()
