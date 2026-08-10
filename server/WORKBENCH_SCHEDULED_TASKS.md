# Workbench offline scheduled tasks

This module is an ADP-local scheduler. It does **not** forward the browser's
`AppTrigger`, `Timer`, App, customer, Agent, credential or scope fields to
Tencent. It reuses the existing owned `Conversation` / durable `WorkbenchTurn`
/ `CoreChat` path and therefore has the same provider evidence, concurrency,
Agent-limit and private-file controls as an interactive Turn.

## Security contract

- The feature is disabled unless `WORKBENCH_SCHEDULED_TASKS_ENABLED=true`.
- Production startup requires PostgreSQL. SQLite is supported only by isolated
  tests; it is not a multi-instance lease implementation.
- Every task snapshots and binds `AccountId`, `BindingId`, canonical subject,
  customer, new-api user, App, App profile, config version, auth epoch and Agent.
  None of those fields is accepted by the JSON API.
- Creation, edit, pause, resume, run-now and delete require an active workbench
  session, a recent SSO authentication and a Strict same-site double-submit
  CSRF token.
- The offline delegation is a random 256-bit value represented in storage only
  by SHA-256, capability version and its immutable scope snapshot. Rotation
  retains revoked versions. Delegations expire and are revoked by pause/delete.
- Before initial provider submission and during a long stream, the worker asks
  claw-control to re-authorize the stored tuple. Member/customer/App disable,
  auth-epoch change, config/plan version change or local shadow-account disable
  fails closed.
- An expired delegation atomically pauses the task before materialization; the
  owner must reauthenticate and resume it to mint a new capability version.
- `chat` and `scheduled_tasks` capabilities are both required. Search, tools and
  connectors remain unavailable. The current provider Agent must still have the
  same owner binding, and its output/reasoning limits are enforced again.
- Prompts are stored as task content in PostgreSQL; there is no secret field and
  callers must not put credentials in prompts. Attachments are only opaque
  `WorkbenchFileId` references and are re-authorized/presigned at every run.
- List responses omit prompts. Only an exact owner lookup returns prompt text.
  Audit/outbox records and metrics never contain prompts, file locators, account
  display data or provider credentials.

## Schedule and delivery semantics

- Supported schedules are one ISO-8601 instant with an explicit offset or a
  numeric five-field cron expression (`minute hour day month weekday`) plus a
  fixed IANA timezone.
- Cron lists, ranges and steps are supported. The configured minimum interval,
  per-task daily limit, runtime, retry count/backoff, task count and attachment
  count are hard bounded.
- DST spring gaps are skipped naturally. A repeated wall-clock minute during a
  fall-back hour runs once, not twice.
- `misfire_policy=skip` records a skipped run after the configured grace period.
  `fire_once` creates at most one catch-up run and advances from current time;
  an outage never creates an unbounded backlog.
- `(task, scheduled instant)` is a unique idempotency key. PostgreSQL row locks,
  renewable leases and compare predicates prevent two blue/green workers from
  claiming the same run.
- Only a failure proven to occur before provider acceptance is retried. An
  expired lease after the submission boundary, missing completion evidence or
  an ambiguous provider result becomes `provider_unknown` and is never
  resubmitted automatically.
- Pause/delete cancel queued and retrying runs. Provider cancellation is not
  claimed: an already executing provider Turn may continue until its bounded
  runtime and is still accounted/audited.

## Same-origin API

The public edge prefixes these backend routes with `/workbench`:

- `GET|POST /workbench/scheduled-tasks`
- `GET|PATCH|DELETE /workbench/scheduled-tasks/{task_id}`
- `POST /workbench/scheduled-tasks/{task_id}/pause`
- `POST /workbench/scheduled-tasks/{task_id}/resume`
- `POST /workbench/scheduled-tasks/{task_id}/run`
- `GET /workbench/scheduled-tasks/{task_id}/runs?limit=20`

Mutation requests must send the `claw_workbench_csrf` cookie value in
`X-Workbench-CSRF`. Task JSON contains only content and scheduling fields:
`name`, `prompt`, optional `attachment_ids` / `conversation_id`,
`schedule_kind`, `cron_expression` or `once_at`, `timezone`, `misfire_policy`,
`max_runtime_seconds`, `daily_run_limit`, `max_retries` and
`retry_backoff_seconds`.

## Customer workbench UI

When claw-control grants `scheduled_tasks`, the workbench header exposes a
workbench-only task manager. It supports list, create, pause, resume, edit while
paused, delete, run-now, run-status/history refresh, and five-second polling
while the panel is open. Mutations are disabled when the workbench is read-only
and the browser sends only task content/schedule fields plus the same-origin
CSRF header; customer/App/Agent scope is never editable in the UI. English and
Chinese copy, responsive layout, keyboard close and visible error states are
included. The legacy ADP AppTrigger/CronTask UI and behavior are unchanged.

## Operations

Monitor `workbench_scheduled_task_actions_total{action}`,
`workbench_scheduled_materialized_total{result}`,
`workbench_scheduled_runs_total{status}` and
`workbench_scheduled_worker_failures_total{stage}`. Keep the metrics listener
internal. Lifecycle audits are written transactionally with state changes and
drained to structured logs by a local outbox. Backups must include all four
`workbench_scheduled_*` tables.

## Threat model

| Threat | Control |
|---|---|
| Browser forges customer/App/Agent | API schema rejects scope fields; worker uses persisted exact scope |
| IDOR reads another prompt | every lookup matches binding/account/customer/user/App/profile; miss is 404 |
| Stolen old delegation runs later | only digest stored; version/expiry/status/epoch/config checked per run |
| Blue and green submit twice | unique occurrence key, PostgreSQL `SKIP LOCKED`, renewable run lease |
| Crash after provider submission | persisted submission boundary; lease recovery marks `provider_unknown` |
| Outage creates a retry storm | skip/fire-once misfire policy, daily limit and bounded exponential retry |
| DST creates duplicate work | UTC scan plus strictly increasing local wall minute |
| Task enables tools/connectors | scheduled policy rejects all unbounded capabilities |
| File URL or secret leaks | only opaque file IDs persist; safe projections and prompt-free audit/metrics |
