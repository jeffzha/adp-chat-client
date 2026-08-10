"""Memory-isolated Tencent AGS/E2B code-execution worker.

The e2b-code-interpreter 2.9.0 parser appends a complete Jupyter frame to its
Execution object before invoking callbacks.  Running the public ``run_code``
API in this short-lived process lets the web process enforce a hard memory
boundary even for a single oversized frame.  The parent accepts only this
small, versioned JSON protocol and never forwards worker stderr.
"""

import asyncio
import json
import os
import sys
from typing import Any


_PROTOCOL_VERSION = 1
_API_KEY_ENV = "WORKBENCH_AGSX_CODE_WORKER_API_KEY"


class _OutputLimit(RuntimeError):
    pass


def _apply_process_limits(memory_bytes: int, cpu_seconds: int) -> None:
    if os.name != "posix":
        raise RuntimeError("worker_memory_limit_unavailable")
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


async def _execute(request: dict[str, Any]) -> dict[str, Any]:
    from e2b import TimeoutException
    from e2b_code_interpreter import AsyncSandbox

    instance_id = request["instance_id"]
    code = request["code"]
    language = request["language"]
    domain = request["domain"]
    timeout_seconds = request["timeout_seconds"]
    request_timeout_seconds = request["request_timeout_seconds"]
    limit = request["max_output_bytes"]
    api_key = os.environ.pop(_API_KEY_ENV, "")
    if not api_key.startswith("ark_") or len(api_key) < 16:
        return {"version": _PROTOCOL_VERSION, "ok": False, "code": "agsx_api_key_invalid"}

    received = 0
    stdout: list[str] = []
    stderr: list[str] = []
    results: list[str] = []
    execution_error: list[str] = []

    def consume(value: Any, *, depth: int = 0, seen: set[int] | None = None) -> None:
        nonlocal received
        if value is None or isinstance(value, (bool, int, float)):
            return
        if isinstance(value, str):
            received += len(value.encode("utf-8"))
        elif isinstance(value, bytes):
            received += len(value)
        else:
            if depth >= 16:
                raise _OutputLimit
            seen = seen or set()
            identity = id(value)
            if identity in seen:
                return
            seen.add(identity)
            if isinstance(value, dict):
                for key, item in value.items():
                    consume(key, depth=depth + 1, seen=seen)
                    consume(item, depth=depth + 1, seen=seen)
            elif isinstance(value, (list, tuple, set)):
                for item in value:
                    consume(item, depth=depth + 1, seen=seen)
            elif hasattr(value, "__dict__"):
                consume(vars(value), depth=depth + 1, seen=seen)
        if received > limit:
            raise _OutputLimit

    def append(target: list[str], value: Any) -> None:
        if isinstance(value, str):
            consume(value)
            target.append(value)

    def on_stdout(message: Any) -> None:
        append(stdout, getattr(message, "line", None))

    def on_stderr(message: Any) -> None:
        append(stderr, getattr(message, "line", None))

    def on_result(result: Any) -> None:
        append(results, getattr(result, "text", None))
        for field in (
            "html",
            "markdown",
            "svg",
            "png",
            "jpeg",
            "pdf",
            "latex",
            "json",
            "javascript",
            "data",
            "chart",
            "extra",
        ):
            if hasattr(result, field):
                consume(getattr(result, field))
                setattr(result, field, None)

    def on_error(error: Any) -> None:
        name = getattr(error, "name", None)
        value = getattr(error, "value", None)
        if isinstance(name, str) and isinstance(value, str):
            append(execution_error, f"{name}: {value}")
        elif isinstance(name, str):
            append(execution_error, name)
        if hasattr(error, "traceback"):
            consume(error.traceback)
            error.traceback = ""

    try:
        sandbox = await AsyncSandbox.connect(
            sandbox_id=instance_id,
            api_key=api_key,
            validate_api_key=False,
            domain=domain,
            request_timeout=request_timeout_seconds,
        )
    except Exception:
        return {
            "version": _PROTOCOL_VERSION,
            "ok": False,
            "code": "provider_data_unavailable",
        }
    try:
        await sandbox.run_code(
            code,
            language=language,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            on_result=on_result,
            on_error=on_error,
            timeout=timeout_seconds,
            request_timeout=request_timeout_seconds,
        )
        return {
            "version": _PROTOCOL_VERSION,
            "ok": True,
            "stdout": "".join(stdout),
            "stderr": "".join(stderr),
            "results": results,
            "error": execution_error[0] if execution_error else None,
        }
    except _OutputLimit:
        return {"version": _PROTOCOL_VERSION, "ok": False, "code": "output_limit_exceeded"}
    except TimeoutException:
        return {"version": _PROTOCOL_VERSION, "ok": False, "code": "sandbox_runtime_timeout"}
    except (MemoryError, asyncio.CancelledError):
        return {"version": _PROTOCOL_VERSION, "ok": False, "code": "sandbox_runtime_unknown"}
    except Exception:
        # A transport/protocol exception after run_code was invoked cannot prove
        # whether the remote kernel accepted the cell.  The parent must stop the
        # whole sandbox instead of treating this as a safe pre-submit failure.
        return {"version": _PROTOCOL_VERSION, "ok": False, "code": "sandbox_runtime_unknown"}


def _validated_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != _PROTOCOL_VERSION:
        raise ValueError("worker_protocol_invalid")
    if not isinstance(value.get("instance_id"), str) or not value["instance_id"]:
        raise ValueError("worker_protocol_invalid")
    if not isinstance(value.get("code"), str) or not value["code"]:
        raise ValueError("worker_protocol_invalid")
    if value.get("language") not in {"python", "javascript", "typescript", "r", "java", "bash"}:
        raise ValueError("worker_protocol_invalid")
    if not isinstance(value.get("domain"), str) or not value["domain"]:
        raise ValueError("worker_protocol_invalid")
    for name in (
        "timeout_seconds",
        "request_timeout_seconds",
        "max_output_bytes",
        "memory_bytes",
    ):
        if not isinstance(value.get(name), int) or value[name] <= 0:
            raise ValueError("worker_protocol_invalid")
    return value


def main() -> int:
    maximum_response_bytes = 65536
    try:
        raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("worker_protocol_invalid")
        request = _validated_request(json.loads(raw))
        maximum_response_bytes = request["max_output_bytes"] + 65536
        _apply_process_limits(
            request["memory_bytes"],
            request["timeout_seconds"] + 5,
        )
        response = asyncio.run(_execute(request))
    except BaseException:
        response = {
            "version": _PROTOCOL_VERSION,
            "ok": False,
            "code": "sandbox_runtime_unknown",
        }
    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded) > maximum_response_bytes:
        encoded = json.dumps(
            {
                "version": _PROTOCOL_VERSION,
                "ok": False,
                "code": "output_limit_exceeded",
            },
            separators=(",", ":"),
        ).encode("ascii")
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
