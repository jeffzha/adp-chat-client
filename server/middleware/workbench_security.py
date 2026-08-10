import os
import re
from pathlib import Path

from sanic import json

from app_factory import TAgenticApp
from config import tagentic_config
from core.workbench_control import WorkbenchControlClient, WorkbenchControlError


app = TAgenticApp.get_app()


_DISABLED_LOCAL_AUTH_PATHS = frozenset(
    {
        "/login",
        "/account/customer",
        "/account/providers",
    }
)

_WORKBENCH_ENDPOINT_METHODS = {
    "/": frozenset({"GET"}),
    "/auth/sso": frozenset({"GET"}),
    "/account/info": frozenset({"GET"}),
    "/application/list": frozenset({"GET"}),
    "/agent/config": frozenset({"GET"}),
    "/chat/message": frozenset({"POST"}),
    "/chat/turn/events": frozenset({"GET"}),
    "/chat/turn/cancel": frozenset({"POST"}),
    "/chat/messages": frozenset({"GET"}),
    "/chat/conversations": frozenset({"GET"}),
    "/chat/conversation/delete": frozenset({"POST"}),
    "/feedback/rate": frozenset({"POST"}),
    "/file/upload": frozenset({"POST"}),
    "/file/download": frozenset({"GET"}),
    "/file/parse": frozenset({"POST"}),
    "/reference/detail": frozenset({"POST"}),
    "/share/create": frozenset({"POST"}),
    "/suggestions": frozenset({"GET"}),
    "/system/config": frozenset({"GET"}),
    "/integrations": frozenset({"GET"}),
    "/integrations/bindings": frozenset({"POST"}),
}


def workbench_endpoint_is_allowed(
    method: str,
    path: str,
    *,
    helper_asr_enabled: bool = False,
) -> bool:
    """Return whether a route and method are part of the audited workbench surface."""
    normalized_method = str(method or "").upper()
    normalized_path = str(path or "")
    if normalized_path == "/api/internal/workbench/retention/intents":
        return normalized_method == "POST"
    if normalized_path == "/sandbox/config":
        return normalized_method == "GET"
    if normalized_path == "/sandbox":
        return normalized_method in {"GET", "POST"}
    if normalized_path in {
        "/sandbox/acceptance/provider-start-count",
        "/sandbox/acceptance/instances",
    }:
        return normalized_method == "GET"
    if normalized_path == "/sandbox/acceptance/cleanup":
        return normalized_method == "POST"
    if normalized_path.startswith("/sandbox/"):
        segments = normalized_path.strip("/").split("/")
        if len(segments) == 2 and segments[1]:
            return normalized_method == "GET"
        if len(segments) == 3 and segments[1]:
            action = segments[2]
            if action in {"pause", "resume", "stop", "code", "shell", "pty"}:
                return normalized_method == "POST"
            if action == "files":
                return normalized_method in {"GET", "PUT"}
        if (
            len(segments) == 4
            and segments[1]
            and segments[2:] == ["pty", "connect"]
        ):
            return normalized_method == "GET"
        if (
            len(segments) == 4
            and segments[1]
            and segments[2:] == ["shell", "stream"]
        ):
            return normalized_method == "POST"
        return False
    if normalized_path == "/scheduled-tasks":
        return normalized_method in {"GET", "POST"}
    if normalized_path.startswith("/scheduled-tasks/"):
        segments = normalized_path.strip("/").split("/")
        if len(segments) == 2:
            return normalized_method in {"GET", "PATCH", "DELETE"}
        if len(segments) == 3 and segments[2] in {"pause", "resume", "run"}:
            return normalized_method == "POST"
        if len(segments) == 3 and segments[2] == "runs":
            return normalized_method == "GET"
        return False
    if normalized_path == "/integrations/bindings":
        return normalized_method == "POST"
    if normalized_path.startswith("/integrations/"):
        segments = normalized_path.strip("/").split("/")
        if (
            len(segments) == 5
            and segments[1] == "connectors"
            and segments[2]
            and segments[3:] == ["oauth", "start"]
        ):
            return normalized_method == "POST"
        if (
            len(segments) == 4
            and segments[1] == "connectors"
            and segments[2]
            and segments[3] == "disconnect"
        ):
            return normalized_method == "POST"
        if (
            len(segments) == 4
            and segments[1:3] == ["oauth", "callback"]
            and segments[3]
        ):
            return normalized_method == "GET"
        return False
    if normalized_path == "/static" or normalized_path.startswith("/static/"):
        return normalized_method in {"GET", "HEAD"}
    if normalized_path.startswith("/adp/") and normalized_path.count("/") == 2:
        return normalized_method == "POST" and bool(normalized_path.removeprefix("/adp/"))
    if normalized_path == "/helper/asr/url":
        return helper_asr_enabled and normalized_method == "GET"
    return normalized_method in _WORKBENCH_ENDPOINT_METHODS.get(
        normalized_path,
        frozenset(),
    )


