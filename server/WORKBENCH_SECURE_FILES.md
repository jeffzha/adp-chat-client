# Workbench secure file pipeline

Workbench mode never uploads browser content through ADP's legacy
`AppId=0, IsPublic=True` path. `POST /file/upload` instead performs this sequence:

1. Enforce the product size limit while streaming into an OS temporary quarantine file.
2. Validate the filename extension, declared MIME type, magic bytes, and reject executable/polyglot indicators.
3. Scan the complete quarantine file with an internal ClamAV `INSTREAM` service.
4. Upload only a clean file to a pre-existing, private COS bucket under a random customer/binding-owned key.
5. Persist only an encrypted `{Storage,ObjectKey,Bucket,Region,ContentSha256}` locator and integrity hashes. The streaming SHA-256 is also stored as private COS object metadata.
6. Return only `WorkbenchFileId`, name, MIME type, and size to the browser.
7. Resolve ownership and generate a short-lived COS URL server-side immediately before an ADP chat request.

The temporary locator and configured provider credentials are recursively redacted
from live SSE events, durable Turn events, and history projections, including nested
objects and URL-encoded representations. History is rejected as a whole with 502 if
any returned record has a `ConversationId` different from the requested owned
conversation. `/file/parse` remains an explicit 503 in workbench mode because its
legacy provider contract expects public locator fields.

Provider-generated Workspace files use a separate read-only path. After an
owned `DescribeConversation`, the provider Workspace ID is encrypted and the
browser receives only a local `ww_*` handle. Directory listing and file download
resolve that handle after full ownership revalidation. Downloads are bounded by
the smaller of the frozen plan file limit and the absolute process limit, use a
fresh server-side Workspace credential, disable redirects, require HTTPS, and
return `Content-Disposition: attachment`, `no-store`, `nosniff`, and a sandboxed
CSP. The legacy preview flow remains unavailable because it exposes a signed
COS URL to browser code.

Provider Workspace origins also require an explicit DNS-suffix allowlist, an
empty URL path, the official `X-File-Ticket` header, and public-only A/AAAA
answers pinned for the connection after resolution. Downloads hold a durable
customer/member concurrency lease and stream in bounded 64 KiB chunks; the ADP
worker never accumulates the full provider file in memory. A chunked response
that crosses the byte ceiling is terminated and the upstream connection and
lease are released.

Scanner connection errors, timeouts, malformed verdicts, and missing scanner/COS
configuration all fail closed. Quarantine files are deleted in a `finally` block and
are never served. The COS bucket is not automatically created: operations must
pre-create it with public access blocked, encryption/lifecycle policies as required,
and grant the dedicated CAM identity only the object permissions needed below the
`workbench/` prefix.

## Configuration

Set `WORKBENCH_FILES_ENABLED=true` only after configuring:

- `WORKBENCH_FILE_SCANNER_HOST`, `WORKBENCH_FILE_SCANNER_PORT`, and scanner timeout;
- scanner, process and quarantine bounds. The effective per-file limit is the minimum of the customer's plan limit, `WORKBENCH_FILE_SCANNER_MAX_BYTES`, `WORKBENCH_FILE_ABSOLUTE_MAX_BYTES`, and `WORKBENCH_FILE_QUARANTINE_CAPACITY_BYTES / WORKBENCH_FILE_MAX_CONCURRENT_UPLOADS`;
- dedicated `WORKBENCH_FILE_COS_SECRET_ID` / `WORKBENCH_FILE_COS_SECRET_KEY`;
- `WORKBENCH_FILE_COS_REGION` and `WORKBENCH_FILE_COS_BUCKET`;
- a short `WORKBENCH_FILE_URL_EXPIRE_SECONDS` (default 300 seconds).
- an independent standard-base64 32-byte `WORKBENCH_FILE_LOCATOR_KEY` and
  non-secret `WORKBENCH_FILE_LOCATOR_KEY_ID`. This key must not reuse the
  ADP-to-control HMAC, session, usage-evidence, Workspace-locator, COS, or
  provider credential. Rotation moves the old key into the server-only
  `WORKBENCH_FILE_LOCATOR_PREVIOUS_KEYS_JSON` map until every old locator has
  been re-encrypted or deleted; the JWE `kid` selects the correct key.

ClamAV must be reachable only on the internal service network. Its `StreamMaxLength`
must be at least the maximum file size allowed by the customer's workbench policy.
The default process limit is 50 MiB with two concurrent uploads and 100 MiB usable
quarantine capacity. The container tmpfs must be larger than that usable capacity
(128 MiB is the recommended minimum for these defaults). Every Blue/Green replica
has its own semaphore and quarantine capacity; total scanner throughput must be
sized for the sum of all replicas.
Production must mount that tmpfs at the dedicated
`WORKBENCH_FILE_QUARANTINE_DIR` (recommended
`/var/lib/workbench-quarantine`); sharing the general `/tmp` defeats the capacity
assumption. The 100 MiB configured usable capacity deliberately leaves 28 MiB
headroom in the recommended 128 MiB mount.

If database ownership binding or the control-plane ownership report does not finish
successfully, the row never becomes `active` and the router performs a compensating
private-COS delete before returning an error. Failed compensation is logged without
bucket or object identifiers and should be covered by a private-bucket lifecycle rule.
An uncatchable process or host failure in the narrow interval after COS accepts the
object and before the pending database binding commits can still leave an unreachable
private object. Configure a lifecycle expiration for the upload prefix and run an
operator orphan audit that compares COS keys with active/pending bindings; this is a
residual distributed-transaction risk, not a reason to expose the locator.

## Existing `util/cos.py` audit

The generic helper is intentionally not reused by this pipeline. It reads ambient
Tencent credentials, automatically creates missing buckets, exposes URL-oriented
helpers, caches clients without a bucket/security-policy boundary, and does not force
an object-private ACL. These behaviors are useful to legacy application features but
do not satisfy the workbench isolation boundary. The secure pipeline therefore uses
separate settings, refuses to create buckets, forces HTTPS and `ACL=private`, uses
random owned keys, and never returns a signed locator to the browser.
