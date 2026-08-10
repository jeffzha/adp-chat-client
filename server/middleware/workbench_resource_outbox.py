import asyncio
import logging

from app_factory import TAgenticApp
from config import tagentic_config
from core.workbench_resource_reporter import WorkbenchResourceReporter


app = TAgenticApp.get_app()


async def _run_resource_outbox(app_instance):
    interval = tagentic_config.WORKBENCH_RESOURCE_OUTBOX_INTERVAL_SECONDS
    batch_size = tagentic_config.WORKBENCH_RESOURCE_OUTBOX_BATCH_SIZE
    while True:
        db = app_instance.config["sessionmaker"]()
        try:
            await WorkbenchResourceReporter.drain(db, batch_size)
        except asyncio.CancelledError:
            await db.rollback()
            raise
        except Exception as error:  # pylint: disable=broad-except
            await db.rollback()
            logging.error(
                "[workbench_resource_outbox] retry batch failed: error_type=%s",
                type(error).__name__,
            )
        finally:
            await db.close()
        await asyncio.sleep(interval)


@app.listener("after_server_start")
async def start_resource_outbox(app_instance, _loop):
    if not tagentic_config.WORKBENCH_MODE:
        return
    app_instance.ctx.workbench_resource_outbox_task = asyncio.create_task(
        _run_resource_outbox(app_instance),
        name="workbench-resource-outbox",
    )


@app.listener("before_server_stop")
async def stop_resource_outbox(app_instance, _loop):
    task = getattr(app_instance.ctx, "workbench_resource_outbox_task", None)
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
