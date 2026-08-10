from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class SandboxProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        status_code: int = 502,
        retryable: bool = False,
        instance_id: str | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.instance_id = instance_id


@dataclass(frozen=True)
class ProviderInstance:
    instance_id: str
    status: str
    network_mode: str
    auth_mode: str
    expires_at: datetime | None = None
    tool_id: str = ""
    tool_name: str = ""
    persistent: bool = True


@dataclass(frozen=True)
class CodeResult:
    stdout: str
    stderr: str
    results: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int


@dataclass(frozen=True)
class CommandChunk:
    stream: str
    data: str = ""
    exit_code: int | None = None


class ProviderPty(Protocol):
    @property
    def pid(self) -> int: ...

    def output(self) -> AsyncIterator[bytes]: ...

    async def wait(self) -> None: ...

    async def send_input(self, data: bytes) -> None: ...

    async def resize(self, *, rows: int, cols: int) -> None: ...

    async def kill(self) -> bool: ...


class ManagedSandboxProvider(Protocol):
    async def start(
        self,
        *,
        client_token: str,
        timeout_seconds: int,
        metadata: dict[str, str],
    ) -> ProviderInstance: ...

    async def describe(self, instance_id: str) -> ProviderInstance | None: ...

    async def pause(self, instance_id: str) -> ProviderInstance: ...

    async def resume(
        self, instance_id: str, *, timeout_seconds: int
    ) -> ProviderInstance: ...

    async def stop(self, instance_id: str) -> None: ...

    async def execute_code(
        self,
        instance_id: str,
        *,
        code: str,
        language: str,
        timeout_seconds: int,
    ) -> CodeResult: ...

    async def run_command(
        self,
        instance_id: str,
        *,
        command: str,
        cwd: str,
        timeout_seconds: int,
    ) -> CommandResult: ...

    def stream_command(
        self,
        instance_id: str,
        *,
        command: str,
        cwd: str,
        timeout_seconds: int,
    ) -> AsyncIterator[CommandChunk]: ...

    async def read_file(
        self,
        instance_id: str,
        *,
        path: str,
        timeout_seconds: int,
        max_bytes: int,
    ) -> bytes: ...

    async def write_file(
        self,
        instance_id: str,
        *,
        path: str,
        data: bytes,
        timeout_seconds: int,
    ) -> None: ...

    async def create_pty(
        self,
        instance_id: str,
        *,
        rows: int,
        cols: int,
        timeout_seconds: int,
        output_queue_frames: int,
    ) -> ProviderPty: ...

    async def connect_pty(
        self,
        instance_id: str,
        *,
        pid: int,
        timeout_seconds: int,
        output_queue_frames: int,
    ) -> ProviderPty: ...

    async def kill_pty(self, instance_id: str, *, pid: int) -> bool: ...
