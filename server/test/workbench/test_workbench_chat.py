from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.chat as chat_module
from config import tagentic_config
from core.chat import CoreChat
from core.conversation import CoreConversation
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from vendor.tcadp import tcadp as tcadp_module
from vendor.tcadp.tcadp import TCADP


class _Conversation:
    def __init__(self, conversation_id):
        self.Id = conversation_id

    def to_dict(self):
        return {"Id": self.Id}


class _ConversationCallback:
    def __init__(self, events):
        self.events = events
        self.created_ids = []

    async def create(self, title=None, vendor_conversation_id=None):
        self.events.append("local")
        self.created_ids.append(vendor_conversation_id)
        return _Conversation(vendor_conversation_id)

    async def update(self, conversation_id=None, title=None):
        return _Conversation(conversation_id)


def _vendor():
    return TCADP(
        {
            "Vendor": "Tencent",
            "ServiceVendor": "ChinaTencentCloud",
            "AppId": "provider-app-7",
            "AppKey": "provider-app-key-secret",
        },
        "customer-app-7",
    )


@pytest.mark.asyncio
async def test_tcadp_creates_provider_conversation_before_local_ownership(monkeypatch):
    provider_conversation_id = "dd6a4e74-b2d4-463f-ba01-429304a4a142"
    vendor = _vendor()
    events = []
    callback = _ConversationCallback(events)

    async def create_provider(action, payload, **_kwargs):
        events.append("provider")
        assert action == "CreateConversation"
        assert payload == {
            "Type": 5,
            "AppId": "provider-app-7",
            "AppKey": "provider-app-key-secret",
            "UserId": "napi:prod:customer:7:user:9",
            "AgentId": "agent-9",
        }
        return {"ConversationId": provider_conversation_id}

    monkeypatch.setattr(vendor, "forward_request", create_provider)
    stream = vendor.chat(
        "napi:prod:customer:7:user:9",
        [{"Type": "text", "Text": "hello"}],
        None,
        True,
        callback,
        agent_id="agent-9",
    )

    event = await anext(stream)
    await stream.aclose()

    assert events == ["provider", "local"]
    assert callback.created_ids == [provider_conversation_id]
    assert provider_conversation_id.encode() in event


@pytest.mark.asyncio
async def test_tcadp_static_runtime_omits_agent_id_from_provider_creation(monkeypatch):
    provider_conversation_id = "dd6a4e74-b2d4-463f-ba01-429304a4a142"
    vendor = _vendor()
    callback = _ConversationCallback([])
    create_provider = AsyncMock(return_value={"ConversationId": provider_conversation_id})
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(vendor, "forward_request", create_provider)

    stream = vendor.chat(
        "napi:prod:customer:7:user:9",
        [{"Type": "text", "Text": "hello"}],
        None,
        True,
        callback,
        agent_id=None,
    )
    await anext(stream)
    await stream.aclose()

    create_provider.assert_awaited_once_with(
        "CreateConversation",
        {
            "Type": 5,
            "AppId": "provider-app-7",
            "AppKey": "provider-app-key-secret",
            "UserId": "napi:prod:customer:7:user:9",
        },
    )


@pytest.mark.asyncio
async def test_tcadp_does_not_persist_or_chat_when_provider_creation_fails(monkeypatch):
    vendor = _vendor()
    callback = _ConversationCallback([])
    monkeypatch.setattr(
        vendor,
        "forward_request",
        AsyncMock(side_effect=RuntimeError("provider unavailable")),
    )

    stream = vendor.chat(
        "napi:prod:customer:7:user:9",
        [{"Type": "text", "Text": "hello"}],
        None,
        True,
        callback,
        agent_id="agent-9",
    )
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await anext(stream)

    assert callback.created_ids == []


@pytest.mark.asyncio
async def test_tcadp_rejects_invalid_provider_conversation_id_before_local_write(monkeypatch):
    vendor = _vendor()
    callback = _ConversationCallback([])
    monkeypatch.setattr(
        vendor,
        "forward_request",
        AsyncMock(return_value={"ConversationId": "not-a-uuid"}),
    )

    stream = vendor.chat(
        "napi:prod:customer:7:user:9",
        [{"Type": "text", "Text": "hello"}],
        None,
        True,
        callback,
        agent_id="agent-9",
    )
    with pytest.raises(ValueError, match="valid ConversationId"):
        await anext(stream)

    assert callback.created_ids == []


