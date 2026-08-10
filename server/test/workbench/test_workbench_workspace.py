from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
import importlib
import sys

import pytest
from sanic.exceptions import SanicException

from config import tagentic_config
from core.conversation import CoreConversation
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_resource_reporter import WorkbenchResourceReporter
from core.workbench_secure_file import WorkbenchSecureFilePipeline
from core.workbench_workspace import CoreWorkbenchWorkspace, WorkbenchWorkspaceError
from model.chat import ChatConversation
from model.workbench import (
    WorkbenchConversationWorkspace,
    WorkbenchFileWorkspace,
    WorkbenchWorkspace,
)


ACCOUNT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CONVERSATION_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _identity(**overrides):
    values = {
        "binding_id": "binding-7",
        "canonical_subject": "napi:prod:customer:7:user:9",
        "customer_id": 7,
        "new_api_user_id": 9,
        "auth_epoch": 3,
        "display_name": "User",
        "application_id": "customer-app-7",
        "app_profile_id": "7",
        "access_mode": "active",
        "config_version": 4,
    }
    values.update(overrides)
    return WorkbenchIdentityContext(**values)


def _app_context(**overrides):
    values = {
        "application_id": "customer-app-7",
        "app_profile_id": "7",
        "config_version": 4,
        "auth_epoch": 3,
        "vendor": "Tencent",
        "service_vendor": "ChinaTencentCloud",
        "app_id": "provider-app-7",
        "app_key": "provider-secret",
        "space_id": "space-7",
        "template_agent_id": "template-7",
        "secret_id": "secret-id",
        "secret_key": "secret-key",
        "capabilities": ("chat", "files"),
        "limits": {},
    }
    values.update(overrides)
    return WorkbenchAppContext(**values)


class _Result:
    def __init__(self, value=None, row=None):
        self.value = value
        self.row = row

    def scalar(self):
        return self.value

    def first(self):
        return self.row


class _NestedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


@pytest.fixture(autouse=True)
def _workspace_key(monkeypatch):
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_SERVICE_HMAC_SECRET",
        "test-only-workbench-workspace-secret",
    )
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_WORKSPACE_LOCATOR_KEY",
        "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=",
    )
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_WORKSPACE_LOCATOR_KEY_ID",
        "locator-v1",
    )
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_WORKSPACE_LOCATOR_PREVIOUS_KEYS_JSON",
        "{}",
    )


@pytest.mark.asyncio
async def test_conversation_workspace_is_idempotent_for_the_exact_scope():
    added = []
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(None)]),
        add=added.append,
        flush=AsyncMock(),
        begin_nested=lambda: _NestedTransaction(),
    )

    workspace = await CoreWorkbenchWorkspace.ensure_for_conversation(
        db,
        conversation_id=CONVERSATION_ID,
        account_id=ACCOUNT_ID,
        identity=_identity(),
        app_context=_app_context(),
    )

    assert workspace.WorkspaceId.startswith("ww_")
    assert str(workspace.ConversationId) == CONVERSATION_ID
    assert workspace.BindingId == "binding-7"
    assert str(workspace.AccountId) == ACCOUNT_ID
    assert workspace.CustomerId == 7
    assert workspace.ApplicationId == "customer-app-7"
    assert workspace.ProviderAppId == "provider-app-7"
    assert workspace.AppProfileId == "7"
    assert workspace.ConfigVersion == 4
    assert workspace.Status == "pending"
    assert added == [workspace]

    db.execute = AsyncMock(return_value=_Result(workspace))
    same = await CoreWorkbenchWorkspace.ensure_for_conversation(
        db,
        conversation_id=CONVERSATION_ID,
        account_id=ACCOUNT_ID,
        identity=_identity(),
        app_context=_app_context(),
    )
    assert same is workspace
    assert len(added) == CoreWorkbenchWorkspace.MAX_WORKSPACES_PER_CONVERSATION


