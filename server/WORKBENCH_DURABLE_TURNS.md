# Durable workbench Turns

Workbench chat execution is detached from the browser SSE subscription. A
browser disconnect removes only that subscriber; it does not cancel or resubmit
the provider Turn.

## Idempotent submission

`POST /chat/message` accepts `ClientRequestId` as a UUID. The bundled client
generates one UUID per user submission and reuses that value for the lifetime of
the request. The server stores a SHA-256 digest of the canonical logical request
and enforces a unique key over:

`BindingId + ApplicationId + ClientRequestId`

The same key and digest subscribes to the existing Turn. The same key with a
different digest returns `409`. A terminal or `provider_unknown` Turn is never
automatically submitted again.

## Execution and replay

The process owns a background task that independently acquires the database
runtime lease, verifies the trusted Agent limits, opens the upstream iterator,
and persists ordered SSE frames. `WorkbenchTurnEvent.Sequence` is exposed as the
SSE `id`. A subscriber reconnects with:

`GET /chat/turn/events?TurnId=<turn-id>`

and `Last-Event-ID`. Ownership is checked against account, customer binding, and
application. A foreign Turn is returned as `404`.

Persistence is bounded to 2,000 frames, 256 KiB per frame, and 16 MiB per Turn.
One frame is reserved for the terminal status. Reaching a bound stops local
consumption and records a failure instead of silently dropping output.

Open background Turns and replay subscribers are reauthorized on the configured
`WORKBENCH_STREAM_REAUTH_SECONDS` interval. A matching `SESSION_REVOKE` or
`CACHE_INVALIDATE` control event also triggers immediate trusted-control
reauthorization. Event `auth_epoch` values are scoped to the emitting system and
are deliberately not compared with the admitted workbench session epoch. The
effective identity and application context returned by reauthorization decide
whether the local provider iterator/subscription remains open. Closing the local
iterator does not assert that Tencent cancelled a provider-side job.

`POST /chat/turn/cancel` persists an owned, idempotent `cancel_requested`
intent and emits it into the replay sequence. The browser then closes only its
subscription. The background task deliberately keeps consuming and persisting
the provider stream because the currently verified Tencent contract has no
provider-cancel operation. A later normal provider completion therefore changes
the Turn to `completed` while the cancellation row remains an unconfirmed
historical intent. `cancel_confirmed` is terminal and can be written only through
the server-side confirmation primitive with a SHA-256 of official provider
evidence; no public route guesses or manufactures that evidence.

## State and restart rules

States are `submitted`, `running`, `cancel_requested`, `completed`,
`failed_before_accept`, `failed_after_accept`, `cancel_confirmed`, and
`provider_unknown`. `cancel_requested` is non-terminal; `cancel_confirmed` is
terminal. The configured
`WORKBENCH_INSTANCE_ID` must be stable across restarts of one deployment slot and
unique across concurrently running slots. On startup, only non-terminal Turns
owned by an older lifecycle of that same instance are marked
`provider_unknown`. They are never automatically rerun.

This integration does not claim that Tencent can cancel or resume a provider
Turn. Recovery means replaying locally persisted SSE frames only. If the process
dies after a provider request may have been accepted, the outcome is unknown and
must be reconciled rather than guessed.
