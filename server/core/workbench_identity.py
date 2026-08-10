import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import tagentic_config
from core.account import CoreAccount
from core.session import SessionToken
from core.workbench_control import (
    WorkbenchControlClient,
    WorkbenchControlError,
    WorkbenchAppContext,
    WorkbenchIdentityContext,
)
from core.workbench_app_resolver import WorkbenchAppResolver
from core.workbench_metrics import WORKBENCH_METRICS
from core.workbench_resource_reporter import (
    WorkbenchResourceReporter,
    WorkbenchResourceReportError,
)
from model.account import AccountRole, AccountStatus
from model.workbench import WorkbenchBrowserSession, WorkbenchIdentity


class WorkbenchIdentityError(RuntimeError):
    def __init__(self, message: str, status_code: int = 403):
        super().__init__(message)
        self.status_code = status_code


class CoreWorkbenchIdentity:
    @staticmethod
    async def exchange_ticket(
        db: AsyncSession,
        ticket: str,
        browser_binding: str,
    ) -> tuple[str, WorkbenchIdentityContext]:
        if not ticket or len(ticket) > 8192:
            raise WorkbenchIdentityError("invalid workbench ticket", 400)
        if not browser_binding or len(browser_binding) > 8192:
            raise WorkbenchIdentityError("invalid SSO browser binding", 400)

        context = await WorkbenchControlClient.consume_ticket(ticket, browser_binding)
        identities = (
            await db.execute(
                select(WorkbenchIdentity)
                .where(
                    or_(
                        WorkbenchIdentity.BindingId == context.binding_id,
                        WorkbenchIdentity.CanonicalSubject == context.canonical_subject,
                    )
                )
                .with_for_update()
            )
        ).scalars().all()
        if len(identities) > 1:
            raise WorkbenchIdentityError("workbench identity binding conflict", 409)
        identity = identities[0] if identities else None

        if identity is None:
            account = await CoreAccount.create_account(db, name=context.display_name)
            account.Role = AccountRole.NORMAL
            account.Status = AccountStatus.ACTIVE
            identity = WorkbenchIdentity(
                BindingId=context.binding_id,
                CanonicalSubject=context.canonical_subject,
                AccountId=account.Id,
                CustomerId=context.customer_id,
                NewApiUserId=context.new_api_user_id,
                AuthEpoch=context.auth_epoch,
                Status="active",
            )
            db.add(identity)
        else:
            CoreWorkbenchIdentity._assert_same_identity(identity, context)
            if context.auth_epoch < identity.AuthEpoch:
                raise WorkbenchIdentityError("stale workbench identity epoch")
            account = await CoreAccount.get(db, str(identity.AccountId))
            if account is None:
                raise WorkbenchIdentityError("shadow account is missing", 409)
            if account.Role != AccountRole.NORMAL:
                raise WorkbenchIdentityError("shadow account cannot have an administrative role", 409)
            identity.AuthEpoch = context.auth_epoch
            identity.Status = "active"
            account.Name = context.display_name
            account.Status = AccountStatus.ACTIVE

        now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
        expires_at = now + timedelta(
            minutes=tagentic_config.WORKBENCH_SESSION_EXPIRE_MINUTES
        )
        session_id = secrets.token_urlsafe(32)
        identity.LastAuthenticatedAt = now
        account.LastLoginAt = now
        db.add(
            WorkbenchBrowserSession(
                SessionIdDigest=hashlib.sha256(session_id.encode("ascii")).hexdigest(),
                BindingId=context.binding_id,
                AccountId=account.Id,
                CustomerId=context.customer_id,
                NewApiUserId=context.new_api_user_id,
                CanonicalSubject=context.canonical_subject,
                AuthEpoch=context.auth_epoch,
                ApplicationId=context.application_id,
                AppProfileId=context.app_profile_id,
                ConfigVersion=context.config_version,
                AuthenticatedAt=now,
                ExpiresAt=expires_at,
                Status="active",
            )
        )
        db.add(account)
        db.add(identity)
        account_report = await WorkbenchResourceReporter.enqueue(
            db,
            identity=context,
            resource_type="account",
            resource_id=str(account.Id),
        )
        await db.commit()

        try:
            await WorkbenchControlClient.confirm_identity(
                binding_id=context.binding_id,
                canonical_subject=context.canonical_subject,
                adp_account_id=str(account.Id),
                adp_account_version=1,
            )
        except WorkbenchControlError as error:
            WORKBENCH_METRICS.inc(
                "workbench_identity_bind_total", result="failure"
            )
            raise WorkbenchIdentityError(str(error), error.status_code) from error
        WORKBENCH_METRICS.inc("workbench_identity_bind_total", result="success")

        try:
            await WorkbenchResourceReporter.deliver_event(
                db,
                account_report.EventId,
                fail_closed_on_rejection=True,
            )
        except WorkbenchResourceReportError as error:
            raise WorkbenchIdentityError(str(error), error.status_code) from error

        token = CoreWorkbenchIdentity._create_session_token(
            account.Id,
            identity,
            context,
            session_id=session_id,
            authenticated_at=now,
            expires_at=expires_at,
        )
        return token, context

    @staticmethod
    async def authorize_session(
        db: AsyncSession,
        claims: dict[str, Any],
        *,
        method: str,
        resource_path: str,
        supplied_application_id: str | None,
    ) -> tuple[WorkbenchIdentityContext, WorkbenchAppContext]:
        binding_id = str(claims.get("BindingId") or "")
        canonical_subject = str(claims.get("Subject") or "")
        account_id = str(claims.get("AccountId") or "")
        try:
            auth_epoch = int(claims.get("AuthEpoch"))
        except (TypeError, ValueError) as error:
            raise WorkbenchIdentityError("invalid workbench session epoch") from error
        if not binding_id or not canonical_subject or not account_id:
            raise WorkbenchIdentityError("invalid workbench session")

        identity = (
            await db.execute(
                select(WorkbenchIdentity).where(WorkbenchIdentity.AccountId == account_id)
            )
        ).scalar()
        if identity is None or identity.Status != "active":
            raise WorkbenchIdentityError("workbench identity is not active")
        if (
            identity.BindingId != binding_id
            or identity.CanonicalSubject != canonical_subject
            or identity.AuthEpoch != auth_epoch
        ):
            raise WorkbenchIdentityError("workbench session no longer matches its identity")

        account = await CoreAccount.get(db, account_id)
        if account is None:
            raise WorkbenchIdentityError("shadow account is missing", 409)
        if account.Status != AccountStatus.ACTIVE or account.Role != AccountRole.NORMAL:
            raise WorkbenchIdentityError("shadow account is not active")

        try:
            session_config_version = int(claims.get("ConfigVersion"))
        except (TypeError, ValueError) as error:
            raise WorkbenchIdentityError("invalid workbench browser session") from error
        session_application_id = str(claims.get("ApplicationId") or "")
        session_app_profile_id = str(claims.get("AppProfileId") or "")
        if not session_application_id or not session_app_profile_id or session_config_version <= 0:
            raise WorkbenchIdentityError("invalid workbench browser session")
        browser_session = await CoreWorkbenchIdentity.require_browser_session(
            db,
            claims=claims,
            account_id=account_id,
            identity=WorkbenchIdentityContext(
                binding_id=identity.BindingId,
                canonical_subject=identity.CanonicalSubject,
                customer_id=identity.CustomerId,
                new_api_user_id=identity.NewApiUserId,
                auth_epoch=identity.AuthEpoch,
                display_name="",
                application_id=session_application_id,
                app_profile_id=session_app_profile_id,
                access_mode="active",
                config_version=session_config_version,
            ),
        )

        try:
            context = await WorkbenchControlClient.authorize(
                binding_id=binding_id,
                canonical_subject=canonical_subject,
                auth_epoch=auth_epoch,
                method=method,
                resource_path=resource_path,
            )
        except WorkbenchControlError as error:
            raise WorkbenchIdentityError(str(error), error.status_code) from error
        CoreWorkbenchIdentity._assert_same_identity(identity, context)
        if context.auth_epoch != identity.AuthEpoch:
            raise WorkbenchIdentityError("workbench identity epoch was revoked")
        if supplied_application_id and supplied_application_id != context.application_id:
            raise WorkbenchIdentityError("application does not belong to the active workbench context")
        try:
            requested_app_profile_id = int(context.app_profile_id)
            if requested_app_profile_id <= 0:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise WorkbenchIdentityError("workbench App profile identifier is invalid", 409) from error
        try:
            app_context = await WorkbenchControlClient.get_app_context(
                binding_id=binding_id,
                canonical_subject=canonical_subject,
                auth_epoch=auth_epoch,
                requested_app_profile_id=requested_app_profile_id,
                requested_config_version=context.config_version,
                purpose="interactive",
            )
            if (
                app_context.application_id != context.application_id
                or app_context.app_profile_id != context.app_profile_id
                or app_context.config_version != context.config_version
                or app_context.auth_epoch != context.auth_epoch
                or app_context.application_id != browser_session.ApplicationId
                or app_context.app_profile_id != browser_session.AppProfileId
                or app_context.config_version != browser_session.ConfigVersion
            ):
                raise WorkbenchIdentityError("workbench app context does not match authorization", 409)
            await WorkbenchAppResolver.ensure_vendor(app_context)
        except WorkbenchControlError as error:
            raise WorkbenchIdentityError(str(error), error.status_code) from error
        return context, app_context

    @staticmethod
    async def require_browser_session(
        db: AsyncSession,
        *,
        claims: dict[str, Any],
        account_id: str,
        identity: WorkbenchIdentityContext,
        maximum_age_seconds: int | None = None,
    ) -> WorkbenchBrowserSession:
        session_id = str(claims.get("sid") or "")
        try:
            auth_time = int(claims.get("auth_time"))
        except (TypeError, ValueError) as error:
            raise WorkbenchIdentityError("invalid workbench browser session") from error
        if (
            len(session_id) < 40
            or len(session_id) > 128
            or not all(char.isalnum() or char in "-_" for char in session_id)
            or auth_time <= 0
        ):
            raise WorkbenchIdentityError("invalid workbench browser session")
        authenticated_at = datetime.fromtimestamp(auth_time, UTC).replace(tzinfo=None)
        now = datetime.now(UTC).replace(tzinfo=None)
        try:
            account_key = uuid.UUID(str(account_id))
        except (TypeError, ValueError, AttributeError) as error:
            raise WorkbenchIdentityError("invalid workbench browser session") from error
        row = (
            await db.execute(
                select(WorkbenchBrowserSession).where(
                    WorkbenchBrowserSession.SessionIdDigest
                    == hashlib.sha256(session_id.encode("ascii")).hexdigest(),
                    WorkbenchBrowserSession.BindingId == identity.binding_id,
                    WorkbenchBrowserSession.AccountId == account_key,
                    WorkbenchBrowserSession.CustomerId == identity.customer_id,
                    WorkbenchBrowserSession.NewApiUserId == identity.new_api_user_id,
                    WorkbenchBrowserSession.CanonicalSubject == identity.canonical_subject,
                    WorkbenchBrowserSession.AuthEpoch == identity.auth_epoch,
                    WorkbenchBrowserSession.ApplicationId == identity.application_id,
                    WorkbenchBrowserSession.AppProfileId == identity.app_profile_id,
                    WorkbenchBrowserSession.ConfigVersion == identity.config_version,
                    WorkbenchBrowserSession.Status == "active",
                    WorkbenchBrowserSession.ExpiresAt > now,
                )
            )
        ).scalar()
        if row is None or row.AuthenticatedAt != authenticated_at:
            raise WorkbenchIdentityError("workbench browser session is not active")
        if maximum_age_seconds is not None:
            cutoff = now - timedelta(seconds=maximum_age_seconds)
            if row.AuthenticatedAt < cutoff:
                raise WorkbenchIdentityError(
                    "recent workbench authentication is required", 401
                )
        return row

    @staticmethod
    async def authorize_offline_scope(
        db: AsyncSession,
        *,
        account_id: str,
        binding_id: str,
        canonical_subject: str,
        customer_id: int,
        new_api_user_id: int,
        auth_epoch: int,
        application_id: str,
        app_profile_id: str,
        config_version: int,
        method: str = "POST",
        resource_path: str = "/workbench/scheduled-tasks/execute",
        purpose: str = "scheduled_task",
    ) -> tuple[WorkbenchIdentityContext, WorkbenchAppContext]:
        """Re-introspect a persisted offline scope without minting a browser session.

        Every identifier comes from the stored delegation, never from a worker
        payload.  Exact epoch/profile/version comparisons make customer, member,
        App and plan changes revoke pending work before a provider side effect.
        """

        identity = (
            await db.execute(
                select(WorkbenchIdentity).where(
                    WorkbenchIdentity.AccountId == account_id,
                    WorkbenchIdentity.BindingId == binding_id,
                    WorkbenchIdentity.CanonicalSubject == canonical_subject,
                    WorkbenchIdentity.CustomerId == customer_id,
                    WorkbenchIdentity.NewApiUserId == new_api_user_id,
                    WorkbenchIdentity.AuthEpoch == auth_epoch,
                    WorkbenchIdentity.Status == "active",
                )
            )
        ).scalar()
        if identity is None:
            raise WorkbenchIdentityError("offline workbench identity is revoked")
        account = await CoreAccount.get(db, account_id)
        if (
            account is None
            or account.Status != AccountStatus.ACTIVE
            or account.Role != AccountRole.NORMAL
        ):
            raise WorkbenchIdentityError("offline shadow account is not active")

        try:
            exact_app_profile_id = int(app_profile_id)
        except (TypeError, ValueError) as error:
            raise WorkbenchIdentityError("offline App profile identifier is invalid", 409) from error
        if exact_app_profile_id <= 0 or config_version <= 0:
            raise WorkbenchIdentityError("offline App scope is invalid", 409)

        try:
            context = await WorkbenchControlClient.authorize(
                binding_id=binding_id,
                canonical_subject=canonical_subject,
                auth_epoch=auth_epoch,
                method=method,
                resource_path=resource_path,
                customer_id=customer_id,
                app_profile_id=exact_app_profile_id,
                config_version=config_version,
            )
            app_context = await WorkbenchControlClient.get_app_context(
                binding_id=binding_id,
                canonical_subject=canonical_subject,
                auth_epoch=auth_epoch,
                requested_app_profile_id=exact_app_profile_id,
                requested_config_version=config_version,
                purpose=purpose,
            )
        except WorkbenchControlError as error:
            raise WorkbenchIdentityError(str(error), error.status_code) from error
        CoreWorkbenchIdentity._assert_same_identity(identity, context)
        if (
            context.customer_id != customer_id
            or context.new_api_user_id != new_api_user_id
            or context.auth_epoch != auth_epoch
            or context.application_id != application_id
            or str(context.app_profile_id) != str(app_profile_id)
            or int(context.config_version) != int(config_version)
            or app_context.auth_epoch != auth_epoch
            or app_context.application_id != application_id
            or str(app_context.app_profile_id) != str(app_profile_id)
            or int(app_context.config_version) != int(config_version)
        ):
            raise WorkbenchIdentityError("offline workbench scope is stale", 409)
        try:
            await WorkbenchAppResolver.ensure_vendor(app_context)
        except WorkbenchControlError as error:
            raise WorkbenchIdentityError(str(error), error.status_code) from error
        return context, app_context

    @staticmethod
    def _assert_same_identity(
        identity: WorkbenchIdentity,
        context: WorkbenchIdentityContext,
    ) -> None:
        if (
            identity.BindingId != context.binding_id
            or identity.CanonicalSubject != context.canonical_subject
            or identity.CustomerId != context.customer_id
            or identity.NewApiUserId != context.new_api_user_id
        ):
            raise WorkbenchIdentityError("workbench identity binding conflict", 409)

    @staticmethod
    def _create_session_token(
        account_id: Any,
        identity: WorkbenchIdentity,
        context: WorkbenchIdentityContext,
        *,
        session_id: str,
        authenticated_at: datetime,
        expires_at: datetime,
    ) -> str:
        return SessionToken.create(
            {
                "AccountId": str(account_id),
                "BindingId": identity.BindingId,
                "Subject": identity.CanonicalSubject,
                "CustomerId": identity.CustomerId,
                "NewApiUserId": identity.NewApiUserId,
                "AuthEpoch": identity.AuthEpoch,
                "ApplicationId": context.application_id,
                "AppProfileId": context.app_profile_id,
                "ConfigVersion": context.config_version,
                "sid": session_id,
                "auth_time": int(authenticated_at.replace(tzinfo=UTC).timestamp()),
                "token_source": "workbench_sso",
                "exp": int(expires_at.replace(tzinfo=UTC).timestamp()),
            }
        )