@pytest.mark.asyncio
async def test_scope_mismatch_fails_before_any_database_lookup():
    db = SimpleNamespace(execute=AsyncMock())

    with pytest.raises(WorkbenchWorkspaceError, match="incomplete"):
        await CoreWorkbenchWorkspace.ensure_for_conversation(
            db,
            conversation_id=CONVERSATION_ID,
            account_id=ACCOUNT_ID,
            identity=_identity(customer_id=8),
            app_context=_app_context(app_profile_id="8"),
        )

    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_conversations_get_distinct_workspaces():
    added = []
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(None), _Result(None)]),
        add=added.append,
        flush=AsyncMock(),
        begin_nested=lambda: _NestedTransaction(),
    )
    other_conversation_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"

    first = await CoreWorkbenchWorkspace.ensure_for_conversation(
        db,
        conversation_id=CONVERSATION_ID,
        account_id=ACCOUNT_ID,
        identity=_identity(),
        app_context=_app_context(),
    )
    second = await CoreWorkbenchWorkspace.ensure_for_conversation(
        db,
        conversation_id=other_conversation_id,
        account_id=ACCOUNT_ID,
        identity=_identity(),
        app_context=_app_context(),
    )

    assert first.WorkspaceId != second.WorkspaceId
    assert str(first.ConversationId) == CONVERSATION_ID
    assert str(second.ConversationId) == other_conversation_id


def test_provider_workspace_locator_is_encrypted_and_never_in_public_projection():
    provider_locator = "provider-workspace-secret-7"
    ciphertext = CoreWorkbenchWorkspace.encrypt_provider_locator(provider_locator)
    workspace = WorkbenchWorkspace(
        WorkspaceId="ww_owned",
        ConversationId=CONVERSATION_ID,
        Kind="conversation",
        Status="active",
        ProviderLocatorCiphertext=ciphertext,
    )

    assert provider_locator not in ciphertext
    assert CoreWorkbenchWorkspace._decrypt_provider_locator(ciphertext) == provider_locator
    assert CoreWorkbenchWorkspace.public_descriptor(workspace) == {
        "WorkspaceId": "ww_owned",
        "ConversationId": CONVERSATION_ID,
        "Kind": "conversation",
        "Status": "active",
    }
    assert "ProviderLocatorCiphertext" not in CoreWorkbenchWorkspace.public_descriptor(workspace)
    assert "ProviderLocatorKeyId" not in CoreWorkbenchWorkspace.public_descriptor(workspace)


def test_provider_workspace_locator_key_rotation_uses_explicit_previous_key(monkeypatch):
    ciphertext = CoreWorkbenchWorkspace.encrypt_provider_locator("provider-workspace-v1")
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_WORKSPACE_LOCATOR_KEY",
        "cXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXE=",
    )
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_WORKSPACE_LOCATOR_KEY_ID",
        "locator-v2",
    )
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_WORKSPACE_LOCATOR_PREVIOUS_KEYS_JSON",
        '{"locator-v1":"a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="}',
    )

    assert CoreWorkbenchWorkspace._decrypt_provider_locator(ciphertext) == "provider-workspace-v1"

    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_WORKSPACE_LOCATOR_PREVIOUS_KEYS_JSON",
        "{}",
    )
    with pytest.raises(WorkbenchWorkspaceError, match="unavailable"):
        CoreWorkbenchWorkspace._decrypt_provider_locator(ciphertext)


