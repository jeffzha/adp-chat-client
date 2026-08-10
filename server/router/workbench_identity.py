import secrets

from sanic import json, redirect
from sanic.request.types import Request
from sanic.views import HTTPMethodView

from app_factory import TAgenticApp
from config import tagentic_config
from core.workbench_control import WorkbenchControlError
from core.workbench_identity import CoreWorkbenchIdentity, WorkbenchIdentityError
from core.workbench_csrf import WORKBENCH_CSRF_COOKIE
from util.auth_cookie import add_auth_token_cookie


app = TAgenticApp.get_app()
SSO_BROWSER_BINDING_COOKIE = "claw_sso_binding"
SSO_BROWSER_BINDING_PATH = "/workbench/auth/sso"


def clear_sso_browser_binding(response):
    response.delete_cookie(
        SSO_BROWSER_BINDING_COOKIE,
        path=SSO_BROWSER_BINDING_PATH,
    )
    return response


def sso_error(message: str, status: int):
    response = json({"success": False, "message": message}, status=status)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return clear_sso_browser_binding(response)


class WorkbenchSSOApi(HTTPMethodView):
    async def get(self, request: Request):
        if not tagentic_config.WORKBENCH_MODE:
            return sso_error("workbench SSO is disabled", 404)

        ticket = request.args.get("ticket")
        if not isinstance(ticket, str):
            return sso_error("ticket is required", 400)
        browser_binding = request.cookies.get(SSO_BROWSER_BINDING_COOKIE)
        if not isinstance(browser_binding, str) or not browser_binding:
            return sso_error("SSO browser binding is required", 400)

        try:
            token, context = await CoreWorkbenchIdentity.exchange_ticket(
                request.ctx.db,
                ticket,
                browser_binding,
            )
        except WorkbenchControlError as error:
            await request.ctx.db.rollback()
            return sso_error(str(error), error.status_code)
        except WorkbenchIdentityError as error:
            await request.ctx.db.rollback()
            return sso_error(str(error), error.status_code)

        response = redirect(tagentic_config.WORKBENCH_REDIRECT_PATH, status=302)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Workbench-Access-Mode"] = context.access_mode
        add_auth_token_cookie(
            response,
            config=app.config,
            token=token,
            path=tagentic_config.WORKBENCH_COOKIE_PATH,
            max_age=tagentic_config.WORKBENCH_SESSION_EXPIRE_MINUTES * 60,
        )
        response.add_cookie(
            WORKBENCH_CSRF_COOKIE,
            secrets.token_urlsafe(32),
            path=tagentic_config.WORKBENCH_COOKIE_PATH,
            max_age=tagentic_config.WORKBENCH_SESSION_EXPIRE_MINUTES * 60,
            httponly=False,
            secure=True,
            samesite="Strict",
        )
        return clear_sso_browser_binding(response)


app.add_route(WorkbenchSSOApi.as_view(), "/auth/sso")