@pytest.mark.asyncio
async def test_tcadp_workbench_stream_redacts_locator_split_across_text_deltas(monkeypatch):
    private_url = (
        "https://private-workbench-1250000000.cos.ap-guangzhou.myqcloud.com/"
        "workbench/customer-7/binding-9/private.pdf"
        "?q-sign-algorithm=sha1&q-signature=sensitive-signature"
    )
    text = f"before:{private_url}:after"
    boundaries = (0, 13, 47, 93, len(text))
    lines = [
        (
            "data: "
            + json.dumps(
                {"Type": "text.delta", "Text": text[start:end]},
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        for start, end in zip(boundaries, boundaries[1:])
    ]

    class _Content:
        async def readline(self):
            return lines.pop(0) if lines else b""

    class _Response:
        status = 200
        content = _Content()
        connection = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def close(self):
            return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            return _Response()

        async def close(self):
            return None

    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_FILE_COS_BUCKET",
        "private-workbench-1250000000",
    )
    monkeypatch.setattr(tcadp_module.aiohttp, "ClientSession", lambda **_kwargs: _Session())
    vendor = _vendor()
    callback = _ConversationCallback([])

    output = [
        event
        async for event in vendor.chat(
            "napi:prod:customer:7:user:9",
            [{"Type": "file", "File": {"FileUrl": private_url, "Url": private_url}}],
            "dd6a4e74-b2d4-463f-ba01-429304a4a142",
            False,
            callback,
            agent_id="agent-9",
        )
    ]
    deltas = []
    for event in output:
        rendered = event.decode("utf-8") if isinstance(event, bytes) else event
        if not rendered.startswith("data: "):
            continue
        payload = json.loads(rendered.removeprefix("data: ").strip())
        if payload.get("Type") == "text.delta":
            deltas.append(payload["Text"])

    reconstructed = "".join(deltas)
    assert reconstructed.startswith("before:")
    assert reconstructed.endswith(":after")
    assert "[workbench-private-file]" in reconstructed
    assert "private-workbench-1250000000" not in reconstructed
    assert "workbench/customer-7" not in reconstructed
    assert "q-sign" not in reconstructed
    assert "sensitive-signature" not in reconstructed


@pytest.mark.asyncio
async def test_tcadp_captures_completion_evidence_before_browser_projection(monkeypatch):
    private_url = (
        "https://private-workbench-1250000000.cos.ap-guangzhou.myqcloud.com/"
        "workbench/customer-7/binding-9/private.pdf?q-signature=sensitive"
    )
    provider_payload = {
        "Type": "response.completed",
        "Response": {
            "RecordId": "record-9",
            "ConversationId": "dd6a4e74-b2d4-463f-ba01-429304a4a142",
            "AppKey": "provider-app-key-secret",
            "PrivateUrl": private_url,
        },
    }
    lines = [("data: " + json.dumps(provider_payload) + "\n").encode()]

    class _Content:
        async def readline(self):
            return lines.pop(0) if lines else b""

    class _Response:
        status = 200
        content = _Content()
        connection = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def close(self):
            return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            return _Response()

        async def close(self):
            return None

    captured = []

    async def capture(payload):
        captured.append(payload)

    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_FILE_COS_BUCKET",
        "private-workbench-1250000000",
    )
    monkeypatch.setattr(tcadp_module.aiohttp, "ClientSession", lambda **_kwargs: _Session())

    output = [
        event
        async for event in _vendor().chat(
            "napi:prod:customer:7:user:9",
            [{"Type": "text", "Text": "hello"}],
            "dd6a4e74-b2d4-463f-ba01-429304a4a142",
            False,
            _ConversationCallback([]),
            agent_id="agent-9",
            workbench_evidence_callback=capture,
        )
    ]

    assert captured == [provider_payload]
    rendered = b"".join(item if isinstance(item, bytes) else item.encode() for item in output)
    assert b"provider-app-key-secret" not in rendered
    assert b"private-workbench-1250000000" not in rendered
    assert b"[secret-redacted]" in rendered
    assert b"[workbench-private-file]" in rendered


