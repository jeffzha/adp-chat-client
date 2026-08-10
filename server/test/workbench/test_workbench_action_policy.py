from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.agent import CoreAgent
from core.conversation import CoreConversation
from core.workbench_action_policy import (
    PreparedWorkbenchAction,
    WorkbenchActionPolicy,
    WorkbenchActionPolicyError,
)
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_workspace import CoreWorkbenchWorkspace


class FakeTencentVendor:
    application_id = "customer-app-7"
    config = {
        "Vendor": "Tencent",
        "ServiceVendor": "ChinaTencentCloud",
        "AppId": "provider-app-7",
        "AppKey": "provider-app-key-secret",
    }

    @staticmethod
    def get_vendor():
        return "Tencent"


@pytest.fixture
def identity():
    return WorkbenchIdentityContext(
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


@pytest.fixture
def vendor():
    return FakeTencentVendor()


@pytest.fixture
def app_context():
    return WorkbenchAppContext(
        application_id="customer-app-7",
        app_profile_id="7",
        config_version=4,
        auth_epoch=3,
        vendor="Tencent",
        service_vendor="ChinaTencentCloud",
        app_id="provider-app-7",
        app_key="provider-app-key-secret",
        space_id="space-7",
        template_agent_id="template-agent-7",
        secret_id="secret-id",
        secret_key="secret-key",
        capabilities=("chat",),
        limits={},
    )


def request_body(payload=None, **extra):
    body = {
        "ApplicationId": "customer-app-7",
        "Payload": payload or {},
    }
    body.update(extra)
    return body


@pytest.mark.asyncio
async def test_unknown_action_is_denied_before_vendor_or_database_use(identity, vendor):
    with pytest.raises(WorkbenchActionPolicyError, match="not available") as exc_info:
        await WorkbenchActionPolicy.prepare(
            None,
            action="DeleteApp",
            request_body=request_body(),
            account_id="account-9",
            identity=identity,
            vendor_app=vendor,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_describe_conversation_binds_provider_workspace_but_projects_local_handle(
    monkeypatch,
    identity,
    app_context,
):
    bind = AsyncMock(return_value=SimpleNamespace(WorkspaceId="ww_owned"))
    monkeypatch.setattr(
        CoreWorkbenchWorkspace,
        "bind_provider_locator_for_conversation",
        bind,
    )
    prepared = PreparedWorkbenchAction(
        action="DescribeConversation",
        application_id="customer-app-7",
        provider_app_id="provider-app-7",
        agent_id="user-agent-9",
        conversation_id="4abd149a-e010-4a6c-bc52-1132658f149d",
        identity=identity,
        app_context=app_context,
        payload={},
    )

    projected = await WorkbenchActionPolicy.project_response(
        object(),
        prepared=prepared,
        account_id="account-9",
        response={
            "ConversationId": prepared.conversation_id,
            "Type": 5,
            "AppId": "provider-app-7",
            "AgentId": "user-agent-9",
            "Workspace": {
                "WorkspaceId": "workspace_v2-opaque",
                "StorageType": "sandbox_e2b",
            },
        },
    )

    assert projected["Workspace"] == {
        "WorkspaceId": "ww_owned",
        "StorageType": "sandbox_e2b",
    }
    assert "provider-app-7" not in repr(projected)
    assert "workspace_v2-opaque" not in repr(projected)
    bind.assert_awaited_once()
    assert bind.await_args.kwargs["provider_locator"] == "workspace_v2-opaque"


@pytest.mark.asyncio
async def test_list_dir_resolves_local_workspace_and_projects_only_safe_entries(
    monkeypatch,
    identity,
    vendor,
    app_context,
):
    resolver = AsyncMock(
        return_value=(
            SimpleNamespace(WorkspaceId="ww_owned"),
            "6576c839-839c-4a54-b820-408aec31dbe0",
        )
    )
    monkeypatch.setattr(
        CoreWorkbenchWorkspace,
        "resolve_provider_locator",
        resolver,
    )
    vendor.list_dir = AsyncMock(
        return_value={
            "entries": [
                {
                    "name": "report.txt",
                    "type": "FILE_TYPE_FILE",
                    "path": "/workdir/report.txt",
                    "size": "12",
                    "owner": "provider-internal",
                }
            ]
        }
    )

    prepared = await WorkbenchActionPolicy.prepare(
        object(),
        action="ListDir",
        request_body=request_body(
            {
                "app_id": "customer-app-7",
                "path": "/workdir",
                "depth": 1,
                "workspace_id": "ww_owned",
            }
        ),
        account_id="account-9",
        identity=identity,
        vendor_app=vendor,
        app_context=app_context,
    )

    assert prepared.local_response == {
        "entries": [
            {
                "name": "report.txt",
                "type": "FILE_TYPE_FILE",
                "path": "/workdir/report.txt",
                "size": "12",
            }
        ]
    }
    vendor.list_dir.assert_awaited_once_with(
        app_id="provider-app-7",
        path="/workdir",
        depth=1,
        workspace_id="6576c839-839c-4a54-b820-408aec31dbe0",
        user_id=identity.canonical_subject,
    )


@pytest.mark.asyncio
async def test_list_dir_rejects_path_traversal_before_workspace_resolution(
    monkeypatch,
    identity,
    vendor,
    app_context,
):
    resolver = AsyncMock()
    monkeypatch.setattr(
        CoreWorkbenchWorkspace,
        "resolve_provider_locator",
        resolver,
    )

    with pytest.raises(WorkbenchActionPolicyError, match="canonical /workdir"):
        await WorkbenchActionPolicy.prepare(
            object(),
            action="ListDir",
            request_body=request_body(
                {
                    "app_id": "customer-app-7",
                    "path": "/workdir/../secret",
                    "depth": 1,
                    "workspace_id": "ww_owned",
                }
            ),
            account_id="account-9",
            identity=identity,
            vendor_app=vendor,
            app_context=app_context,
        )

    resolver.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["Service", "Version", "Region", "Action"])
async def test_routing_fields_are_rejected_from_workbench_envelope(identity, vendor, field):
    with pytest.raises(WorkbenchActionPolicyError, match="unsupported fields"):
        await WorkbenchActionPolicy.prepare(
            None,
            action="DescribeApp",
            request_body=request_body(**{field: "attacker-controlled"}),
            account_id="account-9",
            identity=identity,
            vendor_app=vendor,
        )


@pytest.mark.asyncio
async def test_describe_app_uses_trusted_app_and_redacts_nested_secrets(identity, vendor):
    prepared = await WorkbenchActionPolicy.prepare(
        None,
        action="DescribeApp",
        request_body=request_body(),
        account_id="account-9",
        identity=identity,
        vendor_app=vendor,
    )

    assert prepared.payload == {
        "AppId": "provider-app-7",
        "FieldMask": {"Paths": ["AppConfig"]},
    }

    projected = await WorkbenchActionPolicy.project_response(
        None,
        prepared=prepared,
        account_id="account-9",
        response={
            "App": {
                "Metadata": {
                    "AppId": "provider-app-7",
                    "SpaceId": "space-secret",
                    "Name": "Customer app",
                },
                "SecretInfo": {"AppKey": "must-not-leak"},
                "Config": {
                    "Plugin": {
                        "HeaderParameterList": [{"Name": "Authorization", "Value": "secret"}],
                    }
                },
            },
            "RequestId": "request-1",
        },
    )

    assert projected == {
        "App": {
            "Metadata": {"Name": "Customer app"},
            "Config": {"Plugin": {}},
        },
        "RequestId": "request-1",
    }


@pytest.mark.asyncio
async def test_provider_error_does_not_echo_secret_bearing_message(identity, vendor):
    prepared = await WorkbenchActionPolicy.prepare(
        None,
        action="DescribeApp",
        request_body=request_body(),
        account_id="account-9",
        identity=identity,
        vendor_app=vendor,
    )

    projected = await WorkbenchActionPolicy.project_response(
        None,
        prepared=prepared,
        account_id="account-9",
        response={
            "Error": {
                "Code": "InvalidParameter",
                "Message": "bad AppKey provider-app-key-secret",
            },
            "RequestId": "request-err-1",
        },
    )

    assert projected == {
        "Error": {
            "Code": "InvalidParameter",
            "Message": "provider request failed",
        },
        "RequestId": "request-err-1",
    }


@pytest.mark.asyncio
async def test_copy_agent_is_idempotently_handled_by_trusted_provisioner(
    monkeypatch,
    identity,
    vendor,
):
    ensure = AsyncMock(return_value=SimpleNamespace(AgentId="user-agent-9"))
    monkeypatch.setattr(CoreAgent, "ensure", ensure)

    prepared = await WorkbenchActionPolicy.prepare(
        None,
        action="CopyAgentFromApp",
        request_body=request_body(),
        account_id="account-9",
        identity=identity,
        vendor_app=vendor,
    )

    ensure.assert_awaited_once_with(
        None,
        "account-9",
        "customer-app-7",
        vendor,
        identity_context=identity,
    )
    assert prepared.payload == {}
    assert prepared.local_response == {"ParentAgentId": "user-agent-9"}


@pytest.mark.asyncio
async def test_invalid_copy_payload_cannot_trigger_provisioning(monkeypatch, identity, vendor):
    ensure = AsyncMock(return_value=SimpleNamespace(AgentId="user-agent-9"))
    monkeypatch.setattr(CoreAgent, "ensure", ensure)

    with pytest.raises(WorkbenchActionPolicyError, match="outside the trusted context"):
        await WorkbenchActionPolicy.prepare(
            None,
            action="CopyAgentFromApp",
            request_body=request_body({"AppId": "attacker-app", "Kind": 0}),
            account_id="account-9",
            identity=identity,
            vendor_app=vendor,
        )

    ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_only_session_cannot_provision_or_create_resources(monkeypatch, identity, vendor):
    ensure = AsyncMock(return_value=SimpleNamespace(AgentId="user-agent-9"))
    monkeypatch.setattr(CoreAgent, "ensure", ensure)
    read_only_identity = WorkbenchIdentityContext(
        **{**identity.__dict__, "access_mode": "read_only"}
    )

    with pytest.raises(WorkbenchActionPolicyError, match="read-only"):
        await WorkbenchActionPolicy.prepare(
            None,
            action="CreateConversation",
            request_body=request_body(),
            account_id="account-9",
            identity=read_only_identity,
            vendor_app=vendor,
        )

    ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfirmed_provider_environment_is_fail_closed(identity, vendor):
    vendor.config = {**vendor.config, "ServiceVendor": "International"}

    with pytest.raises(WorkbenchActionPolicyError, match="no workbench Action policy") as exc_info:
        await WorkbenchActionPolicy.prepare(
            None,
            action="DescribeApp",
            request_body=request_body(),
            account_id="account-9",
            identity=identity,
            vendor_app=vendor,
        )

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_describe_agent_overwrites_client_identifiers(monkeypatch, identity, vendor):
    ensure = AsyncMock(return_value=SimpleNamespace(AgentId="user-agent-9"))
    monkeypatch.setattr(CoreAgent, "ensure", ensure)

    prepared = await WorkbenchActionPolicy.prepare(
        None,
        action="DescribeAgentDetail",
        request_body=request_body(),
        account_id="account-9",
        identity=identity,
        vendor_app=vendor,
    )

    assert prepared.payload == {
        "AppId": "provider-app-7",
        "AgentId": "user-agent-9",
    }

    with pytest.raises(WorkbenchActionPolicyError, match="outside the trusted context"):
        await WorkbenchActionPolicy.prepare(
            None,
            action="DescribeAgentDetail",
            request_body=request_body({"AgentId": "someone-else"}),
            account_id="account-9",
            identity=identity,
            vendor_app=vendor,
        )


@pytest.mark.asyncio
async def test_agent_actions_accept_current_client_identifiers_but_never_trust_them(
    monkeypatch,
    identity,
    vendor,
):
    ensure = AsyncMock(return_value=SimpleNamespace(AgentId="user-agent-9"))
    monkeypatch.setattr(CoreAgent, "ensure", ensure)

    copied = await WorkbenchActionPolicy.prepare(
        None,
        action="CopyAgentFromApp",
        request_body=request_body(
            {"Kind": 1, "AppId": "customer-app-7"}
        ),
        account_id="account-9",
        identity=identity,
        vendor_app=vendor,
    )
    described = await WorkbenchActionPolicy.prepare(
        None,
        action="DescribeAgentDetail",
        request_body=request_body(
            {
                "AppId": "customer-app-7",
                "AgentId": "user-agent-9",
                "Domain": 2,
            }
        ),
        account_id="account-9",
        identity=identity,
        vendor_app=vendor,
    )

    assert copied.local_response == {"ParentAgentId": "user-agent-9"}
    assert described.payload == {
        "AppId": "provider-app-7",
        "AgentId": "user-agent-9",
    }


@pytest.mark.asyncio
async def test_generic_modify_agent_remains_closed_to_browser_callers(
    identity,
    vendor,
):
    with pytest.raises(WorkbenchActionPolicyError, match="trusted server") as exc_info:
        await WorkbenchActionPolicy.prepare(
            None,
            action="ModifyAgent",
            request_body=request_body(
                {
                    "Agent": {"Instructions": "safe-looking patch"},
                    "UpdateMask": {"Paths": ["Instructions"]},
                }
            ),
            account_id="account-9",
            identity=identity,
            vendor_app=vendor,
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_create_conversation_injects_api_identity_and_records_ownership(
    monkeypatch,
    identity,
    vendor,
    app_context,
):
    monkeypatch.setattr(
        CoreAgent,
        "ensure",
        AsyncMock(return_value=SimpleNamespace(AgentId="user-agent-9")),
    )
    get_conversation = AsyncMock(return_value=None)
    create_conversation = AsyncMock()
    monkeypatch.setattr(CoreConversation, "get", get_conversation)
    monkeypatch.setattr(CoreConversation, "create", create_conversation)

    prepared = await WorkbenchActionPolicy.prepare(
        None,
        action="CreateConversation",
        request_body=request_body({"Title": "Quarterly report"}),
        account_id="account-9",
        identity=identity,
        vendor_app=vendor,
        app_context=app_context,
    )

    assert prepared.payload == {
        "Type": 5,
        "AppId": "provider-app-7",
        "AppKey": "provider-app-key-secret",
        "UserId": "napi:prod:customer:7:user:9",
        "AgentId": "user-agent-9",
    }

    response = {
        "ConversationId": "dd6a4e74-b2d4-463f-ba01-429304a4a142",
        "RequestId": "request-2",
    }
    projected = await WorkbenchActionPolicy.project_response(
        None,
        prepared=prepared,
        account_id="account-9",
        response=response,
    )

    create_conversation.assert_awaited_once_with(
        None,
        "account-9",
        "customer-app-7",
        title="Quarterly report",
        conversation_id="dd6a4e74-b2d4-463f-ba01-429304a4a142",
        workbench_identity=identity,
        workbench_app_context=app_context,
        workbench_agent_id="user-agent-9",
    )
    assert projected == response


@pytest.mark.asyncio
async def test_create_conversation_rejects_explicit_non_api_type(
    monkeypatch,
    identity,
    vendor,
    app_context,
):
    monkeypatch.setattr(
        CoreAgent,
        "ensure",
        AsyncMock(return_value=SimpleNamespace(AgentId="user-agent-9")),
    )

    with pytest.raises(
        WorkbenchActionPolicyError,
        match="CreateConversation Type must be between 5 and 5",
    ) as exc_info:
        await WorkbenchActionPolicy.prepare(
            None,
            action="CreateConversation",
            request_body=request_body(
                {
                    "Type": 1,
                    "AppId": "customer-app-7",
                    "AgentId": "user-agent-9",
                }
            ),
            account_id="account-9",
            identity=identity,
            vendor_app=vendor,
            app_context=app_context,
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_create_conversation_omitted_type_is_injected_as_api_scope(
    monkeypatch,
    identity,
    vendor,
    app_context,
):
    monkeypatch.setattr(
        CoreAgent,
        "ensure",
        AsyncMock(return_value=SimpleNamespace(AgentId="user-agent-9")),
    )

    prepared = await WorkbenchActionPolicy.prepare(
        None,
        action="CreateConversation",
        request_body=request_body(
            {
                "AppId": "customer-app-7",
                "AgentId": "user-agent-9",
            }
        ),
        account_id="account-9",
        identity=identity,
        vendor_app=vendor,
        app_context=app_context,
    )

    assert prepared.payload["Type"] == 5


@pytest.mark.asyncio
async def test_describe_conversation_requires_local_owner_and_forces_type_five(
    monkeypatch,
    identity,
    vendor,
    app_context,
):
    conversation_id = "4abd149a-e010-4a6c-bc52-1132658f149d"
    monkeypatch.setattr(
        CoreConversation,
        "get_owned",
        AsyncMock(
            return_value=SimpleNamespace(
                AccountId="account-9",
                ApplicationId="customer-app-7",
            )
        ),
    )
    monkeypatch.setattr(
        CoreAgent,
        "ensure",
        AsyncMock(return_value=SimpleNamespace(AgentId="user-agent-9")),
    )

    prepared = await WorkbenchActionPolicy.prepare(
        None,
        action="DescribeConversation",
        request_body=request_body({"ConversationId": conversation_id, "Type": 5}),
        account_id="account-9",
        identity=identity,
        vendor_app=vendor,
        app_context=app_context,
    )

    assert prepared.payload == {
        "ConversationId": conversation_id,
        "Type": 5,
        "AppKey": "provider-app-key-secret",
        "UserId": "napi:prod:customer:7:user:9",
    }

    with pytest.raises(WorkbenchActionPolicyError, match="outside the trusted context"):
        await WorkbenchActionPolicy.project_response(
            None,
            prepared=prepared,
            account_id="account-9",
            response={
                "ConversationId": conversation_id,
                "Type": 5,
                "AppId": "another-provider-app",
                "AgentId": "user-agent-9",
            },
        )


@pytest.mark.asyncio
async def test_conversation_list_bounds_pagination_and_filters_provider_leakage(
    monkeypatch,
    identity,
    vendor,
    app_context,
):
    monkeypatch.setattr(
        CoreAgent,
        "ensure",
        AsyncMock(return_value=SimpleNamespace(AgentId="user-agent-9")),
    )
    monkeypatch.setattr(
        CoreConversation,
        "get_owned",
        AsyncMock(return_value=SimpleNamespace(Id="owned-conversation")),
    )
    prepared = await WorkbenchActionPolicy.prepare(
        None,
        action="DescribeConversationList",
        request_body=request_body({"Keyword": "report", "Limit": 50, "Offset": 0}),
        account_id="account-9",
        identity=identity,
        vendor_app=vendor,
        app_context=app_context,
    )

    assert prepared.payload["Type"] == 5
    assert prepared.payload["UserId"] == identity.canonical_subject
    assert prepared.payload["AgentId"] == "user-agent-9"

    projected = await WorkbenchActionPolicy.project_response(
        None,
        prepared=prepared,
        account_id="account-9",
        response={
            "ConversationList": [
                {
                    "ConversationId": "4abd149a-e010-4a6c-bc52-1132658f149d",
                    "Type": 5,
                    "AppId": "provider-app-7",
                    "AgentId": "user-agent-9",
                    "Title": "owned",
                },
                {
                    "ConversationId": "5abd149a-e010-4a6c-bc52-1132658f149d",
                    "Type": 5,
                    "AppId": "provider-app-8",
                    "AgentId": "user-agent-8",
                    "Title": "foreign",
                },
            ],
            "TotalCount": "2",
        },
    )

    assert projected["ConversationList"] == [
        {
            "ConversationId": "4abd149a-e010-4a6c-bc52-1132658f149d",
            "Type": 5,
            "AgentId": "user-agent-9",
            "Title": "owned",
        }
    ]
    assert projected["TotalCount"] == "1"


@pytest.mark.asyncio
async def test_message_history_rejects_cross_conversation_records(
    monkeypatch,
    identity,
    vendor,
    app_context,
):
    conversation_id = "4abd149a-e010-4a6c-bc52-1132658f149d"
    monkeypatch.setattr(
        CoreConversation,
        "get_owned",
        AsyncMock(
            return_value=SimpleNamespace(
                AccountId="account-9",
                ApplicationId="customer-app-7",
            )
        ),
    )
    monkeypatch.setattr(
        CoreAgent,
        "ensure",
        AsyncMock(return_value=SimpleNamespace(AgentId="user-agent-9")),
    )
    prepared = await WorkbenchActionPolicy.prepare(
        None,
        action="DescribeConversationMessageList",
        request_body=request_body(
            {
                "ConversationId": conversation_id,
                "Limit": 10,
                "RecordQueryDirection": 1,
            }
        ),
        account_id="account-9",
        identity=identity,
        vendor_app=vendor,
        app_context=app_context,
    )

    assert prepared.payload["Type"] == 5
    assert prepared.payload["Limit"] == 10

    with pytest.raises(WorkbenchActionPolicyError, match="another conversation"):
        await WorkbenchActionPolicy.project_response(
            None,
            prepared=prepared,
            account_id="account-9",
            response={
                "MessageList": [
                    {
                        "ConversationId": "5abd149a-e010-4a6c-bc52-1132658f149d",
                        "MessageId": "message-1",
                    }
                ]
            },
        )


@pytest.mark.asyncio
async def test_message_history_redacts_private_file_locators(
    monkeypatch,
    identity,
    vendor,
    app_context,
):
    conversation_id = "4abd149a-e010-4a6c-bc52-1132658f149d"
    monkeypatch.setattr(
        CoreConversation,
        "get_owned",
        AsyncMock(
            return_value=SimpleNamespace(
                AccountId="account-9",
                ApplicationId="customer-app-7",
            )
        ),
    )
    monkeypatch.setattr(
        CoreAgent,
        "ensure",
        AsyncMock(return_value=SimpleNamespace(AgentId="user-agent-9")),
    )
    prepared = await WorkbenchActionPolicy.prepare(
        None,
        action="DescribeConversationMessageList",
        request_body=request_body({"ConversationId": conversation_id}),
        account_id="account-9",
        identity=identity,
        vendor_app=vendor,
        app_context=app_context,
    )

    projected = await WorkbenchActionPolicy.project_response(
        None,
        prepared=prepared,
        account_id="account-9",
        response={
            "MessageList": [
                {
                    "ConversationId": conversation_id,
                    "MessageId": "message-1",
                    "Content": (
                        "https://private.cos.ap-guangzhou.myqcloud.com/"
                        "workbench/customer-7/binding-9/private.pdf"
                        "?q-sign-algorithm=sha1&q-signature=sensitive"
                    ),
                }
            ]
        },
    )

    rendered = str(projected)
    assert "q-sign" not in rendered
    assert "workbench/customer-7" not in rendered


@pytest.mark.asyncio
async def test_describe_agent_rejects_missing_provider_agent_id(monkeypatch, identity, vendor):
    monkeypatch.setattr(
        CoreAgent,
        "ensure",
        AsyncMock(return_value=SimpleNamespace(AgentId="user-agent-9")),
    )
    prepared = await WorkbenchActionPolicy.prepare(
        None,
        action="DescribeAgentDetail",
        request_body=request_body(),
        account_id="account-9",
        identity=identity,
        vendor_app=vendor,
    )

    with pytest.raises(WorkbenchActionPolicyError, match="outside the trusted context") as exc_info:
        await WorkbenchActionPolicy.project_response(
            None,
            prepared=prepared,
            account_id="account-9",
            response={"Agent": {"Name": "missing identifier"}},
        )

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", ["ConversationId", "AgentId"])
async def test_conversation_list_rejects_missing_provider_identifiers(
    monkeypatch,
    identity,
    vendor,
    app_context,
    missing_field,
):
    monkeypatch.setattr(
        CoreAgent,
        "ensure",
        AsyncMock(return_value=SimpleNamespace(AgentId="user-agent-9")),
    )
    prepared = await WorkbenchActionPolicy.prepare(
        None,
        action="DescribeConversationList",
        request_body=request_body(),
        account_id="account-9",
        identity=identity,
        vendor_app=vendor,
        app_context=app_context,
    )
    item = {
        "ConversationId": "4abd149a-e010-4a6c-bc52-1132658f149d",
        "Type": 5,
        "AppId": "provider-app-7",
        "AgentId": "user-agent-9",
    }
    item.pop(missing_field)

    with pytest.raises(WorkbenchActionPolicyError) as exc_info:
        await WorkbenchActionPolicy.project_response(
            None,
            prepared=prepared,
            account_id="account-9",
            response={"ConversationList": [item]},
        )

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_message_history_rejects_missing_provider_conversation_id(
    monkeypatch,
    identity,
    vendor,
    app_context,
):
    conversation_id = "4abd149a-e010-4a6c-bc52-1132658f149d"
    monkeypatch.setattr(
        CoreConversation,
        "get_owned",
        AsyncMock(
            return_value=SimpleNamespace(
                AccountId="account-9",
                ApplicationId="customer-app-7",
            )
        ),
    )
    monkeypatch.setattr(
        CoreAgent,
        "ensure",
        AsyncMock(return_value=SimpleNamespace(AgentId="user-agent-9")),
    )
    prepared = await WorkbenchActionPolicy.prepare(
        None,
        action="DescribeConversationMessageList",
        request_body=request_body({"ConversationId": conversation_id}),
        account_id="account-9",
        identity=identity,
        vendor_app=vendor,
        app_context=app_context,
    )

    with pytest.raises(WorkbenchActionPolicyError) as exc_info:
        await WorkbenchActionPolicy.project_response(
            None,
            prepared=prepared,
            account_id="account-9",
            response={"MessageList": [{"MessageId": "message-1"}]},
        )

    assert exc_info.value.status_code == 502
