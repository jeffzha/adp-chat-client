import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Callable

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import tagentic_config
from core.workbench_async_cleanup import bounded_cleanup
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_identity import CoreWorkbenchIdentity, WorkbenchIdentityError
from core.workbench_policy import WorkbenchPolicy
from core.workbench_runtime import RuntimeLease, WorkbenchRuntimeGuard
from core.workbench_sandbox.contracts import (
    CodeResult,
    CommandChunk,
    CommandResult,
    ManagedSandboxProvider,
    ProviderInstance,
    SandboxProviderError,
)
from core.workbench_sandbox.provider import TencentAGSXProvider
from core.workbench_sandbox.secrets import read_secret_file
from model.chat import ChatConversation
from model.workbench import WorkbenchConversationWorkspace
from model.workbench_sandbox import WorkbenchSandbox, WorkbenchSandboxAudit
from util.database import db_connection


_LANGUAGES = {"python", "javascript", "typescript", "java", "r", "bash"}
_TERMINAL_STATUSES = {"stopped", "failed", "starting_failed"}
_OWNED_INSTANCE_STATUSES = {
    "starting",
    "running",
    "stopping",
    "pausing",
    "paused",
    "pause_failed",
    "resuming",
    "resume_failed",
    "stopping_failed",
    "stop_failed",
    "provider_unknown",
}


