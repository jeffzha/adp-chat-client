import asyncio
import logging
import re
from urllib.parse import urlsplit

import ujson
from sanic import json
from sanic.exceptions import SanicException
from sanic.request.types import Request
from sanic.response import ResponseStream, raw
from sanic.views import HTTPMethodView

from app_factory import TAgenticApp
from config import tagentic_config
from core.workbench_csrf import valid_workbench_csrf
from core.workbench_policy import WorkbenchPolicyError
from core.workbench_identity import CoreWorkbenchIdentity
from core.workbench_sandbox import (
    WorkbenchSandboxError,
    WorkbenchSandboxPtyError,
    WorkbenchSandboxPtyService,
    WorkbenchSandboxService,
)
from core.workbench_sandbox.pty import workbench_pty_reaper
from core.workbench_sandbox.acceptance import WorkbenchSandboxAcceptance
from core.workbench_sandbox.provider import TencentAGSXProvider
from core.workbench_stream import WorkbenchStreamGuard
from router import check_login, login_required
from util.database import db_connection


app = TAgenticApp.get_app()


@app.listener("before_server_start")
async def validate_managed_sandbox_readiness(_app, _loop) -> None:
    if (
        tagentic_config.WORKBENCH_SANDBOX_PTY_ENABLED
        and not tagentic_config.WORKBENCH_SANDBOX_ENABLED
    ):
        raise RuntimeError("WORKBENCH_SANDBOX_PTY_ENABLED requires managed sandbox")
    if tagentic_config.WORKBENCH_SANDBOX_ENABLED:
        TencentAGSXProvider.validate_readiness()
    WorkbenchSandboxAcceptance.validate_readiness()


def _csrf(request: Request) -> None:
    if not valid_workbench_csrf(request.cookies, request.headers):
        raise SanicException("workbench CSRF validation failed", status_code=403)


def _error(error: Exception) -> SanicException:
    if isinstance(error, WorkbenchSandboxPtyError):
        return SanicException(error.code, status_code=error.status_code)
    if isinstance(error, WorkbenchSandboxError):
        return SanicException(error.code, status_code=error.status_code)
    if isinstance(error, WorkbenchPolicyError):
        message = str(error).lower()
        if error.status_code >= 500:
            code = "sandbox_policy_invalid"
            status_code = 503
        elif "read-only" in message:
            code = "sandbox_read_only"
            status_code = 403
        elif "capability" in message and "files" in message:
            code = "sandbox_files_disabled"
            status_code = 403
        elif "capability" in message:
            code = "sandbox_capability_disabled"
            status_code = 403
        else:
            code = "sandbox_forbidden"
            status_code = 403
        return SanicException(code, status_code=status_code)
    logging.warning(
        "[workbench_sandbox_api] operation failed error_type=%s",
        type(error).__name__,
    )
    return SanicException("sandbox_operation_failed", status_code=503)


def _contexts(request: Request) -> dict:
    return {
        "account_id": request.ctx.account_id,
        "identity": request.ctx.workbench_context,
        "app_context": request.ctx.workbench_app_context,
    }


class WorkbenchSandboxListApi(HTTPMethodView):
    @login_required
    async def get(self, request: Request):
        try:
            result = await WorkbenchSandboxService.find_local(
                request.ctx.db,
                conversation_id=request.args.get("conversation_id"),
                **_contexts(request),
            )
        except Exception as error:
            raise _error(error) from error
        return json(result, headers={"Cache-Control": "no-store"})

    @login_required
    async def post(self, request: Request):
        _csrf(request)
        payload = request.json if isinstance(request.json, dict) else {}
        try:
            acceptance = await WorkbenchSandboxAcceptance.fault_context_from_request(
                request.ctx.db,
                session_claims=dict(request.ctx.session_claims),
                headers=request.headers,
                request_host=request.host,
                conversation_id=payload.get("conversation_id"),
                **_contexts(request),
            )
            result = await WorkbenchSandboxService.create_or_reuse(
                request.ctx.db,
                payload=payload,
                provider_start_observer=(
                    acceptance.observe_provider_start if acceptance else None
                ),
                **_contexts(request),
            )
        except Exception as error:
            raise _error(error) from error
        return json(result, status=201, headers={"Cache-Control": "no-store"})


