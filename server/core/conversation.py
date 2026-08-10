from datetime import UTC, datetime
from dataclasses import dataclass
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, delete, and_, or_
from sanic.exceptions import SanicException
from model.chat import ChatConversation, ChatRecord
from model.workbench import WorkbenchConversationWorkspace, WorkbenchWorkspace
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_resource_reporter import (
    WorkbenchResourceReporter,
    WorkbenchResourceReportError,
)
from core.workbench_workspace import CoreWorkbenchWorkspace, WorkbenchWorkspaceError
from core.workbench_app_lineage import WorkbenchAppLineageStore


@dataclass(frozen=True)
class WorkbenchConversationReadContext:
    application_id: str
    provider_app_id: str
    app_profile_id: str
    config_version: int


class CoreConversation:
    @staticmethod
    def _workbench_scope_filters(
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> tuple:
        CoreWorkbenchWorkspace._assert_scope(identity, app_context)
        return (
            WorkbenchConversationWorkspace.BindingId == identity.binding_id,
            WorkbenchConversationWorkspace.AccountId == account_id,
            WorkbenchConversationWorkspace.CustomerId == identity.customer_id,
            WorkbenchConversationWorkspace.ApplicationId == app_context.application_id,
            WorkbenchConversationWorkspace.ProviderAppId == app_context.app_id,
            WorkbenchConversationWorkspace.AppProfileId == str(app_context.app_profile_id),
            WorkbenchConversationWorkspace.ConfigVersion == int(app_context.config_version),
            WorkbenchConversationWorkspace.Status == "active",
            WorkbenchWorkspace.BindingId == identity.binding_id,
            WorkbenchWorkspace.AccountId == account_id,
            WorkbenchWorkspace.CustomerId == identity.customer_id,
            WorkbenchWorkspace.ApplicationId == app_context.application_id,
            WorkbenchWorkspace.ProviderAppId == app_context.app_id,
            WorkbenchWorkspace.AppProfileId == str(app_context.app_profile_id),
            WorkbenchWorkspace.ConfigVersion == int(app_context.config_version),
            WorkbenchWorkspace.Status == "active",
            ChatConversation.AccountId == account_id,
            ChatConversation.ApplicationId == app_context.application_id,
        )

    @classmethod
    def _owned_statement(
        cls,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ):
        return (
            select(ChatConversation)
            .join(
                WorkbenchConversationWorkspace,
                WorkbenchConversationWorkspace.ConversationId == ChatConversation.Id,
            )
            .join(
                WorkbenchWorkspace,
                WorkbenchWorkspace.WorkspaceId == WorkbenchConversationWorkspace.WorkspaceId,
            )
            .where(*cls._workbench_scope_filters(account_id, identity, app_context))
        )

    @classmethod
    async def _readable_statement(
        cls,
        db: AsyncSession,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        include_mapping: bool = False,
    ):
        CoreWorkbenchWorkspace._assert_scope(identity, app_context)
        tuples = (
            (
                app_context.application_id,
                app_context.app_id,
                str(app_context.app_profile_id),
                int(app_context.config_version),
            ),
            *await WorkbenchAppLineageStore.readable_source_tuples(
                db,
                identity,
                app_context,
            ),
        )
        tuple_filters = [
            and_(
                ChatConversation.ApplicationId == application_id,
                WorkbenchConversationWorkspace.ApplicationId == application_id,
                WorkbenchConversationWorkspace.ProviderAppId == provider_app_id,
                WorkbenchConversationWorkspace.AppProfileId == app_profile_id,
                WorkbenchConversationWorkspace.ConfigVersion == config_version,
                WorkbenchWorkspace.ApplicationId == application_id,
                WorkbenchWorkspace.ProviderAppId == provider_app_id,
                WorkbenchWorkspace.AppProfileId == app_profile_id,
                WorkbenchWorkspace.ConfigVersion == config_version,
            )
            for application_id, provider_app_id, app_profile_id, config_version in tuples
        ]
        selected = (ChatConversation, WorkbenchConversationWorkspace) if include_mapping else (ChatConversation,)
        return (
            select(*selected)
            .join(
                WorkbenchConversationWorkspace,
                WorkbenchConversationWorkspace.ConversationId == ChatConversation.Id,
            )
            .join(
                WorkbenchWorkspace,
                WorkbenchWorkspace.WorkspaceId == WorkbenchConversationWorkspace.WorkspaceId,
            )
            .where(
                WorkbenchConversationWorkspace.BindingId == identity.binding_id,
                WorkbenchConversationWorkspace.AccountId == account_id,
                WorkbenchConversationWorkspace.CustomerId == identity.customer_id,
                WorkbenchConversationWorkspace.Status == "active",
                WorkbenchWorkspace.BindingId == identity.binding_id,
                WorkbenchWorkspace.AccountId == account_id,
                WorkbenchWorkspace.CustomerId == identity.customer_id,
                WorkbenchWorkspace.Status == "active",
                ChatConversation.AccountId == account_id,
                or_(*tuple_filters),
            )
        )

    @staticmethod
    async def list(
        db: AsyncSession,
        account_id: str,
        application_id: str = None,
        workbench_identity: WorkbenchIdentityContext | None = None,
        workbench_app_context: WorkbenchAppContext | None = None,
    ) -> list[ChatConversation]:
        if (workbench_identity is None) != (workbench_app_context is None):
            raise SanicException("incomplete workbench conversation scope", status_code=500)
        if workbench_identity is not None:
            try:
                stmt = await CoreConversation._readable_statement(
                    db,
                    account_id,
                    workbench_identity,
                    workbench_app_context,
                )
            except WorkbenchWorkspaceError as error:
                raise SanicException(str(error), status_code=error.status_code) from error
        else:
            stmt = select(ChatConversation).where(ChatConversation.AccountId == account_id)
        if application_id and workbench_identity is None:
            stmt = stmt.where(ChatConversation.ApplicationId == application_id)
        conversations = (await db.execute(
            stmt.order_by(desc(ChatConversation.LastActiveAt))
        )).scalars()
        return conversations

    @staticmethod
    async def delete(
        db: AsyncSession,
        account_id: str,
        conversation_id: str,
        workbench_identity: WorkbenchIdentityContext | None = None,
        workbench_app_context: WorkbenchAppContext | None = None,
    ):
        # 先按 ownership 定位会话，防止越权删除他人会话
        if (workbench_identity is None) != (workbench_app_context is None):
            raise SanicException("incomplete workbench conversation scope", status_code=500)
        mapping = None
        if workbench_identity is not None:
            stmt = CoreConversation._owned_statement(
                account_id,
                workbench_identity,
                workbench_app_context,
            ).where(ChatConversation.Id == conversation_id).limit(1)
            conversation = (await db.execute(stmt)).scalar()
            if conversation is not None:
                mapping = (
                    await db.execute(
                        select(WorkbenchConversationWorkspace).where(
                            WorkbenchConversationWorkspace.ConversationId == conversation_id,
                            *CoreConversation._workbench_scope_filters(
                                account_id,
                                workbench_identity,
                                workbench_app_context,
                            )[:8],
                        ).limit(1)
                    )
                ).scalar()
        else:
            conversation = (await db.execute(select(ChatConversation).where(
                ChatConversation.AccountId == account_id,
                ChatConversation.Id == conversation_id
            ).limit(1))).scalar()
        if conversation is None:
            raise SanicException("conversation not found", status_code=404)
        # 级联清理该会话下的所有消息记录，避免孤儿数据
        # 注：SharedConversation.Records 是 JSON 快照（分享时已冻结），
        # 与原会话解耦，此处不联动删除，保留已分享链接的可访问性。
        await db.execute(
            delete(ChatRecord).where(ChatRecord.ConversationId == conversation_id)
        )
        if mapping is not None:
            mapping.Status = "deleted"
        await db.delete(conversation)
        await db.commit()

    @staticmethod
    async def get_application_id(
        db: AsyncSession,
        account_id: str,
        conversation_id: str,
        workbench_identity: WorkbenchIdentityContext | None = None,
        workbench_app_context: WorkbenchAppContext | None = None,
    ) -> str:
        if (workbench_identity is None) != (workbench_app_context is None):
            raise SanicException("incomplete workbench conversation scope", status_code=500)
        if workbench_identity is not None:
            stmt = CoreConversation._owned_statement(
                account_id,
                workbench_identity,
                workbench_app_context,
            ).where(ChatConversation.Id == conversation_id).limit(1)
        else:
            stmt = select(ChatConversation).where(
                ChatConversation.AccountId == account_id,
                ChatConversation.Id == conversation_id,
            ).limit(1)
        conversation = (await db.execute(stmt)).scalar()
        if conversation is None:
            raise SanicException("conversation not found", status_code=404)
        return conversation.ApplicationId

    @staticmethod
    async def get_read_context(
        db: AsyncSession,
        account_id: str,
        conversation_id: str,
        workbench_identity: WorkbenchIdentityContext,
        workbench_app_context: WorkbenchAppContext,
    ) -> WorkbenchConversationReadContext:
        try:
            normalized_conversation_id = UUID(str(conversation_id))
            stmt = await CoreConversation._readable_statement(
                db,
                account_id,
                workbench_identity,
                workbench_app_context,
                include_mapping=True,
            )
        except (ValueError, WorkbenchWorkspaceError) as error:
            if isinstance(error, ValueError):
                raise SanicException("conversation not found", status_code=404) from error
            raise SanicException(str(error), status_code=error.status_code) from error
        row = (
            await db.execute(
                stmt.where(ChatConversation.Id == normalized_conversation_id).limit(1)
            )
        ).first()
        if row is None:
            raise SanicException("conversation not found", status_code=404)
        _conversation, mapping = row
        return WorkbenchConversationReadContext(
            application_id=mapping.ApplicationId,
            provider_app_id=mapping.ProviderAppId,
            app_profile_id=str(mapping.AppProfileId),
            config_version=int(mapping.ConfigVersion),
        )

    @staticmethod
    async def exists(
        db: AsyncSession,
        account_id: str,
        conversation_id: str,
        workbench_identity: WorkbenchIdentityContext | None = None,
        workbench_app_context: WorkbenchAppContext | None = None,
    ) -> bool:
        """判断指定账号下是否存在某个 conversation_id 的会话记录（用于跨设备/重置场景下补写本地记录）。"""
        if not account_id or not conversation_id:
            return False
        if (workbench_identity is None) != (workbench_app_context is None):
            return False
        if workbench_identity is not None:
            stmt = CoreConversation._owned_statement(
                account_id,
                workbench_identity,
                workbench_app_context,
            ).where(ChatConversation.Id == conversation_id).limit(1)
        else:
            stmt = select(ChatConversation.Id).where(
                ChatConversation.AccountId == account_id,
                ChatConversation.Id == conversation_id,
            ).limit(1)
        row = (await db.execute(stmt)).first()
        return row is not None

    @staticmethod
    async def create(
        db: AsyncSession,
        account_id: str,
        application_id: str,
        title: str = "new conversation",
        conversation_id: str = None,
        workbench_identity: WorkbenchIdentityContext | None = None,
        workbench_app_context: WorkbenchAppContext | None = None,
        workbench_agent_id: str | None = None,
    ) -> ChatConversation:
        conversation = ChatConversation(
            AccountId=account_id,
            ApplicationId=application_id,
            Title=title,
            Id=conversation_id
        )
        db.add(conversation)
        report = None
        workspace_report = None
        workspace = None
        workspace_mapping = None
        if workbench_identity is not None:
            if workbench_app_context is None or not workbench_agent_id:
                raise SanicException(
                    "conversation ownership context and Agent are required",
                    status_code=500,
                )
            if conversation.Id is None:
                await db.flush()
            try:
                workspace, workspace_mapping = await CoreWorkbenchWorkspace.stage_conversation(
                    db,
                    conversation_id=str(conversation.Id),
                    agent_id=workbench_agent_id,
                    account_id=account_id,
                    identity=workbench_identity,
                    app_context=workbench_app_context,
                )
                report = await WorkbenchResourceReporter.enqueue(
                    db,
                    identity=workbench_identity,
                    resource_type="conversation",
                    resource_id=str(conversation.Id),
                    parent_resource_type="agent",
                    parent_resource_id=workbench_agent_id,
                )
                if workspace.Status != "active":
                    workspace_report = await WorkbenchResourceReporter.enqueue(
                        db,
                        identity=workbench_identity,
                        resource_type="workspace",
                        resource_id=workspace.WorkspaceId,
                        parent_resource_type="conversation",
                        parent_resource_id=str(workspace.ConversationId),
                    )
            except (WorkbenchResourceReportError, WorkbenchWorkspaceError) as error:
                raise SanicException(str(error), status_code=error.status_code) from error
        await db.commit()
        if report is not None:
            try:
                conversation_status = await WorkbenchResourceReporter.deliver_event(
                    db,
                    report.EventId,
                    fail_closed_on_rejection=True,
                )
            except WorkbenchResourceReportError as error:
                raise SanicException(str(error), status_code=error.status_code) from error
            if conversation_status != "delivered":
                raise SanicException(
                    "conversation ownership confirmation is unavailable",
                    status_code=503,
                )
            try:
                await CoreWorkbenchWorkspace.activate_for_conversation(
                    db,
                    workspace=workspace,
                    mapping=workspace_mapping,
                    identity=workbench_identity,
                    report_event_id=(
                        workspace_report.EventId
                        if workspace_report is not None
                        else None
                    ),
                )
            except WorkbenchWorkspaceError as error:
                raise SanicException(str(error), status_code=error.status_code) from error
        return conversation

    @staticmethod
    async def get(db: AsyncSession, conversation_id: str) -> ChatConversation:
        conversation = (await db.execute(select(ChatConversation).where(
            ChatConversation.Id == conversation_id
        ).limit(1))).scalar()
        return conversation

    @staticmethod
    async def get_owned(
        db: AsyncSession,
        account_id: str,
        application_id: str,
        conversation_id: str,
        workbench_identity: WorkbenchIdentityContext | None = None,
        workbench_app_context: WorkbenchAppContext | None = None,
    ) -> ChatConversation:
        if (workbench_identity is None) != (workbench_app_context is None):
            return None
        if workbench_identity is not None:
            stmt = CoreConversation._owned_statement(
                account_id,
                workbench_identity,
                workbench_app_context,
            ).where(
                ChatConversation.Id == conversation_id,
                ChatConversation.ApplicationId == application_id,
            ).limit(1)
        else:
            stmt = select(ChatConversation).where(
                ChatConversation.Id == conversation_id,
                ChatConversation.AccountId == account_id,
                ChatConversation.ApplicationId == application_id,
            ).limit(1)
        return (await db.execute(stmt)).scalar()

    @staticmethod
    async def update(db: AsyncSession, conversation: ChatConversation, title: str = None):
        conversation.LastActiveAt = datetime.now(UTC).replace(tzinfo=None)
        if title is not None:
            conversation.Title = title
        await db.commit()