@pytest.mark.asyncio
async def test_provider_workspace_locator_binds_once_and_is_idempotent():
    workspace = SimpleNamespace(
        WorkspaceId="ww_owned",
        ProviderLocatorCiphertext=None,
        ProviderLocatorKeyId=None,
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_Result(workspace)),
        commit=AsyncMock(),
    )

    bound = await CoreWorkbenchWorkspace.bind_provider_locator_for_conversation(
        db,
        conversation_id=CONVERSATION_ID,
        provider_locator="6576c839-839c-4a54-b820-408aec31dbe0",
        account_id=ACCOUNT_ID,
        identity=_identity(),
        app_context=_app_context(),
    )

    assert bound is workspace
    assert "6576c839-839c-4a54-b820-408aec31dbe0" not in (
        workspace.ProviderLocatorCiphertext
    )
    assert CoreWorkbenchWorkspace._decrypt_provider_locator(
        workspace.ProviderLocatorCiphertext
    ) == "6576c839-839c-4a54-b820-408aec31dbe0"
    db.commit.assert_awaited_once()

    db.commit.reset_mock()
    await CoreWorkbenchWorkspace.bind_provider_locator_for_conversation(
        db,
        conversation_id=CONVERSATION_ID,
        provider_locator="6576c839-839c-4a54-b820-408aec31dbe0",
        account_id=ACCOUNT_ID,
        identity=_identity(),
        app_context=_app_context(),
    )
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_workspace_locator_cannot_be_rebound():
    workspace = SimpleNamespace(
        WorkspaceId="ww_owned",
        ProviderLocatorCiphertext=CoreWorkbenchWorkspace.encrypt_provider_locator(
            "6576c839-839c-4a54-b820-408aec31dbe0"
        ),
        ProviderLocatorKeyId="locator-v1",
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_Result(workspace)),
        commit=AsyncMock(),
    )

    with pytest.raises(WorkbenchWorkspaceError, match="different Workspace") as raised:
        await CoreWorkbenchWorkspace.bind_provider_locator_for_conversation(
            db,
            conversation_id=CONVERSATION_ID,
            provider_locator="7587d940-90ad-40b1-8fb5-9b255fe80550",
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
        )

    assert raised.value.status_code == 502
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversation_creation_reports_and_activates_workspace_ownership(monkeypatch):
    added = []
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(None), _Result(None)]),
        add=added.append,
        flush=AsyncMock(),
        commit=AsyncMock(),
        begin_nested=lambda: _NestedTransaction(),
    )
    reports = [
        SimpleNamespace(EventId="wre_conversation"),
        SimpleNamespace(EventId="wre_workspace"),
    ]
    enqueue = AsyncMock(side_effect=reports)
    deliver = AsyncMock(return_value="delivered")
    monkeypatch.setattr(WorkbenchResourceReporter, "enqueue", enqueue)
    monkeypatch.setattr(WorkbenchResourceReporter, "deliver_event", deliver)

    conversation = await CoreConversation.create(
        db,
        ACCOUNT_ID,
        "customer-app-7",
        conversation_id=CONVERSATION_ID,
        workbench_identity=_identity(),
        workbench_app_context=_app_context(),
        workbench_agent_id="agent-7",
    )

    workspace = next(item for item in added if isinstance(item, WorkbenchWorkspace))
    mapping = next(
        item for item in added if isinstance(item, WorkbenchConversationWorkspace)
    )
    assert isinstance(conversation, ChatConversation)
    assert mapping.WorkspaceId == workspace.WorkspaceId
    assert mapping.AgentId == "agent-7"
    assert mapping.Status == "active"
    assert workspace.Status == "active"
    assert enqueue.await_args_list[0].kwargs == {
        "identity": _identity(),
        "resource_type": "conversation",
        "resource_id": CONVERSATION_ID,
        "parent_resource_type": "agent",
        "parent_resource_id": "agent-7",
    }
    assert enqueue.await_args_list[1].kwargs == {
        "identity": _identity(),
        "resource_type": "workspace",
        "resource_id": workspace.WorkspaceId,
        "parent_resource_type": "conversation",
        "parent_resource_id": CONVERSATION_ID,
    }
    assert deliver.await_count == 2


