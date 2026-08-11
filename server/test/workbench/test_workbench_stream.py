import asyncio
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

import core.workbench_stream as stream_module
from core.workbench_stream import (
    WorkbenchStreamGuard,
    WorkbenchStreamReauthorizationError,
)


class _SilentStream:
    def __init__(self):
        self.started = False
        self.cancelled = False
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.started = True
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_invalid_stream_setup_still_closes_upstream_and_releases_lease(
    monkeypatch,
):
    upstream = _SilentStream()
    lease = object()
    release = AsyncMock()
    monkeypatch.setattr(stream_module.WorkbenchRuntimeGuard, "release", release)

    with pytest.raises(
        WorkbenchStreamReauthorizationError,
        match="interval is invalid",
    ):
        await WorkbenchStreamGuard.pump(
            upstream,
            AsyncMock(),
            claims={},
            method="POST",
            resource_path="/chat/message",
            application_id="customer-app-7",
            app_profile_id="profile-7",
            config_version=4,
            lease=lease,
            max_runtime_seconds=60,
            reauthorization_interval_seconds=0,
        )

    assert upstream.closed is True
    release.assert_awaited_once_with(lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_path", ["/chat/message", "/file/parse"])
async def test_silent_workbench_stream_revocation_closes_upstream_and_releases_lease(
    monkeypatch,
    resource_path,
):
    upstream = _SilentStream()
    release = AsyncMock()
    reauthorize = AsyncMock(
        side_effect=WorkbenchStreamReauthorizationError("revoked")
    )
    monotonic_values = iter((0.0, 0.0, 30.0))

    async def provider_remains_silent(tasks, **_kwargs):
        await asyncio.sleep(0)
        return set(), set(tasks)

    monkeypatch.setattr(stream_module.asyncio, "wait", provider_remains_silent)
    monkeypatch.setattr(WorkbenchStreamGuard, "reauthorize", reauthorize)
    monkeypatch.setattr(stream_module.WorkbenchRuntimeGuard, "release", release)

    with pytest.raises(WorkbenchStreamReauthorizationError, match="revoked"):
        await WorkbenchStreamGuard.pump(
            upstream,
            AsyncMock(),
            claims={"exp": int(time.time()) + 300},
            method="POST",
            resource_path=resource_path,
            application_id="customer-app-7",
            app_profile_id="profile-7",
            config_version=4,
            lease=object(),
            max_runtime_seconds=900,
            reauthorization_interval_seconds=30,
            monotonic=lambda: next(monotonic_values),
        )

    reauthorize.assert_awaited_once()
    assert upstream.started is True
    assert upstream.cancelled is True
    assert upstream.closed is True
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_periodic_reauthorization_uses_a_short_lived_database_session(monkeypatch):
    events = []
    current_app_context = type(
        "AppContext",
        (),
        {"app_profile_id": "profile-7", "config_version": 4},
    )()
    authorize = AsyncMock(return_value=(object(), current_app_context))

    @asynccontextmanager
    async def short_lived_connection():
        events.append("opened")
        try:
            yield object()
        finally:
            events.append("closed")

    monkeypatch.setattr(stream_module, "db_connection", short_lived_connection)
    monkeypatch.setattr(
        stream_module.CoreWorkbenchIdentity,
        "authorize_session",
        authorize,
    )

    await WorkbenchStreamGuard.reauthorize(
        claims={"exp": int(time.time()) + 300},
        method="POST",
        resource_path="/chat/message",
        application_id="customer-app-7",
        app_profile_id="profile-7",
        config_version=4,
    )

    assert events == ["opened", "closed"]
    authorize.assert_awaited_once()
    assert authorize.await_args.kwargs == {
        "method": "POST",
        "resource_path": "/chat/message",
        "supplied_application_id": "customer-app-7",
    }


@pytest.mark.asyncio
async def test_revoked_accepted_turn_drains_provider_to_terminal_evidence(monkeypatch):
    async def finite_stream():
        yield "first"
        yield "terminal"

    release = AsyncMock()
    reauthorize = AsyncMock(
        side_effect=WorkbenchStreamReauthorizationError("revoked")
    )
    write = AsyncMock()
    monotonic_values = iter((0.0, 0.0, 30.0, 31.0, 32.0, 33.0))
    monkeypatch.setattr(WorkbenchStreamGuard, "reauthorize", reauthorize)
    monkeypatch.setattr(stream_module.WorkbenchRuntimeGuard, "release", release)

    await WorkbenchStreamGuard.pump(
        finite_stream(),
        write,
        claims={"exp": int(time.time()) + 300},
        method="POST",
        resource_path="/chat/message",
        application_id="customer-app-7",
        app_profile_id="profile-7",
        config_version=4,
        lease=object(),
        max_runtime_seconds=900,
        reauthorization_interval_seconds=30,
        drain_after_reauthorization_failure=True,
        monotonic=lambda: next(monotonic_values, 33.0),
    )

    reauthorize.assert_awaited_once()
    assert [call.args[0] for call in write.await_args_list] == ["first", "terminal"]
    release.assert_awaited_once()
