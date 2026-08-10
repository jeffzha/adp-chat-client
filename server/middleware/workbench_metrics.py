import logging

from aiohttp import web

from app_factory import TAgenticApp
from config import tagentic_config
from core.workbench_metrics import WORKBENCH_METRICS


app = TAgenticApp.get_app()


async def _metrics(_request: web.Request) -> web.Response:
    return web.Response(
        text=WORKBENCH_METRICS.render(),
        headers={
            "Cache-Control": "no-store",
            "Content-Type": "text/plain; version=0.0.4; charset=utf-8",
        },
    )


@app.listener("after_server_start")
async def start_workbench_metrics(app_instance, _loop):
    if not (
        tagentic_config.WORKBENCH_MODE
        and tagentic_config.WORKBENCH_METRICS_ENABLED
    ):
        return
    metrics_app = web.Application(client_max_size=1024)
    metrics_app.router.add_get("/internal/metrics", _metrics)
    runner = web.AppRunner(metrics_app, access_log=None)
    await runner.setup()
    site = web.TCPSite(
        runner,
        host=tagentic_config.WORKBENCH_METRICS_HOST,
        port=tagentic_config.WORKBENCH_METRICS_PORT,
        shutdown_timeout=2.0,
    )
    await site.start()
    app_instance.ctx.workbench_metrics_runner = runner
    logging.info(
        "workbench internal metrics listener started host=%s port=%s",
        tagentic_config.WORKBENCH_METRICS_HOST,
        tagentic_config.WORKBENCH_METRICS_PORT,
    )


@app.listener("before_server_stop")
async def stop_workbench_metrics(app_instance, _loop):
    runner = getattr(app_instance.ctx, "workbench_metrics_runner", None)
    if runner is not None:
        await runner.cleanup()
