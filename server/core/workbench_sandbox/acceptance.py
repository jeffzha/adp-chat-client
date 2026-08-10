import hmac
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import tagentic_config
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_identity import CoreWorkbenchIdentity
from core.workbench_sandbox.contracts import (
    ManagedSandboxProvider,
    ProviderInstance,
    SandboxProviderError,
)
from core.workbench_sandbox.provider import TencentAGSXProvider
from core.workbench_sandbox.secrets import read_secret_file
from core.workbench_sandbox.service import WorkbenchSandboxError, WorkbenchSandboxService
from model.workbench_sandbox import WorkbenchSandbox
from model.workbench_sandbox_acceptance import WorkbenchSandboxAcceptanceEvent


_FAULT_MODE = "provider_start_response_lost"
_TERMINAL_PROVIDER_STATUSES = {"stopped", "failed", "starting_failed"}
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/=-]{32,256}")


def _uuid(raw: Any, *, code: str) -> str:
    try:
        return str(uuid.UUID(str(raw or "").strip()))
    except (ValueError, TypeError, AttributeError) as error:
        raise WorkbenchSandboxError(code) from error


def _expected_origin() -> tuple[str, str]:
    parsed = urlsplit(str(tagentic_config.WORKBENCH_PUBLIC_BASE_URL or "").strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise WorkbenchSandboxError("acceptance_configuration_invalid", 503)
    try:
        port = parsed.port
    except ValueError as error:
        raise WorkbenchSandboxError("acceptance_configuration_invalid", 503) from error
    if port not in (None, 443):
        raise WorkbenchSandboxError("acceptance_configuration_invalid", 503)
    host = parsed.hostname.lower().rstrip(".")
    return f"https://{host}", host


def _acceptance_token() -> str:
    try:
        value = read_secret_file(
            tagentic_config.WORKBENCH_SANDBOX_ACCEPTANCE_TOKEN_FILE,
            minimum_bytes=32,
            maximum_bytes=256,
        ).decode("ascii")
    except (OSError, UnicodeDecodeError) as error:
        raise WorkbenchSandboxError("acceptance_configuration_invalid", 503) from error
    if _TOKEN_PATTERN.fullmatch(value) is None:
        raise WorkbenchSandboxError("acceptance_configuration_invalid", 503)
    return value


@dataclass(frozen=True)
class WorkbenchSandboxAcceptanceContext:
    acceptance_run_id: str
    conversation_id: str
    account_id: str
    identity: WorkbenchIdentityContext
    app_context: WorkbenchAppContext

    async def observe_provider_start(
        self,
        db: AsyncSession,
        sandbox: WorkbenchSandbox,
        instance: ProviderInstance,
    ) -> None:
        if (
            sandbox.AccountId != self.account_id
            or sandbox.BindingId != self.identity.binding_id
            or sandbox.CustomerId != self.identity.customer_id
            or sandbox.NewApiUserId != self.identity.new_api_user_id
            or sandbox.ApplicationId != self.app_context.application_id
            or sandbox.AppProfileId != self.app_context.app_profile_id
            or sandbox.ConfigVersion != self.app_context.config_version
            or sandbox.AuthEpoch != self.identity.auth_epoch
            or sandbox.ConversationId != self.conversation_id
        ):
            raise WorkbenchSandboxError("acceptance_scope_mismatch", 409)
        db.add(
            WorkbenchSandboxAcceptanceEvent(
                AcceptanceRunId=self.acceptance_run_id,
                AccountId=self.account_id,
                BindingId=self.identity.binding_id,
                CustomerId=self.identity.customer_id,
                NewApiUserId=self.identity.new_api_user_id,
                ApplicationId=self.app_context.application_id,
                AppProfileId=self.app_context.app_profile_id,
                ConfigVersion=self.app_context.config_version,
                AuthEpoch=self.identity.auth_epoch,
                ConversationId=self.conversation_id,
                SandboxId=sandbox.SandboxId,
                Generation=sandbox.Generation,
                ProviderInstanceId=instance.instance_id,
                FaultMode=_FAULT_MODE,
                CleanupStatus="pending",
                CleanupAttempts=0,
            )
        )
        # Evidence must survive the deliberate response-loss exception. The normal
        # sandbox state transition then records provider_unknown in its own commit.
        await db.commit()
        raise SandboxProviderError(
            "acceptance_provider_response_lost",
            status_code=503,
            retryable=False,
            instance_id=instance.instance_id,
        )


class WorkbenchSandboxAcceptance:
    provider_factory: Callable[[], ManagedSandboxProvider] = TencentAGSXProvider

    @staticmethod
    def validate_readiness() -> None:
        if not tagentic_config.WORKBENCH_SANDBOX_ACCEPTANCE_FAULTS_ENABLED:
            return
        if (
            tagentic_config.WORKBENCH_DEPLOYMENT_TIER != "acceptance"
            or not tagentic_config.WORKBENCH_MODE
            or not tagentic_config.WORKBENCH_SANDBOX_ENABLED
        ):
            raise RuntimeError(
                "sandbox acceptance faults require the isolated acceptance deployment tier"
            )
        try:
            _expected_origin()
            _acceptance_token()
        except WorkbenchSandboxError as error:
            raise RuntimeError(error.code) from error

    @staticmethod
    def _require_enabled() -> None:
        if (
            not tagentic_config.WORKBENCH_SANDBOX_ACCEPTANCE_FAULTS_ENABLED
            or tagentic_config.WORKBENCH_DEPLOYMENT_TIER != "acceptance"
        ):
            raise WorkbenchSandboxError("acceptance_not_found", 404)

    @classmethod
    async def authorize(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        session_claims: dict[str, Any],
        headers: Any,
        request_host: str,
        acceptance_run_id: Any,
        conversation_id: Any,
    ) -> WorkbenchSandboxAcceptanceContext:
        cls._require_enabled()
        expected_origin, expected_host = _expected_origin()
        supplied_origin = str(headers.get("Origin") or "").strip().lower()
        supplied_host = str(request_host or "").strip().lower().rstrip(".")
        if supplied_origin not in {expected_origin, f"{expected_origin}:443"} or (
            supplied_host not in {expected_host, f"{expected_host}:443"}
        ):
            raise WorkbenchSandboxError("acceptance_same_origin_required", 403)
        supplied_token = str(headers.get("X-Workbench-Acceptance-Token") or "")
        expected_token = _acceptance_token()
        if not hmac.compare_digest(supplied_token, expected_token):
            raise WorkbenchSandboxError("acceptance_not_found", 404)

        run_id = _uuid(acceptance_run_id, code="acceptance_run_id_invalid")
        exact_conversation_id = WorkbenchSandboxService._conversation_id(conversation_id)
        WorkbenchSandboxService._authorize(identity, app_context, mutation=True)
        await CoreWorkbenchIdentity.require_browser_session(
            db,
            claims=session_claims,
            account_id=account_id,
            identity=identity,
            maximum_age_seconds=(
                tagentic_config.WORKBENCH_SANDBOX_ACCEPTANCE_REAUTH_SECONDS
            ),
        )
        await WorkbenchSandboxService._assert_conversation(
            db,
            conversation_id=exact_conversation_id,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
        )
        return WorkbenchSandboxAcceptanceContext(
            acceptance_run_id=run_id,
            conversation_id=exact_conversation_id,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
        )

    @classmethod
    async def fault_context_from_request(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        session_claims: dict[str, Any],
        headers: Any,
        request_host: str,
        conversation_id: Any,
    ) -> WorkbenchSandboxAcceptanceContext | None:
        fault_mode = str(headers.get("X-Workbench-Acceptance-Fault") or "")
        has_acceptance_header = any(
            headers.get(name)
            for name in (
                "X-Workbench-Acceptance-Fault",
                "X-Workbench-Acceptance-Run-Id",
                "X-Workbench-Acceptance-Token",
            )
        )
        if not has_acceptance_header:
            return None
        if fault_mode != _FAULT_MODE:
            cls._require_enabled()
            raise WorkbenchSandboxError("acceptance_fault_invalid")
        return await cls.authorize(
            db,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            session_claims=session_claims,
            headers=headers,
            request_host=request_host,
            acceptance_run_id=headers.get("X-Workbench-Acceptance-Run-Id"),
            conversation_id=conversation_id,
        )

    @staticmethod
    async def _events(
        db: AsyncSession,
        context: WorkbenchSandboxAcceptanceContext,
    ) -> list[WorkbenchSandboxAcceptanceEvent]:
        return list(
            (
                await db.execute(
                    select(WorkbenchSandboxAcceptanceEvent)
                    .where(
                        WorkbenchSandboxAcceptanceEvent.AcceptanceRunId
                        == context.acceptance_run_id,
                        WorkbenchSandboxAcceptanceEvent.AccountId
                        == context.account_id,
                        WorkbenchSandboxAcceptanceEvent.BindingId
                        == context.identity.binding_id,
                        WorkbenchSandboxAcceptanceEvent.CustomerId
                        == context.identity.customer_id,
                        WorkbenchSandboxAcceptanceEvent.NewApiUserId
                        == context.identity.new_api_user_id,
                        WorkbenchSandboxAcceptanceEvent.ApplicationId
                        == context.app_context.application_id,
                        WorkbenchSandboxAcceptanceEvent.AppProfileId
                        == context.app_context.app_profile_id,
                        WorkbenchSandboxAcceptanceEvent.ConfigVersion
                        == context.app_context.config_version,
                        WorkbenchSandboxAcceptanceEvent.AuthEpoch
                        == context.identity.auth_epoch,
                        WorkbenchSandboxAcceptanceEvent.ConversationId
                        == context.conversation_id,
                    )
                    .order_by(WorkbenchSandboxAcceptanceEvent.CreatedAt.asc())
                )
            )
            .scalars()
            .all()
        )

    @staticmethod
    def _project(
        context: WorkbenchSandboxAcceptanceContext,
        events: list[WorkbenchSandboxAcceptanceEvent],
    ) -> dict[str, Any]:
        return {
            "acceptance_run_id": context.acceptance_run_id,
            "conversation_id": context.conversation_id,
            "provider_start_count": len(events),
            "instances": [
                {
                    "sandbox_id": event.SandboxId,
                    "generation": event.Generation,
                    "fault_mode": event.FaultMode,
                    "cleanup_status": event.CleanupStatus,
                    "cleanup_attempts": event.CleanupAttempts,
                    "cleanup_error_code": event.CleanupErrorCode,
                    "provider_start_observed_at": (
                        event.CreatedAt.isoformat() + "Z" if event.CreatedAt else None
                    ),
                }
                for event in events
            ],
        }

    @classmethod
    async def report(
        cls,
        db: AsyncSession,
        context: WorkbenchSandboxAcceptanceContext,
    ) -> dict[str, Any]:
        return cls._project(context, await cls._events(db, context))

    @classmethod
    async def cleanup(
        cls,
        db: AsyncSession,
        context: WorkbenchSandboxAcceptanceContext,
        *,
        provider: ManagedSandboxProvider | None = None,
    ) -> dict[str, Any]:
        events = await cls._events(db, context)
        provider = provider or cls.provider_factory()
        cleanup_requested = 0
        cleanup_succeeded = 0
        cleanup_failed = 0
        completed_locators: dict[str, tuple[str, str | None]] = {}

        for event in events:
            if event.ProviderInstanceId in completed_locators:
                event.CleanupStatus, event.CleanupErrorCode = completed_locators[
                    event.ProviderInstanceId
                ]
                continue
            if event.CleanupStatus in {"stopped", "not_found"}:
                completed_locators[event.ProviderInstanceId] = (
                    event.CleanupStatus,
                    event.CleanupErrorCode,
                )
                cleanup_succeeded += 1
                continue
            cleanup_requested += 1
            event.CleanupAttempts = int(event.CleanupAttempts or 0) + 1
            try:
                await provider.stop(event.ProviderInstanceId)
                observed = await provider.describe(event.ProviderInstanceId)
                event.CleanupStatus = (
                    "stopped"
                    if observed is None or observed.status in _TERMINAL_PROVIDER_STATUSES
                    else "stop_requested"
                )
                event.CleanupErrorCode = None
                cleanup_succeeded += 1
            except Exception as error:  # Provider SDK failures are isolated per instance.
                if (
                    isinstance(error, SandboxProviderError)
                    and error.code == "provider_instance_not_found"
                ):
                    event.CleanupStatus = "not_found"
                    event.CleanupErrorCode = None
                    cleanup_succeeded += 1
                else:
                    event.CleanupStatus = "failed"
                    event.CleanupErrorCode = "sandbox_cleanup_failed"
                    cleanup_failed += 1
            completed_locators[event.ProviderInstanceId] = (
                event.CleanupStatus,
                event.CleanupErrorCode,
            )

            sandbox = (
                await db.execute(
                    select(WorkbenchSandbox).where(
                        WorkbenchSandbox.SandboxId == event.SandboxId,
                        WorkbenchSandbox.Generation == event.Generation,
                        WorkbenchSandbox.ProviderInstanceId == event.ProviderInstanceId,
                        WorkbenchSandbox.AccountId == context.account_id,
                        WorkbenchSandbox.BindingId == context.identity.binding_id,
                        WorkbenchSandbox.CustomerId == context.identity.customer_id,
                        WorkbenchSandbox.NewApiUserId
                        == context.identity.new_api_user_id,
                        WorkbenchSandbox.ApplicationId
                        == context.app_context.application_id,
                        WorkbenchSandbox.ConversationId == context.conversation_id,
                    )
                )
            ).scalar()
            if sandbox is not None and event.CleanupStatus in {
                "stopped",
                "not_found",
                "stop_requested",
            }:
                previous = sandbox.Status
                local_status = (
                    "stopping" if event.CleanupStatus == "stop_requested" else "stopped"
                )
                sandbox.Status = local_status
                sandbox.ErrorCode = (
                    None if local_status == "stopping" else "acceptance_cleanup"
                )
                sandbox.LeaseOwner = None
                sandbox.LeaseUntil = None
                sandbox.Version = int(sandbox.Version or 0) + 1
                WorkbenchSandboxService._audit(
                    db,
                    sandbox,
                    event_type="acceptance_cleanup",
                    outcome="accepted",
                    status_from=previous,
                    status_to=local_status,
                    error_code=(
                        "acceptance_cleanup" if local_status == "stopped" else None
                    ),
                )

        await db.commit()
        result = cls._project(context, events)
        result.update(
            {
                "cleanup_requested": cleanup_requested,
                "cleanup_succeeded": cleanup_succeeded,
                "cleanup_failed": cleanup_failed,
                "complete": cleanup_failed == 0
                and all(
                    event.CleanupStatus in {"stopped", "not_found"}
                    for event in events
                ),
                "completed_at": datetime.now(UTC).isoformat(),
            }
        )
        return result
