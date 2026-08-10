import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from config import tagentic_config


def test_sso_route_is_get_only_and_registered_after_caddy_prefix_strip():
    fake_app = SimpleNamespace(config={}, add_route=Mock())
    sys.modules.pop("router.workbench_identity", None)

    try:
        with patch("app_factory.TAgenticApp.get_app", return_value=fake_app):
            module = importlib.import_module("router.workbench_identity")

        assert hasattr(module.WorkbenchSSOApi, "get")
        assert not hasattr(module.WorkbenchSSOApi, "post")
        fake_app.add_route.assert_called_once()
        assert fake_app.add_route.call_args.args[1] == "/auth/sso"
    finally:
        sys.modules.pop("router.workbench_identity", None)


@pytest.mark.asyncio
async def test_sso_route_requires_forwards_and_clears_browser_binding(monkeypatch):
    fake_app = SimpleNamespace(
        config={"WORKBENCH_MODE": True},
        add_route=Mock(),
    )
    sys.modules.pop("router.workbench_identity", None)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_REDIRECT_PATH", "/workbench/")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_COOKIE_PATH", "/workbench")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SESSION_EXPIRE_MINUTES", 15)

    try:
        with patch("app_factory.TAgenticApp.get_app", return_value=fake_app):
            module = importlib.import_module("router.workbench_identity")

        context = SimpleNamespace(access_mode="active")
        exchange = AsyncMock(return_value=("adp-session", context))
        monkeypatch.setattr(module.CoreWorkbenchIdentity, "exchange_ticket", exchange)
        request = SimpleNamespace(
            args={"ticket": "one-time-ticket"},
            cookies={"claw_sso_binding": "browser-binding"},
            ctx=SimpleNamespace(db=object()),
        )

        response = await module.WorkbenchSSOApi().get(request)

        assert response.status == 302
        exchange.assert_awaited_once_with(
            request.ctx.db,
            "one-time-ticket",
            "browser-binding",
        )
        cookie_headers = [str(cookie) for cookie in response.cookies.cookies]
        assert any("token=adp-session" in value for value in cookie_headers)
        assert any(
            "claw_sso_binding=" in value
            and "Path=/workbench/auth/sso" in value
            and "Max-Age=0" in value
            for value in cookie_headers
        )
    finally:
        sys.modules.pop("router.workbench_identity", None)


@pytest.mark.asyncio
async def test_sso_route_rejects_missing_browser_binding_without_consuming(monkeypatch):
    fake_app = SimpleNamespace(config={}, add_route=Mock())
    sys.modules.pop("router.workbench_identity", None)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)

    try:
        with patch("app_factory.TAgenticApp.get_app", return_value=fake_app):
            module = importlib.import_module("router.workbench_identity")

        exchange = AsyncMock()
        monkeypatch.setattr(module.CoreWorkbenchIdentity, "exchange_ticket", exchange)
        request = SimpleNamespace(
            args={"ticket": "one-time-ticket"},
            cookies={},
            ctx=SimpleNamespace(db=object()),
        )

        response = await module.WorkbenchSSOApi().get(request)

        assert response.status == 400
        exchange.assert_not_awaited()
        assert any(
            "claw_sso_binding=" in str(cookie)
            and "Path=/workbench/auth/sso" in str(cookie)
            and "Max-Age=0" in str(cookie)
            for cookie in response.cookies.cookies
        )
    finally:
        sys.modules.pop("router.workbench_identity", None)
