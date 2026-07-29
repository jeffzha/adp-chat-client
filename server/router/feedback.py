from sanic import json
from sanic.views import HTTPMethodView
from sanic_restful_api import reqparse
from sanic.request.types import Request
from sanic.log import logger
from router import login_required
from core.conversation import CoreConversation
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

        try:
            application_id = await CoreConversation.get_application_id(
                request.ctx.db,
                request.ctx.account_id,
                args['ConversationId']
            )
        except Exception:
            # 渠道/定时任务会话不在本地 ChatConversation 表中，回退使用前端传入的 ApplicationId
            if args.get('ApplicationId'):
                application_id = args['ApplicationId']
                logger.info(f"feedback/rate fallback: conv={args['ConversationId']} not in local db, using ApplicationId={application_id}")
            else:
                raise

        vendor_app = app.get_vendor_app(application_id)
        logger.info(f"feedback/rate: conv={args['ConversationId']} record={args['RecordId']} score={args['Score']} app_id={application_id}")

        await vendor_app.rate(
            request.ctx.db,
            request.ctx.account_id,
            args['ConversationId'],
            args['RecordId'],
            args['Score']
        )

        return json({})


app.add_route(TCADPFeedbackRateApi.as_view(), "/feedback/rate")
