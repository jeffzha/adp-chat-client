import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.workbench_scheduled as scheduled_module
from config import tagentic_config
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_control import WorkbenchControlClient, WorkbenchControlError
from core.workbench_identity import CoreWorkbenchIdentity, WorkbenchIdentityError
from core.workbench_scheduled import (
    WorkbenchCron,
    WorkbenchScheduledError,
    WorkbenchScheduledService,
)
from core.workbench_turn import WorkbenchTurnManager
from core.workbench_stream import WorkbenchStreamGuard
from model.agent import AgentConfig
from model.workbench import WorkbenchAgentBinding, WorkbenchBrowserSession, WorkbenchIdentity
from model.account import AccountRole, AccountStatus
from model.workbench_scheduled import (
    WorkbenchScheduledAudit,
    WorkbenchScheduledDelegation,
    WorkbenchScheduledRun,
    WorkbenchScheduledTask,
)
from core.workbench_csrf import valid_workbench_csrf


ACCOUNT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SESSION_ID = "s" * 43
AUTH_TIME = int(datetime.now(UTC).timestamp())


class _AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    def add(self, value):
        self.session.add(value)

    async def delete(self, value):
        self.session.delete(value)

    async def execute(self, statement, params=None):
        return self.session.execute(statement, params or {})

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def flush(self):
        self.session.flush()


def _identity(binding="binding-7", customer=7):
    return WorkbenchIdentityContext(
        binding_id=binding,
        canonical_subject=f"napi:prod:customer:{customer}:user:9",
        customer_id=customer,
        new_api_user_id=9,
        auth_epoch=3,
        display_name="User 9",
        application_id="customer-app-7",
        app_profile_id="17",
        access_mode="active",
        config_version=5,
    )


def _app_context(capabilities=("chat", "scheduled_tasks")):
    return WorkbenchAppContext(
        application_id="customer-app-7",
        app_profile_id="17",
        config_version=5,
        auth_epoch=3,
        vendor="Tencent",
        service_vendor="ChinaTencentCloud",
        app_id="provider-app-7",
        app_key="provider-key",
        space_id="space-7",
        template_agent_id="template-7",
        secret_id="secret-id",
        secret_key="secret-key",
        capabilities=capabilities,
        limits={
            "customer_concurrency": 5,
            "user_concurrency": 1,
            "max_runtime_seconds": 120,
            "max_reasoning_rounds": 20,
            "max_output_tokens": 8192,
            "web_search_per_turn": 0,
            "max_file_bytes": 1024,
        },
    )


def _session_claims(
    *,
    session_id=SESSION_ID,
    account_id=ACCOUNT_ID,
    identity=None,
    auth_time=AUTH_TIME,
):
    identity = identity or _identity()
    return {
        "AccountId": str(account_id),
        "BindingId": identity.binding_id,
        "Subject": identity.canonical_subject,
        "AuthEpoch": identity.auth_epoch,
        "ApplicationId": identity.application_id,
        "AppProfileId": identity.app_profile_id,
        "ConfigVersion": identity.config_version,
        "sid": session_id,
        "auth_time": auth_time,
        "token_source": "workbench_sso",
    }


def _payload(**updates):
    value = {
        "name": "weekday report",
        "prompt": "Summarize the owned conversation.",
        "attachment_ids": [],
        "conversation_id": None,
        "schedule_kind": "cron",
        "cron_expression": "*/30 * * * *",
        "timezone": "Asia/Shanghai",
        "misfire_policy": "skip",
        "max_runtime_seconds": 60,
        "daily_run_limit": 12,
        "max_retries": 1,
        "retry_backoff_seconds": 30,
    }
    value.update(updates)
    return value


