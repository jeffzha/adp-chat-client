import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config import tagentic_config
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_file_ownership import (
    WorkbenchFileOwnership,
    WorkbenchFileOwnershipError,
)
from core.workbench_resource_reporter import WorkbenchResourceReporter
from vendor.interface import FileSizeLimitExceeded
from vendor.tcadp.tcadp import TCADP


def _identity():
    return WorkbenchIdentityContext(
        binding_id="binding-9",
        canonical_subject="napi:prod:customer:7:user:9",
        customer_id=7,
        new_api_user_id=9,
        auth_epoch=3,
        display_name="User",
        application_id="customer-app-7",
        app_profile_id="7",
        access_mode="active",
        config_version=4,
    )


def _app_context():
    return WorkbenchAppContext(
        application_id="customer-app-7",
        app_profile_id="7",
        config_version=4,
        auth_epoch=3,
        vendor="Tencent",
        service_vendor="ChinaTencentCloud",
        app_id="provider-app-7",
        app_key="secret-app-key",
        space_id="space-7",
        template_agent_id="template-7",
        secret_id="secret-id",
        secret_key="secret-key",
        capabilities=("files",),
        limits={},
    )


class _Result:
    def __init__(self, scalar):
        self._scalar = scalar

    def scalar(self):
        return self._scalar


@pytest.fixture(autouse=True)
def _workbench_locator_secret(monkeypatch):
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_FILE_LOCATOR_KEY",
        base64.b64encode(b"f" * 32).decode("ascii"),
    )
    monkeypatch.setattr(tagentic_config, "WORKBENCH_FILE_LOCATOR_KEY_ID", "test-v1")
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_FILE_LOCATOR_PREVIOUS_KEYS_JSON",
        "{}",
    )


@pytest.mark.asyncio
async def test_upload_binding_persists_server_resolvable_locator_with_integrity_hashes(monkeypatch):
    db = SimpleNamespace(add=AsyncMock(), commit=AsyncMock())
    added = []
    db.add = added.append
    report = SimpleNamespace(EventId="wre_stable_upload")
    enqueue = AsyncMock(return_value=report)
    deliver = AsyncMock(return_value="delivered")
    monkeypatch.setattr(WorkbenchResourceReporter, "enqueue", enqueue)
    monkeypatch.setattr(WorkbenchResourceReporter, "deliver_event", deliver)
    file_workspace = SimpleNamespace(Status="pending")
    monkeypatch.setattr(
        "core.workbench_file_ownership.CoreWorkbenchWorkspace.stage_file",
        AsyncMock(return_value=file_workspace),
    )
    result = {
        "PrivateObjectKey": "workbench/customer-7/binding-9/random.pdf",
        "PrivateBucket": "customer-private-bucket",
        "PrivateRegion": "ap-guangzhou",
        "ContentSha256": "a" * 64,
        "Size": 123,
    }

    file_id = await WorkbenchFileOwnership.bind_upload(
        db,
        account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        identity=_identity(),
        app_context=_app_context(),
        result=result,
        file_name="report.pdf",
        file_type="application/pdf",
    )

    binding = added[0]
    assert file_id.startswith("wf_")
    assert binding.FileUrlHash == "a" * 64
    assert binding.CosUrlHash != result["PrivateObjectKey"]
    assert binding.CosBucketHash != result["PrivateBucket"]
    assert binding.FileName == "report.pdf"
    assert binding.FileType == "pdf"
    assert WorkbenchFileOwnership._decrypt_locator(binding.LocatorCiphertext) == {
        "Storage": "private_cos_v1",
        "ObjectKey": result["PrivateObjectKey"],
        "Bucket": result["PrivateBucket"],
        "Region": result["PrivateRegion"],
        "ContentSha256": "a" * 64,
    }
    assert result["PrivateObjectKey"] not in binding.LocatorCiphertext
    assert not hasattr(binding, "FileUrl")
    assert binding.Status == "active"
    assert file_workspace.Status == "active"
    assert db.commit.await_count == 2
    enqueue.assert_awaited_once_with(
        db,
        identity=_identity(),
        resource_type="file",
        resource_id=file_id,
        parent_resource_type="account",
        parent_resource_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    deliver.assert_awaited_once_with(
        db,
        report.EventId,
        fail_closed_on_rejection=True,
    )


@pytest.mark.asyncio
async def test_workbench_file_id_from_another_user_fails_closed():
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(None)))

    with pytest.raises(WorkbenchFileOwnershipError, match="file not found"):
        await WorkbenchFileOwnership.resolve_uploaded_file(
            db,
            file_id="wf_owned_by_another_user",
            account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            identity=_identity(),
            app_context=_app_context(),
            conversation_id=None,
        )


