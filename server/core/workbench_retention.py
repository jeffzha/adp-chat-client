import hashlib
import hmac
import json as std_json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, update

from model.chat import ChatConversation, ChatRecord, SharedConversation
from model.workbench import (
    WorkbenchAgentBinding,
    WorkbenchBrowserSession,
    WorkbenchConversationWorkspace,
    WorkbenchFileBinding,
    WorkbenchFileWorkspace,
    WorkbenchIdentity,
    WorkbenchResourceOutbox,
    WorkbenchRuntimeLease,
    WorkbenchTurn,
    WorkbenchTurnCancellation,
    WorkbenchTurnEvidence,
    WorkbenchTurnEvent,
    WorkbenchTurnUsageDatum,
    WorkbenchWorkspace,
)
from model.workbench_integration import (
    WorkbenchConnectorCredential,
    WorkbenchConnectorScope,
    WorkbenchIntegrationAudit,
    WorkbenchIntegrationBinding,
    WorkbenchOAuthRevocation,
    WorkbenchOAuthState,
)
from model.workbench_retention import WorkbenchRetentionReceipt
from model.workbench_sandbox import WorkbenchSandbox, WorkbenchSandboxAudit, WorkbenchSandboxPty
from model.workbench_sandbox_acceptance import WorkbenchSandboxAcceptanceEvent
from model.workbench_scheduled import (
    WorkbenchScheduledAudit,
    WorkbenchScheduledDelegation,
    WorkbenchScheduledRun,
    WorkbenchScheduledTask,
)
from core.workbench_file_ownership import WorkbenchFileOwnership
from core.workbench_secure_file import PrivateCosStorage, WorkbenchSecureFileError


