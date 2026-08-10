from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytz
from pytz import UnknownTimeZoneError
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app_factory import TAgenticApp
from config import tagentic_config
from core.agent import CoreAgent
from core.conversation import CoreConversation
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_file_ownership import WorkbenchFileOwnership
from core.workbench_identity import CoreWorkbenchIdentity, WorkbenchIdentityError
from core.workbench_metrics import WORKBENCH_METRICS
from core.workbench_policy import WorkbenchPolicy
from core.workbench_turn import WorkbenchTurnManager
from core.workbench_integrations import WorkbenchIntegrations
from model.workbench import WorkbenchAgentBinding, WorkbenchTurn
from model.workbench_scheduled import (
    WorkbenchScheduledAudit,
    WorkbenchScheduledDelegation,
    WorkbenchScheduledRun,
    WorkbenchScheduledTask,
)
from util.database import db_connection


class WorkbenchScheduledError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class _CronField:
    values: frozenset[int]
    wildcard: bool


class WorkbenchCron:
    """Bounded five-field cron evaluated against an immutable IANA timezone."""

    _RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))

    def __init__(self, expression: str, timezone_name: str):
        if not isinstance(expression, str) or len(expression) > 128:
            raise WorkbenchScheduledError("cron_expression is invalid")
        parts = expression.strip().split()
        if len(parts) != 5:
            raise WorkbenchScheduledError("cron_expression must contain five fields")
        try:
            self.timezone = pytz.timezone(timezone_name)
        except (UnknownTimeZoneError, AttributeError) as error:
            raise WorkbenchScheduledError("timezone must be an IANA timezone") from error
        self.timezone_name = timezone_name
        self.expression = " ".join(parts)
        self.fields = tuple(
            self._parse_field(part, minimum, maximum)
            for part, (minimum, maximum) in zip(parts, self._RANGES, strict=True)
        )

    @staticmethod
    def _parse_field(raw: str, minimum: int, maximum: int) -> _CronField:
        if not raw or len(raw) > 64:
            raise WorkbenchScheduledError("cron_expression contains an invalid field")
        values: set[int] = set()
        wildcard = raw == "*" or raw.startswith("*/")
        for fragment in raw.split(","):
            base, separator, step_text = fragment.partition("/")
            if separator:
                if not step_text.isdigit():
                    raise WorkbenchScheduledError("cron_expression step is invalid")
                step = int(step_text)
                if step < 1 or step > maximum - minimum + 1:
                    raise WorkbenchScheduledError("cron_expression step is invalid")
            else:
                step = 1
            if base == "*":
                start, end = minimum, maximum
            elif "-" in base:
                start_text, end_text = base.split("-", 1)
                if not start_text.isdigit() or not end_text.isdigit():
                    raise WorkbenchScheduledError("cron_expression range is invalid")
                start, end = int(start_text), int(end_text)
            elif base.isdigit():
                start = end = int(base)
            else:
                raise WorkbenchScheduledError("cron_expression field is invalid")
            if start < minimum or end > maximum or start > end:
                raise WorkbenchScheduledError("cron_expression value is out of range")
            values.update(range(start, end + 1, step))
        if not values:
            raise WorkbenchScheduledError("cron_expression field is empty")
        return _CronField(frozenset(values), wildcard)

    def _matches(self, local: datetime) -> bool:
        minute, hour, day, month, weekday = self.fields
        cron_weekday = (local.weekday() + 1) % 7
        day_match = local.day in day.values
        weekday_match = cron_weekday in weekday.values
        if day.wildcard and weekday.wildcard:
            calendar_match = True
        elif day.wildcard:
            calendar_match = weekday_match
        elif weekday.wildcard:
            calendar_match = day_match
        else:
            calendar_match = day_match or weekday_match
        return (
            local.minute in minute.values
            and local.hour in hour.values
            and local.month in month.values
            and calendar_match
        )

    def next_after(self, after_utc: datetime) -> datetime:
        aware_after = _aware_utc(after_utc)
        minimum_wall = aware_after.astimezone(self.timezone).replace(
            second=0, microsecond=0, tzinfo=None
        )
        candidate = aware_after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(370 * 24 * 60):
            local = candidate.astimezone(self.timezone)
            # During a fall-back hour, execute a matching wall-clock minute only
            # once. Spring-forward gaps naturally have no matching UTC instant.
            if local.replace(tzinfo=None) > minimum_wall and self._matches(local):
                return candidate.replace(tzinfo=None)
            candidate += timedelta(minutes=1)
        raise WorkbenchScheduledError("cron_expression has no occurrence within 370 days")


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _naive_utc(value: datetime) -> datetime:
    return _aware_utc(value).replace(tzinfo=None)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class WorkbenchScheduledService:
    ACTIVE_STATUSES = frozenset({"active", "paused"})
    RUNNABLE_STATUSES = frozenset({"queued", "retrying"})
    TERMINAL_RUN_STATUSES = frozenset(
        {
            "completed",
            "failed_before_accept",
            "failed_after_accept",
            "provider_unknown",
            "cancelled",
            "revoked",
            "skipped_misfire",
            "skipped_limit",
        }
    )
    instance_id = f"{WorkbenchTurnManager.instance_id}:schedule"[:128]
    _stop = asyncio.Event()
    _worker: asyncio.Task | None = None

    @staticmethod
    def require_enabled() -> None:
        if not (
            tagentic_config.WORKBENCH_MODE
            and tagentic_config.WORKBENCH_SCHEDULED_TASKS_ENABLED
        ):
            raise WorkbenchScheduledError("scheduled tasks are disabled", 404)

    @staticmethod
    async def require_recent_reauthentication(
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        session_claims: dict[str, Any],
    ) -> None:
        try:
            await CoreWorkbenchIdentity.require_browser_session(
                db,
                claims=session_claims,
                account_id=account_id,
                identity=identity,
                maximum_age_seconds=tagentic_config.WORKBENCH_SCHEDULE_REAUTH_SECONDS,
            )
        except WorkbenchIdentityError as error:
            raise WorkbenchScheduledError(
                "recent workbench reauthentication is required", 401
            ) from error

    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        payload: Any,
        session_claims: dict[str, Any],
    ) -> WorkbenchScheduledTask:
        cls.require_enabled()
        await cls.require_recent_reauthentication(
            db,
            account_id=account_id,
            identity=identity,
            session_claims=session_claims,
        )
        values = await cls._validated_definition(
            db,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            payload=payload,
        )
        active_count = (
            await db.execute(
                select(func.count(WorkbenchScheduledTask.Id)).where(
                    WorkbenchScheduledTask.BindingId == identity.binding_id,
                    WorkbenchScheduledTask.AccountId == account_id,
                    WorkbenchScheduledTask.ApplicationId == app_context.application_id,
                    WorkbenchScheduledTask.Status != "deleted",
                )
            )
        ).scalar_one()
        if active_count >= tagentic_config.WORKBENCH_SCHEDULE_MAX_TASKS_PER_USER:
            raise WorkbenchScheduledError("scheduled task count limit reached", 429)

        task_id = f"wst_{uuid.uuid4().hex}"
        task = WorkbenchScheduledTask(
            TaskId=task_id,
            BindingId=identity.binding_id,
            AccountId=account_id,
            CustomerId=identity.customer_id,
            NewApiUserId=identity.new_api_user_id,
            CanonicalSubject=identity.canonical_subject,
            ApplicationId=app_context.application_id,
            ProviderAppId=app_context.app_id,
            AppProfileId=str(app_context.app_profile_id),
            ConfigVersion=app_context.config_version,
            AuthEpoch=identity.auth_epoch,
            AgentId=values["agent_id"],
            ConversationId=values["conversation_id"],
            Name=values["name"],
            Prompt=values["prompt"],
            AttachmentIdsJson=_canonical(values["attachment_ids"]),
            ScheduleKind=values["schedule_kind"],
            CronExpression=values["cron_expression"],
            Timezone=values["timezone"],
            OnceAt=values["once_at"],
            NextRunAt=values["next_run_at"],
            MisfirePolicy=values["misfire_policy"],
            MaxRuntimeSeconds=values["max_runtime_seconds"],
            DailyRunLimit=values["daily_run_limit"],
            MaxRetries=values["max_retries"],
            RetryBackoffSeconds=values["retry_backoff_seconds"],
            PlanSnapshotJson=values["plan_snapshot"],
            PlanSnapshotHash=values["plan_snapshot_hash"],
            Status="active",
        )
        delegation = cls._new_delegation(task, capability_version=1)
        db.add(task)
        db.add(delegation)
        cls._audit(db, task=task, action="create", result="allowed")
        await db.commit()
        WORKBENCH_METRICS.inc("workbench_scheduled_task_actions_total", action="create")
        return task

    @classmethod
    async def update(
        cls,
        db: AsyncSession,
        *,
        task_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        payload: Any,
        session_claims: dict[str, Any],
    ) -> WorkbenchScheduledTask:
        cls.require_enabled()
        await cls.require_recent_reauthentication(
            db,
            account_id=account_id,
            identity=identity,
            session_claims=session_claims,
        )
        task = await cls._owned_task(
            db, task_id, account_id, identity, app_context, lock=True
        )
        if task is None or task.Status == "deleted":
            raise WorkbenchScheduledError("scheduled task not found", 404)
        if task.Status != "paused":
            raise WorkbenchScheduledError("pause the scheduled task before editing", 409)
        current = cls._definition_payload(task)
        if not isinstance(payload, dict):
            raise WorkbenchScheduledError("request body must be an object")
        unknown = set(payload).difference(current)
        if unknown:
            raise WorkbenchScheduledError(
                "request contains unsupported fields: " + ", ".join(sorted(unknown))
            )
        current.update(payload)
        values = await cls._validated_definition(
            db,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            payload=current,
        )
        task.Name = values["name"]
        task.Prompt = values["prompt"]
        task.AttachmentIdsJson = _canonical(values["attachment_ids"])
        task.ConversationId = values["conversation_id"]
        task.ScheduleKind = values["schedule_kind"]
        task.CronExpression = values["cron_expression"]
        task.Timezone = values["timezone"]
        task.OnceAt = values["once_at"]
        task.NextRunAt = None
        task.MisfirePolicy = values["misfire_policy"]
        task.MaxRuntimeSeconds = values["max_runtime_seconds"]
        task.DailyRunLimit = values["daily_run_limit"]
        task.MaxRetries = values["max_retries"]
        task.RetryBackoffSeconds = values["retry_backoff_seconds"]
        task.PlanSnapshotJson = values["plan_snapshot"]
        task.PlanSnapshotHash = values["plan_snapshot_hash"]
        task.ProviderAppId = app_context.app_id
        task.ConfigVersion = app_context.config_version
        task.AuthEpoch = identity.auth_epoch
        task.AgentId = values["agent_id"]
        task.Version += 1
        await cls._rotate_delegation(db, task, activate=False)
        cls._audit(db, task=task, action="update", result="allowed")
        await db.commit()
        WORKBENCH_METRICS.inc("workbench_scheduled_task_actions_total", action="update")
        return task

    @classmethod
    async def pause(
        cls,
        db: AsyncSession,
        *,
        task_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        session_claims: dict[str, Any],
    ) -> WorkbenchScheduledTask:
        cls.require_enabled()
        await cls.require_recent_reauthentication(
            db,
            account_id=account_id,
            identity=identity,
            session_claims=session_claims,
        )
        task = await cls._owned_task(
            db, task_id, account_id, identity, app_context, lock=True
        )
        if task is None or task.Status == "deleted":
            raise WorkbenchScheduledError("scheduled task not found", 404)
        if task.Status != "paused":
            task.Status = "paused"
            task.NextRunAt = None
            task.Version += 1
            await cls._revoke_delegation(db, task.TaskId)
            await db.execute(
                update(WorkbenchScheduledRun)
                .where(
                    WorkbenchScheduledRun.TaskId == task.TaskId,
                    WorkbenchScheduledRun.Status.in_(cls.RUNNABLE_STATUSES),
                )
                .values(Status="cancelled", FinishedAt=datetime.now(UTC).replace(tzinfo=None))
            )
            cls._audit(db, task=task, action="pause", result="allowed")
            await db.commit()
            WORKBENCH_METRICS.inc("workbench_scheduled_task_actions_total", action="pause")
        return task

    @classmethod
    async def resume(
        cls,
        db: AsyncSession,
        *,
        task_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        session_claims: dict[str, Any],
    ) -> WorkbenchScheduledTask:
        cls.require_enabled()
        await cls.require_recent_reauthentication(
            db,
            account_id=account_id,
            identity=identity,
            session_claims=session_claims,
        )
        task = await cls._owned_task(
            db, task_id, account_id, identity, app_context, lock=True
        )
        if task is None or task.Status == "deleted":
            raise WorkbenchScheduledError("scheduled task not found", 404)
        if task.Status != "paused":
            return task
        WorkbenchPolicy.validate_scheduled_turn(
            identity,
            app_context,
            task_max_runtime_seconds=task.MaxRuntimeSeconds,
        )
        if (
            task.AuthEpoch != identity.auth_epoch
            or task.ConfigVersion != app_context.config_version
            or task.AppProfileId != str(app_context.app_profile_id)
        ):
            raise WorkbenchScheduledError("scheduled task scope changed; edit it before resuming", 409)
        next_run = cls._next_for_task(task, datetime.now(UTC).replace(tzinfo=None))
        if next_run is None:
            raise WorkbenchScheduledError("one-time schedule is no longer in the future", 409)
        task.Status = "active"
        task.NextRunAt = next_run
        task.Version += 1
        await cls._rotate_delegation(db, task, activate=True)
        cls._audit(db, task=task, action="resume", result="allowed")
        await db.commit()
        WORKBENCH_METRICS.inc("workbench_scheduled_task_actions_total", action="resume")
        return task

    @classmethod
    async def delete(
        cls,
        db: AsyncSession,
        *,
        task_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        session_claims: dict[str, Any],
    ) -> None:
        cls.require_enabled()
        await cls.require_recent_reauthentication(
            db,
            account_id=account_id,
            identity=identity,
            session_claims=session_claims,
        )
        task = await cls._owned_task(
            db, task_id, account_id, identity, app_context, lock=True
        )
        if task is None:
            raise WorkbenchScheduledError("scheduled task not found", 404)
        if task.Status != "deleted":
            now = datetime.now(UTC).replace(tzinfo=None)
            task.Status = "deleted"
            task.NextRunAt = None
            task.DeletedAt = now
            task.Version += 1
            await cls._revoke_delegation(db, task.TaskId)
            await db.execute(
                update(WorkbenchScheduledRun)
                .where(
                    WorkbenchScheduledRun.TaskId == task.TaskId,
                    WorkbenchScheduledRun.Status.in_(cls.RUNNABLE_STATUSES),
                )
                .values(Status="cancelled", FinishedAt=now)
            )
            cls._audit(db, task=task, action="delete", result="allowed")
            await db.commit()
            WORKBENCH_METRICS.inc("workbench_scheduled_task_actions_total", action="delete")

    @classmethod
    async def list_owned(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> list[WorkbenchScheduledTask]:
        cls.require_enabled()
        return (
            await db.execute(
                select(WorkbenchScheduledTask)
                .where(
                    *cls._owner_filters(account_id, identity, app_context),
                    WorkbenchScheduledTask.Status != "deleted",
                )
                .order_by(WorkbenchScheduledTask.CreatedAt.desc())
                .limit(tagentic_config.WORKBENCH_SCHEDULE_MAX_TASKS_PER_USER)
            )
        ).scalars().all()

    @classmethod
    async def get_owned(
        cls,
        db: AsyncSession,
        *,
        task_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> WorkbenchScheduledTask | None:
        cls.require_enabled()
        task = await cls._owned_task(db, task_id, account_id, identity, app_context)
        return task if task is not None and task.Status != "deleted" else None

    @classmethod
    async def list_runs(
        cls,
        db: AsyncSession,
        *,
        task_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        limit: int,
    ) -> list[WorkbenchScheduledRun]:
        task = await cls.get_owned(
            db,
            task_id=task_id,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
        )
        if task is None:
            raise WorkbenchScheduledError("scheduled task not found", 404)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise WorkbenchScheduledError("limit must be between 1 and 100")
        return (
            await db.execute(
                select(WorkbenchScheduledRun)
                .where(WorkbenchScheduledRun.TaskId == task.TaskId)
                .order_by(WorkbenchScheduledRun.ScheduledFor.desc())
                .limit(limit)
            )
        ).scalars().all()

    @classmethod
    async def run_now(
        cls,
        db: AsyncSession,
        *,
        task_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        session_claims: dict[str, Any],
    ) -> WorkbenchScheduledRun:
        cls.require_enabled()
        await cls.require_recent_reauthentication(
            db,
            account_id=account_id,
            identity=identity,
            session_claims=session_claims,
        )
        task = await cls._owned_task(
            db, task_id, account_id, identity, app_context, lock=True
        )
        if task is None or task.Status == "deleted":
            raise WorkbenchScheduledError("scheduled task not found", 404)
        if task.Status != "active":
            raise WorkbenchScheduledError("scheduled task is not active", 409)
        delegation = (
            await db.execute(
                select(WorkbenchScheduledDelegation)
                .where(WorkbenchScheduledDelegation.TaskId == task.TaskId)
                .order_by(WorkbenchScheduledDelegation.CapabilityVersion.desc())
                .limit(1)
            )
        ).scalar()
        if not cls._delegation_valid(task, delegation):
            raise WorkbenchScheduledError("scheduled task delegation is expired", 409)
        WorkbenchPolicy.validate_scheduled_turn(
            identity,
            app_context,
            task_max_runtime_seconds=task.MaxRuntimeSeconds,
        )
        now = datetime.now(UTC).replace(tzinfo=None)
        if await cls._daily_limit_reached(db, task, now):
            raise WorkbenchScheduledError("scheduled task daily run limit reached", 429)
        nonce = uuid.uuid4().hex
        run = WorkbenchScheduledRun(
            RunId=f"wsr_{uuid.uuid4().hex}",
            TaskId=task.TaskId,
            IdempotencyKey=hashlib.sha256(
                f"{task.TaskId}:manual:{nonce}".encode()
            ).hexdigest(),
            ScheduledFor=now,
            Status="queued",
            NextAttemptAt=now,
        )
        db.add(run)
        cls._audit(
            db,
            task=task,
            action="run_now",
            result="allowed",
            run_id=run.RunId,
        )
        await db.commit()
        WORKBENCH_METRICS.inc(
            "workbench_scheduled_materialized_total", result="queued"
        )
        WORKBENCH_METRICS.inc(
            "workbench_scheduled_task_actions_total", action="run_now"
        )
        return run

    @classmethod
    async def _validated_definition(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        payload: Any,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise WorkbenchScheduledError("request body must be an object")
        allowed = {
            "name",
            "prompt",
            "attachment_ids",
            "conversation_id",
            "schedule_kind",
            "cron_expression",
            "timezone",
            "once_at",
            "misfire_policy",
            "max_runtime_seconds",
            "daily_run_limit",
            "max_retries",
            "retry_backoff_seconds",
        }
        unknown = set(payload).difference(allowed)
        if unknown:
            raise WorkbenchScheduledError(
                "request contains unsupported fields: " + ", ".join(sorted(unknown))
            )
        name = cls._text(payload.get("name"), "name", 1, 120)
        prompt = cls._text(
            payload.get("prompt"),
            "prompt",
            1,
            tagentic_config.WORKBENCH_SCHEDULE_MAX_PROMPT_CHARS,
            allow_newlines=True,
        )
        attachment_ids = payload.get("attachment_ids", [])
        if (
            not isinstance(attachment_ids, list)
            or len(attachment_ids) > tagentic_config.WORKBENCH_SCHEDULE_MAX_ATTACHMENTS
            or any(
                not isinstance(value, str)
                or not value.startswith("wf_")
                or len(value) > 64
                for value in attachment_ids
            )
            or len(set(attachment_ids)) != len(attachment_ids)
        ):
            raise WorkbenchScheduledError("attachment_ids is invalid")
        conversation_id = payload.get("conversation_id") or None
        if conversation_id is not None:
            try:
                conversation_id = str(uuid.UUID(str(conversation_id)))
            except (TypeError, ValueError, AttributeError) as error:
                raise WorkbenchScheduledError("conversation_id must be a UUID") from error
            conversation = await CoreConversation.get_owned(
                db,
                account_id,
                app_context.application_id,
                conversation_id,
                workbench_identity=identity,
                workbench_app_context=app_context,
            )
            if conversation is None:
                raise WorkbenchScheduledError("conversation not found", 404)
        if attachment_ids and conversation_id is None:
            raise WorkbenchScheduledError(
                "conversation_id is required when attachment_ids are configured"
            )
        if attachment_ids:
            WorkbenchPolicy.require_capability(app_context, "files")
            await WorkbenchFileOwnership.prepare_chat_contents(
                db,
                contents=[
                    {"Type": "file", "File": {"WorkbenchFileId": file_id}}
                    for file_id in attachment_ids
                ],
                account_id=account_id,
                identity=identity,
                app_context=app_context,
                conversation_id=conversation_id,
            )

        max_runtime = cls._integer(
            payload.get("max_runtime_seconds"), "max_runtime_seconds", 30, 86400
        )
        limits = WorkbenchPolicy.validate_scheduled_turn(
            identity,
            app_context,
            task_max_runtime_seconds=max_runtime,
        )
        max_runtime = min(max_runtime, limits["max_runtime_seconds"])
        daily_limit = cls._integer(
            payload.get("daily_run_limit"),
            "daily_run_limit",
            1,
            tagentic_config.WORKBENCH_SCHEDULE_MAX_DAILY_RUNS,
        )
        max_retries = cls._integer(payload.get("max_retries", 1), "max_retries", 0, 3)
        retry_backoff = cls._integer(
            payload.get("retry_backoff_seconds", 30),
            "retry_backoff_seconds",
            5,
            3600,
        )
        timezone_name = cls._text(payload.get("timezone"), "timezone", 1, 64)
        try:
            timezone = pytz.timezone(timezone_name)
        except UnknownTimeZoneError as error:
            raise WorkbenchScheduledError("timezone must be an IANA timezone") from error
        if timezone_name not in pytz.all_timezones_set:
            raise WorkbenchScheduledError("timezone must be an IANA timezone")
        schedule_kind = payload.get("schedule_kind")
        if not isinstance(schedule_kind, str) or schedule_kind not in {"cron", "once"}:
            raise WorkbenchScheduledError("schedule_kind must be cron or once")
        cron_expression = None
        once_at = None
        now = datetime.now(UTC).replace(tzinfo=None)
        if schedule_kind == "cron":
            supplied_once = payload.get("once_at")
            if supplied_once is not None and supplied_once != "":
                raise WorkbenchScheduledError("once_at is not valid for a cron schedule")
            cron = WorkbenchCron(payload.get("cron_expression"), timezone_name)
            cron_expression = cron.expression
            next_run = cron.next_after(now)
            second = cron.next_after(next_run)
            minimum = timedelta(
                minutes=tagentic_config.WORKBENCH_SCHEDULE_MIN_INTERVAL_MINUTES
            )
            if second - next_run < minimum:
                raise WorkbenchScheduledError("cron schedule is too frequent")
        else:
            supplied_cron = payload.get("cron_expression")
            if supplied_cron is not None and supplied_cron != "":
                raise WorkbenchScheduledError("cron_expression is not valid for a one-time schedule")
            raw_once = payload.get("once_at")
            if not isinstance(raw_once, str) or len(raw_once) > 64:
                raise WorkbenchScheduledError("once_at must be an ISO-8601 timestamp with an offset")
            try:
                parsed_once = datetime.fromisoformat(raw_once.replace("Z", "+00:00"))
            except ValueError as error:
                raise WorkbenchScheduledError("once_at is invalid") from error
            if parsed_once.tzinfo is None:
                raise WorkbenchScheduledError("once_at must include a UTC offset")
            once_at = _naive_utc(parsed_once)
            if once_at < now + timedelta(minutes=1):
                raise WorkbenchScheduledError("once_at must be at least one minute in the future")
            if once_at > now + timedelta(days=tagentic_config.WORKBENCH_SCHEDULE_DELEGATION_DAYS):
                raise WorkbenchScheduledError("once_at exceeds the delegation lifetime")
            # Validate that the supplied IANA timezone is usable without trusting
            # any browser-derived customer scope.
            _ = _aware_utc(once_at).astimezone(timezone)
            next_run = once_at
        misfire_policy = payload.get("misfire_policy", "skip")
        if not isinstance(misfire_policy, str) or misfire_policy not in {"skip", "fire_once"}:
            raise WorkbenchScheduledError("misfire_policy must be skip or fire_once")

        agent = await CoreAgent.get(db, account_id, app_context.application_id)
        if agent is None or not str(agent.AgentId or "").strip():
            raise WorkbenchScheduledError("workbench Agent is not provisioned", 409)
        binding = (
            await db.execute(
                select(WorkbenchAgentBinding).where(
                    WorkbenchAgentBinding.BindingId == identity.binding_id,
                    WorkbenchAgentBinding.AccountId == account_id,
                    WorkbenchAgentBinding.ApplicationId == app_context.application_id,
                    WorkbenchAgentBinding.AgentId == agent.AgentId,
                    WorkbenchAgentBinding.Status == "active",
                )
            )
        ).scalar()
        if binding is None:
            raise WorkbenchScheduledError("workbench Agent binding is not active", 409)
        snapshot_value = {
            "capabilities": sorted(WorkbenchPolicy.capabilities(app_context)),
            "limits": limits,
            "app_profile_id": str(app_context.app_profile_id),
            "config_version": app_context.config_version,
            "max_runtime_seconds": max_runtime,
            "daily_run_limit": daily_limit,
        }
        plan_snapshot = _canonical(snapshot_value)
        return {
            "name": name,
            "prompt": prompt,
            "attachment_ids": attachment_ids,
            "conversation_id": conversation_id,
            "schedule_kind": schedule_kind,
            "cron_expression": cron_expression,
            "timezone": timezone_name,
            "once_at": once_at,
            "next_run_at": next_run,
            "misfire_policy": misfire_policy,
            "max_runtime_seconds": max_runtime,
            "daily_run_limit": daily_limit,
            "max_retries": max_retries,
            "retry_backoff_seconds": retry_backoff,
            "plan_snapshot": plan_snapshot,
            "plan_snapshot_hash": hashlib.sha256(plan_snapshot.encode()).hexdigest(),
            "agent_id": str(agent.AgentId),
        }

    @staticmethod
    def _text(
        value: Any,
        field: str,
        minimum: int,
        maximum: int,
        *,
        allow_newlines: bool = False,
    ) -> str:
        if not isinstance(value, str):
            raise WorkbenchScheduledError(f"{field} must be a string")
        normalized = value.strip()
        if not minimum <= len(normalized) <= maximum or "\x00" in normalized:
            raise WorkbenchScheduledError(f"{field} is invalid")
        if not allow_newlines and any(ord(character) < 32 for character in normalized):
            raise WorkbenchScheduledError(f"{field} contains control characters")
        return normalized

    @staticmethod
    def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise WorkbenchScheduledError(
                f"{field} must be an integer between {minimum} and {maximum}"
            )
        return value

    @staticmethod
    def _owner_filters(
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> tuple:
        return (
            WorkbenchScheduledTask.BindingId == identity.binding_id,
            WorkbenchScheduledTask.AccountId == account_id,
            WorkbenchScheduledTask.CustomerId == identity.customer_id,
            WorkbenchScheduledTask.NewApiUserId == identity.new_api_user_id,
            WorkbenchScheduledTask.ApplicationId == app_context.application_id,
            WorkbenchScheduledTask.AppProfileId == str(app_context.app_profile_id),
        )

    @classmethod
    async def _owned_task(
        cls,
        db: AsyncSession,
        task_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        *,
        lock: bool = False,
    ) -> WorkbenchScheduledTask | None:
        if not isinstance(task_id, str) or not task_id.startswith("wst_") or len(task_id) > 64:
            return None
        statement = select(WorkbenchScheduledTask).where(
            WorkbenchScheduledTask.TaskId == task_id,
            *cls._owner_filters(account_id, identity, app_context),
        )
        if lock:
            statement = statement.with_for_update()
        return (await db.execute(statement)).scalar()

    @classmethod
    def _new_delegation(
        cls,
        task: WorkbenchScheduledTask,
        *,
        capability_version: int,
        active: bool = True,
    ) -> WorkbenchScheduledDelegation:
        plaintext = secrets.token_urlsafe(32)
        return WorkbenchScheduledDelegation(
            DelegationId=f"wsd_{uuid.uuid4().hex}",
            TaskId=task.TaskId,
            CapabilityVersion=capability_version,
            CapabilityDigest=hashlib.sha256(plaintext.encode()).hexdigest(),
            AuthEpoch=task.AuthEpoch,
            ConfigVersion=task.ConfigVersion,
            PlanSnapshotHash=task.PlanSnapshotHash,
            Status="active" if active else "paused",
            ExpiresAt=datetime.now(UTC).replace(tzinfo=None)
            + timedelta(days=tagentic_config.WORKBENCH_SCHEDULE_DELEGATION_DAYS),
        )

    @classmethod
    async def _rotate_delegation(
        cls,
        db: AsyncSession,
        task: WorkbenchScheduledTask,
        *,
        activate: bool,
    ) -> None:
        existing = (
            await db.execute(
                select(WorkbenchScheduledDelegation)
                .where(WorkbenchScheduledDelegation.TaskId == task.TaskId)
                .order_by(WorkbenchScheduledDelegation.CapabilityVersion.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar()
        version = int(existing.CapabilityVersion) + 1 if existing is not None else 1
        if existing is not None:
            existing.Status = "revoked"
            existing.RevokedAt = datetime.now(UTC).replace(tzinfo=None)
            db.add(existing)
        db.add(cls._new_delegation(task, capability_version=version, active=activate))

    @staticmethod
    async def _revoke_delegation(db: AsyncSession, task_id: str) -> None:
        now = datetime.now(UTC).replace(tzinfo=None)
        await db.execute(
            update(WorkbenchScheduledDelegation)
            .where(
                WorkbenchScheduledDelegation.TaskId == task_id,
                WorkbenchScheduledDelegation.Status != "revoked",
            )
            .values(Status="revoked", RevokedAt=now)
        )

    @staticmethod
    def _audit(
        db: AsyncSession,
        *,
        task: WorkbenchScheduledTask,
        action: str,
        result: str,
        run_id: str | None = None,
        detail_code: str | None = None,
    ) -> None:
        db.add(
            WorkbenchScheduledAudit(
                EventId=f"wsa_{uuid.uuid4().hex}",
                TaskId=task.TaskId,
                RunId=run_id,
                BindingId=task.BindingId,
                CustomerId=task.CustomerId,
                ApplicationId=task.ApplicationId,
                Action=action[:32],
                Result=result[:16],
                DetailCode=str(detail_code or "")[:64] or None,
                OutboxStatus="pending",
            )
        )

    @staticmethod
    def project_task(task: WorkbenchScheduledTask, *, include_prompt: bool) -> dict[str, Any]:
        value = {
            "task_id": task.TaskId,
            "name": task.Name,
            "status": task.Status,
            "schedule_kind": task.ScheduleKind,
            "cron_expression": task.CronExpression,
            "timezone": task.Timezone,
            "once_at": task.OnceAt.isoformat() + "Z" if task.OnceAt else None,
            "next_run_at": task.NextRunAt.isoformat() + "Z" if task.NextRunAt else None,
            "misfire_policy": task.MisfirePolicy,
            "max_runtime_seconds": task.MaxRuntimeSeconds,
            "daily_run_limit": task.DailyRunLimit,
            "max_retries": task.MaxRetries,
            "retry_backoff_seconds": task.RetryBackoffSeconds,
            "conversation_id": str(task.ConversationId) if task.ConversationId else None,
            "attachment_ids": json.loads(task.AttachmentIdsJson),
            "version": task.Version,
            "created_at": task.CreatedAt.isoformat() + "Z" if task.CreatedAt else None,
            "updated_at": task.UpdatedAt.isoformat() + "Z" if task.UpdatedAt else None,
        }
        if include_prompt:
            value["prompt"] = task.Prompt
        return value

    @staticmethod
    def project_run(run: WorkbenchScheduledRun) -> dict[str, Any]:
        return {
            "run_id": run.RunId,
            "task_id": run.TaskId,
            "scheduled_for": run.ScheduledFor.isoformat() + "Z",
            "status": run.Status,
            "attempt_count": run.AttemptCount,
            "turn_id": run.TurnId,
            "error_code": run.ErrorCode,
            "started_at": run.StartedAt.isoformat() + "Z" if run.StartedAt else None,
            "finished_at": run.FinishedAt.isoformat() + "Z" if run.FinishedAt else None,
        }

    @staticmethod
    def _definition_payload(task: WorkbenchScheduledTask) -> dict[str, Any]:
        return {
            "name": task.Name,
            "prompt": task.Prompt,
            "attachment_ids": json.loads(task.AttachmentIdsJson),
            "conversation_id": str(task.ConversationId) if task.ConversationId else None,
            "schedule_kind": task.ScheduleKind,
            "cron_expression": task.CronExpression,
            "timezone": task.Timezone,
            "once_at": task.OnceAt.replace(tzinfo=UTC).isoformat() if task.OnceAt else None,
            "misfire_policy": task.MisfirePolicy,
            "max_runtime_seconds": task.MaxRuntimeSeconds,
            "daily_run_limit": task.DailyRunLimit,
            "max_retries": task.MaxRetries,
            "retry_backoff_seconds": task.RetryBackoffSeconds,
        }

    @staticmethod
    def _next_for_task(task: WorkbenchScheduledTask, after: datetime) -> datetime | None:
        if task.ScheduleKind == "once":
            return task.OnceAt if task.OnceAt and task.OnceAt > after else None
        return WorkbenchCron(task.CronExpression, task.Timezone).next_after(after)

    @classmethod
    async def materialize_due(cls, now: datetime | None = None) -> int:
        cls.require_enabled()
        now = now or datetime.now(UTC).replace(tzinfo=None)
        created = 0
        async with db_connection() as db:
            tasks = (
                await db.execute(
                    select(WorkbenchScheduledTask)
                    .where(
                        WorkbenchScheduledTask.Status == "active",
                        WorkbenchScheduledTask.NextRunAt.is_not(None),
                        WorkbenchScheduledTask.NextRunAt <= now,
                    )
                    .order_by(WorkbenchScheduledTask.NextRunAt.asc())
                    .limit(tagentic_config.WORKBENCH_SCHEDULE_BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            for task in tasks:
                delegation = (
                    await db.execute(
                        select(WorkbenchScheduledDelegation)
                        .where(WorkbenchScheduledDelegation.TaskId == task.TaskId)
                        .order_by(WorkbenchScheduledDelegation.CapabilityVersion.desc())
                        .limit(1)
                    )
                ).scalar()
                if not cls._delegation_valid(task, delegation):
                    task.Status = "paused"
                    task.NextRunAt = None
                    db.add(task)
                    cls._audit(
                        db,
                        task=task,
                        action="expire",
                        result="denied",
                        detail_code="DelegationExpired",
                    )
                    continue
                scheduled_for = task.NextRunAt
                lag = max(0.0, (now - scheduled_for).total_seconds())
                if (
                    task.MisfirePolicy == "skip"
                    and lag > tagentic_config.WORKBENCH_SCHEDULE_MISFIRE_GRACE_SECONDS
                ):
                    run_status = "skipped_misfire"
                elif await cls._daily_limit_reached(db, task, scheduled_for):
                    run_status = "skipped_limit"
                else:
                    run_status = "queued"
                idempotency = hashlib.sha256(
                    f"{task.TaskId}:{scheduled_for.isoformat()}".encode()
                ).hexdigest()
                existing = (
                    await db.execute(
                        select(WorkbenchScheduledRun.Id).where(
                            WorkbenchScheduledRun.IdempotencyKey == idempotency
                        )
                    )
                ).first()
                if existing is None:
                    run = WorkbenchScheduledRun(
                        RunId=f"wsr_{uuid.uuid4().hex}",
                        TaskId=task.TaskId,
                        IdempotencyKey=idempotency,
                        ScheduledFor=scheduled_for,
                        Status=run_status,
                        NextAttemptAt=now,
                        FinishedAt=now if run_status.startswith("skipped_") else None,
                    )
                    db.add(run)
                    cls._audit(
                        db,
                        task=task,
                        action="materialize",
                        result="allowed" if run_status == "queued" else "denied",
                        run_id=run.RunId,
                        detail_code=run_status,
                    )
                    created += 1
                    WORKBENCH_METRICS.inc(
                        "workbench_scheduled_materialized_total",
                        result="queued" if run_status == "queued" else "skipped",
                    )
                if task.ScheduleKind == "once":
                    task.NextRunAt = None
                    if run_status.startswith("skipped_"):
                        task.Status = "completed"
                else:
                    # fire_once intentionally advances from now and never replays
                    # every missed tick after an outage.
                    task.NextRunAt = cls._next_for_task(task, max(now, scheduled_for))
                db.add(task)
            await db.commit()
        return created

    @staticmethod
    async def _daily_limit_reached(
        db: AsyncSession,
        task: WorkbenchScheduledTask,
        scheduled_for: datetime,
    ) -> bool:
        timezone = pytz.timezone(task.Timezone)
        local = _aware_utc(scheduled_for).astimezone(timezone)
        local_start = timezone.localize(
            datetime(local.year, local.month, local.day), is_dst=None
        )
        next_date = local.date() + timedelta(days=1)
        local_end = timezone.localize(
            datetime(next_date.year, next_date.month, next_date.day), is_dst=None
        )
        start = local_start.astimezone(UTC).replace(tzinfo=None)
        end = local_end.astimezone(UTC).replace(tzinfo=None)
        count = (
            await db.execute(
                select(func.count(WorkbenchScheduledRun.Id)).where(
                    WorkbenchScheduledRun.TaskId == task.TaskId,
                    WorkbenchScheduledRun.ScheduledFor >= start,
                    WorkbenchScheduledRun.ScheduledFor < end,
                    WorkbenchScheduledRun.Status.not_in(
                        {"skipped_misfire", "skipped_limit", "cancelled", "revoked"}
                    ),
                )
            )
        ).scalar_one()
        return count >= task.DailyRunLimit

    @classmethod
    async def claim_due(cls, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(UTC).replace(tzinfo=None)
        lease_until = now + timedelta(seconds=tagentic_config.WORKBENCH_SCHEDULE_LEASE_SECONDS)
        claimed: list[str] = []
        async with db_connection() as db:
            expired = (
                await db.execute(
                    select(WorkbenchScheduledRun)
                    .where(
                        WorkbenchScheduledRun.Status.in_({"leased", "executing"}),
                        WorkbenchScheduledRun.LeaseUntil <= now,
                    )
                    .limit(tagentic_config.WORKBENCH_SCHEDULE_BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            for run in expired:
                task = (
                    await db.execute(
                        select(WorkbenchScheduledTask).where(
                            WorkbenchScheduledTask.TaskId == run.TaskId
                        )
                    )
                ).scalar()
                if run.ProviderStarted or run.TurnId:
                    run.Status = "provider_unknown"
                    run.ErrorCode = "LeaseExpiredAfterSubmission"
                    run.FinishedAt = now
                elif task is not None and run.AttemptCount <= task.MaxRetries:
                    run.Status = "retrying"
                    run.NextAttemptAt = now + timedelta(
                        seconds=task.RetryBackoffSeconds
                        * max(1, 2 ** max(0, run.AttemptCount - 1))
                    )
                else:
                    run.Status = "failed_before_accept"
                    run.ErrorCode = "LeaseExpired"
                    run.FinishedAt = now
                run.LeaseOwner = None
                run.LeaseUntil = None
                db.add(run)
            runs = (
                await db.execute(
                    select(WorkbenchScheduledRun)
                    .where(
                        WorkbenchScheduledRun.Status.in_(cls.RUNNABLE_STATUSES),
                        WorkbenchScheduledRun.NextAttemptAt <= now,
                        or_(
                            WorkbenchScheduledRun.LeaseUntil.is_(None),
                            WorkbenchScheduledRun.LeaseUntil <= now,
                        ),
                    )
                    .order_by(WorkbenchScheduledRun.NextAttemptAt.asc())
                    .limit(tagentic_config.WORKBENCH_SCHEDULE_BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            for run in runs:
                task = (
                    await db.execute(
                        select(WorkbenchScheduledTask).where(
                            WorkbenchScheduledTask.TaskId == run.TaskId
                        )
                    )
                ).scalar()
                if task is None or task.Status != "active":
                    run.Status = "revoked"
                    run.ErrorCode = "TaskNotActive"
                    run.FinishedAt = now
                    db.add(run)
                    continue
                run.Status = "leased"
                run.AttemptCount += 1
                run.LeaseOwner = cls.instance_id
                run.LeaseUntil = lease_until
                run.StartedAt = run.StartedAt or now
                db.add(run)
                claimed.append(run.RunId)
            await db.commit()
        return claimed

    @classmethod
    async def execute_run(cls, run_id: str) -> None:
        heartbeat: asyncio.Task | None = None
        try:
            async with db_connection() as db:
                run = (
                    await db.execute(
                        select(WorkbenchScheduledRun)
                        .where(
                            WorkbenchScheduledRun.RunId == run_id,
                            WorkbenchScheduledRun.Status == "leased",
                            WorkbenchScheduledRun.LeaseOwner == cls.instance_id,
                        )
                        .with_for_update()
                    )
                ).scalar()
                if run is None:
                    return
                task = (
                    await db.execute(
                        select(WorkbenchScheduledTask).where(
                            WorkbenchScheduledTask.TaskId == run.TaskId
                        )
                    )
                ).scalar()
                delegation = (
                    await db.execute(
                    select(WorkbenchScheduledDelegation).where(
                        WorkbenchScheduledDelegation.TaskId == run.TaskId
                    ).order_by(WorkbenchScheduledDelegation.CapabilityVersion.desc()).limit(1)
                    )
                ).scalar()
                if not cls._delegation_valid(task, delegation):
                    await cls._finish_locked(
                        db, run, task, "revoked", "DelegationRevoked"
                    )
                    return
                run.Status = "executing"
                db.add(run)
                await db.commit()
                task_scope = cls._scope_snapshot(task)
                attempt = run.AttemptCount

            heartbeat = asyncio.create_task(cls._heartbeat(run_id))
            identity, app_context = await cls._authorize_scope(task_scope)
            limits = WorkbenchPolicy.validate_scheduled_turn(
                identity,
                app_context,
                task_max_runtime_seconds=task_scope["max_runtime_seconds"],
            )
            expected_snapshot = cls._plan_snapshot(
                app_context,
                limits,
                task_scope["max_runtime_seconds"],
                task_scope["daily_run_limit"],
            )
            if hashlib.sha256(expected_snapshot.encode()).hexdigest() != task_scope["plan_snapshot_hash"]:
                raise WorkbenchScheduledError("scheduled task plan snapshot changed", 409)

            async with db_connection() as db:
                agent_binding = (
                    await db.execute(
                        select(WorkbenchAgentBinding).where(
                            WorkbenchAgentBinding.BindingId == task_scope["binding_id"],
                            WorkbenchAgentBinding.AccountId == task_scope["account_id"],
                            WorkbenchAgentBinding.ApplicationId == task_scope["application_id"],
                            WorkbenchAgentBinding.AgentId == task_scope["agent_id"],
                            WorkbenchAgentBinding.Status == "active",
                        )
                    )
                ).scalar()
                if agent_binding is None:
                    raise WorkbenchScheduledError("scheduled task Agent binding is revoked", 409)
                contents: list[dict[str, Any]] = [
                    {"Type": "text", "Text": task_scope["prompt"]}
                ]
                contents.extend(
                    {"Type": "file", "File": {"WorkbenchFileId": file_id}}
                    for file_id in task_scope["attachment_ids"]
                )
                prepared_contents = await WorkbenchFileOwnership.prepare_chat_contents(
                    db,
                    contents=contents,
                    account_id=task_scope["account_id"],
                    identity=identity,
                    app_context=app_context,
                    conversation_id=task_scope["conversation_id"],
                )
                client_request_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:attempt:{attempt}")
                )
                request_digest = WorkbenchTurnManager.request_digest(
                    {
                        "Contents": contents,
                        "ConversationId": task_scope["conversation_id"],
                        "ApplicationId": task_scope["application_id"],
                        "SearchNetwork": False,
                        "IsChannel": False,
                        "ScheduledRunId": run_id,
                    }
                )
                if tagentic_config.WORKBENCH_INTEGRATIONS_ENABLED:
                    await WorkbenchIntegrations.revalidate_turn(
                        db,
                        account_id=task_scope["account_id"],
                        identity=identity,
                        app_context=app_context,
                    )
                submission = await WorkbenchTurnManager.create_or_get(
                    db,
                    account_id=task_scope["account_id"],
                    identity=identity,
                    app_context=app_context,
                    client_request_id=client_request_id,
                    request_digest=request_digest,
                    conversation_id=task_scope["conversation_id"],
                )
                await db.execute(
                    update(WorkbenchScheduledRun)
                    .where(
                        WorkbenchScheduledRun.RunId == run_id,
                        WorkbenchScheduledRun.Status == "executing",
                        WorkbenchScheduledRun.LeaseOwner == cls.instance_id,
                    )
                    .values(TurnId=submission.turn_id, ProviderStarted=True)
                )
                await db.commit()

            async def offline_reauthorize() -> None:
                await cls._authorize_scope(task_scope)

            vendor_app = TAgenticApp.get_app().get_vendor_app(task_scope["application_id"])
            await WorkbenchTurnManager.execute(
                turn_id=submission.turn_id,
                vendor_app=vendor_app,
                account_id=task_scope["account_id"],
                identity=identity,
                app_context=app_context,
                claims={},
                contents=prepared_contents,
                conversation_id=task_scope["conversation_id"],
                search_network=False,
                custom_variables={},
                limits=limits,
                offline_reauthorize=offline_reauthorize,
            )
            async with db_connection() as db:
                turn = (
                    await db.execute(
                        select(WorkbenchTurn).where(WorkbenchTurn.TurnId == submission.turn_id)
                    )
                ).scalar()
                status = turn.Status if turn is not None else "provider_unknown"
                await cls._settle_attempt(db, run_id, status, task_scope)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            async with db_connection() as db:
                await cls._settle_attempt(
                    db,
                    run_id,
                    "failed_before_accept",
                    None,
                    error_code=type(error).__name__,
                )
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    @classmethod
    async def _authorize_scope(
        cls, scope: dict[str, Any]
    ) -> tuple[WorkbenchIdentityContext, WorkbenchAppContext]:
        async with db_connection() as db:
            task = (
                await db.execute(
                    select(WorkbenchScheduledTask).where(
                        WorkbenchScheduledTask.TaskId == scope["task_id"],
                        WorkbenchScheduledTask.Status == "active",
                        WorkbenchScheduledTask.Version == scope["version"],
                    )
                )
            ).scalar()
            delegation = (
                await db.execute(
                    select(WorkbenchScheduledDelegation).where(
                        WorkbenchScheduledDelegation.TaskId == scope["task_id"]
                    ).order_by(WorkbenchScheduledDelegation.CapabilityVersion.desc()).limit(1)
                )
            ).scalar()
            if not cls._delegation_valid(task, delegation):
                raise WorkbenchScheduledError("scheduled task delegation is revoked", 403)
            identity, app_context = await CoreWorkbenchIdentity.authorize_offline_scope(
                db,
                account_id=scope["account_id"],
                binding_id=scope["binding_id"],
                canonical_subject=scope["canonical_subject"],
                customer_id=scope["customer_id"],
                new_api_user_id=scope["new_api_user_id"],
                auth_epoch=scope["auth_epoch"],
                application_id=scope["application_id"],
                app_profile_id=scope["app_profile_id"],
                config_version=scope["config_version"],
            )
            if app_context.app_id != scope["provider_app_id"]:
                raise WorkbenchScheduledError("scheduled task provider App changed", 409)
            return identity, app_context

    @staticmethod
    def _delegation_valid(task, delegation) -> bool:
        now = datetime.now(UTC).replace(tzinfo=None)
        return bool(
            task is not None
            and task.Status == "active"
            and delegation is not None
            and delegation.Status == "active"
            and delegation.ExpiresAt > now
            and delegation.AuthEpoch == task.AuthEpoch
            and delegation.ConfigVersion == task.ConfigVersion
            and delegation.PlanSnapshotHash == task.PlanSnapshotHash
            and len(delegation.CapabilityDigest or "") == 64
        )

    @staticmethod
    def _scope_snapshot(task: WorkbenchScheduledTask) -> dict[str, Any]:
        return {
            "task_id": task.TaskId,
            "version": task.Version,
            "binding_id": task.BindingId,
            "account_id": str(task.AccountId),
            "customer_id": task.CustomerId,
            "new_api_user_id": task.NewApiUserId,
            "canonical_subject": task.CanonicalSubject,
            "application_id": task.ApplicationId,
            "provider_app_id": task.ProviderAppId,
            "app_profile_id": task.AppProfileId,
            "config_version": task.ConfigVersion,
            "auth_epoch": task.AuthEpoch,
            "agent_id": task.AgentId,
            "conversation_id": str(task.ConversationId) if task.ConversationId else None,
            "prompt": task.Prompt,
            "attachment_ids": json.loads(task.AttachmentIdsJson),
            "max_runtime_seconds": task.MaxRuntimeSeconds,
            "daily_run_limit": task.DailyRunLimit,
            "plan_snapshot_hash": task.PlanSnapshotHash,
        }

    @staticmethod
    def _plan_snapshot(
        app_context: WorkbenchAppContext,
        limits: dict[str, int],
        max_runtime_seconds: int,
        daily_run_limit: int,
    ) -> str:
        return _canonical(
            {
                "capabilities": sorted(WorkbenchPolicy.capabilities(app_context)),
                "limits": limits,
                "app_profile_id": str(app_context.app_profile_id),
                "config_version": app_context.config_version,
                "max_runtime_seconds": min(max_runtime_seconds, limits["max_runtime_seconds"]),
                "daily_run_limit": daily_run_limit,
            }
        )

    @classmethod
    async def _settle_attempt(
        cls,
        db: AsyncSession,
        run_id: str,
        turn_status: str,
        scope: dict[str, Any] | None,
        *,
        error_code: str | None = None,
    ) -> None:
        run = (
            await db.execute(
                select(WorkbenchScheduledRun)
                .where(
                    WorkbenchScheduledRun.RunId == run_id,
                    WorkbenchScheduledRun.LeaseOwner == cls.instance_id,
                )
                .with_for_update()
            )
        ).scalar()
        if run is None or run.Status not in {"leased", "executing"}:
            return
        task = (
            await db.execute(
                select(WorkbenchScheduledTask).where(
                    WorkbenchScheduledTask.TaskId == run.TaskId
                )
            )
        ).scalar()
        now = datetime.now(UTC).replace(tzinfo=None)
        if error_code and run.ProviderStarted:
            # An exception escaping after the durable submission boundary is
            # ambiguous even if no Turn status was recovered. Never retry it.
            turn_status = "provider_unknown"
        if (
            turn_status == "failed_before_accept"
            and task is not None
            and task.Status == "active"
            and run.AttemptCount <= task.MaxRetries
        ):
            run.Status = "retrying"
            run.ProviderStarted = False
            run.TurnId = None
            run.NextAttemptAt = now + timedelta(
                seconds=min(
                    86400,
                    task.RetryBackoffSeconds * max(1, 2 ** (run.AttemptCount - 1)),
                )
            )
            result = "retry"
        else:
            run.Status = (
                turn_status
                if turn_status in {
                    "completed",
                    "failed_before_accept",
                    "failed_after_accept",
                    "provider_unknown",
                    "cancel_confirmed",
                }
                else "provider_unknown"
            )
            if run.Status == "cancel_confirmed":
                run.Status = "cancelled"
            run.FinishedAt = now
            result = run.Status
            if task is not None and task.ScheduleKind == "once" and run.Status == "completed":
                task.Status = "completed"
                db.add(task)
        run.ErrorCode = str(error_code or "")[:64] or (
            None if run.Status == "completed" else str(turn_status)[:64]
        )
        run.LeaseOwner = None
        run.LeaseUntil = None
        db.add(run)
        if task is not None:
            cls._audit(
                db,
                task=task,
                action="execute",
                result="allowed" if result == "completed" else "denied",
                run_id=run.RunId,
                detail_code=result,
            )
        await db.commit()
        if result != "retry":
            WORKBENCH_METRICS.inc(
                "workbench_scheduled_runs_total",
                status=result if result in {"completed", "failed_before_accept", "failed_after_accept", "provider_unknown", "cancelled"} else "other",
            )

    @classmethod
    async def _finish_locked(
        cls,
        db: AsyncSession,
        run: WorkbenchScheduledRun,
        task: WorkbenchScheduledTask | None,
        status: str,
        error_code: str,
    ) -> None:
        run.Status = status
        run.ErrorCode = error_code[:64]
        run.LeaseOwner = None
        run.LeaseUntil = None
        run.FinishedAt = datetime.now(UTC).replace(tzinfo=None)
        db.add(run)
        if task is not None:
            cls._audit(
                db,
                task=task,
                action="execute",
                result="denied",
                run_id=run.RunId,
                detail_code=error_code,
            )
        await db.commit()

    @classmethod
    async def _heartbeat(cls, run_id: str) -> None:
        interval = max(10, tagentic_config.WORKBENCH_SCHEDULE_LEASE_SECONDS // 3)
        while True:
            await asyncio.sleep(interval)
            async with db_connection() as db:
                result = await db.execute(
                    update(WorkbenchScheduledRun)
                    .where(
                        WorkbenchScheduledRun.RunId == run_id,
                        WorkbenchScheduledRun.Status == "executing",
                        WorkbenchScheduledRun.LeaseOwner == cls.instance_id,
                    )
                    .values(
                        LeaseUntil=datetime.now(UTC).replace(tzinfo=None)
                        + timedelta(seconds=tagentic_config.WORKBENCH_SCHEDULE_LEASE_SECONDS)
                    )
                )
                await db.commit()
                if getattr(result, "rowcount", 0) != 1:
                    return

    @classmethod
    async def drain_audit_outbox(cls) -> int:
        delivered = 0
        now = datetime.now(UTC).replace(tzinfo=None)
        async with db_connection() as db:
            events = (
                await db.execute(
                    select(WorkbenchScheduledAudit)
                    .where(WorkbenchScheduledAudit.OutboxStatus == "pending")
                    .order_by(WorkbenchScheduledAudit.CreatedAt.asc())
                    .limit(tagentic_config.WORKBENCH_SCHEDULE_BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            for event in events:
                # Structured security log deliberately excludes prompt, files,
                # provider credentials and user display data.
                logging.info(
                    "[workbench_scheduled_audit] event=%s task=%s run=%s customer=%s app=%s action=%s result=%s detail=%s",
                    event.EventId,
                    event.TaskId,
                    event.RunId or "",
                    event.CustomerId,
                    event.ApplicationId,
                    event.Action,
                    event.Result,
                    event.DetailCode or "",
                )
                event.OutboxStatus = "delivered"
                event.DeliveredAt = now
                db.add(event)
                delivered += 1
            await db.commit()
        return delivered

    @classmethod
    async def worker_loop(cls) -> None:
        cls._stop.clear()
        while not cls._stop.is_set():
            try:
                await cls.materialize_due()
                run_ids = await cls.claim_due()
                if run_ids:
                    await asyncio.gather(
                        *(cls.execute_run(run_id) for run_id in run_ids),
                        return_exceptions=True,
                    )
                await cls.drain_audit_outbox()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                WORKBENCH_METRICS.inc(
                    "workbench_scheduled_worker_failures_total", stage="loop"
                )
                logging.warning(
                    "[workbench_scheduled_worker] iteration failed error_type=%s",
                    type(error).__name__,
                )
            try:
                await asyncio.wait_for(
                    cls._stop.wait(),
                    timeout=tagentic_config.WORKBENCH_SCHEDULE_WORKER_INTERVAL_SECONDS,
                )
            except TimeoutError:
                pass

    @classmethod
    def start_worker(cls) -> asyncio.Task:
        if cls._worker is not None and not cls._worker.done():
            return cls._worker
        cls._worker = asyncio.create_task(cls.worker_loop())
        return cls._worker

    @classmethod
    async def stop_worker(cls) -> None:
        cls._stop.set()
        worker = cls._worker
        cls._worker = None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
