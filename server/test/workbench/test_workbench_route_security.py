import importlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sanic.exceptions import SanicException

from config import tagentic_config
from core.agent import CoreAgent
from core.conversation import CoreConversation
from core.workbench_policy import WorkbenchPolicyError


class _FakeApp:
    config = SimpleNamespace(WORKBENCH_OAUTH_STATE_TTL_SECONDS=300)

    def middleware(self, *_args, **_kwargs):
        return lambda function: function

    def listener(self, *_args, **_kwargs):
        return lambda function: function

    def add_route(self, *_args, **_kwargs):
        return None

    def add_websocket_route(self, *_args, **_kwargs):
        return None


for module_name in (
    "middleware.workbench_security",
    "router.agent",
    "router.feedback",
    "router.chat",
    "router.workbench_integrations",
    "router.workbench_sandbox",
):
    sys.modules.pop(module_name, None)

with patch("app_factory.TAgenticApp.get_app", return_value=_FakeApp()):
    security_module = importlib.import_module("middleware.workbench_security")
    agent_router = importlib.import_module("router.agent")
    feedback_router = importlib.import_module("router.feedback")
    chat_router = importlib.import_module("router.chat")
    integrations_router = importlib.import_module("router.workbench_integrations")
    sandbox_router = importlib.import_module("router.workbench_sandbox")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"),
        ("GET", "/static/app/index"),
        ("HEAD", "/static/logo.svg"),
        ("GET", "/auth/sso"),
        ("GET", "/account/info"),
        ("GET", "/application/list"),
        ("GET", "/agent/config"),
        ("POST", "/chat/message"),
        ("GET", "/chat/turn/events"),
        ("POST", "/chat/turn/cancel"),
        ("GET", "/chat/messages"),
        ("GET", "/chat/conversations"),
        ("POST", "/chat/conversation/delete"),
        ("POST", "/feedback/rate"),
        ("POST", "/file/upload"),
        ("POST", "/file/parse"),
        ("GET", "/integrations"),
        ("POST", "/integrations/bindings"),
        ("POST", "/integrations/connectors/connector-safe/oauth/start"),
        ("POST", "/integrations/connectors/connector-safe/disconnect"),
        ("GET", "/integrations/oauth/callback/example"),
        ("POST", "/adp/DescribeConversation"),
        ("GET", "/sandbox/config"),
        ("GET", "/sandbox"),
        ("POST", "/sandbox"),
        ("GET", "/sandbox/acceptance/provider-start-count"),
        ("GET", "/sandbox/acceptance/instances"),
        ("POST", "/sandbox/acceptance/cleanup"),
        ("GET", "/sandbox/sbx_0123456789abcdef0123456789abcdef"),
        ("POST", "/sandbox/sbx_0123456789abcdef0123456789abcdef/pause"),
        ("POST", "/sandbox/sbx_0123456789abcdef0123456789abcdef/resume"),
        ("POST", "/sandbox/sbx_0123456789abcdef0123456789abcdef/stop"),
        ("POST", "/sandbox/sbx_0123456789abcdef0123456789abcdef/code"),
        ("POST", "/sandbox/sbx_0123456789abcdef0123456789abcdef/shell"),
        ("POST", "/sandbox/sbx_0123456789abcdef0123456789abcdef/shell/stream"),
        ("GET", "/sandbox/sbx_0123456789abcdef0123456789abcdef/files"),
        ("PUT", "/sandbox/sbx_0123456789abcdef0123456789abcdef/files"),
        ("POST", "/sandbox/sbx_0123456789abcdef0123456789abcdef/pty"),
        ("GET", "/sandbox/sbx_0123456789abcdef0123456789abcdef/pty/connect"),
    ],
)
def test_audited_workbench_endpoint_methods_are_allowed(method, path):
    assert security_module.workbench_endpoint_is_allowed(method, path)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/login"),
        ("GET", "/account/providers"),
        ("GET", "/oauth/callback/tencent"),
        ("POST", "/agent/config"),
        ("GET", "/adp/DescribeApp"),
        ("POST", "/adp/DescribeApp/nested"),
        ("OPTIONS", "/chat/message"),
        ("GET", "/unknown"),
        ("GET", "/helper/asr/url"),
        ("GET", "/integrations/connectors/connector-safe/oauth/start"),
        ("POST", "/integrations/oauth/callback/example"),
        ("GET", "/integrations/oauth/callback/example/nested"),
        ("DELETE", "/sandbox"),
        ("POST", "/sandbox/acceptance/provider-start-count"),
        ("POST", "/sandbox/acceptance/instances"),
        ("GET", "/sandbox/acceptance/cleanup"),
        ("DELETE", "/sandbox/sbx_0123456789abcdef0123456789abcdef"),
        ("GET", "/sandbox/sbx_0123456789abcdef0123456789abcdef/shell"),
        ("POST", "/sandbox/sbx_0123456789abcdef0123456789abcdef/files"),
        ("POST", "/sandbox/sbx_0123456789abcdef0123456789abcdef/unknown"),
        ("POST", "/sandbox/sbx_0123456789abcdef0123456789abcdef/pty/connect"),
        ("GET", "/sandbox/sbx_0123456789abcdef0123456789abcdef/pty/connect/extra"),
        ("GET", "/sandbox/a/b/c/d"),
    ],
)
def test_unlisted_workbench_endpoint_methods_are_denied(method, path):
    assert not security_module.workbench_endpoint_is_allowed(method, path)


