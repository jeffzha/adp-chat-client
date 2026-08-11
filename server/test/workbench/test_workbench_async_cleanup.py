import asyncio

import pytest

from core.workbench_async_cleanup import bounded_cleanup


@pytest.mark.asyncio
async def test_bounded_cleanup_accepts_success_and_projects_failure():
    async def success():
        return None

    async def failure():
        raise RuntimeError("provider detail must not escape")

    assert await bounded_cleanup(success(), timeout_seconds=0.05) is True
    assert await bounded_cleanup(failure(), timeout_seconds=0.05) is False


@pytest.mark.asyncio
async def test_bounded_cleanup_projects_synchronous_factory_failure():
    def failure():
        raise RuntimeError("provider detail must not escape")

    assert await bounded_cleanup(failure, timeout_seconds=0.05) is False


@pytest.mark.asyncio
async def test_bounded_cleanup_returns_when_provider_ignores_cancellation():
    release = asyncio.Event()
    cancellation_seen = asyncio.Event()

    async def stubborn_provider_cleanup():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()

    async with asyncio.timeout(0.5):
        assert await bounded_cleanup(
            stubborn_provider_cleanup(),
            timeout_seconds=0.01,
        ) is False
    assert cancellation_seen.is_set()

    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_bounded_cleanup_projects_caller_cancellation_without_waiting_forever():
    started = asyncio.Event()
    release = asyncio.Event()
    cancellation_seen = asyncio.Event()

    async def stubborn_provider_cleanup():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()

    cleanup = asyncio.create_task(
        bounded_cleanup(stubborn_provider_cleanup(), timeout_seconds=30)
    )
    await started.wait()
    cleanup.cancel()

    async with asyncio.timeout(0.5):
        assert await cleanup is False
    assert cancellation_seen.is_set()

    release.set()
    await asyncio.sleep(0)