class WorkbenchRetentionError(RuntimeError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class CoreWorkbenchRetention:
    _ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
    _file_storage_factory = PrivateCosStorage

    @classmethod
    def parse_intent(cls, payload: Any) -> tuple[dict[str, Any], str]:
        required = {"intent_id", "customer_id", "policy_version", "cutoff_at", "legal_hold"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise WorkbenchRetentionError("retention intent schema is invalid", 400)
        intent_id = payload.get("intent_id")
        customer_id = payload.get("customer_id")
        policy_version = payload.get("policy_version")
        legal_hold = payload.get("legal_hold")
        if (
            not isinstance(intent_id, str)
            or cls._ID.fullmatch(intent_id) is None
            or isinstance(customer_id, bool)
            or not isinstance(customer_id, int)
            or customer_id <= 0
            or isinstance(policy_version, bool)
            or not isinstance(policy_version, int)
            or policy_version <= 0
            or legal_hold is not False
        ):
            raise WorkbenchRetentionError("retention intent is invalid or under legal hold", 409)
        cutoff_text = payload.get("cutoff_at")
        if not isinstance(cutoff_text, str) or len(cutoff_text) > 64:
            raise WorkbenchRetentionError("retention cutoff is invalid", 400)
        try:
            cutoff = datetime.fromisoformat(cutoff_text.replace("Z", "+00:00"))
        except ValueError as error:
            raise WorkbenchRetentionError("retention cutoff is invalid", 400) from error
        if cutoff.tzinfo is None:
            raise WorkbenchRetentionError("retention cutoff must include a timezone", 400)
        normalized = {
            "intent_id": intent_id,
            "customer_id": customer_id,
            "policy_version": policy_version,
            "cutoff_at": cutoff.astimezone(timezone.utc).replace(tzinfo=None),
            "legal_hold": False,
        }
        digest_payload = {
            **normalized,
            "cutoff_at": normalized["cutoff_at"].isoformat(timespec="microseconds") + "Z",
        }
        digest = hashlib.sha256(
            std_json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return normalized, digest

    @staticmethod
    def verify_signed_request(
        method: str,
        path: str,
        body: bytes,
        headers: Any,
        secret: str,
        allowed_skew_seconds: int,
        now: int | None = None,
    ) -> str:
        nonce = str(headers.get("X-Workbench-Nonce", "")).strip()
        timestamp = str(headers.get("X-Workbench-Timestamp", "")).strip()
        signature = str(headers.get("X-Workbench-Signature", "")).strip().lower()
        try:
            signed_at = int(timestamp)
        except ValueError as error:
            raise WorkbenchRetentionError("valid retention signature is required", 401) from error
        if (
            headers.get("X-Workbench-Contract-Version") != "2"
            or headers.get("X-Workbench-Service") != "claw-control"
            or path != "/api/internal/workbench/retention/intents"
            or method != "POST"
            or not nonce
            or len(nonce) > 96
            or abs((int(time.time()) if now is None else now) - signed_at)
            > allowed_skew_seconds
            or not signature
            or len(secret) < 32
        ):
            raise WorkbenchRetentionError("valid retention signature is required", 401)
        body_hash = hashlib.sha256(body).hexdigest()
        canonical = "\n".join(("2", method, path, timestamp, nonce, body_hash))
        expected = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise WorkbenchRetentionError("valid retention signature is required", 401)
        return nonce

    @staticmethod
    def _receipt_view(receipt: WorkbenchRetentionReceipt) -> dict[str, Any]:
        return {
            "receipt_id": receipt.ReceiptId,
            "intent_id": receipt.IntentId,
            "status": receipt.Status,
            "counts": std_json.loads(receipt.CountsJson),
        }

    @classmethod
    async def execute(cls, db, payload: Any) -> dict[str, Any]:
        intent, request_digest = cls.parse_intent(payload)
        existing = (
            await db.execute(
                select(WorkbenchRetentionReceipt)
                .where(WorkbenchRetentionReceipt.IntentId == intent["intent_id"])
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.RequestDigest != request_digest:
                raise WorkbenchRetentionError("retention intent replay payload changed", 409)
            return cls._receipt_view(existing)

        customer_id = intent["customer_id"]
        credentials = list(
            (
                await db.execute(
                    select(WorkbenchConnectorCredential).where(
                        WorkbenchConnectorCredential.CustomerId == customer_id
                    )
                )
            ).scalars()
        )
        revocations = list(
            (
                await db.execute(
                    select(WorkbenchOAuthRevocation).where(
                        WorkbenchOAuthRevocation.CustomerId == customer_id
                    )
                )
            ).scalars()
        )
        if any(
            credential.Status != "revoked"
            or credential.RevocationStatus != "provider_revoked"
            for credential in credentials
        ) or any(revocation.Status != "completed" for revocation in revocations):
            raise WorkbenchRetentionError(
                "OAuth provider revocation is not durably completed", 409
            )
        binding_ids = list(
            (
                await db.execute(
                    select(WorkbenchIdentity.BindingId).where(
                        WorkbenchIdentity.CustomerId == customer_id
                    )
                )
            ).scalars()
        )
        conversation_ids = list(
            (
                await db.execute(
                    select(WorkbenchConversationWorkspace.ConversationId).where(
                        WorkbenchConversationWorkspace.CustomerId == customer_id
                    )
                )
            ).scalars()
        )
        file_ids = list(
            (
                await db.execute(
                    select(WorkbenchFileWorkspace.FileId).where(
                        WorkbenchFileWorkspace.CustomerId == customer_id
                    )
                )
            ).scalars()
        )
        file_bindings = []
        if file_ids:
            file_bindings = list(
                (
                    await db.execute(
                        select(WorkbenchFileBinding).where(
                            WorkbenchFileBinding.FileId.in_(file_ids)
                        )
                    )
                ).scalars()
            )
        turn_ids = list(
            (
                await db.execute(
                    select(WorkbenchTurn.TurnId).where(WorkbenchTurn.CustomerId == customer_id)
                )
            ).scalars()
        )
        task_ids = list(
            (
                await db.execute(
                    select(WorkbenchScheduledTask.TaskId).where(
                        WorkbenchScheduledTask.CustomerId == customer_id
                    )
                )
            ).scalars()
        )
        sandbox_ids = list(
            (
                await db.execute(
                    select(WorkbenchSandbox.SandboxId).where(
                        WorkbenchSandbox.CustomerId == customer_id
                    )
                )
            ).scalars()
        )

        counts: dict[str, int] = {}

        if file_bindings:
            try:
                storage = cls._file_storage_factory()
                for binding in file_bindings:
                    locator = WorkbenchFileOwnership._decrypt_locator(
                        binding.LocatorCiphertext
                    )
                    if not isinstance(locator, dict) or set(locator) != {
                        "Storage",
                        "ObjectKey",
                        "Bucket",
                        "Region",
                        "ContentSha256",
                    }:
                        raise WorkbenchRetentionError(
                            "stored retention file locator is invalid", 500
                        )
                    locator = {
                        key: str(locator.get(key) or "").strip() for key in locator
                    }
                    if (
                        locator["Storage"] != "private_cos_v1"
                        or not locator["ObjectKey"]
                        or not locator["Bucket"]
                        or not locator["Region"]
                        or WorkbenchFileOwnership._digest(locator["ObjectKey"])
                        != binding.CosUrlHash
                        or WorkbenchFileOwnership._digest(locator["Bucket"])
                        != binding.CosBucketHash
                        or locator["ContentSha256"] != binding.FileUrlHash
                    ):
                        raise WorkbenchRetentionError(
                            "stored retention file locator failed integrity check", 500
                        )
                    await storage.delete(
                        locator["ObjectKey"],
                        bucket=locator["Bucket"],
                        region=locator["Region"],
                    )
            except WorkbenchRetentionError:
                raise
            except Exception as error:
                raise WorkbenchRetentionError(
                    "private file retention cleanup is unavailable", 503
                ) from error
        counts["private_cos_objects_deleted"] = len(file_bindings)

        async def remove(name: str, model, predicate) -> None:
            result = await db.execute(delete(model).where(predicate))
            counts[name] = max(0, int(result.rowcount or 0))

        if turn_ids:
            await remove("turn_events", WorkbenchTurnEvent, WorkbenchTurnEvent.TurnId.in_(turn_ids))
            await remove("turn_usage", WorkbenchTurnUsageDatum, WorkbenchTurnUsageDatum.TurnId.in_(turn_ids))
            await remove("turn_evidence", WorkbenchTurnEvidence, WorkbenchTurnEvidence.TurnId.in_(turn_ids))
            await remove("turn_cancellations", WorkbenchTurnCancellation, WorkbenchTurnCancellation.TurnId.in_(turn_ids))
        await remove("turns", WorkbenchTurn, WorkbenchTurn.CustomerId == customer_id)

        if task_ids:
            await remove("scheduled_delegations", WorkbenchScheduledDelegation, WorkbenchScheduledDelegation.TaskId.in_(task_ids))
            await remove("scheduled_runs", WorkbenchScheduledRun, WorkbenchScheduledRun.TaskId.in_(task_ids))
        await remove("scheduled_audits", WorkbenchScheduledAudit, WorkbenchScheduledAudit.CustomerId == customer_id)
        await remove("scheduled_tasks", WorkbenchScheduledTask, WorkbenchScheduledTask.CustomerId == customer_id)

        if sandbox_ids:
            await remove("sandbox_ptys", WorkbenchSandboxPty, WorkbenchSandboxPty.SandboxId.in_(sandbox_ids))
            await remove("sandbox_acceptance", WorkbenchSandboxAcceptanceEvent, WorkbenchSandboxAcceptanceEvent.SandboxId.in_(sandbox_ids))
        await remove("sandbox_audits", WorkbenchSandboxAudit, WorkbenchSandboxAudit.CustomerId == customer_id)
        await remove("sandboxes", WorkbenchSandbox, WorkbenchSandbox.CustomerId == customer_id)

        await remove("oauth_states", WorkbenchOAuthState, WorkbenchOAuthState.CustomerId == customer_id)
        await remove("connector_credentials", WorkbenchConnectorCredential, WorkbenchConnectorCredential.CustomerId == customer_id)
        await remove("connector_scopes", WorkbenchConnectorScope, WorkbenchConnectorScope.CustomerId == customer_id)
        await remove("integration_bindings", WorkbenchIntegrationBinding, WorkbenchIntegrationBinding.CustomerId == customer_id)
        await remove("integration_audits", WorkbenchIntegrationAudit, WorkbenchIntegrationAudit.CustomerId == customer_id)
        await remove(
            "oauth_revocations",
            WorkbenchOAuthRevocation,
            WorkbenchOAuthRevocation.CustomerId == customer_id,
        )

        if file_ids:
            archived = await db.execute(
                update(WorkbenchFileBinding)
                .where(WorkbenchFileBinding.FileId.in_(file_ids))
                .values(Status="archived", LocatorCiphertext="")
            )
            counts["file_bindings_archived"] = max(0, int(archived.rowcount or 0))
        else:
            counts["file_bindings_archived"] = 0
        await remove("file_workspaces", WorkbenchFileWorkspace, WorkbenchFileWorkspace.CustomerId == customer_id)
        await remove("conversation_workspaces", WorkbenchConversationWorkspace, WorkbenchConversationWorkspace.CustomerId == customer_id)
        await remove("workspaces", WorkbenchWorkspace, WorkbenchWorkspace.CustomerId == customer_id)

        if conversation_ids:
            await remove("chat_records", ChatRecord, ChatRecord.ConversationId.in_(conversation_ids))
            await remove("shared_conversations", SharedConversation, SharedConversation.ParentConversationId.in_(conversation_ids))
            await remove("conversations", ChatConversation, ChatConversation.Id.in_(conversation_ids))
        else:
            counts.update({"chat_records": 0, "shared_conversations": 0, "conversations": 0})

        await remove("browser_sessions", WorkbenchBrowserSession, WorkbenchBrowserSession.CustomerId == customer_id)
        await remove("runtime_leases", WorkbenchRuntimeLease, WorkbenchRuntimeLease.CustomerId == customer_id)
        await remove("resource_outbox", WorkbenchResourceOutbox, WorkbenchResourceOutbox.CustomerId == customer_id)
        if binding_ids:
            await remove("agent_bindings", WorkbenchAgentBinding, WorkbenchAgentBinding.BindingId.in_(binding_ids))
        else:
            counts["agent_bindings"] = 0
        archived_identities = await db.execute(
            update(WorkbenchIdentity)
            .where(WorkbenchIdentity.CustomerId == customer_id)
            .values(Status="archived", AuthEpoch=WorkbenchIdentity.AuthEpoch + 1)
        )
        counts["identities_archived"] = max(0, int(archived_identities.rowcount or 0))

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        receipt = WorkbenchRetentionReceipt(
            ReceiptId="rtr_" + uuid.uuid4().hex,
            IntentId=intent["intent_id"],
            RequestDigest=request_digest,
            CustomerId=customer_id,
            PolicyVersion=intent["policy_version"],
            CutoffAt=intent["cutoff_at"],
            Status="completed",
            CountsJson=std_json.dumps(counts, sort_keys=True, separators=(",", ":")),
            CompletedAt=now,
        )
        db.add(receipt)
        await db.commit()
        return cls._receipt_view(receipt)
