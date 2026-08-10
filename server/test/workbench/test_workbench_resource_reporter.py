from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.workbench_control import (
    WorkbenchControlClient,
    WorkbenchControlError,
    WorkbenchIdentityContext,
)
from core.workbench_resource_reporter import (
    WorkbenchResourceReporter,
    WorkbenchResourceReportError,
)


class _Result:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar(self):
        return self._scalar


def _identity(*, binding_id="binding-7", app_profile_id="17"):
    return WorkbenchIdentityContext(
        binding_id=binding_id,
        canonical_subject="napi:prod:customer:7:user:9",
        customer_id=7,
        new_api_user_id=9,
        auth_epoch=3,
        display_name="User",
        application_id="provider-app-7",
        app_profile_id=app_profile_id,
        access_mode="active",
        config_version=4,
    )


@pytest.mark.asyncio
async def test_enqueue_persists_stable_event_and_exact_parent_scope():
    added = []
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_Result()),
        add=added.append,
        flush=AsyncMock(),
    )

    event = await WorkbenchResourceReporter.enqueue(
        db,
        identity=_identity(),
        resource_type="conversation",
        resource_id="conversation-7",
        parent_resource_type="agent",
        parent_resource_id="agent-7",
    )

    assert event.EventId.startswith("wre_")
    assert event.ResourceKeyHash
    assert event.BindingId == "binding-7"
    assert event.AppProfileId == 17
    assert event.ConfigVersion == 4
    assert event.ParentResourceType == "agent"
    assert event.ParentResourceId == "agent-7"
    assert added == [event]
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_enqueue_rejects_cross_identity_reuse_and_broken_parent_chain():
    existing = SimpleNamespace(
        BindingId="binding-other",
        CanonicalSubject="napi:prod:customer:8:user:10",
        CustomerId=8,
        ApplicationId="provider-app-8",
        AppProfileId=18,
        ConfigVersion=1,
        ResourceType="agent",
        ResourceId="agent-7",
        ParentResourceType="account",
        ParentResourceId="account-8",
        SourceVersion=1,
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(existing)))

    with pytest.raises(WorkbenchResourceReportError, match="different ownership scope"):
        await WorkbenchResourceReporter.enqueue(
            db,
            identity=_identity(),
            resource_type="agent",
            resource_id="agent-7",
            parent_resource_type="account",
            parent_resource_id="account-7",
        )

    with pytest.raises(WorkbenchResourceReportError, match="parent chain"):
        await WorkbenchResourceReporter.enqueue(
            db,
            identity=_identity(),
            resource_type="conversation",
            resource_id="conversation-7",
            parent_resource_type="account",
            parent_resource_id="account-7",
        )


def _pending_event():
    return SimpleNamespace(
        EventId="wre_stable",
        BindingId="binding-7",
        CanonicalSubject="napi:prod:customer:7:user:9",
        CustomerId=7,
        ApplicationId="provider-app-7",
        AppProfileId=17,
        ConfigVersion=4,
        ResourceType="agent",
        ResourceId="agent-7",
        ParentResourceType="account",
        ParentResourceId="account-7",
        SourceVersion=1,
        Status="pending",
        AttemptCount=0,
        LeaseUntil=None,
        LastError=None,
        NextAttemptAt=None,
        ControlBindingId=None,
        DeliveredAt=None,
    )


@pytest.mark.asyncio
async def test_delivery_retries_transient_failure_with_same_event(monkeypatch):
    event = _pending_event()
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_Result(event)),
        commit=AsyncMock(),
    )
    bind = AsyncMock(side_effect=WorkbenchControlError("unavailable", 503))
    monkeypatch.setattr(WorkbenchControlClient, "bind_resource", bind)

    status = await WorkbenchResourceReporter.deliver_event(db, event.EventId)

    assert status == "retry"
    assert event.Status == "retry"
    assert event.AttemptCount == 1
    assert event.LastError == "control_status_503"
    assert bind.await_args.kwargs["source_event_id"] == "wre_stable"
    assert bind.await_args.kwargs["source_version"] == 1


@pytest.mark.asyncio
async def test_permanent_scope_rejection_fails_closed(monkeypatch):
    event = _pending_event()
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_Result(event)),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        WorkbenchControlClient,
        "bind_resource",
        AsyncMock(side_effect=WorkbenchControlError("forbidden", 403)),
    )

    with pytest.raises(WorkbenchResourceReportError, match="rejected"):
        await WorkbenchResourceReporter.deliver_event(
            db,
            event.EventId,
            fail_closed_on_rejection=True,
        )

    assert event.Status == "rejected"
    assert event.LastError == "control_status_403"
