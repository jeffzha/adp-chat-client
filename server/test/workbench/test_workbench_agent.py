from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.agent import AgentProvisioningError, CoreAgent
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from model.workbench import WorkbenchAgentBinding


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


def _agent_detail(
    *,
    max_tokens=4096,
    max_reasoning_round=10,
    tool_list=None,
    plugin_list=None,
):
    return {
        "Agent": {
            "AgentId": "user-agent-9",
            "Model": {
                "ModelId": "TCADP/test-model",
                "Alias": "Test model",
                "ModelParameters": {
                    "Temperature": 0.7,
                    "MaxTokens": max_tokens,
                },
            },
            "AdvancedConfig": {
                "HistoryLimit": 10,
                "MaxReasoningRound": max_reasoning_round,
            },
            "ToolList": [] if tool_list is None else tool_list,
            "PluginList": [] if plugin_list is None else plugin_list,
            "SkillList": [],
        }
    }


def _limit_db(binding):
    return SimpleNamespace(
        execute=AsyncMock(return_value=_ScalarResult(binding)),
        add=lambda _value: None,
        commit=AsyncMock(),
    )


def _runtime_app_context(provider_app_mode, runtime_profile):
    return WorkbenchAppContext(
        application_id="customer-app-7",
        app_profile_id="7",
        config_version=4,
        auth_epoch=3,
        vendor="Tencent",
        service_vendor="ChinaTencentCloud",
        app_id="provider-app-7",
        app_key="provider-app-key",
        space_id="space-7",
        template_agent_id="",
        secret_id="secret-id",
        secret_key="secret-key",
        capabilities=("chat",),
        limits={},
        provider_app_mode=provider_app_mode,
        runtime_profile=runtime_profile,
        execution_enabled=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_app_mode", "runtime_profile"),
    [
        (1, "standard_v2"),
        (2, "multi_agent_v2"),
        (3, "workflow_v2"),
        (4, "claw_static_v2"),
    ],
)
async def test_non_dynamic_runtime_principal_never_calls_provider_agent_api(
    monkeypatch, provider_app_mode, runtime_profile
):
    identity_row = SimpleNamespace(BindingId="binding-7")
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_ScalarResult(identity_row)),
        add=lambda _value: None,
    )
    vendor = SimpleNamespace(forward_request=AsyncMock())
    monkeypatch.setattr(CoreAgent, "_lock_workbench_binding", AsyncMock(return_value=None))
    monkeypatch.setattr(CoreAgent, "get", AsyncMock(return_value=None))
    report = AsyncMock()
    monkeypatch.setattr(CoreAgent, "_report_workbench_agent", report)
    identity = WorkbenchIdentityContext(
        binding_id="binding-7",
        canonical_subject="napi:prod:customer:7:user:9",
        customer_id=7,
        new_api_user_id=9,
        auth_epoch=3,
        display_name="User 9",
        application_id="customer-app-7",
        app_profile_id="7",
        access_mode="active",
        config_version=4,
    )

    principal = await CoreAgent.ensure_runtime_principal(
        db,
        "account-9",
        _runtime_app_context(provider_app_mode, runtime_profile),
        vendor,
        max_output_tokens=8192,
        max_reasoning_rounds=20,
        identity_context=identity,
    )

    assert principal.ownership_id.startswith("wrp_")
    assert principal.provider_agent_id is None
    vendor.forward_request.assert_not_awaited()
    report.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_agent_row_blocks_provider_copy_before_side_effect(monkeypatch):
    identity = SimpleNamespace(BindingId="binding-7")
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _ScalarResult(identity),
                _ScalarResult(None),
            ]
        )
    )
    legacy = SimpleNamespace(AgentId="legacy-agent-9")
    monkeypatch.setattr(CoreAgent, "get", AsyncMock(return_value=legacy))
    vendor = SimpleNamespace(
        config={"AppId": "provider-app-7"},
        forward_request=AsyncMock(),
    )

    with pytest.raises(AgentProvisioningError, match="requires reconciliation"):
        await CoreAgent.ensure(
            db,
            "account-9",
            "customer-app-7",
            vendor,
            identity_context=WorkbenchIdentityContext(
                binding_id="binding-7",
                canonical_subject="napi:test:customer:7:user:9",
                customer_id=7,
                new_api_user_id=9,
                auth_epoch=1,
                display_name="Test",
                application_id="customer-app-7",
                app_profile_id="17",
                access_mode="active",
                config_version=3,
            ),
        )

    vendor.forward_request.assert_not_awaited()


