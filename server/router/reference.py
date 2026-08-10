from sanic import json
from sanic.views import HTTPMethodView
from sanic.request.types import Request
from sanic.exceptions import SanicException
from sanic_restful_api import reqparse

from router import authorize_workbench_request, check_login
from config import tagentic_config
from core.workbench_policy import WorkbenchPolicy, WorkbenchPolicyError
from core.share import CoreShareConversation
from app_factory import TAgenticApp

app: TAgenticApp = TAgenticApp.get_app()


class ReferenceDetailApi(HTTPMethodView):
    async def post(self, request: Request):
        parser = reqparse.RequestParser()
        parser.add_argument("ApplicationId", type=str, required=False, location="json")
        parser.add_argument("ShareId", type=str, required=False, location="json")
        parser.add_argument("ReferenceIds", type=list[str], required=True, location="json")
        args = parser.parse_args(request)

        if tagentic_config.WORKBENCH_MODE:
            if args.get("ShareId"):
                raise SanicException(
                    "shared references are disabled in workbench mode",
                    status_code=403,
                )
            claims = check_login(request)
            await authorize_workbench_request(request, claims)
            try:
                WorkbenchPolicy.require_capability(
                    request.ctx.workbench_app_context,
                    "chat",
                )
            except WorkbenchPolicyError as error:
                raise SanicException(str(error), status_code=error.status_code) from error
            if (
                args.get("ApplicationId")
                and args["ApplicationId"] != request.ctx.workbench_app_context.application_id
            ):
                raise SanicException(
                    "ApplicationId is outside the active workbench context",
                    status_code=403,
                )
            # ReferenceIds are currently supplied by the browser and there is no
            # local Record/Turn binding for them. Keep the proxy closed instead of
            # allowing cross-user ID probing.
            raise SanicException(
                "workbench references are disabled until provider record ownership binding is confirmed",
                status_code=503,
            )

        reference_ids = args["ReferenceIds"] or []
        if not reference_ids:
            return json({"References": []})

        application_id = args["ApplicationId"]
        share_id = args["ShareId"]
        account_id = None

        if share_id:
            shared_conversation = await CoreShareConversation.list(request.ctx.db, share_id)
            if shared_conversation is None:
                raise SanicException("share not found", status_code=404)
            application_id = shared_conversation.ApplicationId
        else:
            if not application_id:
                raise SanicException("ApplicationId or ShareId is required", status_code=400)
            check_login(request)
            account_id = request.ctx.account_id

        vendor_app = app.get_vendor_app(application_id)
        try:
            references = await vendor_app.get_reference_details(account_id, reference_ids)
        except NotImplementedError as error:
            raise SanicException(str(error), status_code=501) from error

        return json({"References": references})


app.add_route(ReferenceDetailApi.as_view(), "/reference/detail")
