import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


def _consume_background_result(task: asyncio.Future[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        # Cleanup failures are projected by the caller.  This callback only
        # prevents an eventually-completing abandoned task from producing an
        # unhandled-exception warning.
        pass


async def bounded_cleanup(
    cleanup: Awaitable[Any] | Callable[[], Awaitable[Any]],
    *,
    timeout_seconds: float,
) -> bool:
    """Run best-effort provider cleanup without trusting cancellation semantics.

    Some SDK coroutines can swallow ``CancelledError``.  A timeout followed by
    an unbounded ``gather`` would therefore hang the request and retain its
    runtime lease forever.  After a short second-stage drain, detach a still
    non-cooperative task and let the caller persist an unknown outcome.
    """

    try:
        awaitable = cleanup() if callable(cleanup) else cleanup
        task = asyncio.ensure_future(awaitable)
    except (Exception, asyncio.CancelledError):
        return False
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        return True
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
        task.add_done_callback(_consume_background_result)
        return False
    except Exception:
        if not task.done():
            task.cancel()
        try:
            done, _ = await asyncio.wait(
                {task},
                timeout=min(1.0, max(0.05, float(timeout_seconds))),
            )
        except asyncio.CancelledError:
            task.add_done_callback(_consume_background_result)
            return False
        if task in done:
            await asyncio.gather(task, return_exceptions=True)
        else:
            task.add_done_callback(_consume_background_result)
        return False
