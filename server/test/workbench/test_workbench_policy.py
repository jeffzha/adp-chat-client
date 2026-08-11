from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.workbench_runtime as runtime_module
from core.workbench_control import (
    WORKBENCH_LIMIT_MAXIMUMS,
    WorkbenchAppContext,
    WorkbenchIdentityContext,
)
from core.workbench_policy import WorkbenchPolicy, WorkbenchPolicyError
from core.workbench_runtime import WorkbenchRuntimeError, WorkbenchRuntimeGuard


def _identity(access_mode="active"):
    return WorkbenchIdentityContext(
        binding_id="binding-9",
        canonical_subject="napi:prod:customer:7:user:9",
        customer_id=7,
        new_api_user_id=9,
        auth_epoch=3,
        display_name="User",
        application_id="customer-app-7",
        app_profile_id="profile-7",
        access_mode=access_mode,
    )


def _app_context(capabilities=(), **limit_overrides):
    limits = {
        "customer_concurrency": 10,
        "user_concurrency": 1,
        "max_runtime_seconds": 900,
        "max_reasoning_rounds": 20,
        "max_output_tokens": 8192,
        "web_search_per_turn": 3,
        "max_file_bytes": 52428800,
    }
    limits.update(limit_overrides)
    return WorkbenchAppContext(
        application_id="customer-app-7",
        app_profile_id="profile-7",
        config_version=4,
        auth_epoch=3,
        vendor="Tencent",
        service_vendor="ChinaTencentCloud",
        app_id="provider-app-7",
        app_key="secret-app-key",
        space_id="space-7",
        template_agent_id="template-7",
        secret_id="secret-id",
        secret_key="secret-key",
        capabilities=tuple(capabilities),
        limits=limits,
    )


def test_chat_capabilities_are_default_deny():
    with pytest.raises(WorkbenchPolicyError, match="chat"):
        WorkbenchPolicy.validate_new_turn(
            _identity(),
            _app_context(),
            search_network=False,
        )


def test_read_only_session_cannot_start_a_turn():
    with pytest.raises(WorkbenchPolicyError, match="read-only"):
        WorkbenchPolicy.validate_new_turn(
            _identity("read_only"),
            _app_context(capabilities=("chat",)),
            search_network=False,
        )


def test_plain_chat_returns_the_trusted_limits_for_server_side_enforcement():
    limits = WorkbenchPolicy.validate_new_turn(
        _identity(),
        _app_context(capabilities=("chat", "files")),
        search_network=False,
    )

    assert limits["max_output_tokens"] == 8192
    assert limits["max_reasoning_rounds"] == 20


def test_unaccepted_runtime_profile_cannot_start_a_turn():
    context = _app_context(capabilities=("chat",))
    context = WorkbenchAppContext(
        **{
            **context.__dict__,
            "provider_app_mode": 1,
            "runtime_profile": "standard_v2",
            "execution_enabled": False,
            "template_agent_id": "",
        }
    )

    with pytest.raises(WorkbenchPolicyError, match="execution is not enabled"):
        WorkbenchPolicy.validate_new_turn(
            _identity(),
            context,
            search_network=False,
        )


@pytest.mark.parametrize(
    "capability",
    ["web_search", "tools", "connectors"],
)
def test_unbounded_chat_capabilities_remain_fail_closed(capability):
    with pytest.raises(WorkbenchPolicyError, match="unbounded capabilities"):
        WorkbenchPolicy.validate_new_turn(
            _identity(),
            _app_context(capabilities=("chat", capability)),
            search_network=False,
        )


def test_bounded_scheduled_task_capability_does_not_disable_interactive_chat():
    limits = WorkbenchPolicy.validate_new_turn(
        _identity(),
        _app_context(capabilities=("chat", "scheduled_tasks")),
        search_network=False,
    )

    assert limits["max_runtime_seconds"] == 900