@pytest.mark.asyncio
async def test_each_conversation_reports_its_own_distinct_workspace_parent(monkeypatch):
    added = []
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[_Result(None), _Result(None), _Result(None), _Result(None)]
        ),
        add=added.append,
        flush=AsyncMock(),
        commit=AsyncMock(),
        begin_nested=lambda: _NestedTransaction(),
    )
    other_conversation_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    enqueue = AsyncMock(
        side_effect=[
            SimpleNamespace(EventId="wre_conversation_1"),
            SimpleNamespace(EventId="wre_workspace_1"),
            SimpleNamespace(EventId="wre_conversation_2"),
            SimpleNamespace(EventId="wre_workspace_2"),
        ]
    )
    monkeypatch.setattr(WorkbenchResourceReporter, "enqueue", enqueue)
    monkeypatch.setattr(
        WorkbenchResourceReporter,
        "deliver_event",
        AsyncMock(return_value="delivered"),
    )

    for conversation_id in (CONVERSATION_ID, other_conversation_id):
        await CoreConversation.create(
            db,
            ACCOUNT_ID,
            "customer-app-7",
            conversation_id=conversation_id,
            workbench_identity=_identity(),
            workbench_app_context=_app_context(),
            workbench_agent_id="agent-7",
        )

    workspaces = [item for item in added if isinstance(item, WorkbenchWorkspace)]
    assert len(workspaces) == 2
    assert workspaces[0].WorkspaceId != workspaces[1].WorkspaceId
    workspace_reports = [
        call.kwargs
        for call in enqueue.await_args_list
        if call.kwargs["resource_type"] == "workspace"
    ]
    assert [report["resource_id"] for report in workspace_reports] == [
        workspaces[0].WorkspaceId,
        workspaces[1].WorkspaceId,
    ]
    assert [report["parent_resource_id"] for report in workspace_reports] == [
        CONVERSATION_ID,
        other_conversation_id,
    ]


@pytest.mark.asyncio
async def test_conversation_lookup_contains_every_ownership_dimension():
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(None)))

    result = await CoreConversation.get_owned(
        db,
        ACCOUNT_ID,
        "customer-app-7",
        CONVERSATION_ID,
        workbench_identity=_identity(),
        workbench_app_context=_app_context(),
    )

    assert result is None
    sql = str(db.execute.await_args.args[0])
    for column in (
        "workbench_conversation_workspace.\"BindingId\"",
        "workbench_conversation_workspace.\"AccountId\"",
        "workbench_conversation_workspace.\"CustomerId\"",
        "workbench_conversation_workspace.\"ApplicationId\"",
        "workbench_conversation_workspace.\"ProviderAppId\"",
        "workbench_conversation_workspace.\"AppProfileId\"",
        "workbench_conversation_workspace.\"ConfigVersion\"",
        "workbench_workspace.\"WorkspaceId\"",
    ):
        assert column in sql


@pytest.mark.asyncio
async def test_file_workspace_lookup_is_not_authorized_by_file_id_alone():
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(row=None)))

    result = await CoreWorkbenchWorkspace.get_owned_file_workspace(
        db,
        file_id="wf_owned",
        account_id=ACCOUNT_ID,
        identity=_identity(),
        app_context=_app_context(),
        conversation_id=None,
    )

    assert result is None
    sql = str(db.execute.await_args.args[0])
    for column in (
        "workbench_file_workspace.\"FileId\"",
        "workbench_file_workspace.\"BindingId\"",
        "workbench_file_workspace.\"AccountId\"",
        "workbench_file_workspace.\"CustomerId\"",
        "workbench_file_workspace.\"ApplicationId\"",
        "workbench_file_workspace.\"ProviderAppId\"",
        "workbench_file_workspace.\"AppProfileId\"",
        "workbench_file_workspace.\"ConfigVersion\"",
    ):
        assert column in sql


@pytest.mark.asyncio
async def test_account_owned_file_binds_once_to_an_exact_owned_conversation_workspace():
    file_mapping = SimpleNamespace(WorkspaceId=None)
    conversation_mapping = SimpleNamespace(WorkspaceId="ww_owned")
    workspace = SimpleNamespace(WorkspaceId="ww_owned")
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(file_mapping),
                _Result(row=(conversation_mapping, workspace)),
            ]
        ),
        flush=AsyncMock(),
    )

    result = await CoreWorkbenchWorkspace.get_owned_file_workspace(
        db,
        file_id="wf_owned",
        account_id=ACCOUNT_ID,
        identity=_identity(),
        app_context=_app_context(),
        conversation_id=CONVERSATION_ID,
    )

    assert result == (workspace, file_mapping)
    assert file_mapping.WorkspaceId == "ww_owned"
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_file_bound_to_another_conversation_workspace_fails_closed():
    file_mapping = SimpleNamespace(WorkspaceId="ww_first")
    conversation_mapping = SimpleNamespace(WorkspaceId="ww_second")
    workspace = SimpleNamespace(WorkspaceId="ww_second")
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(file_mapping),
                _Result(row=(conversation_mapping, workspace)),
            ]
        ),
        flush=AsyncMock(),
    )

    result = await CoreWorkbenchWorkspace.get_owned_file_workspace(
        db,
        file_id="wf_owned",
        account_id=ACCOUNT_ID,
        identity=_identity(),
        app_context=_app_context(),
        conversation_id=CONVERSATION_ID,
    )

    assert result is None
    assert file_mapping.WorkspaceId == "ww_first"
    db.flush.assert_not_awaited()


