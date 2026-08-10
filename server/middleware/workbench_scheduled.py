import logging

from app_factory import TAgenticApp
from config import tagentic_config
from core.workbench_scheduled import WorkbenchScheduledService


app = TAgenticApp.get_app()


@app.listener("after_server_start")
async def start_workbench_scheduled_worker(app_instance, _loop):
    if not (
        tagentic_config.WORKBENCH_MODE
        and tagentic_config.WORKBENCH_SCHEDULED_TASKS_ENABLED
    ):
        return
    engine = app_instance.config.get("db")
    dialect = str(getattr(getattr(engine, "dialect", None), "name", ""))
    if dialect != "postgresql":
        raise RuntimeError(
            "scheduled tasks require PostgreSQL in production; SQLite is test-only"
        )
    WorkbenchScheduledService.start_worker()
    logging.info(
        "workbench scheduled-task worker started instance=%s",
        WorkbenchScheduledService.instance_id,
    )


@app.listener("before_server_stop")
async def stop_workbench_scheduled_worker(_app, _loop):
    if tagentic_config.WORKBENCH_SCHEDULED_TASKS_ENABLED:
        await WorkbenchScheduledService.stop_worker()