def test_migration_registers_agent_binding_table():
    from unittest.mock import patch

    with patch("app_factory.TAgenticApp.get_app", return_value=SimpleNamespace()):
        from core.migration import Migration

    assert WorkbenchAgentBinding in Migration.tables()


@pytest.mark.asyncio
async def test_turn_limits_modify_full_model_sections_and_verify_readback(monkeypatch):
    record = SimpleNamespace(AgentId="user-agent-9")
    binding = SimpleNamespace(
        Status="active",
        AgentId="user-agent-9",
        AttemptId="old-attempt",
        ErrorCode=None,
        LimitFingerprint=None,
        LimitsVerifiedAt=None,
    )
    db = _limit_db(binding)
    vendor = SimpleNamespace(
        config={"AppId": "provider-app-7"},
        forward_request=AsyncMock(
            side_effect=[
                _agent_detail(),
                {"RequestId": "modify-request"},
                _agent_detail(max_tokens=8192, max_reasoning_round=20),
            ]
        ),
    )
    monkeypatch.setattr(CoreAgent, "ensure", AsyncMock(return_value=record))

    result = await CoreAgent.ensure_turn_limits(
        db,
        "account-9",
        "customer-app-7",
        vendor,
        max_output_tokens=8192,
        max_reasoning_rounds=20,
    )

    assert result is record
    assert vendor.forward_request.await_args_list[1].args == (
        "ModifyAgent",
        {
            "AppId": "provider-app-7",
            "AgentId": "user-agent-9",
            "Agent": {
                "Model": {
                    "ModelId": "TCADP/test-model",
                    "Alias": "Test model",
                    "ModelParameters": {
                        "Temperature": 0.7,
                        "MaxTokens": 8192,
                    },
                },
                "AdvancedConfig": {
                    "HistoryLimit": 10,
                    "MaxReasoningRound": 20,
                },
            },
            "UpdateMask": {"Paths": ["Model", "AdvancedConfig"]},
        },
    )
    assert binding.Status == "active"
    assert binding.LimitFingerprint
    assert binding.LimitsVerifiedAt is not None
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_turn_limits_initialize_nullable_provider_max_tokens(monkeypatch):
    record = SimpleNamespace(AgentId="user-agent-9")
    binding = SimpleNamespace(
        Status="active",
        AgentId="user-agent-9",
        AttemptId="old-attempt",
        ErrorCode=None,
        LimitFingerprint=None,
        LimitsVerifiedAt=None,
    )
    db = _limit_db(binding)
    initial = _agent_detail(max_reasoning_round=100)
    del initial["Agent"]["Model"]["ModelParameters"]["MaxTokens"]
    vendor = SimpleNamespace(
        config={"AppId": "provider-app-7"},
        forward_request=AsyncMock(
            side_effect=[
                initial,
                {"RequestId": "modify-request"},
                _agent_detail(max_tokens=8192, max_reasoning_round=20),
            ]
        ),
    )
    monkeypatch.setattr(CoreAgent, "ensure", AsyncMock(return_value=record))

    await CoreAgent.ensure_turn_limits(
        db,
        "account-9",
        "customer-app-7",
        vendor,
        max_output_tokens=8192,
        max_reasoning_rounds=20,
    )

    modify_payload = vendor.forward_request.await_args_list[1].args[1]
    assert modify_payload["Agent"]["Model"]["ModelParameters"]["MaxTokens"] == 8192
    assert modify_payload["Agent"]["AdvancedConfig"]["MaxReasoningRound"] == 20
    assert binding.Status == "active"


@pytest.mark.asyncio
async def test_matching_turn_limits_are_verified_without_modify(monkeypatch):
    record = SimpleNamespace(AgentId="user-agent-9")
    binding = SimpleNamespace(
        Status="active",
        AgentId="user-agent-9",
        AttemptId="old-attempt",
        ErrorCode=None,
        LimitFingerprint=None,
        LimitsVerifiedAt=None,
    )
    db = _limit_db(binding)
    vendor = SimpleNamespace(
        config={"AppId": "provider-app-7"},
        forward_request=AsyncMock(
            return_value=_agent_detail(max_tokens=8192, max_reasoning_round=20)
        ),
    )
    monkeypatch.setattr(CoreAgent, "ensure", AsyncMock(return_value=record))

    await CoreAgent.ensure_turn_limits(
        db,
        "account-9",
        "customer-app-7",
        vendor,
        max_output_tokens=8192,
        max_reasoning_rounds=20,
    )

    vendor.forward_request.assert_awaited_once_with(
        "DescribeAgentDetail",
        {"AppId": "provider-app-7", "AgentId": "user-agent-9"},
    )
    assert binding.Status == "active"


