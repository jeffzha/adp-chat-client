import logging

from sanic import json, redirect
from sanic.exceptions import SanicException
from sanic.request.types import Request
from sanic.views import HTTPMethodView

from app_factory import TAgenticApp
from core.workbench_csrf import valid_workbench_csrf
from core.workbench_integrations import (
    WorkbenchIntegrationError,
    WorkbenchIntegrations,
)
from core.workbench_integration_policy import (
    WorkbenchIntegrationPolicy,
    WorkbenchIntegrationPolicyError,
)
from router import login_required


app = TAgenticApp.get_app()
OAUTH_TRANSACTION_COOKIE = "claw_oauth_tx"


def _require_csrf(request: Request) -> None:
    if not valid_workbench_csrf(request.cookies, request.headers):
        raise SanicException("workbench CSRF validation failed", status_code=403)


def _error(error: Exception) -> SanicException:
    if isinstance(error, (WorkbenchIntegrationError, WorkbenchIntegrationPolicyError)):
        status = int(error.status_code)
        return SanicException(str(error), status_code=status)
    logging.warning(
        "[workbench_integrations] operation failed error_type=%s",
        type(error).__name__,
    )
    return SanicException("integration operation failed closed", status_code=503)


class WorkbenchIntegrationCatalogApi(HTTPMethodView):
    @login_required
    async def get(self, request: Request):
        try:
            payload = await WorkbenchIntegrations.catalog(
                request.ctx.db,
                account_id=request.ctx.account_id,
                identity=request.ctx.workbench_context,
                app_context=request.ctx.workbench_app_context,
            )
        except Exception as error:
            raise _error(error) from error
        return json(payload, headers={"Cache-Control": "no-store"})


class WorkbenchIntegrationBindingApi(HTTPMethodView):
    @login_required
    async def post(self, request: Request):
        _require_csrf(request)
        payload = request.json if isinstance(request.json, dict) else {}
        action = str(payload.get("action") or "").strip().lower()
        if action not in {"bind", "unbind"}:
            raise SanicException("integration action is invalid", status_code=400)
        try:
            result = await WorkbenchIntegrations.bind_skill(
                request.ctx.db,
                account_id=request.ctx.account_id,
                identity=request.ctx.workbench_context,
                app_context=request.ctx.workbench_app_context,
                kind=payload.get("kind"),
                resource_id=payload.get("resource_id"),
                parent_id=payload.get("parent_resource_id"),
                bind=action == "bind",
                session_claims=dict(request.ctx.session_claims),
            )
        except Exception as error:
            raise _error(error) from error
        return json(result, headers={"Cache-Control": "no-store"})


class WorkbenchConnectorOAuthStartApi(HTTPMethodView):
    @login_required
    async def post(self, request: Request, connector_id: str):
        _require_csrf(request)
        try:
            result = await WorkbenchIntegrations.start_oauth(
                request.ctx.db,
                account_id=request.ctx.account_id,
                identity=request.ctx.workbench_context,
                app_context=request.ctx.workbench_app_context,
                connector_id=connector_id,
                session_claims=dict(request.ctx.session_claims),
            )
        except Exception as error:
            raise _error(error) from error
        browser_cookie = result.pop("_browser_cookie")
        response = json(result, headers={"Cache-Control": "no-store"})
        response.add_cookie(
            OAUTH_TRANSACTION_COOKIE,
            browser_cookie,
            path=WorkbenchIntegrationPolicy.callback_cookie_path(),
            max_age=app.config.WORKBENCH_OAUTH_STATE_TTL_SECONDS,
            httponly=True,
            secure=True,
            samesite="Lax",
        )
        return response


class WorkbenchConnectorDisconnectApi(HTTPMethodView):
    @login_required
    async def post(self, request: Request, connector_id: str):
        _require_csrf(request)
        try:
            result = await WorkbenchIntegrations.disconnect(
                request.ctx.db,
                account_id=request.ctx.account_id,
                identity=request.ctx.workbench_context,
                app_context=request.ctx.workbench_app_context,
                connector_id=connector_id,
                session_claims=dict(request.ctx.session_claims),
            )
        except Exception as error:
            raise _error(error) from error
        return json(result, headers={"Cache-Control": "no-store"})


class WorkbenchOAuthCallbackApi(HTTPMethodView):
    @login_required
    async def get(self, request: Request, provider_id: str):
        result = await WorkbenchIntegrations.finish_oauth(
            request.ctx.db,
            provider_id=str(provider_id or "").strip().lower(),
            state=str(request.args.get("state") or ""),
            code=str(request.args.get("code") or ""),
            provider_error=str(request.args.get("error") or ""),
            browser_cookie=str(request.cookies.get(OAUTH_TRANSACTION_COOKIE) or ""),
            account_id=request.ctx.account_id,
            identity=request.ctx.workbench_context,
            app_context=request.ctx.workbench_app_context,
            session_claims=dict(request.ctx.session_claims),
        )
        response = redirect(result.location, status=303)
        response.delete_cookie(
            OAUTH_TRANSACTION_COOKIE,
            path=WorkbenchIntegrationPolicy.callback_cookie_path(),
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response


app.add_route(
    WorkbenchIntegrationCatalogApi.as_view(),
    "/integrations",
    name="workbench_integration_catalog",
)
app.add_route(
    WorkbenchIntegrationBindingApi.as_view(),
    "/integrations/bindings",
    name="workbench_integration_binding",
)
app.add_route(
    WorkbenchConnectorOAuthStartApi.as_view(),
    "/integrations/connectors/<connector_id:str>/oauth/start",
    name="workbench_connector_oauth_start",
)
app.add_route(
    WorkbenchConnectorDisconnectApi.as_view(),
    "/integrations/connectors/<connector_id:str>/disconnect",
    name="workbench_connector_disconnect",
)
app.add_route(
    WorkbenchOAuthCallbackApi.as_view(),
    "/integrations/oauth/callback/<provider_id:str>",
    name="workbench_oauth_callback",
)
