# Workbench control events

The ADP backend consumes revocation and cache-invalidation events emitted by
`claw-control`. Events reduce revocation latency; they do not replace the
database/control-plane authorization checks performed for each request and
during long-lived stream reauthorization.

## Delivery model

The subscriber uses both Redis transports exposed by the control plane:

- `claw:control:events` Pub/Sub provides low-latency delivery.
- `claw:control:events:stream` provides replay after a disconnect or process
  restart.

Each ADP process persists its last consumed Stream ID under
`claw:control:events:cursor:<WORKBENCH_INSTANCE_ID>`. Processed EventKeys use
per-instance TTL markers under
`claw:control:events:processed:<WORKBENCH_INSTANCE_ID>:<sha256(EventKey)>`.
An in-memory bounded EventKey set also suppresses Pub/Sub/Stream races.

The subscriber accepts only `SESSION_REVOKE` and `CACHE_INVALIDATE`. It
validates the exact envelope and event-specific payload before dispatch.
Malformed or unknown events are recorded without logging the raw payload and
are ignored. Listener failure leaves the Stream cursor unchanged so the event
can be retried. Listener implementations must therefore be idempotent.

`CACHE_INVALIDATE` automatically evicts the local `WorkbenchAppResolver`
entry for the supplied `application_id`. `SESSION_REVOKE` and
`CACHE_INVALIDATE` are also exposed to local runtime components through the
listener API:

```python
from core.workbench_control_events import (
    SESSION_REVOKE,
    register_control_event_listener,
)

unregister = register_control_event_listener(SESSION_REVOKE, on_revoke)
# Call unregister() during component shutdown.
```

A durable-Turn listener should scope by `customer_id` and `new_api_user_id`,
then close only local subscriptions/turns whose admitted authorization epoch
is older than the event's `auth_epoch`. This prevents replayed historical
events from closing a newly authorized session. Event callbacks must not
authorize requests or make a failed Redis read permissive.

## Required configuration

The existing `REDIS_HOST`, `REDIS_PORT`, `REDIS_USERNAME`, `REDIS_PASSWORD`,
`REDIS_DB`, and `REDIS_USE_SSL` settings must select the same logical Redis
database used by `claw-control` for these events.

| Variable | Meaning |
| --- | --- |
| `WORKBENCH_INSTANCE_ID` | Stable and unique ID for one concurrently running ADP instance, 1-64 characters (`A-Z`, `a-z`, digits, `.`, `_`, `-`). Required when `WORKBENCH_MODE=true`. |
| `WORKBENCH_CONTROL_EVENT_CHANNEL` | Pub/Sub channel. The Stream name is derived as `<channel>:stream`; it must equal the control-plane setting. |
| `WORKBENCH_CONTROL_EVENT_PROCESSED_TTL_SECONDS` | Retention for per-instance EventKey markers; default 7 days. |
| `WORKBENCH_CONTROL_EVENT_MAX_BACKOFF_SECONDS` | Reconnect exponential-backoff cap; default 30 seconds. |
| `WORKBENCH_CONTROL_EVENT_MAX_BYTES` | Maximum serialized event size; default 64 KiB. |

`WORKBENCH_INSTANCE_ID` is a consumer identity, not a release color. Every
simultaneously running replica needs a different ID. A replica must retain the
same ID across restarts so it can resume its cursor. For example, use
`adp-blue-1`, `adp-blue-2`, and `adp-green-1`; never configure two live
replicas with the same value. Blue/green replacement should transfer an ID
only after the previous owner has stopped.

The Redis ACL needs only the selected database/key scope and the commands used
by this consumer: connection/authentication (and `SELECT` when using a
non-default database), `PING`, `SUBSCRIBE`, `UNSUBSCRIBE`, `GET`, `SET`, and
`XREAD`. Its key pattern must cover `<channel>:stream`, cursor keys, and
processed-marker keys; its Pub/Sub channel pattern must cover the configured
channel. Use Redis TLS and credentials in production. Do not put secrets in the
instance ID, channel, or event metadata.

## Failure behavior and operations

Redis connection failures trigger bounded exponential reconnects. The ADP
server remains available because Redis events are an acceleration path only;
per-request control reauthorization and periodic stream reauthorization remain
the source of truth and fail closed under their own policies. Redis failure
must never cause a cached allow decision to be extended.

The control-plane Stream has finite retention. Alert on sustained subscriber
disconnects so an instance returns before its cursor falls outside retention.
If retention has already removed the cursor's next event, authorization is
still protected by normal control checks, but operators should restart that
instance with a deliberate replay/reconciliation procedure instead of copying
another replica's cursor.

Operational logs contain event type/key, instance ID, cursor entry ID, error
class, and retry delay only. Raw events, payloads, Redis credentials, HMAC
secrets, and exception messages are not logged.

## App migration cutover contract

`APP_MIGRATION_CUTOVER` is the only event allowed to activate an App history
lineage. Its exact payload binds customer, lineage/job, source and target local
ApplicationId, provider AppId, App profile, config version, and target config
fingerprint. The envelope additionally requires `signature_version=1` and an
HMAC-SHA256 made with `WORKBENCH_SERVICE_HMAC_SECRET` over:

`version\nevent_key\ncustomer_id\nevent_type\ncreated_at\nsha256(canonical_payload)`

The canonical payload is UTF-8 JSON with sorted keys and no insignificant
whitespace. Missing or invalid signatures, tuple changes on replay, and
customer mismatches are listener failures: the stream cursor is not advanced
and no lineage is activated. Prepare, verify, rebuild, replan, retry, and
rollback events are not accepted as lineage activation events.
