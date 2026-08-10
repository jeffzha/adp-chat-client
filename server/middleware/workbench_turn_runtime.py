import logging

from app_factory import TAgenticApp
from config import tagentic_config
from core.workbench_turn import WorkbenchTurnManager


app = TAgenticApp.get_app()


@app.listener("after_server_start")
async def recover_workbench_turns(_app, _loop):
    if not tagentic_config.WORKBENCH_MODE:
        return
    recovered = await WorkbenchTurnManager.recover_previous_lifecycle()
    if recovered:
        logging.warning(
            "[workbench_turn_runtime] marked %s previous-lifecycle Turns provider_unknown",
            recovered,
        )


@app.listener("before_server_stop")
async def stop_workbench_turns(_app, _loop):
    if tagentic_config.WORKBENCH_MODE:
        await WorkbenchTurnManager.shutdown()
