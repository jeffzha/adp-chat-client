import hashlib
import hmac
import time
from unittest.mock import AsyncMock

import pytest
from sanic import json

from config import tagentic_config
from core.workbench_control import (
    WORKBENCH_CONTRACT_VERSION,
    WORKBENCH_CONTRACT_VERSION_HEADER,
    WORKBENCH_LIMIT_MAXIMUMS,
    WorkbenchControlClient,
    WorkbenchControlError,
)
from util.auth_cookie import add_auth_token_cookie


def _identity_payload() -> dict:
    return {
        "binding_id": "binding-1",
        "canonical_subject": "napi:prod:customer:7:user:9",
        "customer_id": 7,
        "new_api_user_id": 9,
        "auth_epoch": 1,
        "display_name": "User 9",
        "application_id": "claw-app-7",
        "app_profile_id": "profile-7",
        "config_version": 1,
        "access_mode": "active",
        "allowed": True,
    }


@pytest.mark.asyncio
async def test_ticket_consume_submits_browser_binding(monkeypatch):
    request = AsyncMock(return_value=_identity_payload())
    monkeypatch.setattr(WorkbenchControlClient, "_request", request)

    await WorkbenchControlClient.consume_ticket("one-time-ticket", "browser-binding")

    request.assert_awaited_once_with(
        "POST",
        "/api/internal/workbench/tickets/consume",
        payload={
            "ticket": "one-time-ticket",
            "browser_binding": "browser-binding",
        },
    )


@pytest.mark.asyncio
async def test_app_context_accepts_typed_sandbox_and_rejects_unknown_capability(monkeypatch):
    payload = {
        "application_id": "customer-app-7",
        "app_profile_id": "17",
        "config_version": 1,
        "auth_epoch": 1,
        "vendor": "Tencent",
        "service_vendor": "ChinaTencentCloud",
        "app_id": "provider-app-7",
        "app_key": "app-key",
        "space_id": "space-7",
        "template_agent_id": "template-7",
        "secret_id": "secret-id",
        "secret_key": "secret-key",
        "customer_id": 7,
        "provider_environment": "china_tencent_cloud",
        "region": "ap-guangzhou",
        "expires_at": 1786200300,
        "capabilities": ["chat", "sandbox"],
        "limits": {
            "customer_concurrency": 2,
            "user_concurrency": 1,
            "max_runtime_seconds": 600,
            "max_reasoning_rounds": 20,
            "max_output_tokens": 8192,
            "web_search_per_turn": 0,
            "max_file_bytes": 1024,
        },
    }
    monkeypatch.setattr(WorkbenchControlClient, "_request", AsyncMock(return_value=payload))
    context = await WorkbenchControlClient.get_app_context(
        binding_id="binding-7",
        canonical_subject="napi:prod:customer:7:user:9",
        auth_epoch=1,
        requested_app_profile_id=17,
        requested_config_version=1,
        purpose="interactive",
    )
    assert context.capabilities == ("chat", "sandbox")

    payload["capabilities"] = ["chat", "sandbox_everything"]
    with pytest.raises(WorkbenchControlError, match="capability is unknown"):
        await WorkbenchControlClient.get_app_context(
            binding_id="binding-7",
            canonical_subject="napi:prod:customer:7:user:9",
            auth_epoch=1,
            requested_app_profile_id=17,
            requested_config_version=1,
            purpose="interactive",
        )

    payload["capabilities"] = ["chat", "sandbox"]
    payload["payment_status"] = "paid"
    with pytest.raises(WorkbenchControlError, match="unknown fields"):
        await WorkbenchControlClient.get_app_context(
            binding_id="binding-7",
            canonical_subject="napi:prod:customer:7:user:9",
            auth_epoch=1,
            requested_app_profile_id=17,
            requested_config_version=1,
            purpose="interactive",
        )

    payload.pop("payment_status")
    payload["auth_epoch"] = 0
    with pytest.raises(WorkbenchControlError, match="failed validation"):
        await WorkbenchControlClient.get_app_context(
            binding_id="binding-7",
            canonical_subject="napi:prod:customer:7:user:9",
            auth_epoch=1,
            requested_app_profile_id=17,
            requested_config_version=1,
            purpose="interactive",
        )


