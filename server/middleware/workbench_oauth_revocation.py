import asyncio
import logging

from app_factory import TAgenticApp
from config import tagentic_config
from core.workbench_integrations import WorkbenchIntegrations


app = TAgenticApp.get_app()


async def _revocation_loop(app_instance):
    while True:
        db = app_instance.config["sessionmaker"]()
        try:
            await WorkbenchIntegrations.drain_revocations(
                db, tagentic_config.WORKBENCH_OAUTH_REVOCATION_BATCH_SIZE
            )
        except asyncio.CancelledError:
            await db.rollback()
            raise
        except Exception as error:  # pylint: disable=broad-except
            await db.rollback()
            logging.error(
                "[workbench_oauth_revocation] retry batch failed: error_type=%s",
                type(error).__name__,
            )
        finally:
            await db.close()
        await asyncio.sleep(
            tagentic_config.WORKBENCH_OAUTH_REVOCATION_INTERVAL_SECONDS
        )


@app.listener("after_server_start")
async def start_oauth_revocation_worker(app_instance, _loop):
    if not tagentic_config.WORKBENCH_MODE:
        return
    app_instance.ctx.workbench_oauth_revocation_task = asyncio.create_task(
        _revocation_loop(app_instance), name="workbench-oauth-revocation"
    )


@app.listener("before_server_stop")
async def stop_oauth_revocation_worker(app_instance, _loop):
    task = getattr(app_instance.ctx, "workbench_oauth_revocation_task", None)
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
