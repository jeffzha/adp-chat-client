from sanic import json
from sanic.views import HTTPMethodView
from sanic.request.types import Request
from config import tagentic_config
from router import authorize_workbench_request, check_login
from app_factory import TAgenticApp
app = TAgenticApp.get_app()


class ApplicationListApi(HTTPMethodView):
    async def get(self, request: Request):
        if tagentic_config.WORKBENCH_MODE:
            claims = check_login(request)
            context = await authorize_workbench_request(request, claims)
            vendor_app = app.get_vendor_app(context.application_id)
            info = await vendor_app.get_info(use_trusted_app_id=True)
            info.ApplicationId = context.application_id
            return json({"Applications": [info]})
        apps_info = request.ctx.apps_info
        return json({"Applications": apps_info})


app.add_route(ApplicationListApi.as_view(), "/application/list")
