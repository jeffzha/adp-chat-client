from pydantic import Field, NonNegativeInt, PositiveInt
from pydantic_settings import BaseSettings


class WorkbenchConfig(BaseSettings):
    """Configuration for the trusted new-api workbench integration."""

    WORKBENCH_MODE: bool = Field(
        description="Require trusted workbench SSO and disable local account entry points.",
        default=False,
    )

    WORKBENCH_CONTROL_URL: str = Field(
        description="Internal base URL of the claw-control service.",
        default="",
    )

    WORKBENCH_SERVICE_HMAC_SECRET: str = Field(
        description="Shared HMAC secret used only for ADP-to-control internal requests.",
        default="",
    )

    WORKBENCH_SERVICE_ID: str = Field(
        description="Stable service identity included in signed internal requests.",
        default="adp-backend",
    )

    WORKBENCH_SESSION_EXPIRE_MINUTES: PositiveInt = Field(
        description="Lifetime of an ADP shadow-account browser session.",
        default=15,
    )

    WORKBENCH_STREAM_REAUTH_SECONDS: PositiveInt = Field(
        description=(
            "Interval between full session, authorization, and app-context checks "
            "while a workbench SSE stream remains open."
        ),
        default=30,
    )

    WORKBENCH_CONTROL_TIMEOUT_SECONDS: PositiveInt = Field(
        description="Timeout for claw-control internal API calls.",
        default=10,
    )

    WORKBENCH_CONTROL_MAX_RESPONSE_BYTES: PositiveInt = Field(
        description="Maximum accepted response body size from claw-control.",
        default=1024 * 1024,
    )

    WORKBENCH_APP_MIGRATION_WORKER_ENABLED: bool = Field(
        description=(
            "Poll signed claw-control migration tasks and rebuild every active "
            "binding's Agent in a verified target App before AppId cutover."
        ),
        default=False,
    )

    WORKBENCH_APP_MIGRATION_POLL_SECONDS: PositiveInt = Field(
        description="Delay between empty App migration task polls.",
        default=5,
        le=300,
    )

    WORKBENCH_APP_MIGRATION_LEASE_SECONDS: PositiveInt = Field(
        description="Fail-closed lease for one target-App Agent rebuild task.",
        default=60,
        ge=10,
        le=120,
    )

    WORKBENCH_ALLOW_INSECURE_CONTROL_HTTP: bool = Field(
        description="Development-only escape hatch for an HTTP claw-control URL.",
        default=False,
    )

    WORKBENCH_CONTROL_CA_FILE: str = Field(
        description="Optional CA bundle used to verify claw-control TLS.",
        default="",
    )

    WORKBENCH_CONTROL_CLIENT_CERT_FILE: str = Field(
        description="Optional client certificate for claw-control mTLS.",
        default="",
    )

    WORKBENCH_CONTROL_CLIENT_KEY_FILE: str = Field(
        description="Optional client private key for claw-control mTLS.",
        default="",
    )

    WORKBENCH_ALLOWED_CLOCK_SKEW_SECONDS: NonNegativeInt = Field(
        description="Maximum accepted clock skew for signed service calls.",
        default=30,
    )

    WORKBENCH_COOKIE_PATH: str = Field(
        description="Browser cookie path used by the same-origin workbench deployment.",
        default="/workbench",
    )

    WORKBENCH_REDIRECT_PATH: str = Field(
        description="Safe relative path returned after a successful SSO exchange.",
        default="/workbench/",
    )

    WORKBENCH_ENABLE_HELPER_ASR: bool = Field(
        description=(
            "Explicitly expose the legacy /helper/asr/url endpoint in workbench mode. "
            "It remains disabled by default because it returns a provider URL to the browser."
        ),
        default=False,
    )

    WORKBENCH_FILES_ENABLED: bool = Field(
        description=(
            "Enable the private, malware-scanned workbench upload pipeline. "
            "The endpoint fails closed unless both scanner and private COS are configured."
        ),
        default=False,
    )

    WORKBENCH_FILE_SCANNER_HOST: str = Field(
        description="Internal ClamAV clamd host used for INSTREAM scanning.",
        default="",
    )

    WORKBENCH_FILE_SCANNER_PORT: PositiveInt = Field(
        description="Internal ClamAV clamd TCP port.",
        default=3310,
    )

    WORKBENCH_FILE_SCANNER_TIMEOUT_SECONDS: PositiveInt = Field(
        description="Connection and response timeout for malware scanning.",
        default=30,
    )

    WORKBENCH_FILE_SCANNER_MAX_BYTES: PositiveInt = Field(
        description="Configured ClamAV StreamMaxLength available to workbench uploads.",
        default=50 * 1024 * 1024,
    )

    WORKBENCH_FILE_ABSOLUTE_MAX_BYTES: PositiveInt = Field(
        description="Process-side hard ceiling applied even when a plan allows larger files.",
        default=50 * 1024 * 1024,
    )

    WORKBENCH_FILE_QUARANTINE_CAPACITY_BYTES: PositiveInt = Field(
        description="Usable bytes reserved for this process in its quarantine filesystem.",
        default=100 * 1024 * 1024,
    )

    WORKBENCH_FILE_QUARANTINE_DIR: str = Field(
        description="Dedicated tmpfs mount used only for quarantined workbench uploads.",
        default="",
    )

    WORKBENCH_FILE_MAX_CONCURRENT_UPLOADS: PositiveInt = Field(
        description="Per-process uploads allowed to occupy quarantine storage concurrently.",
        default=2,
    )

    WORKBENCH_FILE_COS_REGION: str = Field(
        description="Region of the dedicated private workbench COS bucket.",
        default="",
    )

    WORKBENCH_FILE_COS_BUCKET: str = Field(
        description="Existing private COS bucket dedicated to workbench uploads.",
        default="",
    )

    WORKBENCH_FILE_COS_SECRET_ID: str = Field(
        description="Least-privilege COS credential used only by workbench file storage.",
        default="",
    )

    WORKBENCH_FILE_COS_SECRET_KEY: str = Field(
        description="Least-privilege COS secret used only by workbench file storage.",
        default="",
    )

    WORKBENCH_FILE_URL_EXPIRE_SECONDS: PositiveInt = Field(
        description="Lifetime of server-generated COS URLs sent only to the ADP provider.",
        default=300,
    )

    WORKBENCH_FILE_LOCATOR_KEY: str = Field(
        description=(
            "Standard-base64 encoding of an independent 32-byte key used only "
            "for private file locators at rest. Never reuse the service HMAC key."
        ),
        default="",
    )

    WORKBENCH_FILE_LOCATOR_KEY_ID: str = Field(
        description="Non-secret identifier for the active private file locator key.",
        default="v1",
    )

    WORKBENCH_FILE_LOCATOR_PREVIOUS_KEYS_JSON: str = Field(
        description=(
            "JSON object mapping historical key IDs to standard-base64 32-byte keys. "
            "Retain entries until all private file locators have been rotated or deleted."
        ),
        default="{}",
    )

    WORKBENCH_RESOURCE_OUTBOX_INTERVAL_SECONDS: PositiveInt = Field(
        description="Retry interval for pending control-plane resource ownership reports.",
        default=5,
    )

    WORKBENCH_RESOURCE_OUTBOX_BATCH_SIZE: PositiveInt = Field(
        description="Maximum resource ownership reports processed in one retry batch.",
        default=50,
    )

    WORKBENCH_VENDOR_CACHE_IDLE_SECONDS: PositiveInt = Field(
        description="Idle lifetime of an in-memory dynamic customer vendor context.",
        default=300,
    )

    WORKBENCH_VENDOR_CACHE_MAX_ENTRIES: PositiveInt = Field(
        description="Per-process LRU bound for dynamic customer vendor contexts.",
        default=128,
    )

    WORKBENCH_INSTANCE_ID: str = Field(
        description=(
            "Stable, deployment-assigned identifier for this ADP instance. It scopes "
            "the durable claw-control Redis Stream cursor and must not change on restart."
        ),
        default="",
    )

    WORKBENCH_CONTROL_EVENT_CHANNEL: str = Field(
        description="Redis Pub/Sub channel used by claw-control; its Stream is <channel>:stream.",
        default="claw:control:events",
    )

    WORKBENCH_CONTROL_EVENT_PROCESSED_TTL_SECONDS: PositiveInt = Field(
        description="Redis retention for per-instance EventKey idempotency markers.",
        default=7 * 24 * 60 * 60,
    )

    WORKBENCH_CONTROL_EVENT_MAX_BACKOFF_SECONDS: PositiveInt = Field(
        description="Maximum reconnect backoff after Redis control-event failures.",
        default=30,
    )

    WORKBENCH_CONTROL_EVENT_MAX_BYTES: PositiveInt = Field(
        description="Maximum accepted serialized claw-control event size.",
        default=64 * 1024,
    )

    WORKBENCH_USAGE_EVIDENCE_KEY: str = Field(
        description=(
            "Standard-base64 encoding of an independent 32-byte key used to "
            "encrypt response.completed usage evidence at rest."
        ),
        default="",
    )

    WORKBENCH_USAGE_EVIDENCE_KEY_ID: str = Field(
        description=(
            "Non-secret identifier of the active usage-evidence encryption key. "
            "Keep old key material available when rotating this value."
        ),
        default="v1",
    )

    WORKBENCH_WORKSPACE_LOCATOR_KEY: str = Field(
        description=(
            "Standard-base64 encoding of an independent 32-byte key used only "
            "for provider Workspace locators at rest. Never reuse the service HMAC key."
        ),
        default="",
    )

    WORKBENCH_WORKSPACE_LOCATOR_KEY_ID: str = Field(
        description="Non-secret identifier for the active Workspace locator key.",
        default="v1",
    )

    WORKBENCH_WORKSPACE_LOCATOR_PREVIOUS_KEYS_JSON: str = Field(
        description=(
            "JSON object mapping historical key IDs to standard-base64 32-byte keys. "
            "Retain entries until every locator using them is re-encrypted or deleted."
        ),
        default="{}",
    )

    WORKBENCH_WORKSPACE_HOST_SUFFIXES: str = Field(
        description=(
            "Comma-separated DNS suffix allowlist for the HTTPS Domain returned "
            "by CreateWorkspaceCredential. Leave empty to fail closed until a "
            "real provider response has been verified."
        ),
        default="",
    )

    WORKBENCH_METRICS_ENABLED: bool = Field(
        description=(
            "Enable the independent Prometheus listener. It is disabled by default "
            "and must never be published by the public ingress."
        ),
        default=False,
    )

    WORKBENCH_METRICS_HOST: str = Field(
        description="Bind address for the independent internal metrics listener.",
        default="127.0.0.1",
        pattern=r"^[A-Za-z0-9.:-]{1,255}$",
    )

    WORKBENCH_METRICS_PORT: PositiveInt = Field(
        description="Port for the independent internal metrics listener.",
        default=9100,
        le=65535,
    )

    WORKBENCH_SCHEDULED_TASKS_ENABLED: bool = Field(
        description="Enable the bounded local offline scheduled-task worker and API.",
        default=False,
    )

    WORKBENCH_SCHEDULE_REAUTH_SECONDS: PositiveInt = Field(
        description="Maximum age of the SSO authentication allowed to create or mutate a task.",
        default=300,
        le=900,
    )

    WORKBENCH_SCHEDULE_WORKER_INTERVAL_SECONDS: PositiveInt = Field(
        description="Polling interval for due scheduled tasks and runnable attempts.",
        default=5,
        le=60,
    )

    WORKBENCH_SCHEDULE_LEASE_SECONDS: PositiveInt = Field(
        description="Renewable database lease duration for one scheduled run.",
        default=90,
        ge=30,
        le=600,
    )

    WORKBENCH_SCHEDULE_BATCH_SIZE: PositiveInt = Field(
        description="Maximum due tasks or runs claimed in one worker iteration.",
        default=20,
        le=100,
    )

    WORKBENCH_SCHEDULE_MIN_INTERVAL_MINUTES: PositiveInt = Field(
        description="Smallest permitted interval between cron occurrences.",
        default=15,
        ge=5,
        le=1440,
    )

    WORKBENCH_SCHEDULE_MAX_TASKS_PER_USER: PositiveInt = Field(
        description="Hard ceiling for non-deleted tasks owned by one binding and App.",
        default=20,
        le=100,
    )

    WORKBENCH_SCHEDULE_MAX_DAILY_RUNS: PositiveInt = Field(
        description="Hard ceiling for a task's configured daily execution count.",
        default=24,
        le=96,
    )

    WORKBENCH_SCHEDULE_MAX_PROMPT_CHARS: PositiveInt = Field(
        description="Maximum Unicode characters stored in a scheduled prompt.",
        default=16000,
        le=100000,
    )

    WORKBENCH_SCHEDULE_MAX_ATTACHMENTS: PositiveInt = Field(
        description="Maximum opaque owned WorkbenchFileIds stored by one task.",
        default=10,
        le=32,
    )

    WORKBENCH_SCHEDULE_DELEGATION_DAYS: PositiveInt = Field(
        description="Maximum lifetime of a revocable offline delegation snapshot.",
        default=30,
        le=90,
    )

    WORKBENCH_SCHEDULE_MISFIRE_GRACE_SECONDS: PositiveInt = Field(
        description="Delay after which skip-policy occurrences are recorded as skipped.",
        default=300,
        le=86400,
    )

    WORKBENCH_INTEGRATIONS_ENABLED: bool = Field(
        description=(
            "Enable the workbench integration catalog, Skill binding, and local "
            "connector OAuth vault. Provider tool/plugin/connector execution remains "
            "fail-closed until Tencent publishes the required contract."
        ),
        default=False,
    )

    WORKBENCH_INTEGRATION_ALLOWLIST_JSON: str = Field(
        description=(
            "Server-owned JSON allowlist keyed by trusted ApplicationId. Browser "
            "resource identifiers are never accepted outside this allowlist."
        ),
        default="{}",
    )

    WORKBENCH_INTEGRATION_ALLOWLIST_FILE: str = Field(
        description=(
            "Preferred mounted JSON file for the integration allowlist. It is "
            "mutually exclusive with a non-empty inline JSON value."
        ),
        default="",
    )

    WORKBENCH_OAUTH_PROVIDERS_JSON: str = Field(
        description=(
            "Server-owned OAuth provider metadata allowlist. Client secrets must be "
            "mounted-file references, never literal values."
        ),
        default="{}",
    )

    WORKBENCH_OAUTH_PROVIDERS_FILE: str = Field(
        description=(
            "Preferred mounted JSON file for OAuth provider metadata. It is mutually "
            "exclusive with a non-empty inline JSON value."
        ),
        default="",
    )

    WORKBENCH_OAUTH_SECRET_DIR: str = Field(
        description=(
            "Mounted directory containing OAuth client-secret files named by the "
            "server-owned provider metadata client_secret_ref."
        ),
        default="",
    )

    WORKBENCH_PUBLIC_BASE_URL: str = Field(
        description="Canonical public HTTPS origin used to construct OAuth callbacks.",
        default="",
    )

    WORKBENCH_CONNECTOR_TOKEN_KEY: str = Field(
        description=(
            "Standard-base64 independent 32-byte key used only for connector token JWE."
        ),
        default="",
    )

    WORKBENCH_CONNECTOR_TOKEN_KEY_FILE: str = Field(
        description="Preferred mounted file containing the active connector token key.",
        default="",
    )

    WORKBENCH_CONNECTOR_TOKEN_KEY_ID: str = Field(
        description="Non-secret identifier for the active connector token key.",
        default="v1",
    )

    WORKBENCH_CONNECTOR_TOKEN_PREVIOUS_KEYS_JSON: str = Field(
        description=(
            "JSON object mapping previous connector token key IDs to standard-base64 keys."
        ),
        default="{}",
    )

    WORKBENCH_CONNECTOR_TOKEN_PREVIOUS_KEYS_FILE: str = Field(
        description="Mounted JSON file containing previous connector token keys.",
        default="",
    )

    WORKBENCH_OAUTH_STATE_KEY: str = Field(
        description=(
            "Standard-base64 independent 32-byte key used only for OAuth PKCE verifier JWE."
        ),
        default="",
    )

    WORKBENCH_OAUTH_STATE_KEY_FILE: str = Field(
        description="Preferred mounted file containing the active OAuth state key.",
        default="",
    )

    WORKBENCH_OAUTH_STATE_KEY_ID: str = Field(
        description="Non-secret identifier for the active OAuth state key.",
        default="v1",
    )

    WORKBENCH_OAUTH_STATE_PREVIOUS_KEYS_JSON: str = Field(
        description="JSON object mapping previous OAuth state key IDs to base64 keys.",
        default="{}",
    )

    WORKBENCH_OAUTH_STATE_PREVIOUS_KEYS_FILE: str = Field(
        description="Mounted JSON file containing previous OAuth state keys.",
        default="",
    )

    WORKBENCH_OAUTH_STATE_TTL_SECONDS: PositiveInt = Field(
        description="Lifetime of a one-time OAuth authorization transaction.",
        default=300,
        le=900,
    )

    WORKBENCH_INTEGRATION_REAUTH_SECONDS: PositiveInt = Field(
        description="Maximum SSO authentication age for OAuth and binding mutations.",
        default=300,
        le=900,
    )

    WORKBENCH_OAUTH_HTTP_TIMEOUT_SECONDS: PositiveInt = Field(
        description="Total timeout for a DNS-pinned OAuth token or revocation request.",
        default=15,
        le=60,
    )

    WORKBENCH_OAUTH_MAX_RESPONSE_BYTES: PositiveInt = Field(
        description="Maximum accepted OAuth token/revocation response size.",
        default=256 * 1024,
        le=1024 * 1024,
    )

    WORKBENCH_OAUTH_REVOCATION_INTERVAL_SECONDS: PositiveInt = Field(
        description="Polling interval for durable OAuth compensation revocations.",
        default=15,
        le=300,
    )

    WORKBENCH_OAUTH_REVOCATION_BATCH_SIZE: PositiveInt = Field(
        description="Maximum durable OAuth revocations claimed by one worker pass.",
        default=20,
        le=100,
    )

    WORKBENCH_OAUTH_REVOCATION_LEASE_SECONDS: PositiveInt = Field(
        description="Blue/green claim lease for one OAuth revocation attempt.",
        default=60,
        ge=15,
        le=300,
    )

    WORKBENCH_SANDBOX_ENABLED: bool = Field(
        description="Enable the owner-scoped Tencent Agent Runtime sandbox API.",
        default=False,
    )

    WORKBENCH_SANDBOX_CODE_ENABLED: bool = Field(
        description="Enable memory-isolated E2B run_code execution.",
        default=False,
    )

    WORKBENCH_SANDBOX_PTY_ENABLED: bool = Field(
        description=(
            "Enable the browser PTY gateway. Keep false until the Tencent "
            "provider contract has passed production acceptance."
        ),
        default=False,
    )

    WORKBENCH_SANDBOX_PTY_TICKET_TTL_SECONDS: PositiveInt = Field(
        description="Lifetime of a one-use browser PTY WebSocket ticket.",
        default=30,
        le=60,
    )

    WORKBENCH_SANDBOX_PTY_MAX_RUNTIME_SECONDS: PositiveInt = Field(
        description="Hard ceiling for a browser PTY session.",
        default=600,
        ge=30,
        le=3600,
    )

    WORKBENCH_SANDBOX_PTY_REAUTH_SECONDS: PositiveInt = Field(
        description="Continuous owner authorization interval for browser PTY.",
        default=15,
        le=60,
    )

    WORKBENCH_SANDBOX_PTY_HEARTBEAT_SECONDS: PositiveInt = Field(
        description="Persistent PTY ownership heartbeat interval.",
        default=10,
        le=60,
    )

    WORKBENCH_SANDBOX_PTY_STALE_SECONDS: PositiveInt = Field(
        description="Age after which another Blue/Green process may reap a PTY.",
        default=45,
        ge=20,
        le=300,
    )

    WORKBENCH_SANDBOX_PTY_REAPER_SECONDS: PositiveInt = Field(
        description="Interval for reclaiming expired or abandoned PTY sessions.",
        default=15,
        le=300,
    )

    WORKBENCH_SANDBOX_PTY_GLOBAL_CAPACITY: PositiveInt = Field(
        description="Database-enforced global browser PTY capacity.",
        default=100,
        le=10000,
    )

    WORKBENCH_SANDBOX_PTY_CUSTOMER_CAPACITY: PositiveInt = Field(
        description="Database-enforced browser PTY capacity per customer.",
        default=10,
        le=1000,
    )

    WORKBENCH_SANDBOX_PTY_USER_CAPACITY: PositiveInt = Field(
        description="Database-enforced browser PTY capacity per user.",
        default=2,
        le=100,
    )

    WORKBENCH_SANDBOX_PTY_MAX_INPUT_FRAME_BYTES: PositiveInt = Field(
        description="Maximum UTF-8 input payload in one browser PTY frame.",
        default=16 * 1024,
        le=1024 * 1024,
    )

    WORKBENCH_SANDBOX_PTY_MAX_OUTPUT_FRAME_BYTES: PositiveInt = Field(
        description="Maximum binary output WebSocket frame size.",
        default=32 * 1024,
        le=1024 * 1024,
    )

    WORKBENCH_SANDBOX_PTY_MAX_INPUT_BYTES: PositiveInt = Field(
        description="Hard aggregate browser input ceiling for one PTY.",
        default=1024 * 1024,
        le=64 * 1024 * 1024,
    )

    WORKBENCH_SANDBOX_PTY_MAX_OUTPUT_BYTES: PositiveInt = Field(
        description="Hard aggregate provider output ceiling for one PTY.",
        default=8 * 1024 * 1024,
        le=128 * 1024 * 1024,
    )

    WORKBENCH_SANDBOX_PTY_MAX_INPUT_FRAMES_PER_SECOND: PositiveInt = Field(
        description="Hard browser PTY input-frame rate ceiling.",
        default=30,
        le=1000,
    )

    WORKBENCH_SANDBOX_PTY_OUTPUT_QUEUE_FRAMES: PositiveInt = Field(
        description="Bounded in-process provider-output queue depth.",
        default=32,
        le=1024,
    )

    WORKBENCH_SANDBOX_PROVIDER: str = Field(
        description="Managed sandbox provider; only tencent_agsx is supported.",
        default="tencent_agsx",
        pattern=r"^tencent_agsx$",
    )

    WORKBENCH_AGSX_REGION: str = Field(
        description="Tencent Agent Runtime region, for example ap-guangzhou.",
        default="",
        pattern=r"^[a-z0-9-]{0,64}$",
    )

    WORKBENCH_AGSX_DOMAIN: str = Field(
        description="E2B-compatible domain; must equal <region>.tencentags.com.",
        default="",
        max_length=255,
    )

    WORKBENCH_AGSX_CONTROL_ENDPOINT: str = Field(
        description="Official Tencent Agent Runtime control-plane SDK endpoint.",
        default="ags.tencentcloudapi.com",
        pattern=r"^[A-Za-z0-9.-]{1,255}$",
    )

    WORKBENCH_AGSX_TOOL_ID: str = Field(
        description="Server-selected Tencent sandbox ToolId.",
        default="",
        max_length=128,
    )

    WORKBENCH_AGSX_TOOL_NAME: str = Field(
        description="Server-selected Tencent sandbox ToolName/E2B template.",
        default="",
        max_length=128,
    )

    WORKBENCH_AGSX_API_KEY_FILE: str = Field(
        description="Mounted file containing the E2B-compatible ark_ API key.",
        default="",
    )

    WORKBENCH_AGSX_CAM_SECRET_ID_FILE: str = Field(
        description="Mounted file containing the least-privilege CAM SecretId.",
        default="",
    )

    WORKBENCH_AGSX_CAM_SECRET_KEY_FILE: str = Field(
        description="Mounted file containing the least-privilege CAM SecretKey.",
        default="",
    )

    WORKBENCH_SANDBOX_CLIENT_TOKEN_KEY_FILE: str = Field(
        description="Mounted independent HMAC key for deterministic provider ClientToken values.",
        default="",
    )

    WORKBENCH_SANDBOX_NETWORK_MODE: str = Field(
        description="Fail-closed network mode. The first release permits SANDBOX only.",
        default="SANDBOX",
        pattern=r"^SANDBOX$",
    )

    WORKBENCH_SANDBOX_AUTH_MODE: str = Field(
        description="Fail-closed sandbox authentication mode. TOKEN is mandatory.",
        default="TOKEN",
        pattern=r"^TOKEN$",
    )

    WORKBENCH_SANDBOX_LEASE_SECONDS: PositiveInt = Field(
        description="Blue/green provisioning lease duration.",
        default=30,
        ge=15,
        le=300,
    )

    WORKBENCH_SANDBOX_STARTS_PER_USER_MINUTE: PositiveInt = Field(
        description="Maximum provider starts per user in one rolling minute.",
        default=6,
        le=600,
    )

    WORKBENCH_SANDBOX_STARTS_PER_CUSTOMER_MINUTE: PositiveInt = Field(
        description="Maximum provider starts per customer in one rolling minute.",
        default=60,
        le=6000,
    )

    WORKBENCH_SANDBOX_TERMINAL_RETENTION_DAYS: PositiveInt = Field(
        description="Days to retain terminal sandbox handles before cleanup.",
        default=30,
        le=3650,
    )

    WORKBENCH_SANDBOX_DEFAULT_TIMEOUT_SECONDS: PositiveInt = Field(
        description="Default provider instance lifetime.",
        default=600,
        ge=30,
        le=86400,
    )

    WORKBENCH_SANDBOX_HARD_MAX_RUNTIME_SECONDS: PositiveInt = Field(
        description="Server hard ceiling for instance and operation lifetime.",
        default=3600,
        ge=30,
        le=86400,
    )

    WORKBENCH_SANDBOX_MAX_COMMAND_SECONDS: PositiveInt = Field(
        description="Server hard ceiling for one code or Shell operation.",
        default=120,
        le=3600,
    )

    WORKBENCH_SANDBOX_REAUTH_SECONDS: PositiveInt = Field(
        description="Maximum SSO authentication age for code execution requests.",
        default=300,
        le=900,
    )

    WORKBENCH_SANDBOX_PROVIDER_TIMEOUT_SECONDS: PositiveInt = Field(
        description="Request timeout for bounded provider data-plane operations.",
        default=30,
        le=300,
    )

    WORKBENCH_SANDBOX_MAX_CODE_CHARS: PositiveInt = Field(
        description="Maximum submitted code characters.",
        default=65536,
        le=1048576,
    )

    WORKBENCH_SANDBOX_MAX_COMMAND_CHARS: PositiveInt = Field(
        description="Maximum submitted non-interactive Shell command characters.",
        default=8192,
        le=65536,
    )

    WORKBENCH_SANDBOX_MAX_OUTPUT_BYTES: PositiveInt = Field(
        description="Maximum aggregate output bytes for one code or Shell operation.",
        default=1024 * 1024,
        le=16 * 1024 * 1024,
    )

    WORKBENCH_SANDBOX_CODE_WORKER_MEMORY_BYTES: PositiveInt = Field(
        description="Hard RLIMIT_AS ceiling for one isolated run_code SDK worker.",
        default=512 * 1024 * 1024,
        ge=256 * 1024 * 1024,
        le=2 * 1024 * 1024 * 1024,
    )

    WORKBENCH_SANDBOX_HARD_MAX_FILE_BYTES: PositiveInt = Field(
        description="Server hard ceiling for one managed-sandbox file read or write.",
        default=10 * 1024 * 1024,
        le=100 * 1024 * 1024,
    )

    WORKBENCH_DEPLOYMENT_TIER: str = Field(
        description=(
            "Deployment safety tier. Acceptance-only fault injection requires the "
            "exact value acceptance; production is the fail-closed default."
        ),
        default="production",
        pattern=r"^(production|staging|acceptance|development|test)$",
    )

    WORKBENCH_SANDBOX_ACCEPTANCE_FAULTS_ENABLED: bool = Field(
        description=(
            "Enable the authenticated managed-sandbox acceptance harness. Startup "
            "fails unless WORKBENCH_DEPLOYMENT_TIER is acceptance."
        ),
        default=False,
    )

    WORKBENCH_SANDBOX_ACCEPTANCE_TOKEN_FILE: str = Field(
        description="Mounted file containing the independent acceptance harness token.",
        default="",
    )

    WORKBENCH_SANDBOX_ACCEPTANCE_REAUTH_SECONDS: PositiveInt = Field(
        description="Maximum SSO authentication age for acceptance harness requests.",
        default=300,
        le=900,
    )
