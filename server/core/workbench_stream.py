import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from config import tagentic_config
from core.workbench_identity import CoreWorkbenchIdentity
from core.workbench_runtime import RuntimeLease, WorkbenchRuntimeGuard
from util.database import db_connection


class WorkbenchStreamReauthorizationError(RuntimeError):
    """The trusted workbench authorization changed while an SSE stream was open."""


class WorkbenchStreamGuard:
    """Pump an upstream SSE iterator while continuously reauthorizing its owner.

    The upstream ``__anext__`` call and the reauthorization timer are raced, so
    authorization is checked even when the provider is completely silent.  A
    new database session is opened only for each check and is closed before the
    stream resumes.
    """

    @staticmethod
    async def reauthorize(
        *,
        claims: dict[str, Any],
        method: str,
        resource_path: str,
        application_id: str,
        app_profile_id: str,
        config_version: int,
    ) -> None:
        try:
            expires_at = int(claims.get("exp", 0))
        except (TypeError, ValueError) as error:
            raise WorkbenchStreamReauthorizationError(
                "workbench session is no longer valid"
            ) from error
        if expires_at <= int(time.time()):
            raise WorkbenchStreamReauthorizationError(
                "workbench session is no longer valid"
            )

        try:
            async with db_connection() as db:
                _, current_app_context = await CoreWorkbenchIdentity.authorize_session(
                    db,
                    claims,
                    method=method,
                    resource_path=resource_path,
                    supplied_application_id=application_id,
                )
            if (
                current_app_context.app_profile_id != app_profile_id
                or current_app_context.config_version != config_version
            ):
                raise WorkbenchStreamReauthorizationError(
                    "workbench app context changed"
                )
        except asyncio.CancelledError:
            raise
        except WorkbenchStreamReauthorizationError:
            raise
        except Exception as error:
            raise WorkbenchStreamReauthorizationError(
                "workbench authorization was revoked"
            ) from error

    @classmethod
    async def pump(
        cls,
        upstream: AsyncIterator[Any],
        write: Callable[[Any], Awaitable[None]],
        *,
        claims: dict[str, Any],
        method: str,
        resource_path: str,
        application_id: str,
        app_profile_id: str,
        config_version: int,
        lease: RuntimeLease | None,
        max_runtime_seconds: int,
        reauthorization_interval_seconds: int | None = None,
        offline_reauthorize: Callable[[], Awaitable[None]] | None = None,
        drain_after_reauthorization_failure: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        interval = (
            reauthorization_interval_seconds
            if reauthorization_interval_seconds is not None
            else tagentic_config.WORKBENCH_STREAM_REAUTH_SECONDS
        )
        if interval <= 0:
            raise WorkbenchStreamReauthorizationError(
                "workbench stream reauthorization interval is invalid"
            )

        next_item = asyncio.create_task(anext(upstream))
        next_reauthorization = monotonic() + interval
        try:
            async with asyncio.timeout(max_runtime_seconds):
                while True:
                    wait_seconds = max(0.0, next_reauthorization - monotonic())
                    completed, _ = await asyncio.wait(
                        {next_item},
                        timeout=wait_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    reauthorization_due = monotonic() >= next_reauthorization
                    if reauthorization_due:
                        try:
                            if offline_reauthorize is not None:
                                await offline_reauthorize()
                            else:
                                await cls.reauthorize(
                                    claims=claims,
                                    method=method,
                                    resource_path=resource_path,
                                    application_id=application_id,
                                    app_profile_id=app_profile_id,
                                    config_version=config_version,
                                )
                        except Exception:
                            if not drain_after_reauthorization_failure:
                                raise
                            # The already-accepted provider call has no verified
                            # cancellation contract.  Stop authorizing browser
                            # subscribers separately, but keep draining its output
                            # to a durable terminal state within the original lease.
                            next_reauthorization = float("inf")
                        else:
                            next_reauthorization = monotonic() + interval

                    if next_item in completed:
                        try:
                            data = next_item.result()
                        except StopAsyncIteration:
                            return
                        await write(data)
                        next_item = asyncio.create_task(anext(upstream))
        finally:
            if not next_item.done():
                next_item.cancel()
            await asyncio.gather(next_item, return_exceptions=True)
            try:
                await upstream.aclose()
            finally:
                await WorkbenchRuntimeGuard.release(lease)