@pytest.mark.asyncio
async def test_unconfirmed_resource_report_never_activates_file_binding(monkeypatch):
    added = []
    db = SimpleNamespace(add=added.append, commit=AsyncMock())
    report = SimpleNamespace(EventId="wre_retry", Status="retry")
    monkeypatch.setattr(
        WorkbenchResourceReporter,
        "enqueue",
        AsyncMock(return_value=report),
    )
    monkeypatch.setattr(
        WorkbenchResourceReporter,
        "deliver_event",
        AsyncMock(return_value="retry"),
    )
    file_workspace = SimpleNamespace(Status="pending")
    monkeypatch.setattr(
        "core.workbench_file_ownership.CoreWorkbenchWorkspace.stage_file",
        AsyncMock(return_value=file_workspace),
    )

    with pytest.raises(WorkbenchFileOwnershipError, match="confirmation") as raised:
        await WorkbenchFileOwnership.bind_upload(
            db,
            account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            identity=_identity(),
            app_context=_app_context(),
            result={
                "PrivateObjectKey": "workbench/customer-7/binding-9/random.pdf",
                "PrivateBucket": "customer-private-bucket",
                "PrivateRegion": "ap-guangzhou",
                "ContentSha256": "d" * 64,
                "Size": 123,
            },
            file_name="report.pdf",
            file_type="application/pdf",
        )

    assert raised.value.status_code == 503
    assert added[0].Status == "failed"
    assert file_workspace.Status == "failed"
    assert report.Status == "cancelled"
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_pre_turn_file_is_always_reported_as_account_owned(monkeypatch):
    added = []
    db = SimpleNamespace(add=added.append, commit=AsyncMock())
    report = SimpleNamespace(EventId="wre_workspace_file")
    enqueue = AsyncMock(return_value=report)
    monkeypatch.setattr(WorkbenchResourceReporter, "enqueue", enqueue)
    monkeypatch.setattr(
        WorkbenchResourceReporter,
        "deliver_event",
        AsyncMock(return_value="delivered"),
    )
    file_workspace = SimpleNamespace(Status="pending")
    monkeypatch.setattr(
        "core.workbench_file_ownership.CoreWorkbenchWorkspace.stage_file",
        AsyncMock(return_value=file_workspace),
    )

    file_id = await WorkbenchFileOwnership.bind_upload(
        db,
        account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        identity=_identity(),
        app_context=_app_context(),
        result={
            "PrivateObjectKey": "workbench/customer-7/binding-9/random.pdf",
            "PrivateBucket": "customer-private-bucket",
            "PrivateRegion": "ap-guangzhou",
            "ContentSha256": "e" * 64,
            "Size": 123,
        },
        file_name="report.pdf",
        file_type="application/pdf",
    )

    enqueue.assert_awaited_once_with(
        db,
        identity=_identity(),
        resource_type="file",
        resource_id=file_id,
        parent_resource_type="account",
        parent_resource_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    assert file_workspace.Status == "active"


@pytest.mark.asyncio
async def test_workbench_file_id_resolves_only_the_stored_locator(monkeypatch):
    locator = {
        "Storage": "private_cos_v1",
        "ObjectKey": "workbench/owned/file.pdf",
        "Bucket": "owned-bucket",
        "Region": "ap-guangzhou",
        "ContentSha256": "b" * 64,
    }
    binding = SimpleNamespace(
        ProviderAppId="provider-app-7",
        FileName="report.pdf",
        FileType="pdf",
        FileSize=123,
        FileUrlHash="b" * 64,
        CosUrlHash=WorkbenchFileOwnership._digest(locator["ObjectKey"]),
        CosBucketHash=WorkbenchFileOwnership._digest(locator["Bucket"]),
        LocatorCiphertext=WorkbenchFileOwnership._encrypt_locator(locator),
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(binding)))
    monkeypatch.setattr(
        "core.workbench_file_ownership.WorkbenchSecureFilePipeline.presign_private_locator",
        lambda value: "https://cos.example/trusted-file?short-signature",
    )
    monkeypatch.setattr(
        "core.workbench_file_ownership.CoreWorkbenchWorkspace.get_owned_file_workspace",
        AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace())),
    )

    contents = await WorkbenchFileOwnership.prepare_chat_contents(
        db,
        contents=[
            {
                "Type": "file",
                "File": {
                    "WorkbenchFileId": "wf_owned",
                },
            }
        ],
        account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        identity=_identity(),
        app_context=_app_context(),
        conversation_id=None,
    )

    assert contents == [
        {
            "Type": "file",
            "File": {
                "FileName": "report.pdf",
                "FileSize": "123",
                "FileUrl": "https://cos.example/trusted-file?short-signature",
                "FileType": "pdf",
                "Url": "https://cos.example/trusted-file?short-signature",
            },
        }
    ]