def validate_workbench_file_configuration() -> None:
    """Reject an enabled file pipeline that cannot enforce its security contract."""

    if not tagentic_config.WORKBENCH_FILES_ENABLED:
        return

    if not str(tagentic_config.WORKBENCH_FILE_SCANNER_HOST or "").strip():
        raise RuntimeError("WORKBENCH_FILE_SCANNER_HOST is required when files are enabled")
    if not 1 <= tagentic_config.WORKBENCH_FILE_SCANNER_PORT <= 65535:
        raise RuntimeError("WORKBENCH_FILE_SCANNER_PORT must be between 1 and 65535")
    if not 1 <= tagentic_config.WORKBENCH_FILE_SCANNER_TIMEOUT_SECONDS <= 120:
        raise RuntimeError(
            "WORKBENCH_FILE_SCANNER_TIMEOUT_SECONDS must be between 1 and 120"
        )
    if not 1 <= tagentic_config.WORKBENCH_FILE_ABSOLUTE_MAX_BYTES <= 50 * 1024 * 1024:
        raise RuntimeError(
            "WORKBENCH_FILE_ABSOLUTE_MAX_BYTES must be between 1 and 52428800"
        )
    if (
        tagentic_config.WORKBENCH_FILE_SCANNER_MAX_BYTES
        < tagentic_config.WORKBENCH_FILE_ABSOLUTE_MAX_BYTES
    ):
        raise RuntimeError(
            "WORKBENCH_FILE_SCANNER_MAX_BYTES must cover the absolute file limit"
        )
    if not 1 <= tagentic_config.WORKBENCH_FILE_MAX_CONCURRENT_UPLOADS <= 16:
        raise RuntimeError(
            "WORKBENCH_FILE_MAX_CONCURRENT_UPLOADS must be between 1 and 16"
        )
    required_capacity = (
        tagentic_config.WORKBENCH_FILE_ABSOLUTE_MAX_BYTES
        * tagentic_config.WORKBENCH_FILE_MAX_CONCURRENT_UPLOADS
    )
    if tagentic_config.WORKBENCH_FILE_QUARANTINE_CAPACITY_BYTES < required_capacity:
        raise RuntimeError(
            "WORKBENCH_FILE_QUARANTINE_CAPACITY_BYTES cannot hold every concurrent upload"
        )
    if tagentic_config.WORKBENCH_FILE_QUARANTINE_CAPACITY_BYTES > 100 * 1024 * 1024:
        raise RuntimeError(
            "WORKBENCH_FILE_QUARANTINE_CAPACITY_BYTES cannot exceed 104857600"
        )

    quarantine = Path(
        str(tagentic_config.WORKBENCH_FILE_QUARANTINE_DIR or "").strip()
    )
    if (
        not quarantine.is_absolute()
        or not quarantine.is_dir()
        or not os.access(quarantine, os.R_OK | os.W_OK | os.X_OK)
    ):
        raise RuntimeError(
            "WORKBENCH_FILE_QUARANTINE_DIR must be an accessible dedicated directory"
        )

    region = str(tagentic_config.WORKBENCH_FILE_COS_REGION or "").strip()
    bucket = str(tagentic_config.WORKBENCH_FILE_COS_BUCKET or "").strip()
    if (
        not re.fullmatch(r"[a-z0-9-]{1,64}", region)
        or not re.fullmatch(r"[a-z0-9-]{1,255}", bucket)
        or not str(tagentic_config.WORKBENCH_FILE_COS_SECRET_ID or "").strip()
        or not str(tagentic_config.WORKBENCH_FILE_COS_SECRET_KEY or "").strip()
    ):
        raise RuntimeError("private workbench COS configuration is invalid")
    if not 30 <= tagentic_config.WORKBENCH_FILE_URL_EXPIRE_SECONDS <= 900:
        raise RuntimeError(
            "WORKBENCH_FILE_URL_EXPIRE_SECONDS must be between 30 and 900"
        )


@app.middleware("request")
async def restrict_workbench_endpoint_surface(request):
    if not tagentic_config.WORKBENCH_MODE:
        return None
    if workbench_endpoint_is_allowed(
        request.method,
        request.server_path,
        helper_asr_enabled=tagentic_config.WORKBENCH_ENABLE_HELPER_ASR,
    ):
        return None
    return json(
        {"success": False, "message": "endpoint is not available in workbench mode"},
        status=404,
        headers={"Cache-Control": "no-store"},
    )


@app.middleware("request")
async def disable_local_authentication(request):
    if not tagentic_config.WORKBENCH_MODE:
        return None
    if request.server_path in _DISABLED_LOCAL_AUTH_PATHS or request.server_path.startswith("/oauth/"):
        return json({"success": False, "message": "local authentication is disabled"}, status=404)
    return None


@app.listener("before_server_start")
async def validate_workbench_configuration(_app, _loop):
    if not tagentic_config.WORKBENCH_MODE:
        if tagentic_config.WORKBENCH_APP_MIGRATION_WORKER_ENABLED:
            raise RuntimeError(
                "WORKBENCH_APP_MIGRATION_WORKER_ENABLED requires WORKBENCH_MODE"
            )
        return
    if tagentic_config.AUTO_CREATE_ACCOUNT:
        raise RuntimeError("AUTO_CREATE_ACCOUNT must be false when WORKBENCH_MODE is enabled")
    try:
        base_url = WorkbenchControlClient.validated_base_url()
        WorkbenchControlClient.tls_context(base_url)
    except WorkbenchControlError as error:
        raise RuntimeError(str(error)) from error
    if len(tagentic_config.WORKBENCH_SERVICE_HMAC_SECRET) < 32:
        raise RuntimeError("WORKBENCH_SERVICE_HMAC_SECRET must contain at least 32 characters")
    if not tagentic_config.SECRET_KEY:
        raise RuntimeError("SECRET_KEY is required when WORKBENCH_MODE is enabled")
    if not 30 <= tagentic_config.WORKBENCH_VENDOR_CACHE_IDLE_SECONDS <= 3600:
        raise RuntimeError(
            "WORKBENCH_VENDOR_CACHE_IDLE_SECONDS must be between 30 and 3600"
        )
    if not 1 <= tagentic_config.WORKBENCH_VENDOR_CACHE_MAX_ENTRIES <= 1000:
        raise RuntimeError(
            "WORKBENCH_VENDOR_CACHE_MAX_ENTRIES must be between 1 and 1000"
        )
    validate_workbench_file_configuration()
