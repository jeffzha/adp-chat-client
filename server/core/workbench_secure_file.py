import asyncio
import hashlib
import logging
import mimetypes
import os
import re
import struct
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit

from qcloud_cos import CosConfig, CosS3Client

from config import tagentic_config
from core.workbench_metrics import WORKBENCH_METRICS
from vendor.interface import FileSizeLimitExceeded


logger = logging.getLogger(__name__)


class WorkbenchSecureFileError(RuntimeError):
    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


class MalwareDetectedError(WorkbenchSecureFileError):
    def __init__(self):
        super().__init__("file was rejected by malware scanning", 422)


class WorkbenchStreamingSecretRedactor:
    """Redact exact secret patterns even when text arrives across SSE deltas."""

    MAX_PATTERN_COUNT = 256
    MAX_PATTERN_LENGTH = 8192
    MAX_PATTERN_BYTES = 64 * 1024
    MAX_FEED_CHARS = 1024 * 1024
    _TERMINAL = object()

    def __init__(self, patterns: tuple[tuple[str, str], ...]):
        unique = {}
        for pattern, replacement in patterns:
            if not isinstance(pattern, str) or len(pattern) < 4:
                continue
            if len(pattern) > self.MAX_PATTERN_LENGTH:
                raise WorkbenchSecureFileError(
                    "workbench streaming redaction pattern is too large",
                    502,
                )
            unique.setdefault(pattern, str(replacement))
        if (
            len(unique) > self.MAX_PATTERN_COUNT
            or sum(len(pattern.encode("utf-8")) for pattern in unique)
            > self.MAX_PATTERN_BYTES
        ):
            raise WorkbenchSecureFileError(
                "workbench streaming redaction pattern set is too large",
                502,
            )

        self._trie = {}
        for pattern, replacement in unique.items():
            node = self._trie
            for character in pattern:
                node = node.setdefault(character, {})
            node[self._TERMINAL] = replacement
        self._pending = ""

    def feed(self, value: str) -> str:
        if not isinstance(value, str):
            raise WorkbenchSecureFileError(
                "provider text delta is invalid",
                502,
            )
        if len(value) > self.MAX_FEED_CHARS:
            raise WorkbenchSecureFileError(
                "provider text delta exceeds the redaction limit",
                502,
            )
        projected = []
        for character in value:
            self._pending += character
            self._drain(projected)
        return "".join(projected)

    def finish(self) -> str:
        projected = []
        self._drain(projected, final=True)
        if self._pending:
            # An incomplete suffix may be the beginning of a locator or secret
            # whose stream was interrupted. It must not be released verbatim.
            projected.append("[secret-redacted]")
            self._pending = ""
        return "".join(projected)

    def _drain(self, projected: list[str], *, final: bool = False) -> None:
        while self._pending:
            node = self._trie
            matched_prefix = True
            longest_terminal: tuple[int, str] | None = None
            for index, character in enumerate(self._pending, start=1):
                child = node.get(character)
                if child is None:
                    matched_prefix = False
                    break
                node = child
                replacement = node.get(self._TERMINAL)
                if replacement is not None:
                    longest_terminal = (index, replacement)
            if matched_prefix:
                replacement = node.get(self._TERMINAL)
                has_longer_pattern = any(key != self._TERMINAL for key in node)
                if replacement is not None and (final or not has_longer_pattern):
                    projected.append(replacement)
                    self._pending = ""
                return
            if longest_terminal is not None:
                length, replacement = longest_terminal
                projected.append(replacement)
                self._pending = self._pending[length:]
                continue
            projected.append(self._pending[0])
            self._pending = self._pending[1:]


@dataclass(frozen=True)
class StoredWorkbenchFile:
    object_key: str
    bucket: str
    region: str
    file_name: str
    mime_type: str
    size: int
    content_sha256: str

    def ownership_result(self) -> dict:
        return {
            "PrivateObjectKey": self.object_key,
            "PrivateBucket": self.bucket,
            "PrivateRegion": self.region,
            "MimeType": self.mime_type,
            "Size": self.size,
            "ContentSha256": self.content_sha256,
        }