@pytest.fixture
def scheduled_store(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def register_uuid(dbapi_connection, _connection_record):
        dbapi_connection.create_function("uuid_generate_v4", 0, lambda: uuid.uuid4().hex)

    for model in (
        WorkbenchIdentity,
        WorkbenchBrowserSession,
        WorkbenchAgentBinding,
        AgentConfig,
        WorkbenchScheduledTask,
        WorkbenchScheduledDelegation,
        WorkbenchScheduledRun,
        WorkbenchScheduledAudit,
    ):
        model.__table__.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    authenticated_at = datetime.fromtimestamp(AUTH_TIME, UTC).replace(tzinfo=None)
    with sessions() as session:
        session.add(
            WorkbenchIdentity(
                BindingId="binding-7",
                CanonicalSubject="napi:prod:customer:7:user:9",
                AccountId=ACCOUNT_ID,
                CustomerId=7,
                NewApiUserId=9,
                AuthEpoch=3,
                Status="active",
                LastAuthenticatedAt=authenticated_at,
            )
        )
        session.add(
            WorkbenchBrowserSession(
                SessionIdDigest=hashlib.sha256(SESSION_ID.encode("ascii")).hexdigest(),
                BindingId="binding-7",
                AccountId=ACCOUNT_ID,
                CustomerId=7,
                NewApiUserId=9,
                CanonicalSubject="napi:prod:customer:7:user:9",
                AuthEpoch=3,
                ApplicationId="customer-app-7",
                AppProfileId="17",
                ConfigVersion=5,
                AuthenticatedAt=authenticated_at,
                ExpiresAt=authenticated_at + timedelta(hours=1),
                Status="active",
            )
        )
        session.add(
            AgentConfig(
                AccountId=ACCOUNT_ID,
                ApplicationId="customer-app-7",
                AgentId="agent-7",
            )
        )
        session.add(
            WorkbenchAgentBinding(
                BindingId="binding-7",
                AccountId=ACCOUNT_ID,
                ApplicationId="customer-app-7",
                AgentId="agent-7",
                Status="active",
                AttemptId="attempt-7",
            )
        )
        session.commit()

    @asynccontextmanager
    async def connection():
        session = sessions()
        try:
            yield _AsyncSessionAdapter(session)
        finally:
            session.close()

    monkeypatch.setattr(scheduled_module, "db_connection", connection)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SCHEDULED_TASKS_ENABLED", True)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SCHEDULE_MIN_INTERVAL_MINUTES", 15)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SCHEDULE_MISFIRE_GRACE_SECONDS", 300)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SCHEDULE_BATCH_SIZE", 20)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SCHEDULE_LEASE_SECONDS", 90)
    yield sessions, connection
    engine.dispose()


async def _create(connection, payload=None, identity=None):
    async with connection() as db:
        return await WorkbenchScheduledService.create(
            db,
            account_id=ACCOUNT_ID,
            identity=identity or _identity(),
            app_context=_app_context(),
            payload=payload or _payload(),
            session_claims=_session_claims(identity=identity or _identity()),
        )


def test_cron_rejects_invalid_fields_and_too_frequent_schedule():
    with pytest.raises(WorkbenchScheduledError):
        WorkbenchCron("* * *", "Asia/Shanghai")
    with pytest.raises(WorkbenchScheduledError):
        WorkbenchCron("61 * * * *", "Asia/Shanghai")
    with pytest.raises(WorkbenchScheduledError):
        WorkbenchCron("* * * * *", "Etc/DefinitelyMissing")


def test_cron_dst_gap_is_skipped_and_fallback_wall_minute_is_not_duplicated():
    spring = WorkbenchCron("30 2 * * *", "America/New_York")
    after = datetime(2026, 3, 8, 6, 59, tzinfo=UTC)
    occurrence = spring.next_after(after)
    assert occurrence == datetime(2026, 3, 9, 6, 30)

    fallback = WorkbenchCron("30 1 * * *", "America/New_York")
    first = fallback.next_after(datetime(2026, 11, 1, 4, 0, tzinfo=UTC))
    second = fallback.next_after(first)
    assert first == datetime(2026, 11, 1, 5, 30)
    assert second == datetime(2026, 11, 2, 6, 30)


