from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.account import CoreAccount
from core.workbench_control import (
    WorkbenchControlClient,
    WorkbenchControlError,
    WorkbenchIdentityContext,
)
from core.workbench_identity import CoreWorkbenchIdentity, WorkbenchIdentityError
from core.workbench_metrics import WORKBENCH_METRICS
from core.workbench_resource_reporter import WorkbenchResourceReporter
from model.account import AccountRole, AccountStatus


class _ScalarResult:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _ExchangeDB:
    def __init__(self):
        self.execute = AsyncMock(return_value=_ScalarResult(rows=[]))
        self.add = lambda _value: None
        self.commit = AsyncMock()


def _context():
    return WorkbenchIdentityContext(
        binding_id="binding-7",
        canonical_subject="napi:prod:customer:7:user:9",
        customer_id=7,
        new_api_user_id=9,
        auth_epoch=3,
        display_name="User 9",
        application_id="customer-app-7",
        app_profile_id="profile-7",
        access_mode="active",
    )


@pytest.mark.asyncio
async def test_ticket_exchange_fails_when_binding_and_subject_resolve_to_two_rows(monkeypatch):
    context = _context()
    monkeypatch.setattr(
        WorkbenchControlClient,
        "consume_ticket",
        AsyncMock(return_value=context),
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=_ScalarResult(
                rows=[
                    SimpleNamespace(BindingId=context.binding_id),
                    SimpleNamespace(CanonicalSubject=context.canonical_subject),
                ]
            )
        )
    )

    with pytest.raises(WorkbenchIdentityError, match="binding conflict") as exc_info:
        await CoreWorkbenchIdentity.exchange_ticket(
            db,
            "one-time-ticket",
            "browser-binding",
        )

    assert exc_info.value.status_code == 409
    WorkbenchControlClient.consume_ticket.assert_awaited_once_with(
        "one-time-ticket",
        "browser-binding",
    )


@pytest.mark.asyncio
async def test_ticket_exchange_records_control_binding_confirmation_result(monkeypatch):
    context = _context()
    account = SimpleNamespace(
        Id="account-9",
        Role=AccountRole.NORMAL,
        Status=AccountStatus.ACTIVE,
        Name="",
        LastLoginAt=None,
    )
    monkeypatch.setattr(
        WorkbenchControlClient, "consume_ticket", AsyncMock(return_value=context)
    )
    monkeypatch.setattr(CoreAccount, "create_account", AsyncMock(return_value=account))
    monkeypatch.setattr(
        WorkbenchResourceReporter,
        "enqueue",
        AsyncMock(return_value=SimpleNamespace(EventId="event-1")),
    )
    monkeypatch.setattr(WorkbenchResourceReporter, "deliver_event", AsyncMock())
    monkeypatch.setattr(
        CoreWorkbenchIdentity,
        "_create_session_token",
        lambda *_args, **_kwargs: "session-token",
    )

    WORKBENCH_METRICS.reset_for_test()
    confirm = AsyncMock()
    monkeypatch.setattr(WorkbenchControlClient, "confirm_identity", confirm)
    token, _ = await CoreWorkbenchIdentity.exchange_ticket(
        _ExchangeDB(), "one-time-ticket", "browser-binding"
    )
    assert token == "session-token"
    assert 'workbench_identity_bind_total{result="success"} 1' in WORKBENCH_METRICS.render()
    assert 'workbench_identity_bind_total{result="failure"} 0' in WORKBENCH_METRICS.render()

    WORKBENCH_METRICS.reset_for_test()
    confirm.side_effect = WorkbenchControlError("control rejected", 403)
    with pytest.raises(WorkbenchIdentityError, match="control rejected") as exc_info:
        await CoreWorkbenchIdentity.exchange_ticket(
            _ExchangeDB(), "one-time-ticket", "browser-binding"
        )
    assert exc_info.value.status_code == 403
    assert 'workbench_identity_bind_total{result="success"} 0' in WORKBENCH_METRICS.render()
    assert 'workbench_identity_bind_total{result="failure"} 1' in WORKBENCH_METRICS.render()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "role"),
    [
        (AccountStatus.BANNED, AccountRole.NORMAL),
        (AccountStatus.ACTIVE, AccountRole.ADMIN),
    ],
)
async def test_session_authorization_rejects_disabled_or_privileged_shadow_account(
    monkeypatch,
    status,
    role,
):
    context = _context()
    identity = SimpleNamespace(
        BindingId=context.binding_id,
        CanonicalSubject=context.canonical_subject,
        AccountId="account-9",
        CustomerId=context.customer_id,
        NewApiUserId=context.new_api_user_id,
        AuthEpoch=context.auth_epoch,
        Status="active",
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=_ScalarResult(scalar=identity)))
    monkeypatch.setattr(
        CoreAccount,
        "get",
        AsyncMock(return_value=SimpleNamespace(Status=status, Role=role)),
    )
    authorize = AsyncMock(return_value=context)
    monkeypatch.setattr(WorkbenchControlClient, "authorize", authorize)

    with pytest.raises(WorkbenchIdentityError, match="shadow account is not active"):
        await CoreWorkbenchIdentity.authorize_session(
            db,
            {
                "BindingId": context.binding_id,
                "Subject": context.canonical_subject,
                "AccountId": "account-9",
                "AuthEpoch": context.auth_epoch,
            },
            method="GET",
            resource_path="/application/list",
            supplied_application_id=None,
        )

    authorize.assert_not_awaited()
