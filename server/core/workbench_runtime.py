import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text

from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from model.workbench import WorkbenchRuntimeLease
from util.database import db_connection


class WorkbenchRuntimeError(RuntimeError):
    def __init__(self, message: str, status_code: int = 429):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class RuntimeLease:
    lease_id: str
    max_runtime_seconds: int


class WorkbenchRuntimeGuard:
    """PostgreSQL-backed customer and member concurrency limits.

    Transaction-scoped advisory locks serialize the count-and-insert operation
    across every ADP process.  The durable expiry makes a lease recoverable when
    a process dies before its normal ``finally`` cleanup runs.
    """

    @staticmethod
    async def acquire(
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        operation: str,
    ) -> RuntimeLease:
        limits = app_context.limits or {}
        customer_limit = int(limits.get("customer_concurrency", 0))
        configured_user_limit = int(limits.get("user_concurrency", 1))
        user_limit = min(configured_user_limit, 1)
        max_runtime = int(limits.get("max_runtime_seconds", 0))
        if customer_limit <= 0 or configured_user_limit <= 0 or max_runtime <= 0:
            raise WorkbenchRuntimeError("workbench runtime limits are invalid", 503)

        lease_id = uuid.uuid4().hex
        expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=max_runtime)
        lock_keys = sorted(
            {
                f"workbench:customer:{identity.customer_id}",
                f"workbench:user:{identity.customer_id}:{identity.binding_id}",
            }
        )

        async with db_connection() as db:
            try:
                # ADP's supported database is PostgreSQL. hashtext is available on
                # every supported PostgreSQL version and collisions only make the
                # limiter more conservative; they cannot permit excess traffic.
                for lock_key in lock_keys:
                    await db.execute(
                        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                        {"lock_key": lock_key},
                    )

                await db.execute(
                    delete(WorkbenchRuntimeLease).where(
                        WorkbenchRuntimeLease.ExpiresAt <= func.current_timestamp()
                    )
                )
                active_clause = WorkbenchRuntimeLease.ExpiresAt > func.current_timestamp()
                customer_count = (
                    await db.execute(
                        select(func.count(WorkbenchRuntimeLease.Id)).where(
                            WorkbenchRuntimeLease.CustomerId == identity.customer_id,
                            active_clause,
                        )
                    )
                ).scalar_one()
                if customer_count >= customer_limit:
                    raise WorkbenchRuntimeError("customer concurrency limit reached")

                user_count = (
                    await db.execute(
                        select(func.count(WorkbenchRuntimeLease.Id)).where(
                            WorkbenchRuntimeLease.CustomerId == identity.customer_id,
                            WorkbenchRuntimeLease.BindingId == identity.binding_id,
                            active_clause,
                        )
                    )
                ).scalar_one()
                if user_count >= user_limit:
                    raise WorkbenchRuntimeError("user concurrency limit reached")

                db.add(
                    WorkbenchRuntimeLease(
                        LeaseId=lease_id,
                        BindingId=identity.binding_id,
                        CustomerId=identity.customer_id,
                        AccountId=account_id,
                        ApplicationId=app_context.application_id,
                        Operation=operation,
                        MaxRuntimeSeconds=max_runtime,
                        ExpiresAt=expires_at,
                    )
                )
                await db.commit()
            except WorkbenchRuntimeError:
                await db.rollback()
                raise
            except Exception as error:
                await db.rollback()
                raise WorkbenchRuntimeError(
                    "workbench concurrency guard is unavailable",
                    503,
                ) from error

        return RuntimeLease(lease_id=lease_id, max_runtime_seconds=max_runtime)

    @staticmethod
    async def release(lease: RuntimeLease | None) -> None:
        if lease is None:
            return
        async with db_connection() as db:
            try:
                await db.execute(
                    delete(WorkbenchRuntimeLease).where(
                        WorkbenchRuntimeLease.LeaseId == lease.lease_id
                    )
                )
                await db.commit()
            except Exception:
                # Expiry remains the recovery path if the database is temporarily
                # unavailable during normal cleanup.
                await db.rollback()
