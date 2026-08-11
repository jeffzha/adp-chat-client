import hashlib
import hmac
import ssl
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import ujson

from config import tagentic_config
from core.workbench_metrics import WORKBENCH_METRICS
from core.workbench_runtime_profile import (
    CLAW_DYNAMIC_V2,
    WorkbenchRuntimeProfile,
    validate_runtime_profile,
)


WORKBENCH_CAPABILITIES = frozenset(
    {
        "chat",
        "files",
        "web_search",
        "scheduled_tasks",
        "catalog_models",
        "catalog_skills",
        "catalog_plugins",
        "skills",
        "tools",
        "connectors",
        "sandbox",
    }
)
WORKBENCH_LIMIT_MAXIMUMS = {
    "customer_concurrency": 1000,
    "user_concurrency": 100,
    "max_runtime_seconds": 86400,
    "max_reasoning_rounds": 1000,
    "max_output_tokens": 1_000_000,
    "web_search_per_turn": 1000,
    "max_file_bytes": 1 << 30,
}
WORKBENCH_CONTRACT_VERSION_HEADER = "X-Workbench-Contract-Version"
WORKBENCH_CONTRACT_VERSION = "1"
WORKBENCH_APP_CONTEXT_REQUIRED_FIELDS = frozenset(
    {
        "application_id",
        "app_profile_id",
        "config_version",
        "auth_epoch",
        "vendor",
        "service_vendor",
        "app_id",
        "app_key",
        "space_id",
        "template_agent_id",
        "secret_id",
        "secret_key",
        "capabilities",
        "limits",
    }
)
WORKBENCH_APP_CONTEXT_OPTIONAL_FIELDS = frozenset(
    {
        "customer_id",
        "provider_environment",
        "region",
        "expires_at",
        "provider_app_mode",
        "runtime_profile",
        "execution_enabled",
    }
)
WORKBENCH_RESOURCE_BIND_RESPONSE_REQUIRED_FIELDS = frozenset(
    {
        "resource_binding_id",
        "binding_id",
        "resource_type",
        "resource_id",
        "source_version",
        "status",
        "idempotent",
    }
)
WORKBENCH_RESOURCE_BIND_RESPONSE_OPTIONAL_FIELDS = frozenset(
    {"parent_resource_type", "parent_resource_id"}
)


class WorkbenchControlError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class WorkbenchIdentityContext:
    binding_id: str
    canonical_subject: str
    customer_id: int
    new_api_user_id: int
    auth_epoch: int
    display_name: str
    application_id: str
    app_profile_id: str
    access_mode: str
    config_version: int = 0


@dataclass(frozen=True)
class WorkbenchAppContext:
    application_id: str
    app_profile_id: str
    config_version: int
    auth_epoch: int
    vendor: str
    service_vendor: str
    app_id: str
    app_key: str
    space_id: str
    template_agent_id: str
    secret_id: str
    secret_key: str
    capabilities: tuple[str, ...] = ()
    limits: dict[str, int] | None = None
    provider_app_mode: int = 4
    runtime_profile: str = CLAW_DYNAMIC_V2
    execution_enabled: bool = True

    @property
    def runtime(self) -> WorkbenchRuntimeProfile:
        return validate_runtime_profile(
            self.provider_app_mode,
            self.runtime_profile,
            self.execution_enabled,
        )


@dataclass(frozen=True, repr=False)
class WorkbenchAppMigrationTask:
    migration_job_id: str
    migration_member_id: str
    attempt_id: str
    lease_token: str
    lease_expires_at: datetime
    binding_id: str
    adp_account_id: str
    customer_id: int
    source_application_id: str
    target_application_id: str
    target_app_profile_id: int
    target_config_version: int
    target_config_fingerprint: str
    mode: str
    known_target_agent_id: str
    provider: WorkbenchAppContext
    provider_app_mode: int
    runtime_profile: str
    execution_enabled: bool


