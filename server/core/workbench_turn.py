import asyncio
import hashlib
import json
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, AsyncIterator, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import tagentic_config
from core.agent import CoreAgent
from core.chat import CoreChat
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_control_events import CACHE_INVALIDATE, SESSION_REVOKE, WorkbenchControlEvent
from core.workbench_runtime import RuntimeLease, WorkbenchRuntimeGuard
from core.workbench_secure_file import WorkbenchSecureFilePipeline
from core.workbench_stream import WorkbenchStreamGuard
from core.workbench_usage_telemetry import WorkbenchUsageTelemetry
from core.workbench_metrics import WORKBENCH_METRICS, evidence_failure_reason
from model.workbench import (
    WorkbenchAgentBinding,
    WorkbenchTurn,
    WorkbenchTurnCancellation,
    WorkbenchTurnEvent,
)
from util.database import db_connection
from vendor.interface import BaseVendor


class WorkbenchTurnError(RuntimeError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class WorkbenchTurnSubmission:
    turn_id: str
    client_request_id: str
    created: bool


@dataclass(frozen=True)
class WorkbenchTurnCancellationResult:
    turn_id: str
    status: str
    provider_cancel_supported: bool = False


@dataclass(frozen=True)
class _LiveTurnContext:
    task: asyncio.Task
    identity: WorkbenchIdentityContext
    app_context: WorkbenchAppContext
    claims: dict[str, Any]
    method: str
    resource_path: str


class WorkbenchTurnManager:
    """Durable, idempotent execution and replay for workbench chat Turns."""

    TERMINAL_STATUSES = frozenset(
        {
            "completed",
            "failed_before_accept",
            "failed_after_accept",
            "cancel_confirmed",
            "provider_unknown",
        }
    )
    NON_TERMINAL_STATUSES = frozenset({"submitted", "running", "cancel_requested"})
    MAX_PERSISTED_EVENTS = 2000
    MAX_EVENT_BYTES = 256 * 1024
    MAX_TOTAL_EVENT_BYTES = 16 * 1024 * 1024
    TERMINAL_EVENT_RESERVE_BYTES = 1024
    REPLAY_BATCH_SIZE = 100
    REPLAY_POLL_SECONDS = 1.0

    instance_id = (
        str(tagentic_config.WORKBENCH_INSTANCE_ID or "").strip()
        or str(os.getenv("WORKBENCH_INSTANCE_ID") or "").strip()
        or str(os.getenv("HOSTNAME") or os.getenv("COMPUTERNAME") or "").strip()
        or socket.gethostname()
    )[:128]
    lifecycle_id = uuid.uuid4().hex
    _tasks: set[asyncio.Task] = set()
    _live_turns: dict[asyncio.Task, _LiveTurnContext] = {}
    _subscribers: dict[asyncio.Task, _LiveTurnContext] = {}
    _event_signals: dict[str, asyncio.Event] = {}
    _replay_subscriber_counts: dict[str, int] = {}

    @classmethod
    def _notify_replay_subscribers(cls, turn_id: str) -> None:
        signal = cls._event_signals.get(turn_id)
        if signal is not None:
            signal.set()

    @staticmethod
    def normalize_client_request_id(value: Any) -> str:
        if value in (None, ""):
            return str(uuid.uuid4())
        try:
            return str(uuid.UUID(str(value)))
        except (ValueError, AttributeError, TypeError) as error:
            raise WorkbenchTurnError("ClientRequestId must be a UUID", 400) from error

    @staticmethod
    def request_digest(payload: dict[str, Any]) -> str:
        try:
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise WorkbenchTurnError("workbench chat request is not canonicalizable", 400) from error
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _event_data(payload: dict[str, Any]) -> str:
        return "data: " + json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n\n"

    @staticmethod
    def _completed_payload(event_data: str) -> dict[str, Any] | None:
        data_lines = []
        for line in event_data.replace("\r\n", "\n").split("\n"):
            if line == "data":
                data_lines.append("")
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        if not data_lines:
            return None
        try:
            payload = json.loads("\n".join(data_lines))
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("Type") != "response.completed":
            return None
        return payload

    @classmethod
    def _turn_metadata_event(cls, turn: WorkbenchTurn) -> str:
        return cls._event_data(
            {
                "Type": "workbench.turn",
                "TurnId": turn.TurnId,
                "ClientRequestId": turn.ClientRequestId,
                "Status": turn.Status,
            }
        )

    @classmethod
    def _terminal_event(cls, turn: WorkbenchTurn) -> str:
        payload = {
            "Type": "workbench.turn_status",
            "TurnId": turn.TurnId,
            "Status": turn.Status,
        }
        if turn.ErrorCode:
            payload["ErrorCode"] = turn.ErrorCode
        return cls._event_data(payload)

    @classmethod
    async def create_or_get(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        client_request_id: str,
        request_digest: str,
        conversation_id: str | None,
    ) -> WorkbenchTurnSubmission:
        client_request_id = cls.normalize_client_request_id(client_request_id)
        persistence_started_at = time.perf_counter()
        existing = (
            await db.execute(
                select(WorkbenchTurn).where(
                    WorkbenchTurn.BindingId == identity.binding_id,
                    WorkbenchTurn.ApplicationId == app_context.application_id,
                    WorkbenchTurn.ClientRequestId == client_request_id,
                )
            )
        ).scalar()
        if existing is not None:
            if existing.RequestDigest != request_digest:
                raise WorkbenchTurnError(
                    "ClientRequestId is already bound to a different request",
                    409,
                )
            return WorkbenchTurnSubmission(existing.TurnId, client_request_id, False)

        # Integration mutations use this exact per-Agent database row and lock
        # order. Holding it until the submitted Turn commits makes the Turn
        # visible before any SkillList mutation can pass its post-lock check.
        agent_binding = (
            await db.execute(
                select(WorkbenchAgentBinding)
                .where(
                    WorkbenchAgentBinding.BindingId == identity.binding_id,
                    WorkbenchAgentBinding.AccountId == account_id,
                    WorkbenchAgentBinding.ApplicationId == app_context.application_id,
                )
                .with_for_update()
            )
        ).scalar()
        if agent_binding is not None and (
            agent_binding.Status != "active" or not agent_binding.AgentId
        ):
            raise WorkbenchTurnError(
                "workbench Agent is being reconfigured; retry Turn submission",
                409,
            )

        turn = WorkbenchTurn(
            TurnId=f"wt_{uuid.uuid4().hex}",
            BindingId=identity.binding_id,
            AccountId=account_id,
            CustomerId=identity.customer_id,
            ApplicationId=app_context.application_id,
            ClientRequestId=client_request_id,
            RequestDigest=request_digest,
            Status="submitted",
            ConversationId=conversation_id or None,
            OwnerInstanceId=cls.instance_id,
            OwnerLifecycleId=cls.lifecycle_id,
            EventCount=1,
            EventBytes=0,
        )
        metadata = cls._turn_metadata_event(turn)
        turn.EventBytes = len(metadata.encode("utf-8"))
        db.add(turn)
        db.add(
            WorkbenchTurnEvent(
                TurnId=turn.TurnId,
                Sequence=1,
                EventData=metadata,
                EventBytes=turn.EventBytes,
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = (
                await db.execute(
                    select(WorkbenchTurn).where(
                        WorkbenchTurn.BindingId == identity.binding_id,
                        WorkbenchTurn.ApplicationId == app_context.application_id,
                        WorkbenchTurn.ClientRequestId == client_request_id,
                    )
                )
            ).scalar()
            if existing is None:
                raise WorkbenchTurnError("workbench Turn could not be created", 503)
            if existing.RequestDigest != request_digest:
                raise WorkbenchTurnError(
                    "ClientRequestId is already bound to a different request",
                    409,
                )
            return WorkbenchTurnSubmission(existing.TurnId, client_request_id, False)
        WORKBENCH_METRICS.record_turn_event_commit(
            1,
            max(0.0, time.perf_counter() - persistence_started_at),
        )
        return WorkbenchTurnSubmission(turn.TurnId, client_request_id, True)

    @classmethod
    async def get_owned(
        cls,
        db: AsyncSession,
        *,
        turn_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> WorkbenchTurn | None:
        return (
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

    @classmethod
    async def _append_provider_event(cls, turn_id: str, data: Any) -> None:
        if isinstance(data, bytes):
            try:
                event_data = data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise WorkbenchTurnError("provider event is not valid UTF-8", 502) from error
        elif isinstance(data, str):
            event_data = data
        else:
            raise WorkbenchTurnError("provider event has an unsupported type", 502)
        raw_event_bytes = len(event_data.encode("utf-8"))
        if raw_event_bytes <= 0 or raw_event_bytes > cls.MAX_EVENT_BYTES:
            raise WorkbenchTurnError("provider event exceeds the persistence limit", 502)
        # Replay sequence numbers are the only resumable SSE IDs exposed to the
        # browser.  An upstream id field would otherwise override our prepended
        # id and make Last-Event-ID unusable or ambiguous after reconnect.
        event_data = "\n".join(
            line
            for line in event_data.split("\n")
            if line != "id" and not line.startswith("id:")
        )
        event_bytes = len(event_data.encode("utf-8"))
        if not event_data.strip() or event_bytes > cls.MAX_EVENT_BYTES:
            raise WorkbenchTurnError("provider event has no persistable data", 502)

        first_event_seconds = None
        persistence_started_at = time.perf_counter()
        async with db_connection() as db:
            turn = (
                await db.execute(
                    select(WorkbenchTurn)
                    .where(WorkbenchTurn.TurnId == turn_id)
                    .with_for_update()
                )
            ).scalar()
            if (
                turn is None
                or turn.Status not in cls.NON_TERMINAL_STATUSES
                or turn.OwnerInstanceId != cls.instance_id
                or turn.OwnerLifecycleId != cls.lifecycle_id
            ):
                raise WorkbenchTurnError("workbench Turn ownership changed", 409)
            if turn.EventCount >= cls.MAX_PERSISTED_EVENTS - 1:
                raise WorkbenchTurnError("workbench Turn event count limit reached", 502)
            if (
                turn.EventBytes + event_bytes
                > cls.MAX_TOTAL_EVENT_BYTES - cls.TERMINAL_EVENT_RESERVE_BYTES
            ):
                raise WorkbenchTurnError("workbench Turn event byte limit reached", 502)
            sequence = turn.EventCount + 1
            db.add(
                WorkbenchTurnEvent(
                    TurnId=turn_id,
                    Sequence=sequence,
                    EventData=event_data,
                    EventBytes=event_bytes,
                )
            )
            turn.EventCount = sequence
            turn.EventBytes += event_bytes
            if turn.AcceptedAt is None:
                accepted_at = datetime.now(UTC).replace(tzinfo=None)
                turn.AcceptedAt = accepted_at
                first_event_seconds = max(0.0, (accepted_at - turn.CreatedAt).total_seconds())
            if turn.Status != "cancel_requested":
                turn.Status = "running"
            db.add(turn)
            await db.commit()
        WORKBENCH_METRICS.record_turn_event_commit(
            1,
            max(0.0, time.perf_counter() - persistence_started_at),
        )
        if first_event_seconds is not None:
            WORKBENCH_METRICS.observe("workbench_first_event_seconds", first_event_seconds)
        cls._notify_replay_subscribers(turn_id)

    @classmethod
    async def _set_running(cls, turn_id: str) -> None:
        async with db_connection() as db:
            turn = (
                await db.execute(
                    select(WorkbenchTurn)
                    .where(WorkbenchTurn.TurnId == turn_id)
                    .with_for_update()
                )
            ).scalar()
            if (
                turn is None
                or turn.Status not in {"submitted", "cancel_requested"}
                or turn.OwnerInstanceId != cls.instance_id
                or turn.OwnerLifecycleId != cls.lifecycle_id
            ):
                raise WorkbenchTurnError("workbench Turn is not executable", 409)
            if turn.Status != "cancel_requested":
                turn.Status = "running"
            db.add(turn)
            await db.commit()

    @classmethod
    async def request_cancel(
        cls,
        db: AsyncSession,
        *,
        turn_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        reason_code: str = "user_stop",
    ) -> WorkbenchTurnCancellationResult:
        if reason_code != "user_stop":
            raise WorkbenchTurnError("unsupported cancellation reason", 400)
        persistence_started_at = time.perf_counter()
        turn = (
            await db.execute(
                select(WorkbenchTurn)
                .where(
                    WorkbenchTurn.TurnId == turn_id,
                    WorkbenchTurn.BindingId == identity.binding_id,
                    WorkbenchTurn.AccountId == account_id,
                    WorkbenchTurn.CustomerId == identity.customer_id,
                    WorkbenchTurn.ApplicationId == app_context.application_id,
                )
                .with_for_update()
            )
        ).scalar()
        if turn is None:
            raise WorkbenchTurnError("workbench Turn not found", 404)
        cancellation = (
            await db.execute(
                select(WorkbenchTurnCancellation).where(
                    WorkbenchTurnCancellation.TurnId == turn_id
                )
            )
        ).scalar()
        if turn.Status in cls.TERMINAL_STATUSES:
            if cancellation is not None:
                return WorkbenchTurnCancellationResult(turn_id, turn.Status)
            raise WorkbenchTurnError("workbench Turn is already terminal", 409)
        if turn.Status not in cls.NON_TERMINAL_STATUSES:
            raise WorkbenchTurnError("workbench Turn cannot be cancelled", 409)
        if cancellation is not None:
            return WorkbenchTurnCancellationResult(turn_id, turn.Status)
        if turn.EventCount >= cls.MAX_PERSISTED_EVENTS - 1:
            raise WorkbenchTurnError("workbench Turn event count limit reached", 409)

        now = datetime.now(UTC).replace(tzinfo=None)
        cancellation = WorkbenchTurnCancellation(
            CancellationId=f"wtc_{uuid.uuid4().hex}",
            TurnId=turn_id,
            BindingId=identity.binding_id,
            AccountId=account_id,
            CustomerId=identity.customer_id,
            ApplicationId=app_context.application_id,
            Status="cancel_requested",
            ReasonCode=reason_code,
            RequestedAt=now,
        )
        turn.Status = "cancel_requested"
        status_event = cls._terminal_event(turn)
        event_bytes = len(status_event.encode("utf-8"))
        if turn.EventBytes + event_bytes > cls.MAX_TOTAL_EVENT_BYTES:
            raise WorkbenchTurnError("workbench Turn event byte limit reached", 409)
        sequence = turn.EventCount + 1
        turn.EventCount = sequence
        turn.EventBytes += event_bytes
        db.add(cancellation)
        db.add(
            WorkbenchTurnEvent(
                TurnId=turn_id,
                Sequence=sequence,
                EventData=status_event,
                EventBytes=event_bytes,
            )
        )
        db.add(turn)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            cancellation = (
                await db.execute(
                    select(WorkbenchTurnCancellation).where(
                        WorkbenchTurnCancellation.TurnId == turn_id,
                        WorkbenchTurnCancellation.BindingId == identity.binding_id,
                        WorkbenchTurnCancellation.ApplicationId
                        == app_context.application_id,
                    )
                )
            ).scalar()
            if cancellation is None:
                raise WorkbenchTurnError("workbench Turn cancellation failed", 409)
            current = (
                await db.execute(
                    select(WorkbenchTurn).where(WorkbenchTurn.TurnId == turn_id)
                )
            ).scalar()
            status = current.Status if current is not None else cancellation.Status
            return WorkbenchTurnCancellationResult(turn_id, status)
        WORKBENCH_METRICS.record_turn_event_commit(
            1,
            max(0.0, time.perf_counter() - persistence_started_at),
        )
        cls._notify_replay_subscribers(turn_id)
        return WorkbenchTurnCancellationResult(turn_id, "cancel_requested")

    @classmethod
    async def confirm_cancel(
        cls,
        db: AsyncSession,
        *,
        turn_id: str,
        provider_evidence_sha256: str,
    ) -> WorkbenchTurnCancellationResult:
        evidence_hash = str(provider_evidence_sha256 or "").strip().lower()
        if len(evidence_hash) != 64 or any(char not in "0123456789abcdef" for char in evidence_hash):
            raise WorkbenchTurnError("provider cancellation evidence is invalid", 400)
        persistence_started_at = time.perf_counter()
        turn = (
            await db.execute(
                select(WorkbenchTurn)
                .where(WorkbenchTurn.TurnId == turn_id)
                .with_for_update()
            )
        ).scalar()
        cancellation = (
            await db.execute(
                select(WorkbenchTurnCancellation)
                .where(WorkbenchTurnCancellation.TurnId == turn_id)
                .with_for_update()
            )
        ).scalar()
        if turn is None or cancellation is None:
            raise WorkbenchTurnError("workbench Turn cancellation not found", 404)
        if cancellation.Status == "cancel_confirmed":
            if cancellation.ProviderEvidenceSha256 != evidence_hash:
                raise WorkbenchTurnError("provider cancellation evidence conflicts", 409)
            return WorkbenchTurnCancellationResult(turn_id, "cancel_confirmed")
        if turn.Status != "cancel_requested" or cancellation.Status != "cancel_requested":
            raise WorkbenchTurnError("workbench Turn cancellation cannot be confirmed", 409)

        cancellation.Status = "cancel_confirmed"
        cancellation.ProviderEvidenceSha256 = evidence_hash
        cancellation.ConfirmedAt = datetime.now(UTC).replace(tzinfo=None)
        turn.Status = "cancel_confirmed"
        turn.CompletedAt = cancellation.ConfirmedAt
        status_event = cls._terminal_event(turn)
        event_bytes = len(status_event.encode("utf-8"))
        sequence = turn.EventCount + 1
        turn.EventCount = sequence
        turn.EventBytes += event_bytes
        db.add(cancellation)
        db.add(
            WorkbenchTurnEvent(
                TurnId=turn_id,
                Sequence=sequence,
                EventData=status_event,
                EventBytes=event_bytes,
            )
        )
        db.add(turn)
        await db.commit()
        WORKBENCH_METRICS.record_turn_event_commit(
            1,
            max(0.0, time.perf_counter() - persistence_started_at),
        )
        WORKBENCH_METRICS.record_terminal(
            "cancel_confirmed",
            (turn.CompletedAt - turn.CreatedAt).total_seconds(),
        )
        cls._notify_replay_subscribers(turn_id)
        return WorkbenchTurnCancellationResult(turn_id, "cancel_confirmed")

    @classmethod
    async def _finalize(cls, turn_id: str, status: str, error_code: str | None = None) -> None:
        if status not in cls.TERMINAL_STATUSES:
            raise ValueError("invalid terminal workbench Turn status")
        persistence_started_at = time.perf_counter()
        duration_seconds = None
        async with db_connection() as db:
            turn = (
                await db.execute(
                    select(WorkbenchTurn)
                    .where(WorkbenchTurn.TurnId == turn_id)
                    .with_for_update()
                )
            ).scalar()
            if turn is None or turn.Status in cls.TERMINAL_STATUSES:
                return
            if (
                turn.OwnerInstanceId != cls.instance_id
                or turn.OwnerLifecycleId != cls.lifecycle_id
            ):
                return
            turn.Status = status
            turn.ErrorCode = str(error_code or "")[:64] or None
            completed_at = datetime.now(UTC).replace(tzinfo=None)
            turn.CompletedAt = completed_at
            duration_seconds = max(0.0, (completed_at - turn.CreatedAt).total_seconds())
            terminal = cls._terminal_event(turn)
            terminal_bytes = len(terminal.encode("utf-8"))
            sequence = turn.EventCount + 1
            db.add(
                WorkbenchTurnEvent(
                    TurnId=turn_id,
                    Sequence=sequence,
                    EventData=terminal,
                    EventBytes=terminal_bytes,
                )
            )
            turn.EventCount = sequence
            turn.EventBytes += terminal_bytes
            db.add(turn)
            await db.commit()
        if duration_seconds is not None:
            WORKBENCH_METRICS.record_turn_event_commit(
                1,
                max(0.0, time.perf_counter() - persistence_started_at),
            )
            WORKBENCH_METRICS.record_terminal(status, duration_seconds)
        cls._notify_replay_subscribers(turn_id)

    @classmethod
    async def execute(
        cls,
        *,
        turn_id: str,
        vendor_app: BaseVendor,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        claims: dict[str, Any],
        contents: list,
        conversation_id: str | None,
        search_network: bool,
        custom_variables: dict,
        limits: dict[str, int],
        offline_reauthorize: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        lease: RuntimeLease | None = None
        provider_started = False
        accepted = False
        completion_evidence_captured = False
        upstream: AsyncIterator[Any] | None = None
        event_buffer = ""
        private_urls = WorkbenchSecureFilePipeline.private_urls(contents)
        sensitive_values = tuple(
            value
            for value in (
                app_context.app_key,
                app_context.secret_id,
                app_context.secret_key,
            )
            if isinstance(value, str) and len(value) >= 4
        )
        try:
            lease = await WorkbenchRuntimeGuard.acquire(
                account_id=account_id,
                identity=identity,
                app_context=app_context,
                operation="chat",
            )
            async with db_connection() as db:
                agent = await CoreAgent.ensure_turn_limits(
                    db,
                    account_id,
                    app_context.application_id,
                    vendor_app,
                    max_output_tokens=limits["max_output_tokens"],
                    max_reasoning_rounds=limits["max_reasoning_rounds"],
                    identity_context=identity,
                )
            await cls._set_running(turn_id)

            async def capture_usage_evidence(payload: dict[str, Any]) -> None:
                nonlocal completion_evidence_captured
                try:
                    async with db_connection() as db:
                        await WorkbenchUsageTelemetry.capture_completed_event(
                            db,
                            turn_id=turn_id,
                            account_id=account_id,
                            identity=identity,
                            app_context=app_context,
                            payload=payload,
                        )
                except Exception as error:
                    WORKBENCH_METRICS.inc(
                        "workbench_usage_evidence_capture_failures_total",
                        reason=evidence_failure_reason(error),
                    )
                    raise
                completion_evidence_captured = True

            upstream = CoreChat.message(
                vendor_app,
                account_id,
                contents,
                conversation_id,
                search_network,
                custom_variables,
                is_channel=False,
                workbench_limits=limits,
                workbench_turn_serialized=True,
                workbench_agent_id=agent.AgentId,
                workbench_identity=identity,
                workbench_app_context=app_context,
                workbench_evidence_callback=capture_usage_evidence,
            )
            provider_started = True

            async def persist(data: Any) -> None:
                nonlocal accepted, completion_evidence_captured, event_buffer
                if isinstance(data, bytes):
                    try:
                        chunk = data.decode("utf-8")
                    except UnicodeDecodeError as error:
                        raise WorkbenchTurnError(
                            "provider event is not valid UTF-8",
                            502,
                        ) from error
                elif isinstance(data, str):
                    chunk = data
                else:
                    raise WorkbenchTurnError(
                        "provider event has an unsupported type",
                        502,
                    )
                chunk = WorkbenchSecureFilePipeline.redact_private_urls(
                    chunk,
                    private_urls,
                    sensitive_values,
                )
                event_buffer += chunk.replace("\r\n", "\n")
                while "\n\n" in event_buffer:
                    frame, event_buffer = event_buffer.split("\n\n", 1)
                    if not frame.strip():
                        continue
                    frame_data = frame + "\n\n"
                    if not completion_evidence_captured:
                        completed_payload = cls._completed_payload(frame_data)
                        if completed_payload is not None:
                            await capture_usage_evidence(completed_payload)
                    await cls._append_provider_event(turn_id, frame_data)
                    accepted = True
                if len(event_buffer.encode("utf-8")) > cls.MAX_EVENT_BYTES:
                    raise WorkbenchTurnError(
                        "provider event exceeds the persistence limit",
                        502,
                    )

            await WorkbenchStreamGuard.pump(
                upstream,
                persist,
                claims=claims,
                method="POST",
                resource_path="/chat/message",
                application_id=app_context.application_id,
                app_profile_id=app_context.app_profile_id,
                config_version=app_context.config_version,
                lease=lease,
                max_runtime_seconds=limits["max_runtime_seconds"],
                offline_reauthorize=offline_reauthorize,
                # Once Tencent has accepted a Turn, both interactive and scheduled
                # callers must keep consuming its terminal evidence when no trusted
                # cancellation contract exists.  Browser replay is authorized and
                # revoked independently.
                drain_after_reauthorization_failure=True,
            )
            if event_buffer.strip():
                frame_data = event_buffer + "\n\n"
                if not completion_evidence_captured:
                    completed_payload = cls._completed_payload(frame_data)
                    if completed_payload is not None:
                        await capture_usage_evidence(completed_payload)
                await cls._append_provider_event(turn_id, frame_data)
                accepted = True
                event_buffer = ""
            if not completion_evidence_captured:
                raise WorkbenchTurnError(
                    "provider stream ended without auditable response.completed evidence",
                    502,
                )
            lease = None
            upstream = None
            await cls._finalize(turn_id, "completed")
        except asyncio.CancelledError:
            await cls._finalize(
                turn_id,
                "failed_before_accept" if not provider_started else "provider_unknown",
                "ProcessShutdown",
            )
            raise
        except Exception as error:
            if not provider_started:
                status = "failed_before_accept"
            elif accepted:
                status = "failed_after_accept"
            else:
                status = "provider_unknown"
            await cls._finalize(turn_id, status, type(error).__name__)
            logging.warning(
                "[WorkbenchTurnManager] Turn %s ended with %s (%s)",
                turn_id,
                status,
                type(error).__name__,
            )
        finally:
            if upstream is not None:
                try:
                    await upstream.aclose()
                except Exception:
                    pass
            await WorkbenchRuntimeGuard.release(lease)

    @classmethod
    def schedule(cls, **kwargs: Any) -> asyncio.Task:
        task = asyncio.create_task(cls.execute(**kwargs))
        cls._tasks.add(task)
        WORKBENCH_METRICS.add("workbench_active_turns", 1)
        cls._live_turns[task] = _LiveTurnContext(
            task=task,
            identity=kwargs["identity"],
            app_context=kwargs["app_context"],
            claims=dict(kwargs["claims"]),
            method="POST",
            resource_path="/chat/message",
        )

        def done(completed: asyncio.Task) -> None:
            cls._tasks.discard(completed)
            cls._live_turns.pop(completed, None)
            WORKBENCH_METRICS.add("workbench_active_turns", -1)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        return task

    @classmethod
    async def replay(
        cls,
        write,
        *,
        turn_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        after_sequence: int = 0,
        claims: dict[str, Any] | None = None,
        method: str = "GET",
        resource_path: str = "/chat/turn/events",
    ) -> None:
        sequence = after_sequence
        WORKBENCH_METRICS.add("workbench_active_sse", 1)
        if after_sequence > 0:
            WORKBENCH_METRICS.inc("workbench_sse_reconnect_total")
        cls._replay_subscriber_counts[turn_id] = (
            cls._replay_subscriber_counts.get(turn_id, 0) + 1
        )
        task = asyncio.current_task()
        reauthorization_interval = tagentic_config.WORKBENCH_STREAM_REAUTH_SECONDS
        next_reauthorization = time.monotonic() + reauthorization_interval
        if task is not None and claims is not None:
            cls._subscribers[task] = _LiveTurnContext(
                task=task,
                identity=identity,
                app_context=app_context,
                claims=dict(claims),
                method=method,
                resource_path=resource_path,
            )
        try:
            while True:
                signal = cls._event_signals.setdefault(turn_id, asyncio.Event())
                signal.clear()
                if claims is not None and time.monotonic() >= next_reauthorization:
                    await WorkbenchStreamGuard.reauthorize(
                        claims=claims,
                        method=method,
                        resource_path=resource_path,
                        application_id=app_context.application_id,
                        app_profile_id=app_context.app_profile_id,
                        config_version=app_context.config_version,
                    )
                    next_reauthorization = time.monotonic() + reauthorization_interval
                async with db_connection() as db:
                    turn = await cls.get_owned(
                        db,
                        turn_id=turn_id,
                        account_id=account_id,
                        identity=identity,
                        app_context=app_context,
                    )
                    if turn is None:
                        return
                    events = (
                        await db.execute(
                            select(WorkbenchTurnEvent)
                            .where(
                                WorkbenchTurnEvent.TurnId == turn_id,
                                WorkbenchTurnEvent.Sequence > sequence,
                            )
                            .order_by(WorkbenchTurnEvent.Sequence.asc())
                            .limit(cls.REPLAY_BATCH_SIZE)
                        )
                    ).scalars().all()
                    status = turn.Status
                for event in events:
                    sequence = int(event.Sequence)
                    await write(f"id: {sequence}\n{event.EventData}".encode("utf-8"))
                if status in cls.TERMINAL_STATUSES and not events:
                    return
                try:
                    await asyncio.wait_for(
                        signal.wait(),
                        timeout=cls.REPLAY_POLL_SECONDS,
                    )
                except TimeoutError:
                    pass
        finally:
            WORKBENCH_METRICS.add("workbench_active_sse", -1)
            if task is not None:
                cls._subscribers.pop(task, None)
            remaining = cls._replay_subscriber_counts.get(turn_id, 1) - 1
            if remaining <= 0:
                cls._replay_subscriber_counts.pop(turn_id, None)
                cls._event_signals.pop(turn_id, None)
            else:
                cls._replay_subscriber_counts[turn_id] = remaining

    @classmethod
    async def handle_control_event(cls, event: WorkbenchControlEvent) -> None:
        """Re-introspect matching live work instead of comparing unrelated epochs."""
        payload = event.payload
        contexts = tuple(
            (False, context) for context in cls._live_turns.values()
        ) + tuple((True, context) for context in cls._subscribers.values())
        for is_subscriber, context in contexts:
            if event.event_type == SESSION_REVOKE:
                if (
                    context.identity.customer_id != payload["customer_id"]
                    or context.identity.new_api_user_id != payload["new_api_user_id"]
                    or (
                        payload.get("binding_id")
                        and context.identity.binding_id != payload["binding_id"]
                    )
                ):
                    continue
            elif event.event_type == CACHE_INVALIDATE:
                if (
                    context.identity.customer_id != payload["customer_id"]
                    or context.app_context.application_id != payload["application_id"]
                ):
                    continue
            else:
                continue
            try:
                await WorkbenchStreamGuard.reauthorize(
                    claims=context.claims,
                    method=context.method,
                    resource_path=context.resource_path,
                    application_id=context.app_context.application_id,
                    app_profile_id=context.app_context.app_profile_id,
                    config_version=context.app_context.config_version,
                )
            except Exception:
                if is_subscriber:
                    context.task.cancel()

    @classmethod
    async def recover_previous_lifecycle(cls) -> int:
        persistence_started_at = time.perf_counter()
        recovered = 0
        recovered_durations = []
        async with db_connection() as db:
            turns = (
                await db.execute(
                    select(WorkbenchTurn)
                    .where(
                        WorkbenchTurn.OwnerInstanceId == cls.instance_id,
                        WorkbenchTurn.OwnerLifecycleId != cls.lifecycle_id,
                        WorkbenchTurn.Status.in_(cls.NON_TERMINAL_STATUSES),
                    )
                    .with_for_update()
                )
            ).scalars().all()
            for turn in turns:
                turn.Status = "provider_unknown"
                turn.ErrorCode = "PreviousLifecycleEnded"
                completed_at = datetime.now(UTC).replace(tzinfo=None)
                turn.CompletedAt = completed_at
                recovered_durations.append(max(0.0, (completed_at - turn.CreatedAt).total_seconds()))
                terminal = cls._terminal_event(turn)
                terminal_bytes = len(terminal.encode("utf-8"))
                sequence = turn.EventCount + 1
                db.add(
                    WorkbenchTurnEvent(
                        TurnId=turn.TurnId,
                        Sequence=sequence,
                        EventData=terminal,
                        EventBytes=terminal_bytes,
                    )
                )
                turn.EventCount = sequence
                turn.EventBytes += terminal_bytes
                db.add(turn)
                recovered += 1
            await db.commit()
        if recovered > 0:
            WORKBENCH_METRICS.record_turn_event_commit(
                recovered,
                max(0.0, time.perf_counter() - persistence_started_at),
            )
        for duration_seconds in recovered_durations:
            WORKBENCH_METRICS.record_terminal("provider_unknown", duration_seconds)
        return recovered

    @classmethod
    async def shutdown(cls) -> None:
        tasks = tuple(cls._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
