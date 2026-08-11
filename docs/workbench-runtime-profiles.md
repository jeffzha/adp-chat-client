# Workbench runtime profile boundary

This fork consumes a provider-derived runtime profile from the signed
`claw-control` `app-context` contract. The browser cannot submit or override
`AppMode`, `AppId`, `SpaceId`, `AgentId`, the runtime profile, or its execution
gate.

## Profiles

| Provider AppMode | Runtime profile | Tencent user Agent | Initial execution gate |
|---:|---|---|---|
| 1 | `standard_v2` | Never copied or sent | Disabled |
| 2 | `multi_agent_v2` | Never copied or sent | Disabled |
| 3 | `workflow_v2` | Never copied or sent | Disabled |
| 4 | `claw_static_v2` | Never copied or sent | Disabled |
| 4 | `claw_dynamic_v2` | One Kind=1 Agent per binding and Application | Enabled for the accepted Chat subset |

The mode/profile mapping is closed. Partial fields, unknown values, mismatched
mode/profile pairs, and an execution-enabled profile that has not passed local
acceptance all fail closed. An app-context from the preceding contract version,
which has none of the three runtime fields, is interpreted as the already
accepted `claw_dynamic_v2` profile so the current production Claw Chat path is
backward compatible. Once any runtime field is present, all three are required.

## Conversation and ownership behavior

Every profile retains the canonical VisitorId, `CreateConversation(Type=5)`,
durable Turn/SSE replay, history scope, hidden drain, and provider evidence.
Only `claw_dynamic_v2` sends a server-resolved `AgentId` to Tencent and may call
`CopyAgentFromApp(Kind=1)`, `DescribeAgentDetail`, or `ModifyAgent`.

The existing ownership schema requires a Conversation parent identifier. A
non-dynamic profile therefore gets a deterministic, opaque local runtime
principal (`wrp_...`). It is reported through the existing ownership contract
but is never sent to Tencent and is not a provider Agent. A profile/config
change cannot silently reuse a provider Agent or local runtime principal; it
fails with a reconciliation conflict.

Generic `Action` compatibility calls obey the same boundary. In particular,
non-dynamic `CreateConversation` and `DescribeConversationList` resolve only the
local `wrp_...` ownership principal and omit `AgentId` from the Tencent request.
An `AgentId` supplied by the browser is rejected rather than forwarded.

## App migration contract

Migration claim tasks carry `provider_app_mode`, `runtime_profile`, and
`execution_enabled` both in the signed task snapshot and in its signed provider
snapshot. Both triples are closed-set validated and must match exactly. Missing,
partial, or inconsistent triples fail before any provider operation.

Dynamic Claw migration retains the existing copy/readback flow. A non-dynamic
migration accepts only `mode=copy` with no `known_target_agent_id`; it establishes
and verifies the deterministic local `wrp_...` ownership lineage without
instantiating a provider client or calling `CopyAgentFromApp` or
`DescribeAgentDetail`. Its success report deliberately has empty
`target_agent_id` and `target_readback_hash`; readiness is represented by the
signed target profile/config tuple and the active local ownership binding.

## Enabling another profile

Code support does not authorize provider execution. Before adding a profile to
`LOCALLY_ACCEPTED_EXECUTION_PROFILES`, run a real published Tencent Application
acceptance suite covering at least:

1. provider `DescribeApp` readback and exact AppMode/profile derivation;
2. `CreateConversation` with Type 5, canonical UserId, and no AgentId;
3. `/adp/v2/chat` completion plus durable SSE replay and history consistency;
4. provider error projection, throttling, revocation, and hidden drain;
5. a trace assertion that no Copy/Describe/Modify Agent operation occurred;
6. capability-specific inputs and events for Standard, Multi-Agent, or Workflow.

Changing only the control-plane `execution_enabled` value is insufficient: the
ADP fork also rejects profiles missing from its local acceptance allowlist.
