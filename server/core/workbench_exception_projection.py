import logging
from typing import Any

from sanic import exceptions


logger = logging.getLogger(__name__)


def _bounded_log_value(value: Any, maximum: int) -> str:
    normalized = str(value or "-")
    normalized = "".join(
        character if 32 <= ord(character) < 127 else "_"
        for character in normalized
    )
    return normalized[:maximum] or "-"


def project_workbench_exception(request: Any, exception: Exception) -> tuple[dict, int]:
    """Return and log a secret-safe workbench error response.

    Only explicit Sanic client errors are allowed to retain their message. An
    unexpected exception, or any server-side error, can contain provider
    request bodies, credentials, signed URLs, or object locators and is
    therefore projected to one stable public error.
    """

    raw_status = getattr(exception, "status_code", 500)
    try:
        status_code = int(raw_status)
    except (TypeError, ValueError):
        status_code = 500
    if status_code < 400 or status_code > 599:
        status_code = 500

    request_id = _bounded_log_value(getattr(request, "id", None), 128)
    method = _bounded_log_value(getattr(request, "method", None), 16)
    route = _bounded_log_value(
        getattr(request, "server_path", None) or getattr(request, "path", None),
        512,
    )
    error_type = _bounded_log_value(type(exception).__name__, 128)
    logger.error(
        "[workbench_error] request_id=%s method=%s route=%s status=%s error_type=%s",
        request_id,
        method,
        route,
        status_code,
        error_type,
    )

    if isinstance(exception, exceptions.SanicException) and status_code < 500:
        message = str(exception) or "workbench request was rejected"
        public_type = error_type
    else:
        message = "workbench request failed"
        public_type = "WorkbenchError"
    return {
        "Error": {
            "Message": message,
            "Exception": public_type,
        }
    }, status_code