async def _acceptance_context(request: Request, payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    return await WorkbenchSandboxAcceptance.authorize(
        request.ctx.db,
        session_claims=dict(request.ctx.session_claims),
        headers=request.headers,
        request_host=request.host,
        acceptance_run_id=(
            payload.get("acceptance_run_id")
            or request.args.get("acceptance_run_id")
        ),
        conversation_id=(
            payload.get("conversation_id") or request.args.get("conversation_id")
        ),
        **_contexts(request),
    )


class WorkbenchSandboxAcceptanceStartCountApi(HTTPMethodView):
    @login_required
    async def get(self, request: Request):
        try:
            context = await _acceptance_context(request)
            result = await WorkbenchSandboxAcceptance.report(request.ctx.db, context)
        except Exception as error:
            raise _error(error) from error
        return json(
            {
                "acceptance_run_id": result["acceptance_run_id"],
                "conversation_id": result["conversation_id"],
                "provider_start_count": result["provider_start_count"],
            },
            headers={"Cache-Control": "no-store"},
        )


class WorkbenchSandboxAcceptanceInstancesApi(HTTPMethodView):
    @login_required
    async def get(self, request: Request):
        try:
            context = await _acceptance_context(request)
            result = await WorkbenchSandboxAcceptance.report(request.ctx.db, context)
        except Exception as error:
            raise _error(error) from error
        return json(result, headers={"Cache-Control": "no-store"})


class WorkbenchSandboxAcceptanceCleanupApi(HTTPMethodView):
    @login_required
    async def post(self, request: Request):
        _csrf(request)
        payload = request.json if isinstance(request.json, dict) else {}
        try:
            context = await _acceptance_context(request, payload)
            result = await WorkbenchSandboxAcceptance.cleanup(request.ctx.db, context)
        except Exception as error:
            raise _error(error) from error
        return json(result, headers={"Cache-Control": "no-store"})


class WorkbenchSandboxFeatureApi(HTTPMethodView):
    @login_required
    async def get(self, request: Request):
        return json(
            {
                "sandbox_enabled": tagentic_config.WORKBENCH_SANDBOX_ENABLED,
                "shell_enabled": True,
                "files_enabled": True,
                "code_execution_enabled": (
                    WorkbenchSandboxService.code_execution_available(
                        request.ctx.workbench_app_context
                    )
                ),
                "pty_enabled": WorkbenchSandboxPtyService.available(
                    request.ctx.workbench_app_context
                ),
            },
            headers={"Cache-Control": "no-store"},
        )


class WorkbenchSandboxApi(HTTPMethodView):
    @login_required
    async def get(self, request: Request, sandbox_id: str):
        try:
            result = await WorkbenchSandboxService.query(
                request.ctx.db,
                sandbox_id=sandbox_id,
                conversation_id=request.args.get("conversation_id"),
                **_contexts(request),
            )
        except Exception as error:
            raise _error(error) from error
        return json(result, headers={"Cache-Control": "no-store"})


class WorkbenchSandboxLifecycleApi(HTTPMethodView):
    @login_required
    async def post(self, request: Request, sandbox_id: str, action: str):
        _csrf(request)
        payload = request.json if isinstance(request.json, dict) else {}
        try:
            result = await WorkbenchSandboxService.lifecycle(
                request.ctx.db,
                sandbox_id=sandbox_id,
                action=action,
                conversation_id=payload.get("conversation_id"),
                **_contexts(request),
            )
        except Exception as error:
            raise _error(error) from error
        return json(result, headers={"Cache-Control": "no-store"})


class WorkbenchSandboxCodeApi(HTTPMethodView):
    @login_required
    async def post(self, request: Request, sandbox_id: str):
        _csrf(request)
        try:
            await WorkbenchSandboxService.require_recent_reauthentication(
                request.ctx.db,
                account_id=request.ctx.account_id,
                identity=request.ctx.workbench_context,
                session_claims=dict(request.ctx.session_claims),
            )
            result = await WorkbenchSandboxService.execute_code(
                request.ctx.db,
                sandbox_id=sandbox_id,
                payload=request.json,
                **_contexts(request),
            )
        except Exception as error:
            raise _error(error) from error
        return json(result, headers={"Cache-Control": "no-store"})


class WorkbenchSandboxShellApi(HTTPMethodView):
    @login_required
    async def post(self, request: Request, sandbox_id: str):
        _csrf(request)
        try:
            result = await WorkbenchSandboxService.run_command(
                request.ctx.db,
                sandbox_id=sandbox_id,
                payload=request.json,
                **_contexts(request),
            )
        except Exception as error:
            raise _error(error) from error
        return json(result, headers={"Cache-Control": "no-store"})


class WorkbenchSandboxShellStreamApi(HTTPMethodView):
    @login_required
    async def post(self, request: Request, sandbox_id: str):
        _csrf(request)
        try:
            upstream, lease, timeout, _ = await WorkbenchSandboxService.stream_command(
                request.ctx.db,
                sandbox_id=sandbox_id,
                payload=request.json,
                **_contexts(request),
            )
        except Exception as error:
            raise _error(error) from error

        async def streaming_fn(response):
            async def write(chunk) -> None:
                payload = {
                    "type": chunk.stream,
                    "data": chunk.data,
                    "exit_code": chunk.exit_code,
                }
                await response.write(ujson.dumps(payload, ensure_ascii=False) + "\n")

            try:
                await WorkbenchStreamGuard.pump(
                    upstream,
                    write,
                    claims=dict(request.ctx.session_claims),
                    method="POST",
                    resource_path=f"/sandbox/{sandbox_id}/shell/stream",
                    application_id=request.ctx.workbench_app_context.application_id,
                    app_profile_id=request.ctx.workbench_app_context.app_profile_id,
                    config_version=request.ctx.workbench_app_context.config_version,
                    lease=lease,
                    max_runtime_seconds=timeout,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.warning(
                    "[workbench_sandbox_stream] closed error_type=%s",
                    type(error).__name__,
                )
                await response.write(
                    ujson.dumps(
                        {"type": "error", "code": "sandbox_stream_closed"}
                    )
                    + "\n"
                )

        return ResponseStream(
            streaming_fn,
            content_type="application/x-ndjson; charset=utf-8",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )


class WorkbenchSandboxFileApi(HTTPMethodView):
    @login_required
    async def get(self, request: Request, sandbox_id: str):
        try:
            data = await WorkbenchSandboxService.read_file(
                request.ctx.db,
                sandbox_id=sandbox_id,
                conversation_id=request.args.get("conversation_id"),
                path=request.args.get("path"),
                **_contexts(request),
            )
        except Exception as error:
            raise _error(error) from error
        return raw(
            data,
            content_type="application/octet-stream",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": "attachment; filename=workspace-file.bin",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @login_required
    async def put(self, request: Request, sandbox_id: str):
        _csrf(request)
        conversation_id = request.args.get("conversation_id")
        try:
            maximum = await WorkbenchSandboxService.prepare_file_write(
                request.ctx.db,
                sandbox_id=sandbox_id,
                conversation_id=conversation_id,
                **_contexts(request),
            )
            raw_content_length = request.headers.get("content-length")
            if raw_content_length is not None:
                try:
                    content_length = int(raw_content_length)
                except (TypeError, ValueError) as error:
                    raise WorkbenchSandboxError("content_length_invalid") from error
                if content_length < 0:
                    raise WorkbenchSandboxError("content_length_invalid")
                if content_length > maximum:
                    await WorkbenchSandboxService.audit_file_limit(
                        request.ctx.db,
                        sandbox_id=sandbox_id,
                        conversation_id=conversation_id,
                        **_contexts(request),
                    )
                    raise WorkbenchSandboxError("file_limit_exceeded", 413)

            if request.stream is None:
                data = bytes(request.body)
                if len(data) > maximum:
                    await WorkbenchSandboxService.audit_file_limit(
                        request.ctx.db,
                        sandbox_id=sandbox_id,
                        conversation_id=conversation_id,
                        **_contexts(request),
                    )
                    raise WorkbenchSandboxError("file_limit_exceeded", 413)
            else:
                chunks: list[bytes] = []
                received = 0
                while True:
                    chunk = await request.stream.read()
                    if chunk is None:
                        break
                    received += len(chunk)
                    if received > maximum:
                        await WorkbenchSandboxService.audit_file_limit(
                            request.ctx.db,
                            sandbox_id=sandbox_id,
                            conversation_id=conversation_id,
                            **_contexts(request),
                        )
                        raise WorkbenchSandboxError("file_limit_exceeded", 413)
                    chunks.append(chunk)
                data = b"".join(chunks)
            await WorkbenchSandboxService.write_file(
                request.ctx.db,
                sandbox_id=sandbox_id,
                conversation_id=conversation_id,
                path=request.args.get("path"),
                data=data,
                **_contexts(request),
            )
        except Exception as error:
            raise _error(error) from error
        return json({"written": True}, headers={"Cache-Control": "no-store"})


class WorkbenchSandboxPtyApi(HTTPMethodView):
    @login_required
    async def post(self, request: Request, sandbox_id: str):
        _csrf(request)
        try:
            await WorkbenchSandboxService.require_recent_reauthentication(
                request.ctx.db,
                account_id=request.ctx.account_id,
                identity=request.ctx.workbench_context,
                session_claims=dict(request.ctx.session_claims),
            )
            result = await WorkbenchSandboxPtyService.mint_ticket(
                request.ctx.db,
                sandbox_id=sandbox_id,
                payload=request.json,
                claims=dict(request.ctx.session_claims),
                **_contexts(request),
            )
        except Exception as error:
            raise _error(error) from error
        return json(
            result,
            status=201,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )


_PTY_CONNECT_PATH = re.compile(r"^/sandbox/(sbx_[0-9a-f]{32})/pty/connect$")


@app.middleware("request")
async def authorize_workbench_pty_handshake(request: Request):
    match = _PTY_CONNECT_PATH.fullmatch(request.server_path)
    if match is None:
        return None
    if request.method != "GET" or not tagentic_config.WORKBENCH_MODE:
        return json(
            {"success": False, "message": "pty_handshake_rejected"},
            status=404,
            headers={"Cache-Control": "no-store"},
        )
    try:
        public = urlsplit(str(tagentic_config.WORKBENCH_PUBLIC_BASE_URL or ""))
        expected_origin = f"{public.scheme.lower()}://{public.netloc.lower()}"
        if (
            public.scheme.lower() not in {"http", "https"}
            or not public.netloc
            or str(request.headers.get("origin") or "").lower() != expected_origin
            or str(request.host or "").lower() != public.netloc.lower()
        ):
            raise WorkbenchSandboxPtyError("pty_origin_invalid", 403)
        ticket = WorkbenchSandboxPtyService.parse_protocol_header(
            request.headers.get("sec-websocket-protocol")
        )
        claims = check_login(request)
        async with db_connection() as db:
            identity, app_context = await CoreWorkbenchIdentity.authorize_session(
                db,
                claims,
                method="GET",
                resource_path=request.server_path,
                supplied_application_id=str(claims.get("ApplicationId") or ""),
            )
            connection = await WorkbenchSandboxPtyService.consume_ticket(
                db,
                sandbox_id=match.group(1),
                ticket=ticket,
                account_id=request.ctx.account_id,
                identity=identity,
                app_context=app_context,
                claims=dict(claims),
            )
        request.ctx.workbench_pty_connection = connection
        request.ctx.workbench_pty_claims = dict(claims)
    except Exception as error:
        projected = _error(error)
        return json(
            {"success": False, "message": "pty_handshake_rejected"},
            status=projected.status_code,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )
    return None


async def workbench_sandbox_pty_socket(request: Request, websocket, sandbox_id: str):
    connection = getattr(request.ctx, "workbench_pty_connection", None)
    claims = getattr(request.ctx, "workbench_pty_claims", None)
    if connection is None or not isinstance(claims, dict):
        await websocket.close(code=1008, reason="pty_handshake_rejected")
        return
    await WorkbenchSandboxPtyService.serve(
        websocket,
        connection=connection,
        claims=claims,
    )


@app.listener("after_server_start")
async def start_workbench_pty_reaper(_app, _loop) -> None:
    if tagentic_config.WORKBENCH_SANDBOX_PTY_ENABLED:
        _app.ctx.workbench_pty_reaper = asyncio.create_task(workbench_pty_reaper())


@app.listener("before_server_stop")
async def stop_workbench_pty_reaper(_app, _loop) -> None:
    task = getattr(_app.ctx, "workbench_pty_reaper", None)
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


app.add_route(
    WorkbenchSandboxFeatureApi.as_view(),
    "/sandbox/config",
    name="workbench_sandbox_feature",
)
app.add_route(
    WorkbenchSandboxListApi.as_view(),
    "/sandbox",
    name="workbench_sandbox_list",
)
app.add_route(
    WorkbenchSandboxAcceptanceStartCountApi.as_view(),
    "/sandbox/acceptance/provider-start-count",
    name="workbench_sandbox_acceptance_provider_start_count",
)
app.add_route(
    WorkbenchSandboxAcceptanceInstancesApi.as_view(),
    "/sandbox/acceptance/instances",
    name="workbench_sandbox_acceptance_instances",
)
app.add_route(
    WorkbenchSandboxAcceptanceCleanupApi.as_view(),
    "/sandbox/acceptance/cleanup",
    name="workbench_sandbox_acceptance_cleanup",
)
app.add_route(
    WorkbenchSandboxApi.as_view(),
    "/sandbox/<sandbox_id:str>",
    name="workbench_sandbox",
)
app.add_route(
    WorkbenchSandboxLifecycleApi.as_view(),
    "/sandbox/<sandbox_id:str>/<action:str>",
    name="workbench_sandbox_lifecycle",
)
app.add_route(
    WorkbenchSandboxCodeApi.as_view(),
    "/sandbox/<sandbox_id:str>/code",
    name="workbench_sandbox_code",
)
app.add_route(
    WorkbenchSandboxShellApi.as_view(),
    "/sandbox/<sandbox_id:str>/shell",
    name="workbench_sandbox_shell",
)
app.add_route(
    WorkbenchSandboxShellStreamApi.as_view(),
    "/sandbox/<sandbox_id:str>/shell/stream",
    name="workbench_sandbox_shell_stream",
)
app.add_route(
    WorkbenchSandboxFileApi.as_view(),
    "/sandbox/<sandbox_id:str>/files",
    name="workbench_sandbox_file",
    stream=True,
)
app.add_route(
    WorkbenchSandboxPtyApi.as_view(),
    "/sandbox/<sandbox_id:str>/pty",
    name="workbench_sandbox_pty",
)
app.add_websocket_route(
    workbench_sandbox_pty_socket,
    "/sandbox/<sandbox_id:str>/pty/connect",
    name="workbench_sandbox_pty_connect",
    subprotocols=["claw-workbench-pty-v1"],
)
