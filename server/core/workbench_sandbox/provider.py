import asyncio
import base64
import json
import logging
import os
import re
import secrets
import shlex
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from config import tagentic_config
from core.workbench_sandbox.contracts import (
    CodeResult,
    CommandChunk,
    CommandResult,
    ProviderInstance,
    ProviderPty,
    SandboxProviderError,
)
from core.workbench_sandbox.secrets import read_secret_file


T = TypeVar("T")


class _TencentPty:
    def __init__(self, *, sandbox: Any, handle: Any, queue: asyncio.Queue[Any]) -> None:
        self._sandbox = sandbox
        self._handle = handle
        self._queue = queue

    @property
    def pid(self) -> int:
        return int(self._handle.pid)

    async def _output(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield bytes(item)

    def output(self) -> AsyncIterator[bytes]:
        return self._output()

    async def wait(self) -> None:
        try:
            await self._handle.wait()
        finally:
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def send_input(self, data: bytes) -> None:
        await self._sandbox.pty.send_stdin(
            self.pid,
            data,
            request_timeout=tagentic_config.WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS,
        )

    async def resize(self, *, rows: int, cols: int) -> None:
        from e2b import PtySize

        await self._sandbox.pty.resize(
            self.pid,
            PtySize(rows=rows, cols=cols),
            request_timeout=tagentic_config.WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS,
        )

    async def kill(self) -> bool:
        return bool(await self._handle.kill())


class TencentAGSXProvider:
    """Tencent Agent Runtime control plane plus its public E2B data plane.

    The SDK imports are deliberately lazy.  A disabled deployment therefore
    does not require credentials and tests can inject a local fake provider.
    No direct provider URL or bearer token is returned to callers or persisted.
    """

    _STATUS_MAP = {
        "STARTING": "starting",
        "RUNNING": "running",
        "STOPPING": "stopping",
        "STOPPED": "stopped",
        "STOP_FAILED": "stop_failed",
        "FAILED": "failed",
        "PAUSING": "pausing",
        "PAUSED": "paused",
        "PAUSE_FAILED": "pause_failed",
        "RESUMING": "resuming",
        "RESUME_FAILED": "resume_failed",
        "STARTING_FAILED": "starting_failed",
        "STOPPING_FAILED": "stopping_failed",
    }
    _INSTANCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    _STREAM_QUEUE_SIZE = 16
    _OUTPUT_LIMIT_EXIT = 197

    class _CodeWorkerProtocolLimit(RuntimeError):
        pass

    @staticmethod
    def _code_worker_environment(api_key: str) -> dict[str, str]:
        server_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        return {
            "WORKBENCH_AGSX_CODE_WORKER_API_KEY": api_key,
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": server_root,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }

    def __init__(self) -> None:
        self.validate_configuration()

    @staticmethod
    def _secret_file(path_value: str, name: str) -> str:
        try:
            value = read_secret_file(path_value).decode("utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise SandboxProviderError(f"{name}_unavailable", status_code=503) from error
        if not value or len(value) > 8192 or any(ord(char) < 32 for char in value):
            raise SandboxProviderError(f"{name}_invalid", status_code=503)
        return value

    @staticmethod
    def validate_configuration() -> None:
        region = tagentic_config.WORKBENCH_AGSX_REGION.strip().lower()
        domain = tagentic_config.WORKBENCH_AGSX_DOMAIN.strip().lower()
        expected_domain = f"{region}.tencentags.com" if region else ""
        if not region or domain != expected_domain:
            raise SandboxProviderError("provider_domain_invalid", status_code=503)
        if (
            tagentic_config.WORKBENCH_AGSX_CONTROL_ENDPOINT.strip().lower()
            != "ags.tencentcloudapi.com"
        ):
            raise SandboxProviderError(
                "provider_control_endpoint_invalid",
                status_code=503,
            )
        tool_id = tagentic_config.WORKBENCH_AGSX_TOOL_ID.strip()
        tool_name = tagentic_config.WORKBENCH_AGSX_TOOL_NAME.strip()
        if not tool_id and not tool_name:
            raise SandboxProviderError("provider_tool_not_configured", status_code=503)
        if tagentic_config.WORKBENCH_SANDBOX_NETWORK_MODE != "SANDBOX":
            raise SandboxProviderError("provider_network_mode_forbidden", status_code=503)
        if tagentic_config.WORKBENCH_SANDBOX_AUTH_MODE != "TOKEN":
            raise SandboxProviderError("provider_auth_mode_forbidden", status_code=503)

    @classmethod
    def validate_readiness(cls) -> None:
        cls.validate_configuration()
        api_key = cls._secret_file(
            tagentic_config.WORKBENCH_AGSX_API_KEY_FILE,
            "agsx_api_key",
        )
        if not api_key.startswith("ark_") or len(api_key) < 16:
            raise SandboxProviderError("agsx_api_key_invalid", status_code=503)
        cls._secret_file(
            tagentic_config.WORKBENCH_AGSX_CAM_SECRET_ID_FILE,
            "cam_secret_id",
        )
        cls._secret_file(
            tagentic_config.WORKBENCH_AGSX_CAM_SECRET_KEY_FILE,
            "cam_secret_key",
        )
        try:
            read_secret_file(
                tagentic_config.WORKBENCH_SANDBOX_CLIENT_TOKEN_KEY_FILE,
                minimum_bytes=32,
            )
        except OSError as error:
            raise SandboxProviderError(
                "client_token_key_unavailable",
                status_code=503,
            ) from error

    @classmethod
    def _cloud_client(cls):
        from tencentcloud.ags.v20250920.ags_client import AgsClient
        from tencentcloud.common import credential
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile

        secret_id = cls._secret_file(
            tagentic_config.WORKBENCH_AGSX_CAM_SECRET_ID_FILE,
            "cam_secret_id",
        )
        secret_key = cls._secret_file(
            tagentic_config.WORKBENCH_AGSX_CAM_SECRET_KEY_FILE,
            "cam_secret_key",
        )
        http_profile = HttpProfile()
        http_profile.endpoint = tagentic_config.WORKBENCH_AGSX_CONTROL_ENDPOINT.strip()
        http_profile.reqTimeout = (
            tagentic_config.WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS
        )
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        return AgsClient(
            credential.Credential(secret_id, secret_key),
            tagentic_config.WORKBENCH_AGSX_REGION.strip(),
            client_profile,
        )

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        return min(0.5, 0.1 * (2**attempt) + secrets.randbelow(50) / 1000)

    @classmethod
    async def _control_call(
        cls,
        operation: Callable[[], T],
        *,
        retry_count: int = 3,
    ) -> T:
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
            TencentCloudSDKException,
        )

        for attempt in range(retry_count):
            try:
                return await asyncio.to_thread(operation)
            except TencentCloudSDKException as error:
                code = str(getattr(error, "code", ""))
                throttled = "Limit" in code or "Throttl" in code or "Frequency" in code
                if throttled and attempt + 1 < retry_count:
                    await asyncio.sleep(cls._retry_delay(attempt))
                    continue
                raise SandboxProviderError(
                    "provider_rate_limited" if throttled else "provider_control_failed",
                    status_code=429 if throttled else 502,
                    retryable=throttled,
                ) from error
            except (OSError, TimeoutError) as error:
                if attempt + 1 < retry_count:
                    await asyncio.sleep(cls._retry_delay(attempt))
                    continue
                raise SandboxProviderError(
                    "provider_control_unavailable", retryable=True
                ) from error
        raise SandboxProviderError("provider_control_unavailable", retryable=True)

    @staticmethod
    def _parse_expiry(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed

    @classmethod
    def _project_instance(cls, instance: Any) -> ProviderInstance:
        raw_status = str(getattr(instance, "Status", "") or "").upper()
        status = cls._STATUS_MAP.get(raw_status)
        if status is None:
            raise SandboxProviderError(
                "provider_status_unknown",
                instance_id=str(getattr(instance, "InstanceId", "") or "") or None,
            )
        return ProviderInstance(
            instance_id=str(getattr(instance, "InstanceId", "") or ""),
            status=status,
            network_mode=str(getattr(instance, "NetworkMode", "") or "").upper(),
            auth_mode=str(getattr(instance, "AuthMode", "") or "").upper(),
            expires_at=cls._parse_expiry(getattr(instance, "ExpiresAt", None)),
            tool_id=str(getattr(instance, "ToolId", "") or ""),
            tool_name=str(getattr(instance, "ToolName", "") or ""),
            persistent=getattr(instance, "Persistent", None),
        )

    @staticmethod
    def _validate_provider_instance(instance: ProviderInstance) -> None:
        if not TencentAGSXProvider._INSTANCE_ID.fullmatch(instance.instance_id):
            raise SandboxProviderError("provider_contract_invalid")
        if instance.network_mode != "SANDBOX" or instance.auth_mode != "TOKEN":
            raise SandboxProviderError("provider_security_contract_mismatch")
        if instance.persistent is not False:
            raise SandboxProviderError("provider_security_contract_mismatch")
        configured_tool_id = tagentic_config.WORKBENCH_AGSX_TOOL_ID.strip()
        configured_tool_name = tagentic_config.WORKBENCH_AGSX_TOOL_NAME.strip()
        if (
            not instance.tool_id
            or not instance.tool_name
            or (configured_tool_id and instance.tool_id != configured_tool_id)
            or (configured_tool_name and instance.tool_name != configured_tool_name)
        ):
            raise SandboxProviderError("provider_tool_mismatch")

    async def start(
        self,
        *,
        client_token: str,
        timeout_seconds: int,
        metadata: dict[str, str],
    ) -> ProviderInstance:
        from tencentcloud.ags.v20250920 import models

        request = models.StartSandboxInstanceRequest()
        request.ToolId = tagentic_config.WORKBENCH_AGSX_TOOL_ID.strip() or None
        request.ToolName = tagentic_config.WORKBENCH_AGSX_TOOL_NAME.strip() or None
        request.Timeout = f"{timeout_seconds}s"
        request.ClientToken = client_token
        request.AuthMode = "TOKEN"
        request.Metadata = []
        for key, value in sorted(metadata.items()):
            item = models.MetadataVar()
            item.Name = key
            item.Value = value
            request.Metadata.append(item)
        client = self._cloud_client()
        response = await self._control_call(lambda: client.StartSandboxInstance(request))
        try:
            instance = self._project_instance(response.Instance)
            self._validate_provider_instance(instance)
        except SandboxProviderError as error:
            instance_id = str(getattr(response.Instance, "InstanceId", "") or "")
            if instance_id:
                try:
                    await self.stop(instance_id)
                except SandboxProviderError:
                    logging.warning(
                        "[workbench_sandbox] fail-closed cleanup failed provider=tencent_agsx"
                    )
            raise SandboxProviderError(
                error.code,
                status_code=error.status_code,
                retryable=error.retryable,
                instance_id=instance_id or error.instance_id,
            ) from error
        return instance

    async def describe(self, instance_id: str) -> ProviderInstance | None:
        from tencentcloud.ags.v20250920 import models

        request = models.DescribeSandboxInstanceListRequest()
        request.InstanceIds = [instance_id]
        request.Limit = 1
        client = self._cloud_client()
        response = await self._control_call(
            lambda: client.DescribeSandboxInstanceList(request)
        )
        if not response.InstanceSet:
            return None
        raw_instance = response.InstanceSet[0]
        if str(getattr(raw_instance, "InstanceId", "") or "") != instance_id:
            raise SandboxProviderError(
                "provider_instance_mismatch",
                instance_id=instance_id,
            )
        instance = self._project_instance(raw_instance)
        self._validate_provider_instance(instance)
        return instance

    async def pause(self, instance_id: str) -> ProviderInstance:
        from tencentcloud.ags.v20250920 import models

        request = models.PauseSandboxInstanceRequest()
        request.InstanceId = instance_id
        request.Memory = True
        client = self._cloud_client()
        await self._control_call(
            lambda: client.PauseSandboxInstance(request),
            retry_count=1,
        )
        instance = await self.describe(instance_id)
        if instance is None:
            raise SandboxProviderError("provider_instance_not_found", status_code=404)
        return instance

    async def resume(
        self, instance_id: str, *, timeout_seconds: int
    ) -> ProviderInstance:
        from tencentcloud.ags.v20250920 import models

        request = models.ResumeSandboxInstanceRequest()
        request.InstanceId = instance_id
        request.Timeout = f"{timeout_seconds}s"
        client = self._cloud_client()
        await self._control_call(
            lambda: client.ResumeSandboxInstance(request),
            retry_count=1,
        )
        instance = await self.describe(instance_id)
        if instance is None:
            raise SandboxProviderError("provider_instance_not_found", status_code=404)
        return instance

    async def stop(self, instance_id: str) -> None:
        from tencentcloud.ags.v20250920 import models

        request = models.StopSandboxInstanceRequest()
        request.InstanceId = instance_id
        client = self._cloud_client()
        await self._control_call(
            lambda: client.StopSandboxInstance(request),
            retry_count=1,
        )

    @classmethod
    async def _connect(cls, instance_id: str):
        from e2b_code_interpreter import AsyncSandbox

        api_key = cls._secret_file(
            tagentic_config.WORKBENCH_AGSX_API_KEY_FILE,
            "agsx_api_key",
        )
        if not api_key.startswith("ark_") or len(api_key) < 16:
            raise SandboxProviderError("agsx_api_key_invalid", status_code=503)
        try:
            return await AsyncSandbox.connect(
                sandbox_id=instance_id,
                api_key=api_key,
                validate_api_key=False,
                domain=tagentic_config.WORKBENCH_AGSX_DOMAIN.strip(),
                request_timeout=tagentic_config.WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS,
            )
        except Exception as error:
            raise SandboxProviderError("provider_data_unavailable", retryable=True) from error

    async def execute_code(
        self,
        instance_id: str,
        *,
        code: str,
        language: str,
        timeout_seconds: int,
    ) -> CodeResult:
        if os.name != "posix":
            raise SandboxProviderError(
                "provider_contract_unavailable",
                status_code=503,
            )
        api_key = self._secret_file(
            tagentic_config.WORKBENCH_AGSX_API_KEY_FILE,
            "agsx_api_key",
        )
        if not api_key.startswith("ark_") or len(api_key) < 16:
            raise SandboxProviderError("agsx_api_key_invalid", status_code=503)
        maximum_output = int(tagentic_config.WORKBENCH_SANDBOX_MAX_OUTPUT_BYTES)
        request = json.dumps(
            {
                "version": 1,
                "instance_id": instance_id,
                "code": code,
                "language": language,
                "domain": tagentic_config.WORKBENCH_AGSX_DOMAIN.strip(),
                "timeout_seconds": timeout_seconds,
                "request_timeout_seconds": min(
                    timeout_seconds,
                    tagentic_config.WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS,
                ),
                "max_output_bytes": maximum_output,
                "memory_bytes": int(
                    tagentic_config.WORKBENCH_SANDBOX_CODE_WORKER_MEMORY_BYTES
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        environment = self._code_worker_environment(api_key)
        protocol_limit = maximum_output + 65536
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "core.workbench_sandbox.code_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )

        async def read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
            chunks: list[bytes] = []
            received = 0
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return b"".join(chunks)
                received += len(chunk)
                if received > limit:
                    if process.returncode is None:
                        process.kill()
                    raise self._CodeWorkerProtocolLimit
                chunks.append(chunk)

        stdout_task = asyncio.create_task(read_bounded(process.stdout, protocol_limit))
        stderr_task = asyncio.create_task(read_bounded(process.stderr, 65536))

        async def exchange() -> tuple[bytes, bytes, int]:
            process.stdin.write(request)
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()
            return await asyncio.gather(stdout_task, stderr_task, process.wait())

        try:
            stdout, _stderr, _ = await asyncio.wait_for(
                exchange(),
                timeout=timeout_seconds + 2,
            )
        except self._CodeWorkerProtocolLimit as error:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise SandboxProviderError(
                "output_limit_exceeded",
                status_code=413,
            ) from error
        except (TimeoutError, asyncio.CancelledError) as error:
            if process.returncode is None:
                process.kill()
            await process.wait()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise SandboxProviderError(
                "sandbox_runtime_timeout",
                status_code=504,
            ) from error
        except Exception as error:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise SandboxProviderError("sandbox_runtime_unknown") from error
        finally:
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

        if process.returncode != 0:
            raise SandboxProviderError("sandbox_runtime_unknown")
        try:
            response = json.loads(stdout)
        except (UnicodeError, ValueError) as error:
            raise SandboxProviderError("sandbox_runtime_unknown") from error
        if not isinstance(response, dict) or response.get("version") != 1:
            raise SandboxProviderError("sandbox_runtime_unknown")
        if response.get("ok") is not True:
            code_value = response.get("code")
            if code_value == "output_limit_exceeded":
                raise SandboxProviderError(code_value, status_code=413)
            if code_value == "sandbox_runtime_timeout":
                raise SandboxProviderError(code_value, status_code=504)
            if code_value == "agsx_api_key_invalid":
                raise SandboxProviderError(code_value, status_code=503)
            if code_value == "provider_data_unavailable":
                raise SandboxProviderError(code_value)
            raise SandboxProviderError("sandbox_runtime_unknown")
        stdout_value = response.get("stdout")
        stderr_value = response.get("stderr")
        results_value = response.get("results")
        error_value = response.get("error")
        if (
            not isinstance(stdout_value, str)
            or not isinstance(stderr_value, str)
            or not isinstance(results_value, list)
            or not all(isinstance(item, str) for item in results_value)
            or (error_value is not None and not isinstance(error_value, str))
        ):
            raise SandboxProviderError("sandbox_runtime_unknown")
        projected = CodeResult(
            stdout=stdout_value,
            stderr=stderr_value,
            results=tuple(results_value),
            error=error_value,
        )
        if sum(
            len(value.encode("utf-8"))
            for value in (
                projected.stdout,
                projected.stderr,
                *projected.results,
                projected.error or "",
            )
        ) > maximum_output:
            raise SandboxProviderError("output_limit_exceeded", status_code=413)
        return projected

    @classmethod
    def _bounded_shell_command(cls, command: str) -> str:
        encoded = base64.urlsafe_b64encode(command.encode("utf-8")).decode("ascii")
        limit = int(tagentic_config.WORKBENCH_SANDBOX_MAX_OUTPUT_BYTES)
        limiter = r'''import base64, os, selectors, signal, subprocess, sys
command = base64.urlsafe_b64decode(sys.argv[1].encode("ascii")).decode("utf-8")
limit = int(sys.argv[2])
proc = subprocess.Popen(["/bin/sh", "-lc", command], stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
selector = selectors.DefaultSelector()
selector.register(proc.stdout, selectors.EVENT_READ, 1)
selector.register(proc.stderr, selectors.EVENT_READ, 2)
written = 0
overflow = False
while selector.get_map():
    for key, _ in selector.select():
        remaining = limit - written
        chunk = os.read(key.fileobj.fileno(), min(8192, max(1, remaining + 1)))
        if not chunk:
            selector.unregister(key.fileobj)
            key.fileobj.close()
            continue
        if remaining > 0:
            bounded = chunk[:remaining]
            os.write(key.data, bounded)
            written += len(bounded)
        if len(chunk) > max(0, remaining):
            overflow = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            for registered in list(selector.get_map().values()):
                selector.unregister(registered.fileobj)
                registered.fileobj.close()
            break
    if overflow:
        break
exit_code = proc.wait()
sys.exit(197 if overflow else (exit_code if 0 <= exit_code <= 255 else 1))'''
        return "python3 -c {} {} {}".format(
            shlex.quote(limiter),
            shlex.quote(encoded),
            limit,
        )

    async def run_command(
        self,
        instance_id: str,
        *,
        command: str,
        cwd: str,
        timeout_seconds: int,
    ) -> CommandResult:
        from e2b import CommandExitException

        try:
            sandbox = await self._connect(instance_id)
            result = await sandbox.commands.run(
                self._bounded_shell_command(command),
                cwd=cwd,
                stdin=False,
                timeout=timeout_seconds,
                request_timeout=timeout_seconds,
            )
            return CommandResult(
                stdout=str(result.stdout),
                stderr=str(result.stderr),
                exit_code=int(result.exit_code),
            )
        except CommandExitException as error:
            if int(error.exit_code) == self._OUTPUT_LIMIT_EXIT:
                raise SandboxProviderError(
                    "output_limit_exceeded",
                    status_code=413,
                ) from error
            return CommandResult(
                stdout=str(error.stdout),
                stderr=str(error.stderr),
                exit_code=int(error.exit_code),
            )
        except SandboxProviderError:
            raise
        except Exception as error:
            raise SandboxProviderError("provider_command_failed") from error

    async def _command_stream(
        self,
        instance_id: str,
        *,
        command: str,
        cwd: str,
        timeout_seconds: int,
    ) -> AsyncIterator[CommandChunk]:
        from e2b import CommandExitException

        queue: asyncio.Queue[CommandChunk | Exception | None] = asyncio.Queue(
            maxsize=self._STREAM_QUEUE_SIZE
        )

        async def on_stdout(data: Any) -> None:
            await queue.put(CommandChunk(stream="stdout", data=str(data)))

        async def on_stderr(data: Any) -> None:
            await queue.put(CommandChunk(stream="stderr", data=str(data)))

        async def run() -> None:
            try:
                sandbox = await self._connect(instance_id)
                result = await sandbox.commands.run(
                    self._bounded_shell_command(command),
                    cwd=cwd,
                    stdin=False,
                    on_stdout=on_stdout,
                    on_stderr=on_stderr,
                    timeout=timeout_seconds,
                    request_timeout=timeout_seconds,
                )
                await queue.put(CommandChunk(stream="exit", exit_code=int(result.exit_code)))
            except CommandExitException as error:
                if int(error.exit_code) == self._OUTPUT_LIMIT_EXIT:
                    await queue.put(
                        SandboxProviderError(
                            "output_limit_exceeded",
                            status_code=413,
                        )
                    )
                else:
                    await queue.put(
                        CommandChunk(stream="exit", exit_code=int(error.exit_code))
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await queue.put(error)
            finally:
                if not asyncio.current_task().cancelling():
                    await queue.put(None)

        task = asyncio.create_task(run())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    if isinstance(item, SandboxProviderError):
                        raise item
                    raise SandboxProviderError("provider_command_failed") from item
                yield item
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def stream_command(
        self,
        instance_id: str,
        *,
        command: str,
        cwd: str,
        timeout_seconds: int,
    ) -> AsyncIterator[CommandChunk]:
        return self._command_stream(
            instance_id,
            command=command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )

    async def read_file(
        self,
        instance_id: str,
        *,
        path: str,
        timeout_seconds: int,
        max_bytes: int,
    ) -> bytes:
        try:
            sandbox = await self._connect(instance_id)
            reader = await sandbox.files.read(
                path,
                format="stream",
                request_timeout=timeout_seconds,
                stream_idle_timeout=timeout_seconds,
            )
            chunks: list[bytes] = []
            received = 0
            async with reader:
                async for chunk in reader:
                    received += len(chunk)
                    if received > max_bytes:
                        raise SandboxProviderError(
                            "file_limit_exceeded",
                            status_code=413,
                        )
                    chunks.append(chunk)
            return b"".join(chunks)
        except SandboxProviderError:
            raise
        except Exception as error:
            raise SandboxProviderError("provider_file_read_failed") from error

    async def write_file(
        self,
        instance_id: str,
        *,
        path: str,
        data: bytes,
        timeout_seconds: int,
    ) -> None:
        try:
            sandbox = await self._connect(instance_id)
            await sandbox.files.write(
                path,
                data,
                request_timeout=timeout_seconds,
                use_octet_stream=True,
            )
        except SandboxProviderError:
            raise
        except Exception as error:
            raise SandboxProviderError("provider_file_write_failed") from error

    @staticmethod
    def _pty_callback(queue: asyncio.Queue[Any]):
        overflowed = False

        async def on_data(data: Any) -> None:
            nonlocal overflowed
            if overflowed:
                return
            try:
                view = memoryview(data)
            except TypeError:
                overflowed = True
                failure = SandboxProviderError(
                    "pty_output_frame_invalid",
                    status_code=502,
                )
            else:
                if (
                    view.nbytes
                    > tagentic_config.WORKBENCH_SANDBOX_PTY_MAX_OUTPUT_FRAME_BYTES
                ):
                    overflowed = True
                    failure = SandboxProviderError(
                        "pty_output_frame_exceeded",
                        status_code=413,
                    )
                else:
                    failure = None
            if overflowed:
                while True:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                queue.put_nowait(failure)
                return
            try:
                queue.put_nowait(bytes(view))
            except asyncio.QueueFull:
                overflowed = True
                while True:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                queue.put_nowait(
                    SandboxProviderError(
                        "pty_output_queue_exceeded",
                        status_code=413,
                    )
                )

        return on_data

    async def create_pty(
        self,
        instance_id: str,
        *,
        rows: int,
        cols: int,
        timeout_seconds: int,
        output_queue_frames: int,
    ) -> ProviderPty:
        from e2b import PtySize

        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=output_queue_frames)
        try:
            sandbox = await self._connect(instance_id)
            handle = await sandbox.pty.create(
                PtySize(rows=rows, cols=cols),
                self._pty_callback(queue),
                user="user",
                cwd="/workspace",
                timeout=timeout_seconds,
                request_timeout=tagentic_config.WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS,
            )
            return _TencentPty(sandbox=sandbox, handle=handle, queue=queue)
        except SandboxProviderError:
            raise
        except Exception as error:
            raise SandboxProviderError("provider_pty_create_failed") from error

    async def connect_pty(
        self,
        instance_id: str,
        *,
        pid: int,
        timeout_seconds: int,
        output_queue_frames: int,
    ) -> ProviderPty:
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=output_queue_frames)
        try:
            sandbox = await self._connect(instance_id)
            handle = await sandbox.pty.connect(
                pid,
                self._pty_callback(queue),
                timeout=timeout_seconds,
                request_timeout=tagentic_config.WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS,
            )
            return _TencentPty(sandbox=sandbox, handle=handle, queue=queue)
        except SandboxProviderError:
            raise
        except Exception as error:
            raise SandboxProviderError("provider_pty_connect_failed") from error

    async def kill_pty(self, instance_id: str, *, pid: int) -> bool:
        try:
            sandbox = await self._connect(instance_id)
            return bool(
                await sandbox.pty.kill(
                    pid,
                    request_timeout=tagentic_config.WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS,
                )
            )
        except Exception as error:
            raise SandboxProviderError("provider_pty_kill_failed") from error
