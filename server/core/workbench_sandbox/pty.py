"""Durable, same-origin browser PTY gateway for managed sandboxes.

The browser receives only a short-lived one-use ticket and a local session id.
Provider instance ids, remote process ids, credentials and direct endpoints stay
server-side.  The feature is disabled by default until the Tencent E2B PTY
acceptance suite has passed in the target region.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import tagentic_config
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_identity import CoreWorkbenchIdentity
from core.workbench_policy import WorkbenchPolicy
from core.workbench_runtime import RuntimeLease, WorkbenchRuntimeError, WorkbenchRuntimeGuard
from core.workbench_sandbox.contracts import ManagedSandboxProvider, ProviderPty, SandboxProviderError
from core.workbench_sandbox.service import WorkbenchSandboxError, WorkbenchSandboxService
from model.workbench_sandbox import WorkbenchSandbox, WorkbenchSandboxPty
from util.database import db_connection


_PROTOCOL = "claw-workbench-pty-v1"
_TICKET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
_LIVE_STATUSES = frozenset({"ticket_consumed", "connecting", "open", "closing", "cleanup_pending"})


class WorkbenchSandboxPtyError(RuntimeError):
    def __init__(self, code: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class WorkbenchPtyConnection:
    pty_session_id: str
    sandbox_id: str
    sandbox_generation: int
    provider_instance_id: str
    conversation_id: str
    application_id: str
    app_profile_id: str
    config_version: int
    deadline_at: datetime
    rows: int
    cols: int
    lease: RuntimeLease


class WorkbenchSandboxPtyService:
    provider_factory = WorkbenchSandboxService.provider_factory

    @staticmethod
    def available(app_context: WorkbenchAppContext) -> bool:
        return bool(
            tagentic_config.WORKBENCH_SANDBOX_ENABLED
            and tagentic_config.WORKBENCH_SANDBOX_PTY_ENABLED
            and "sandbox" in app_context.capabilities
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    @staticmethod
    def _bounded_int(raw: Any, *, default: int, minimum: int, maximum: int, code: str) -> int:
        value = default if raw is None else raw
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise WorkbenchSandboxPtyError(code)
        return value

    @staticmethod
    def _session_digest(claims: dict[str, Any]) -> str:
        session_id = str(claims.get("sid") or "")
        if len(session_id) < 40 or len(session_id) > 128 or not all(
            character.isalnum() or character in "-_" for character in session_id
        ):
            raise WorkbenchSandboxPtyError("pty_session_invalid", 401)
        return hashlib.sha256(session_id.encode("ascii")).hexdigest()

    @staticmethod
    async def _advisory_locks(
        db: AsyncSession,
        *,
        customer_id: int,
        binding_id: str,
        sandbox_id: str,
    ) -> None:
        bind = db.get_bind()
        if bind is None or bind.dialect.name != "postgresql":
            return
        for key in sorted(
            {
                "workbench:pty:global",
                f"workbench:pty:customer:{customer_id}",
                f"workbench:pty:user:{customer_id}:{binding_id}",
                f"workbench:pty:sandbox:{sandbox_id}",
            }
        ):
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": key},
            )

    @classmethod
    async def mint_ticket(
        cls,
        db: AsyncSession,
        *,
        sandbox_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        claims: dict[str, Any],
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not cls.available(app_context):
            raise WorkbenchSandboxPtyError("sandbox_pty_disabled", 503)
        WorkbenchPolicy.require_active(identity)
        payload = payload if isinstance(payload, dict) else {}
        if set(payload) - {"conversation_id", "rows", "cols", "timeout_seconds"}:
            raise WorkbenchSandboxPtyError("pty_request_invalid")
        rows = cls._bounded_int(payload.get("rows"), default=24, minimum=2, maximum=200, code="pty_size_invalid")
        cols = cls._bounded_int(payload.get("cols"), default=80, minimum=10, maximum=400, code="pty_size_invalid")
        timeout = cls._bounded_int(
            payload.get("timeout_seconds"),
            default=min(600, tagentic_config.WORKBENCH_SANDBOX_PTY_MAX_RUNTIME_SECONDS),
            minimum=1,
            maximum=tagentic_config.WORKBENCH_SANDBOX_PTY_MAX_RUNTIME_SECONDS,
            code="pty_timeout_invalid",
        )
        sandbox = await WorkbenchSandboxService._owned(
            db,
            sandbox_id=sandbox_id,
            conversation_id=payload.get("conversation_id"),
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            mutation=True,
            lock=True,
        )
        if sandbox.Status != "running" or not sandbox.ProviderInstanceId:
            raise WorkbenchSandboxPtyError("sandbox_not_running", 409)
        now = cls._now()
        remaining = int((sandbox.ExpiresAt - now).total_seconds()) if sandbox.ExpiresAt else timeout
        timeout = min(timeout, remaining, int(sandbox.TimeoutSeconds))
        if timeout <= 0:
            raise WorkbenchSandboxPtyError("sandbox_not_running", 409)
        await cls._advisory_locks(
            db,
            customer_id=identity.customer_id,
            binding_id=identity.binding_id,
            sandbox_id=sandbox_id,
        )
        await db.execute(
            update(WorkbenchSandboxPty)
            .where(
                WorkbenchSandboxPty.SandboxId == sandbox_id,
                WorkbenchSandboxPty.BindingId == identity.binding_id,
                WorkbenchSandboxPty.Status == "ticket_issued",
            )
            .values(Status="superseded", ClosedAt=now, ErrorCode="ticket_superseded")
        )
        ticket = secrets.token_urlsafe(32)
        ticket_hash = hashlib.sha256(ticket.encode("ascii")).hexdigest()
        expires_at = now + timedelta(
            seconds=tagentic_config.WORKBENCH_SANDBOX_PTY_TICKET_TTL_SECONDS
        )
        pty_session_id = "pty_" + secrets.token_hex(16)
        row = WorkbenchSandboxPty(
            PtySessionId=pty_session_id,
            SandboxId=sandbox.SandboxId,
            SandboxGeneration=sandbox.Generation,
            ProviderInstanceId=sandbox.ProviderInstanceId,
            BindingId=identity.binding_id,
            AccountId=sandbox.AccountId,
            CustomerId=identity.customer_id,
            NewApiUserId=identity.new_api_user_id,
            CanonicalSubject=identity.canonical_subject,
            ApplicationId=app_context.application_id,
            AppProfileId=app_context.app_profile_id,
            ConfigVersion=app_context.config_version,
            AuthEpoch=identity.auth_epoch,
            ConversationId=sandbox.ConversationId,
            TicketHash=ticket_hash,
            TicketExpiresAt=expires_at,
            Status="ticket_issued",
            Rows=rows,
            Cols=cols,
            TimeoutSeconds=timeout,
            DeadlineAt=now + timedelta(seconds=timeout),
            LastHeartbeatAt=now,
            LeaseOwner=cls._session_digest(claims),
        )
        db.add(row)
        await db.commit()
        return {
            "pty_session_id": pty_session_id,
            "ticket": ticket,
            "expires_at": expires_at.isoformat() + "Z",
            "protocol": _PROTOCOL,
        }

    @classmethod
    async def consume_ticket(
        cls,
        db: AsyncSession,
        *,
        sandbox_id: str,
        ticket: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        claims: dict[str, Any],
    ) -> WorkbenchPtyConnection:
        if not cls.available(app_context):
            raise WorkbenchSandboxPtyError("sandbox_pty_disabled", 503)
        WorkbenchPolicy.require_active(identity)
        if not _TICKET_PATTERN.fullmatch(ticket):
            raise WorkbenchSandboxPtyError("pty_ticket_invalid", 401)
        now = cls._now()
        digest = hashlib.sha256(ticket.encode("ascii")).hexdigest()
        row = (
            await db.execute(
                select(WorkbenchSandboxPty)
                .where(
                    WorkbenchSandboxPty.SandboxId == sandbox_id,
                    WorkbenchSandboxPty.TicketHash == digest,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise WorkbenchSandboxPtyError("pty_ticket_invalid", 401)
        if row.Status != "ticket_issued" or row.TicketConsumedAt is not None:
            raise WorkbenchSandboxPtyError("pty_ticket_replayed", 409)
        if row.TicketExpiresAt <= now:
            row.Status = "expired"
            row.ClosedAt = now
            await db.commit()
            raise WorkbenchSandboxPtyError("pty_ticket_expired", 401)
        if (
            row.LeaseOwner != cls._session_digest(claims)
            or str(row.AccountId) != str(account_id)
            or row.BindingId != identity.binding_id
            or row.CustomerId != identity.customer_id
            or row.NewApiUserId != identity.new_api_user_id
            or row.CanonicalSubject != identity.canonical_subject
            or row.AuthEpoch != identity.auth_epoch
            or row.ApplicationId != app_context.application_id
            or row.AppProfileId != app_context.app_profile_id
            or row.ConfigVersion != app_context.config_version
        ):
            raise WorkbenchSandboxPtyError("pty_ticket_scope_mismatch", 403)
        sandbox = (
            await db.execute(
                select(WorkbenchSandbox)
                .where(
                    WorkbenchSandbox.SandboxId == sandbox_id,
                    WorkbenchSandbox.Generation == row.SandboxGeneration,
                    WorkbenchSandbox.ProviderInstanceId == row.ProviderInstanceId,
                    WorkbenchSandbox.Status == "running",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if sandbox is None:
            raise WorkbenchSandboxPtyError("sandbox_not_running", 409)
        await cls._advisory_locks(
            db,
            customer_id=identity.customer_id,
            binding_id=identity.binding_id,
            sandbox_id=sandbox_id,
        )
        # The ticket currently being consumed is not an existing occupant.  Exclude
        # it explicitly instead of relying on ORM autoflush timing: production uses
        # AsyncSession while the cross-database tests use a synchronous adapter.
        active_filter = (
            WorkbenchSandboxPty.Status.in_(_LIVE_STATUSES),
            WorkbenchSandboxPty.PtySessionId != row.PtySessionId,
        )
        global_count = (
            await db.execute(select(func.count()).select_from(WorkbenchSandboxPty).where(*active_filter))
        ).scalar_one()
        customer_count = (
            await db.execute(
                select(func.count()).select_from(WorkbenchSandboxPty).where(
                    *active_filter, WorkbenchSandboxPty.CustomerId == identity.customer_id
                )
            )
        ).scalar_one()
        user_count = (
            await db.execute(
                select(func.count()).select_from(WorkbenchSandboxPty).where(
                    *active_filter,
                    WorkbenchSandboxPty.CustomerId == identity.customer_id,
                    WorkbenchSandboxPty.BindingId == identity.binding_id,
                )
            )
        ).scalar_one()
        if (
            global_count >= tagentic_config.WORKBENCH_SANDBOX_PTY_GLOBAL_CAPACITY
            or customer_count >= tagentic_config.WORKBENCH_SANDBOX_PTY_CUSTOMER_CAPACITY
            or user_count >= tagentic_config.WORKBENCH_SANDBOX_PTY_USER_CAPACITY
        ):
            row.Status = "capacity_rejected"
            row.TicketConsumedAt = now
            row.ClosedAt = now
            row.ErrorCode = "pty_capacity"
            await db.commit()
            raise WorkbenchSandboxPtyError("pty_capacity", 429)
        row.TicketConsumedAt = now
        row.Status = "ticket_consumed"
        row.LeaseOwner = tagentic_config.WORKBENCH_INSTANCE_ID
        row.LeaseUntil = now + timedelta(seconds=row.TimeoutSeconds)
        await db.commit()
        try:
            lease = await WorkbenchRuntimeGuard.acquire(
                account_id=account_id,
                identity=identity,
                app_context=app_context,
                operation="sandbox_pty",
            )
        except WorkbenchRuntimeError as error:
            async with db_connection() as state_db:
                await state_db.execute(
                    update(WorkbenchSandboxPty)
                    .where(
                        WorkbenchSandboxPty.PtySessionId == row.PtySessionId,
                        WorkbenchSandboxPty.Status == "ticket_consumed",
                    )
                    .values(
                        Status="capacity_rejected",
                        ClosedAt=cls._now(),
                        ErrorCode="pty_capacity",
                        LeaseOwner=None,
                        LeaseUntil=None,
                    )
                )
                await state_db.commit()
            raise WorkbenchSandboxPtyError("pty_capacity", error.status_code) from error
        deadline = min(
            row.DeadlineAt,
            cls._now() + timedelta(seconds=lease.max_runtime_seconds),
            sandbox.ExpiresAt or row.DeadlineAt,
        )
        async with db_connection() as state_db:
            changed = await state_db.execute(
                update(WorkbenchSandboxPty)
                .where(
                    WorkbenchSandboxPty.PtySessionId == row.PtySessionId,
                    WorkbenchSandboxPty.Status == "ticket_consumed",
                )
                .values(
                    Status="connecting",
                    DeadlineAt=deadline,
                    LeaseOwner=lease.lease_id,
                    LeaseUntil=deadline,
                    LastHeartbeatAt=cls._now(),
                )
            )
            await state_db.commit()
            if changed.rowcount != 1:
                await WorkbenchRuntimeGuard.release(lease)
                raise WorkbenchSandboxPtyError("pty_state_conflict", 409)
        return WorkbenchPtyConnection(
            pty_session_id=row.PtySessionId,
            sandbox_id=row.SandboxId,
            sandbox_generation=row.SandboxGeneration,
            provider_instance_id=row.ProviderInstanceId,
            conversation_id=row.ConversationId,
            application_id=row.ApplicationId,
            app_profile_id=row.AppProfileId,
            config_version=row.ConfigVersion,
            deadline_at=deadline,
            rows=row.Rows,
            cols=row.Cols,
            lease=lease,
        )

    @staticmethod
    def parse_protocol_header(raw: str | None) -> str:
        values = [item.strip() for item in str(raw or "").split(",") if item.strip()]
        if len(values) != 2 or values[0] != _PROTOCOL or not values[1].startswith("ticket."):
            raise WorkbenchSandboxPtyError("pty_protocol_invalid", 400)
        ticket = values[1][len("ticket.") :]
        if not _TICKET_PATTERN.fullmatch(ticket):
            raise WorkbenchSandboxPtyError("pty_ticket_invalid", 401)
        return ticket

    @classmethod
    async def _set_state(cls, pty_session_id: str, expected: set[str], **values: Any) -> bool:
        async with db_connection() as db:
            result = await db.execute(
                update(WorkbenchSandboxPty)
                .where(
                    WorkbenchSandboxPty.PtySessionId == pty_session_id,
                    WorkbenchSandboxPty.Status.in_(expected),
                )
                .values(**values)
            )
            await db.commit()
            return result.rowcount == 1

    @classmethod
    async def _heartbeat(cls, connection: WorkbenchPtyConnection) -> None:
        while True:
            await asyncio.sleep(tagentic_config.WORKBENCH_SANDBOX_PTY_HEARTBEAT_SECONDS)
            now = cls._now()
            changed = await cls._set_state(
                connection.pty_session_id,
                {"open"},
                LastHeartbeatAt=now,
                LeaseUntil=min(connection.deadline_at, now + timedelta(seconds=tagentic_config.WORKBENCH_SANDBOX_PTY_STALE_SECONDS)),
            )
            if not changed:
                raise WorkbenchSandboxPtyError("pty_session_revoked", 403)

    @classmethod
    async def _reauthorize(
        cls,
        connection: WorkbenchPtyConnection,
        claims: dict[str, Any],
    ) -> None:
        while True:
            await asyncio.sleep(tagentic_config.WORKBENCH_SANDBOX_PTY_REAUTH_SECONDS)
            async with db_connection() as db:
                identity, app_context = await CoreWorkbenchIdentity.authorize_session(
                    db,
                    claims,
                    method="GET",
                    resource_path=f"/sandbox/{connection.sandbox_id}/pty/connect",
                    supplied_application_id=connection.application_id,
                )
                WorkbenchPolicy.require_active(identity)
                WorkbenchPolicy.require_capability(app_context, "sandbox")
                row = (
                    await db.execute(
                        select(WorkbenchSandboxPty).where(
                            WorkbenchSandboxPty.PtySessionId == connection.pty_session_id,
                            WorkbenchSandboxPty.BindingId == identity.binding_id,
                            WorkbenchSandboxPty.AuthEpoch == identity.auth_epoch,
                            WorkbenchSandboxPty.AppProfileId == app_context.app_profile_id,
                            WorkbenchSandboxPty.ConfigVersion == app_context.config_version,
                            WorkbenchSandboxPty.Status == "open",
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise WorkbenchSandboxPtyError("pty_session_revoked", 403)

    @classmethod
    async def _abnormal_cleanup(
        cls,
        connection: WorkbenchPtyConnection,
        provider: ManagedSandboxProvider,
        handle: ProviderPty | None,
        code: str,
    ) -> None:
        await cls._set_state(
            connection.pty_session_id,
            {"connecting", "open", "closing", "cleanup_pending"},
            Status="cleanup_pending",
            ErrorCode=code[:64],
            LastHeartbeatAt=cls._now(),
        )
        if handle is not None:
            try:
                await asyncio.wait_for(
                    handle.kill(),
                    timeout=tagentic_config.WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS,
                )
            except Exception:
                pass
        await WorkbenchSandboxService._bounded_stream_stop(
            sandbox_id=connection.sandbox_id,
            provider_instance_id=connection.provider_instance_id,
            provider=provider,
            event_type="pty_cleanup",
            error_code=code,
        )
        await cls._set_state(
            connection.pty_session_id,
            {"cleanup_pending"},
            Status="cleanup_requested",
            ClosedAt=cls._now(),
            LeaseOwner=None,
            LeaseUntil=None,
        )

    @classmethod
    async def serve(
        cls,
        websocket: Any,
        *,
        connection: WorkbenchPtyConnection,
        claims: dict[str, Any],
        provider: ManagedSandboxProvider | None = None,
    ) -> None:
        provider = provider or cls.provider_factory()
        handle: ProviderPty | None = None
        normal_close = False
        error_code = "pty_connection_lost"
        tasks: set[asyncio.Task[Any]] = set()
        try:
            remaining = max(1, int((connection.deadline_at - cls._now()).total_seconds()))
            handle = await provider.create_pty(
                connection.provider_instance_id,
                rows=connection.rows,
                cols=connection.cols,
                timeout_seconds=remaining,
                output_queue_frames=tagentic_config.WORKBENCH_SANDBOX_PTY_OUTPUT_QUEUE_FRAMES,
            )
            if handle.pid <= 0:
                raise SandboxProviderError("provider_pty_pid_invalid")
            opened = await cls._set_state(
                connection.pty_session_id,
                {"connecting"},
                Status="open",
                ProviderPid=handle.pid,
                LastHeartbeatAt=cls._now(),
            )
            if not opened:
                raise WorkbenchSandboxPtyError("pty_state_conflict", 409)
            await websocket.send(json.dumps({"type": "ready", "pty_session_id": connection.pty_session_id}))
            input_bytes = 0
            output_bytes = 0
            input_frames = 0
            output_frames = 0
            recent_frames: deque[float] = deque()

            async def output_loop() -> None:
                nonlocal output_bytes, output_frames
                assert handle is not None
                async for chunk in handle.output():
                    if len(chunk) > tagentic_config.WORKBENCH_SANDBOX_PTY_MAX_OUTPUT_FRAME_BYTES:
                        raise WorkbenchSandboxPtyError("pty_output_limit", 413)
                    output_bytes += len(chunk)
                    output_frames += 1
                    if output_bytes > tagentic_config.WORKBENCH_SANDBOX_PTY_MAX_OUTPUT_BYTES:
                        raise WorkbenchSandboxPtyError("pty_output_limit", 413)
                    await websocket.send(chunk)

            async def input_loop() -> None:
                nonlocal normal_close, input_bytes, input_frames
                assert handle is not None
                while True:
                    message = await websocket.recv()
                    if not isinstance(message, str):
                        raise WorkbenchSandboxPtyError("pty_frame_invalid")
                    encoded = message.encode("utf-8")
                    if len(encoded) > 16 * 1024:
                        raise WorkbenchSandboxPtyError("pty_frame_invalid", 413)
                    try:
                        value = json.loads(message)
                    except (TypeError, ValueError) as error:
                        raise WorkbenchSandboxPtyError("pty_frame_invalid") from error
                    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
                        raise WorkbenchSandboxPtyError("pty_frame_invalid")
                    kind = value["type"]
                    if kind == "close" and set(value) == {"type"}:
                        normal_close = True
                        return
                    if kind == "resize" and set(value) == {"type", "rows", "cols"}:
                        rows = cls._bounded_int(value["rows"], default=0, minimum=2, maximum=200, code="pty_size_invalid")
                        cols = cls._bounded_int(value["cols"], default=0, minimum=10, maximum=400, code="pty_size_invalid")
                        await handle.resize(rows=rows, cols=cols)
                        continue
                    if kind != "input" or set(value) != {"type", "data"} or not isinstance(value.get("data"), str):
                        raise WorkbenchSandboxPtyError("pty_frame_invalid")
                    data = value["data"].encode("utf-8")
                    if len(data) > tagentic_config.WORKBENCH_SANDBOX_PTY_MAX_INPUT_FRAME_BYTES:
                        raise WorkbenchSandboxPtyError("pty_input_limit", 413)
                    now_mono = time.monotonic()
                    while recent_frames and recent_frames[0] <= now_mono - 1:
                        recent_frames.popleft()
                    if len(recent_frames) >= tagentic_config.WORKBENCH_SANDBOX_PTY_MAX_INPUT_FRAMES_PER_SECOND:
                        raise WorkbenchSandboxPtyError("pty_input_rate", 429)
                    recent_frames.append(now_mono)
                    input_bytes += len(data)
                    input_frames += 1
                    if input_bytes > tagentic_config.WORKBENCH_SANDBOX_PTY_MAX_INPUT_BYTES:
                        raise WorkbenchSandboxPtyError("pty_input_limit", 413)
                    await handle.send_input(data)

            output_task = asyncio.create_task(output_loop())
            input_task = asyncio.create_task(input_loop())
            wait_task = asyncio.create_task(handle.wait())
            tasks = {
                output_task,
                input_task,
                wait_task,
                asyncio.create_task(cls._heartbeat(connection)),
                asyncio.create_task(cls._reauthorize(connection, claims)),
            }
            done, pending = await asyncio.wait(
                tasks,
                timeout=max(0.0, (connection.deadline_at - cls._now()).total_seconds()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                error_code = "pty_timeout"
                raise WorkbenchSandboxPtyError(error_code, 504)
            for task in done:
                exception = task.exception()
                if exception is not None:
                    raise exception
            provider_exited = wait_task in done or output_task in done
            normal_close = True
            await cls._set_state(
                connection.pty_session_id,
                {"open"},
                Status="closing",
                NormalClose=True,
                InputBytes=input_bytes,
                OutputBytes=output_bytes,
                InputFrames=input_frames,
                OutputFrames=output_frames,
            )
            if not provider_exited:
                try:
                    killed = await handle.kill()
                    if killed is False:
                        raise WorkbenchSandboxPtyError("pty_kill_unknown", 503)
                except Exception as error:
                    normal_close = False
                    error_code = "pty_kill_unknown"
                    raise WorkbenchSandboxPtyError(error_code, 503) from error
            await cls._set_state(
                connection.pty_session_id,
                {"closing"},
                Status="closed",
                ClosedAt=cls._now(),
                LeaseOwner=None,
                LeaseUntil=None,
            )
            try:
                await websocket.send(json.dumps({"type": "exit", "exit_code": 0}))
            except Exception:
                pass
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            error_code = "pty_cancelled"
            raise
        except Exception as error:
            if isinstance(error, WorkbenchSandboxPtyError):
                error_code = error.code
            elif isinstance(error, SandboxProviderError):
                error_code = "pty_provider_failed"
            try:
                await websocket.send(json.dumps({"type": "error", "code": "pty_session_closed"}))
            except Exception:
                pass
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                if not normal_close:
                    await cls._abnormal_cleanup(connection, provider, handle, error_code)
            finally:
                await WorkbenchRuntimeGuard.release(connection.lease)

    @classmethod
    async def reap_once(cls, provider: ManagedSandboxProvider | None = None) -> int:
        """Reclaim expired tickets and abandoned sessions across Blue/Green."""

        provider = provider or cls.provider_factory()
        now = cls._now()
        stale = now - timedelta(seconds=tagentic_config.WORKBENCH_SANDBOX_PTY_STALE_SECONDS)
        async with db_connection() as db:
            await db.execute(
                update(WorkbenchSandboxPty)
                .where(
                    WorkbenchSandboxPty.Status == "ticket_issued",
                    WorkbenchSandboxPty.TicketExpiresAt <= now,
                )
                .values(Status="expired", ClosedAt=now, ErrorCode="ticket_expired")
            )
            rows = (
                await db.execute(
                    select(WorkbenchSandboxPty).where(
                        WorkbenchSandboxPty.Status.in_(_LIVE_STATUSES),
                        or_(
                            WorkbenchSandboxPty.DeadlineAt <= now,
                            WorkbenchSandboxPty.LastHeartbeatAt <= stale,
                        ),
                    )
                )
            ).scalars().all()
            claimed: list[tuple[str, str, str, int | None, str | None, int]] = []
            for row in rows:
                result = await db.execute(
                    update(WorkbenchSandboxPty)
                    .where(
                        WorkbenchSandboxPty.PtySessionId == row.PtySessionId,
                        WorkbenchSandboxPty.Status == row.Status,
                    )
                    .values(Status="cleanup_pending", ErrorCode="pty_stale", LastHeartbeatAt=now)
                )
                if result.rowcount == 1:
                    claimed.append(
                        (
                            row.PtySessionId,
                            row.SandboxId,
                            row.ProviderInstanceId,
                            row.ProviderPid,
                            row.LeaseOwner,
                            row.TimeoutSeconds,
                        )
                    )
            await db.commit()
        for pty_session_id, sandbox_id, instance_id, pid, lease_id, timeout in claimed:
            if pid:
                try:
                    await provider.kill_pty(instance_id, pid=pid)
                except Exception:
                    pass
            await WorkbenchSandboxService._bounded_stream_stop(
                sandbox_id=sandbox_id,
                provider_instance_id=instance_id,
                provider=provider,
                event_type="pty_reaper",
                error_code="pty_stale",
            )
            await cls._set_state(
                pty_session_id,
                {"cleanup_pending"},
                Status="cleanup_requested",
                ClosedAt=cls._now(),
                LeaseOwner=None,
                LeaseUntil=None,
            )
            if lease_id:
                await WorkbenchRuntimeGuard.release(
                    RuntimeLease(lease_id=lease_id, max_runtime_seconds=timeout)
                )
        return len(claimed)


async def workbench_pty_reaper() -> None:
    while True:
        await asyncio.sleep(tagentic_config.WORKBENCH_SANDBOX_PTY_REAPER_SECONDS)
        if not tagentic_config.WORKBENCH_SANDBOX_PTY_ENABLED:
            continue
        try:
            await WorkbenchSandboxPtyService.reap_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            # No provider or credential details are ever logged here.
            continue
