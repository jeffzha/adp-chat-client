import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone

import ujson
from sanic import json
from sanic.request.types import Request
from sanic.views import HTTPMethodView
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from app_factory import TAgenticApp
from config import tagentic_config
from core.workbench_retention import CoreWorkbenchRetention, WorkbenchRetentionError
from model.workbench_retention import WorkbenchInboundNonce


app = TAgenticApp.get_app()
CONTRACT_VERSION = "2"
CONTRACT_VERSION_HEADER = "X-Workbench-Contract-Version"
RETENTION_PATH = "/api/internal/workbench/retention/intents"
MAX_BODY_BYTES = 64 * 1024


def _signed_response(payload, status: int, nonce: str):
    response = json(payload, status=status, headers={"Cache-Control": "no-store"})
    timestamp = str(int(time.time()))
    body_hash = hashlib.sha256(response.body).hexdigest()
    canonical = "\n".join(
        (CONTRACT_VERSION, str(status), RETENTION_PATH, timestamp, nonce, body_hash)
    )
    signature = hmac.new(
        tagentic_config.WORKBENCH_SERVICE_HMAC_SECRET.encode(),
        canonical.encode(),
        hashlib.sha256,
    ).hexdigest()
    response.headers[CONTRACT_VERSION_HEADER] = CONTRACT_VERSION
    response.headers["X-Workbench-Response-Timestamp"] = timestamp
    response.headers["X-Workbench-Response-Nonce"] = nonce
    response.headers["X-Workbench-Response-Signature"] = signature
    return response


async def _authenticate(request: Request) -> str:
    if len(request.body) > MAX_BODY_BYTES:
        raise WorkbenchRetentionError("valid retention signature is required", 401)
    nonce = CoreWorkbenchRetention.verify_signed_request(
        request.method,
        RETENTION_PATH,
        request.body,
        request.headers,
        tagentic_config.WORKBENCH_SERVICE_HMAC_SECRET,
        tagentic_config.WORKBENCH_ALLOWED_CLOCK_SKEW_SECONDS,
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await request.ctx.db.execute(
        delete(WorkbenchInboundNonce).where(WorkbenchInboundNonce.ExpiresAt < now)
    )
    request.ctx.db.add(
        WorkbenchInboundNonce(
            ServiceName="claw-control",
            Nonce=nonce,
            ExpiresAt=now
            + timedelta(
                seconds=max(1, tagentic_config.WORKBENCH_ALLOWED_CLOCK_SKEW_SECONDS * 2)
            ),
        )
    )
    try:
        await request.ctx.db.flush()
    except IntegrityError as error:
        await request.ctx.db.rollback()
        raise WorkbenchRetentionError("retention request nonce was replayed", 409) from error
    return nonce


class WorkbenchRetentionIntentApi(HTTPMethodView):
    async def post(self, request: Request):
        nonce = str(request.headers.get("X-Workbench-Nonce", "")).strip()
        if not tagentic_config.WORKBENCH_MODE:
            return json({"success": False, "message": "not found"}, status=404)
        try:
            nonce = await _authenticate(request)
            try:
                payload = ujson.loads(request.body)
            except (TypeError, ValueError) as error:
                raise WorkbenchRetentionError("retention request JSON is invalid", 400) from error
            receipt = await CoreWorkbenchRetention.execute(request.ctx.db, payload)
            return _signed_response({"success": True, "data": receipt}, 200, nonce)
        except WorkbenchRetentionError as error:
            await request.ctx.db.rollback()
            if nonce and len(nonce) <= 96 and len(tagentic_config.WORKBENCH_SERVICE_HMAC_SECRET) >= 32:
                return _signed_response(
                    {"success": False, "message": str(error)}, error.status_code, nonce
                )
            return json({"success": False, "message": str(error)}, status=error.status_code)
        except Exception:
            await request.ctx.db.rollback()
            if nonce and len(nonce) <= 96 and len(tagentic_config.WORKBENCH_SERVICE_HMAC_SECRET) >= 32:
                return _signed_response(
                    {"success": False, "message": "retention processing failed"}, 500, nonce
                )
            return json(
                {"success": False, "message": "retention processing failed"}, status=500
            )


app.add_route(WorkbenchRetentionIntentApi.as_view(), RETENTION_PATH)