class WorkbenchSandboxError(RuntimeError):
    def __init__(self, code: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class WorkbenchSandboxService:
    provider_factory: Callable[[], ManagedSandboxProvider] = TencentAGSXProvider
    runtime_guard = WorkbenchRuntimeGuard
    state_session_factory = staticmethod(db_connection)

    @staticmethod
    def code_execution_available(app_context: WorkbenchAppContext) -> bool:
        return bool(
            tagentic_config.WORKBENCH_SANDBOX_ENABLED
            and tagentic_config.WORKBENCH_SANDBOX_CODE_ENABLED
            and "sandbox" in app_context.capabilities
        )

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
                maximum_age_seconds=tagentic_config.WORKBENCH_SANDBOX_REAUTH_SECONDS,
            )
        except WorkbenchIdentityError as error:
            raise WorkbenchSandboxError("sandbox_reauth_required", 401) from error

    @staticmethod
    def _enabled() -> None:
        if not tagentic_config.WORKBENCH_SANDBOX_ENABLED:
            raise WorkbenchSandboxError("sandbox_disabled", 503)
        if tagentic_config.WORKBENCH_SANDBOX_PROVIDER != "tencent_agsx":
            raise WorkbenchSandboxError("sandbox_provider_invalid", 503)

    @classmethod
    def _authorize(
        cls,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        *,
        mutation: bool,
        files: bool = False,
    ) -> dict[str, int]:
        cls._enabled()
        if mutation:
            WorkbenchPolicy.require_active(identity)
        WorkbenchPolicy.require_capability(app_context, "sandbox")
        if files:
            WorkbenchPolicy.require_capability(app_context, "files")
        return WorkbenchPolicy.validated_limits(app_context)

    @staticmethod
    def _conversation_id(raw: Any) -> str:
        value = str(raw or "").strip()
        try:
            parsed = uuid.UUID(value)
        except (ValueError, TypeError, AttributeError):
            raise WorkbenchSandboxError("conversation_id_invalid")
        return str(parsed)

    @staticmethod
    async def _assert_conversation(
        db: AsyncSession,
        *,
        conversation_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> None:
        owned = (
            await db.execute(
                select(WorkbenchConversationWorkspace.Id)
                .join(
                    ChatConversation,
                    ChatConversation.Id == WorkbenchConversationWorkspace.ConversationId,
                )
                .where(
                    WorkbenchConversationWorkspace.ConversationId
                    == uuid.UUID(conversation_id),
                    WorkbenchConversationWorkspace.BindingId == identity.binding_id,
                    WorkbenchConversationWorkspace.AccountId == account_id,
                    WorkbenchConversationWorkspace.CustomerId == identity.customer_id,
                    WorkbenchConversationWorkspace.ApplicationId
                    == app_context.application_id,
                    WorkbenchConversationWorkspace.ProviderAppId == app_context.app_id,
                    WorkbenchConversationWorkspace.AppProfileId
                    == app_context.app_profile_id,
                    WorkbenchConversationWorkspace.ConfigVersion
                    == app_context.config_version,
                    ChatConversation.AccountId == account_id,
                    ChatConversation.ApplicationId == app_context.application_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if owned is None:
            raise WorkbenchSandboxError("conversation_not_owned", 404)

    @staticmethod
    def _bounded_int(raw: Any, *, default: int, minimum: int, maximum: int) -> int:
        value = default if raw is None else raw
        if isinstance(value, bool) or not isinstance(value, int):
            raise WorkbenchSandboxError("timeout_invalid")
        if value < minimum or value > maximum:
            raise WorkbenchSandboxError("timeout_invalid")
        return value

    @staticmethod
    def _secret_file(path_value: str) -> bytes:
        try:
            value = read_secret_file(path_value, minimum_bytes=32)
        except OSError as error:
            raise WorkbenchSandboxError("client_token_key_unavailable", 503) from error
        return value

    @classmethod
    def _client_token(cls, scope: bytes, generation: int) -> tuple[str, str]:
        message = scope + b"\x00generation\x00" + generation.to_bytes(8, "big")
        digest = hmac.new(
            cls._secret_file(tagentic_config.WORKBENCH_SANDBOX_CLIENT_TOKEN_KEY_FILE),
            message,
            hashlib.sha256,
        ).digest()
        token = "wb_" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return token, hashlib.sha256(token.encode("ascii")).hexdigest()

    @staticmethod
    def _scope(
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        conversation_id: str,
    ) -> bytes:
        parts = (
            "workbench-sandbox-scope-v1",
            str(identity.customer_id),
            str(identity.new_api_user_id),
            app_context.application_id,
            conversation_id,
        )
        encoded = bytearray()
        for part in parts:
            value = part.encode("utf-8")
            encoded.extend(len(value).to_bytes(4, "big"))
            encoded.extend(value)
        return bytes(encoded)

    @staticmethod
    async def _provisioning_locks(
        db: AsyncSession,
        *,
        identity: WorkbenchIdentityContext,
        scope: bytes,
    ) -> None:
        bind = db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            lock_keys = sorted(
                {
                    f"workbench:sandbox:customer:{identity.customer_id}",
                    (
                        "workbench:sandbox:user:"
                        f"{identity.customer_id}:{identity.new_api_user_id}"
                    ),
                    (
                        "workbench:sandbox:scope:"
                        + hashlib.sha256(scope).hexdigest()
                    ),
                }
            )
            for lock_key in lock_keys:
                await db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                    {"lock_key": lock_key},
                )

    @staticmethod
    def _assert_snapshot(
        sandbox: WorkbenchSandbox,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> None:
        if (
            sandbox.BindingId != identity.binding_id
            or sandbox.AuthEpoch != identity.auth_epoch
            or sandbox.AppProfileId != app_context.app_profile_id
            or sandbox.ConfigVersion != app_context.config_version
            or app_context.auth_epoch != identity.auth_epoch
        ):
            raise WorkbenchSandboxError("sandbox_authorization_changed", 409)

    @staticmethod
    def _project(sandbox: WorkbenchSandbox) -> dict[str, Any]:
        return {
            "sandbox_id": sandbox.SandboxId,
            "conversation_id": sandbox.ConversationId,
            "status": sandbox.Status,
            "timeout_seconds": sandbox.TimeoutSeconds,
            "expires_at": sandbox.ExpiresAt.isoformat() + "Z" if sandbox.ExpiresAt else None,
            "created_at": sandbox.CreatedAt.isoformat() + "Z" if sandbox.CreatedAt else None,
            "updated_at": sandbox.UpdatedAt.isoformat() + "Z" if sandbox.UpdatedAt else None,
        }

    @staticmethod
    def _audit(
        db: AsyncSession,
        sandbox: WorkbenchSandbox,
        *,
        event_type: str,
        outcome: str,
        status_from: str | None = None,
        status_to: str | None = None,
        error_code: str | None = None,
    ) -> None:
        db.add(
            WorkbenchSandboxAudit(
                SandboxId=sandbox.SandboxId,
                BindingId=sandbox.BindingId,
                CustomerId=sandbox.CustomerId,
                NewApiUserId=sandbox.NewApiUserId,
                ApplicationId=sandbox.ApplicationId,
                ConversationId=sandbox.ConversationId,
                EventType=event_type[:64],
                Outcome=outcome[:32],
                StatusFrom=status_from[:24] if status_from else None,
                StatusTo=status_to[:24] if status_to else None,
                ErrorCode=error_code[:64] if error_code else None,
            )
        )

    @staticmethod
    def _copy_instance(sandbox: WorkbenchSandbox, instance: ProviderInstance) -> None:
        sandbox.ProviderInstanceId = instance.instance_id
        sandbox.Status = instance.status
        sandbox.ExpiresAt = instance.expires_at
        sandbox.ErrorCode = None
        sandbox.LeaseOwner = None
        sandbox.LeaseUntil = None
        sandbox.Version = int(sandbox.Version or 0) + 1

    @staticmethod
    def _instance_values(instance: ProviderInstance) -> dict[str, Any]:
        return {
            "ProviderInstanceId": instance.instance_id,
            "Status": instance.status,
            "ExpiresAt": instance.expires_at,
            "ErrorCode": None,
            "LeaseOwner": None,
            "LeaseUntil": None,
        }

    @classmethod
    async def _cas_transition(
        cls,
        db: AsyncSession,
        sandbox: WorkbenchSandbox,
        *,
        expected_version: int,
        values: dict[str, Any],
        event_type: str,
        outcome: str,
        error_code: str | None = None,
    ) -> bool:
        previous = sandbox.Status
        result = await db.execute(
            update(WorkbenchSandbox)
            .where(
                WorkbenchSandbox.Id == sandbox.Id,
                WorkbenchSandbox.Version == expected_version,
            )
            .values(**values, Version=expected_version + 1)
        )
        if result.rowcount != 1:
            await db.rollback()
            return False
        for name, value in values.items():
            setattr(sandbox, name, value)
        sandbox.Version = expected_version + 1
        cls._audit(
            db,
            sandbox,
            event_type=event_type,
            outcome=outcome,
            status_from=previous,
            status_to=values.get("Status", previous),
            error_code=error_code,
        )
        await db.commit()
        return True

    @staticmethod
    def _provider_error(error: SandboxProviderError) -> WorkbenchSandboxError:
        allowed = {
            "provider_rate_limited",
            "provider_instance_not_found",
            "provider_contract_unavailable",
            "output_limit_exceeded",
            "file_limit_exceeded",
            "sandbox_runtime_timeout",
            "sandbox_runtime_unknown",
        }
        code = error.code if error.code in allowed else "sandbox_provider_failed"
        return WorkbenchSandboxError(code, error.status_code)

    @classmethod
    async def create_or_reuse(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        payload: dict[str, Any] | None,
        provider: ManagedSandboxProvider | None = None,
        provider_start_observer: Callable[
            [AsyncSession, WorkbenchSandbox, ProviderInstance], Awaitable[None]
        ]
        | None = None,
    ) -> dict[str, Any]:
        limits = cls._authorize(identity, app_context, mutation=True)
        payload = payload if isinstance(payload, dict) else {}
        conversation_id = cls._conversation_id(payload.get("conversation_id"))
        await cls._assert_conversation(
            db,
            conversation_id=conversation_id,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
        )
        hard_max = min(
            tagentic_config.WORKBENCH_SANDBOX_HARD_MAX_RUNTIME_SECONDS,
            limits["max_runtime_seconds"],
            24 * 60 * 60,
        )
        timeout_seconds = cls._bounded_int(
            payload.get("timeout_seconds"),
            default=min(
                tagentic_config.WORKBENCH_SANDBOX_DEFAULT_TIMEOUT_SECONDS,
                hard_max,
            ),
            minimum=30,
            maximum=hard_max,
        )
        scope = cls._scope(identity, app_context, conversation_id)
        lease_owner = tagentic_config.WORKBENCH_INSTANCE_ID.strip()
        if not lease_owner or len(lease_owner) > 128:
            raise WorkbenchSandboxError("sandbox_instance_id_invalid", 503)
        now = datetime.now(UTC).replace(tzinfo=None)
        lease_until = now + timedelta(
            seconds=tagentic_config.WORKBENCH_SANDBOX_LEASE_SECONDS
        )

        await cls._provisioning_locks(db, identity=identity, scope=scope)
        terminal_cutoff = now - timedelta(
            days=tagentic_config.WORKBENCH_SANDBOX_TERMINAL_RETENTION_DAYS
        )
        await db.execute(
            delete(WorkbenchSandbox).where(
                WorkbenchSandbox.CustomerId == identity.customer_id,
                WorkbenchSandbox.Status.in_(_TERMINAL_STATUSES),
                WorkbenchSandbox.UpdatedAt < terminal_cutoff,
            )
        )
        expired = (
            await db.execute(
                select(WorkbenchSandbox)
                .where(
                    WorkbenchSandbox.CustomerId == identity.customer_id,
                    WorkbenchSandbox.Status.in_(_OWNED_INSTANCE_STATUSES),
                    WorkbenchSandbox.ExpiresAt.is_not(None),
                    WorkbenchSandbox.ExpiresAt <= now,
                )
                .with_for_update()
            )
        ).scalars().all()
        for expired_sandbox in expired:
            previous = expired_sandbox.Status
            expired_sandbox.Status = "stopped"
            expired_sandbox.ErrorCode = "expired"
            expired_sandbox.LeaseOwner = None
            expired_sandbox.LeaseUntil = None
            expired_sandbox.Version = int(expired_sandbox.Version or 0) + 1
            cls._audit(
                db,
                expired_sandbox,
                event_type="instance_expired",
                outcome="accepted",
                status_from=previous,
                status_to="stopped",
                error_code="expired",
            )
        sandbox = (
            await db.execute(
                select(WorkbenchSandbox)
                .where(
                    WorkbenchSandbox.CustomerId == identity.customer_id,
                    WorkbenchSandbox.NewApiUserId == identity.new_api_user_id,
                    WorkbenchSandbox.ApplicationId == app_context.application_id,
                    WorkbenchSandbox.ConversationId == conversation_id,
                )
                .with_for_update()
            )
        ).scalar()
        if sandbox is not None and sandbox.Status in _OWNED_INSTANCE_STATUSES:
            cls._assert_snapshot(sandbox, identity, app_context)
            _, expected_hash = cls._client_token(scope, sandbox.Generation)
            if sandbox.ClientTokenHash != expected_hash:
                raise WorkbenchSandboxError("sandbox_idempotency_conflict", 409)
            await db.commit()
            return cls._project(sandbox)
        if (
            sandbox is not None
            and sandbox.Status == "provisioning"
            and sandbox.LeaseUntil is not None
            and sandbox.LeaseUntil > now
        ):
            await db.commit()
            raise WorkbenchSandboxError("sandbox_provisioning", 409)

        active_clause = or_(
            (
                WorkbenchSandbox.Status.in_(_OWNED_INSTANCE_STATUSES)
                & or_(
                    WorkbenchSandbox.ExpiresAt.is_(None),
                    WorkbenchSandbox.ExpiresAt > now,
                )
            ),
            WorkbenchSandbox.Status == "provisioning",
        )
        capacity_filters = [active_clause]
        if sandbox is not None and sandbox.Status == "provisioning":
            cls._assert_snapshot(sandbox, identity, app_context)
            # An expired lease may only be taken over by this exact owner scope.
            # The row still occupies capacity for every other conversation.
            capacity_filters.append(WorkbenchSandbox.Id != sandbox.Id)
        customer_count = (
            await db.execute(
                select(func.count(WorkbenchSandbox.Id)).where(
                    WorkbenchSandbox.CustomerId == identity.customer_id,
                    *capacity_filters,
                )
            )
        ).scalar_one()
        if customer_count >= limits["customer_concurrency"]:
            await db.commit()
            raise WorkbenchSandboxError("sandbox_customer_capacity", 429)

        user_count = (
            await db.execute(
                select(func.count(WorkbenchSandbox.Id)).where(
                    WorkbenchSandbox.CustomerId == identity.customer_id,
                    WorkbenchSandbox.NewApiUserId == identity.new_api_user_id,
                    *capacity_filters,
                )
            )
        ).scalar_one()
        if user_count >= limits["user_concurrency"]:
            await db.commit()
            raise WorkbenchSandboxError("sandbox_user_capacity", 429)

        rate_cutoff = now - timedelta(minutes=1)
        customer_starts = (
            await db.execute(
                select(func.count(WorkbenchSandboxAudit.Id)).where(
                    WorkbenchSandboxAudit.CustomerId == identity.customer_id,
                    WorkbenchSandboxAudit.EventType == "start_attempt",
                    WorkbenchSandboxAudit.CreatedAt >= rate_cutoff,
                )
            )
        ).scalar_one()
        user_starts = (
            await db.execute(
                select(func.count(WorkbenchSandboxAudit.Id)).where(
                    WorkbenchSandboxAudit.CustomerId == identity.customer_id,
                    WorkbenchSandboxAudit.NewApiUserId == identity.new_api_user_id,
                    WorkbenchSandboxAudit.EventType == "start_attempt",
                    WorkbenchSandboxAudit.CreatedAt >= rate_cutoff,
                )
            )
        ).scalar_one()
        if (
            customer_starts
            >= tagentic_config.WORKBENCH_SANDBOX_STARTS_PER_CUSTOMER_MINUTE
            or user_starts >= tagentic_config.WORKBENCH_SANDBOX_STARTS_PER_USER_MINUTE
        ):
            await db.commit()
            raise WorkbenchSandboxError("sandbox_start_rate_limited", 429)

        if sandbox is None:
            generation = secrets.randbits(63) or 1
            client_token, client_token_hash = cls._client_token(scope, generation)
            sandbox = WorkbenchSandbox(
                SandboxId="sbx_" + secrets.token_hex(16),
                BindingId=identity.binding_id,
                AccountId=account_id,
                CustomerId=identity.customer_id,
                NewApiUserId=identity.new_api_user_id,
                ApplicationId=app_context.application_id,
                AppProfileId=app_context.app_profile_id,
                ConfigVersion=app_context.config_version,
                AuthEpoch=identity.auth_epoch,
                ConversationId=conversation_id,
                Provider="tencent_agsx",
                ClientTokenHash=client_token_hash,
                Generation=generation,
                Version=1,
                Status="provisioning",
                TimeoutSeconds=timeout_seconds,
                LeaseOwner=lease_owner,
                LeaseUntil=lease_until,
            )
            db.add(sandbox)
        else:
            generation = sandbox.Generation
            if sandbox.Status in _TERMINAL_STATUSES:
                generation += 1
            client_token, client_token_hash = cls._client_token(scope, generation)
            sandbox.BindingId = identity.binding_id
            sandbox.AccountId = account_id
            sandbox.AppProfileId = app_context.app_profile_id
            sandbox.ConfigVersion = app_context.config_version
            sandbox.AuthEpoch = identity.auth_epoch
            sandbox.ClientTokenHash = client_token_hash
            sandbox.Generation = generation
            sandbox.Status = "provisioning"
            sandbox.TimeoutSeconds = timeout_seconds
            sandbox.LeaseOwner = lease_owner
            sandbox.LeaseUntil = lease_until
            sandbox.ErrorCode = None
            sandbox.Version = int(sandbox.Version or 0) + 1
        cls._audit(
            db,
            sandbox,
            event_type="start_attempt",
            outcome="accepted",
            status_from=sandbox.Status,
            status_to="provisioning",
        )
        try:
            await db.commit()
        except IntegrityError as error:
            await db.rollback()
            raise WorkbenchSandboxError("sandbox_provisioning", 409) from error
        attempt_version = int(sandbox.Version)

        provider = provider or cls.provider_factory()
        metadata = {
            "scope_hash": hashlib.sha256(scope).hexdigest(),
            "sandbox_id": hashlib.sha256(sandbox.SandboxId.encode("utf-8")).hexdigest(),
        }
        try:
            instance = await provider.start(
                client_token=client_token,
                timeout_seconds=timeout_seconds,
                metadata=metadata,
            )
            if provider_start_observer is not None:
                try:
                    await provider_start_observer(db, sandbox, instance)
                except SandboxProviderError:
                    raise
                except Exception as error:
                    raise SandboxProviderError(
                        "provider_start_observer_failed",
                        status_code=503,
                        retryable=False,
                        instance_id=instance.instance_id,
                    ) from error
            if not await cls._cas_transition(
                db,
                sandbox,
                expected_version=attempt_version,
                values=cls._instance_values(instance),
                event_type="start_result",
                outcome="accepted",
            ):
                raise WorkbenchSandboxError("sandbox_state_conflict", 409)
        except SandboxProviderError as error:
            if error.instance_id:
                values = {
                    "ProviderInstanceId": error.instance_id,
                    "Status": "provider_unknown",
                    "ErrorCode": error.code[:64],
                    "LeaseOwner": None,
                    "LeaseUntil": None,
                }
            else:
                values = {
                    "Status": "provisioning" if error.retryable else "failed",
                    "ErrorCode": error.code[:64],
                    "LeaseOwner": None,
                    "LeaseUntil": now if error.retryable else None,
                }
            applied = await cls._cas_transition(
                db,
                sandbox,
                expected_version=attempt_version,
                values=values,
                event_type="provider_unknown" if error.instance_id else "start_result",
                outcome="unknown" if error.instance_id else "rejected",
                error_code=error.code,
            )
            if not applied:
                raise WorkbenchSandboxError("sandbox_state_conflict", 409) from error
            raise cls._provider_error(error) from error
        return cls._project(sandbox)

    @classmethod
    async def find_local(
        cls,
        db: AsyncSession,
        *,
        conversation_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> dict[str, Any]:
        """Return the exact owner-scoped local handle without touching provider APIs."""

        cls._authorize(identity, app_context, mutation=False)
        exact_conversation_id = cls._conversation_id(conversation_id)
        try:
            await cls._assert_conversation(
                db,
                conversation_id=exact_conversation_id,
                account_id=account_id,
                identity=identity,
                app_context=app_context,
            )
        except WorkbenchSandboxError as error:
            raise WorkbenchSandboxError("sandbox_not_found", 404) from error
        sandbox = (
            await db.execute(
                select(WorkbenchSandbox).where(
                    WorkbenchSandbox.ConversationId == exact_conversation_id,
                    WorkbenchSandbox.AccountId == account_id,
                    WorkbenchSandbox.BindingId == identity.binding_id,
                    WorkbenchSandbox.CustomerId == identity.customer_id,
                    WorkbenchSandbox.NewApiUserId == identity.new_api_user_id,
                    WorkbenchSandbox.ApplicationId == app_context.application_id,
                    WorkbenchSandbox.AppProfileId == app_context.app_profile_id,
                    WorkbenchSandbox.ConfigVersion == app_context.config_version,
                    WorkbenchSandbox.AuthEpoch == identity.auth_epoch,
                )
            )
        ).scalar()
        if sandbox is None:
            raise WorkbenchSandboxError("sandbox_not_found", 404)
        try:
            cls._assert_snapshot(sandbox, identity, app_context)
        except WorkbenchSandboxError as error:
            raise WorkbenchSandboxError("sandbox_not_found", 404) from error
        return cls._project(sandbox)

    @classmethod
    async def _owned(
        cls,
        db: AsyncSession,
        *,
        sandbox_id: str,
        conversation_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        mutation: bool,
        files: bool = False,
        lock: bool = False,
    ) -> WorkbenchSandbox:
        cls._authorize(identity, app_context, mutation=mutation, files=files)
        if not re.fullmatch(r"sbx_[0-9a-f]{32}", sandbox_id):
            raise WorkbenchSandboxError("sandbox_not_found", 404)
        normalized_conversation_id = cls._conversation_id(conversation_id)
        try:
            await cls._assert_conversation(
                db,
                conversation_id=normalized_conversation_id,
                account_id=account_id,
                identity=identity,
                app_context=app_context,
            )
        except WorkbenchSandboxError as error:
            raise WorkbenchSandboxError("sandbox_not_found", 404) from error
        statement = select(WorkbenchSandbox).where(
                    WorkbenchSandbox.SandboxId == sandbox_id,
                    WorkbenchSandbox.ConversationId
                    == normalized_conversation_id,
                    WorkbenchSandbox.AccountId == account_id,
                    WorkbenchSandbox.BindingId == identity.binding_id,
                    WorkbenchSandbox.CustomerId == identity.customer_id,
                    WorkbenchSandbox.NewApiUserId == identity.new_api_user_id,
                    WorkbenchSandbox.ApplicationId == app_context.application_id,
                )
        if lock:
            statement = statement.with_for_update()
        sandbox = (await db.execute(statement)).scalar()
        if sandbox is None:
            raise WorkbenchSandboxError("sandbox_not_found", 404)
        cls._assert_snapshot(sandbox, identity, app_context)
        return sandbox

    @classmethod
    async def query(
        cls,
        db: AsyncSession,
        *,
        sandbox_id: str,
        conversation_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        provider: ManagedSandboxProvider | None = None,
    ) -> dict[str, Any]:
        sandbox = await cls._owned(
            db,
            sandbox_id=sandbox_id,
            conversation_id=conversation_id,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            mutation=False,
        )
        if sandbox.ProviderInstanceId and sandbox.Status not in _TERMINAL_STATUSES:
            now = datetime.now(UTC).replace(tzinfo=None)
            if (
                sandbox.Status in {"pausing", "resuming", "stopping"}
                and sandbox.LeaseUntil is not None
                and sandbox.LeaseUntil > now
            ):
                await db.commit()
                return cls._project(sandbox)
            provider = provider or cls.provider_factory()
            expected_version = int(sandbox.Version)
            await db.commit()
            try:
                instance = await provider.describe(sandbox.ProviderInstanceId)
            except SandboxProviderError as error:
                applied = await cls._cas_transition(
                    db,
                    sandbox,
                    expected_version=expected_version,
                    values={
                        "Status": "provider_unknown",
                        "ErrorCode": error.code[:64],
                    },
                    event_type="query_provider_unknown",
                    outcome="unknown",
                    error_code=error.code,
                )
                if not applied:
                    raise WorkbenchSandboxError("sandbox_state_conflict", 409) from error
                raise cls._provider_error(error) from error
            if instance is None:
                values = {
                    "Status": "provider_unknown",
                    "ErrorCode": "provider_instance_missing",
                }
                event_type = "query_provider_unknown"
                outcome = "unknown"
                error_code = "provider_instance_missing"
            else:
                values = cls._instance_values(instance)
                if sandbox.Status == "stopping" and instance.status not in (
                    _TERMINAL_STATUSES
                    | {"stopping", "stopping_failed", "stop_failed"}
                ):
                    values["Status"] = "stopping"
                event_type = "query_reconciled"
                outcome = "accepted"
                error_code = None
            if not await cls._cas_transition(
                db,
                sandbox,
                expected_version=expected_version,
                values=values,
                event_type=event_type,
                outcome=outcome,
                error_code=error_code,
            ):
                raise WorkbenchSandboxError("sandbox_state_conflict", 409)
        return cls._project(sandbox)

    @classmethod
    async def lifecycle(
        cls,
        db: AsyncSession,
        *,
        action: str,
        sandbox_id: str,
        conversation_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        provider: ManagedSandboxProvider | None = None,
    ) -> dict[str, Any]:
        if action not in {"pause", "resume", "stop"}:
            raise WorkbenchSandboxError("sandbox_action_not_found", 404)
        sandbox = await cls._owned(
            db,
            sandbox_id=sandbox_id,
            conversation_id=conversation_id,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            mutation=True,
        )
        if action == "stop" and sandbox.Status == "stopped":
            return cls._project(sandbox)
        if not sandbox.ProviderInstanceId:
            raise WorkbenchSandboxError("sandbox_not_ready", 409)
        if action == "pause" and sandbox.Status == "paused":
            return cls._project(sandbox)
        if action == "resume" and sandbox.Status == "running":
            return cls._project(sandbox)
        if action == "pause" and sandbox.Status != "running":
            raise WorkbenchSandboxError("sandbox_state_conflict", 409)
        if action == "resume" and sandbox.Status != "paused":
            raise WorkbenchSandboxError("sandbox_state_conflict", 409)
        provider = provider or cls.provider_factory()
        expected_version = int(sandbox.Version)
        transition_status = {
            "stop": "stopping",
            "pause": "pausing",
            "resume": "resuming",
        }[action]
        recovering_stop = action == "stop" and sandbox.Status == "stopping"
        if recovering_stop:
            now = datetime.now(UTC).replace(tzinfo=None)
            if sandbox.LeaseUntil is not None and sandbox.LeaseUntil > now:
                await db.commit()
                return cls._project(sandbox)
            await db.commit()
            try:
                observed = await provider.describe(sandbox.ProviderInstanceId)
            except SandboxProviderError as error:
                applied = await cls._cas_transition(
                    db,
                    sandbox,
                    expected_version=expected_version,
                    values={
                        "Status": "provider_unknown",
                        "ErrorCode": error.code[:64],
                        "LeaseOwner": None,
                        "LeaseUntil": None,
                    },
                    event_type="lifecycle_stop_recovery",
                    outcome="unknown",
                    error_code=error.code,
                )
                if not applied:
                    raise WorkbenchSandboxError("sandbox_state_conflict", 409) from error
                raise cls._provider_error(error) from error
            if observed is None:
                if not await cls._cas_transition(
                    db,
                    sandbox,
                    expected_version=expected_version,
                    values={
                        "Status": "provider_unknown",
                        "ErrorCode": "provider_instance_missing",
                        "LeaseOwner": None,
                        "LeaseUntil": None,
                    },
                    event_type="lifecycle_stop_recovery",
                    outcome="unknown",
                    error_code="provider_instance_missing",
                ):
                    raise WorkbenchSandboxError("sandbox_state_conflict", 409)
                return cls._project(sandbox)
            if observed.status in (_TERMINAL_STATUSES | {"stopping", "stopping_failed", "stop_failed"}):
                if not await cls._cas_transition(
                    db,
                    sandbox,
                    expected_version=expected_version,
                    values=cls._instance_values(observed),
                    event_type="lifecycle_stop_recovery",
                    outcome="accepted",
                ):
                    raise WorkbenchSandboxError("sandbox_state_conflict", 409)
                return cls._project(sandbox)
            if not await cls._cas_transition(
                db,
                sandbox,
                expected_version=expected_version,
                values={
                    "Status": "stopping",
                    "ErrorCode": None,
                    "LeaseOwner": tagentic_config.WORKBENCH_INSTANCE_ID,
                    "LeaseUntil": now
                    + timedelta(
                        seconds=tagentic_config.WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS
                    ),
                },
                event_type="stop_recovery_requested",
                outcome="accepted",
            ):
                raise WorkbenchSandboxError("sandbox_state_conflict", 409)
            expected_version += 1
        else:
            if not await cls._cas_transition(
                db,
                sandbox,
                expected_version=expected_version,
                values={
                    "Status": transition_status,
                    "ErrorCode": None,
                    "LeaseOwner": tagentic_config.WORKBENCH_INSTANCE_ID,
                    "LeaseUntil": datetime.now(UTC).replace(tzinfo=None)
                    + timedelta(
                        seconds=tagentic_config.WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS
                    ),
                },
                event_type=f"{action}_requested",
                outcome="accepted",
            ):
                raise WorkbenchSandboxError("sandbox_state_conflict", 409)
            expected_version += 1
        try:
            if action == "stop":
                await provider.stop(sandbox.ProviderInstanceId)
                instance = await provider.describe(sandbox.ProviderInstanceId)
                if instance is None:
                    values = {
                        "Status": "provider_unknown",
                        "ErrorCode": "provider_instance_missing",
                    }
                elif instance.status in _TERMINAL_STATUSES or instance.status in {
                    "stopping",
                    "stopping_failed",
                    "stop_failed",
                }:
                    values = cls._instance_values(instance)
                else:
                    values = {"Status": "stopping", "ErrorCode": None}
            elif action == "pause":
                values = cls._instance_values(
                    await provider.pause(sandbox.ProviderInstanceId)
                )
            else:
                values = cls._instance_values(
                    await provider.resume(
                        sandbox.ProviderInstanceId,
                        timeout_seconds=sandbox.TimeoutSeconds,
                    )
                )
            if not await cls._cas_transition(
                db,
                sandbox,
                expected_version=expected_version,
                values=values,
                event_type=f"lifecycle_{action}",
                outcome="accepted",
            ):
                raise WorkbenchSandboxError("sandbox_state_conflict", 409)
        except SandboxProviderError as error:
            applied = await cls._cas_transition(
                db,
                sandbox,
                expected_version=expected_version,
                values={
                    "Status": "provider_unknown",
                    "ErrorCode": error.code[:64],
                    "LeaseOwner": None,
                    "LeaseUntil": None,
                },
                event_type=f"lifecycle_{action}",
                outcome="unknown",
                error_code=error.code,
            )
            if not applied:
                raise WorkbenchSandboxError("sandbox_state_conflict", 409) from error
            raise cls._provider_error(error) from error
        return cls._project(sandbox)

    @staticmethod
    def _output_bytes(parts: list[str]) -> int:
        return sum(len(part.encode("utf-8")) for part in parts)

    @classmethod
    async def _runtime(
        cls,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        operation: str,
    ) -> RuntimeLease:
        return await cls.runtime_guard.acquire(
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            operation=operation,
        )

    @classmethod
    async def _bounded_stop(
        cls,
        db: AsyncSession,
        sandbox: WorkbenchSandbox,
        provider: ManagedSandboxProvider,
        *,
        event_type: str,
        error_code: str,
    ) -> None:
        sandbox_id = sandbox.Id
        provider_instance_id = sandbox.ProviderInstanceId
        for _attempt in range(3):
            expected_version = int(sandbox.Version)
            if await cls._cas_transition(
                db,
                sandbox,
                expected_version=expected_version,
                values={
                    "Status": "stopping",
                    "ErrorCode": error_code[:64],
                    "LeaseOwner": tagentic_config.WORKBENCH_INSTANCE_ID,
                    "LeaseUntil": datetime.now(UTC).replace(tzinfo=None)
                    + timedelta(
                        seconds=tagentic_config.WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS
                    ),
                },
                event_type=f"{event_type}_requested",
                outcome="accepted",
                error_code=error_code,
            ):
                break
            sandbox = (
                await db.execute(
                    select(WorkbenchSandbox)
                    .where(
                        WorkbenchSandbox.Id == sandbox_id,
                        WorkbenchSandbox.ProviderInstanceId == provider_instance_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if sandbox is None or sandbox.Status in _TERMINAL_STATUSES:
                return
            now = datetime.now(UTC).replace(tzinfo=None)
            if (
                sandbox.Status == "stopping"
                and sandbox.LeaseUntil is not None
                and sandbox.LeaseUntil > now
            ):
                return
        else:
            # A safety stop must not disappear merely because a status refresh won
            # several consecutive CAS races.  The provider locator was re-read and
            # matched on every retry, so stopping it remains scoped to this row.
            status, outcome = await cls._provider_stop_outcome(
                provider,
                provider_instance_id,
            )
            previous = sandbox.Status
            sandbox.Status = status
            sandbox.ErrorCode = error_code[:64]
            sandbox.LeaseOwner = None
            sandbox.LeaseUntil = None
            sandbox.Version = int(sandbox.Version) + 1
            cls._audit(
                db,
                sandbox,
                event_type=event_type,
                outcome=outcome,
                status_from=previous,
                status_to=status,
                error_code=error_code,
            )
            db.add(sandbox)
            await db.commit()
            return
        expected_version += 1
        status, outcome = await cls._provider_stop_outcome(
            provider,
            sandbox.ProviderInstanceId,
        )
        await cls._cas_transition(
            db,
            sandbox,
            expected_version=expected_version,
            values={
                "Status": status,
                "ErrorCode": error_code[:64],
                "LeaseOwner": None,
                "LeaseUntil": None,
            },
            event_type=event_type,
            outcome=outcome,
            error_code=error_code,
        )

    @staticmethod
    async def _provider_stop_outcome(
        provider: ManagedSandboxProvider,
        provider_instance_id: str,
    ) -> tuple[str, str]:
        if await bounded_cleanup(
            lambda: provider.stop(provider_instance_id),
            timeout_seconds=tagentic_config.WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS,
        ):
            return "stopping", "accepted"
        return "provider_unknown", "unknown"

    @classmethod
    async def _bounded_stream_stop(
        cls,
        *,
        sandbox_id: str,
        provider_instance_id: str,
        provider: ManagedSandboxProvider,
        event_type: str,
        error_code: str,
    ) -> None:
        async with cls.state_session_factory() as db:
            sandbox = (
                await db.execute(
                    select(WorkbenchSandbox)
                    .where(
                        WorkbenchSandbox.SandboxId == sandbox_id,
                        WorkbenchSandbox.ProviderInstanceId == provider_instance_id,
                        ~WorkbenchSandbox.Status.in_(_TERMINAL_STATUSES),
                    )
                )
            ).scalar_one_or_none()
            if sandbox is None:
                return
            await cls._bounded_stop(
                db,
                sandbox,
                provider,
                event_type=event_type,
                error_code=error_code,
            )

    @classmethod
    async def _stream_operation_audit(
        cls,
        *,
        sandbox_id: str,
        provider_instance_id: str,
        event_type: str,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        async with cls.state_session_factory() as db:
            sandbox = (
                await db.execute(
                    select(WorkbenchSandbox).where(
                        WorkbenchSandbox.SandboxId == sandbox_id,
                        WorkbenchSandbox.ProviderInstanceId == provider_instance_id,
                    )
                )
            ).scalar_one_or_none()
            if sandbox is None:
                return
            cls._audit(
                db,
                sandbox,
                event_type=event_type,
                outcome=outcome,
                error_code=error_code,
            )
            await db.commit()

    @classmethod
    async def execute_code(
        cls,
        db: AsyncSession,
        *,
        sandbox_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        payload: dict[str, Any] | None,
        provider: ManagedSandboxProvider | None = None,
    ) -> dict[str, Any]:
        if not tagentic_config.WORKBENCH_SANDBOX_CODE_ENABLED:
            raise WorkbenchSandboxError("sandbox_code_disabled", 503)
        payload = payload if isinstance(payload, dict) else {}
        sandbox = await cls._owned(
            db,
            sandbox_id=sandbox_id,
            conversation_id=payload.get("conversation_id"),
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            mutation=True,
        )
        if sandbox.Status != "running" or not sandbox.ProviderInstanceId:
            raise WorkbenchSandboxError("sandbox_not_running", 409)
        code = payload.get("code")
        if not isinstance(code, str) or not code or len(code) > tagentic_config.WORKBENCH_SANDBOX_MAX_CODE_CHARS:
            raise WorkbenchSandboxError("code_invalid")
        language = str(payload.get("language") or "python").lower()
        if language not in _LANGUAGES:
            raise WorkbenchSandboxError("language_invalid")
        timeout = cls._bounded_int(
            payload.get("timeout_seconds"),
            default=min(60, tagentic_config.WORKBENCH_SANDBOX_MAX_COMMAND_SECONDS),
            minimum=1,
            maximum=min(
                tagentic_config.WORKBENCH_SANDBOX_MAX_COMMAND_SECONDS,
                sandbox.TimeoutSeconds,
            ),
        )
        lease = await cls._runtime(
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            operation="sandbox_code",
        )
        provider = provider or cls.provider_factory()
        await db.commit()
        try:
            result: CodeResult = await asyncio.wait_for(
                provider.execute_code(
                    sandbox.ProviderInstanceId,
                    code=code,
                    language=language,
                    timeout_seconds=timeout,
                ),
                timeout=timeout + 2,
            )
            output = [result.stdout, result.stderr, *result.results, result.error or ""]
            if cls._output_bytes(output) > tagentic_config.WORKBENCH_SANDBOX_MAX_OUTPUT_BYTES:
                await cls._bounded_stop(
                    db,
                    sandbox,
                    provider,
                    event_type="code_output_limit",
                    error_code="output_limit_exceeded",
                )
                raise WorkbenchSandboxError("output_limit_exceeded", 413)
            cls._audit(
                db,
                sandbox,
                event_type="code_execute",
                outcome="accepted",
            )
            await db.commit()
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "results": list(result.results),
                "error": result.error,
            }
        except SandboxProviderError as error:
            if error.code == "output_limit_exceeded":
                await cls._bounded_stop(
                    db,
                    sandbox,
                    provider,
                    event_type="code_output_limit",
                    error_code=error.code,
                )
            elif error.code == "sandbox_runtime_timeout":
                await cls._bounded_stop(
                    db,
                    sandbox,
                    provider,
                    event_type="code_timeout",
                    error_code=error.code,
                )
            elif error.code == "sandbox_runtime_unknown":
                await cls._bounded_stop(
                    db,
                    sandbox,
                    provider,
                    event_type="code_unknown",
                    error_code=error.code,
                )
            else:
                cls._audit(
                    db,
                    sandbox,
                    event_type="code_execute",
                    outcome="rejected",
                    error_code=error.code,
                )
                await db.commit()
            raise cls._provider_error(error) from error
        except asyncio.CancelledError:
            await cls._bounded_stop(
                db,
                sandbox,
                provider,
                event_type="code_cancelled",
                error_code="operation_cancelled",
            )
            raise
        except TimeoutError as error:
            await cls._bounded_stop(
                db,
                sandbox,
                provider,
                event_type="code_timeout",
                error_code="sandbox_runtime_timeout",
            )
            raise WorkbenchSandboxError("sandbox_runtime_timeout", 504) from error
        finally:
            await cls.runtime_guard.release(lease)

    @classmethod
    async def run_command(
        cls,
        db: AsyncSession,
        *,
        sandbox_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        payload: dict[str, Any] | None,
        provider: ManagedSandboxProvider | None = None,
    ) -> dict[str, Any]:
        sandbox, command, cwd, timeout = await cls._command_request(
            db,
            sandbox_id=sandbox_id,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            payload=payload,
        )
        lease = await cls._runtime(
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            operation="sandbox_shell",
        )
        provider = provider or cls.provider_factory()
        await db.commit()
        try:
            result: CommandResult = await asyncio.wait_for(
                provider.run_command(
                    sandbox.ProviderInstanceId,
                    command=command,
                    cwd=cwd,
                    timeout_seconds=timeout,
                ),
                timeout=timeout + 2,
            )
            if cls._output_bytes([result.stdout, result.stderr]) > tagentic_config.WORKBENCH_SANDBOX_MAX_OUTPUT_BYTES:
                await cls._bounded_stop(
                    db,
                    sandbox,
                    provider,
                    event_type="shell_output_limit",
                    error_code="output_limit_exceeded",
                )
                raise WorkbenchSandboxError("output_limit_exceeded", 413)
            cls._audit(
                db,
                sandbox,
                event_type="shell_execute",
                outcome="accepted",
            )
            await db.commit()
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
            }
        except SandboxProviderError as error:
            if error.code == "output_limit_exceeded":
                await cls._bounded_stop(
                    db,
                    sandbox,
                    provider,
                    event_type="shell_output_limit",
                    error_code=error.code,
                )
            else:
                cls._audit(
                    db,
                    sandbox,
                    event_type="shell_execute",
                    outcome="rejected",
                    error_code=error.code,
                )
                await db.commit()
            raise cls._provider_error(error) from error
        except asyncio.CancelledError:
            await cls._bounded_stop(
                db,
                sandbox,
                provider,
                event_type="shell_cancelled",
                error_code="operation_cancelled",
            )
            raise
        except TimeoutError as error:
            await cls._bounded_stop(
                db,
                sandbox,
                provider,
                event_type="shell_timeout",
                error_code="sandbox_runtime_timeout",
            )
            raise WorkbenchSandboxError("sandbox_runtime_timeout", 504) from error
        finally:
            await cls.runtime_guard.release(lease)

    @classmethod
    async def _command_request(
        cls,
        db: AsyncSession,
        *,
        sandbox_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        payload: dict[str, Any] | None,
    ) -> tuple[WorkbenchSandbox, str, str, int]:
        payload = payload if isinstance(payload, dict) else {}
        sandbox = await cls._owned(
            db,
            sandbox_id=sandbox_id,
            conversation_id=payload.get("conversation_id"),
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            mutation=True,
        )
        if sandbox.Status != "running" or not sandbox.ProviderInstanceId:
            raise WorkbenchSandboxError("sandbox_not_running", 409)
        command = payload.get("command")
        if not isinstance(command, str) or not command or len(command) > tagentic_config.WORKBENCH_SANDBOX_MAX_COMMAND_CHARS:
            raise WorkbenchSandboxError("command_invalid")
        cwd = cls.sandbox_path(payload.get("cwd") or ".")
        timeout = cls._bounded_int(
            payload.get("timeout_seconds"),
            default=min(60, tagentic_config.WORKBENCH_SANDBOX_MAX_COMMAND_SECONDS),
            minimum=1,
            maximum=min(
                tagentic_config.WORKBENCH_SANDBOX_MAX_COMMAND_SECONDS,
                sandbox.TimeoutSeconds,
            ),
        )
        return sandbox, command, cwd, timeout

    @classmethod
    async def stream_command(
        cls,
        db: AsyncSession,
        *,
        sandbox_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        payload: dict[str, Any] | None,
        provider: ManagedSandboxProvider | None = None,
    ) -> tuple[AsyncIterator[CommandChunk], RuntimeLease, int, str]:
        sandbox, command, cwd, timeout = await cls._command_request(
            db,
            sandbox_id=sandbox_id,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            payload=payload,
        )
        lease = await cls._runtime(
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            operation="sandbox_shell_stream",
        )
        provider = provider or cls.provider_factory()
        await db.commit()
        upstream = provider.stream_command(
            sandbox.ProviderInstanceId,
            command=command,
            cwd=cwd,
            timeout_seconds=timeout,
        )

        async def bounded() -> AsyncIterator[CommandChunk]:
            received = 0
            completed = False
            event_type = "stream_cancelled"
            error_code = "operation_cancelled"
            try:
                async for chunk in upstream:
                    received += len(chunk.data.encode("utf-8"))
                    if received > tagentic_config.WORKBENCH_SANDBOX_MAX_OUTPUT_BYTES:
                        event_type = "stream_output_limit"
                        error_code = "output_limit_exceeded"
                        raise WorkbenchSandboxError("output_limit_exceeded", 413)
                    yield chunk
                completed = True
                await cls._stream_operation_audit(
                    sandbox_id=sandbox.SandboxId,
                    provider_instance_id=sandbox.ProviderInstanceId,
                    event_type="shell_stream",
                    outcome="accepted",
                )
            except SandboxProviderError as error:
                event_type = (
                    "stream_output_limit"
                    if error.code == "output_limit_exceeded"
                    else "stream_provider_failed"
                )
                error_code = error.code
                raise cls._provider_error(error) from error
            except asyncio.CancelledError:
                raise
            finally:
                close_failed = not await bounded_cleanup(
                    upstream.aclose,
                    timeout_seconds=tagentic_config.WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS,
                )
                if not completed or close_failed:
                    await cls._bounded_stream_stop(
                        sandbox_id=sandbox.SandboxId,
                        provider_instance_id=sandbox.ProviderInstanceId,
                        provider=provider,
                        event_type=event_type,
                        error_code=(
                            "provider_stream_close_failed" if close_failed else error_code
                        ),
                    )

        return bounded(), lease, timeout, sandbox.ProviderInstanceId

    @staticmethod
    def sandbox_path(raw: Any) -> str:
        if not isinstance(raw, str) or not raw or len(raw) > 512:
            raise WorkbenchSandboxError("path_invalid")
        if "\\" in raw or "\x00" in raw or any(ord(char) < 32 for char in raw):
            raise WorkbenchSandboxError("path_invalid")
        path = PurePosixPath(raw)
        if path.is_absolute() or any(part in {"", ".."} for part in path.parts):
            raise WorkbenchSandboxError("path_invalid")
        parts = [part for part in path.parts if part != "."]
        if not parts:
            return "/workspace"
        if any(len(part) > 128 for part in parts):
            raise WorkbenchSandboxError("path_invalid")
        return "/workspace/" + "/".join(parts)

    @classmethod
    async def prepare_file_write(
        cls,
        db: AsyncSession,
        *,
        sandbox_id: str,
        conversation_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> int:
        limits = cls._authorize(identity, app_context, mutation=True, files=True)
        sandbox = await cls._owned(
            db,
            sandbox_id=sandbox_id,
            conversation_id=conversation_id,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            mutation=True,
            files=True,
        )
        if sandbox.Status != "running" or not sandbox.ProviderInstanceId:
            raise WorkbenchSandboxError("sandbox_not_running", 409)
        maximum = min(
            limits["max_file_bytes"],
            tagentic_config.WORKBENCH_SANDBOX_HARD_MAX_FILE_BYTES,
        )
        await db.commit()
        return maximum

    @classmethod
    async def audit_file_limit(
        cls,
        db: AsyncSession,
        *,
        sandbox_id: str,
        conversation_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> None:
        sandbox = await cls._owned(
            db,
            sandbox_id=sandbox_id,
            conversation_id=conversation_id,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            mutation=True,
            files=True,
        )
        cls._audit(
            db,
            sandbox,
            event_type="file_write_limit",
            outcome="rejected",
            error_code="file_limit_exceeded",
        )
        await db.commit()

    @classmethod
    async def read_file(
        cls,
        db: AsyncSession,
        *,
        sandbox_id: str,
        conversation_id: str,
        path: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        provider: ManagedSandboxProvider | None = None,
    ) -> bytes:
        limits = cls._authorize(identity, app_context, mutation=False, files=True)
        sandbox = await cls._owned(
            db,
            sandbox_id=sandbox_id,
            conversation_id=conversation_id,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            mutation=False,
            files=True,
        )
        if sandbox.Status != "running" or not sandbox.ProviderInstanceId:
            raise WorkbenchSandboxError("sandbox_not_running", 409)
        provider = provider or cls.provider_factory()
        maximum = min(
            limits["max_file_bytes"],
            tagentic_config.WORKBENCH_SANDBOX_HARD_MAX_FILE_BYTES,
        )
        lease = await cls._runtime(
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            operation="sandbox_file_read",
        )
        await db.commit()
        try:
            data = await provider.read_file(
                sandbox.ProviderInstanceId,
                path=cls.sandbox_path(path),
                timeout_seconds=tagentic_config.WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS,
                max_bytes=maximum,
            )
            cls._audit(
                db,
                sandbox,
                event_type="file_read",
                outcome="accepted",
            )
            await db.commit()
        except SandboxProviderError as error:
            cls._audit(
                db,
                sandbox,
                event_type="file_read",
                outcome="rejected",
                error_code=error.code,
            )
            if error.code == "file_limit_exceeded":
                cls._audit(
                    db,
                    sandbox,
                    event_type="file_read_limit",
                    outcome="rejected",
                    error_code=error.code,
                )
            await db.commit()
            raise cls._provider_error(error) from error
        finally:
            await cls.runtime_guard.release(lease)
        return data

    @classmethod
    async def write_file(
        cls,
        db: AsyncSession,
        *,
        sandbox_id: str,
        conversation_id: str,
        path: str,
        data: bytes,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        provider: ManagedSandboxProvider | None = None,
    ) -> None:
        limits = cls._authorize(identity, app_context, mutation=True, files=True)
        maximum = min(
            limits["max_file_bytes"],
            tagentic_config.WORKBENCH_SANDBOX_HARD_MAX_FILE_BYTES,
        )
        sandbox = await cls._owned(
            db,
            sandbox_id=sandbox_id,
            conversation_id=conversation_id,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            mutation=True,
            files=True,
        )
        if len(data) > maximum:
            cls._audit(
                db,
                sandbox,
                event_type="file_write_limit",
                outcome="rejected",
                error_code="file_limit_exceeded",
            )
            await db.commit()
            raise WorkbenchSandboxError("file_limit_exceeded", 413)
        if sandbox.Status != "running" or not sandbox.ProviderInstanceId:
            raise WorkbenchSandboxError("sandbox_not_running", 409)
        provider = provider or cls.provider_factory()
        lease = await cls._runtime(
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            operation="sandbox_file_write",
        )
        await db.commit()
        try:
            await provider.write_file(
                sandbox.ProviderInstanceId,
                path=cls.sandbox_path(path),
                data=data,
                timeout_seconds=tagentic_config.WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS,
            )
            cls._audit(
                db,
                sandbox,
                event_type="file_write",
                outcome="accepted",
            )
            await db.commit()
        except SandboxProviderError as error:
            cls._audit(
                db,
                sandbox,
                event_type="file_write",
                outcome="rejected",
                error_code=error.code,
            )
            await db.commit()
            raise cls._provider_error(error) from error
        finally:
            await cls.runtime_guard.release(lease)
