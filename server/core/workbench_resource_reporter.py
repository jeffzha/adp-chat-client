import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workbench_control import (
    WorkbenchControlClient,
    WorkbenchControlError,
    WorkbenchIdentityContext,
)
from model.workbench import (
    WorkbenchConversationWorkspace,
    WorkbenchResourceOutbox,
    WorkbenchWorkspace,
)


class WorkbenchResourceReportError(RuntimeError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


class WorkbenchResourceReporter:
    _ALLOWED_PARENTS = {
        "account": frozenset({""}),
        "agent": frozenset({"account"}),
        "conversation": frozenset({"agent"}),
        "workspace": frozenset({"conversation"}),
        "file": frozenset({"account", "workspace"}),
    }
    # A missing parent can be an ordering race with an earlier outbox event, so
    # 404 remains retryable. Other client errors indicate a durable scope or
    # payload mismatch and fail closed.
    _PERMANENT_CONTROL_STATUSES = frozenset({400, 401, 403, 409, 410, 422})

    @staticmethod
    async def _mark_local_scope_retry(
        db: AsyncSession,
        event: WorkbenchResourceOutbox,
        now: datetime,
        error_code: str,
    ) -> str:
        event.AttemptCount += 1
        event.Status = "retry"
        event.LeaseUntil = None
        event.LastError = error_code
        event.NextAttemptAt = now + timedelta(
            seconds=min(300, 2 ** min(event.AttemptCount, 8))
        )
        await db.commit()
        logging.warning(
            "[workbench_resource_outbox] local ownership activation failed event=%s code=%s",
            event.EventId,
            error_code,
        )
        return event.Status

    @staticmethod
    def _resource_key(resource_type: str, resource_id: str) -> str:
        digest = hashlib.sha256()
        for part in (resource_type, resource_id):
            encoded = part.encode("utf-8")
            digest.update(f"{len(encoded)}:".encode("ascii"))
            digest.update(encoded)
        return digest.hexdigest()

    @classmethod
    async def enqueue(
        cls,
        db: AsyncSession,
        *,
        identity: WorkbenchIdentityContext,
        resource_type: str,
        resource_id: str,
        parent_resource_type: str = "",
        parent_resource_id: str = "",
        source_version: int = 1,
    ) -> WorkbenchResourceOutbox:
        resource_type = str(resource_type or "").strip().lower()
        resource_id = str(resource_id or "").strip()
        parent_resource_type = str(parent_resource_type or "").strip().lower()
        parent_resource_id = str(parent_resource_id or "").strip()
        allowed_parents = cls._ALLOWED_PARENTS.get(resource_type)
        if allowed_parents is None or parent_resource_type not in allowed_parents:
            raise WorkbenchResourceReportError("invalid resource parent chain", 500)
        if (
            not resource_id
            or len(resource_id) > 255
            or (parent_resource_type and not parent_resource_id)
            or len(parent_resource_id) > 255
            or source_version <= 0
        ):
            raise WorkbenchResourceReportError("invalid resource report", 500)
        try:
            app_profile_id = int(identity.app_profile_id)
            config_version = int(identity.config_version)
        except (TypeError, ValueError) as error:
            raise WorkbenchResourceReportError("invalid control scope identifiers", 500) from error
        if (
            not identity.binding_id
            or not identity.canonical_subject
            or identity.customer_id <= 0
            or not identity.application_id
            or app_profile_id <= 0
            or config_version <= 0
        ):
            raise WorkbenchResourceReportError("incomplete control scope", 500)

        resource_key = cls._resource_key(resource_type, resource_id)
        existing = (
            await db.execute(
                select(WorkbenchResourceOutbox)
                .where(WorkbenchResourceOutbox.ResourceKeyHash == resource_key)
                .with_for_update()
            )
        ).scalar()
        expected = {
            "BindingId": identity.binding_id,
            "CanonicalSubject": identity.canonical_subject,
            "CustomerId": identity.customer_id,
            "ApplicationId": identity.application_id,
            "AppProfileId": app_profile_id,
            "ConfigVersion": config_version,
            "ResourceType": resource_type,
            "ResourceId": resource_id,
            "ParentResourceType": parent_resource_type,
            "ParentResourceId": parent_resource_id,
            "SourceVersion": source_version,
        }
        if existing is not None:
            if any(getattr(existing, key) != value for key, value in expected.items()):
                raise WorkbenchResourceReportError(
                    "resource is already queued for a different ownership scope",
                    409,
                )
            return existing

        event = WorkbenchResourceOutbox(
            EventId=f"wre_{uuid.uuid4().hex}",
            ResourceKeyHash=resource_key,
            **expected,
            Status="pending",
            AttemptCount=0,
            NextAttemptAt=datetime.now(UTC).replace(tzinfo=None),
        )
        db.add(event)
        await db.flush()
        return event

    @classmethod
    async def deliver_event(
        cls,
        db: AsyncSession,
        event_id: str,
        *,
        fail_closed_on_rejection: bool = False,
    ) -> str:
        now = datetime.now(UTC).replace(tzinfo=None)
        event = (
            await db.execute(
                select(WorkbenchResourceOutbox)
                .where(WorkbenchResourceOutbox.EventId == event_id)
                .with_for_update()
            )
        ).scalar()
        if event is None:
            raise WorkbenchResourceReportError("resource report event not found", 500)
        if event.Status in {"delivered", "rejected"}:
            if event.Status == "rejected" and fail_closed_on_rejection:
                raise WorkbenchResourceReportError("resource ownership report was rejected")
            return event.Status
        if event.Status == "delivering" and event.LeaseUntil and event.LeaseUntil > now:
            return event.Status
        event.Status = "delivering"
        event.LeaseUntil = now + timedelta(seconds=30)
        await db.commit()

        try:
            result = await WorkbenchControlClient.bind_resource(
                binding_id=event.BindingId,
                canonical_subject=event.CanonicalSubject,
                customer_id=event.CustomerId,
                application_id=event.ApplicationId,
                app_profile_id=event.AppProfileId,
                config_version=event.ConfigVersion,
                resource_type=event.ResourceType,
                resource_id=event.ResourceId,
                parent_resource_type=event.ParentResourceType,
                parent_resource_id=event.ParentResourceId,
                source_event_id=event.EventId,
                source_version=event.SourceVersion,
            )
        except WorkbenchControlError as error:
            event.AttemptCount += 1
            event.LeaseUntil = None
            event.LastError = f"control_status_{error.status_code}"
            if error.status_code in cls._PERMANENT_CONTROL_STATUSES:
                event.Status = "rejected"
                event.NextAttemptAt = now
            else:
                event.Status = "retry"
                delay_seconds = min(300, 2 ** min(event.AttemptCount, 8))
                event.NextAttemptAt = now + timedelta(seconds=delay_seconds)
            await db.commit()
            if event.Status == "rejected" and fail_closed_on_rejection:
                raise WorkbenchResourceReportError(
                    "resource ownership report was rejected",
                    error.status_code,
                ) from error
            logging.warning(
                "[workbench_resource_outbox] delivery failed event=%s status=%s",
                event.EventId,
                event.LastError,
            )
            return event.Status

        if event.ResourceType == "conversation":
            conversation_mapping = (
                await db.execute(
                    select(WorkbenchConversationWorkspace)
                    .where(
                        WorkbenchConversationWorkspace.ConversationId == event.ResourceId,
                        WorkbenchConversationWorkspace.BindingId == event.BindingId,
                        WorkbenchConversationWorkspace.CustomerId == event.CustomerId,
                        WorkbenchConversationWorkspace.ApplicationId == event.ApplicationId,
                        WorkbenchConversationWorkspace.AppProfileId == str(event.AppProfileId),
                        WorkbenchConversationWorkspace.ConfigVersion == event.ConfigVersion,
                        WorkbenchConversationWorkspace.Status.in_(("pending", "active")),
                    )
                    .with_for_update()
                )
            ).scalar()
            if conversation_mapping is None:
                return await cls._mark_local_scope_retry(
                    db,
                    event,
                    now,
                    "local_conversation_workspace_missing",
                )
            workspace = (
                await db.execute(
                    select(WorkbenchWorkspace).where(
                        WorkbenchWorkspace.WorkspaceId == conversation_mapping.WorkspaceId,
                        WorkbenchWorkspace.ConversationId == event.ResourceId,
                        WorkbenchWorkspace.BindingId == event.BindingId,
                        WorkbenchWorkspace.CustomerId == event.CustomerId,
                        WorkbenchWorkspace.ApplicationId == event.ApplicationId,
                        WorkbenchWorkspace.AppProfileId == str(event.AppProfileId),
                        WorkbenchWorkspace.ConfigVersion == event.ConfigVersion,
                        WorkbenchWorkspace.Status.in_(("pending", "active")),
                    ).limit(1)
                )
            ).scalar()
            if workspace is None:
                return await cls._mark_local_scope_retry(
                    db,
                    event,
                    now,
                    "local_conversation_workspace_scope_missing",
                )
            if workspace.Status == "active":
                conversation_mapping.Status = "active"

        if event.ResourceType == "workspace":
            workspace = (
                await db.execute(
                    select(WorkbenchWorkspace)
                    .where(
                        WorkbenchWorkspace.WorkspaceId == event.ResourceId,
                        WorkbenchWorkspace.ConversationId == event.ParentResourceId,
                        WorkbenchWorkspace.BindingId == event.BindingId,
                        WorkbenchWorkspace.CustomerId == event.CustomerId,
                        WorkbenchWorkspace.ApplicationId == event.ApplicationId,
                        WorkbenchWorkspace.AppProfileId == str(event.AppProfileId),
                        WorkbenchWorkspace.ConfigVersion == event.ConfigVersion,
                        WorkbenchWorkspace.Status.in_(("pending", "active")),
                    )
                    .with_for_update()
                )
            ).scalar()
            if workspace is None:
                return await cls._mark_local_scope_retry(
                    db,
                    event,
                    now,
                    "local_workspace_scope_missing",
                )
            conversation_mapping = (
                await db.execute(
                    select(WorkbenchConversationWorkspace)
                    .where(
                        WorkbenchConversationWorkspace.ConversationId
                        == event.ParentResourceId,
                        WorkbenchConversationWorkspace.WorkspaceId
                        == workspace.WorkspaceId,
                        WorkbenchConversationWorkspace.BindingId == event.BindingId,
                        WorkbenchConversationWorkspace.CustomerId == event.CustomerId,
                        WorkbenchConversationWorkspace.ApplicationId == event.ApplicationId,
                        WorkbenchConversationWorkspace.AppProfileId == str(event.AppProfileId),
                        WorkbenchConversationWorkspace.ConfigVersion == event.ConfigVersion,
                        WorkbenchConversationWorkspace.Status.in_(("pending", "active")),
                    )
                    .with_for_update()
                )
            ).scalar()
            if conversation_mapping is None:
                return await cls._mark_local_scope_retry(
                    db,
                    event,
                    now,
                    "local_workspace_conversation_mapping_missing",
                )
            workspace.Status = "active"
            conversation_mapping.Status = "active"

        event.Status = "delivered"
        event.LeaseUntil = None
        event.LastError = None
        event.ControlBindingId = str(result["resource_binding_id"])
        event.DeliveredAt = datetime.now(UTC).replace(tzinfo=None)
        await db.commit()
        return event.Status

    @classmethod
    async def drain(cls, db: AsyncSession, batch_size: int) -> int:
        now = datetime.now(UTC).replace(tzinfo=None)
        events = (
            await db.execute(
                select(WorkbenchResourceOutbox)
                .where(
                    or_(
                        (WorkbenchResourceOutbox.Status.in_(("pending", "retry")))
                        & (WorkbenchResourceOutbox.NextAttemptAt <= now),
                        (WorkbenchResourceOutbox.Status == "delivering")
                        & (WorkbenchResourceOutbox.LeaseUntil <= now),
                    )
                )
                .order_by(WorkbenchResourceOutbox.NextAttemptAt.asc())
                .limit(batch_size)
            )
        ).scalars().all()
        delivered = 0
        for event in events:
            status = await cls.deliver_event(db, event.EventId)
            if status == "delivered":
                delivered += 1
        return delivered