@pytest.mark.asyncio
async def test_tampered_stored_locator_fails_closed(monkeypatch):
    locator = {
        "Storage": "private_cos_v1",
        "ObjectKey": "workbench/owned/file.pdf",
        "Bucket": "owned-bucket",
        "Region": "ap-guangzhou",
        "ContentSha256": "c" * 64,
    }
    ciphertext = WorkbenchFileOwnership._encrypt_locator(locator)
    segments = ciphertext.split(".")
    segments[3] = ("A" if segments[3][0] != "A" else "B") + segments[3][1:]
    binding = SimpleNamespace(
        ProviderAppId="provider-app-7",
        FileName="report.pdf",
        FileType="pdf",
        FileSize=123,
        FileUrlHash="c" * 64,
        CosUrlHash=WorkbenchFileOwnership._digest(locator["ObjectKey"]),
        CosBucketHash=WorkbenchFileOwnership._digest(locator["Bucket"]),
        LocatorCiphertext=".".join(segments),
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(binding)))
    monkeypatch.setattr(
        "core.workbench_file_ownership.CoreWorkbenchWorkspace.get_owned_file_workspace",
        AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace())),
    )

    with pytest.raises(WorkbenchFileOwnershipError, match="stored file locator is invalid"):
        await WorkbenchFileOwnership.resolve_uploaded_file(
            db,
            file_id="wf_owned",
            account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            identity=_identity(),
            app_context=_app_context(),
            conversation_id=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contents",
    [
        [{"Type": "file", "File": {"FileUrl": "https://attacker.example/file"}}],
        [{"Type": "file", "File": {"DocId": "victim-doc"}}],
        [
            {
                "Type": "widget_action",
                "WidgetAction": {"Payload": {"nested": {"Url": "https://attacker.example"}}},
            }
        ],
        [{"Type": "text", "Text": "hello", "Nested": {"CustomVariables": {"x": "y"}}}],
    ],
)
async def test_arbitrary_locator_and_nested_custom_variables_are_rejected(contents):
    db = SimpleNamespace(execute=AsyncMock())

    with pytest.raises(WorkbenchFileOwnershipError, match="untrusted content locator"):
        await WorkbenchFileOwnership.prepare_chat_contents(
            db,
            contents=contents,
            account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            identity=_identity(),
            app_context=_app_context(),
            conversation_id=None,
        )

    db.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (
            [
                {
                    "Type": "file",
                    "File": {
                        "WorkbenchFileId": "wf_owned",
                        "FileName": "browser-controlled.pdf",
                    },
                }
            ],
            "untrusted file locator",
        ),
        ([{"Type": "image", "Image": {"Id": "unknown"}}], "unsupported workbench content type"),
    ],
)
async def test_unmapped_workbench_content_shapes_fail_closed(contents, message):
    db = SimpleNamespace(execute=AsyncMock())

    with pytest.raises(WorkbenchFileOwnershipError, match=message):
        await WorkbenchFileOwnership.prepare_chat_contents(
            db,
            contents=contents,
            account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            identity=_identity(),
            app_context=_app_context(),
            conversation_id=None,
        )

    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_deeply_nested_workbench_payload_is_bounded():
    nested = {}
    cursor = nested
    for _ in range(34):
        cursor["Nested"] = {}
        cursor = cursor["Nested"]
    db = SimpleNamespace(execute=AsyncMock())

    with pytest.raises(WorkbenchFileOwnershipError, match="too deeply nested"):
        await WorkbenchFileOwnership.prepare_chat_contents(
            db,
            contents=[{"Type": "text", "Text": "hello", "Nested": nested}],
            account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            identity=_identity(),
            app_context=_app_context(),
            conversation_id=None,
        )

    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_declared_oversized_upload_is_rejected_before_provider_credentials(monkeypatch):
    provider_request = AsyncMock()
    monkeypatch.setattr("vendor.tcadp.tcadp.tc_request", provider_request)
    vendor = TCADP(
        {
            "Vendor": "Tencent",
            "ServiceVendor": "ChinaTencentCloud",
            "AppId": "provider-app-7",
            "AppKey": "provider-app-key-secret",
        },
        "customer-app-7",
    )
    request = SimpleNamespace(
        headers={"Content-Length": "11"},
        stream=SimpleNamespace(read=AsyncMock()),
    )

    with pytest.raises(FileSizeLimitExceeded, match="max_file_bytes"):
        await vendor.upload(
            SimpleNamespace(),
            request,
            "account-9",
            "text/plain",
            mode="claw",
            max_file_bytes=10,
        )

    provider_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_workbench_mode_can_never_call_legacy_public_claw_upload(monkeypatch):
    provider_request = AsyncMock()
    monkeypatch.setattr("vendor.tcadp.tcadp.tc_request", provider_request)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    vendor = TCADP(
        {
            "Vendor": "Tencent",
            "ServiceVendor": "ChinaTencentCloud",
            "AppId": "provider-app-7",
            "AppKey": "provider-app-key-secret",
        },
        "customer-app-7",
    )

    with pytest.raises(ValueError, match="private scanned file pipeline"):
        await vendor.upload(
            SimpleNamespace(),
            SimpleNamespace(headers={}, stream=SimpleNamespace(read=AsyncMock())),
            "account-9",
            "application/pdf",
            mode="claw",
        )

    provider_request.assert_not_awaited()