def test_provider_workspace_fields_are_removed_from_provider_projection():
    projected = WorkbenchSecureFilePipeline.redact_private_urls(
        {
            "WorkspaceId": "provider-workspace-secret",
            "Nested": {"ProviderWorkspaceLocator": "provider-locator-secret"},
        },
        (),
    )

    assert projected == {
        "WorkspaceId": "[workspace-redacted]",
        "Nested": {"ProviderWorkspaceLocator": "[workspace-redacted]"},
    }


def test_workspace_mapping_tables_are_part_of_the_shared_metadata():
    table_names = set(WorkbenchWorkspace.metadata.tables)
    assert {
        "workbench_workspace",
        "workbench_conversation_workspace",
        "workbench_file_workspace",
    }.issubset(table_names)
    assert WorkbenchFileWorkspace.__tablename__ in table_names
    assert WorkbenchFileWorkspace.__table__.c.WorkspaceId.nullable is True


@pytest.mark.asyncio
@pytest.mark.parametrize("close_error", [None, RuntimeError("close failed")])
async def test_owned_workspace_download_uses_encrypted_provider_locator(
    monkeypatch,
    close_error,
):
    provider_stream = SimpleNamespace(
        content_type="text/plain",
        file_name="report.txt",
        close=AsyncMock(side_effect=close_error),
    )

    async def chunks():
        yield b"safe "
        yield b"report"

    provider_stream.iter_chunks = chunks
    open_stream = AsyncMock(return_value=provider_stream)

    class _FakeApp:
        def add_route(self, *_args, **_kwargs):
            return None

        def get_vendor_app(self, application_id):
            assert application_id == "customer-app-7"
            return SimpleNamespace(open_file_stream=open_stream)

    class _Parser:
        def add_argument(self, *_args, **_kwargs):
            return None

        def parse_args(self, _request):
                return {
                    "ApplicationId": "customer-app-7",
                    "AppId": "customer-app-7",
                "WorkspaceId": "ww_owned",
                "Path": "/workdir/report.txt",
            }

    sys.modules.pop("router.file_download", None)
    with patch("app_factory.TAgenticApp.get_app", return_value=_FakeApp()):
        file_download = importlib.import_module("router.file_download")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(file_download.reqparse, "RequestParser", _Parser)
    monkeypatch.setattr(
        file_download.WorkbenchPolicy,
        "validate_file_read",
        lambda _context: {"max_file_bytes": 1024},
    )
    resolver = AsyncMock(
        return_value=(
            SimpleNamespace(WorkspaceId="ww_owned"),
            "6576c839-839c-4a54-b820-408aec31dbe0",
        )
    )
    monkeypatch.setattr(
        file_download.CoreWorkbenchWorkspace,
        "resolve_provider_locator",
        resolver,
    )
    monkeypatch.setattr(
        file_download.tagentic_config,
        "WORKBENCH_FILE_ABSOLUTE_MAX_BYTES",
        2048,
    )
    lease = SimpleNamespace(lease_id="lease-file", max_runtime_seconds=60)
    acquire = AsyncMock(return_value=lease)
    release = AsyncMock()
    monkeypatch.setattr(file_download.WorkbenchRuntimeGuard, "acquire", acquire)
    monkeypatch.setattr(file_download.WorkbenchRuntimeGuard, "release", release)
    rendered = {}
    monkeypatch.setattr(
        file_download,
        "ResponseStream",
        lambda streaming_fn, **kwargs: rendered.update(
            {"streaming_fn": streaming_fn, **kwargs}
        ) or rendered,
    )
    request = SimpleNamespace(
        args={
            "ApplicationId": "customer-app-7",
            "AppId": "customer-app-7",
            "WorkspaceId": "ww_owned",
            "Path": "/workdir/report.txt",
        },
        ctx=SimpleNamespace(
            db=object(),
            account_id=ACCOUNT_ID,
            session_claims={"exp": 4102444800},
            workbench_context=_identity(),
            workbench_app_context=_app_context(),
        ),
    )

    response = await file_download.FileDownloadApi.get.__wrapped__(
        file_download.FileDownloadApi(),
        request,
    )

    assert response["headers"]["Cache-Control"] == "private, no-store"
    assert response["headers"]["X-Content-Type-Options"] == "nosniff"
    written = []
    streaming = response["streaming_fn"](
        SimpleNamespace(write=AsyncMock(side_effect=lambda chunk: written.append(chunk)))
    )
    if close_error is None:
        await streaming
    else:
        with pytest.raises(RuntimeError, match="close failed"):
            await streaming
    assert b"".join(written) == b"safe report"
    resolver.assert_awaited_once_with(
        request.ctx.db,
        workspace_id="ww_owned",
        account_id=ACCOUNT_ID,
        identity=request.ctx.workbench_context,
        app_context=request.ctx.workbench_app_context,
    )
    acquire.assert_awaited_once_with(
        account_id=ACCOUNT_ID,
        identity=request.ctx.workbench_context,
        app_context=request.ctx.workbench_app_context,
        operation="file_download",
    )
    open_stream.assert_awaited_once_with(
        app_id="provider-app-7",
        workspace_id="6576c839-839c-4a54-b820-408aec31dbe0",
        path="/workdir/report.txt",
        max_bytes=1024,
        user_id=request.ctx.workbench_context.canonical_subject,
        timeout_seconds=max(1, lease.max_runtime_seconds - 1),
    )
    provider_stream.close.assert_awaited_once_with()
    release.assert_awaited_once_with(lease)


