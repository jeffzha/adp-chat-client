# Workbench Skill and connector security boundary

Retrieved and rechecked: **2026-08-10**. Provider API version: **ADP 2026-05-20**.

This module deliberately implements only behavior supported by the published
Tencent contract. It is disabled unless both `WORKBENCH_MODE=true` and
`WORKBENCH_INTEGRATIONS_ENABLED=true`.

## Official contract used

- [Dynamically modify Agent configuration in Claw mode](https://cloud.tencent.com/document/product/1759/133870)
- [Integrate a workbench](https://cloud.tencent.com/document/product/1759/133871)
- [ModifyAgent](https://cloud.tencent.com/document/api/1759/132543)
- [ADP API overview](https://cloud.tencent.com/document/product/1759/132555)
- [DescribeSkillSummaryList](https://cloud.tencent.com/document/api/1759/132540)
- [DescribePluginSummaryList](https://cloud.tencent.com/document/api/1759/132499)
- [DescribePlugin](https://cloud.tencent.com/document/api/1759/132500)
- [Using connectors](https://cloud.tencent.com/document/product/1759/115874)
- [Official Tencent Cloud Python SDK ADP 2026-05-20 models](https://github.com/TencentCloud/tencentcloud-sdk-python/blob/master/tencentcloud/adp/v20260520/models.py)
- [Official Tencent Cloud Python SDK ADP 2026-05-20 client](https://github.com/TencentCloud/tencentcloud-sdk-python/blob/master/tencentcloud/adp/v20260520/adp_client.py)

The contract proves that `ModifyAgent` can update the complete `SkillList`,
`PluginList` and `ToolList`; the dynamic-Claw guide explicitly requires
`UpdateMask.Paths=["ToolList","PluginList"]` for tool binding. Connector is
represented as `PluginClass=1`. The API exposes no ETag/version/CAS field, so
the implementation serializes mutations with the user Agent database row and
uses exact provider pre-read and post-read verification.

The public Cloud API exposes plugin OAuth metadata (`AuthType=3`,
`OAuthConsent=1`) but no `StartOAuth`, code exchange, refresh, revoke,
disconnect, credential-reference or end-user-token-to-Agent Action. Therefore:

- Skill bind/unbind is enabled. The server sends exactly
  `UpdateMask.Paths=["SkillList"]`, submits the complete locally authorized set,
  and reads `DescribeAgentDetail` back before committing success.
- Allowlisted Plugin, Tool and Connector binding is enabled only for provider
  resources whose configuration is complete, whose tools are read-only
  (`ToolAccessMode=1`), and which use only bounded non-OAuth provider
  authorization (`AuthType=0/1/2`). All `AuthType=3` resources remain blocked.
- Every mutation submits complete `PluginList` and `ToolList` together and
  verifies both lists exactly. A timeout, mismatch or incomplete readback marks
  both Agent and binding `provider_unknown`.
- Unbind is a verified logical disable: the physical Plugin/Tool entries remain
  and the selected Tool config is written with `IsDisabled=true`. Physical list
  removal/clearing remains disabled.
- Connector OAuth can acquire, encrypt, list as metadata and revoke a local
  user credential. It never injects that token into an ADP request, Agent
  headers, query parameters, prompts or browser responses.
- Every exchanged token is first protected by an encrypted durable revocation
  record. Failed post-exchange reauthorization, reconnect replacement,
  disconnect, App disable and membership revoke all enqueue provider
  compensation before local state is released. Blue/Green workers claim those
  records with leases, retry bounded provider failures, wipe ciphertext after
  confirmation and retain an explicit `provider_unknown` state when remote
  revocation cannot be proven.
- Every interactive and scheduled Turn revalidates the exact customer/user/App
  scope, provider Plugin/Tool set, disabled bits, authorization status and
  read-only access mode before provider execution. Agent limit enforcement only
  touches Model/AdvancedConfig and preserves these independently verified lists.

## Scope and state

Every binding, credential and pending OAuth transaction stores the complete
tuple:

```text
ADP AccountId
BindingId + CanonicalSubject
CustomerId + NewApiUserId + AuthEpoch
ApplicationId + ProviderAppId + AppProfileId + ConfigVersion
resource/provider/connector identifier
```

Every signed Workbench session also carries a random `sid` and immutable
`auth_time`. The database stores only the SHA-256 `sid` digest together with
the same identity/App/config tuple, authentication time, expiry and active
status. Recent-auth checks apply to that exact browser session; signing in in a
different browser or tab session never refreshes an older session.

All reads and writes match the complete tuple. Browser-supplied customer, App,
Agent, OAuth endpoint, redirect URI, client ID, scope, secret, token, header or
query fields are ignored because the dedicated APIs do not accept them. The
server derives them from the current trusted workbench context and mounted
allowlists.

The user catalog is the intersection of:

```text
server ApplicationId/config-version allowlist
∩ package capabilities returned by claw-control
∩ current user action/consent
```

Capability intersections are:

| Kind | Required capabilities |
|---|---|
| Skill | `catalog_skills` and `skills` |
| Plugin | `catalog_plugins` and `tools` |
| Tool | `catalog_plugins` and `tools` |
| Connector | `catalog_plugins` and `connectors` |

A Skill/Plugin/Tool/Connector mutation and every ordinary or scheduled Turn submission lock the same
`workbench_agent_binding` row before inspecting or changing state. The lock
order is always Agent binding first, then Turn/binding rows; `AttemptId` is the
Agent mutation epoch. A mutation returns 409 while any Turn in `submitted`,
`running` or `cancel_requested` exists, and a Turn returns 409 while the Agent
is not active. This prevents a Turn from slipping between the active-Turn check
and the provider `SkillList` replacement. Before mutation, the provider Skill
IDs must exactly equal the active local binding set. Mismatch returns 409 and
requires reconciliation. Ambiguous provider mutation results mark both Agent
and binding `provider_unknown`.

For Plugin/Tool/Connector mutation the pre-read physical Plugin and Tool IDs,
active Tool IDs and local active/revoked rows must match exactly. The server
then submits both complete lists in one `ModifyAgent` call. Post-read config
must match the submitted canonical config exactly; every Plugin must have
`AuthConfigStatus=2` and every Tool must be available and non-write. Local OAuth
credentials are never copied into these configurations.

`SESSION_REVOKE` and `CACHE_INVALIDATE` control events revoke matching local
credentials, bindings and pending OAuth states. Request-time and callback-time
claw-control authorization independently fail closed, so a delayed event cannot
restore access. Provider Skill state is not silently rewritten by a revocation
event; provider non-empty Skill state keeps all subsequent Turns blocked.

## OAuth 2.1 flow

1. A recently authenticated active browser session starts a connector flow.
2. The connector and provider are resolved only from mounted allowlists.
3. The server creates 256-bit state and transaction nonce values plus a PKCE
   verifier and a separate browser transaction secret. Only SHA-256
   state/nonce/session/transaction-cookie digests and an encrypted verifier are
   stored. The authorization request uses `code_challenge_method=S256`.
4. The callback URL is built only from `WORKBENCH_PUBLIC_BASE_URL` and the
   server-known provider ID. Start sets a host-only, path-scoped, Secure,
   HttpOnly, `SameSite=Lax` one-time transaction cookie. Callback requires the
   current signed Workbench session and exact transaction cookie, then compares
   both digests plus the complete identity/App/config tuple.
5. A monotonic connector generation is locked and incremented on every start
   and disconnect. A new start supersedes all older pending starts. Disconnect
   supersedes pending starts before returning. Callback and credential
   persistence both compare the captured generation, so an older callback can
   never reconnect after disconnect or a newer start.
6. The stored transaction is locked, expiry checked and consumed once before
   any token exchange. Provider denial also consumes it. The exact offline
   identity/customer/App/config tuple is reauthorized before exchange and
   again before persistence.
7. Token/revocation HTTP requests use allowlisted HTTPS hosts on port 443,
   public-only DNS resolution, pinned resolved IPs, standard hostname TLS
   verification, redirects disabled, bounded time and bounded bodies.
8. Only access token, optional refresh/ID token, Bearer token type, granted
   allowlisted scopes and bounded expiry are accepted. Tokens are canonicalized
   and encrypted as compact JWE (`A256KW` + `A256GCM`) under an independent
   active/previous key ring.
9. APIs return status, scope, token type and expiry metadata only. Token values,
   ciphertext, digest, PKCE verifier and provider raw errors are never returned.

The `nonce` is an opaque transaction nonce bound inside the one-time state and
also sent in the authorization request. It is not claimed to authenticate an
OIDC identity, and the module does not consume an ID token as user identity.

Disconnect revokes locally first. If an allowlisted revocation endpoint exists,
the server then makes a best-effort pinned request and records only
`provider_revoked`, `provider_revoke_failed` or `provider_not_applicable`.

## Customer APIs

All responses use `Cache-Control: no-store`. Mutations require the existing
double-submit `claw_workbench_csrf` token.

| Method and path | Authentication | Behavior |
|---|---|---|
| `GET /integrations` | workbench session | Allowlisted catalog, binding state and credential metadata |
| `POST /integrations/bindings` | session + CSRF + recent auth | Bind or safely disable an allowlisted resource; physical Plugin/Tool clearing is not exposed |
| `POST /integrations/connectors/{id}/oauth/start` | session + CSRF + recent auth | Returns one allowlisted authorization URL, never a token |
| `GET /integrations/oauth/callback/{provider}` | current workbench session + host-only one-time transaction cookie + state | Consumes callback and redirects to a fixed workbench result URL |
| `POST /integrations/connectors/{id}/disconnect` | session + CSRF + recent auth | Local-first revoke; returns metadata only |

The customer UI distinguishes Skill, Plugin, Tool and Connector. It exposes
bind/safe-disable only. User OAuth and local-token injection are not offered by
this binding UI; blocked resources expose a stable reason such as
`user_oauth_unsupported`, `write_tool_unsupported` or
`provider_config_incomplete`.

## Configuration

Mounted files are preferred in production. Inline JSON/key settings exist only
for tests and local development. When a file setting is present, a non-empty
inline value is rejected (`{}` is treated as the default empty value for JSON
maps).

| Setting | Secret | Validation and purpose |
|---|---:|---|
| `WORKBENCH_INTEGRATIONS_ENABLED` | No | Boolean; both this and `WORKBENCH_MODE` must be true |
| `WORKBENCH_INTEGRATION_ALLOWLIST_FILE` | No | Existing UTF-8 JSON regular file, maximum 1 MiB |
| `WORKBENCH_INTEGRATION_ALLOWLIST_JSON` | No | Inline alternative, maximum 1 MiB; default `{}` |
| `WORKBENCH_OAUTH_PROVIDERS_FILE` | No | Existing UTF-8 JSON regular file, maximum 1 MiB |
| `WORKBENCH_OAUTH_PROVIDERS_JSON` | No | Inline alternative, maximum 1 MiB; default `{}` |
| `WORKBENCH_OAUTH_SECRET_DIR` | Path only | Existing non-symlink directory. Each `client_secret_ref` resolves to one direct, non-symlink regular child file; 16-4096 UTF-8 characters, no NUL |
| `WORKBENCH_PUBLIC_BASE_URL` | No | HTTPS only, port 443/default, no credentials/query/fragment or `..`; include deployment base path such as `/workbench` |
| `WORKBENCH_CONNECTOR_TOKEN_KEY_FILE` | **Yes, contents** | Mounted standard-base64 32-byte active key; file maximum 4096 bytes |
| `WORKBENCH_CONNECTOR_TOKEN_KEY` | **Yes** | Inline alternative, exact standard-base64 32-byte value |
| `WORKBENCH_CONNECTOR_TOKEN_KEY_ID` | No | Non-empty identifier, maximum 64 characters; default `v1` |
| `WORKBENCH_CONNECTOR_TOKEN_PREVIOUS_KEYS_FILE` | **Yes, contents** | Mounted JSON map, maximum 64 KiB; at most 16 old keys |
| `WORKBENCH_CONNECTOR_TOKEN_PREVIOUS_KEYS_JSON` | **Yes** | Inline alternative; default `{}` |
| `WORKBENCH_OAUTH_STATE_KEY_FILE` | **Yes, contents** | Mounted standard-base64 independent 32-byte active PKCE/state key |
| `WORKBENCH_OAUTH_STATE_KEY` | **Yes** | Inline alternative, exact standard-base64 32-byte value |
| `WORKBENCH_OAUTH_STATE_KEY_ID` | No | Non-empty identifier, maximum 64 characters; default `v1` |
| `WORKBENCH_OAUTH_STATE_PREVIOUS_KEYS_FILE` | **Yes, contents** | Mounted JSON map, maximum 64 KiB; at most 16 old keys |
| `WORKBENCH_OAUTH_STATE_PREVIOUS_KEYS_JSON` | **Yes** | Inline alternative; default `{}` |
| `WORKBENCH_OAUTH_STATE_TTL_SECONDS` | No | Integer `1..900`; default 300 |
| `WORKBENCH_INTEGRATION_REAUTH_SECONDS` | No | Integer `1..900`; default 300 |
| `WORKBENCH_OAUTH_HTTP_TIMEOUT_SECONDS` | No | Integer `1..60`; default 15 |
| `WORKBENCH_OAUTH_MAX_RESPONSE_BYTES` | No | Integer `1..1048576`; default 262144 |

Previous-key JSON has the form `{ "old-key-id": "base64-32-byte-key" }`.
Key IDs must be unique, 1-64 characters and cannot equal the active ID. Old key
material must be retained until every row encrypted under it is revoked or
rotated.

### Integration allowlist schema

```json
{
  "applications": {
    "trusted-application-id": {
      "config_versions": [5],
      "resources": [
        {
          "kind": "skill",
          "id": "skill-id",
          "name_zh": "客户可见名称",
          "name_en": "Customer-visible name"
        },
        {
          "kind": "connector",
          "id": "connector-plugin-id",
          "name_zh": "连接器名称",
          "name_en": "Connector name",
          "provider_id": "provider-name",
          "requires_oauth": true
        }
      ]
    }
  }
}
```

Maximum 500 resources per App. `kind` is exactly `skill`, `plugin`, `tool` or
`connector`. IDs are 1-128 characters matching `[A-Za-z0-9][A-Za-z0-9._:-]*`.
A Tool requires `parent_id` (its Plugin ID); other kinds forbid it. Only a
Connector has `provider_id` and `requires_oauth`.

### OAuth provider metadata schema

```json
{
  "providers": {
    "provider-name": {
      "authorization_url": "https://oauth.example.com/authorize",
      "token_url": "https://oauth.example.com/token",
      "revocation_url": "https://oauth.example.com/revoke",
      "client_id": "public-client-id",
      "client_secret_ref": "provider-client-secret",
      "token_auth_method": "client_secret_basic",
      "allowed_scopes": ["files.read"],
      "allowed_hosts": ["oauth.example.com"]
    }
  }
}
```

Provider IDs and secret refs match `[a-z0-9][a-z0-9_-]*` and are at most 64
characters. `token_auth_method` is `client_secret_basic`,
`client_secret_post` or `none`; `none` forbids a secret ref. URLs must be HTTPS
on port 443/default and use an exact host from `allowed_hosts`; wildcards,
userinfo and fragments are forbidden. At least one and at most 32 printable
ASCII scopes are required. `revocation_url` is optional.

## Startup dependencies

- Python 3.12 dependencies from `pyproject.toml`, including `aiohttp`,
  `jwcrypto`, SQLAlchemy and asyncpg.
- Workbench PostgreSQL migration must create:
  `workbench_browser_session`, `workbench_integration_binding`,
  `workbench_connector_credential`, `workbench_connector_scope`,
  `workbench_oauth_state_v2`, and `workbench_integration_audit`.
- Deployment intentionally does not migrate pending rows from the legacy
  `workbench_oauth_state` table: those rows lack session-cookie binding and a
  connector generation, so all pre-deployment OAuth flows must be restarted.
- Workbench auth cookies are `SameSite=Lax` only so a top-level cross-site
  provider redirect can carry the current HttpOnly session. All state-changing
  customer APIs remain POST-only and require the double-submit CSRF token.
- Existing workbench SSO, claw-control authz/app-context APIs, CSRF cookie,
  Redis control-event subscriber and per-user `CopyAgentFromApp(Kind=1)` Agent.
- Plan capabilities listed above and an App published with dynamic Claw Agent
  configuration enabled.
- Public ingress must route the exact callback path to ADP while preserving
  HTTPS and must not cache it. Internal database/control/metrics paths remain
  non-public.
- Mounted allowlist/provider/key/secret files must be readable by the ADP
  process. The OAuth secret directory and its referenced direct child files
  must not be symlinks. Token and OAuth-state keys must be independently
  generated.

## Inputs still required from the operator/provider

The safe foundation cannot be enabled without:

1. trusted `ApplicationId`, allowed `config_versions`, exact Skill/Plugin/Tool/
   Connector IDs, localized names and intended package capabilities;
2. for each external OAuth provider: exact authorization/token/revocation URLs,
   client ID, token authentication method, minimum scopes, exact hosts and one
   mounted client-secret file when confidential-client authentication is used;
3. the canonical public `/workbench` origin/path registered at each provider;
4. an official Tencent contract for end-user OAuth token handoff/reference to
   an Agent before Connector execution can be enabled;
5. an official Tencent guarantee for complete `PluginList`/`ToolList`
   replacement and empty-list behavior, plus enforceable per-Turn tool limits
   or another atomic execution boundary, before Plugin/Tool/Connector binding
   and execution can be enabled.

No undocumented ADP Action or token injection mechanism should be added as a
workaround.