@pytest.mark.asyncio
async def test_core_chat_persists_returned_provider_id_for_trusted_owner(monkeypatch):
    provider_conversation_id = "dd6a4e74-b2d4-463f-ba01-429304a4a142"
    db = object()

    @asynccontextmanager
    async def fake_db_connection():
        yield db

    class _Vendor:
        application_id = "customer-app-7"

        async def chat(self, _account_id, _contents, _conversation_id, is_new, callback, **kwargs):
            assert is_new is True
            assert kwargs["agent_id"] == "agent-9"
            conversation = await callback.create(
                vendor_conversation_id=provider_conversation_id
            )
            yield str(conversation.Id).encode()

    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(chat_module, "db_connection", fake_db_connection)
    monkeypatch.setattr(
        CoreChat,
        "resolve_vendor_account_id",
        AsyncMock(return_value="napi:prod:customer:7:user:9"),
    )
    create = AsyncMock(return_value=_Conversation(provider_conversation_id))
    monkeypatch.setattr(CoreConversation, "create", create)
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
    app_context = WorkbenchAppContext(
        application_id="customer-app-7",
        app_profile_id="7",
        config_version=4,
        auth_epoch=3,
        vendor="Tencent",
        service_vendor="ChinaTencentCloud",
        app_id="provider-app-7",
        app_key="provider-secret",
        space_id="space-7",
        template_agent_id="template-7",
        secret_id="secret-id",
        secret_key="secret-key",
        capabilities=("chat",),
        limits={},
    )

    messages = [
        item
        async for item in CoreChat.message(
            _Vendor(),
            "account-9",
            [{"Type": "text", "Text": "hello"}],
            None,
            True,
            {},
            workbench_limits={
                "max_output_tokens": 8192,
                "max_reasoning_rounds": 20,
            },
            workbench_turn_serialized=True,
            workbench_agent_id="agent-9",
            workbench_identity=identity,
            workbench_app_context=app_context,
        )
    ]

    create.assert_awaited_once_with(
        db,
        "account-9",
        "customer-app-7",
        title="hello",
        conversation_id=provider_conversation_id,
        workbench_identity=identity,
        workbench_app_context=app_context,
        workbench_agent_id="agent-9",
    )
    assert messages == [provider_conversation_id.encode()]


@pytest.mark.asyncio
async def test_core_chat_keeps_local_ownership_without_sending_static_principal(
    monkeypatch,
):
    provider_conversation_id = "dd6a4e74-b2d4-463f-ba01-429304a4a142"
    db = object()

    @asynccontextmanager
    async def fake_db_connection():
        yield db

    class _Vendor:
        application_id = "customer-app-7"

        async def chat(
            self,
            _account_id,
            _contents,
            _conversation_id,
            is_new,
            callback,
            **kwargs,
        ):
            assert is_new is True
            assert kwargs["agent_id"] is None
            conversation = await callback.create(
                vendor_conversation_id=provider_conversation_id
            )
            yield str(conversation.Id).encode()

    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(chat_module, "db_connection", fake_db_connection)
    monkeypatch.setattr(
        CoreChat,
        "resolve_vendor_account_id",
        AsyncMock(return_value="napi:prod:customer:7:user:9"),
    )
    create = AsyncMock(return_value=_Conversation(provider_conversation_id))
    monkeypatch.setattr(CoreConversation, "create", create)
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
    app_context = WorkbenchAppContext(
        application_id="customer-app-7",
        app_profile_id="7",
        config_version=4,
        auth_epoch=3,
        vendor="Tencent",
        service_vendor="ChinaTencentCloud",
        app_id="provider-app-7",
        app_key="provider-secret",
        space_id="space-7",
        template_agent_id="",
        secret_id="secret-id",
        secret_key="secret-key",
        capabilities=("chat",),
        limits={},
        provider_app_mode=1,
        runtime_profile="standard_v2",
        execution_enabled=False,
    )

    messages = [
        item
        async for item in CoreChat.message(
            _Vendor(),
            "account-9",
            [{"Type": "text", "Text": "hello"}],
            None,
            True,
            {},
            workbench_limits={
                "max_output_tokens": 8192,
                "max_reasoning_rounds": 20,
            },
            workbench_turn_serialized=True,
            workbench_agent_id="wrp_local-ownership",
            workbench_send_agent_id=False,
            workbench_identity=identity,
            workbench_app_context=app_context,
        )
    ]

    create.assert_awaited_once_with(
        db,
        "account-9",
        "customer-app-7",
        title="hello",
        conversation_id=provider_conversation_id,
        workbench_identity=identity,
        workbench_app_context=app_context,
        workbench_agent_id="wrp_local-ownership",
    )
    assert messages == [provider_conversation_id.encode()]