def test_bounded_sandbox_capability_does_not_disable_interactive_or_scheduled_chat():
    context = _app_context(capabilities=("chat", "scheduled_tasks", "sandbox"))
    assert WorkbenchPolicy.validate_new_turn(
        _identity(), context, search_network=False
    )["max_runtime_seconds"] == 900
    assert WorkbenchPolicy.validate_scheduled_turn(
        _identity(), context, task_max_runtime_seconds=300
    )["max_runtime_seconds"] == 300


def test_web_search_is_closed_when_provider_call_count_cannot_be_enforced():
    capabilities = (
        "chat",
        "tools",
        "connectors",
        "web_search",
    )
    with pytest.raises(WorkbenchPolicyError, match="search-call count"):
        WorkbenchPolicy.validate_new_turn(
            _identity(),
            _app_context(capabilities=capabilities),
            search_network=True,
        )


def test_file_limit_is_bounded_even_when_control_plane_is_misconfigured():
    with pytest.raises(WorkbenchPolicyError, match="limits are invalid"):
        WorkbenchPolicy.validate_file_write(
            _identity(),
            _app_context(capabilities=("files",), max_file_bytes=1073741825),
        )


@pytest.mark.parametrize("limit_name", sorted(WORKBENCH_LIMIT_MAXIMUMS))
def test_every_trusted_limit_is_independently_bounded(limit_name):
    with pytest.raises(WorkbenchPolicyError, match="limits are invalid"):
        WorkbenchPolicy.validated_limits(
            _app_context(
                capabilities=("chat",),
                **{limit_name: WORKBENCH_LIMIT_MAXIMUMS[limit_name] + 1},
            )
        )


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _RuntimeDb:
    def __init__(self, counts):
        self.counts = iter(counts)
        self.statements = []
        self.added = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        if statement.__class__.__name__ == "Select":
            return _ScalarResult(next(self.counts))
        return SimpleNamespace()

    def add(self, value):
        self.added.append(value)


@pytest.mark.asyncio
async def test_runtime_lease_serializes_in_postgres_and_persists_recoverable_expiry(monkeypatch):
    db = _RuntimeDb([2, 0])

    @asynccontextmanager
    async def fake_connection():
        yield db

    monkeypatch.setattr(runtime_module, "db_connection", fake_connection)
    lease = await WorkbenchRuntimeGuard.acquire(
        account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        identity=_identity(),
        app_context=_app_context(capabilities=("chat",)),
        operation="chat",
    )

    advisory = [str(statement) for statement, _ in db.statements[:2]]
    assert all("pg_advisory_xact_lock" in statement for statement in advisory)
    assert len(db.added) == 1
    assert db.added[0].LeaseId == lease.lease_id
    assert db.added[0].MaxRuntimeSeconds == 900
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_lease_rejects_customer_limit_before_insert(monkeypatch):
    db = _RuntimeDb([10])

    @asynccontextmanager
    async def fake_connection():
        yield db

    monkeypatch.setattr(runtime_module, "db_connection", fake_connection)
    with pytest.raises(WorkbenchRuntimeError, match="customer concurrency"):
        await WorkbenchRuntimeGuard.acquire(
            account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            identity=_identity(),
            app_context=_app_context(capabilities=("chat",)),
            operation="chat",
        )

    assert db.added == []
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_lease_caps_effective_user_concurrency_at_one(monkeypatch):
    db = _RuntimeDb([0, 1])

    @asynccontextmanager
    async def fake_connection():
        yield db

    monkeypatch.setattr(runtime_module, "db_connection", fake_connection)
    with pytest.raises(WorkbenchRuntimeError, match="user concurrency"):
        await WorkbenchRuntimeGuard.acquire(
            account_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            identity=_identity(),
            app_context=_app_context(
                capabilities=("chat",),
                user_concurrency=5,
            ),
            operation="chat",
        )

    assert db.added == []
    db.rollback.assert_awaited_once()
