import asyncio
import logging

from app_factory import TAgenticApp
from config import tagentic_config
from core.workbench_app_resolver import WorkbenchAppResolver


app = TAgenticApp.get_app()


async def _eviction_loop() -> None:
    interval = max(
        5,
        min(30, tagentic_config.WORKBENCH_VENDOR_CACHE_IDLE_SECONDS // 2),
    )
    while True:
        await asyncio.sleep(interval)
        try:
            evicted = await WorkbenchAppResolver.evict_expired()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # pylint: disable=broad-except
            logging.error(
                "[workbench_vendor_cache] eviction failed error_type=%s",
                type(error).__name__,
            )
            continue
        if evicted:
            logging.info(
                "[workbench_vendor_cache] evicted=%s reason=idle_ttl",
                evicted,
            )


@app.listener("after_server_start")
async def start_workbench_vendor_cache_eviction(_app, _loop):
    if not tagentic_config.WORKBENCH_MODE:
        return
    _app.ctx.workbench_vendor_cache_task = asyncio.create_task(_eviction_loop())


@app.listener("before_server_stop")
async def stop_workbench_vendor_cache_eviction(_app, _loop):
    task = getattr(_app.ctx, "workbench_vendor_cache_task", None)
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