@pytest.mark.asyncio
async def test_app_context_request_is_exact_and_purpose_bound(monkeypatch):
    request = AsyncMock(
        side_effect=WorkbenchControlError("expected test rejection", 403)
    )
    monkeypatch.setattr(WorkbenchControlClient, "_request", request)

    with pytest.raises(WorkbenchControlError, match="expected test rejection"):
        await WorkbenchControlClient.get_app_context(
            binding_id="binding-7",
            canonical_subject="napi:prod:customer:7:user:9",
            auth_epoch=3,
            requested_app_profile_id=17,
            requested_config_version=4,
            purpose="scheduled_task",
        )

    request.assert_awaited_once_with(
        "POST",
        "/api/internal/workbench/app-context",
        payload={
            "binding_id": "binding-7",
            "canonical_subject": "napi:prod:customer:7:user:9",
            "auth_epoch": 3,
            "requested_app_profile_id": 17,
            "requested_config_version": 4,
            "purpose": "scheduled_task",
        },
    )

    request.reset_mock()
    with pytest.raises(WorkbenchControlError, match="request is invalid"):
        await WorkbenchControlClient.get_app_context(
            binding_id="binding-7",
            canonical_subject="napi:prod:customer:7:user:9",
            auth_epoch=3,
            requested_app_profile_id=17,
            requested_config_version=4,
            purpose="arbitrary-provider-call",
        )
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_name", sorted(WORKBENCH_LIMIT_MAXIMUMS))
async def test_app_context_rejects_limits_above_the_independent_control_contract(
    monkeypatch, limit_name
):
    payload = {
        "application_id": "customer-app-7",
        "app_profile_id": "17",
        "config_version": 1,
        "auth_epoch": 1,
        "vendor": "Tencent",
        "service_vendor": "ChinaTencentCloud",
        "app_id": "provider-app-7",
        "app_key": "app-key",
        "space_id": "space-7",
        "template_agent_id": "template-7",
        "secret_id": "secret-id",
        "secret_key": "secret-key",
        "capabilities": ["chat"],
        "limits": {
            "customer_concurrency": 10,
            "user_concurrency": 1,
            "max_runtime_seconds": 900,
            "max_reasoning_rounds": 20,
            "max_output_tokens": 8192,
            "web_search_per_turn": 0,
            "max_file_bytes": 0,
        },
    }
    payload["limits"][limit_name] = WORKBENCH_LIMIT_MAXIMUMS[limit_name] + 1
    monkeypatch.setattr(
        WorkbenchControlClient, "_request", AsyncMock(return_value=payload)
    )

    with pytest.raises(WorkbenchControlError, match="failed validation"):
        await WorkbenchControlClient.get_app_context(
            binding_id="binding-7",
            canonical_subject="napi:prod:customer:7:user:9",
            auth_epoch=1,
            requested_app_profile_id=17,
            requested_config_version=1,
            purpose="interactive",
        )


@pytest.mark.asyncio
async def test_identity_confirmation_and_resource_binding_use_control_v2(monkeypatch):
    request = AsyncMock(
        side_effect=[
            {
                "binding_id": "binding-1",
                "canonical_subject": "napi:prod:customer:7:user:9",
                "adp_account_id": "account-9",
                "adp_account_version": 1,
            },
            {
                "resource_binding_id": "wrb_123",
                "binding_id": "binding-1",
                "resource_type": "agent",
                "resource_id": "agent-9",
                "parent_resource_type": "account",
                "parent_resource_id": "account-9",
                "source_version": 1,
                "status": "active",
                "idempotent": False,
            },
        ]
    )
    monkeypatch.setattr(WorkbenchControlClient, "_request", request)

    await WorkbenchControlClient.confirm_identity(
        binding_id="binding-1",
        canonical_subject="napi:prod:customer:7:user:9",
        adp_account_id="account-9",
        adp_account_version=1,
    )
    result = await WorkbenchControlClient.bind_resource(
        binding_id="binding-1",
        canonical_subject="napi:prod:customer:7:user:9",
        customer_id=7,
        application_id="provider-app-7",
        app_profile_id=17,
        config_version=4,
        resource_type="agent",
        resource_id="agent-9",
        parent_resource_type="account",
        parent_resource_id="account-9",
        source_event_id="wre_stable",
        source_version=1,
    )

    assert result == {
        "resource_binding_id": "wrb_123",
        "binding_id": "binding-1",
        "resource_type": "agent",
        "resource_id": "agent-9",
        "parent_resource_type": "account",
        "parent_resource_id": "account-9",
        "source_version": 1,
        "status": "active",
        "idempotent": False,
    }
    assert request.await_args_list[0].args == (
        "POST",
        "/api/internal/workbench/identities/confirm",
    )
    assert request.await_args_list[1].args == (
        "POST",
        "/api/internal/workbench/resources/bind",
    )
    assert request.await_args_list[1].kwargs["payload"] == {
        "binding_id": "binding-1",
        "canonical_subject": "napi:prod:customer:7:user:9",
        "customer_id": 7,
        "application_id": "provider-app-7",
        "app_profile_id": 17,
        "config_version": 4,
        "resource_type": "agent",
        "resource_id": "agent-9",
        "parent_resource_type": "account",
        "parent_resource_id": "account-9",
        "source_event_id": "wre_stable",
        "source_version": 1,
    }


