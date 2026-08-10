import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote_plus

import pytest

from config import tagentic_config
from core.workbench_secure_file import (
    MalwareDetectedError,
    ClamAVInstreamScanner,
    WorkbenchSecureFileError,
    WorkbenchSecureFilePipeline,
    WorkbenchStreamingSecretRedactor,
)
from vendor.interface import FileSizeLimitExceeded
from vendor.tcadp.tcadp import TCADP


class _Stream:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def read(self):
        if not self.chunks:
            return None
        return self.chunks.pop(0)


class _Storage:
    bucket = "private-workbench-1250000000"
    region = "ap-guangzhou"

    def __init__(self):
        self.put = AsyncMock()
        self.delete = AsyncMock()


@pytest.fixture(autouse=True)
def _enable_files(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_FILES_ENABLED", True)


@pytest.mark.asyncio
async def test_clean_upload_is_scanned_then_written_under_random_owned_key():
    scanner = SimpleNamespace(scan=AsyncMock())
    storage = _Storage()
    pipeline = WorkbenchSecureFilePipeline(scanner=scanner, storage=storage)
    request = SimpleNamespace(
        headers={"Content-Length": "14"},
        stream=_Stream([b"%PDF-1.7\nclean"]),
    )

    stored = await pipeline.store_request(
        request,
        file_name="report.pdf",
        declared_type="application/pdf",
        max_file_bytes=1024,
        customer_id=7,
        binding_id="binding-9",
    )

    scanner.scan.assert_awaited_once()
    quarantine_path = scanner.scan.await_args.args[0]
    assert not Path(quarantine_path).exists()
    storage.put.assert_awaited_once()
    assert storage.put.await_args.args[3] == stored.content_sha256
    assert stored.object_key.startswith("workbench/customer-7/binding-9/")
    assert stored.object_key.endswith(".pdf")
    assert stored.size == 14
    assert stored.content_sha256 == (
        "21da2c863a9ab83580b7acacc83c1723039f74f991a4c5795384f2c847a4b574"
    )
    assert "Url" not in stored.ownership_result()

    await pipeline.discard(stored)
    storage.delete.assert_awaited_once_with(
        stored.object_key,
        bucket=stored.bucket,
        region=stored.region,
    )


@pytest.mark.asyncio
async def test_malware_verdict_prevents_storage_and_cleans_quarantine():
    scanned = []

    async def reject(path):
        scanned.append(path)
        raise MalwareDetectedError()

    storage = _Storage()
    pipeline = WorkbenchSecureFilePipeline(
        scanner=SimpleNamespace(scan=reject),
        storage=storage,
    )
    request = SimpleNamespace(headers={}, stream=_Stream([b"%PDF-1.7\nmalicious"]))

    with pytest.raises(MalwareDetectedError):
        await pipeline.store_request(
            request,
            file_name="report.pdf",
            declared_type="application/pdf",
            max_file_bytes=1024,
            customer_id=7,
            binding_id="binding-9",
        )

    assert not Path(scanned[0]).exists()
    storage.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_during_cos_put_waits_then_compensates_private_object():
    started = asyncio.Event()
    finish = asyncio.Event()
    storage = _Storage()

    async def put(*_args):
        started.set()
        await finish.wait()

    storage.put = AsyncMock(side_effect=put)
    pipeline = WorkbenchSecureFilePipeline(
        scanner=SimpleNamespace(scan=AsyncMock()),
        storage=storage,
    )
    task = asyncio.create_task(
        pipeline.store_request(
            SimpleNamespace(headers={}, stream=_Stream([b"%PDF-1.7\nclean"])),
            file_name="report.pdf",
            declared_type="application/pdf",
            max_file_bytes=1024,
            customer_id=7,
            binding_id="binding-9",
        )
    )
    await started.wait()
    task.cancel()
    finish.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    storage.delete.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scanner",
    [
        ClamAVInstreamScanner("", 3310, 1),
        SimpleNamespace(
            scan=AsyncMock(
                side_effect=WorkbenchSecureFileError(
                    "workbench malware scanner did not return a clean verdict"
                )
            )
        ),
    ],
)
async def test_missing_or_unknown_scanner_verdict_never_reaches_storage(scanner):
    storage = _Storage()
    pipeline = WorkbenchSecureFilePipeline(scanner=scanner, storage=storage)
    request = SimpleNamespace(headers={}, stream=_Stream([b"%PDF-1.7\nclean"]))

    with pytest.raises(WorkbenchSecureFileError):
        await pipeline.store_request(
            request,
            file_name="report.pdf",
            declared_type="application/pdf",
            max_file_bytes=1024,
            customer_id=7,
            binding_id="binding-9",
        )

    storage.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_scanner_timeout_never_reaches_storage(monkeypatch):
    open_connection = AsyncMock(side_effect=asyncio.TimeoutError())
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    storage = _Storage()
    pipeline = WorkbenchSecureFilePipeline(
        scanner=ClamAVInstreamScanner("clamav", 3310, 1),
        storage=storage,
    )
    request = SimpleNamespace(headers={}, stream=_Stream([b"%PDF-1.7\nclean"]))

    with pytest.raises(WorkbenchSecureFileError, match="unavailable"):
        await pipeline.store_request(
            request,
            file_name="report.pdf",
            declared_type="application/pdf",
            max_file_bytes=1024,
            customer_id=7,
            binding_id="binding-9",
        )

    storage.put.assert_not_awaited()