@pytest.mark.parametrize(
    ("policy_error", "expected_code", "expected_status"),
    [
        (
            WorkbenchPolicyError("workbench capability is disabled: sandbox", 403),
            "sandbox_capability_disabled",
            403,
        ),
        (
            WorkbenchPolicyError("workbench capability is disabled: files", 403),
            "sandbox_files_disabled",
            403,
        ),
        (
            WorkbenchPolicyError("workbench is read-only", 403),
            "sandbox_read_only",
            403,
        ),
        (
            WorkbenchPolicyError("workbench limits are invalid", 503),
            "sandbox_policy_invalid",
            503,
        ),
    ],
)
def test_sandbox_policy_errors_keep_status_and_project_stable_codes(
    policy_error,
    expected_code,
    expected_status,
):
    projected = sandbox_router._error(policy_error)

    assert projected.status_code == expected_status
    assert str(projected) == expected_code


@pytest.mark.asyncio
async def test_pty_websocket_rejects_cross_origin_before_ticket_or_database_access(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_PUBLIC_BASE_URL",
        "https://gateway.example.com/workbench",
    )
    request = SimpleNamespace(
        server_path="/sandbox/sbx_0123456789abcdef0123456789abcdef/pty/connect",
        method="GET",
        headers={
            "origin": "https://attacker.example",
            "sec-websocket-protocol": "claw-workbench-pty-v1, ticket." + "a" * 43,
        },
        host="gateway.example.com",
    )
    response = await sandbox_router.authorize_workbench_pty_handshake(request)
    assert response.status == 403
    assert b"pty_handshake_rejected" in response.body


def test_helper_asr_requires_explicit_workbench_opt_in():
    assert security_module.workbench_endpoint_is_allowed(
        "GET",
        "/helper/asr/url",
        helper_asr_enabled=True,
    )
    assert not security_module.workbench_endpoint_is_allowed(
        "POST",
        "/helper/asr/url",
        helper_asr_enabled=True,
    )


@pytest.mark.asyncio
async def test_sandbox_recovery_route_reads_only_owner_scoped_local_state(monkeypatch):
    expected = {
        "sandbox_id": "sbx_0123456789abcdef0123456789abcdef",
        "conversation_id": "11111111-1111-4111-8111-111111111111",
        "status": "provider_unknown",
    }
    find_local = AsyncMock(return_value=expected)
    monkeypatch.setattr(
        sandbox_router.WorkbenchSandboxService,
        "find_local",
        find_local,
    )
    request = SimpleNamespace(
        args={"conversation_id": expected["conversation_id"]},
        ctx=SimpleNamespace(
            db=object(),
            account_id="account-9",
            workbench_context=SimpleNamespace(binding_id="binding-9"),
            workbench_app_context=SimpleNamespace(
                application_id="customer-app-7",
                runtime=SimpleNamespace(uses_provider_user_agent=True),
            ),
        ),
    )

    response = await sandbox_router.WorkbenchSandboxListApi.get.__wrapped__(
        sandbox_router.WorkbenchSandboxListApi(),
        request,
    )

    assert response.status == 200
    assert json.loads(response.body) == expected
    assert response.headers["Cache-Control"] == "no-store"
    find_local.assert_awaited_once_with(
        request.ctx.db,
        conversation_id=expected["conversation_id"],
        account_id="account-9",
        identity=request.ctx.workbench_context,
        app_context=request.ctx.workbench_app_context,
    )


