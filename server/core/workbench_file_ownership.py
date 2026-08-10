import base64
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from jwcrypto import jwe, jwk
from jwcrypto.common import base64url_encode
from jwcrypto.jwe import InvalidJWEData

from config import tagentic_config
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_resource_reporter import (
    WorkbenchResourceReporter,
    WorkbenchResourceReportError,
)
from core.workbench_secure_file import (
    WorkbenchSecureFileError,
    WorkbenchSecureFilePipeline,
)
from core.workbench_workspace import CoreWorkbenchWorkspace, WorkbenchWorkspaceError
from model.workbench import WorkbenchFileBinding


class WorkbenchFileOwnershipError(RuntimeError):
    def __init__(self, message: str, status_code: int = 403):
        super().__init__(message)
        self.status_code = status_code


class WorkbenchFileOwnership:
    _ALLOWED_CONTENT_TYPES = frozenset({"text", "file"})
    _LOCATOR_KEYS = frozenset({"docid", "docbizid", "fileid"})

    @staticmethod
    def _digest(value: Any) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def public_upload_result(
        *,
        file_id: str,
        file_name: str,
        file_type: str,
        file_size: Any,
    ) -> dict[str, Any]:
        """Project an upload to inert metadata without provider locators."""

        try:
            normalized_size = int(file_size)
        except (TypeError, ValueError) as error:
            raise WorkbenchFileOwnershipError(
                "upload result has no trusted file size",
                502,
            ) from error
        if (
            not isinstance(file_id, str)
            or not file_id.startswith("wf_")
            or len(file_id) > 64
            or not isinstance(file_name, str)
            or not 1 <= len(file_name) <= 255
            or not isinstance(file_type, str)
            or len(file_type) > 128
            or normalized_size < 0
        ):
            raise WorkbenchFileOwnershipError("upload response metadata is invalid", 502)
        return {
            "WorkbenchFileId": file_id,
            "Name": file_name,
            "Type": file_type,
            "Size": normalized_size,
        }

    @staticmethod
    def _decode_locator_key(encoded: str, key_id: str) -> jwk.JWK:
        if not key_id or len(key_id) > 64:
            raise WorkbenchFileOwnershipError("workbench file locator key id is invalid", 500)
        try:
            key_bytes = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError) as error:
            raise WorkbenchFileOwnershipError("workbench file locator key is invalid", 500) from error
        if len(key_bytes) != 32:
            raise WorkbenchFileOwnershipError(
                "workbench file locator key must decode to 32 bytes",
                500,
            )
        return jwk.JWK(kty="oct", k=base64url_encode(key_bytes), kid=key_id)

    @classmethod
    def _active_locator_key(cls) -> tuple[str, jwk.JWK]:
        key_id = str(tagentic_config.WORKBENCH_FILE_LOCATOR_KEY_ID or "").strip()
        encoded = str(tagentic_config.WORKBENCH_FILE_LOCATOR_KEY or "").strip()
        return key_id, cls._decode_locator_key(encoded, key_id)

    @classmethod
    def _locator_key_for_id(cls, key_id: str) -> jwk.JWK:
        active_key_id, active_key = cls._active_locator_key()
        if key_id == active_key_id:
            return active_key
        raw_previous = str(
            tagentic_config.WORKBENCH_FILE_LOCATOR_PREVIOUS_KEYS_JSON or "{}"
        )
        try:
            previous = json.loads(raw_previous)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise WorkbenchFileOwnershipError(
                "workbench file locator previous keys are invalid",
                500,
            ) from error
        if not isinstance(previous, dict) or any(
            not isinstance(item_key, str) or not isinstance(item_value, str)
            for item_key, item_value in previous.items()
        ):
            raise WorkbenchFileOwnershipError(
                "workbench file locator previous keys are invalid",
                500,
            )
        encoded = previous.get(key_id)
        if encoded is None:
            raise WorkbenchFileOwnershipError(
                "workbench file locator decryption key is unavailable",
                500,
            )
        return cls._decode_locator_key(encoded.strip(), key_id)

    @classmethod
    def _encrypt_locator(cls, locator: dict[str, str]) -> str:
        key_id, key = cls._active_locator_key()
        token = jwe.JWE(
            json.dumps(locator, separators=(",", ":")).encode("utf-8"),
            protected={"alg": "A256KW", "enc": "A256GCM", "kid": key_id},
        )
        token.add_recipient(key)
        return token.serialize(compact=True)

    @classmethod
    def _decrypt_locator(cls, ciphertext: str) -> Any:
        token = jwe.JWE()
        token.deserialize(ciphertext)
        key_id = str(token.jose_header.get("kid") or "").strip()
        token.decrypt(cls._locator_key_for_id(key_id))
        return json.loads(token.payload)

    @classmethod
    async def bind_upload(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        result: dict[str, Any],
        file_name: str,
        file_type: str,
    ) -> str:
        try:
            file_size = int(result.get("Size"))
        except (TypeError, ValueError) as error:
            raise WorkbenchFileOwnershipError("upload result has no trusted file size", 502) from error
        object_key_hash = cls._digest(result.get("PrivateObjectKey"))
        bucket_hash = cls._digest(result.get("PrivateBucket"))
        region = str(result.get("PrivateRegion") or "").strip()
        content_sha256 = str(result.get("ContentSha256") or "").strip().lower()
        if (
            file_size < 0
            or object_key_hash is None
            or bucket_hash is None
            or not region
            or len(content_sha256) != 64
            or any(character not in "0123456789abcdef" for character in content_sha256)
        ):
            raise WorkbenchFileOwnershipError("upload result is incomplete", 502)
        file_name = str(file_name or "").strip()
        file_type = str(file_type or "").strip()
        if "." in file_name:
            file_type = file_name.rsplit(".", 1)[-1].lower()
        if not 1 <= len(file_name) <= 255 or len(file_type) > 128:
            raise WorkbenchFileOwnershipError("upload metadata is invalid", 400)

        locator = {
            "Storage": "private_cos_v1",
            "ObjectKey": str(result.get("PrivateObjectKey") or "").strip(),
            "Bucket": str(result.get("PrivateBucket") or "").strip(),
            "Region": region,
            "ContentSha256": content_sha256,
        }
        if (
            not 1 <= len(locator["ObjectKey"]) <= 1024
            or not 1 <= len(locator["Bucket"]) <= 255
            or not 1 <= len(locator["Region"]) <= 64
        ):
            raise WorkbenchFileOwnershipError("upload locator is invalid", 502)

        file_id = f"wf_{uuid.uuid4().hex}"
        binding = WorkbenchFileBinding(
                FileId=file_id,
                BindingId=identity.binding_id,
                AccountId=account_id,
                ApplicationId=app_context.application_id,
                ProviderAppId=app_context.app_id,
                FileName=file_name,
                FileType=file_type,
                FileUrlHash=content_sha256,
                CosUrlHash=object_key_hash,
                CosBucketHash=bucket_hash,
                LocatorCiphertext=cls._encrypt_locator(locator),
                FileSize=file_size,
                Status="pending",
            )
        db.add(binding)
        try:
            file_workspace = await CoreWorkbenchWorkspace.stage_file(
                db,
                file_id=file_id,
                account_id=account_id,
                identity=identity,
                app_context=app_context,
            )
            report = await WorkbenchResourceReporter.enqueue(
                db,
                identity=identity,
                resource_type="file",
                resource_id=file_id,
                parent_resource_type="account",
                parent_resource_id=str(account_id),
            )
        except (WorkbenchResourceReportError, WorkbenchWorkspaceError) as error:
            raise WorkbenchFileOwnershipError(str(error), error.status_code) from error
        await db.commit()
        try:
            status = await WorkbenchResourceReporter.deliver_event(
                db,
                report.EventId,
                fail_closed_on_rejection=True,
            )
        except WorkbenchResourceReportError as error:
            binding.Status = "failed"
            file_workspace.Status = "failed"
            await db.commit()
            raise WorkbenchFileOwnershipError(str(error), error.status_code) from error
        if status != "delivered":
            binding.Status = "failed"
            file_workspace.Status = "failed"
            report.Status = "cancelled"
            await db.commit()
            raise WorkbenchFileOwnershipError(
                "resource ownership confirmation is unavailable",
                503,
            )
        binding.Status = "active"
        file_workspace.Status = "active"
        await db.commit()
        return file_id

    @classmethod
    async def resolve_uploaded_file(
        cls,
        db: AsyncSession,
        *,
        file_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        conversation_id: str | None,
    ) -> tuple[WorkbenchFileBinding, dict[str, str]]:
        binding = (
            await db.execute(
                select(WorkbenchFileBinding).where(
                    WorkbenchFileBinding.FileId == file_id,
                    WorkbenchFileBinding.BindingId == identity.binding_id,
                    WorkbenchFileBinding.AccountId == account_id,
                    WorkbenchFileBinding.ApplicationId == app_context.application_id,
                    WorkbenchFileBinding.Status == "active",
                ).limit(1)
            )
        ).scalar()
        if binding is None:
            raise WorkbenchFileOwnershipError("file not found", 404)
        if binding.ProviderAppId != app_context.app_id:
            raise WorkbenchFileOwnershipError("file is outside the active workbench context")
        try:
            workspace_scope = await CoreWorkbenchWorkspace.get_owned_file_workspace(
                db,
                file_id=file_id,
                account_id=account_id,
                identity=identity,
                app_context=app_context,
                conversation_id=conversation_id,
            )
        except WorkbenchWorkspaceError as error:
            raise WorkbenchFileOwnershipError(str(error), error.status_code) from error
        if workspace_scope is None:
            raise WorkbenchFileOwnershipError("file not found", 404)

        try:
            locator = cls._decrypt_locator(binding.LocatorCiphertext)
        except (InvalidJWEData, TypeError, ValueError, json.JSONDecodeError) as error:
            raise WorkbenchFileOwnershipError("stored file locator is invalid", 500) from error
        if not isinstance(locator, dict) or set(locator) != {
            "Storage", "ObjectKey", "Bucket", "Region", "ContentSha256"
        }:
            raise WorkbenchFileOwnershipError("stored file locator is invalid", 500)
        locator = {key: str(locator.get(key) or "").strip() for key in locator}
        if (
            locator["Storage"] != "private_cos_v1"
            or not locator["ObjectKey"]
            or not locator["Bucket"]
            or not locator["Region"]
            or len(locator["ContentSha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in locator["ContentSha256"]
            )
        ):
            raise WorkbenchFileOwnershipError("stored file locator is invalid", 500)
        expected_hashes = {
            "FileUrlHash": locator["ContentSha256"],
            "CosUrlHash": cls._digest(locator["ObjectKey"]),
            "CosBucketHash": cls._digest(locator["Bucket"]),
        }
        if any(getattr(binding, field_name) != digest for field_name, digest in expected_hashes.items()):
            raise WorkbenchFileOwnershipError("stored file locator failed integrity check", 500)

        return binding, locator

    @classmethod
    async def prepare_chat_contents(
        cls,
        db: AsyncSession,
        *,
        contents: Any,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        conversation_id: str | None,
    ) -> list[dict[str, Any]]:
        if not isinstance(contents, list) or not contents or len(contents) > 64:
            raise WorkbenchFileOwnershipError("Contents must be a non-empty list", 400)

        prepared: list[dict[str, Any]] = []
        for content in contents:
            cls._reject_untrusted_locator(content)
            if not isinstance(content, dict):
                raise WorkbenchFileOwnershipError("each content item must be an object", 400)
            content_type = content.get("Type")
            if content_type not in cls._ALLOWED_CONTENT_TYPES:
                raise WorkbenchFileOwnershipError("unsupported workbench content type", 400)

            if content_type == "text":
                if set(content).difference({"Type", "Text"}) or not isinstance(content.get("Text"), str):
                    raise WorkbenchFileOwnershipError("invalid text content", 400)
                prepared.append({"Type": "text", "Text": content["Text"]})
                continue

            if set(content) != {"Type", "File"} or not isinstance(content.get("File"), dict):
                raise WorkbenchFileOwnershipError("invalid file content", 400)
            file_info = content["File"]
            if set(file_info) != {"WorkbenchFileId"}:
                raise WorkbenchFileOwnershipError("untrusted file locator", 400)
            file_id = file_info.get("WorkbenchFileId")
            if (
                not isinstance(file_id, str)
                or not file_id.startswith("wf_")
                or len(file_id) > 64
            ):
                raise WorkbenchFileOwnershipError("invalid WorkbenchFileId", 400)
            binding, locator = await cls.resolve_uploaded_file(
                db,
                file_id=file_id,
                account_id=account_id,
                identity=identity,
                app_context=app_context,
                conversation_id=conversation_id,
            )
            try:
                trusted_url = WorkbenchSecureFilePipeline.presign_private_locator(locator)
            except WorkbenchSecureFileError as error:
                raise WorkbenchFileOwnershipError(str(error), error.status_code) from error
            prepared.append(
                {
                    "Type": "file",
                    "File": {
                        "FileName": binding.FileName,
                        "FileSize": str(binding.FileSize),
                        "FileUrl": trusted_url,
                        "FileType": binding.FileType,
                        "Url": trusted_url,
                    },
                }
            )
        return prepared

    @classmethod
    def _reject_untrusted_locator(cls, value: Any) -> None:
        stack = [(value, 0)]
        visited = 0
        while stack:
            current, depth = stack.pop()
            visited += 1
            if depth > 32 or visited > 4096:
                raise WorkbenchFileOwnershipError("workbench content is too deeply nested", 400)
            if isinstance(current, list):
                stack.extend((item, depth + 1) for item in current)
                continue
            if not isinstance(current, dict):
                continue
            for key, child in current.items():
                normalized = str(key).replace("_", "").lower()
                if (
                    normalized == "customvariables"
                    or normalized in cls._LOCATOR_KEYS
                    or normalized.endswith("url")
                ):
                    raise WorkbenchFileOwnershipError("untrusted content locator", 400)
                stack.append((child, depth + 1))
