import base64
import binascii
import hashlib
import hmac
import json
import math
import uuid
from datetime import UTC, datetime
from typing import Any

from jwcrypto import jwe, jwk
from jwcrypto.common import base64url_encode
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import tagentic_config
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from model.workbench import (
    WorkbenchTurn,
    WorkbenchTurnEvidence,
    WorkbenchTurnUsageDatum,
)


class WorkbenchUsageTelemetryError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class WorkbenchUsageTelemetry:
    """Persist provider completion evidence without treating telemetry as billing."""

    SOURCE = "adp.sse.response.completed"
    MAX_EVIDENCE_BYTES = 512 * 1024
    _METRIC_FIELDS = (
        "Elapsed",
        "StartTime",
        "InputTokens",
        "OutputTokens",
        "TotalTokens",
        "ModelName",
        "FirstTokenCost",
        "TotalCost",
        "CacheReadTokens",
        "CacheWriteTokens",
        "CachedTokens",
        "CacheCreationTokens",
        "CacheInputTokens",
    )
    _CACHE_FIELDS = frozenset(
        {
            "CacheReadTokens",
            "CacheWriteTokens",
            "CachedTokens",
            "CacheCreationTokens",
            "CacheInputTokens",
        }
    )

    @staticmethod
    def _key() -> tuple[jwk.JWK, str]:
        encoded = str(tagentic_config.WORKBENCH_USAGE_EVIDENCE_KEY or "").strip()
        key_id = str(tagentic_config.WORKBENCH_USAGE_EVIDENCE_KEY_ID or "").strip()
        if not encoded or not key_id or len(key_id) > 64:
            raise WorkbenchUsageTelemetryError(
                "workbench usage evidence encryption is not configured",
                503,
            )
        try:
            key_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise WorkbenchUsageTelemetryError(
                "workbench usage evidence encryption is not configured",
                503,
            ) from error
        if len(key_bytes) != 32:
            raise WorkbenchUsageTelemetryError(
                "workbench usage evidence encryption is not configured",
                503,
            )
        return (
            jwk.JWK(kty="oct", k=base64url_encode(key_bytes), kid=key_id),
            key_id,
        )

    @classmethod
    def _encrypt(cls, plaintext: bytes) -> tuple[str, str]:
        key, key_id = cls._key()
        token = jwe.JWE(
            plaintext,
            protected={
                "alg": "A256KW",
                "enc": "A256GCM",
                "kid": key_id,
                "typ": "workbench-usage-evidence+jwe",
            },
        )
        token.add_recipient(key)
        return token.serialize(compact=True), key_id

    @classmethod
    def decrypt_for_audit(cls, ciphertext: str, expected_sha256: str) -> dict[str, Any]:
        """Decrypt and verify one evidence object for a future privileged audit API."""

        key, _ = cls._key()
        token = jwe.JWE()
        try:
            token.deserialize(ciphertext, key=key)
            plaintext = bytes(token.payload)
        except Exception as error:
            raise WorkbenchUsageTelemetryError("usage evidence cannot be decrypted", 409) from error
        if not hmac.compare_digest(
            hashlib.sha256(plaintext).hexdigest(),
            str(expected_sha256 or ""),
        ):
            raise WorkbenchUsageTelemetryError("usage evidence integrity check failed", 409)
        try:
            payload = json.loads(plaintext)
        except (TypeError, ValueError) as error:
            raise WorkbenchUsageTelemetryError("usage evidence is invalid", 409) from error
        if not isinstance(payload, dict):
            raise WorkbenchUsageTelemetryError("usage evidence is invalid", 409)
        return payload

    @staticmethod
    def _identifier(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not value:
            return None
        return value[:255]

    @classmethod
    def _metric_projection(cls, stat: dict[str, Any]) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        for name in cls._METRIC_FIELDS:
            value = stat.get(name)
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, float) and not math.isfinite(value):
                continue
            if isinstance(value, (int, float)):
                metrics[name] = value
            elif isinstance(value, str) and len(value) <= 256:
                metrics[name] = value
        metrics["CacheTokensStatus"] = (
            "reported" if any(name in metrics for name in cls._CACHE_FIELDS) else "unknown"
        )
        if "FirstTokenCost" in metrics:
            metrics["FirstTokenCostMeaning"] = "first_token_latency"
        if "TotalCost" in metrics:
            metrics["TotalCostMeaning"] = "model_total_latency"
        metrics["AggregationPolicy"] = "non_additive"
        return metrics

    @classmethod
    def _usage_rows(
        cls,
        payload: dict[str, Any],
        *,
        application_id: str,
    ) -> list[dict[str, Any]]:
        response = payload.get("Response")
        if not isinstance(response, dict):
            return []
        rows: list[dict[str, Any]] = []

        def append(
            stat: Any,
            path: str,
            *,
            workflow_run_id: str | None = None,
            node_id: str | None = None,
            stat_index: int = 0,
        ) -> None:
            if not isinstance(stat, dict):
                return
            stable_key = None
            confidence = "unknown"
            if workflow_run_id and node_id:
                stable_key = hashlib.sha256(
                    (
                        f"{application_id}\0{workflow_run_id}\0{node_id}\0{stat_index}"
                    ).encode("utf-8")
                ).hexdigest()
                confidence = "strong"
            rows.append(
                {
                    "path": path,
                    "metrics": cls._metric_projection(stat),
                    "stable_key": stable_key,
                    "confidence": confidence,
                }
            )

        append(response.get("StatInfo"), "$.Response.StatInfo")
        procedures = response.get("Procedures")
        if not isinstance(procedures, list):
            return rows
        for procedure_index, procedure in enumerate(procedures):
            if not isinstance(procedure, dict):
                continue
            stat_infos = procedure.get("StatInfos")
            if isinstance(stat_infos, list):
                for stat_index, stat in enumerate(stat_infos):
                    append(
                        stat,
                        f"$.Response.Procedures[{procedure_index}].StatInfos[{stat_index}]",
                    )
            agent = procedure.get("Agent")
            if isinstance(agent, dict) and isinstance(agent.get("StatInfos"), list):
                for stat_index, stat in enumerate(agent["StatInfos"]):
                    append(
                        stat,
                        (
                            f"$.Response.Procedures[{procedure_index}].Agent."
                            f"StatInfos[{stat_index}]"
                        ),
                    )
            workflow = procedure.get("Workflow")
            if not isinstance(workflow, dict):
                continue
            workflow_run_id = cls._identifier(workflow.get("WorkflowRunId"))
            run_nodes = workflow.get("RunNodes")
            if not isinstance(run_nodes, list):
                continue
            for node_index, node in enumerate(run_nodes):
                if not isinstance(node, dict):
                    continue
                node_id = cls._identifier(node.get("NodeId"))
                node_stats = node.get("StatInfos")
                if isinstance(node_stats, list):
                    for stat_index, stat in enumerate(node_stats):
                        append(
                            stat,
                            (
                                f"$.Response.Procedures[{procedure_index}].Workflow."
                                f"RunNodes[{node_index}].StatInfos[{stat_index}]"
                            ),
                            workflow_run_id=workflow_run_id,
                            node_id=node_id,
                            stat_index=stat_index,
                        )
                elif isinstance(node.get("StatInfo"), dict):
                    append(
                        node["StatInfo"],
                        (
                            f"$.Response.Procedures[{procedure_index}].Workflow."
                            f"RunNodes[{node_index}].StatInfo"
                        ),
                        workflow_run_id=workflow_run_id,
                        node_id=node_id,
                    )
        return rows

    @classmethod
    async def capture_completed_event(
        cls,
        db: AsyncSession,
        *,
        turn_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        payload: dict[str, Any],
    ) -> str:
        if not isinstance(payload, dict) or payload.get("Type") != "response.completed":
            raise WorkbenchUsageTelemetryError("provider completion evidence is invalid")
        try:
            plaintext = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise WorkbenchUsageTelemetryError(
                "provider completion evidence is invalid"
            ) from error
        if not plaintext or len(plaintext) > cls.MAX_EVIDENCE_BYTES:
            raise WorkbenchUsageTelemetryError("provider completion evidence exceeds the limit")
        evidence_hash = hashlib.sha256(plaintext).hexdigest()

        turn = (
            await db.execute(
                select(WorkbenchTurn).where(
                    WorkbenchTurn.TurnId == turn_id,
                    WorkbenchTurn.BindingId == identity.binding_id,
                    WorkbenchTurn.AccountId == account_id,
                    WorkbenchTurn.CustomerId == identity.customer_id,
                    WorkbenchTurn.ApplicationId == app_context.application_id,
                )
            )
        ).scalar()
        if turn is None:
            raise WorkbenchUsageTelemetryError("workbench Turn ownership changed", 409)

        existing = (
            await db.execute(
                select(WorkbenchTurnEvidence).where(WorkbenchTurnEvidence.TurnId == turn_id)
            )
        ).scalar()
        if existing is not None:
            if existing.EvidenceSha256 != evidence_hash:
                raise WorkbenchUsageTelemetryError(
                    "workbench Turn has conflicting completion evidence",
                    409,
                )
            return existing.EvidenceId

        ciphertext, key_id = cls._encrypt(plaintext)
        response = payload.get("Response")
        response = response if isinstance(response, dict) else {}
        extra_info = response.get("ExtraInfo")
        extra_info = extra_info if isinstance(extra_info, dict) else {}
        usage_rows = cls._usage_rows(payload, application_id=app_context.application_id)
        evidence_id = f"wte_{uuid.uuid4().hex}"
        evidence = WorkbenchTurnEvidence(
            EvidenceId=evidence_id,
            TurnId=turn_id,
            BindingId=identity.binding_id,
            AccountId=account_id,
            CustomerId=identity.customer_id,
            ApplicationId=app_context.application_id,
            AppProfileId=str(app_context.app_profile_id),
            ConfigVersion=app_context.config_version,
            Source=cls.SOURCE,
            EventType="response.completed",
            EvidenceSha256=evidence_hash,
            EvidenceCiphertext=ciphertext,
            EncryptionKeyId=key_id,
            ProviderRequestId=cls._identifier(
                extra_info.get("RequestId")
                or response.get("RequestId")
                or payload.get("RequestId")
            ),
            ProviderTraceId=cls._identifier(
                extra_info.get("TraceId")
                or response.get("TraceId")
                or payload.get("TraceId")
            ),
            ProviderRecordId=cls._identifier(
                response.get("RecordId") or payload.get("RecordId")
            ),
            ConversationId=cls._identifier(
                response.get("ConversationId") or payload.get("ConversationId")
            ),
            DedupeConfidence=(
                "strong"
                if usage_rows and all(row["confidence"] == "strong" for row in usage_rows)
                else "unknown"
            ),
            ObservedAt=datetime.now(UTC).replace(tzinfo=None),
        )
        db.add(evidence)
        for row in usage_rows:
            db.add(
                WorkbenchTurnUsageDatum(
                    UsageId=f"wtu_{uuid.uuid4().hex}",
                    EvidenceId=evidence_id,
                    TurnId=turn_id,
                    Source=cls.SOURCE,
                    SourcePath=row["path"],
                    EvidenceSha256=evidence_hash,
                    StableUsageKey=row["stable_key"],
                    DedupeConfidence=row["confidence"],
                    MetricsJson=json.dumps(
                        row["metrics"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                )
            )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = (
                await db.execute(
                    select(WorkbenchTurnEvidence).where(
                        WorkbenchTurnEvidence.TurnId == turn_id
                    )
                )
            ).scalar()
            if existing is None or existing.EvidenceSha256 != evidence_hash:
                raise WorkbenchUsageTelemetryError(
                    "workbench Turn has conflicting completion evidence",
                    409,
                )
            return existing.EvidenceId
        return evidence_id