@pytest.mark.asyncio
async def test_limit_readback_mismatch_marks_provider_state_unknown(monkeypatch):
    record = SimpleNamespace(AgentId="user-agent-9")
    binding = SimpleNamespace(
        Status="active",
        AgentId="user-agent-9",
        AttemptId="old-attempt",
        ErrorCode=None,
        LimitFingerprint=None,
        LimitsVerifiedAt=None,
    )
    db = _limit_db(binding)
    vendor = SimpleNamespace(
        config={"AppId": "provider-app-7"},
        forward_request=AsyncMock(
            side_effect=[
                _agent_detail(),
                {"RequestId": "modify-request"},
                _agent_detail(),
            ]
        ),
    )
    monkeypatch.setattr(CoreAgent, "ensure", AsyncMock(return_value=record))

    with pytest.raises(AgentProvisioningError, match="did not match"):
        await CoreAgent.ensure_turn_limits(
            db,
            "account-9",
            "customer-app-7",
            vendor,
            max_output_tokens=8192,
            max_reasoning_rounds=20,
        )

    assert binding.Status == "provider_unknown"


@pytest.mark.asyncio
async def test_limit_lock_conflict_fails_before_provider_call(monkeypatch):
    record = SimpleNamespace(AgentId="user-agent-9")
    binding = SimpleNamespace(
        Status="configuring",
        AgentId="user-agent-9",
    )
    db = _limit_db(binding)
    vendor = SimpleNamespace(
        config={"AppId": "provider-app-7"},
        forward_request=AsyncMock(),
    )
    monkeypatch.setattr(CoreAgent, "ensure", AsyncMock(return_value=record))

    with pytest.raises(AgentProvisioningError, match="version or lock conflict"):
        await CoreAgent.ensure_turn_limits(
            db,
            "account-9",
            "customer-app-7",
            vendor,
            max_output_tokens=8192,
            max_reasoning_rounds=20,
        )

    vendor.forward_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_limit_update_preserves_independently_revalidated_integration_lists(monkeypatch):
    record = SimpleNamespace(AgentId="user-agent-9")
    binding = SimpleNamespace(
        Status="active",
        AgentId="user-agent-9",
        AttemptId="old-attempt",
        ErrorCode=None,
        LimitFingerprint=None,
        LimitsVerifiedAt=None,
    )
    db = _limit_db(binding)
    vendor = SimpleNamespace(
        config={"AppId": "provider-app-7"},
        forward_request=AsyncMock(
            return_value=_agent_detail(
                max_tokens=8192,
                max_reasoning_round=20,
                tool_list=[{"ToolId": "provider-managed-read-tool"}],
            )
        ),
    )
    monkeypatch.setattr(CoreAgent, "ensure", AsyncMock(return_value=record))

    await CoreAgent.ensure_turn_limits(
        db,
        "account-9",
        "customer-app-7",
        vendor,
        max_output_tokens=8192,
        max_reasoning_rounds=20,
    )

    assert binding.Status == "active"
    vendor.forward_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_limit_version_change_before_final_commit_fails_closed(monkeypatch):
    record = SimpleNamespace(AgentId="user-agent-9")
    initial = SimpleNamespace(
        Status="active",
        AgentId="user-agent-9",
        AttemptId="old-attempt",
        ErrorCode=None,
        LimitFingerprint=None,
        LimitsVerifiedAt=None,
    )
    competing = SimpleNamespace(
        Status="configuring",
        AgentId="user-agent-9",
        AttemptId="competing-attempt",
        ErrorCode=None,
        LimitFingerprint=None,
        LimitsVerifiedAt=None,
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _ScalarResult(initial),
                _ScalarResult(competing),
            ]
        ),
        add=lambda _value: None,
        commit=AsyncMock(),
    )
    vendor = SimpleNamespace(
        config={"AppId": "provider-app-7"},
        forward_request=AsyncMock(
            return_value=_agent_detail(max_tokens=8192, max_reasoning_round=20)
        ),
    )
    monkeypatch.setattr(CoreAgent, "ensure", AsyncMock(return_value=record))

    with pytest.raises(AgentProvisioningError, match="version changed") as error:
        await CoreAgent.ensure_turn_limits(
            db,
            "account-9",
            "customer-app-7",
            vendor,
            max_output_tokens=8192,
            max_reasoning_rounds=20,
        )

    assert error.value.status_code == 409
    assert db.commit.await_count == 1
