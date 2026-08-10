from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.workbench_app_resolver import WorkbenchAppResolver
from core.workbench_control import WorkbenchAppContext, WorkbenchControlError


class _Vendor:
    def __init__(self, config, application_id):
        self.config = config
        self.application_id = application_id


def _context(**overrides):
    values = {
        "application_id": "customer-app-7",
        "app_profile_id": "profile-7",
        "config_version": 1,
        "auth_epoch": 3,
        "vendor": "Tencent",
        "service_vendor": "ChinaTencentCloud",
        "app_id": "provider-app-7",
        "app_key": "secret-app-key",
        "space_id": "space-7",
        "template_agent_id": "template-agent-7",
        "secret_id": "secret-id",
        "secret_key": "secret-key",
    }
    values.update(overrides)
    return WorkbenchAppContext(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [
        _context(vendor="OpenAICompatible"),
        _context(service_vendor="International"),
        _context(service_vendor="Private"),
    ],
)
async def test_resolver_rejects_unconfirmed_vendor_context_before_registration(context):
    with patch("app_factory.TAgenticApp.get_app") as get_app:
        with pytest.raises(WorkbenchControlError, match="not allowed"):
            await WorkbenchAppResolver.ensure_vendor(context)

    get_app.assert_not_called()


@pytest.fixture
def resolver_cache(monkeypatch):
    app = SimpleNamespace(vendors={"Tencent": _Vendor}, apps={})
    WorkbenchAppResolver._versions.clear()
    WorkbenchAppResolver._last_used.clear()
    clock = {"now": 0.0}
    monkeypatch.setattr(WorkbenchAppResolver, "_clock", lambda: clock["now"])
    monkeypatch.setattr("app_factory.TAgenticApp.get_app", lambda: app)
    yield app, clock
    WorkbenchAppResolver._versions.clear()
    WorkbenchAppResolver._last_used.clear()


@pytest.mark.asyncio
async def test_resolver_replaces_version_and_revoke_removes_all_references(
    resolver_cache,
    monkeypatch,
):
    app, clock = resolver_cache
    monkeypatch.setattr("config.tagentic_config.WORKBENCH_VENDOR_CACHE_IDLE_SECONDS", 60)
    monkeypatch.setattr("config.tagentic_config.WORKBENCH_VENDOR_CACHE_MAX_ENTRIES", 10)
    initial = _context()
    await WorkbenchAppResolver.ensure_vendor(initial)
    first_vendor = app.apps[initial.application_id]

    clock["now"] = 1.0
    rotated = _context(config_version=2, auth_epoch=4, app_key="rotated-secret")
    await WorkbenchAppResolver.ensure_vendor(rotated)

    assert app.apps[initial.application_id] is not first_vendor
    assert app.apps[initial.application_id].config["AppKey"] == "rotated-secret"
    assert WorkbenchAppResolver._versions[initial.application_id] == (2, 4)
    WorkbenchAppResolver.revoke(initial.application_id)
    assert initial.application_id not in app.apps
    assert initial.application_id not in WorkbenchAppResolver._versions
    assert initial.application_id not in WorkbenchAppResolver._last_used


@pytest.mark.asyncio
async def test_resolver_evicts_idle_entries(resolver_cache, monkeypatch):
    app, clock = resolver_cache
    monkeypatch.setattr("config.tagentic_config.WORKBENCH_VENDOR_CACHE_IDLE_SECONDS", 30)
    monkeypatch.setattr("config.tagentic_config.WORKBENCH_VENDOR_CACHE_MAX_ENTRIES", 10)
    await WorkbenchAppResolver.ensure_vendor(_context())

    clock["now"] = 30.0
    assert await WorkbenchAppResolver.evict_expired() == 1
    assert app.apps == {}


@pytest.mark.asyncio
async def test_resolver_uses_lru_bound_and_touch_preserves_hot_entry(
    resolver_cache,
    monkeypatch,
):
    app, clock = resolver_cache
    monkeypatch.setattr("config.tagentic_config.WORKBENCH_VENDOR_CACHE_IDLE_SECONDS", 300)
    monkeypatch.setattr("config.tagentic_config.WORKBENCH_VENDOR_CACHE_MAX_ENTRIES", 2)
    first = _context(application_id="customer-app-1")
    second = _context(application_id="customer-app-2")
    third = _context(application_id="customer-app-3")
    await WorkbenchAppResolver.ensure_vendor(first)
    clock["now"] = 1.0
    await WorkbenchAppResolver.ensure_vendor(second)
    clock["now"] = 2.0
    await WorkbenchAppResolver.ensure_vendor(first)
    clock["now"] = 3.0
    await WorkbenchAppResolver.ensure_vendor(third)

    assert set(app.apps) == {first.application_id, third.application_id}
    assert second.application_id not in WorkbenchAppResolver._versions