@pytest.mark.asyncio
async def test_resource_binding_response_rejects_content_locator_and_secret_fields(
    monkeypatch,
):
    response = {
        "resource_binding_id": "wrb_123",
        "binding_id": "binding-1",
        "resource_type": "agent",
        "resource_id": "agent-9",
        "source_version": 1,
        "status": "active",
        "idempotent": False,
    }
    request = AsyncMock()
    monkeypatch.setattr(WorkbenchControlClient, "_request", request)

    for forbidden_field in ("prompt", "provider_locator", "app_key"):
        request.return_value = {**response, forbidden_field: "must-not-cross-boundary"}
        with pytest.raises(WorkbenchControlError, match="unknown fields"):
            await WorkbenchControlClient.bind_resource(
                binding_id="binding-1",
                canonical_subject="napi:prod:customer:7:user:9",
                customer_id=7,
                application_id="provider-app-7",
                app_profile_id=17,
                config_version=4,
                resource_type="agent",
                resource_id="agent-9",
                parent_resource_type="account",
                parent_resource_id="account-9",
                source_event_id="wre_stable",
                source_version=1,
            )


def test_signed_headers_bind_method_path_body_timestamp_and_nonce(monkeypatch):
    secret = "s" * 48
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SERVICE_HMAC_SECRET", secret)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SERVICE_ID", "adp-test")
    body = b'{"ticket":"one-time"}'

    headers = WorkbenchControlClient.signed_headers(
        "POST",
        "/api/internal/workbench/tickets/consume",
        body,
        1786200000,
        "nonce-1",
    )

    canonical = "\n".join(
        (
            WORKBENCH_CONTRACT_VERSION,
            "POST",
            "/api/internal/workbench/tickets/consume",
            "1786200000",
            "nonce-1",
            hashlib.sha256(body).hexdigest(),
        )
    )
    expected = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    assert headers[WORKBENCH_CONTRACT_VERSION_HEADER] == WORKBENCH_CONTRACT_VERSION
    assert headers["X-Workbench-Service"] == "adp-test"
    assert headers["X-Workbench-Signature"] == expected


@pytest.mark.parametrize(
    "path",
    [
        "/api/internal/workbench/app-context?binding_id=7",
        "/api/internal/workbench/app-context#fragment",
        "/api/internal/workbench/../admin",
        "/api/internal/workbench//app-context",
        "/api/public/workbench/app-context",
    ],
)
def test_control_hmac_contract_forbids_query_fragment_and_ambiguous_paths(
    monkeypatch, path
):
    monkeypatch.setattr(
        tagentic_config, "WORKBENCH_SERVICE_HMAC_SECRET", "s" * 48
    )

    with pytest.raises(WorkbenchControlError, match="contract path"):
        WorkbenchControlClient.signed_headers(
            "POST", path, b"{}", 1786200000, "nonce-1"
        )


def test_signed_response_binds_status_path_body_timestamp_and_request_nonce(monkeypatch):
    secret = "s" * 48
    now = int(time.time())
    nonce = "request-nonce"
    path = "/api/internal/workbench/tickets/consume"
    body = b'{"success":true}'
    canonical = "\n".join(
        (
            WORKBENCH_CONTRACT_VERSION,
            "200",
            path,
            str(now),
            nonce,
            hashlib.sha256(body).hexdigest(),
        )
    )
    signature = hmac.new(
        secret.encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()
    headers = {
        WORKBENCH_CONTRACT_VERSION_HEADER: WORKBENCH_CONTRACT_VERSION,
        "X-Workbench-Response-Timestamp": str(now),
        "X-Workbench-Response-Nonce": nonce,
        "X-Workbench-Response-Signature": signature,
    }
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SERVICE_HMAC_SECRET", secret)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_ALLOWED_CLOCK_SKEW_SECONDS", 30)

    WorkbenchControlClient.verify_signed_response(200, path, body, nonce, headers)

    with pytest.raises(WorkbenchControlError, match="signature"):
        WorkbenchControlClient.verify_signed_response(
            200, path, body + b" ", nonce, headers
        )

    with pytest.raises(WorkbenchControlError, match="authentication"):
        WorkbenchControlClient.verify_signed_response(
            200, path, body, "different-nonce", headers
        )

    unsupported = dict(headers)
    unsupported[WORKBENCH_CONTRACT_VERSION_HEADER] = "2"
    with pytest.raises(WorkbenchControlError, match="contract version"):
        WorkbenchControlClient.verify_signed_response(
            200, path, body, nonce, unsupported
        )