@pytest.mark.asyncio
async def test_cross_scope_workspace_download_is_hidden_as_not_found(monkeypatch):
    class _FakeApp:
        def add_route(self, *_args, **_kwargs):
            return None

    class _Parser:
        def add_argument(self, *_args, **_kwargs):
            return None

        def parse_args(self, _request):
                return {
                    "ApplicationId": "customer-app-7",
                    "AppId": "customer-app-7",
                "WorkspaceId": "ww_foreign",
                "Path": "/workdir/report.txt",
            }

    sys.modules.pop("router.file_download", None)
    with patch("app_factory.TAgenticApp.get_app", return_value=_FakeApp()):
        file_download = importlib.import_module("router.file_download")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(file_download.reqparse, "RequestParser", _Parser)
    monkeypatch.setattr(
        file_download.WorkbenchPolicy,
        "validate_file_read",
        lambda _context: {"max_file_bytes": 1024},
    )
    monkeypatch.setattr(
        file_download.CoreWorkbenchWorkspace,
        "resolve_provider_locator",
        AsyncMock(side_effect=WorkbenchWorkspaceError("workspace not found", 404)),
    )
    request = SimpleNamespace(
        args={
            "ApplicationId": "customer-app-7",
            "AppId": "customer-app-7",
            "WorkspaceId": "ww_foreign",
            "Path": "/workdir/report.txt",
        },
        ctx=SimpleNamespace(
            db=object(),
            account_id=ACCOUNT_ID,
            workbench_context=_identity(),
            workbench_app_context=_app_context(),
        ),
    )

    with pytest.raises(SanicException, match="workspace not found") as raised:
        await file_download.FileDownloadApi.get.__wrapped__(
            file_download.FileDownloadApi(),
            request,
        )

    assert raised.value.status_code == 404
