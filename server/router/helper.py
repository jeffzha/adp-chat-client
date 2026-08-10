from sanic import json
from sanic.views import HTTPMethodView
from sanic.request.types import Request
from sanic.exceptions import SanicException
from router import login_required
from util.tca import asr_url
from config import tagentic_config
from app_factory import TAgenticApp
app = TAgenticApp.get_app()


class TCADPHelperAsrUrlApi(HTTPMethodView):
    @login_required
    async def get(self, request: Request):
        if (
            tagentic_config.WORKBENCH_MODE
            and not tagentic_config.WORKBENCH_ENABLE_HELPER_ASR
        ):
            raise SanicException("voice input is disabled in workbench mode", status_code=404)
        url = asr_url()
        return json({"url": url})


app.add_route(TCADPHelperAsrUrlApi.as_view(), "/helper/asr/url")