def test_identity_context_requires_subject_to_match_customer_and_user():
    payload = {**_identity_payload(), "new_api_user_id": 8}

    with pytest.raises(WorkbenchControlError, match="canonical subject"):
        WorkbenchControlClient._identity_context(payload)


def test_identity_context_preserves_only_validated_fields():
    payload = {
        **_identity_payload(),
        "customer_id": "7",
        "new_api_user_id": "9",
        "auth_epoch": "3",
        "display_name": "User",
        "access_mode": "read_only",
        "app_key": "must-not-be-carried-in-identity-context",
    }

    context = WorkbenchControlClient._identity_context(payload)

    assert context.customer_id == 7
    assert context.new_api_user_id == 9
    assert context.auth_epoch == 3
    assert context.access_mode == "read_only"
    assert not hasattr(context, "app_key")


def test_identity_context_rejects_unknown_access_mode():
    payload = {**_identity_payload(), "access_mode": "administrator"}

    with pytest.raises(WorkbenchControlError, match="access mode"):
        WorkbenchControlClient._identity_context(payload)


def test_identity_context_normalizes_readonly_alias():
    payload = {**_identity_payload(), "access_mode": "READONLY"}

    assert WorkbenchControlClient._identity_context(payload).access_mode == "read_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["consume", "authorize"])
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.pop("allowed"), "incomplete"),
        (lambda value: value.update(allowed=False), "not allowed|failed validation"),
        (lambda value: value.pop("access_mode"), "incomplete"),
        (lambda value: value.pop("config_version"), "incomplete"),
        (lambda value: value.update(config_version=0), "failed validation"),
    ],
)
async def test_ticket_and_authorize_fail_closed_on_incomplete_access_contract(
    monkeypatch, operation, mutation, message
):
    payload = _identity_payload()
    mutation(payload)
    monkeypatch.setattr(
        WorkbenchControlClient, "_request", AsyncMock(return_value=payload)
    )

    with pytest.raises(WorkbenchControlError, match=message):
        if operation == "consume":
            await WorkbenchControlClient.consume_ticket("ticket", "binding")
        else:
            await WorkbenchControlClient.authorize(
                binding_id="binding-1",
                canonical_subject="napi:prod:customer:7:user:9",
                auth_epoch=1,
                method="GET",
                resource_path="/application/list",
            )


def test_control_url_requires_https_unless_explicitly_overridden(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_CONTROL_URL", "http://claw-control:8090")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_ALLOW_INSECURE_CONTROL_HTTP", False)

    with pytest.raises(WorkbenchControlError, match="must use HTTPS"):
        WorkbenchControlClient.validated_base_url()

    monkeypatch.setattr(tagentic_config, "WORKBENCH_ALLOW_INSECURE_CONTROL_HTTP", True)
    assert WorkbenchControlClient.validated_base_url() == "http://claw-control:8090"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@claw-control:8090",
        "https://claw-control:8090/base",
        "https://claw-control:8090?token=secret",
    ],
)
def test_control_url_rejects_credential_and_path_confusion(monkeypatch, url):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_CONTROL_URL", url)

    with pytest.raises(WorkbenchControlError, match="URL is invalid"):
        WorkbenchControlClient.validated_base_url()


class _FakeResponseContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, chunks, content_length=None):
        self.content_length = content_length
        self.content = _FakeResponseContent(chunks)


@pytest.mark.asyncio
async def test_control_response_body_is_bounded_for_streamed_and_declared_sizes(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_CONTROL_MAX_RESPONSE_BYTES", 8)

    with pytest.raises(WorkbenchControlError, match="too large"):
        await WorkbenchControlClient._read_response_body(
            _FakeResponse([b"ignored"], content_length=9)
        )

    with pytest.raises(WorkbenchControlError, match="too large"):
        await WorkbenchControlClient._read_response_body(
            _FakeResponse([b"1234", b"56789"])
        )

    assert await WorkbenchControlClient._read_response_body(
        _FakeResponse([b"1234", b"5678"])
    ) == b"12345678"


def test_workbench_auth_cookie_is_httponly_secure_and_path_scoped():
    response = json({"success": True})

    add_auth_token_cookie(
        response,
        config={"WORKBENCH_MODE": True},
        token="opaque-session",
        max_age=900,
        path="/workbench",
    )

    cookie_header = str(response.cookies.cookies[0])
    assert "Path=/workbench" in cookie_header
    # Lax is required so the Workbench session accompanies the top-level
    # cross-site OAuth callback; mutating APIs still require CSRF protection.
    assert "SameSite=Lax" in cookie_header
    assert "Secure" in cookie_header
    assert "HttpOnly" in cookie_header
