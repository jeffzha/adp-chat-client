import logging

from sanic import json
from sanic.exceptions import SanicException
from sanic.request.types import Request
from sanic.views import HTTPMethodView

from app_factory import TAgenticApp
from core.workbench_scheduled import (
    WorkbenchScheduledService,
)
from core.workbench_csrf import valid_workbench_csrf
from router import login_required


app = TAgenticApp.get_app()


def _require_csrf(request: Request) -> None:
    if not valid_workbench_csrf(request.cookies, request.headers):
        raise SanicException("workbench CSRF validation failed", status_code=403)


def _scheduled_error(error: Exception) -> SanicException:
    if not hasattr(error, "status_code"):
        logging.warning(
            "[workbench_scheduled_api] operation failed error_type=%s",
            type(error).__name__,
        )
        return SanicException("scheduled task operation failed closed", status_code=503)
    status = int(getattr(error, "status_code", 503))
    if status < 400 or status > 599:
        status = 503
    return SanicException(str(error), status_code=status)


class WorkbenchScheduledTaskListApi(HTTPMethodView):
    @login_required
    async def get(self, request: Request):
        try:
            tasks = await WorkbenchScheduledService.list_owned(
                request.ctx.db,
                account_id=request.ctx.account_id,
                identity=request.ctx.workbench_context,
                app_context=request.ctx.workbench_app_context,
            )
        except Exception as error:
            raise _scheduled_error(error) from error
        return json(
            {
                "tasks": [
                    WorkbenchScheduledService.project_task(task, include_prompt=False)
                    for task in tasks
                ]
            },
            headers={"Cache-Control": "no-store"},
        )

    @login_required
    async def post(self, request: Request):
        _require_csrf(request)
        try:
            task = await WorkbenchScheduledService.create(
                request.ctx.db,
                account_id=request.ctx.account_id,
                identity=request.ctx.workbench_context,
                app_context=request.ctx.workbench_app_context,
                payload=request.json,
                session_claims=dict(request.ctx.session_claims),
            )
        except Exception as error:
            raise _scheduled_error(error) from error
        return json(
            WorkbenchScheduledService.project_task(task, include_prompt=True),
            status=201,
            headers={"Cache-Control": "no-store"},
        )


class WorkbenchScheduledTaskApi(HTTPMethodView):
    @login_required
    async def get(self, request: Request, task_id: str):
        try:
            task = await WorkbenchScheduledService.get_owned(
                request.ctx.db,
                task_id=task_id,
                account_id=request.ctx.account_id,
                identity=request.ctx.workbench_context,
                app_context=request.ctx.workbench_app_context,
            )
        except Exception as error:
            raise _scheduled_error(error) from error
        if task is None:
            raise SanicException("scheduled task not found", status_code=404)
        return json(
            WorkbenchScheduledService.project_task(task, include_prompt=True),
            headers={"Cache-Control": "no-store"},
        )

    @login_required
    async def patch(self, request: Request, task_id: str):
        _require_csrf(request)
        try:
            task = await WorkbenchScheduledService.update(
                request.ctx.db,
                task_id=task_id,
                account_id=request.ctx.account_id,
                identity=request.ctx.workbench_context,
                app_context=request.ctx.workbench_app_context,
                payload=request.json,
                session_claims=dict(request.ctx.session_claims),
            )
        except Exception as error:
            raise _scheduled_error(error) from error
        return json(
            WorkbenchScheduledService.project_task(task, include_prompt=True),
            headers={"Cache-Control": "no-store"},
        )

    @login_required
    async def delete(self, request: Request, task_id: str):
        _require_csrf(request)
        try:
            await WorkbenchScheduledService.delete(
                request.ctx.db,
                task_id=task_id,
                account_id=request.ctx.account_id,
                identity=request.ctx.workbench_context,
                app_context=request.ctx.workbench_app_context,
                session_claims=dict(request.ctx.session_claims),
            )
        except Exception as error:
            raise _scheduled_error(error) from error
        return json({"deleted": True}, headers={"Cache-Control": "no-store"})


class WorkbenchScheduledTaskStateApi(HTTPMethodView):
    @login_required
    async def post(self, request: Request, task_id: str, action: str):
        _require_csrf(request)
        if action not in {"pause", "resume", "run"}:
            raise SanicException("scheduled task action not found", status_code=404)
        try:
            if action == "run":
                run = await WorkbenchScheduledService.run_now(
                    request.ctx.db,
                    task_id=task_id,
                    account_id=request.ctx.account_id,
                    identity=request.ctx.workbench_context,
                    app_context=request.ctx.workbench_app_context,
                    session_claims=dict(request.ctx.session_claims),
                )
                return json(
                    WorkbenchScheduledService.project_run(run),
                    status=202,
                    headers={"Cache-Control": "no-store"},
                )
            operation = getattr(WorkbenchScheduledService, action)
            task = await operation(
                request.ctx.db,
                task_id=task_id,
                account_id=request.ctx.account_id,
                identity=request.ctx.workbench_context,
                app_context=request.ctx.workbench_app_context,
                session_claims=dict(request.ctx.session_claims),
            )
        except Exception as error:
            raise _scheduled_error(error) from error
        return json(
            WorkbenchScheduledService.project_task(task, include_prompt=True),
            headers={"Cache-Control": "no-store"},
        )


class WorkbenchScheduledRunListApi(HTTPMethodView):
    @login_required
    async def get(self, request: Request, task_id: str):
        raw_limit = request.args.get("limit", "20")
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as error:
            raise SanicException("limit is invalid", status_code=400) from error
        try:
            runs = await WorkbenchScheduledService.list_runs(
                request.ctx.db,
                task_id=task_id,
                account_id=request.ctx.account_id,
                identity=request.ctx.workbench_context,
                app_context=request.ctx.workbench_app_context,
                limit=limit,
            )
        except Exception as error:
            raise _scheduled_error(error) from error
        return json(
            {"runs": [WorkbenchScheduledService.project_run(run) for run in runs]},
            headers={"Cache-Control": "no-store"},
        )


app.add_route(
    WorkbenchScheduledTaskListApi.as_view(),
    "/scheduled-tasks",
    name="workbench_scheduled_task_list",
)
app.add_route(
    WorkbenchScheduledTaskApi.as_view(),
    "/scheduled-tasks/<task_id:str>",
    name="workbench_scheduled_task",
)
app.add_route(
    WorkbenchScheduledTaskStateApi.as_view(),
    "/scheduled-tasks/<task_id:str>/<action:str>",
    name="workbench_scheduled_task_state",
)
app.add_route(
    WorkbenchScheduledRunListApi.as_view(),
    "/scheduled-tasks/<task_id:str>/runs",
    name="workbench_scheduled_run_list",
)
