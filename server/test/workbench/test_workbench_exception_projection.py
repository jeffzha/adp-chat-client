import logging
from types import SimpleNamespace

from sanic.exceptions import SanicException

from core.workbench_exception_projection import project_workbench_exception


def _request():
    return SimpleNamespace(
        id="request-7",
        method="GET",
        server_path="/chat/messages",
    )


def test_provider_secret_and_signed_url_are_absent_from_response_and_logs(caplog):
    secret = "provider-app-key-secret"
    signed_url = (
        "https://private-workbench.cos.ap-guangzhou.myqcloud.com/object"
        "?q-sign-algorithm=sha1&q-signature=sensitive"
    )

    with caplog.at_level(logging.ERROR):
        body, status = project_workbench_exception(
            _request(),
            RuntimeError(f"provider failed app_key={secret} url={signed_url}"),
        )

    assert status == 500
    assert body == {
        "Error": {
            "Message": "workbench request failed",
            "Exception": "WorkbenchError",
        }
    }
    rendered = f"{body!r}\n{caplog.text}"
    assert secret not in rendered
    assert signed_url not in rendered
    assert "q-signature" not in rendered
    assert "request_id=request-7" in caplog.text
    assert "route=/chat/messages" in caplog.text
    assert "error_type=RuntimeError" in caplog.text


def test_explicit_client_error_keeps_safe_message(caplog):
    with caplog.at_level(logging.ERROR):
        body, status = project_workbench_exception(
            _request(),
            SanicException("file type is not supported", status_code=415),
        )

    assert status == 415
    assert body["Error"]["Message"] == "file type is not supported"
    assert body["Error"]["Exception"] == "SanicException"


def test_explicit_server_error_is_still_redacted(caplog):
    signed_url = "https://private.example/object?q-signature=sensitive"

    with caplog.at_level(logging.ERROR):
        body, status = project_workbench_exception(
            _request(),
            SanicException(signed_url, status_code=503),
        )

    assert status == 503
    assert body["Error"] == {
        "Message": "workbench request failed",
        "Exception": "WorkbenchError",
    }
    assert signed_url not in caplog.text