class ClamAVInstreamScanner:
    _CHUNK_BYTES = 64 * 1024
    _MAX_RESPONSE_BYTES = 1024

    def __init__(self, host: str, port: int, timeout: int):
        self.host = host.strip()
        self.port = port
        self.timeout = timeout

    async def scan(self, path: str) -> None:
        if not self.host:
            raise WorkbenchSecureFileError("workbench malware scanner is not configured")
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout,
            )
            writer.write(b"zINSTREAM\0")
            with open(path, "rb") as source:
                while True:
                    chunk = await asyncio.to_thread(source.read, self._CHUNK_BYTES)
                    if not chunk:
                        break
                    writer.write(struct.pack("!I", len(chunk)))
                    writer.write(chunk)
                    await asyncio.wait_for(writer.drain(), timeout=self.timeout)
            writer.write(struct.pack("!I", 0))
            await asyncio.wait_for(writer.drain(), timeout=self.timeout)
            response = await asyncio.wait_for(
                reader.read(self._MAX_RESPONSE_BYTES + 1),
                timeout=self.timeout,
            )
        except (OSError, asyncio.TimeoutError) as error:
            raise WorkbenchSecureFileError("workbench malware scanner is unavailable") from error
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

        if len(response) > self._MAX_RESPONSE_BYTES:
            raise WorkbenchSecureFileError("workbench malware scanner returned an invalid response")
        verdict = response.rstrip(b"\0\r\n")
        if verdict.endswith(b" FOUND"):
            raise MalwareDetectedError()
        if not verdict.endswith(b" OK"):
            raise WorkbenchSecureFileError("workbench malware scanner did not return a clean verdict")


class PrivateCosStorage:
    def __init__(self):
        self.region = tagentic_config.WORKBENCH_FILE_COS_REGION.strip()
        self.bucket = tagentic_config.WORKBENCH_FILE_COS_BUCKET.strip()
        secret_id = tagentic_config.WORKBENCH_FILE_COS_SECRET_ID.strip()
        secret_key = tagentic_config.WORKBENCH_FILE_COS_SECRET_KEY.strip()
        if not all((self.region, self.bucket, secret_id, secret_key)):
            raise WorkbenchSecureFileError("private workbench COS is not configured")
        if (
            not re.fullmatch(r"[a-z0-9-]{1,64}", self.region)
            or not re.fullmatch(r"[a-z0-9-]{1,255}", self.bucket)
        ):
            raise WorkbenchSecureFileError("private workbench COS configuration is invalid")
        self.client = CosS3Client(
            CosConfig(
                Region=self.region,
                SecretId=secret_id,
                SecretKey=secret_key,
                Scheme="https",
                VerifySSL=True,
            )
        )

    async def put(
        self,
        path: str,
        object_key: str,
        mime_type: str,
        content_sha256: str,
    ) -> None:
        def upload() -> None:
            with open(path, "rb") as source:
                self.client.put_object(
                    Bucket=self.bucket,
                    Body=source,
                    Key=object_key,
                    ContentType=mime_type,
                    Metadata={"workbench-sha256": content_sha256},
                    ACL="private",
                    EnableMD5=True,
                )

        try:
            await asyncio.to_thread(upload)
        except Exception as error:
            logger.error(
                "private workbench COS upload failed: error_type=%s",
                type(error).__name__,
            )
            raise WorkbenchSecureFileError("private workbench file storage is unavailable") from error

    async def delete(self, object_key: str, *, bucket: str, region: str) -> None:
        if bucket != self.bucket or region != self.region:
            raise WorkbenchSecureFileError("stored workbench file is outside private storage", 500)
        try:
            await asyncio.to_thread(
                self.client.delete_object,
                Bucket=bucket,
                Key=object_key,
            )
        except Exception as error:
            logger.error(
                "private workbench COS compensation delete failed: error_type=%s",
                type(error).__name__,
            )
            raise WorkbenchSecureFileError(
                "private workbench file cleanup is unavailable"
            ) from error

    def presign(self, object_key: str, *, bucket: str, region: str) -> str:
        if bucket != self.bucket or region != self.region:
            raise WorkbenchSecureFileError("stored workbench file is outside private storage", 500)
        try:
            return self.client.get_presigned_download_url(
                Bucket=bucket,
                Key=object_key,
                Expired=tagentic_config.WORKBENCH_FILE_URL_EXPIRE_SECONDS,
                SignHost=True,
            )
        except Exception as error:
            logger.error(
                "private workbench COS signing failed: error_type=%s",
                type(error).__name__,
            )
            raise WorkbenchSecureFileError("private workbench file storage is unavailable") from error


