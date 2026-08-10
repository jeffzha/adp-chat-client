import logging

from app_factory import TAgenticApp
from config import tagentic_config
from core.workbench_app_migration import WorkbenchAppMigrationWorker


app = TAgenticApp.get_app()


@app.listener("after_server_start")
async def start_workbench_app_migration_worker(app_instance, _loop):
    if not (
        tagentic_config.WORKBENCH_MODE
        and tagentic_config.WORKBENCH_APP_MIGRATION_WORKER_ENABLED
    ):
        return
    engine = app_instance.config.get("db")
    dialect = str(getattr(getattr(engine, "dialect", None), "name", ""))
    if dialect != "postgresql":
        raise RuntimeError("App migration worker requires PostgreSQL")
    WorkbenchAppMigrationWorker.start(app_instance.config["sessionmaker"])
    logging.info("workbench App migration worker started")


@app.listener("before_server_stop")
async def stop_workbench_app_migration_worker(_app, _loop):
    if tagentic_config.WORKBENCH_APP_MIGRATION_WORKER_ENABLED:
        await WorkbenchAppMigrationWorker.stop()
