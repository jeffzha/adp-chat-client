from uuid import uuid4
from datetime import datetime

import pytest
from sanic.exceptions import SanicException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.conversation import CoreConversation
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from model.chat import ChatConversation
from model.workbench import (
    WorkbenchAppLineage,
    WorkbenchConversationWorkspace,
    WorkbenchWorkspace,
)


class _AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)


def _identity(binding_id="binding-1", account_id=None):
    del account_id
    return WorkbenchIdentityContext(
        binding_id=binding_id,
        canonical_subject="napi:subject:customer:7:user:9",
        customer_id=7,
        new_api_user_id=9,
        auth_epoch=11,
        display_name="User",
        application_id="target-app",
        app_profile_id="22",
        access_mode="active",
        config_version=4,
    )


def _app_context():
    return WorkbenchAppContext(
        application_id="target-app",
        app_profile_id="22",
        config_version=4,
        auth_epoch=11,
        vendor="Tencent",
        service_vendor="ChinaTencentADP",
        app_id="target-provider-app",
        app_key="secret",
        space_id="space",
        template_agent_id="template",
        secret_id="secret-id",
        secret_key="secret-key",
    )


@pytest.mark.asyncio
async def test_history_union_is_exact_to_active_lineage_customer_and_binding():
    engine = create_engine("sqlite://")
    for table in (
        ChatConversation.__table__,
        WorkbenchWorkspace.__table__,
        WorkbenchConversationWorkspace.__table__,
        WorkbenchAppLineage.__table__,
    ):
        table.create(engine)
    session = sessionmaker(engine, expire_on_commit=False)()
    account_id = uuid4()
    current_id = uuid4()
    historical_id = uuid4()
    other_binding_id = uuid4()

    session.add(
        WorkbenchAppLineage(
            Id=uuid4(),
            LineageId="lin_1",
            EventKey="app-migration-cutover:11:22:33",
            CustomerId=7,
            MigrationJobId="mig_1",
            SourceApplicationId="source-app",
            SourceProviderAppId="source-provider-app",
            SourceAppProfileId=11,
            SourceConfigVersion=3,
            TargetApplicationId="target-app",
            TargetProviderAppId="target-provider-app",
            TargetAppProfileId=22,
            TargetConfigVersion=4,
            MigrationConfigFingerprint="sha256:target",
            Status="active",
            ActivatedAt=datetime(2026, 8, 10),
        )
    )
    for conversation_id, application_id, provider_app_id, profile_id, version, binding_id in (
        (current_id, "target-app", "target-provider-app", "22", 4, "binding-1"),
        (historical_id, "source-app", "source-provider-app", "11", 3, "binding-1"),
        (other_binding_id, "source-app", "source-provider-app", "11", 3, "binding-other"),
    ):
        workspace_id = f"ws-{conversation_id.hex}"
        session.add(
            ChatConversation(
                Id=conversation_id,
                AccountId=account_id,
                ApplicationId=application_id,
                Title=application_id,
            )
        )
        session.add(
            WorkbenchWorkspace(
                Id=uuid4(),
                WorkspaceId=workspace_id,
                ConversationId=conversation_id,
                BindingId=binding_id,
                AccountId=account_id,
                CustomerId=7,
                ApplicationId=application_id,
                ProviderAppId=provider_app_id,
                AppProfileId=profile_id,
                ConfigVersion=version,
                Status="active",
            )
        )
        session.add(
            WorkbenchConversationWorkspace(
                Id=uuid4(),
                ConversationId=conversation_id,
                WorkspaceId=workspace_id,
                BindingId=binding_id,
                AccountId=account_id,
                CustomerId=7,
                ApplicationId=application_id,
                ProviderAppId=provider_app_id,
                AppProfileId=profile_id,
                ConfigVersion=version,
                AgentId="agent",
                Status="active",
            )
        )
    session.commit()
    db = _AsyncSessionAdapter(session)

    conversations = await CoreConversation.list(
        db,
        account_id,
        application_id="target-app",
        workbench_identity=_identity(),
        workbench_app_context=_app_context(),
    )
    assert {str(item.Id) for item in conversations} == {
        str(current_id),
        str(historical_id),
    }
    historical = await CoreConversation.get_read_context(
        db,
        account_id,
        historical_id,
        _identity(),
        _app_context(),
    )
    assert (
        historical.application_id,
        historical.provider_app_id,
        historical.app_profile_id,
        historical.config_version,
    ) == ("source-app", "source-provider-app", "11", 3)

    assert await CoreConversation.get_owned(
        db,
        account_id,
        "source-app",
        historical_id,
        workbench_identity=_identity(),
        workbench_app_context=_app_context(),
    ) is None
    with pytest.raises(SanicException) as hidden:
        await CoreConversation.get_read_context(
            db,
            account_id,
            str(other_binding_id),
            _identity(),
            _app_context(),
        )
    assert hidden.value.status_code == 404