@pytest.mark.asyncio
async def test_turn_cancel_route_persists_only_local_intent(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    cancel = AsyncMock(
        return_value=SimpleNamespace(
            turn_id="wt_123",
            status="cancel_requested",
            provider_cancel_supported=False,
        )
    )
    monkeypatch.setattr(chat_router.WorkbenchTurnManager, "request_cancel", cancel)
    request = SimpleNamespace(
        json={"TurnId": "wt_123", "ReasonCode": "user_stop"},
        ctx=SimpleNamespace(
            db=object(),
            account_id="account-9",
            workbench_context=SimpleNamespace(binding_id="binding-9"),
            workbench_app_context=SimpleNamespace(application_id="customer-app-7"),
        ),
    )

    response = await chat_router.WorkbenchTurnCancelApi.post.__wrapped__(
        chat_router.WorkbenchTurnCancelApi(),
        request,
    )

    assert response.status == 202
    payload = json.loads(response.body)
    assert payload == {
        "TurnId": "wt_123",
        "Status": "cancel_requested",
        "ProviderCancelSupported": False,
        "BackgroundConsumptionContinues": True,
    }
    assert response.headers["Cache-Control"] == "no-store"
    cancel.assert_awaited_once_with(
        request.ctx.db,
        turn_id="wt_123",
        account_id="account-9",
        identity=request.ctx.workbench_context,
        app_context=request.ctx.workbench_app_context,
        reason_code="user_stop",
    )


def _configure_valid_files(monkeypatch, tmp_path):
    values = {
        "WORKBENCH_FILES_ENABLED": True,
        "WORKBENCH_FILE_SCANNER_HOST": "workbench-clamav",
        "WORKBENCH_FILE_SCANNER_PORT": 3310,
        "WORKBENCH_FILE_SCANNER_TIMEOUT_SECONDS": 30,
        "WORKBENCH_FILE_SCANNER_MAX_BYTES": 50 * 1024 * 1024,
        "WORKBENCH_FILE_ABSOLUTE_MAX_BYTES": 50 * 1024 * 1024,
        "WORKBENCH_FILE_QUARANTINE_CAPACITY_BYTES": 100 * 1024 * 1024,
        "WORKBENCH_FILE_MAX_CONCURRENT_UPLOADS": 2,
        "WORKBENCH_FILE_QUARANTINE_DIR": str(tmp_path),
        "WORKBENCH_FILE_COS_REGION": "ap-guangzhou",
        "WORKBENCH_FILE_COS_BUCKET": "private-workbench-1234567890",
        "WORKBENCH_FILE_COS_SECRET_ID": "secret-id",
        "WORKBENCH_FILE_COS_SECRET_KEY": "secret-key",
        "WORKBENCH_FILE_URL_EXPIRE_SECONDS": 300,
    }
    for name, value in values.items():
        monkeypatch.setattr(tagentic_config, name, value)


def test_disabled_file_pipeline_needs_no_storage_configuration(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_FILES_ENABLED", False)

    security_module.validate_workbench_file_configuration()


def test_enabled_file_pipeline_accepts_complete_security_contract(monkeypatch, tmp_path):
    _configure_valid_files(monkeypatch, tmp_path)

    security_module.validate_workbench_file_configuration()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("WORKBENCH_FILE_SCANNER_HOST", "", "SCANNER_HOST"),
        ("WORKBENCH_FILE_SCANNER_PORT", 65536, "SCANNER_PORT"),
        ("WORKBENCH_FILE_SCANNER_TIMEOUT_SECONDS", 121, "SCANNER_TIMEOUT_SECONDS"),
        ("WORKBENCH_FILE_SCANNER_MAX_BYTES", 1, "SCANNER_MAX_BYTES"),
        ("WORKBENCH_FILE_ABSOLUTE_MAX_BYTES", 50 * 1024 * 1024 + 1, "ABSOLUTE_MAX_BYTES"),
        ("WORKBENCH_FILE_QUARANTINE_CAPACITY_BYTES", 1, "QUARANTINE_CAPACITY_BYTES"),
        ("WORKBENCH_FILE_QUARANTINE_CAPACITY_BYTES", 100 * 1024 * 1024 + 1, "QUARANTINE_CAPACITY_BYTES"),
        ("WORKBENCH_FILE_COS_SECRET_KEY", "", "COS configuration"),
        ("WORKBENCH_FILE_URL_EXPIRE_SECONDS", 901, "URL_EXPIRE_SECONDS"),
    ],
)
def test_enabled_file_pipeline_rejects_incomplete_security_contract(
    monkeypatch,
    tmp_path,
    field,
    value,
    message,
):
    _configure_valid_files(monkeypatch, tmp_path)
    monkeypatch.setattr(tagentic_config, field, value)

    with pytest.raises(RuntimeError, match=message):
        security_module.validate_workbench_file_configuration()