def test_nested_provider_payload_redacts_bucket_object_and_signature(monkeypatch):
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_FILE_COS_BUCKET",
        "private-workbench-1250000000",
    )
    url = (
        "https://private-workbench-1250000000.cos.ap-guangzhou.myqcloud.com/"
        "workbench/customer-7/binding-9/private.pdf?q-sign-algorithm=sha1&q-sign-time=1"
    )
    projected = WorkbenchSecureFilePipeline.redact_private_urls(
        {
            "nested": [
                {
                    "FileUrl": url,
                    "encoded": quote_plus(url, safe=""),
                    "message": f"provider echoed {url}",
                    "AuthConfig": {"Token": "provider-token"},
                    "AppKey": "provider-app-key",
                }
            ]
        },
        (url, url.split("?", 1)[0]),
        ("provider-token", "provider-app-key"),
    )

    rendered = repr(projected)
    assert "private-workbench-1250000000" not in rendered
    assert "workbench/customer-" not in rendered
    assert "q-sign" not in rendered
    assert "provider-token" not in rendered
    assert "provider-app-key" not in rendered


@pytest.mark.parametrize("split_count", [2, 3, 11])
def test_streaming_redactor_blocks_signed_url_across_many_deltas(
    monkeypatch,
    split_count,
):
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_FILE_COS_BUCKET",
        "private-workbench-1250000000",
    )
    private_url = (
        "https://private-workbench-1250000000.cos.ap-guangzhou.myqcloud.com/"
        "workbench/customer-7/binding-9/private.pdf"
        "?q-sign-algorithm=sha1&q-signature=sensitive-signature"
    )
    patterns = WorkbenchSecureFilePipeline.streaming_secret_patterns(
        [{"Type": "file", "File": {"FileUrl": private_url, "Url": private_url}}],
        ("provider-app-key-secret",),
    )
    redactor = WorkbenchStreamingSecretRedactor(patterns)
    payload = f"before:{private_url}:after"
    boundaries = [len(payload) * index // split_count for index in range(split_count + 1)]

    projected = "".join(
        redactor.feed(payload[boundaries[index]:boundaries[index + 1]])
        for index in range(split_count)
    ) + redactor.finish()

    assert projected.startswith("before:")
    assert projected.endswith(":after")
    assert "[workbench-private-file]" in projected
    assert "private-workbench-1250000000" not in projected
    assert "workbench/customer-7" not in projected
    assert "q-sign" not in projected
    assert "sensitive-signature" not in projected


def test_streaming_redactor_preserves_non_sensitive_delta_order(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_FILE_COS_BUCKET", "private-bucket")
    redactor = WorkbenchStreamingSecretRedactor(
        WorkbenchSecureFilePipeline.streaming_secret_patterns([], ("provider-secret",))
    )

    projected = "".join(
        redactor.feed(part)
        for part in ("ordinary ", "streamed ", "response.")
    ) + redactor.finish()

    assert projected == "ordinary streamed response."


def test_streaming_redactor_blocks_encoded_locator_and_secret(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_FILE_COS_BUCKET", "private-bucket")
    private_url = (
        "https://private-bucket.cos.ap-guangzhou.myqcloud.com/"
        "workbench/customer-7/binding-9/private.pdf?q-signature=sensitive"
    )
    encoded_url = quote_plus(private_url, safe="")
    redactor = WorkbenchStreamingSecretRedactor(
        WorkbenchSecureFilePipeline.streaming_secret_patterns(
            [{"Type": "file", "File": {"FileUrl": private_url}}],
            ("provider-secret",),
        )
    )

    projected = (
        redactor.feed(encoded_url[:17])
        + redactor.feed(encoded_url[17:] + " provider-")
        + redactor.feed("secret")
        + redactor.finish()
    )

    assert "private-bucket" not in projected
    assert "q-signature" not in projected
    assert "provider-secret" not in projected
    assert "[workbench-private-file]" in projected
    assert "[secret-redacted]" in projected


def test_streaming_redactor_redacts_interrupted_secret_prefix():
    redactor = WorkbenchStreamingSecretRedactor(
        (("provider-secret", "[secret-redacted]"),)
    )

    projected = redactor.feed("safe provider-sec") + redactor.finish()

    assert projected == "safe [secret-redacted]"
    assert "provider-sec" not in projected


def test_streaming_redactor_prefers_the_longest_overlapping_secret():
    redactor = WorkbenchStreamingSecretRedactor(
        (
            ("secret", "[short-redacted]"),
            ("secret-long-token", "[long-redacted]"),
        )
    )

    projected = redactor.feed("before secret-long-token after") + redactor.finish()

    assert projected == "before [long-redacted] after"
    assert "long-token" not in projected


def test_streaming_redactor_rejects_unbounded_pattern_and_delta():
    with pytest.raises(WorkbenchSecureFileError, match="pattern is too large"):
        WorkbenchStreamingSecretRedactor(
            (("x" * (WorkbenchStreamingSecretRedactor.MAX_PATTERN_LENGTH + 1), "x"),)
        )

    redactor = WorkbenchStreamingSecretRedactor((("provider-secret", "x"),))
    with pytest.raises(WorkbenchSecureFileError, match="delta exceeds"):
        redactor.feed("x" * (WorkbenchStreamingSecretRedactor.MAX_FEED_CHARS + 1))


@pytest.mark.asyncio
async def test_workbench_history_is_conversation_scoped_then_sanitized(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_FILE_COS_BUCKET",
        "private-workbench-1250000000",
    )
    private_url = (
        "https://private-workbench-1250000000.cos.ap-guangzhou.myqcloud.com/"
        "workbench/customer-7/binding-9/private.pdf?q-sign-algorithm=sha1"
    )
    provider_request = AsyncMock(
        return_value={
            "Response": {
                "Messages": [
                    {
                        "Role": "assistant",
                        "RecordId": "record-1",
                        "ConversationId": "conversation-1",
                        "Status": "completed",
                        "Messages": [
                            {
                                "Type": "reply",
                                "Contents": [
                                    {"Type": "file", "File": {"FileUrl": private_url}}
                                ],
                            }
                        ],
                        "AuthConfig": {"Token": "provider-token"},
                        "AppKey": "provider-key",
                    }
                ]
            }
        }
    )
    monkeypatch.setattr("vendor.tcadp.tcadp.tc_request", provider_request)
    vendor = TCADP(
        {
            "Vendor": "Tencent",
            "ServiceVendor": "ChinaTencentCloud",
            "AppId": "provider-app-7",
            "AppKey": "provider-key",
        },
        "customer-app-7",
    )

    result = await vendor.get_messages_v2(
        SimpleNamespace(),
        "account-9",
        "conversation-1",
        50,
    )

    rendered = repr(result)
    assert "private-workbench-1250000000" not in rendered
    assert "workbench/customer-" not in rendered
    assert "q-sign" not in rendered
    assert "provider-token" not in rendered
    assert "provider-key" not in rendered


@pytest.mark.asyncio
async def test_mixed_conversation_history_fails_closed(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    provider_request = AsyncMock(
        return_value={
            "Response": {
                "Messages": [
                    {
                        "Role": "assistant",
                        "RecordId": "record-1",
                        "ConversationId": "conversation-other",
                        "Status": "completed",
                        "Messages": [],
                    }
                ]
            }
        }
    )
    monkeypatch.setattr("vendor.tcadp.tcadp.tc_request", provider_request)
    vendor = TCADP(
        {
            "Vendor": "Tencent",
            "ServiceVendor": "ChinaTencentCloud",
            "AppId": "provider-app-7",
            "AppKey": "provider-key",
        },
        "customer-app-7",
    )

    with pytest.raises(WorkbenchSecureFileError, match="conversation boundary") as raised:
        await vendor.get_messages_v2(
            SimpleNamespace(),
            "account-9",
            "conversation-1",
            50,
        )

    assert raised.value.status_code == 502


@pytest.mark.asyncio
async def test_history_missing_raw_conversation_id_cannot_be_backfilled(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(
        "vendor.tcadp.tcadp.tc_request",
        AsyncMock(
            return_value={
                "Response": {
                    "Messages": [
                        {
                            "RecordId": "record-1",
                            "Type": "reply",
                            "Contents": [],
                        }
                    ]
                }
            }
        ),
    )
    vendor = TCADP(
        {
            "Vendor": "Tencent",
            "ServiceVendor": "ChinaTencentCloud",
            "AppId": "provider-app-7",
            "AppKey": "provider-key",
        },
        "customer-app-7",
    )

    with pytest.raises(WorkbenchSecureFileError, match="conversation boundary"):
        await vendor.get_messages_v2(
            SimpleNamespace(),
            "account-9",
            "conversation-1",
            50,
        )


def test_disabled_files_fail_closed_before_scanner_or_storage(monkeypatch):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_FILES_ENABLED", False)
    scanner = SimpleNamespace(scan=AsyncMock())
    storage = _Storage()
    with pytest.raises(WorkbenchSecureFileError, match="disabled") as raised:
        WorkbenchSecureFilePipeline(scanner=scanner, storage=storage)
    assert raised.value.status_code == 503
    scanner.scan.assert_not_awaited()
    storage.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_declared_body_is_rejected_before_scanning():
    scanner = SimpleNamespace(scan=AsyncMock())
    storage = _Storage()
    pipeline = WorkbenchSecureFilePipeline(scanner=scanner, storage=storage)
    request = SimpleNamespace(headers={"Content-Length": "11"}, stream=_Stream([]))

    with pytest.raises(FileSizeLimitExceeded):
        await pipeline.store_request(
            request,
            file_name="report.pdf",
            declared_type="application/pdf",
            max_file_bytes=10,
            customer_id=7,
            binding_id="binding-9",
        )

    scanner.scan.assert_not_awaited()
    storage.put.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "mime_type", "content"),
    [
        ("payload.exe", "application/octet-stream", b"MZpayload"),
        ("renamed.pdf", "application/pdf", b"MZpayload"),
        ("image.jpg", "image/png", b"\xff\xd8\xffpayload"),
        ("notes.txt", "text/plain", b"text\0binary"),
    ],
)
async def test_extension_mime_and_signature_screening_fails_before_scan(
    name, mime_type, content
):
    scanner = SimpleNamespace(scan=AsyncMock())
    storage = _Storage()
    pipeline = WorkbenchSecureFilePipeline(scanner=scanner, storage=storage)
    request = SimpleNamespace(headers={}, stream=_Stream([content]))

    with pytest.raises(WorkbenchSecureFileError) as raised:
        await pipeline.store_request(
            request,
            file_name=name,
            declared_type=mime_type,
            max_file_bytes=1024,
            customer_id=7,
            binding_id="binding-9",
        )

    assert raised.value.status_code in {400, 415}
    scanner.scan.assert_not_awaited()
    storage.put.assert_not_awaited()