@pytest.mark.asyncio
async def test_create_persists_exact_scope_and_only_capability_digest(scheduled_store):
    sessions, connection = scheduled_store
    task = await _create(connection)

    with sessions() as session:
        stored = session.execute(select(WorkbenchScheduledTask)).scalar_one()
        delegation = session.execute(select(WorkbenchScheduledDelegation)).scalar_one()
        audit = session.execute(select(WorkbenchScheduledAudit)).scalar_one()
    assert stored.TaskId == task.TaskId
    assert stored.BindingId == "binding-7"
    assert stored.CustomerId == 7
    assert stored.ApplicationId == "customer-app-7"
    assert stored.AppProfileId == "17"
    assert stored.AgentId == "agent-7"
    assert len(delegation.CapabilityDigest) == 64
    assert delegation.CapabilityDigest not in stored.Prompt
    assert audit.Action == "create"
    assert "Summarize" not in (audit.DetailCode or "")


@pytest.mark.asyncio
async def test_browser_scope_fields_are_rejected(scheduled_store):
    _, connection = scheduled_store
    payload = _payload(customer_id=999, application_id="attacker-app", agent_id="attacker")
    with pytest.raises(WorkbenchScheduledError, match="unsupported fields"):
        await _create(connection, payload)


@pytest.mark.asyncio
async def test_idor_lookup_and_pause_return_no_foreign_task(scheduled_store):
    sessions, connection = scheduled_store
    task = await _create(connection)
    foreign = _identity(binding="binding-foreign", customer=8)
    foreign_account = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    with sessions() as session:
        session.add(
            WorkbenchIdentity(
                BindingId=foreign.binding_id,
                CanonicalSubject=foreign.canonical_subject,
                AccountId=foreign_account,
                CustomerId=foreign.customer_id,
                NewApiUserId=foreign.new_api_user_id,
                AuthEpoch=foreign.auth_epoch,
                Status="active",
                LastAuthenticatedAt=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        foreign_session_id = "f" * 43
        foreign_auth_time = int(datetime.now(UTC).timestamp())
        session.add(
            WorkbenchBrowserSession(
                SessionIdDigest=hashlib.sha256(foreign_session_id.encode("ascii")).hexdigest(),
                BindingId=foreign.binding_id,
                AccountId=foreign_account,
                CustomerId=foreign.customer_id,
                NewApiUserId=foreign.new_api_user_id,
                CanonicalSubject=foreign.canonical_subject,
                AuthEpoch=foreign.auth_epoch,
                ApplicationId=foreign.application_id,
                AppProfileId=foreign.app_profile_id,
                ConfigVersion=foreign.config_version,
                AuthenticatedAt=datetime.fromtimestamp(foreign_auth_time, UTC).replace(tzinfo=None),
                ExpiresAt=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
                Status="active",
            )
        )
        session.commit()
    async with connection() as db:
        assert (
            await WorkbenchScheduledService.get_owned(
                db,
                task_id=task.TaskId,
                account_id=foreign_account,
                identity=foreign,
                app_context=_app_context(),
            )
            is None
        )
        with pytest.raises(WorkbenchScheduledError, match="not found"):
            await WorkbenchScheduledService.pause(
                db,
                task_id=task.TaskId,
                account_id=foreign_account,
                identity=foreign,
                app_context=_app_context(),
                session_claims=_session_claims(
                    session_id=foreign_session_id,
                    account_id=foreign_account,
                    identity=foreign,
                    auth_time=foreign_auth_time,
                ),
            )


@pytest.mark.asyncio
async def test_pause_revokes_delegation_cancels_pending_and_resume_rotates(scheduled_store):
    sessions, connection = scheduled_store
    task = await _create(connection)
    now = datetime.now(UTC).replace(tzinfo=None)
    with sessions() as session:
        session.add(
            WorkbenchScheduledRun(
                RunId="wsr_pending",
                TaskId=task.TaskId,
                IdempotencyKey="a" * 64,
                ScheduledFor=now,
                Status="queued",
                NextAttemptAt=now,
            )
        )
        session.commit()
    async with connection() as db:
        await WorkbenchScheduledService.pause(
            db,
            task_id=task.TaskId,
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            session_claims=_session_claims(),
        )
        await WorkbenchScheduledService.resume(
            db,
            task_id=task.TaskId,
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            session_claims=_session_claims(),
        )
    with sessions() as session:
        delegations = session.execute(
            select(WorkbenchScheduledDelegation).order_by(
                WorkbenchScheduledDelegation.CapabilityVersion
            )
        ).scalars().all()
        run = session.execute(select(WorkbenchScheduledRun)).scalar_one()
    assert [item.Status for item in delegations] == ["revoked", "active"]
    assert delegations[0].CapabilityDigest != delegations[1].CapabilityDigest
    assert run.Status == "cancelled"


@pytest.mark.asyncio
async def test_recent_reauthentication_is_required_for_mutation(scheduled_store):
    sessions, connection = scheduled_store
    with sessions() as session:
        browser_session = session.execute(select(WorkbenchBrowserSession)).scalar_one()
        browser_session.AuthenticatedAt = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        session.commit()
    with pytest.raises(WorkbenchScheduledError, match="reauthentication"):
        await _create(connection)


@pytest.mark.asyncio
async def test_missing_capability_and_unbounded_tools_fail_closed(scheduled_store):
    _, connection = scheduled_store
    async with connection() as db:
        with pytest.raises(Exception, match="scheduled_tasks"):
            await WorkbenchScheduledService.create(
                db,
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(("chat",)),
                payload=_payload(),
                session_claims=_session_claims(),
            )
        with pytest.raises(Exception, match="unbounded"):
            await WorkbenchScheduledService.create(
                db,
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(("chat", "scheduled_tasks", "tools")),
                payload=_payload(),
                session_claims=_session_claims(),
            )


@pytest.mark.asyncio
async def test_materialization_is_idempotent_and_misfire_is_recorded(scheduled_store):
    sessions, connection = scheduled_store
    task = await _create(connection)
    now = datetime.now(UTC).replace(tzinfo=None)
    with sessions() as session:
        stored = session.execute(
            select(WorkbenchScheduledTask).where(WorkbenchScheduledTask.TaskId == task.TaskId)
        ).scalar_one()
        stored.NextRunAt = now - timedelta(hours=1)
        session.commit()

    assert await WorkbenchScheduledService.materialize_due(now) == 1
    assert await WorkbenchScheduledService.materialize_due(now) == 0
    with sessions() as session:
        runs = session.execute(select(WorkbenchScheduledRun)).scalars().all()
    assert len(runs) == 1
    assert runs[0].Status == "skipped_misfire"


@pytest.mark.asyncio
async def test_expired_delegation_pauses_task_without_creating_runs(scheduled_store):
    sessions, connection = scheduled_store
    task = await _create(connection)
    now = datetime.now(UTC).replace(tzinfo=None)
    with sessions() as session:
        stored = session.execute(select(WorkbenchScheduledTask)).scalar_one()
        stored.NextRunAt = now
        delegation = session.execute(select(WorkbenchScheduledDelegation)).scalar_one()
        delegation.ExpiresAt = now - timedelta(seconds=1)
        session.commit()
    assert await WorkbenchScheduledService.materialize_due(now) == 0
    with sessions() as session:
        stored = session.execute(select(WorkbenchScheduledTask)).scalar_one()
        run_count = session.execute(
            select(func.count(WorkbenchScheduledRun.Id))
        ).scalar_one()
    assert stored.Status == "paused"
    assert stored.NextRunAt is None
    assert run_count == 0


@pytest.mark.asyncio
async def test_daily_limit_marks_occurrence_skipped(scheduled_store):
    sessions, connection = scheduled_store
    task = await _create(connection, _payload(daily_run_limit=1, misfire_policy="fire_once"))
    now = datetime.now(UTC).replace(tzinfo=None)
    with sessions() as session:
        stored = session.execute(select(WorkbenchScheduledTask)).scalar_one()
        stored.NextRunAt = now
        session.add(
            WorkbenchScheduledRun(
                RunId="wsr_prior",
                TaskId=task.TaskId,
                IdempotencyKey="b" * 64,
                ScheduledFor=now - timedelta(minutes=30),
                Status="completed",
                NextAttemptAt=now,
            )
        )
        session.commit()
    await WorkbenchScheduledService.materialize_due(now)
    with sessions() as session:
        statuses = session.execute(
            select(WorkbenchScheduledRun.Status).order_by(WorkbenchScheduledRun.CreatedAt)
        ).scalars().all()
    assert "skipped_limit" in statuses


@pytest.mark.asyncio
async def test_run_now_is_queued_and_respects_daily_limit(scheduled_store):
    _, connection = scheduled_store
    task = await _create(connection, _payload(daily_run_limit=1))
    async with connection() as db:
        run = await WorkbenchScheduledService.run_now(
            db,
            task_id=task.TaskId,
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            session_claims=_session_claims(),
        )
    assert run.Status == "queued"
    async with connection() as db:
        with pytest.raises(WorkbenchScheduledError, match="daily run limit"):
            await WorkbenchScheduledService.run_now(
                db,
                task_id=task.TaskId,
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                session_claims=_session_claims(),
            )


@pytest.mark.asyncio
async def test_claim_is_single_owner_and_expired_submission_is_not_retried(scheduled_store):
    sessions, connection = scheduled_store
    task = await _create(connection, _payload(misfire_policy="fire_once"))
    now = datetime.now(UTC).replace(tzinfo=None)
    with sessions() as session:
        session.add(
            WorkbenchScheduledRun(
                RunId="wsr_claim",
                TaskId=task.TaskId,
                IdempotencyKey="c" * 64,
                ScheduledFor=now,
                Status="queued",
                NextAttemptAt=now,
            )
        )
        session.add(
            WorkbenchScheduledRun(
                RunId="wsr_expired",
                TaskId=task.TaskId,
                IdempotencyKey="d" * 64,
                ScheduledFor=now,
                Status="executing",
                AttemptCount=1,
                NextAttemptAt=now,
                LeaseOwner="dead-instance",
                LeaseUntil=now - timedelta(seconds=1),
                ProviderStarted=True,
                TurnId="wt_unknown",
            )
        )
        session.commit()
    first = await WorkbenchScheduledService.claim_due(now)
    second = await WorkbenchScheduledService.claim_due(now)
    assert first == ["wsr_claim"]
    assert second == []
    with sessions() as session:
        expired = session.execute(
            select(WorkbenchScheduledRun).where(WorkbenchScheduledRun.RunId == "wsr_expired")
        ).scalar_one()
    assert expired.Status == "provider_unknown"


@pytest.mark.asyncio
async def test_revoked_delegation_fails_before_provider_execution(scheduled_store, monkeypatch):
    sessions, connection = scheduled_store
    task = await _create(connection, _payload(misfire_policy="fire_once"))
    now = datetime.now(UTC).replace(tzinfo=None)
    with sessions() as session:
        delegation = session.execute(select(WorkbenchScheduledDelegation)).scalar_one()
        delegation.Status = "revoked"
        delegation.RevokedAt = now
        session.add(
            WorkbenchScheduledRun(
                RunId="wsr_revoked",
                TaskId=task.TaskId,
                IdempotencyKey="e" * 64,
                ScheduledFor=now,
                Status="leased",
                AttemptCount=1,
                NextAttemptAt=now,
                LeaseOwner=WorkbenchScheduledService.instance_id,
                LeaseUntil=now + timedelta(minutes=1),
            )
        )
        session.commit()
    provider = AsyncMock()
    monkeypatch.setattr(WorkbenchTurnManager, "execute", provider)
    await WorkbenchScheduledService.execute_run("wsr_revoked")
    provider.assert_not_awaited()
    with sessions() as session:
        run = session.execute(
            select(WorkbenchScheduledRun).where(WorkbenchScheduledRun.RunId == "wsr_revoked")
        ).scalar_one()
    assert run.Status == "revoked"


@pytest.mark.asyncio
async def test_exception_after_submission_boundary_is_provider_unknown_not_retry(
    scheduled_store,
):
    sessions, connection = scheduled_store
    task = await _create(connection, _payload(max_retries=3))
    now = datetime.now(UTC).replace(tzinfo=None)
    with sessions() as session:
        session.add(
            WorkbenchScheduledRun(
                RunId="wsr_unknown_provider",
                TaskId=task.TaskId,
                IdempotencyKey="f" * 64,
                ScheduledFor=now,
                Status="executing",
                AttemptCount=1,
                NextAttemptAt=now,
                LeaseOwner=WorkbenchScheduledService.instance_id,
                LeaseUntil=now + timedelta(minutes=1),
                ProviderStarted=True,
                TurnId="wt_ambiguous",
            )
        )
        session.commit()
    async with connection() as db:
        await WorkbenchScheduledService._settle_attempt(
            db,
            "wsr_unknown_provider",
            "failed_before_accept",
            None,
            error_code="UnknownVendorError",
        )
    with sessions() as session:
        run = session.execute(
            select(WorkbenchScheduledRun).where(
                WorkbenchScheduledRun.RunId == "wsr_unknown_provider"
            )
        ).scalar_one()
    assert run.Status == "provider_unknown"
    assert run.NextAttemptAt == now


def test_safe_projection_omits_prompt_from_lists_and_runs():
    now = datetime.now(UTC).replace(tzinfo=None)
    task = SimpleNamespace(
        TaskId="wst_1",
        Name="name",
        Prompt="top secret prompt",
        Status="active",
        ScheduleKind="cron",
        CronExpression="0 * * * *",
        Timezone="UTC",
        OnceAt=None,
        NextRunAt=now,
        MisfirePolicy="skip",
        MaxRuntimeSeconds=60,
        DailyRunLimit=12,
        MaxRetries=1,
        RetryBackoffSeconds=30,
        ConversationId=None,
        AttachmentIdsJson="[]",
        Version=1,
        CreatedAt=now,
        UpdatedAt=now,
    )
    assert "prompt" not in WorkbenchScheduledService.project_task(task, include_prompt=False)
    assert WorkbenchScheduledService.project_task(task, include_prompt=True)["prompt"] == task.Prompt


def test_csrf_requires_matching_cookie_and_header():
    assert valid_workbench_csrf(
        {"claw_workbench_csrf": "a" * 43},
        {"X-Workbench-CSRF": "a" * 43},
    )
    assert not valid_workbench_csrf(
        {"claw_workbench_csrf": "a" * 43},
        {"X-Workbench-CSRF": "b" * 43},
    )


def test_migration_registers_scheduled_tables():
    with patch("app_factory.TAgenticApp.get_app", return_value=SimpleNamespace()):
        from core.migration import Migration

    tables = Migration.tables()
    assert WorkbenchScheduledTask in tables
    assert WorkbenchScheduledDelegation in tables
    assert WorkbenchScheduledRun in tables
    assert WorkbenchScheduledAudit in tables


@pytest.mark.asyncio
async def test_offline_authorization_uses_exact_customer_app_and_plan_tuple(monkeypatch):
    identity = _identity()
    app_context = _app_context()
    local = SimpleNamespace(
        BindingId=identity.binding_id,
        CanonicalSubject=identity.canonical_subject,
        CustomerId=identity.customer_id,
        NewApiUserId=identity.new_api_user_id,
    )

    class Result:
        def scalar(self):
            return local

    db = SimpleNamespace(execute=AsyncMock(return_value=Result()))
    monkeypatch.setattr(
        "core.workbench_identity.CoreAccount.get",
        AsyncMock(
            return_value=SimpleNamespace(
                Status=AccountStatus.ACTIVE,
                Role=AccountRole.NORMAL,
            )
        ),
    )
    authorize = AsyncMock(return_value=identity)
    app_lookup = AsyncMock(return_value=app_context)
    monkeypatch.setattr(WorkbenchControlClient, "authorize", authorize)
    monkeypatch.setattr(WorkbenchControlClient, "get_app_context", app_lookup)
    monkeypatch.setattr(
        "core.workbench_identity.WorkbenchAppResolver.ensure_vendor", AsyncMock()
    )

    resolved = await CoreWorkbenchIdentity.authorize_offline_scope(
        db,
        account_id=str(ACCOUNT_ID),
        binding_id=identity.binding_id,
        canonical_subject=identity.canonical_subject,
        customer_id=identity.customer_id,
        new_api_user_id=identity.new_api_user_id,
        auth_epoch=identity.auth_epoch,
        application_id=identity.application_id,
        app_profile_id=identity.app_profile_id,
        config_version=identity.config_version,
    )

    assert resolved == (identity, app_context)
    authorize.assert_awaited_once_with(
        binding_id=identity.binding_id,
        canonical_subject=identity.canonical_subject,
        auth_epoch=identity.auth_epoch,
        method="POST",
        resource_path="/workbench/scheduled-tasks/execute",
        customer_id=identity.customer_id,
        app_profile_id=17,
        config_version=identity.config_version,
    )
    app_lookup.assert_awaited_once_with(
        binding_id=identity.binding_id,
        canonical_subject=identity.canonical_subject,
        auth_epoch=identity.auth_epoch,
        requested_app_profile_id=17,
        requested_config_version=identity.config_version,
        purpose="scheduled_task",
    )


@pytest.mark.asyncio
async def test_offline_authorization_fails_closed_when_control_revokes_member(monkeypatch):
    identity = _identity()

    class Result:
        def scalar(self):
            return SimpleNamespace(
                BindingId=identity.binding_id,
                CanonicalSubject=identity.canonical_subject,
                CustomerId=identity.customer_id,
                NewApiUserId=identity.new_api_user_id,
            )

    monkeypatch.setattr(
        "core.workbench_identity.CoreAccount.get",
        AsyncMock(
            return_value=SimpleNamespace(
                Status=AccountStatus.ACTIVE,
                Role=AccountRole.NORMAL,
            )
        ),
    )
    monkeypatch.setattr(
        WorkbenchControlClient,
        "authorize",
        AsyncMock(side_effect=WorkbenchControlError("revoked", 403)),
    )
    with pytest.raises(WorkbenchIdentityError, match="revoked"):
        await CoreWorkbenchIdentity.authorize_offline_scope(
            SimpleNamespace(execute=AsyncMock(return_value=Result())),
            account_id=str(ACCOUNT_ID),
            binding_id=identity.binding_id,
            canonical_subject=identity.canonical_subject,
            customer_id=identity.customer_id,
            new_api_user_id=identity.new_api_user_id,
            auth_epoch=identity.auth_epoch,
            application_id=identity.application_id,
            app_profile_id=identity.app_profile_id,
            config_version=identity.config_version,
        )


@pytest.mark.asyncio
async def test_offline_stream_reauthorization_is_used_instead_of_expired_browser_claims(
    monkeypatch,
):
    class SilentStream:
        async def __anext__(self):
            await asyncio.Future()

        async def aclose(self):
            return None

    offline = AsyncMock(side_effect=RuntimeError("delegation revoked"))
    browser_reauth = AsyncMock()
    release = AsyncMock()
    values = iter((0.0, 0.0, 30.0))

    async def remains_silent(tasks, **_kwargs):
        await asyncio.sleep(0)
        return set(), set(tasks)

    monkeypatch.setattr(scheduled_module.asyncio, "wait", remains_silent)
    monkeypatch.setattr(WorkbenchStreamGuard, "reauthorize", browser_reauth)
    monkeypatch.setattr(
        "core.workbench_stream.WorkbenchRuntimeGuard.release", release
    )
    with pytest.raises(RuntimeError, match="revoked"):
        await WorkbenchStreamGuard.pump(
            SilentStream(),
            AsyncMock(),
            claims={},
            method="POST",
            resource_path="/workbench/scheduled-tasks/execute",
            application_id="customer-app-7",
            app_profile_id="17",
            config_version=5,
            lease=object(),
            max_runtime_seconds=60,
            reauthorization_interval_seconds=30,
            offline_reauthorize=offline,
            monotonic=lambda: next(values),
        )
    offline.assert_awaited_once()
    browser_reauth.assert_not_awaited()
    release.assert_awaited_once()
