# Workbench provider limits and fail-closed boundaries

This file records the provider contract used by `WORKBENCH_MODE`. It does not
infer undocumented Tencent behavior.

## Enforced Agent limits

Tencent ADP API version `2026-05-20` documents the following writable fields:

- `AgentSpec.Model.ModelParameters.MaxTokens` — maximum output length;
- `AgentSpec.AdvancedConfig.MaxReasoningRound` — maximum reasoning rounds.

Primary references:

- ModifyAgent: <https://cloud.tencent.com/document/product/1759/132543>
- DescribeAgentDetail: <https://cloud.tencent.com/document/api/1759/132544>
- Agent data structures: <https://cloud.tencent.com/document/api/1759/132545>
- Runtime Agent configuration for Claw workbenches:
  <https://cloud.tencent.com/document/product/1759/133871>

Before every plain chat Turn, the server:

1. acquires the database-backed runtime lease;
2. caps effective per-user concurrency at `1`, even if the control-plane value
   is higher;
3. provisions or loads the user's dedicated `Kind=1` Agent;
4. locks its `WorkbenchAgentBinding` and changes the binding state from
   `active` to `configuring`;
5. calls `DescribeAgentDetail` and verifies the returned `AgentId`;
6. maps the trusted `max_output_tokens` and `max_reasoning_rounds` values to the
   two fields above;
7. if required, calls trusted server-side `ModifyAgent` with full `Model` and
   `AdvancedConfig` sections and `UpdateMask.Paths = ["Model", "AdvancedConfig"]`;
8. calls `DescribeAgentDetail` again and requires exact integer equality before
   changing the binding back to `active` and allowing `/adp/v2/chat` to start.

The browser cannot call this trusted mutation path and cannot supply either
limit. The generic workbench `/adp/ModifyAgent` route remains disabled.

If the binding is already `configuring`, `provider_unknown`, or otherwise not
the expected active version, the Turn fails closed. If a provider mutation was
started but its outcome or readback is unknown, the binding remains
`provider_unknown` and requires reconciliation. A process crash while changing
configuration leaves `configuring`, which also blocks later Turns.

## Capabilities that remain closed

There is still no verified per-Turn provider contract for limiting the number
of calls made by:

- web search;
- Skills bound to the user Agent;
- scheduled tasks/triggers.

`SearchNetwork=true`, unbounded Skill execution and scheduled capabilities stay
closed. Allowlisted read-only Plugin/Tool/Connector catalog, binding and
readback are separate bounded capabilities; integration-dependent Turn execution
remains fail-closed until its provider limits pass live acceptance.
Before that path can open, it requires exact provider pre/post lists at mutation time and a
fresh `DescribeAgentDetail` check before every Turn. User OAuth, write/delete
tools, incomplete authorization and physical unbind remain closed. See
`WORKBENCH_INTEGRATIONS.md` for that boundary.

Plain text chat is allowed with the `chat` capability. File chat additionally
requires `files`, and its browser payload contains only a `WorkbenchFileId`.
The server resolves the encrypted, tenant-bound locator and never trusts a
browser URL or `DocId`.

## Locally enforced limits

The following limits do not depend on undocumented provider fields:

- `max_runtime_seconds`;
- customer concurrency;
- effective user concurrency of one;
- upload byte size;
- uploaded-file and conversation ownership;
- capability gates and read-only mode.

Workspace downloads use the implemented same-origin, owner-scoped proxy. The
server resolves the provider locator from its local user/Turn-bound mapping;
client-supplied `WorkspaceId` and `Path` are not accepted as ownership proof.