def test_public_upload_result_never_exposes_provider_locators():
    projected = WorkbenchFileOwnership.public_upload_result(
        file_id="wf_owned",
        file_name="report.pdf",
        file_type="application/pdf",
        file_size="123",
    )

    assert projected == {
        "WorkbenchFileId": "wf_owned",
        "Name": "report.pdf",
        "Type": "application/pdf",
        "Size": 123,
    }
    assert not {"Url", "CosUrl", "CosBucket", "FileUrl"}.intersection(projected)


def test_file_locator_rotation_can_read_previous_key(monkeypatch):
    locator = {
        "Storage": "private_cos_v1",
        "ObjectKey": "workbench/owned/file.pdf",
        "Bucket": "owned-bucket",
        "Region": "ap-guangzhou",
        "ContentSha256": "a" * 64,
    }
    old_key = base64.b64encode(b"o" * 32).decode("ascii")
    new_key = base64.b64encode(b"n" * 32).decode("ascii")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_FILE_LOCATOR_KEY", old_key)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_FILE_LOCATOR_KEY_ID", "old")
    ciphertext = WorkbenchFileOwnership._encrypt_locator(locator)

    monkeypatch.setattr(tagentic_config, "WORKBENCH_FILE_LOCATOR_KEY", new_key)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_FILE_LOCATOR_KEY_ID", "new")
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_FILE_LOCATOR_PREVIOUS_KEYS_JSON",
        '{"old":"' + old_key + '"}',
    )

    assert WorkbenchFileOwnership._decrypt_locator(ciphertext) == locator
