from sanic import json
from sanic.exceptions import SanicException
from sanic.views import HTTPMethodView
from sanic_restful_api import reqparse
from sanic.request.types import Request
from sanic.log import logger
from router import login_required
from core.conversation import CoreConversation
from config import tagentic_config
from core.workbench_policy import WorkbenchPolicy, WorkbenchPolicyError
from app_factory import TAgenticApp
app: TAgenticApp = TAgenticApp.get_app()


class TCADPFeedbackRateApi(HTTPMethodView):
    @login_required
    async def post(self, request: Request):
        parser = reqparse.RequestParser()
        parser.add_argument("ConversationId", type=str, required=True, location="json")
        parser.add_argument("RecordId", type=str, required=True, location="json")
        parser.add_argument("Score", type=int, required=True, location="json")
        parser.add_argument("ApplicationId", type=str, required=False, location="json", default="")
        args = parser.parse_args(request)

        if tagentic_config.WORKBENCH_MODE:
            try:
                WorkbenchPolicy.require_capability(
                    request.ctx.workbench_app_context,
                    "chat",
                )
                WorkbenchPolicy.require_active(request.ctx.workbench_context)
            except WorkbenchPolicyError as error:
                raise SanicException(str(error), status_code=error.status_code) from error
            raise SanicException(
                "workbench feedback is disabled until provider Record ownership binding is confirmed",
                status_code=503,
            )

        try:
            application_id = await CoreConversation.get_application_id(
                request.ctx.db,
                request.ctx.account_id,
                args['ConversationId']
            )
        except Exception:
            if tagentic_config.WORKBENCH_MODE:
                raise SanicException("conversation not found", status_code=404)
            # 渠道/定时任务会话不在本地 ChatConversation 表中，回退使用前端传入的 ApplicationId
            if args.get('ApplicationId'):
                application_id = args['ApplicationId']
                logger.info(f"feedback/rate fallback: conv={args['ConversationId']} not in local db, using ApplicationId={application_id}")
            else:
                raise

        if (
            tagentic_config.WORKBENCH_MODE
            and application_id != request.ctx.workbench_app_context.application_id
        ):
            raise SanicException("conversation not found", status_code=404)

        vendor_app = app.get_vendor_app(application_id)
        logger.info("feedback/rate: submitting owned record feedback")

        await vendor_app.rate(
            request.ctx.db,
            request.ctx.account_id,
            args['ConversationId'],
            args['RecordId'],
            args['Score']
        )

        return json({})


app.add_route(TCADPFeedbackRateApi.as_view(), "/feedback/rate")