class WorkbenchSecureFilePipeline:
    _upload_semaphore = None
    _upload_semaphore_limit = None
    _ALLOWED_EXTENSIONS = frozenset(
        {
            ".csv", ".doc", ".docx", ".gif", ".jpeg", ".jpg", ".md",
            ".pdf", ".png", ".ppt", ".pptx", ".txt", ".webp", ".xls", ".xlsx",
        }
    )
    _EXTENSION_MIMES = {
        ".csv": {"text/csv", "text/plain", "application/vnd.ms-excel"},
        ".doc": {"application/msword"},
        ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
        ".gif": {"image/gif"},
        ".jpeg": {"image/jpeg"},
        ".jpg": {"image/jpeg"},
        ".md": {"text/markdown", "text/plain"},
        ".pdf": {"application/pdf"},
        ".png": {"image/png"},
        ".ppt": {"application/vnd.ms-powerpoint"},
        ".pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
        ".txt": {"text/plain"},
        ".webp": {"image/webp"},
        ".xls": {"application/vnd.ms-excel"},
        ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    }

    def __init__(self, scanner=None, storage=None):
        if not tagentic_config.WORKBENCH_FILES_ENABLED:
            raise WorkbenchSecureFileError("workbench file upload is disabled", 503)
        self.scanner = scanner or ClamAVInstreamScanner(
            tagentic_config.WORKBENCH_FILE_SCANNER_HOST,
            tagentic_config.WORKBENCH_FILE_SCANNER_PORT,
            tagentic_config.WORKBENCH_FILE_SCANNER_TIMEOUT_SECONDS,
        )
        self.storage = storage or PrivateCosStorage()

    @classmethod
    def _metadata(cls, file_name: str, declared_type: str) -> tuple[str, str, str]:
        safe_name = Path(str(file_name or "")).name.strip()
        if not safe_name or safe_name in {".", ".."} or len(safe_name) > 255:
            raise WorkbenchSecureFileError("invalid upload file name", 400)
        extension = Path(safe_name).suffix.lower()
        if extension not in cls._ALLOWED_EXTENSIONS:
            raise WorkbenchSecureFileError("unsupported upload file extension", 415)
        normalized_type = str(declared_type or "").split(";", 1)[0].strip().lower()
        if normalized_type not in cls._EXTENSION_MIMES[extension]:
            guessed = mimetypes.guess_type(safe_name)[0]
            if normalized_type == "application/octet-stream" and guessed:
                normalized_type = guessed
            if normalized_type not in cls._EXTENSION_MIMES[extension]:
                raise WorkbenchSecureFileError("upload MIME type does not match its extension", 415)
        return safe_name, extension, normalized_type

    @staticmethod
    def _check_signature(source: BinaryIO, extension: str) -> None:
        header = source.read(8192)
        source.seek(0)
        if not header:
            raise WorkbenchSecureFileError("empty files are not accepted", 400)
        if header[:2] == b"MZ" or header.startswith(b"\x7fELF"):
            raise WorkbenchSecureFileError("executable files are not accepted", 415)
        signatures = {
            ".pdf": (b"%PDF-",),
            ".png": (b"\x89PNG\r\n\x1a\n",),
            ".jpg": (b"\xff\xd8\xff",),
            ".jpeg": (b"\xff\xd8\xff",),
            ".gif": (b"GIF87a", b"GIF89a"),
            ".webp": (b"RIFF",),
            ".doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
            ".xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
            ".ppt": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
            ".docx": (b"PK\x03\x04",),
            ".xlsx": (b"PK\x03\x04",),
            ".pptx": (b"PK\x03\x04",),
        }
        expected = signatures.get(extension)
        if expected and not any(header.startswith(signature) for signature in expected):
            raise WorkbenchSecureFileError("file content does not match its extension", 415)
        if extension == ".webp" and (len(header) < 12 or header[8:12] != b"WEBP"):
            raise WorkbenchSecureFileError("file content does not match its extension", 415)
        if extension in {".txt", ".md", ".csv"} and b"\0" in header:
            raise WorkbenchSecureFileError("binary content is not accepted as text", 415)

    @classmethod
    def _concurrency_guard(cls):
        limit = tagentic_config.WORKBENCH_FILE_MAX_CONCURRENT_UPLOADS
        if cls._upload_semaphore is None or cls._upload_semaphore_limit != limit:
            cls._upload_semaphore = asyncio.Semaphore(limit)
            cls._upload_semaphore_limit = limit
        return cls._upload_semaphore

    async def store_request(self, request, **kwargs) -> StoredWorkbenchFile:
        async with self._concurrency_guard():
            return await self._store_request(request, **kwargs)

    async def _store_request(
        self,
        request,
        *,
        file_name: str,
        declared_type: str,
        max_file_bytes: int,
        customer_id: int,
        binding_id: str,
    ) -> StoredWorkbenchFile:
        safe_name, extension, normalized_type = self._metadata(file_name, declared_type)
        if (
            customer_id <= 0
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(binding_id or ""))
        ):
            raise WorkbenchSecureFileError("workbench file ownership scope is invalid", 500)
        declared_size = request.headers.get("Content-Length")
        parsed_size = None
        if declared_size:
            try:
                parsed_size = int(declared_size)
            except ValueError as error:
                raise WorkbenchSecureFileError("invalid Content-Length", 400) from error
            if parsed_size < 0 or parsed_size > max_file_bytes:
                raise FileSizeLimitExceeded("file exceeds max_file_bytes")

        try:
            descriptor, quarantine_path = tempfile.mkstemp(
                prefix="wb-upload-",
                suffix=".quarantine",
                dir=(
                    tagentic_config.WORKBENCH_FILE_QUARANTINE_DIR.strip()
                    or None
                ),
            )
        except OSError as error:
            raise WorkbenchSecureFileError(
                "workbench quarantine storage is unavailable"
            ) from error
        size = 0
        content_digest = hashlib.sha256()
        try:
            try:
                with os.fdopen(descriptor, "wb") as quarantine:
                    while True:
                        chunk = await request.stream.read()
                        if chunk is None:
                            break
                        if not isinstance(chunk, bytes):
                            raise WorkbenchSecureFileError("invalid upload body", 400)
                        size += len(chunk)
                        if size > max_file_bytes:
                            raise FileSizeLimitExceeded("file exceeds max_file_bytes")
                        content_digest.update(chunk)
                        quarantine.write(chunk)
            except OSError as error:
                raise WorkbenchSecureFileError(
                    "workbench quarantine storage is unavailable"
                ) from error
            if parsed_size is not None and size != parsed_size:
                raise WorkbenchSecureFileError("upload body length does not match Content-Length", 400)
            with open(quarantine_path, "rb") as source:
                self._check_signature(source, extension)
            try:
                await self.scanner.scan(quarantine_path)
            except Exception:
                WORKBENCH_METRICS.inc("workbench_file_failures_total", stage="scan")
                raise
            object_key = (
                f"workbench/customer-{customer_id}/{binding_id}/"
                f"{uuid.uuid4().hex}{extension}"
            )
            content_sha256 = content_digest.hexdigest()
            upload_task = asyncio.create_task(
                self.storage.put(
                    quarantine_path,
                    object_key,
                    normalized_type,
                    content_sha256,
                )
            )
            try:
                await asyncio.shield(upload_task)
            except asyncio.CancelledError:
                uploaded = False
                try:
                    await upload_task
                    uploaded = True
                except Exception:
                    WORKBENCH_METRICS.inc("workbench_file_failures_total", stage="upload")
                    pass
                if uploaded:
                    cleanup_task = asyncio.create_task(
                        self.storage.delete(
                            object_key,
                            bucket=self.storage.bucket,
                            region=self.storage.region,
                        )
                    )
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        try:
                            await cleanup_task
                        except Exception:
                            pass
                    except Exception:
                        pass
                raise
            except Exception:
                WORKBENCH_METRICS.inc("workbench_file_failures_total", stage="upload")
                raise
            return StoredWorkbenchFile(
                object_key=object_key,
                bucket=self.storage.bucket,
                region=self.storage.region,
                file_name=safe_name,
                mime_type=normalized_type,
                size=size,
                content_sha256=content_sha256,
            )
        finally:
            try:
                os.remove(quarantine_path)
            except FileNotFoundError:
                pass

    async def discard(self, stored: StoredWorkbenchFile) -> None:
        await self.storage.delete(
            stored.object_key,
            bucket=stored.bucket,
            region=stored.region,
        )

    @staticmethod
    def presign_private_locator(locator: dict[str, str]) -> str:
        storage = PrivateCosStorage()
        return storage.presign(
            locator["ObjectKey"],
            bucket=locator["Bucket"],
            region=locator["Region"],
        )

    @staticmethod
    def private_urls(contents) -> tuple[str, ...]:
        patterns = set()
        if not isinstance(contents, list):
            return ()
        for content in contents:
            if not isinstance(content, dict) or content.get("Type") != "file":
                continue
            file_info = content.get("File")
            if not isinstance(file_info, dict):
                continue
            for key in ("FileUrl", "Url"):
                url = file_info.get(key)
                if isinstance(url, str) and url.startswith("https://"):
                    parsed = urlsplit(url)
                    base_url = url.split("?", 1)[0]
                    candidates = {
                        url,
                        base_url,
                        parsed.netloc,
                        parsed.path,
                        parsed.path.lstrip("/"),
                    }
                    for query_key, query_value in parse_qsl(
                        parsed.query,
                        keep_blank_values=True,
                    ):
                        if query_value:
                            candidates.add(f"{query_key}={query_value}")
                            candidates.add(query_value)
                    for candidate in candidates:
                        if len(candidate) < 4:
                            continue
                        encoded = quote(candidate, safe="")
                        plus_encoded = quote_plus(candidate, safe="")
                        patterns.update(
                            {
                                candidate,
                                encoded,
                                plus_encoded,
                                quote(encoded, safe=""),
                                quote_plus(plus_encoded, safe=""),
                            }
                        )
        return tuple(sorted(patterns, key=len, reverse=True))

    @staticmethod
    def streaming_secret_patterns(
        contents,
        sensitive_values: tuple[str, ...] = (),
    ) -> tuple[tuple[str, str], ...]:
        patterns = {
            pattern: "[workbench-private-file]"
            for pattern in WorkbenchSecureFilePipeline.private_urls(contents)
        }
        private_bucket = str(
            tagentic_config.WORKBENCH_FILE_COS_BUCKET or ""
        ).strip()
        if len(private_bucket) >= 4:
            patterns.setdefault(private_bucket, "[workbench-private-file]")
        for secret in sensitive_values:
            if not isinstance(secret, str) or len(secret) < 4:
                continue
            encoded = quote(secret, safe="")
            plus_encoded = quote_plus(secret, safe="")
            for representation in (
                secret,
                encoded,
                plus_encoded,
                quote(encoded, safe=""),
                quote_plus(plus_encoded, safe=""),
            ):
                patterns[representation] = "[secret-redacted]"
        return tuple(patterns.items())

    @staticmethod
    def redact_private_urls(
        value,
        private_urls: tuple[str, ...],
        sensitive_values: tuple[str, ...] = (),
    ):
        if isinstance(value, str):
            for private_url in private_urls:
                value = value.replace(private_url, "[workbench-private-file]")
            for secret in sensitive_values:
                if isinstance(secret, str) and len(secret) >= 4:
                    for representation in (
                        secret,
                        quote(secret, safe=""),
                        quote_plus(secret, safe=""),
                    ):
                        value = value.replace(representation, "[secret-redacted]")
            private_bucket = str(
                tagentic_config.WORKBENCH_FILE_COS_BUCKET or ""
            ).strip()
            value = re.sub(
                r"q-sign-[^&\s\"']+",
                "[signature-redacted]",
                value,
                flags=re.IGNORECASE,
            )
            value = re.sub(
                r"workbench(?:/|%2f|%252f)customer-[^\s\"']+",
                "[workbench-private-file]",
                value,
                flags=re.IGNORECASE,
            )
            if private_bucket:
                value = value.replace(private_bucket, "[private-bucket]")
            value = re.sub(
                r"https://[^\s\"']+\.cos\.[^\s\"']+/workbench/[^\s\"']+",
                "[workbench-private-file]",
                value,
                flags=re.IGNORECASE,
            )
            return value
        if isinstance(value, list):
            return [
                WorkbenchSecureFilePipeline.redact_private_urls(
                    item,
                    private_urls,
                    sensitive_values,
                )
                for item in value
            ]
        if isinstance(value, dict):
            projected = {}
            sensitive_keys = {
                "appkey", "secretid", "secretkey", "token", "authtoken",
                "authorization", "authconfig", "credentials", "credential",
                "accesstoken", "refreshtoken", "sessiontoken", "securitytoken",
                "tmpsecretid", "tmpsecretkey",
            }
            provider_workspace_keys = {
                "providerworkspaceid",
                "providerworkspacelocator",
                "workspacelocator",
                "workspaceid",
            }
            for key, item in value.items():
                projected_key = (
                    WorkbenchSecureFilePipeline.redact_private_urls(
                        key,
                        private_urls,
                        sensitive_values,
                    )
                    if isinstance(key, str)
                    else key
                )
                normalized_key = (
                    re.sub(r"[^a-z0-9]", "", key.lower())
                    if isinstance(key, str)
                    else ""
                )
                if normalized_key in provider_workspace_keys:
                    projected[projected_key] = "[workspace-redacted]"
                elif (
                    normalized_key in sensitive_keys
                    or normalized_key.endswith("secretid")
                    or normalized_key.endswith("secretkey")
                    or normalized_key.endswith("accesstoken")
                ):
                    projected[projected_key] = "[secret-redacted]"
                else:
                    projected[projected_key] = (
                        WorkbenchSecureFilePipeline.redact_private_urls(
                            item,
                            private_urls,
                            sensitive_values,
                        )
                    )
            return projected
        return value
