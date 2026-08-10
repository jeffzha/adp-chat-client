import asyncio

import redis.asyncio as redis

from app_factory import TAgenticApp
from config import tagentic_config
from core.workbench_control_events import (
    APP_MIGRATION_CUTOVER,
    CACHE_INVALIDATE,
    SESSION_REVOKE,
    WorkbenchControlEventSubscriber,
    invalidate_workbench_app_cache,
    register_control_event_listener,
)
from core.workbench_app_lineage import WorkbenchAppLineageStore
from core.workbench_turn import WorkbenchTurnManager
from core.workbench_integrations import WorkbenchIntegrations


app = TAgenticApp.get_app()


def _redis_client():
    return redis.Redis(
        host=tagentic_config.REDIS_HOST,
        port=tagentic_config.REDIS_PORT,
        username=tagentic_config.REDIS_USERNAME,
        password=tagentic_config.REDIS_PASSWORD,
        db=tagentic_config.REDIS_DB,
        ssl=tagentic_config.REDIS_USE_SSL,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
        health_check_interval=30,
    )


@app.listener("after_server_start")
async def start_workbench_control_events(app_instance, _loop):
    if not tagentic_config.WORKBENCH_MODE:
        return
    subscriber = WorkbenchControlEventSubscriber(
        redis_factory=_redis_client,
        instance_id=tagentic_config.WORKBENCH_INSTANCE_ID,
        channel=tagentic_config.WORKBENCH_CONTROL_EVENT_CHANNEL,
        processed_ttl_seconds=tagentic_config.WORKBENCH_CONTROL_EVENT_PROCESSED_TTL_SECONDS,
        max_backoff_seconds=tagentic_config.WORKBENCH_CONTROL_EVENT_MAX_BACKOFF_SECONDS,
        max_event_bytes=tagentic_config.WORKBENCH_CONTROL_EVENT_MAX_BYTES,
    )
    unregister = (
        register_control_event_listener(
            CACHE_INVALIDATE,
            invalidate_workbench_app_cache,
        ),
        register_control_event_listener(
            SESSION_REVOKE,
            WorkbenchTurnManager.handle_control_event,
        ),
        register_control_event_listener(
            CACHE_INVALIDATE,
            WorkbenchTurnManager.handle_control_event,
        ),
        register_control_event_listener(
            SESSION_REVOKE,
            WorkbenchIntegrations.revoke_for_control_event,
        ),
        register_control_event_listener(
            CACHE_INVALIDATE,
            WorkbenchIntegrations.revoke_for_control_event,
        ),
        register_control_event_listener(
            APP_MIGRATION_CUTOVER,
            WorkbenchAppLineageStore.activate,
        ),
    )
    app_instance.ctx.workbench_control_event_subscriber = subscriber
    app_instance.ctx.workbench_control_event_unregister = unregister
    app_instance.ctx.workbench_control_event_task = asyncio.create_task(
        subscriber.run(),
        name="workbench-control-events",
    )


@app.listener("before_server_stop")
async def stop_workbench_control_events(app_instance, _loop):
    unregister = getattr(app_instance.ctx, "workbench_control_event_unregister", ())
    for callback in unregister:
        callback()
    subscriber = getattr(app_instance.ctx, "workbench_control_event_subscriber", None)
    task = getattr(app_instance.ctx, "workbench_control_event_task", None)
    if subscriber is not None:
        subscriber.stop()
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)
