import json
import hashlib
import hmac
from unittest.mock import AsyncMock

import pytest

from config import tagentic_config

from core.workbench_control_events import (
    APP_MIGRATION_CUTOVER,
    CACHE_INVALIDATE,
    SESSION_REVOKE,
    WorkbenchControlEventBus,
    WorkbenchControlEventSubscriber,
    invalidate_workbench_app_cache,
)


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.stream_entries = []
        self.set_calls = []

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **kwargs):
        self.values[key] = value
        self.set_calls.append((key, value, kwargs))
        return True

    async def xread(self, streams, *, count, block):
        del count, block
        if not self.stream_entries:
            return []
        stream = next(iter(streams))
        entries, self.stream_entries = self.stream_entries, []
        return [(stream, entries)]


def _subscriber(*, instance_id="adp-blue-1", event_bus=None):
    return WorkbenchControlEventSubscriber(
        redis_factory=FakeRedis,
        instance_id=instance_id,
        channel="claw:control:events",
        processed_ttl_seconds=604800,
        max_backoff_seconds=30,
        max_event_bytes=65536,
        event_bus=event_bus,
    )


def _event(event_type=SESSION_REVOKE, event_key="member-revoke:7:9:4"):
    if event_type == SESSION_REVOKE:
        payload = {
            "customer_id": 7,
            "new_api_user_id": 9,
            "auth_epoch": 4,
        }
    elif event_type == CACHE_INVALIDATE:
        payload = {
            "customer_id": 7,
            "customer_app_id": 17,
            "application_id": "provider-app-7",
            "config_version": 4,
            "auth_epoch": 5,
        }
    else:
        payload = {"future": True}
    return json.dumps(
        {
            "event_key": event_key,
            "customer_id": 7,
            "event_type": event_type,
            "payload": payload,
            "created_at": "2026-08-09T03:00:00Z",
        },
        separators=(",", ":"),
    )


def _signed_cutover(secret):
    payload = {
        "customer_id": 7,
        "lineage_id": "lin_1",
        "source_application_id": "source-app",
        "source_provider_app_id": "source-provider-app",
        "source_app_profile_id": 11,
        "source_config_version": 3,
        "target_application_id": "target-app",
        "target_provider_app_id": "target-provider-app",
        "target_app_profile_id": 22,
        "target_config_version": 4,
        "migration_job_id": "mig_1",
        "migration_config_fingerprint": "sha256:target",
    }
    event = {
        "event_key": "app-migration-cutover:11:22:33",
        "customer_id": 7,
        "event_type": APP_MIGRATION_CUTOVER,
        "payload": payload,
        "created_at": "2026-08-09T03:00:00Z",
        "signature_version": "1",
    }
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    canonical = "\n".join(
        (
            "1",
            event["event_key"],
            "7",
            APP_MIGRATION_CUTOVER,
            event["created_at"],
            hashlib.sha256(payload_bytes).hexdigest(),
        )
    )
    event["signature"] = hmac.new(
        secret.encode(),
        canonical.encode(),
        hashlib.sha256,
    ).hexdigest()
    return json.dumps(event, separators=(",", ":"))


def test_strict_parser_accepts_only_known_payload_contracts(caplog):
    subscriber = _subscriber()

    revoked = subscriber.parse_event(_event())
    invalidated = subscriber.parse_event(_event(CACHE_INVALIDATE, "app:17:epoch:5"))

    assert revoked.event_type == SESSION_REVOKE
    assert revoked.payload == {
        "customer_id": 7,
        "new_api_user_id": 9,
        "auth_epoch": 4,
    }
    assert invalidated.payload["application_id"] == "provider-app-7"

    bad = json.loads(_event())
    bad["payload"]["secret"] = "must-not-be-logged"
    assert subscriber.parse_event(json.dumps(bad)) is None
    assert "must-not-be-logged" not in caplog.text

    duplicate_key = _event().replace(
        '"event_type":"SESSION_REVOKE"',
        '"event_type":"SESSION_REVOKE","event_type":"CACHE_INVALIDATE"',
    )
    assert subscriber.parse_event(duplicate_key) is None

    oversized_id = json.loads(_event())
    oversized_id["payload"]["new_api_user_id"] = 1 << 63
    assert subscriber.parse_event(json.dumps(oversized_id)) is None


def test_unknown_event_is_ignored_without_notifying_listener(caplog):
    subscriber = _subscriber()

    assert subscriber.parse_event(_event("FUTURE_EVENT", "future:1")) is None
    assert "unknown event ignored" in caplog.text


def test_migration_lineage_requires_exact_signed_cutover(monkeypatch):
    secret = "migration-event-secret-0123456789abcdef"
    monkeypatch.setattr(tagentic_config, "WORKBENCH_SERVICE_HMAC_SECRET", secret)
    subscriber = _subscriber()

    event = subscriber.parse_event(_signed_cutover(secret))
    assert event.event_type == APP_MIGRATION_CUTOVER
    assert event.payload["source_application_id"] == "source-app"

    unsigned = json.loads(_signed_cutover(secret))
    unsigned.pop("signature")
    assert subscriber.parse_event(json.dumps(unsigned)) is None

    tampered = json.loads(_signed_cutover(secret))
    tampered["payload"]["target_app_profile_id"] = 23
    assert subscriber.parse_event(json.dumps(tampered)) is None


