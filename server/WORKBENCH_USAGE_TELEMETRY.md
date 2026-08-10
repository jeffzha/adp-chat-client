# Workbench Turn usage telemetry

This subsystem records auditable usage evidence for a durable Workbench Turn. It is
usage telemetry only: it does not calculate a per-Turn price, modify a customer
balance, or replace the manually reviewed monthly upstream-cost record in
`claw-control`.

## Capture order and failure behavior

For Tencent ADP SSE, the server handles `response.completed` in this order:

1. parse the provider JSON object;
2. call the trusted evidence sink with the unprojected object;
3. canonicalize the object and calculate SHA-256;
4. encrypt it as compact JWE (`A256KW` + `A256GCM`) and atomically persist evidence
   plus derived usage rows;
5. redact provider credentials and private file locators;
6. persist and replay the browser-safe SSE frame.

If encryption, persistence, integrity, or ownership validation fails, the completion
event is not accepted. A stream that ends without auditable `response.completed`
evidence cannot become `completed`; it becomes `failed_after_accept` or
`provider_unknown` according to whether a safe provider event was already accepted.
Duplicate completion objects with the same SHA-256 are idempotent. A second object for
the same Turn with a different hash is rejected as conflicting evidence.

The encrypted original is stored only in `workbench_turn_evidence`. The browser-safe
event remains in the bounded Turn replay store. Neither original is written to access
logs or application logs.

## Evidence and usage data

`workbench_turn_evidence` binds evidence to all of the following:

- Turn, identity binding, local account, customer and application;
- App profile and configuration version;
- provider RequestId, TraceId, RecordId and ConversationId when supplied;
- source, event type, observed time, encryption key id and plaintext SHA-256.

`workbench_turn_usage_datum` stores one non-additive projection per JSON source path.
Every row repeats the evidence hash and source. The projection never stores prompts,
model input/output content, system text, rewritten queries, credentials, or file URLs.

Supported sources include:

- `$.Response.StatInfo`;
- `$.Response.Procedures[i].StatInfos[j]`;
- `$.Response.Procedures[i].Agent.StatInfos[j]`;
- `$.Response.Procedures[i].Workflow.RunNodes[j].StatInfo[s]`.

Top-level, Procedure and Agent observations use `dedupe_confidence=unknown`. A
Workflow node observation uses `strong` only when the provider supplied both
`WorkflowRunId` and `NodeId`; its stable key also includes application and stat index.
Array position alone is never presented as a strong provider call id.

The records deliberately enforce these semantics:

- top-level and detailed values are never summed;
- `TotalTokens` is not reconstructed from input and output tokens;
- absent cache fields become `CacheTokensStatus=unknown`, never zero;
- `FirstTokenCost` means first-token latency;
- `TotalCost` means provider model elapsed time;
- neither `*Cost` field is interpreted as money.

## Key management

`WORKBENCH_USAGE_EVIDENCE_KEY` must be standard base64 for exactly 32 random bytes.
The deployment reads it from the Docker secret `adp_usage_evidence_key`; it must be
independent from service HMAC, browser session, control evidence and provider keys.
`WORKBENCH_USAGE_EVIDENCE_KEY_ID` is a non-secret identifier persisted with each row.

Generate the first key in a restricted terminal:

```sh
umask 077
openssl rand -base64 32 > secrets/adp_usage_evidence_key
```

The key is intentionally absent from database/Redis backups. Back it up through an
independent secret manager. Before rotating, retain the old key under its recorded key
id and complete a sample decrypt/integrity check for both old and new data. The current
server exposes no browser or general administrator endpoint for decrypting raw evidence;
`decrypt_for_audit` is a server-side primitive for a future separately authorized audit
workflow.