class WorkbenchControlClient:
    @staticmethod
    def validated_base_url() -> str:
        raw_url = tagentic_config.WORKBENCH_CONTROL_URL.strip()
        parsed = urlsplit(raw_url)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise WorkbenchControlError("workbench control URL is invalid", 503)
        if parsed.scheme == "http":
            if not tagentic_config.WORKBENCH_ALLOW_INSECURE_CONTROL_HTTP:
                raise WorkbenchControlError(
                    "workbench control URL must use HTTPS",
                    503,
                )
        elif parsed.scheme != "https":
            raise WorkbenchControlError("workbench control URL must use HTTP(S)", 503)
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def tls_context(base_url: str) -> ssl.SSLContext | None:
        if not base_url.startswith("https://"):
            return None

        ca_file = tagentic_config.WORKBENCH_CONTROL_CA_FILE.strip() or None
        cert_file = tagentic_config.WORKBENCH_CONTROL_CLIENT_CERT_FILE.strip()
        key_file = tagentic_config.WORKBENCH_CONTROL_CLIENT_KEY_FILE.strip()
        if bool(cert_file) != bool(key_file):
            raise WorkbenchControlError(
                "workbench control client certificate and key must be configured together",
                503,
            )
        try:
            context = ssl.create_default_context(cafile=ca_file)
            if cert_file:
                context.load_cert_chain(cert_file, key_file)
        except (OSError, ssl.SSLError) as error:
            raise WorkbenchControlError(
                "workbench control TLS configuration is invalid",
                503,
            ) from error
        return context

    @staticmethod
    async def _read_response_body(response: aiohttp.ClientResponse) -> bytes:
        max_bytes = tagentic_config.WORKBENCH_CONTROL_MAX_RESPONSE_BYTES
        if response.content_length is not None and response.content_length > max_bytes:
            raise WorkbenchControlError("claw-control response is too large", 502)

        chunks: list[bytes] = []
        received = 0
        async for chunk in response.content.iter_chunked(min(65536, max_bytes + 1)):
            received += len(chunk)
            if received > max_bytes:
                raise WorkbenchControlError("claw-control response is too large", 502)
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _canonical_body(payload: dict[str, Any] | None) -> bytes:
        if payload is None:
            return b""
        return ujson.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            escape_forward_slashes=False,
        ).encode("utf-8")

    @staticmethod
    def _validated_contract_path(path: str) -> str:
        if (
            not isinstance(path, str)
            or not path.startswith("/api/internal/workbench/")
            or "?" in path
            or "#" in path
            or "\\" in path
            or any(segment in {"", ".", ".."} for segment in path.split("/")[1:])
        ):
            raise WorkbenchControlError(
                "claw-control internal contract path is invalid",
                500,
            )
        return path

    @staticmethod
    def signed_headers(method: str, path: str, body: bytes, timestamp: int, nonce: str) -> dict[str, str]:
        path = WorkbenchControlClient._validated_contract_path(path)
        secret = tagentic_config.WORKBENCH_SERVICE_HMAC_SECRET
        if not secret:
            raise WorkbenchControlError("workbench service HMAC secret is not configured", 503)

        body_hash = hashlib.sha256(body).hexdigest()
        canonical = "\n".join(
            (WORKBENCH_CONTRACT_VERSION, method.upper(), path, str(timestamp), nonce, body_hash)
        )
        signature = hmac.new(
            secret.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            WORKBENCH_CONTRACT_VERSION_HEADER: WORKBENCH_CONTRACT_VERSION,
            "X-Workbench-Service": tagentic_config.WORKBENCH_SERVICE_ID,
            "X-Workbench-Timestamp": str(timestamp),
            "X-Workbench-Nonce": nonce,
            "X-Workbench-Signature": signature,
        }

    @staticmethod
    def verify_signed_response(
        status: int,
        path: str,
        body: bytes,
        request_nonce: str,
        headers: Any,
    ) -> None:
        path = WorkbenchControlClient._validated_contract_path(path)
        contract_version = str(
            headers.get(WORKBENCH_CONTRACT_VERSION_HEADER, "")
        ).strip()
        if contract_version != WORKBENCH_CONTRACT_VERSION:
            raise WorkbenchControlError(
                "claw-control response contract version is unsupported",
                502,
            )
        timestamp = str(headers.get("X-Workbench-Response-Timestamp", "")).strip()
        response_nonce = str(headers.get("X-Workbench-Response-Nonce", "")).strip()
        signature = str(headers.get("X-Workbench-Response-Signature", "")).strip().lower()
        try:
            signed_at = int(timestamp)
        except ValueError as error:
            raise WorkbenchControlError(
                "claw-control response authentication is invalid",
                502,
            ) from error
        allowed_skew = tagentic_config.WORKBENCH_ALLOWED_CLOCK_SKEW_SECONDS
        if (
            not hmac.compare_digest(response_nonce, request_nonce)
            or abs(int(time.time()) - signed_at) > allowed_skew
        ):
            raise WorkbenchControlError(
                "claw-control response authentication is invalid",
                502,
            )

        body_hash = hashlib.sha256(body).hexdigest()
        canonical = "\n".join(
            (
                WORKBENCH_CONTRACT_VERSION,
                str(status),
                path,
                timestamp,
                request_nonce,
                body_hash,
            )
        )
        expected = hmac.new(
            tagentic_config.WORKBENCH_SERVICE_HMAC_SECRET.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected):
            raise WorkbenchControlError(
                "claw-control response signature is invalid",
                502,
            )

    @classmethod
    async def _request(
        cls,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_url = cls.validated_base_url()
        tls_context = cls.tls_context(base_url)

        request_path = cls._validated_contract_path(path)
        body = cls._canonical_body(payload)
        timestamp = int(time.time())
        nonce = uuid.uuid4().hex
        headers = cls.signed_headers(method, request_path, body, timestamp, nonce)
        timeout = aiohttp.ClientTimeout(total=tagentic_config.WORKBENCH_CONTROL_TIMEOUT_SECONDS)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method,
                    f"{base_url}{request_path}",
                    headers=headers,
                    data=body if body else None,
                    ssl=tls_context,
                ) as response:
                    raw = await cls._read_response_body(response)
                    cls.verify_signed_response(
                        response.status,
                        path,
                        raw,
                        nonce,
                        response.headers,
                    )
                    if response.status < 200 or response.status >= 300:
                        if response.status in {401, 403, 404, 409, 410, 422, 429}:
                            status_code = response.status
                        else:
                            status_code = 502
                        raise WorkbenchControlError(
                            f"claw-control rejected request with status {response.status}",
                            status_code,
                        )
        except WorkbenchControlError:
            raise
        except (aiohttp.ClientError, TimeoutError) as error:
            raise WorkbenchControlError("claw-control is unavailable", 503) from error

        try:
            decoded = ujson.loads(raw)
        except ValueError as error:
            raise WorkbenchControlError("claw-control returned invalid JSON") from error

        if not isinstance(decoded, dict):
            raise WorkbenchControlError("claw-control returned an invalid response envelope")
        if decoded.get("success") is False:
            raise WorkbenchControlError("claw-control rejected the request", 403)
        data = decoded.get("data", decoded)
        if not isinstance(data, dict):
            raise WorkbenchControlError("claw-control response data is invalid")
        return data

    @classmethod
    async def consume_ticket(
        cls,
        ticket: str,
        browser_binding: str,
    ) -> WorkbenchIdentityContext:
        data = await cls._request(
            "POST",
            "/api/internal/workbench/tickets/consume",
            payload={"ticket": ticket, "browser_binding": browser_binding},
        )
        return cls._identity_context(data)

    @classmethod
    async def authorize(
        cls,
        *,
        binding_id: str,
        canonical_subject: str,
        auth_epoch: int,
        method: str,
        resource_path: str,
        customer_id: int = 0,
        app_profile_id: int = 0,
        config_version: int = 0,
    ) -> WorkbenchIdentityContext:
        payload = {
            "binding_id": binding_id,
            "canonical_subject": canonical_subject,
            "auth_epoch": auth_epoch,
            "method": method,
            "resource_path": resource_path,
        }
        if customer_id > 0 or app_profile_id > 0 or config_version > 0:
            if customer_id <= 0 or app_profile_id <= 0 or config_version <= 0:
                raise WorkbenchControlError("exact workbench context tuple is incomplete", 503)
            payload.update(
                {
                    "customer_id": customer_id,
                    "app_profile_id": app_profile_id,
                    "config_version": config_version,
                }
            )
        try:
            data = await cls._request(
                "POST",
                "/api/internal/workbench/authz",
                payload=payload,
            )
        except WorkbenchControlError as error:
            WORKBENCH_METRICS.inc(
                "workbench_authz_denied_total",
                reason="denied" if error.status_code in {401, 403, 404, 409, 410, 422, 429} else "error",
            )
            raise
        if "allowed" not in data:
            WORKBENCH_METRICS.inc("workbench_authz_denied_total", reason="error")
            raise WorkbenchControlError(
                "claw-control identity response is incomplete",
                502,
            )
        if data["allowed"] is not True:
            WORKBENCH_METRICS.inc("workbench_authz_denied_total", reason="denied")
            raise WorkbenchControlError("workbench access is not allowed", 403)
        return cls._identity_context(data)

    @classmethod
    async def get_app_context(
        cls,
        *,
        binding_id: str,
        canonical_subject: str,
        auth_epoch: int,
        requested_app_profile_id: int,
        requested_config_version: int,
        purpose: str,
        current_app_profile_id: int = 0,
        current_config_version: int = 0,
    ) -> WorkbenchAppContext:
        payload = {
            "binding_id": binding_id,
            "canonical_subject": canonical_subject,
            "auth_epoch": auth_epoch,
            "requested_app_profile_id": requested_app_profile_id,
            "requested_config_version": requested_config_version,
            "purpose": purpose,
        }
        if purpose == "history_read":
            payload["current_app_profile_id"] = current_app_profile_id
            payload["current_config_version"] = current_config_version
        if (
            not binding_id
            or not canonical_subject
            or auth_epoch <= 0
            or requested_app_profile_id <= 0
            or requested_config_version <= 0
            or purpose not in {"interactive", "scheduled_task", "integration_refresh", "history_read"}
            or (
                purpose == "history_read"
                and (current_app_profile_id <= 0 or current_config_version <= 0)
            )
        ):
            raise WorkbenchControlError("exact App context request is invalid", 503)
        try:
            data = await cls._request(
                "POST",
                "/api/internal/workbench/app-context",
                payload=payload,
            )
        except WorkbenchControlError as error:
            WORKBENCH_METRICS.inc(
                "workbench_gate_failures_total",
                gate="app",
                reason="denied" if error.status_code in {401, 403, 404, 409, 410, 422, 429} else "error",
            )
            raise
        required = WORKBENCH_APP_CONTEXT_REQUIRED_FIELDS
        if required.difference(data):
            raise WorkbenchControlError("claw-control app context is incomplete")
        if set(data).difference(required | WORKBENCH_APP_CONTEXT_OPTIONAL_FIELDS):
            raise WorkbenchControlError("claw-control app context has unknown fields")
        runtime_fields = {
            "provider_app_mode",
            "runtime_profile",
            "execution_enabled",
        }
        present_runtime_fields = runtime_fields.intersection(data)
        if present_runtime_fields and present_runtime_fields != runtime_fields:
            raise WorkbenchControlError("claw-control runtime profile is incomplete")
        try:
            runtime = validate_runtime_profile(
                data.get("provider_app_mode", 4),
                data.get("runtime_profile", CLAW_DYNAMIC_V2),
                data.get("execution_enabled", True),
            )
        except ValueError as error:
            raise WorkbenchControlError(str(error)) from error
        try:
            config_version = int(data["config_version"])
            context_auth_epoch = int(data["auth_epoch"])
        except (TypeError, ValueError) as error:
            raise WorkbenchControlError("claw-control app context version is invalid") from error
        string_values = {key: str(data[key]).strip() for key in required if key not in {"config_version", "auth_epoch"}}
        string_values = {
            key: value
            for key, value in string_values.items()
            if key not in {"capabilities", "limits"}
        }
        raw_capabilities = data.get("capabilities")
        raw_limits = data.get("limits")
        if not isinstance(raw_capabilities, list) or not isinstance(raw_limits, dict):
            raise WorkbenchControlError("claw-control App policy is invalid")
        capabilities = tuple(
            sorted(
                {
                    str(value).strip().lower()
                    for value in raw_capabilities
                    if isinstance(value, str) and value.strip()
                }
            )
        )
        if set(capabilities).difference(WORKBENCH_CAPABILITIES):
            raise WorkbenchControlError("claw-control App capability is unknown")
        limit_names = {
            "customer_concurrency",
            "user_concurrency",
            "max_runtime_seconds",
            "max_reasoning_rounds",
            "max_output_tokens",
            "web_search_per_turn",
            "max_file_bytes",
        }
        if set(raw_limits) != limit_names:
            raise WorkbenchControlError("claw-control App limits are incomplete")
        if any(
            isinstance(raw_limits[key], bool) or not isinstance(raw_limits[key], int)
            for key in limit_names
        ):
            raise WorkbenchControlError("claw-control App limits are invalid")
        limits = {key: raw_limits[key] for key in limit_names}
        required_nonempty_strings = {
            "application_id",
            "app_profile_id",
            "vendor",
            "service_vendor",
            "app_id",
            "app_key",
            "space_id",
            "secret_id",
            "secret_key",
        }
        if runtime.uses_provider_user_agent:
            required_nonempty_strings.add("template_agent_id")
        if (
            not capabilities
            or config_version <= 0
            or context_auth_epoch <= 0
            or any(not string_values[name] for name in required_nonempty_strings)
            or limits["customer_concurrency"] <= 0
            or limits["user_concurrency"] <= 0
            or limits["max_runtime_seconds"] <= 0
            or limits["max_reasoning_rounds"] <= 0
            or limits["max_output_tokens"] <= 0
            or limits["web_search_per_turn"] < 0
            or limits["max_file_bytes"] < 0
            or any(
                limits[name] > maximum
                for name, maximum in WORKBENCH_LIMIT_MAXIMUMS.items()
            )
        ):
            raise WorkbenchControlError("claw-control app context failed validation")
        return WorkbenchAppContext(
            application_id=string_values["application_id"],
            app_profile_id=string_values["app_profile_id"],
            config_version=config_version,
            auth_epoch=context_auth_epoch,
            vendor=string_values["vendor"],
            service_vendor=string_values["service_vendor"],
            app_id=string_values["app_id"],
            app_key=string_values["app_key"],
            space_id=string_values["space_id"],
            template_agent_id=string_values["template_agent_id"],
            secret_id=string_values["secret_id"],
            secret_key=string_values["secret_key"],
            capabilities=capabilities,
            limits=limits,
            provider_app_mode=runtime.provider_app_mode,
            runtime_profile=runtime.name,
            execution_enabled=runtime.execution_enabled,
        )

    @classmethod
    async def confirm_identity(
        cls,
        *,
        binding_id: str,
        canonical_subject: str,
        adp_account_id: str,
        adp_account_version: int,
    ) -> None:
        data = await cls._request(
            "POST",
            "/api/internal/workbench/identities/confirm",
            payload={
                "binding_id": binding_id,
                "canonical_subject": canonical_subject,
                "adp_account_id": adp_account_id,
                "adp_account_version": adp_account_version,
            },
        )
        try:
            confirmed_version = int(data.get("adp_account_version") or 0)
        except (TypeError, ValueError) as error:
            raise WorkbenchControlError(
                "claw-control identity confirmation is invalid",
                502,
            ) from error
        if (
            str(data.get("binding_id") or "").strip() != binding_id
            or str(data.get("canonical_subject") or "").strip() != canonical_subject
            or str(data.get("adp_account_id") or "").strip() != adp_account_id
            or confirmed_version != adp_account_version
        ):
            raise WorkbenchControlError("claw-control identity confirmation is invalid", 502)

    @classmethod
    async def bind_resource(
        cls,
        *,
        binding_id: str,
        canonical_subject: str,
        customer_id: int,
        application_id: str,
        app_profile_id: int,
        config_version: int,
        resource_type: str,
        resource_id: str,
        parent_resource_type: str,
        parent_resource_id: str,
        source_event_id: str,
        source_version: int,
    ) -> dict[str, Any]:
        data = await cls._request(
            "POST",
            "/api/internal/workbench/resources/bind",
            payload={
                "binding_id": binding_id,
                "canonical_subject": canonical_subject,
                "customer_id": customer_id,
                "application_id": application_id,
                "app_profile_id": app_profile_id,
                "config_version": config_version,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "parent_resource_type": parent_resource_type,
                "parent_resource_id": parent_resource_id,
                "source_event_id": source_event_id,
                "source_version": source_version,
            },
        )
        allowed_response_fields = (
            WORKBENCH_RESOURCE_BIND_RESPONSE_REQUIRED_FIELDS
            | WORKBENCH_RESOURCE_BIND_RESPONSE_OPTIONAL_FIELDS
        )
        if WORKBENCH_RESOURCE_BIND_RESPONSE_REQUIRED_FIELDS.difference(data):
            raise WorkbenchControlError(
                "claw-control resource binding response is incomplete",
                502,
            )
        if set(data).difference(allowed_response_fields):
            raise WorkbenchControlError(
                "claw-control resource binding response has unknown fields",
                502,
            )
        try:
            reported_source_version = int(data.get("source_version") or 0)
        except (TypeError, ValueError) as error:
            raise WorkbenchControlError(
                "claw-control resource binding response is invalid",
                502,
            ) from error
        if (
            str(data.get("binding_id") or "").strip() != binding_id
            or str(data.get("resource_type") or "").strip() != resource_type
            or str(data.get("resource_id") or "").strip() != resource_id
            or str(data.get("parent_resource_type") or "").strip()
            != parent_resource_type
            or str(data.get("parent_resource_id") or "").strip()
            != parent_resource_id
            or reported_source_version != source_version
            or str(data.get("status") or "").strip() != "active"
            or not str(data.get("resource_binding_id") or "").startswith("wrb_")
            or not isinstance(data.get("idempotent"), bool)
        ):
            raise WorkbenchControlError("claw-control resource binding response is invalid", 502)
        return {key: data[key] for key in allowed_response_fields if key in data}

    @classmethod
    async def claim_app_migration_task(
        cls,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> WorkbenchAppMigrationTask | None:
        data = await cls._request(
            "POST",
            "/api/internal/workbench/app-migrations/tasks/claim",
            payload={"worker_id": worker_id, "lease_seconds": lease_seconds},
        )
        if set(data) != {"task"}:
            raise WorkbenchControlError("migration claim response is invalid", 502)
        raw = data["task"]
        if raw is None:
            return None
        required_task_fields = {
            "migration_job_id",
            "migration_member_id",
            "attempt_id",
            "lease_token",
            "lease_expires_at",
            "binding_id",
            "adp_account_id",
            "customer_id",
            "source_application_id",
            "target_application_id",
            "target_app_profile_id",
            "target_config_version",
            "target_config_fingerprint",
            "mode",
            "provider",
            "provider_app_mode",
            "runtime_profile",
            "execution_enabled",
        }
        if not isinstance(raw, dict) or not required_task_fields.issubset(raw) or set(raw).difference(required_task_fields | {"known_target_agent_id"}):
            raise WorkbenchControlError("migration task response is invalid", 502)
        provider = raw["provider"]
        if not isinstance(provider, dict) or set(provider) != {
            "vendor",
            "service_vendor",
            "app_id",
            "app_key",
            "space_id",
            "template_agent_id",
            "secret_id",
            "secret_key",
            "provider_app_mode",
            "runtime_profile",
            "execution_enabled",
        }:
            raise WorkbenchControlError("migration provider context is invalid", 502)
        string_fields = (
            "migration_job_id",
            "migration_member_id",
            "attempt_id",
            "lease_token",
            "binding_id",
            "adp_account_id",
            "source_application_id",
            "target_application_id",
            "target_config_fingerprint",
            "mode",
        )
        values = {name: str(raw.get(name) or "").strip() for name in string_fields}
        known_target_agent_id = str(raw.get("known_target_agent_id") or "").strip()
        provider_values = {
            name: str(provider.get(name) or "").strip()
            for name in (
                "vendor",
                "service_vendor",
                "app_id",
                "app_key",
                "space_id",
                "template_agent_id",
                "secret_id",
                "secret_key",
            )
        }
        try:
            customer_id = int(raw["customer_id"])
            app_profile_id = int(raw["target_app_profile_id"])
            config_version = int(raw["target_config_version"])
            runtime_profile = str(raw["runtime_profile"]).strip()
            provider_app_mode = raw["provider_app_mode"]
            execution_enabled = raw["execution_enabled"]
            runtime = validate_runtime_profile(
                provider_app_mode,
                runtime_profile,
                execution_enabled,
            )
            provider_runtime = validate_runtime_profile(
                provider["provider_app_mode"],
                provider["runtime_profile"],
                provider["execution_enabled"],
            )
            lease_expires_at = datetime.fromisoformat(
                str(raw["lease_expires_at"]).replace("Z", "+00:00")
            ).astimezone(UTC)
        except (TypeError, ValueError) as error:
            raise WorkbenchControlError("migration task types are invalid", 502) from error
        if (
            customer_id <= 0
            or app_profile_id <= 0
            or config_version <= 0
            or any(not value for value in values.values())
            or any(
                not value
                for name, value in provider_values.items()
                if name != "template_agent_id"
            )
            or provider_values["vendor"] != "Tencent"
            or provider_values["service_vendor"] != "ChinaTencentADP"
            or provider_values["app_id"] != values["target_application_id"]
            or not values["target_config_fingerprint"].startswith("sha256:")
            or len(values["target_config_fingerprint"]) != 71
            or len(values["lease_token"]) != 64
            or values["mode"] not in {"copy", "readback"}
            or runtime != provider_runtime
            or (
                runtime.uses_provider_user_agent
                and not provider_values["template_agent_id"]
            )
            or (
                runtime.uses_provider_user_agent
                and values["mode"] == "copy"
                and known_target_agent_id != ""
            )
            or (
                runtime.uses_provider_user_agent
                and values["mode"] == "readback"
                and not known_target_agent_id
            )
            or (
                not runtime.uses_provider_user_agent
                and (values["mode"] != "copy" or known_target_agent_id != "")
            )
            or len(known_target_agent_id) > 128
            or any(
                character not in "0123456789abcdef"
                for character in values["lease_token"]
            )
            or lease_expires_at <= datetime.now(UTC)
            or any(len(value) > 255 for value in values.values())
            or any(len(value) > 4096 for value in provider_values.values())
        ):
            raise WorkbenchControlError("migration task failed validation", 502)
        app_context = WorkbenchAppContext(
            application_id=values["target_application_id"],
            app_profile_id=str(app_profile_id),
            config_version=config_version,
            auth_epoch=1,
            vendor=provider_values["vendor"],
            service_vendor=provider_values["service_vendor"],
            app_id=provider_values["app_id"],
            app_key=provider_values["app_key"],
            space_id=provider_values["space_id"],
            template_agent_id=provider_values["template_agent_id"],
            secret_id=provider_values["secret_id"],
            secret_key=provider_values["secret_key"],
            provider_app_mode=runtime.provider_app_mode,
            runtime_profile=runtime.name,
            execution_enabled=runtime.execution_enabled,
        )
        return WorkbenchAppMigrationTask(
            migration_job_id=values["migration_job_id"],
            migration_member_id=values["migration_member_id"],
            attempt_id=values["attempt_id"],
            lease_token=values["lease_token"],
            lease_expires_at=lease_expires_at,
            binding_id=values["binding_id"],
            adp_account_id=values["adp_account_id"],
            customer_id=customer_id,
            source_application_id=values["source_application_id"],
            target_application_id=values["target_application_id"],
            target_app_profile_id=app_profile_id,
            target_config_version=config_version,
            target_config_fingerprint=values["target_config_fingerprint"],
            mode=values["mode"],
            known_target_agent_id=known_target_agent_id,
            provider=app_context,
            provider_app_mode=runtime.provider_app_mode,
            runtime_profile=runtime.name,
            execution_enabled=runtime.execution_enabled,
        )

    @classmethod
    async def report_app_migration_task(
        cls,
        task: WorkbenchAppMigrationTask,
        *,
        status: str,
        target_agent_id: str = "",
        target_readback_hash: str = "",
        error_code: str = "",
    ) -> dict[str, Any]:
        payload = {
            "migration_member_id": task.migration_member_id,
            "attempt_id": task.attempt_id,
            "lease_token": task.lease_token,
            "status": status,
            "target_app_profile_id": (
                task.target_app_profile_id if status == "succeeded" else 0
            ),
            "target_config_version": (
                task.target_config_version if status == "succeeded" else 0
            ),
            "target_config_fingerprint": (
                task.target_config_fingerprint if status == "succeeded" else ""
            ),
            "target_agent_id": target_agent_id,
            "target_readback_hash": target_readback_hash,
            "error_code": error_code,
        }
        data = await cls._request(
            "POST",
            "/api/internal/workbench/app-migrations/tasks/report",
            payload=payload,
        )
        if set(data) != {
            "migration_job_id",
            "migration_member_id",
            "status",
            "job_status",
            "expected_members",
            "succeeded_members",
            "failed_members",
        }:
            raise WorkbenchControlError("migration report response is invalid", 502)
        if (
            str(data["migration_job_id"]) != task.migration_job_id
            or str(data["migration_member_id"]) != task.migration_member_id
            or str(data["status"]) != status
        ):
            raise WorkbenchControlError(
                "migration report acknowledgement mismatched",
                502,
            )
        return data

    @staticmethod
    def _identity_context(data: dict[str, Any]) -> WorkbenchIdentityContext:
        required = {
            "binding_id",
            "canonical_subject",
            "customer_id",
            "new_api_user_id",
            "auth_epoch",
            "application_id",
            "app_profile_id",
            "config_version",
            "access_mode",
            "allowed",
        }
        if required.difference(data):
            raise WorkbenchControlError("claw-control identity response is incomplete")

        try:
            customer_id = int(data["customer_id"])
            new_api_user_id = int(data["new_api_user_id"])
            auth_epoch = int(data["auth_epoch"])
            config_version = int(data["config_version"])
        except (TypeError, ValueError) as error:
            raise WorkbenchControlError("claw-control identity response contains invalid identifiers") from error

        binding_id = str(data["binding_id"]).strip()
        canonical_subject = str(data["canonical_subject"]).strip()
        application_id = str(data["application_id"]).strip()
        app_profile_id = str(data["app_profile_id"]).strip()
        if (
            not binding_id
            or not canonical_subject
            or not application_id
            or not app_profile_id
            or customer_id <= 0
            or new_api_user_id <= 0
            or auth_epoch < 0
            or config_version <= 0
            or data["allowed"] is not True
        ):
            raise WorkbenchControlError("claw-control identity response failed validation")

        expected_suffix = f":customer:{customer_id}:user:{new_api_user_id}"
        if not canonical_subject.startswith("napi:") or not canonical_subject.endswith(expected_suffix):
            raise WorkbenchControlError("claw-control canonical subject does not match the identity")

        display_name = str(data.get("display_name") or f"User-{new_api_user_id}").strip()
        access_mode = str(data["access_mode"]).strip().lower()
        if access_mode == "readonly":
            access_mode = "read_only"
        if access_mode not in {"active", "read_only"}:
            raise WorkbenchControlError("claw-control access mode is invalid")
        return WorkbenchIdentityContext(
            binding_id=binding_id,
            canonical_subject=canonical_subject,
            customer_id=customer_id,
            new_api_user_id=new_api_user_id,
            auth_epoch=auth_epoch,
            display_name=display_name[:255],
            application_id=application_id,
            app_profile_id=app_profile_id,
            access_mode=access_mode,
            config_version=config_version,
        )
