from sanic import json
from sanic.views import HTTPMethodView
from sanic.request.types import Request

from app_factory import TAgenticApp
from config import tagentic_config

app = TAgenticApp.get_app()


class SystemConfigApi(HTTPMethodView):
    async def get(self, request: Request):
        tc_secret_appid = str(app.config.TC_SECRET_APPID or "").strip()
        enable_voice_input = tc_secret_appid != "" and (
            not tagentic_config.WORKBENCH_MODE
            or tagentic_config.WORKBENCH_ENABLE_HELPER_ASR
        )
        return json({
            "Config": {
                "EnableVoiceInput": enable_voice_input
            }
        })


app.add_route(SystemConfigApi.as_view(), "/system/config")