def test_enabled_file_pipeline_requires_dedicated_existing_directory(
    monkeypatch,
    tmp_path,
):
    _configure_valid_files(monkeypatch, tmp_path)
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_FILE_QUARANTINE_DIR",
        str(tmp_path / "missing"),
    )

    with pytest.raises(RuntimeError, match="dedicated directory"):
        security_module.validate_workbench_file_configuration()


@pytest.mark.asyncio
async def test_non_workbench_mode_does_not_apply_endpoint_allowlist(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", False)
    request = SimpleNamespace(method="POST", server_path="/login")

    assert await security_module.restrict_workbench_endpoint_surface(request) is None


@pytest.mark.asyncio
async def test_workbench_allowlist_returns_non_cacheable_404(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    request = SimpleNamespace(method="POST", server_path="/login")

    response = await security_module.restrict_workbench_endpoint_surface(request)

    assert response.status == 404
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_oauth_start_sets_host_only_callback_scoped_transaction_cookie(monkeypatch):
    start = AsyncMock(
        return_value={
            "authorization_url": "https://oauth.example/authorize",
            "provider_id": "example",
            "connector_id": "connector-safe",
            "expires_at": "2026-08-09T00:05:00Z",
            "_browser_cookie": "browser-transaction-secret",
        }
    )
    monkeypatch.setattr(integrations_router.WorkbenchIntegrations, "start_oauth", start)
    monkeypatch.setattr(integrations_router, "_require_csrf", lambda _request: None)
    monkeypatch.setattr(
        integrations_router.WorkbenchIntegrationPolicy,
        "callback_cookie_path",
        lambda: "/workbench/integrations/oauth/callback/",
    )
    request = SimpleNamespace(
        ctx=SimpleNamespace(
            db=object(),
            account_id="account-9",
            workbench_context=object(),
            workbench_app_context=object(),
            session_claims={"sid": "s" * 43},
        )
    )

    response = await integrations_router.WorkbenchConnectorOAuthStartApi.post.__wrapped__(
        integrations_router.WorkbenchConnectorOAuthStartApi(),
        request,
        "connector-safe",
    )

    cookie = str(response.cookies.cookies[0])
    assert "claw_oauth_tx=browser-transaction-secret" in cookie
    assert "Path=/workbench/integrations/oauth/callback/" in cookie
    assert "SameSite=Lax" in cookie
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "Domain=" not in cookie
    start.assert_awaited_once_with(
        request.ctx.db,
        account_id="account-9",
        identity=request.ctx.workbench_context,
        app_context=request.ctx.workbench_app_context,
        connector_id="connector-safe",
        session_claims={"sid": "s" * 43},
    )


class _Parser:
    def __init__(self, values):
        self.values = values

    def add_argument(self, *_args, **_kwargs):
        return None

    def parse_args(self, _request):
        return dict(self.values)


@pytest.mark.asyncio
async def test_read_only_agent_get_never_provisions(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(
        agent_router.reqparse,
        "RequestParser",
        lambda: _Parser({"ApplicationId": "customer-app-7"}),
    )
    get = AsyncMock(return_value=SimpleNamespace(AgentId="agent-9"))
    ensure = AsyncMock()
    monkeypatch.setattr(CoreAgent, "get", get)
    monkeypatch.setattr(CoreAgent, "ensure", ensure)
    get_vendor_app = Mock()
    monkeypatch.setattr(agent_router, "app", SimpleNamespace(get_vendor_app=get_vendor_app))
    request = SimpleNamespace(
        ctx=SimpleNamespace(
            db=object(),
            account_id="account-9",
            workbench_context=SimpleNamespace(access_mode="read_only"),
            workbench_app_context=SimpleNamespace(
                application_id="customer-app-7",
                runtime=SimpleNamespace(uses_provider_user_agent=True),
            ),
        )
    )

    response = await agent_router.AgentConfigApi.get.__wrapped__(
        agent_router.AgentConfigApi(),
        request,
    )

    assert response.status == 200
    get.assert_awaited_once_with(request.ctx.db, "account-9", "customer-app-7")
    ensure.assert_not_awaited()
    get_vendor_app.assert_not_called()


@pytest.mark.asyncio
async def test_workbench_feedback_fails_before_untrusted_record_lookup(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(
        feedback_router.reqparse,
        "RequestParser",
        lambda: _Parser(
            {
                "ConversationId": "4abd149a-e010-4a6c-bc52-1132658f149d",
                "RecordId": "record-1",
                "Score": 1,
                "ApplicationId": "customer-app-7",
            }
        ),
    )
    get_application_id = AsyncMock()
    monkeypatch.setattr(CoreConversation, "get_application_id", get_application_id)
    request = SimpleNamespace(
        ctx=SimpleNamespace(
            db=object(),
            account_id="account-9",
            workbench_context=SimpleNamespace(access_mode="active"),
            workbench_app_context=SimpleNamespace(
                application_id="customer-app-7",
                capabilities=("chat",),
            ),
        )
    )

    with pytest.raises(SanicException, match="Record ownership") as exc_info:
        await feedback_router.TCADPFeedbackRateApi.post.__wrapped__(
            feedback_router.TCADPFeedbackRateApi(),
            request,
        )

    assert exc_info.value.status_code == 503
    get_application_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_workbench_chat_rejects_foreign_conversation_before_turn_creation(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(
        chat_router.reqparse,
        "RequestParser",
        lambda: _Parser(
            {
                "Contents": [{"Type": "text", "Text": "hello"}],
                "ConversationId": "foreign-conversation",
                "ApplicationId": "ignored-browser-app",
                "SearchNetwork": False,
                "CustomVariables": {},
                "IsChannel": False,
                "ClientRequestId": "dc06a8bd-5b4f-4da4-8457-5e11ca171f37",
            }
        ),
    )
    prepare = AsyncMock(return_value=[{"Type": "text", "Text": "hello"}])
    get_owned = AsyncMock(return_value=None)
    create_or_get = AsyncMock()
    monkeypatch.setattr(chat_router.WorkbenchFileOwnership, "prepare_chat_contents", prepare)
    monkeypatch.setattr(CoreConversation, "get_owned", get_owned)
    monkeypatch.setattr(chat_router.WorkbenchTurnManager, "create_or_get", create_or_get)
    request = SimpleNamespace(
        json={
            "Contents": [{"Type": "text", "Text": "hello"}],
            "ConversationId": "foreign-conversation",
            "ApplicationId": "ignored-browser-app",
            "SearchNetwork": False,
            "IsChannel": False,
            "ClientRequestId": "dc06a8bd-5b4f-4da4-8457-5e11ca171f37",
        },
        ctx=SimpleNamespace(
            db=object(),
            account_id="account-9",
            workbench_context=SimpleNamespace(access_mode="active"),
            workbench_app_context=SimpleNamespace(
                application_id="customer-app-7",
                capabilities=("chat",),
            ),
        ),
    )

    with pytest.raises(SanicException, match="conversation not found") as exc_info:
        await chat_router.ChatMessageApi.post.__wrapped__(
            chat_router.ChatMessageApi(),
            request,
        )

    assert exc_info.value.status_code == 404
    get_owned.assert_awaited_once_with(
        request.ctx.db,
        "account-9",
        "customer-app-7",
        "foreign-conversation",
        workbench_identity=request.ctx.workbench_context,
        workbench_app_context=request.ctx.workbench_app_context,
    )
    create_or_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_workbench_history_always_uses_v2_and_never_legacy_fallback(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(
        chat_router.reqparse,
        "RequestParser",
        lambda: _Parser(
            {
                "ConversationId": "conversation-1",
                "LastRecordId": None,
                "ShareId": None,
            }
        ),
    )
    vendor = SimpleNamespace(
        get_messages_v2=AsyncMock(
            return_value={
                "Records": [],
                "HasMoreBefore": False,
                "LastRecordId": "",
            }
        ),
        get_messages=AsyncMock(),
        get_info=AsyncMock(),
    )
    monkeypatch.setattr(
        chat_router,
        "app",
        SimpleNamespace(
            config=SimpleNamespace(CHAT_MESSAGE_PAGE_SIZE=100),
            get_vendor_app=Mock(return_value=vendor),
        ),
    )
    monkeypatch.setattr(chat_router, "check_login", Mock(return_value={"sub": "user-9"}))
    monkeypatch.setattr(chat_router, "authorize_workbench_request", AsyncMock())
    monkeypatch.setattr(
        CoreConversation,
        "get_read_context",
        AsyncMock(
            return_value=SimpleNamespace(
                application_id="customer-app-7",
                provider_app_id="provider-app-7",
                app_profile_id="17",
                config_version=5,
            )
        ),
    )
    request = SimpleNamespace(
        ctx=SimpleNamespace(
            db=object(),
            account_id="account-9",
            apps_info=[],
            workbench_context=SimpleNamespace(access_mode="active"),
            workbench_app_context=SimpleNamespace(
                application_id="customer-app-7",
                capabilities=("chat",),
            ),
        )
    )

    response = await chat_router.ChatMessageListApi().get(request)

    assert response.status == 200
    vendor.get_messages_v2.assert_awaited_once()
    vendor.get_messages.assert_not_awaited()
    vendor.get_info.assert_not_awaited()
