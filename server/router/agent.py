from sanic import json
from sanic.views import HTTPMethodView
from sanic_restful_api import reqparse
from sanic.request.types import Request
from sanic.exceptions import SanicException

from config import tagentic_config
from router import login_required
from core.agent import AgentProvisioningError, CoreAgent
from app_factory import TAgenticApp

app: TAgenticApp = TAgenticApp.get_app()


class AgentConfigApi(HTTPMethodView):
    @login_required
    async def get(self, request: Request):
        """获取当前用户在指定 application 下的 AgentId"""
        parser = reqparse.RequestParser()
        parser.add_argument("ApplicationId", type=str, required=True, location="args")
        args = parser.parse_args(request)

        if tagentic_config.WORKBENCH_MODE:
            context = request.ctx.workbench_context
            app_context = request.ctx.workbench_app_context
            application_id = app_context.application_id
            if not app_context.runtime.uses_provider_user_agent:
                record = None
            elif str(context.access_mode).strip().lower() == "active":
                vendor_app = app.get_vendor_app(application_id)
                try:
                    record = await CoreAgent.ensure(
                        request.ctx.db,
                        request.ctx.account_id,
                        application_id,
                        vendor_app,
                        identity_context=context,
                    )
                except AgentProvisioningError as error:
                    raise SanicException(str(error), status_code=error.status_code) from error
                except ValueError as error:
                    raise SanicException(str(error), status_code=502) from error
            else:
                record = await CoreAgent.get(
                    request.ctx.db,
                    request.ctx.account_id,
                    application_id,
                )
        else:
            application_id = args["ApplicationId"]
            record = await CoreAgent.get(
                request.ctx.db,
                request.ctx.account_id,
                application_id,
            )
        return json({
            "Response": {
                "ApplicationId": application_id,
                "AgentId": record.AgentId if record else None,
            }
        })

    @login_required
    async def post(self, request: Request):
        """新增或更新当前用户在指定 application 下的 AgentId"""
        if tagentic_config.WORKBENCH_MODE:
            raise SanicException(
                "AgentId is provisioned by the trusted workbench service",
                status_code=405,
            )
        parser = reqparse.RequestParser()
        parser.add_argument("ApplicationId", type=str, required=True, location="json")
        parser.add_argument("AgentId", type=str, required=True, location="json")
        args = parser.parse_args(request)

        record = await CoreAgent.upsert(
            request.ctx.db,
            request.ctx.account_id,
            args["ApplicationId"],
            args["AgentId"],
        )
        return json({
            "Response": {
                "ApplicationId": record.ApplicationId,
                "AgentId": record.AgentId,
            }
        })


app.add_route(AgentConfigApi.as_view(), "/agent/config")