@pytest.mark.asyncio
async def test_history_union_follows_multi_hop_active_lineage_without_making_it_mutable():
    engine = create_engine("sqlite://")
    for table in (
        ChatConversation.__table__,
        WorkbenchWorkspace.__table__,
        WorkbenchConversationWorkspace.__table__,
        WorkbenchAppLineage.__table__,
    ):
        table.create(engine)
    session = sessionmaker(engine, expire_on_commit=False)()
    account_id = uuid4()
    oldest_id = uuid4()
    session.add_all(
        (
            WorkbenchAppLineage(
                Id=uuid4(),
                LineageId="lin_a_b",
                EventKey="app-migration-cutover:a:b",
                CustomerId=7,
                MigrationJobId="mig_a_b",
                SourceApplicationId="oldest-app",
                SourceProviderAppId="oldest-provider-app",
                SourceAppProfileId=10,
                SourceConfigVersion=2,
                TargetApplicationId="source-app",
                TargetProviderAppId="source-provider-app",
                TargetAppProfileId=11,
                TargetConfigVersion=3,
                MigrationConfigFingerprint="sha256:a-b",
                Status="active",
                ActivatedAt=datetime(2026, 8, 9),
            ),
            WorkbenchAppLineage(
                Id=uuid4(),
                LineageId="lin_b_c",
                EventKey="app-migration-cutover:b:c",
                CustomerId=7,
                MigrationJobId="mig_b_c",
                SourceApplicationId="source-app",
                SourceProviderAppId="source-provider-app",
                SourceAppProfileId=11,
                SourceConfigVersion=3,
                TargetApplicationId="target-app",
                TargetProviderAppId="target-provider-app",
                TargetAppProfileId=22,
                TargetConfigVersion=4,
                MigrationConfigFingerprint="sha256:b-c",
                Status="active",
                ActivatedAt=datetime(2026, 8, 10),
            ),
        )
    )
    workspace_id = f"ws-{oldest_id.hex}"
    session.add(
        ChatConversation(
            Id=oldest_id,
            AccountId=account_id,
            ApplicationId="oldest-app",
            Title="oldest",
        )
    )
    session.add(
        WorkbenchWorkspace(
            Id=uuid4(),
            WorkspaceId=workspace_id,
            ConversationId=oldest_id,
            BindingId="binding-1",
            AccountId=account_id,
            CustomerId=7,
            ApplicationId="oldest-app",
            ProviderAppId="oldest-provider-app",
            AppProfileId="10",
            ConfigVersion=2,
            Status="active",
        )
    )
    session.add(
        WorkbenchConversationWorkspace(
            Id=uuid4(),
            ConversationId=oldest_id,
            WorkspaceId=workspace_id,
            BindingId="binding-1",
            AccountId=account_id,
            CustomerId=7,
            ApplicationId="oldest-app",
            ProviderAppId="oldest-provider-app",
            AppProfileId="10",
            ConfigVersion=2,
            AgentId="historical-agent",
            Status="active",
        )
    )
    session.commit()
    db = _AsyncSessionAdapter(session)

    assert {
        str(item.Id)
        for item in await CoreConversation.list(
            db,
            account_id,
            workbench_identity=_identity(),
            workbench_app_context=_app_context(),
        )
    } == {str(oldest_id)}
    assert (
        await CoreConversation.get_owned(
            db,
            account_id,
            "oldest-app",
            oldest_id,
            workbench_identity=_identity(),
            workbench_app_context=_app_context(),
        )
        is None
    )
