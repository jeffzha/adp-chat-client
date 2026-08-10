# AppId migration Agent rebuild worker

The worker is disabled by default in an arbitrary ADP checkout. The managed
production Compose topology explicitly enables it on both Blue and Green;
claw-control leases each member row so only one instance receives a task.

The control claim and report routes use the existing nonce-bearing, signed
internal contract. A claim contains the immutable customer, binding, target App
profile/config fingerprint and provider context. AppKey and Tencent AK/SK are
accepted only on this internal TLS/HMAC response, remain in worker memory, are
removed from the dynamic vendor cache in `finally`, and are never placed in an
ADP table, task log, exception message, or report. Task dataclass repr is also
disabled.

For a normal `copy` task the worker locks the exact active `WorkbenchIdentity`,
creates a provisioning `WorkbenchAgentBinding`, calls
`CopyAgentFromApp(AppId, Kind=1)`, durably records the returned AgentId, then
calls `DescribeAgentDetail`. Only an exact AgentId readback activates both the
binding and `AgentConfig`; only after that local commit does the worker report
success. The report carries no provider response, only a SHA-256 hash of the
sanitized Agent object.

If CopyAgent may have run, the local binding becomes `provider_unknown`. A
normal retry is forbidden. When a returned AgentId was durably retained, an
administrator can request a control-plane retry that produces a `readback`
task. That path calls only `DescribeAgentDetail`; it never calls CopyAgent. An
unknown outcome without an AgentId remains fail-closed. Exact success report
replays are idempotent, and report transport errors are retried before the
lease expires.

Configuration:

- `WORKBENCH_APP_MIGRATION_WORKER_ENABLED`
- `WORKBENCH_APP_MIGRATION_POLL_SECONDS` (1–300)
- `WORKBENCH_APP_MIGRATION_LEASE_SECONDS` (10–120)

The worker requires `WORKBENCH_MODE=true` and PostgreSQL. SQLite remains test
only. Disabling the feature starts no background task; the managed production
preflight rejects that disabled state so a verified migration cannot wait
forever with no claimant.

## Cutover lineage and historical Conversations

Successful governance cutover creates an immutable source/local-App,
source/provider-App, source/profile/config to target/local-App,
target/provider-App, target/profile/config lineage tuple. Preparation,
verification, rebuild readiness, retry, replan, and rollback do not activate
lineage. Only the committed cutover emits `APP_MIGRATION_CUTOVER`.

The Redis envelope is versioned and HMAC-signed by `claw-control` with the ADP
service key. ADP verifies it before persisting `workbench_app_lineage`;
unsigned, tampered, cross-customer, or duplicate-but-different events fail
closed. Blue and Green consume the same durable stream and activation is
idempotent by EventKey and immutable tuple.

Conversation list/read forms the union of the current exact tuple and source
tuples whose active lineage targets that exact current tuple. Ownership still
requires the same customer, binding, account, Conversation workspace, and
Workspace. Historical message reads request a signed `purpose=history_read`
App context from `claw-control`; provider credentials remain server-only.
Send/update/delete/share, provider mutations, sandbox operations, and file
writes continue to accept only the current tuple. Historical resources return
404 for another customer or binding.