@pytest.mark.asyncio
async def test_core_chat_provider_update_is_scoped_to_owner_and_application(monkeypatch):
    conversation_id = "dd6a4e74-b2d4-463f-ba01-429304a4a142"
    db = object()

    @asynccontextmanager
    async def fake_db_connection():
        yield db

    class _Vendor:
        application_id = "customer-app-7"

        async def chat(self, _account_id, _contents, _conversation_id, is_new, callback, **_kwargs):
            assert is_new is False
            conversation = await callback.update(
                conversation_id=conversation_id,
                title="provider title",
            )
            yield str(conversation.Id).encode()

    owned = _Conversation(conversation_id)
    get_owned = AsyncMock(return_value=owned)
    get_unscoped = AsyncMock(side_effect=AssertionError("unscoped lookup must not be used"))
    update = AsyncMock()

    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", False)
    monkeypatch.setattr(chat_module, "db_connection", fake_db_connection)
    monkeypatch.setattr(CoreConversation, "exists", AsyncMock(return_value=True))
    monkeypatch.setattr(CoreConversation, "get_owned", get_owned)
    monkeypatch.setattr(CoreConversation, "get", get_unscoped)
    monkeypatch.setattr(CoreConversation, "update", update)
    monkeypatch.setattr(
        CoreChat,
        "resolve_vendor_account_id",
        AsyncMock(return_value="provider-user-9"),
    )

    messages = [
        item
        async for item in CoreChat.message(
            _Vendor(),
            "account-9",
            [{"Type": "text", "Text": "hello"}],
            conversation_id,
            False,
            {},
        )
    ]

    get_owned.assert_awaited_once_with(
        db,
        "account-9",
        "customer-app-7",
        conversation_id,
        workbench_identity=None,
        workbench_app_context=None,
    )
    get_unscoped.assert_not_awaited()
    update.assert_awaited_once_with(db, owned, title="provider title")
    assert messages == [conversation_id.encode()]


@pytest.mark.asyncio
async def test_trusted_app_info_uses_only_configured_app_id(monkeypatch):
    vendor = _vendor()
    request = AsyncMock(
        return_value={
            "Response": {
                "App": {
                    "Metadata": {
                        "Name": "Trusted claw",
                        "AppMode": 4,
                        "SpaceId": "space-secret",
                    },
                    "Config": {},
                    "Status": {"Status": 2},
                }
            }
        }
    )
    monkeypatch.setattr(tcadp_module, "tc_request", request)

    info = await vendor.get_info(use_trusted_app_id=True)

    assert info.Name == "Trusted claw"
    assert info.Pattern == "ClawAgent"
    assert info.SpaceId is None
    assert vendor.config["AppId"] == "provider-app-7"
    assert request.await_count == 1
    assert request.await_args.args[1:3] == (
        "DescribeApp",
        {"AppId": "provider-app-7", "FieldMask": {"Paths": ["AppConfig"]}},
    )


@pytest.mark.asyncio
async def test_forward_request_logs_payload_keys_without_secret_values(monkeypatch, caplog):
    vendor = _vendor()
    monkeypatch.setattr(
        tcadp_module,
        "tc_request",
        AsyncMock(return_value={"Response": {"ConversationId": "conversation-1"}}),
    )

    with caplog.at_level("INFO"):
        await vendor.forward_request(
            "CreateConversation",
            {"AppKey": "provider-app-key-secret", "UserId": "canonical-user"},
        )

    assert "payload_keys=['AppKey', 'UserId']" in caplog.text
    assert "provider-app-key-secret" not in caplog.text
    assert "canonical-user" not in caplog.text


@pytest.mark.asyncio
async def test_forward_request_does_not_log_provider_error_message(monkeypatch, caplog):
    vendor = _vendor()
    monkeypatch.setattr(
        tcadp_module,
        "tc_request",
        AsyncMock(
            return_value={
                "Response": {
                    "Error": {
                        "Code": "InvalidParameter",
                        "Message": "invalid provider-app-key-secret",
                    },
                    "RequestId": "request-error-1",
                }
            }
        ),
    )

    with caplog.at_level("INFO"):
        with pytest.raises(Exception, match="CreateConversation failed"):
            await vendor.forward_request(
                "CreateConversation",
                {"AppKey": "provider-app-key-secret"},
            )

    assert "InvalidParameter" in caplog.text
    assert "provider-app-key-secret" not in caplog.text
