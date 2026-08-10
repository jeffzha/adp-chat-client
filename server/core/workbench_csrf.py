import hmac
from typing import Any


WORKBENCH_CSRF_COOKIE = "claw_workbench_csrf"


def valid_workbench_csrf(cookies: Any, headers: Any) -> bool:
    cookie = str(cookies.get(WORKBENCH_CSRF_COOKIE) or "")
    header = str(headers.get("X-Workbench-CSRF") or "")
    return bool(
        32 <= len(cookie) <= 128
        and 32 <= len(header) <= 128
        and hmac.compare_digest(cookie, header)
    )
