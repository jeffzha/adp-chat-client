import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from config import tagentic_config

from core.workbench_metrics import WORKBENCH_METRICS


SESSION_REVOKE = "SESSION_REVOKE"
CACHE_INVALIDATE = "CACHE_INVALIDATE"
APP_MIGRATION_CUTOVER = "APP_MIGRATION_CUTOVER"
SUPPORTED_CONTROL_EVENTS = frozenset({SESSION_REVOKE, CACHE_INVALIDATE, APP_MIGRATION_CUTOVER})


class WorkbenchControlEventError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WorkbenchControlEvent:
    event_key: str
    event_type: str
    customer_id: int | None
    payload: dict[str, Any]
    created_at: datetime


ControlEventListener = Callable[
    [WorkbenchControlEvent],
    Awaitable[None] | None,
]


class WorkbenchControlEventBus:
    def __init__(self):
        self._listeners: dict[str, list[ControlEventListener]] = {
            SESSION_REVOKE: [],
            CACHE_INVALIDATE: [],
            APP_MIGRATION_CUTOVER: [],
        }

    def register(
        self,
        event_type: str,
        listener: ControlEventListener,
    ) -> Callable[[], None]:
        if event_type not in SUPPORTED_CONTROL_EVENTS or not callable(listener):
            raise ValueError("unsupported workbench control event listener")
        self._listeners[event_type].append(listener)

        def unregister() -> None:
            try:
                self._listeners[event_type].remove(listener)
            except ValueError:
                pass

        return unregister

    async def notify(self, event: WorkbenchControlEvent) -> None:
        failures = 0
        for listener in tuple(self._listeners[event.event_type]):
            try:
                result = listener(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as error:  # pylint: disable=broad-except
                WORKBENCH_METRICS.inc(
                    "workbench_control_event_failures_total",
                    stage="consume",
                )
                failures += 1
                # Listener exceptions may contain provider/user content. Keep the
                # operational signal without copying exception text or a traceback
                # into logs.
                logging.warning(
                    "[workbench_control_events] listener failed type=%s key=%s error_type=%s",
                    event.event_type,
                    event.event_key,
                    type(error).__name__,
                )
        if failures:
            raise WorkbenchControlEventError("listener_failed")


_event_bus = WorkbenchControlEventBus()


def register_control_event_listener(
    event_type: str,
    listener: ControlEventListener,
) -> Callable[[], None]:
    """Register an in-process revocation listener and return its unsubscribe hook."""

    return _event_bus.register(event_type, listener)


def invalidate_workbench_app_cache(event: WorkbenchControlEvent) -> None:
    """Evict the provider application affected by a validated cache event."""

    from core.workbench_app_resolver import WorkbenchAppResolver

    WorkbenchAppResolver.revoke(event.payload["application_id"])


class WorkbenchControlEventSubscriber:
    _INSTANCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    _STREAM_ID_PATTERN = re.compile(r"^\d+-\d+$")
    _MAX_LOCAL_EVENT_KEYS = 10_000
    _MAX_INT64 = (1 << 63) - 1

    def __init__(
        self,
        *,
        redis_factory: Callable[[], Any],
        instance_id: str,
        channel: str,
        processed_ttl_seconds: int,
        max_backoff_seconds: int,
        max_event_bytes: int,
        event_bus: WorkbenchControlEventBus | None = None,
    ):
        instance_id = str(instance_id or "").strip()
        channel = str(channel or "").strip()
        if not self._INSTANCE_PATTERN.fullmatch(instance_id):
            raise ValueError("WORKBENCH_INSTANCE_ID must be a stable 1-64 character deployment identifier")
        if not channel or len(channel) > 128 or any(char.isspace() or ord(char) < 32 for char in channel):
            raise ValueError("WORKBENCH_CONTROL_EVENT_CHANNEL is invalid")
        if processed_ttl_seconds <= 0 or max_backoff_seconds <= 0 or max_event_bytes <= 0:
            raise ValueError("workbench control event limits must be positive")
        self._redis_factory = redis_factory
        self.instance_id = instance_id
        self.channel = channel
        self.stream = f"{channel}:stream"
        self.cursor_key = f"{channel}:cursor:{instance_id}"
        self._processed_prefix = f"{channel}:processed:{instance_id}:"
        self._processed_ttl_seconds = processed_ttl_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._max_event_bytes = max_event_bytes
        self._event_bus = event_bus or _event_bus
        self._stop = asyncio.Event()
        self._dispatch_lock = asyncio.Lock()
        self._processed_local: OrderedDict[str, None] = OrderedDict()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._consume_connection()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:  # pylint: disable=broad-except
                attempt += 1
                delay = min(self._max_backoff_seconds, 2 ** min(attempt - 1, 8))
                logging.warning(
                    "[workbench_control_events] Redis connection unavailable; retrying "
                    "instance=%s delay=%s error_type=%s",
                    self.instance_id,
                    delay,
                    type(error).__name__,
                )
                await self._wait_backoff(delay)

    async def _wait_backoff(self, delay: int) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def _consume_connection(self) -> None:
        client = self._redis_factory()
        pubsub = None
        tasks: set[asyncio.Task] = set()
        try:
            await client.ping()
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            await pubsub.subscribe(self.channel)
            cursor = await client.get(self.cursor_key)
            cursor = str(cursor or "0-0")
            if not self._STREAM_ID_PATTERN.fullmatch(cursor):
                logging.warning(
                    "[workbench_control_events] invalid persisted cursor; replaying stream instance=%s",
                    self.instance_id,
                )
                cursor = "0-0"

            tasks = {
                asyncio.create_task(self._pubsub_loop(client, pubsub)),
                asyncio.create_task(self._stream_loop(client, cursor)),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(self.channel)
                    await pubsub.aclose()
                except Exception:  # pylint: disable=broad-except
                    pass
            try:
                await client.aclose()
            except Exception:  # pylint: disable=broad-except
                pass

    async def _pubsub_loop(self, client: Any, pubsub: Any) -> None:
        while not self._stop.is_set():
            message = await pubsub.get_message(timeout=1.0)
            if not message:
                continue
            if message.get("type") != "message":
                continue
            await self.handle_serialized_event(client, message.get("data"))

    async def _stream_loop(self, client: Any, cursor: str) -> None:
        while not self._stop.is_set():
            cursor = await self.stream_once(client, cursor, block_ms=1000)

    async def stream_once(self, client: Any, cursor: str, *, block_ms: int = 0) -> str:
        entries = await client.xread(
            {self.stream: cursor},
            count=100,
            block=block_ms,
        )
        for stream_name, messages in entries or []:
            if str(stream_name) != self.stream:
                raise WorkbenchControlEventError("unexpected_stream")
            for stream_id, fields in messages:
                stream_id = str(stream_id)
                if not self._STREAM_ID_PATTERN.fullmatch(stream_id):
                    raise WorkbenchControlEventError("invalid_stream_id")
                advance = True
                if not isinstance(fields, dict) or set(fields) != {"event_key", "event"}:
                    logging.warning(
                        "[workbench_control_events] malformed stream entry ignored instance=%s id=%s",
                        self.instance_id,
                        stream_id,
                    )
                else:
                    event = self.parse_event(fields["event"])
                    if event is None:
                        pass
                    elif str(fields["event_key"]) != event.event_key:
                        logging.warning(
                            "[workbench_control_events] stream EventKey mismatch ignored instance=%s id=%s",
                            self.instance_id,
                            stream_id,
                        )
                    else:
                        try:
                            await self.dispatch(client, event)
                        except WorkbenchControlEventError:
                            advance = False
                if not advance:
                    return cursor
                await client.set(self.cursor_key, stream_id)
                cursor = stream_id
        return cursor

    async def handle_serialized_event(self, client: Any, raw: Any) -> None:
        event = self.parse_event(raw)
        if event is not None:
            await self.dispatch(client, event)

    def parse_event(self, raw: Any) -> WorkbenchControlEvent | None:
        if isinstance(raw, bytes):
            if len(raw) > self._max_event_bytes:
                logging.warning("[workbench_control_events] oversized event ignored")
                return None
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                logging.warning("[workbench_control_events] non-UTF8 event ignored")
                return None
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > self._max_event_bytes:
            logging.warning("[workbench_control_events] invalid event encoding ignored")
            return None
        try:
            decoded = json.loads(raw, object_pairs_hook=self._reject_duplicate_keys)
            decoded = self._verify_cutover_signature(decoded)
            event = self._validate_event(decoded)
        except (ValueError, TypeError, RecursionError, WorkbenchControlEventError) as error:
            reason = error.code if isinstance(error, WorkbenchControlEventError) else "invalid_json"
            logging.warning(
                "[workbench_control_events] invalid event ignored reason=%s",
                reason,
            )
            return None
        if event.event_type not in SUPPORTED_CONTROL_EVENTS:
            logging.warning(
                "[workbench_control_events] unknown event ignored type=%s key=%s",
                event.event_type,
                event.event_key,
            )
            return None
        return event

    async def dispatch(self, client: Any, event: WorkbenchControlEvent) -> None:
        async with self._dispatch_lock:
            if event.event_key in self._processed_local:
                return
            marker = self._processed_marker(event.event_key)
            if await client.get(marker):
                self._remember(event.event_key)
                return
            now = datetime.now(event.created_at.tzinfo)
            WORKBENCH_METRICS.set(
                "workbench_control_event_lag_seconds",
                max(0.0, (now - event.created_at).total_seconds()),
                stage="consume",
            )
            try:
                await self._event_bus.notify(event)
            except WorkbenchControlEventError:
                WORKBENCH_METRICS.inc(
                    "workbench_control_event_failures_total",
                    stage="dispatch",
                )
                raise
            await client.set(marker, "1", ex=self._processed_ttl_seconds)
            self._remember(event.event_key)

    def _remember(self, event_key: str) -> None:
        self._processed_local[event_key] = None
        self._processed_local.move_to_end(event_key)
        while len(self._processed_local) > self._MAX_LOCAL_EVENT_KEYS:
            self._processed_local.popitem(last=False)

    def _processed_marker(self, event_key: str) -> str:
        digest = hashlib.sha256(event_key.encode("utf-8")).hexdigest()
        return f"{self._processed_prefix}{digest}"

    @staticmethod
    def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise WorkbenchControlEventError("duplicate_json_key")
            result[key] = value
        return result

    @classmethod
    def _validate_event(cls, value: Any) -> WorkbenchControlEvent:
        if not isinstance(value, dict):
            raise WorkbenchControlEventError("invalid_envelope")
        allowed = {"event_key", "customer_id", "event_type", "payload", "created_at"}
        if set(value).difference(allowed) or not {"event_key", "event_type", "payload", "created_at"}.issubset(value):
            raise WorkbenchControlEventError("invalid_envelope_fields")
        event_key = cls._bounded_string(value["event_key"], 160, "invalid_event_key")
        event_type = cls._bounded_string(value["event_type"], 64, "invalid_event_type")
        payload = value["payload"]
        if not isinstance(payload, dict):
            raise WorkbenchControlEventError("invalid_payload")
        customer_id = cls._positive_optional_int(value.get("customer_id"), "invalid_customer_id")
        created_at_text = cls._bounded_string(value["created_at"], 64, "invalid_created_at")
        try:
            created_at = datetime.fromisoformat(created_at_text.replace("Z", "+00:00"))
        except ValueError as error:
            raise WorkbenchControlEventError("invalid_created_at") from error
        if created_at.tzinfo is None:
            raise WorkbenchControlEventError("invalid_created_at")

        if event_type == SESSION_REVOKE:
            payload = cls._validate_session_revoke(payload, customer_id)
        elif event_type == CACHE_INVALIDATE:
            payload = cls._validate_cache_invalidate(payload, customer_id)
        elif event_type == APP_MIGRATION_CUTOVER:
            payload = cls._validate_app_migration_cutover(payload, customer_id)
        return WorkbenchControlEvent(event_key, event_type, customer_id, payload, created_at)

    @classmethod
    def _verify_cutover_signature(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("event_type") != APP_MIGRATION_CUTOVER:
            return value
        allowed = {
            "event_key", "customer_id", "event_type", "payload", "created_at",
            "signature_version", "signature",
        }
        if set(value) != allowed or value.get("signature_version") != "1":
            raise WorkbenchControlEventError("invalid_cutover_signature")
        secret = tagentic_config.WORKBENCH_SERVICE_HMAC_SECRET
        signature = value.get("signature")
        customer_id = value.get("customer_id")
        payload = value.get("payload")
        if (
            len(secret) < 32
            or not isinstance(signature, str)
            or len(signature) != 64
            or isinstance(customer_id, bool)
            or not isinstance(customer_id, int)
            or not isinstance(payload, dict)
        ):
            raise WorkbenchControlEventError("invalid_cutover_signature")
        payload_bytes = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        canonical = "\n".join(
            (
                "1",
                str(value.get("event_key", "")),
                str(customer_id),
                APP_MIGRATION_CUTOVER,
                str(value.get("created_at", "")),
                hashlib.sha256(payload_bytes).hexdigest(),
            )
        )
        expected = hmac.new(
            secret.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature.lower(), expected):
            raise WorkbenchControlEventError("invalid_cutover_signature")
        return {
            key: item
            for key, item in value.items()
            if key not in {"signature_version", "signature"}
        }

    @classmethod
    def _validate_app_migration_cutover(
        cls,
        payload: dict[str, Any],
        envelope_customer_id: int | None,
    ) -> dict[str, Any]:
        required = {
            "customer_id", "lineage_id", "source_application_id",
            "source_provider_app_id",
            "source_app_profile_id", "source_config_version",
            "target_application_id", "target_app_profile_id",
            "target_provider_app_id",
            "target_config_version", "migration_job_id",
            "migration_config_fingerprint",
        }
        if set(payload) != required:
            raise WorkbenchControlEventError("invalid_app_migration_cutover_payload")
        customer_id = cls._positive_int(payload["customer_id"], "invalid_customer_id")
        if envelope_customer_id != customer_id:
            raise WorkbenchControlEventError("customer_scope_mismatch")
        return {
            "customer_id": customer_id,
            "lineage_id": cls._bounded_string(payload["lineage_id"], 64, "invalid_lineage_id"),
            "source_application_id": cls._bounded_string(payload["source_application_id"], 64, "invalid_source_application_id"),
            "source_provider_app_id": cls._bounded_string(payload["source_provider_app_id"], 128, "invalid_source_provider_app_id"),
            "source_app_profile_id": cls._positive_int(payload["source_app_profile_id"], "invalid_source_app_profile_id"),
            "source_config_version": cls._positive_int(payload["source_config_version"], "invalid_source_config_version"),
            "target_application_id": cls._bounded_string(payload["target_application_id"], 64, "invalid_target_application_id"),
            "target_provider_app_id": cls._bounded_string(payload["target_provider_app_id"], 128, "invalid_target_provider_app_id"),
            "target_app_profile_id": cls._positive_int(payload["target_app_profile_id"], "invalid_target_app_profile_id"),
            "target_config_version": cls._positive_int(payload["target_config_version"], "invalid_target_config_version"),
            "migration_job_id": cls._bounded_string(payload["migration_job_id"], 64, "invalid_migration_job_id"),
            "migration_config_fingerprint": cls._bounded_string(payload["migration_config_fingerprint"], 80, "invalid_migration_config_fingerprint"),
        }

    @classmethod
    def _validate_session_revoke(
        cls,
        payload: dict[str, Any],
        envelope_customer_id: int | None,
    ) -> dict[str, Any]:
        allowed = {"binding_id", "customer_id", "new_api_user_id", "auth_epoch"}
        if set(payload).difference(allowed) or not {"new_api_user_id", "auth_epoch"}.issubset(payload):
            raise WorkbenchControlEventError("invalid_session_revoke_payload")
        payload_customer_id = cls._positive_optional_int(payload.get("customer_id"), "invalid_customer_id")
        customer_id = payload_customer_id or envelope_customer_id
        if customer_id is None or (
            payload_customer_id is not None
            and envelope_customer_id is not None
            and payload_customer_id != envelope_customer_id
        ):
            raise WorkbenchControlEventError("customer_scope_mismatch")
        new_api_user_id = cls._positive_int(payload["new_api_user_id"], "invalid_new_api_user_id")
        auth_epoch = cls._non_negative_int(payload["auth_epoch"], "invalid_auth_epoch")
        result: dict[str, Any] = {
            "customer_id": customer_id,
            "new_api_user_id": new_api_user_id,
            "auth_epoch": auth_epoch,
        }
        if "binding_id" in payload:
            result["binding_id"] = cls._bounded_string(payload["binding_id"], 64, "invalid_binding_id")
        return result

    @classmethod
    def _validate_cache_invalidate(
        cls,
        payload: dict[str, Any],
        envelope_customer_id: int | None,
    ) -> dict[str, Any]:
        allowed = {
            "customer_id",
            "customer_app_id",
            "application_id",
            "config_version",
            "auth_epoch",
            "status",
        }
        required = {"customer_id", "customer_app_id", "application_id", "auth_epoch"}
        if set(payload).difference(allowed) or not required.issubset(payload):
            raise WorkbenchControlEventError("invalid_cache_invalidate_payload")
        customer_id = cls._positive_int(payload["customer_id"], "invalid_customer_id")
        if envelope_customer_id is not None and envelope_customer_id != customer_id:
            raise WorkbenchControlEventError("customer_scope_mismatch")
        result: dict[str, Any] = {
            "customer_id": customer_id,
            "customer_app_id": cls._positive_int(payload["customer_app_id"], "invalid_customer_app_id"),
            "application_id": cls._bounded_string(payload["application_id"], 128, "invalid_application_id"),
            "auth_epoch": cls._non_negative_int(payload["auth_epoch"], "invalid_auth_epoch"),
        }
        if "config_version" in payload:
            result["config_version"] = cls._positive_int(payload["config_version"], "invalid_config_version")
        if "status" in payload:
            status = cls._bounded_string(payload["status"], 24, "invalid_status")
            if status not in {"draft", "verified", "active", "suspended", "disabled", "archived"}:
                raise WorkbenchControlEventError("invalid_status")
            result["status"] = status
        return result

    @staticmethod
    def _bounded_string(value: Any, maximum: int, code: str) -> str:
        if not isinstance(value, str) or not value or len(value) > maximum or value.strip() != value:
            raise WorkbenchControlEventError(code)
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise WorkbenchControlEventError(code)
        return value

    @staticmethod
    def _positive_int(value: Any, code: str) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            or value > WorkbenchControlEventSubscriber._MAX_INT64
        ):
            raise WorkbenchControlEventError(code)
        return value

    @classmethod
    def _positive_optional_int(cls, value: Any, code: str) -> int | None:
        if value is None:
            return None
        return cls._positive_int(value, code)

    @staticmethod
    def _non_negative_int(value: Any, code: str) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > WorkbenchControlEventSubscriber._MAX_INT64
        ):
            raise WorkbenchControlEventError(code)
        return value
