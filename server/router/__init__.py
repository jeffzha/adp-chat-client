import logging
import uuid
from functools import wraps

from sanic.request.types import Request
from sanic.exceptions import SanicException

from config import tagentic_config
from util.helper import get_remote_ip, get_path_base
from util.auth_cookie import add_auth_token_cookie
from core.error.account import AccountUnauthorized
from core.session import SessionToken
from core.account import CoreAccount
from core.workbench_identity import CoreWorkbenchIdentity, WorkbenchIdentityError


def setup_account_info(request, token):
    token = SessionToken.check(token)
    request.ctx.account_id = token['AccountId']
    request.ctx.session_claims = token
    return token


def check_login(request):
    auth = request.headers.get("Authorization")
    if auth is None:
        auth = request.cookies.get('token', None)
    if auth is None:
        raise AccountUnauthorized()

    auth_token = auth.split(' ')[-1]
    claims = setup_account_info(request, auth_token)
    if tagentic_config.WORKBENCH_MODE and claims.get("token_source") != "workbench_sso":
        raise AccountUnauthorized("A trusted workbench session is required.")
    return claims


async def authorize_workbench_request(request, claims):
    if not tagentic_config.WORKBENCH_MODE:
        return None
    payload = request.json if isinstance(request.json, dict) else {}
    supplied_application_id = payload.get("ApplicationId") or request.args.get("ApplicationId")
    try:
        context, app_context = await CoreWorkbenchIdentity.authorize_session(
            request.ctx.db,
            claims,
            method=request.method,
            resource_path=request.server_path,
            supplied_application_id=supplied_application_id,
        )
    except WorkbenchIdentityError as error:
        raise SanicException(str(error), status_code=error.status_code) from error
    request.ctx.workbench_context = context
    request.ctx.workbench_app_context = app_context
    return context


def login_required(view):
    @wraps(view)
    async def decorated(*args, **kwargs):
        _, request = args

        claims = check_login(request)
        if hasattr(request.ctx, "db"):
            await authorize_workbench_request(request, claims)
            return await view(*args, **kwargs)

        # Streaming routes deliberately do not hold a middleware-managed DB
        # session for the lifetime of an SSE response.  Authorize them with a
        # short-lived session before the handler builds the stream.
        from util.database import db_connection

        async with db_connection() as db:
            request.ctx.db = db
            await authorize_workbench_request(request, claims)
            return await view(*args, **kwargs)

    return decorated


async def auto_login(request: Request):
    need_register = False
    try:
        check_login(request)
        # token 可解析，但需验证 account 是否仍存在于数据库中
        existing = await CoreAccount.get(request.ctx.db, request.ctx.account_id)
        if existing is None:
            need_register = True
    except:  # pylint: disable=bare-except
        need_register = True

    if need_register:
        # AUTO_CREATE_ACCOUNT 场景：给每个自动创建的账号一个可区分的 Name
        auto_suffix = uuid.uuid4().hex[:8]
        account = await CoreAccount.register(
            request.ctx.db,
            name=f'User-{auto_suffix}',
        )
        token = await CoreAccount.login(request.ctx.db, account, get_remote_ip(request))

        def on_response(resp):
            logging.info(
                '[auto_login] new account registed {}'.format(account.Id)
            )
            add_auth_token_cookie(
                resp,
                config=request.app.config,
                token=token,
                path=get_path_base(),
                max_age=315360000,
            )
            return resp
        setup_account_info(request, token)
        return on_response
    return None