@pytest.mark.asyncio
async def test_event_key_is_idempotent_for_pubsub_and_stream_replay():
    bus = WorkbenchControlEventBus()
    callback = AsyncMock()
    bus.register(SESSION_REVOKE, callback)
    subscriber = _subscriber(event_bus=bus)
    client = FakeRedis()
    raw = _event()

    await subscriber.handle_serialized_event(client, raw)
    await subscriber.handle_serialized_event(client, raw)
    client.stream_entries = [
        (
            "1786244400000-0",
            {"event_key": "member-revoke:7:9:4", "event": raw},
        )
    ]
    cursor = await subscriber.stream_once(client, "0-0")

    assert cursor == "1786244400000-0"
    callback.assert_awaited_once()
    assert client.values[subscriber.cursor_key] == cursor


@pytest.mark.asyncio
async def test_stream_supplements_pubsub_gap_and_persists_isolated_cursor():
    first_bus = WorkbenchControlEventBus()
    second_bus = WorkbenchControlEventBus()
    first_callback = AsyncMock()
    second_callback = AsyncMock()
    first_bus.register(CACHE_INVALIDATE, first_callback)
    second_bus.register(CACHE_INVALIDATE, second_callback)
    first = _subscriber(instance_id="adp-blue-1", event_bus=first_bus)
    second = _subscriber(instance_id="adp-green-1", event_bus=second_bus)
    raw = _event(CACHE_INVALIDATE, "app:17:epoch:5")
    client = FakeRedis()

    client.stream_entries = [
        ("1786244400000-1", {"event_key": "app:17:epoch:5", "event": raw})
    ]
    assert await first.stream_once(client, "0-0") == "1786244400000-1"
    client.stream_entries = [
        ("1786244400000-1", {"event_key": "app:17:epoch:5", "event": raw})
    ]
    assert await second.stream_once(client, "0-0") == "1786244400000-1"

    first_callback.assert_awaited_once()
    second_callback.assert_awaited_once()
    assert first.cursor_key != second.cursor_key
    assert client.values[first.cursor_key] == "1786244400000-1"
    assert client.values[second.cursor_key] == "1786244400000-1"


@pytest.mark.asyncio
async def test_bad_stream_message_advances_cursor_but_listener_failure_does_not():
    bus = WorkbenchControlEventBus()
    callback = AsyncMock(side_effect=RuntimeError("callback failed"))
    bus.register(SESSION_REVOKE, callback)
    subscriber = _subscriber(event_bus=bus)
    client = FakeRedis()

    client.stream_entries = [
        ("1786244400000-2", {"unexpected": "bad"}),
    ]
    assert await subscriber.stream_once(client, "0-0") == "1786244400000-2"

    client.stream_entries = [
        (
            "1786244400000-3",
            {"event_key": "member-revoke:7:9:4", "event": _event()},
        ),
    ]
    assert await subscriber.stream_once(client, "1786244400000-2") == "1786244400000-2"
    assert client.values[subscriber.cursor_key] == "1786244400000-2"
    assert not any(key.startswith(subscriber._processed_prefix) for key in client.values)


@pytest.mark.asyncio
async def test_listener_failure_does_not_log_exception_content(caplog):
    bus = WorkbenchControlEventBus()
    bus.register(
        SESSION_REVOKE,
        AsyncMock(side_effect=RuntimeError("sensitive-listener-detail")),
    )
    subscriber = _subscriber(event_bus=bus)
    client = FakeRedis()
    client.stream_entries = [
        (
            "1786244400000-4",
            {"event_key": "member-revoke:7:9:4", "event": _event()},
        )
    ]

    await subscriber.stream_once(
        client,
        "0-0",
    )

    assert "listener failed" in caplog.text
    assert "sensitive-listener-detail" not in caplog.text


@pytest.mark.asyncio
async def test_listener_registration_returns_unsubscribe_callback():
    bus = WorkbenchControlEventBus()
    callback = AsyncMock()
    unsubscribe = bus.register(SESSION_REVOKE, callback)
    subscriber = _subscriber(event_bus=bus)
    client = FakeRedis()

    await subscriber.handle_serialized_event(client, _event(event_key="revoke:first"))
    unsubscribe()
    await subscriber.handle_serialized_event(client, _event(event_key="revoke:second"))

    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_reconnect_uses_exponential_backoff(monkeypatch):
    subscriber = _subscriber()
    calls = 0
    delays = []

    async def consume():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("Redis unavailable")
        subscriber.stop()

    async def wait_backoff(delay):
        delays.append(delay)

    monkeypatch.setattr(subscriber, "_consume_connection", consume)
    monkeypatch.setattr(subscriber, "_wait_backoff", wait_backoff)

    await subscriber.run()

    assert calls == 2
    assert delays == [1]


def test_instance_id_is_required_and_defines_cursor_scope():
    with pytest.raises(ValueError, match="WORKBENCH_INSTANCE_ID"):
        _subscriber(instance_id="")
    assert _subscriber(instance_id="adp-blue-1").cursor_key.endswith(":adp-blue-1")
    assert _subscriber(instance_id="adp-green-1").cursor_key.endswith(":adp-green-1")


def test_cache_invalidation_listener_evicts_application(monkeypatch):
    revoked = []
    monkeypatch.setattr(
        "core.workbench_app_resolver.WorkbenchAppResolver.revoke",
        revoked.append,
    )
    event = _subscriber().parse_event(
        _event(CACHE_INVALIDATE, "app:17:epoch:5"),
    )

    invalidate_workbench_app_cache(event)

    assert revoked == ["provider-app-7"]
