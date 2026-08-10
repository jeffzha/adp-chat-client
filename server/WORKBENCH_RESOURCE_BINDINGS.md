# Workbench resource ownership reports

In workbench mode ADP remains the operational owner of its local records, while claw-control keeps a strict audit and reverse-ownership mirror. A provider success never makes a browser-supplied identifier authoritative.

## Lifecycle

1. ADP confirms the local shadow account or provider resource and writes its local ownership record.
2. In the same database transaction, ADP inserts one `workbench_resource_outbox` row with a generated stable `EventId` and `SourceVersion=1`.
3. After commit, ADP calls `POST /api/internal/workbench/resources/bind` through the HMAC v2 control client.
4. A valid response marks the event `delivered`. A transient failure becomes `retry` with bounded exponential backoff. A crash after the control call but before the local acknowledgement replays the same event safely.
5. A permanent scope or payload rejection becomes `rejected`; the request fails closed where it is still synchronous. Pending or failed reporting never broadens local ownership queries.

The background worker is enabled only with `WORKBENCH_MODE=true`. `WORKBENCH_RESOURCE_OUTBOX_INTERVAL_SECONDS` controls its polling interval and `WORKBENCH_RESOURCE_OUTBOX_BATCH_SIZE` bounds each batch. Blue and green instances use a database lease; stale `delivering` rows are reclaimed and control-side source-event idempotency handles the final crash window.

## Required graph and scope

The normal graph is `account -> agent -> conversation -> workspace -> file`,
with exactly one distinct Workspace per Conversation. A file uploaded before a
Conversation/Workspace exists attaches to the owned account. All parent and
child rows must match the exact binding, canonical subject, customer, customer
App, provider App ID, and active App config version. Resource IDs are globally
unique; a different customer or identity cannot claim an existing identifier.

ADP sends:

```json
{
  "binding_id": "binding-...",
  "canonical_subject": "napi:prod:customer:7:user:9",
  "customer_id": 7,
  "application_id": "provider-app-7",
  "app_profile_id": 17,
  "config_version": 4,
  "resource_type": "conversation",
  "resource_id": "8f0e...",
  "parent_resource_type": "agent",
  "parent_resource_id": "agent-...",
  "source_event_id": "wre_...",
  "source_version": 1
}
```

The service name is taken only from the authenticated `X-Workbench-Service` envelope. No customer/user/App scope in the body can create or update those control-plane records; the endpoint only verifies them against existing active records and inserts the resource mirror.

## Suspended App first-entry rule

A suspended App can issue read-only sessions so already-provisioned members can
read their owned history. It cannot provision a new ADP shadow account or bind
new resources. Consequently, a member who has never completed workbench SSO
cannot enter for the first time while the App is suspended; the account binding
fails closed. An administrator must restore the App to `active`, allow the first
SSO and ownership report to complete, and only then may a later suspension
retain read-only access. Do not relax the resource binding service to accept all
resources from a suspended App: that would turn a read-only state into a new
resource-creation path.
