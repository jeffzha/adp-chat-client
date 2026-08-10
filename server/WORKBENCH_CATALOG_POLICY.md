# Workbench catalog policy

This document defines the deliberately small, read-only Tencent ADP catalog
surface exposed through the generic `POST /adp/<Action>` route in workbench
mode. The implementation follows the Tencent ADP API `2026-05-20` contracts:

- `DescribeModelList`
- `DescribeSkillSummaryList` and `DescribeSkillDetail`
- `DescribePluginSummaryList` and `DescribePlugin`

The upstream service is always `adp`, the API version is always `2026-05-20`,
and the region is always `ap-guangzhou`. These routing values are server-owned;
the browser cannot submit `Service`, `Version`, or `Region`.

## Capability mapping

Catalog access is default-deny and uses capabilities received in the signed,
continuously authorized `WorkbenchAppContext`:

| Capability | Allowed Actions | Meaning |
| --- | --- | --- |
| `catalog_models` | `DescribeModelList` | Discover models usable by Claw mode. The server forces `ModelScene=18`. |
| `catalog_skills` | `DescribeSkillSummaryList`, `DescribeSkillDetail` | Discover and inspect Skills. |
| `catalog_plugins` | `DescribePluginSummaryList`, `DescribePlugin` | Discover and inspect Plugins/tools. The server forces `Module=3`. |

These capabilities are separate from the existing unbounded `tools`
capability. Enabling catalog discovery therefore does not silently enable tool
execution or Agent mutation. Unknown capability strings grant no access.

## Trusted scope and request rules

`ApplicationId` must match the active identity. `SpaceId`, App IDs, AppKey,
SecretId, SecretKey, credentials, authorization configuration, and OAuth data
are never accepted from browser payloads. `SpaceId` comes only from the trusted
control-plane context and must match the server-side vendor instance.

List requests have hard limits:

- `PageNumber`: 0 through 100.
- `PageSize`: 1 through 50.
- `Query`: at most 128 characters.
- `FilterList`: at most five unique, Action-specific filter names; each filter
  has at most eight values of at most 64 characters.
- Skill discovery exposes only the documented `ProviderType`, `CategoryKey`,
  `AnalysisStatus`, and `RiskLevel` filters. `SkillIdList`, `SkillStatus`,
  `ShareStatus`, `Perspective`, and `Creator` remain closed because they either
  create an ID oracle or depend on editor/ownership views.
- Plugin `SortType` is bounded to the documented values 0 through 2.

Skill detail always forces a `USER` perspective. Favorite-only, editor/all
perspectives, ownership filters, custom field masks, arbitrary model scenes,
and arbitrary plugin modules are not exposed.

## Summary-before-detail boundary

Skill and Plugin detail IDs must first appear in a successful, validated summary
response. The allowlist cache is scoped by account, binding, canonical subject,
customer, application, App profile, configuration version, authorization epoch,
Space, and catalog kind. It has a two-minute TTL, LRU scope limit, and a maximum
of 500 IDs per scope.

Application eviction, revocation, or configuration reload clears all catalog
allowlists for that application. Config/auth epoch changes also create a new
cache scope. The cache is intentionally local and fail-closed: after an instance
change, a caller repeats the summary request before requesting detail.

Provider list sizes, counts, item shapes, identifiers, duplicates, and detail ID
echoes are validated before being returned. A provider detail response for a
different identifier is rejected as an upstream boundary violation.

## Response projection

Catalog responses are recursively projected before leaving the server:

- Secret, credential, AuthConfig, OAuth, authorization, plugin header/query,
  Space/App identity, and private package/report URL fields are removed.
- Known trusted credential values are replaced even if the provider echoes them
  under an innocent-looking field.
- HTTP(S) URLs have userinfo, query strings, and fragments removed so signed URL
  credentials cannot escape.
- Provider errors expose only the code `ProviderError` and the generic message
  `provider request failed`; provider-controlled code/message text is discarded.

## Explicitly not exposed

This catalog does not expose Create, Modify, Delete, Release, Favorite,
Unfavorite, Share, OAuth, binding/install, tool execution, Agent configuration,
or model configuration Actions. It does not grant write access to Skills or
Plugins, bind discovered resources to an Agent, or enable arbitrary MCP servers.

The dedicated `/integrations` service is the only exception: it independently
intersects this provider catalog with the signed AppContext and mounted resource
allowlist, then performs Agent-row-locked, exact pre/post-read `ModifyAgent`
binding. This generic `/adp/<Action>` catalog never grants that authority.
