from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from config import tagentic_config
from core.workbench_control import WorkbenchAppContext


class WorkbenchIntegrationPolicyError(RuntimeError):
    def __init__(self, message: str, status_code: int = 403):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class IntegrationResource:
    kind: str
    resource_id: str
    parent_id: str
    name_zh: str
    name_en: str
    provider_id: str
    requires_oauth: bool


@dataclass(frozen=True)
class OAuthProvider:
    provider_id: str
    authorization_url: str
    token_url: str
    revocation_url: str
    client_id: str
    client_secret_ref: str
    token_auth_method: str
    allowed_scopes: tuple[str, ...]
    allowed_hosts: tuple[str, ...]


class WorkbenchIntegrationPolicy:
    """Server-owned allowlist and package intersection for integrations."""

    RESOURCE_CAPABILITIES = {
        "skill": ("catalog_skills", "skills"),
        "plugin": ("catalog_plugins", "tools"),
        "tool": ("catalog_plugins", "tools"),
        "connector": ("catalog_plugins", "connectors"),
    }
    _ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    _PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
    _SECRET_REF = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
    _SCOPE = re.compile(r"^[\x21-\x7e]{1,128}$")

    @classmethod
    def require_enabled(cls) -> None:
        if not (
            tagentic_config.WORKBENCH_MODE
            and tagentic_config.WORKBENCH_INTEGRATIONS_ENABLED
        ):
            raise WorkbenchIntegrationPolicyError("workbench integrations are disabled", 404)

    @staticmethod
    def _load(raw: str, file_name: str, label: str) -> dict[str, Any]:
        inline = str(raw or "").strip()
        file_name = str(file_name or "").strip()
        if file_name:
            if inline not in {"", "{}"}:
                raise WorkbenchIntegrationPolicyError(
                    f"{label} inline and file settings are mutually exclusive", 503
                )
            try:
                path = Path(file_name).resolve(strict=True)
                if not path.is_file() or path.stat().st_size > 1024 * 1024:
                    raise OSError("invalid mounted configuration file")
                inline = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise WorkbenchIntegrationPolicyError(
                    f"{label} file is invalid", 503
                ) from error
        if len(inline.encode("utf-8")) > 1024 * 1024:
            raise WorkbenchIntegrationPolicyError(f"{label} is too large", 503)
        try:
            value = json.loads(inline or "{}")
        except (TypeError, ValueError) as error:
            raise WorkbenchIntegrationPolicyError(f"{label} is invalid", 503) from error
        if not isinstance(value, dict):
            raise WorkbenchIntegrationPolicyError(f"{label} is invalid", 503)
        return value

    @classmethod
    def catalog(cls, app_context: WorkbenchAppContext) -> tuple[IntegrationResource, ...]:
        cls.require_enabled()
        root = cls._load(
            tagentic_config.WORKBENCH_INTEGRATION_ALLOWLIST_JSON,
            tagentic_config.WORKBENCH_INTEGRATION_ALLOWLIST_FILE,
            "integration allowlist",
        )
        if set(root).difference({"applications"}) or not isinstance(root.get("applications", {}), dict):
            raise WorkbenchIntegrationPolicyError("integration allowlist is invalid", 503)
        application = root.get("applications", {}).get(app_context.application_id)
        if application is None:
            return ()
        if not isinstance(application, dict) or set(application).difference({"config_versions", "resources"}):
            raise WorkbenchIntegrationPolicyError("integration application allowlist is invalid", 503)
        versions = application.get("config_versions", [])
        if versions:
            if (
                not isinstance(versions, list)
                or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in versions)
            ):
                raise WorkbenchIntegrationPolicyError("integration config versions are invalid", 503)
            if app_context.config_version not in versions:
                return ()
        items = application.get("resources", [])
        if not isinstance(items, list) or len(items) > 500:
            raise WorkbenchIntegrationPolicyError("integration resources are invalid", 503)
        capabilities = set(app_context.capabilities)
        result: list[IntegrationResource] = []
        seen: set[tuple[str, str, str]] = set()
        for item in items:
            if not isinstance(item, dict) or set(item).difference(
                {"kind", "id", "parent_id", "name_zh", "name_en", "provider_id", "requires_oauth"}
            ):
                raise WorkbenchIntegrationPolicyError("integration resource is invalid", 503)
            kind = str(item.get("kind") or "").strip().lower()
            resource_id = str(item.get("id") or "").strip()
            parent_id = str(item.get("parent_id") or "").strip()
            provider_id = str(item.get("provider_id") or "").strip().lower()
            name_zh = str(item.get("name_zh") or resource_id).strip()
            name_en = str(item.get("name_en") or resource_id).strip()
            requires_oauth = item.get("requires_oauth", False)
            if (
                kind not in cls.RESOURCE_CAPABILITIES
                or not cls._ID.fullmatch(resource_id)
                or (parent_id and not cls._ID.fullmatch(parent_id))
                or not name_zh
                or not name_en
                or len(name_zh) > 128
                or len(name_en) > 128
                or not isinstance(requires_oauth, bool)
                or (kind == "tool" and not parent_id)
                or (kind != "tool" and parent_id)
                or (kind == "connector" and (not provider_id or not cls._PROVIDER_ID.fullmatch(provider_id)))
                or (kind != "connector" and provider_id)
                or (requires_oauth and kind != "connector")
            ):
                raise WorkbenchIntegrationPolicyError("integration resource is invalid", 503)
            key = (kind, resource_id, parent_id)
            if key in seen:
                raise WorkbenchIntegrationPolicyError("integration resource is duplicated", 503)
            seen.add(key)
            if all(required in capabilities for required in cls.RESOURCE_CAPABILITIES[kind]):
                result.append(
                    IntegrationResource(
                        kind,
                        resource_id,
                        parent_id,
                        name_zh,
                        name_en,
                        provider_id,
                        requires_oauth,
                    )
                )
        return tuple(result)

    @classmethod
    def resource(
        cls,
        app_context: WorkbenchAppContext,
        kind: object,
        resource_id: object,
        parent_id: object = "",
    ) -> IntegrationResource:
        normalized_kind = str(kind or "").strip().lower()
        normalized_id = str(resource_id or "").strip()
        normalized_parent = str(parent_id or "").strip()
        for resource in cls.catalog(app_context):
            if (
                resource.kind == normalized_kind
                and resource.resource_id == normalized_id
                and resource.parent_id == normalized_parent
            ):
                return resource
        raise WorkbenchIntegrationPolicyError("integration resource is not allowed", 404)

    @classmethod
    def provider(cls, provider_id: object) -> OAuthProvider:
        cls.require_enabled()
        provider_id = str(provider_id or "").strip().lower()
        if not cls._PROVIDER_ID.fullmatch(provider_id):
            raise WorkbenchIntegrationPolicyError("OAuth provider is not allowed", 404)
        root = cls._load(
            tagentic_config.WORKBENCH_OAUTH_PROVIDERS_JSON,
            tagentic_config.WORKBENCH_OAUTH_PROVIDERS_FILE,
            "OAuth provider allowlist",
        )
        if set(root).difference({"providers"}) or not isinstance(root.get("providers", {}), dict):
            raise WorkbenchIntegrationPolicyError("OAuth provider allowlist is invalid", 503)
        value = root.get("providers", {}).get(provider_id)
        allowed_fields = {
            "authorization_url",
            "token_url",
            "revocation_url",
            "client_id",
            "client_secret_ref",
            "token_auth_method",
            "allowed_scopes",
            "allowed_hosts",
        }
        if not isinstance(value, dict) or set(value).difference(allowed_fields):
            raise WorkbenchIntegrationPolicyError("OAuth provider is not allowed", 404)
        client_id = str(value.get("client_id") or "").strip()
        secret_ref = str(value.get("client_secret_ref") or "").strip()
        auth_method = str(value.get("token_auth_method") or "client_secret_basic").strip()
        scopes = value.get("allowed_scopes")
        hosts = value.get("allowed_hosts")
        if (
            not client_id
            or len(client_id) > 512
            or auth_method not in {"client_secret_basic", "client_secret_post", "none"}
            or (auth_method != "none" and not cls._SECRET_REF.fullmatch(secret_ref))
            or (auth_method == "none" and secret_ref)
            or not isinstance(scopes, list)
            or not scopes
            or len(scopes) > 32
            or any(not isinstance(scope, str) or not cls._SCOPE.fullmatch(scope) for scope in scopes)
            or len(scopes) != len(set(scopes))
            or not isinstance(hosts, list)
            or not hosts
            or len(hosts) > 8
        ):
            raise WorkbenchIntegrationPolicyError("OAuth provider metadata is invalid", 503)
        normalized_hosts: list[str] = []
        for host in hosts:
            normalized = str(host or "").strip().lower().rstrip(".")
            if not normalized or len(normalized) > 253 or "*" in normalized or ":" in normalized:
                raise WorkbenchIntegrationPolicyError("OAuth provider hosts are invalid", 503)
            normalized_hosts.append(normalized)
        urls = {
            "authorization_url": cls._oauth_url(value.get("authorization_url"), normalized_hosts),
            "token_url": cls._oauth_url(value.get("token_url"), normalized_hosts),
        }
        revocation_raw = str(value.get("revocation_url") or "").strip()
        revocation_url = cls._oauth_url(revocation_raw, normalized_hosts) if revocation_raw else ""
        return OAuthProvider(
            provider_id,
            urls["authorization_url"],
            urls["token_url"],
            revocation_url,
            client_id,
            secret_ref,
            auth_method,
            tuple(scopes),
            tuple(normalized_hosts),
        )

    @staticmethod
    def _oauth_url(value: object, allowed_hosts: list[str]) -> str:
        raw = str(value or "").strip()
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except (ValueError, UnicodeError) as error:
            raise WorkbenchIntegrationPolicyError("OAuth endpoint URL is invalid", 503) from error
        host = str(parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "https"
            or not host
            or host not in allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port not in {None, 443}
            or not parsed.path.startswith("/")
        ):
            raise WorkbenchIntegrationPolicyError("OAuth endpoint URL is invalid", 503)
        return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))

    @classmethod
    def client_secret(cls, provider: OAuthProvider) -> str:
        if provider.token_auth_method == "none":
            return ""
        if not cls._SECRET_REF.fullmatch(provider.client_secret_ref):
            raise WorkbenchIntegrationPolicyError("OAuth client secret reference is invalid", 503)
        secret_dir = str(tagentic_config.WORKBENCH_OAUTH_SECRET_DIR or "").strip()
        try:
            configured_root = Path(secret_dir)
            root_metadata = configured_root.lstat()
            if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
                raise OSError("invalid OAuth client secret directory")
            root = configured_root.resolve(strict=True)
            candidate = root / provider.client_secret_ref
            candidate_metadata = candidate.lstat()
            if (
                stat.S_ISLNK(candidate_metadata.st_mode)
                or not stat.S_ISREG(candidate_metadata.st_mode)
                or candidate_metadata.st_size > 4096
                or candidate.resolve(strict=True).parent != root
            ):
                raise OSError("invalid OAuth client secret file")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(candidate, flags)
            try:
                opened_metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened_metadata.st_mode)
                    or opened_metadata.st_size > 4096
                ):
                    raise OSError("invalid OAuth client secret file")
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    secret = handle.read(4097).decode("utf-8").strip()
                if len(secret.encode("utf-8")) > 4096:
                    raise OSError("invalid OAuth client secret file")
            finally:
                os.close(descriptor)
        except (OSError, UnicodeError) as error:
            raise WorkbenchIntegrationPolicyError(
                "OAuth client secret is unavailable", 503
            ) from error
        if len(secret) < 16 or len(secret) > 4096 or "\x00" in secret:
            raise WorkbenchIntegrationPolicyError("OAuth client secret is unavailable", 503)
        return secret

    @classmethod
    def callback_url(cls, provider_id: str) -> str:
        provider_id = str(provider_id or "").strip().lower()
        if not cls._PROVIDER_ID.fullmatch(provider_id):
            raise WorkbenchIntegrationPolicyError("OAuth provider is invalid", 503)
        raw = str(tagentic_config.WORKBENCH_PUBLIC_BASE_URL or "").strip().rstrip("/")
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except (ValueError, UnicodeError) as error:
            raise WorkbenchIntegrationPolicyError("workbench public base URL is invalid", 503) from error
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port not in {None, 443}
            or ".." in parsed.path.split("/")
        ):
            raise WorkbenchIntegrationPolicyError("workbench public base URL is invalid", 503)
        return f"{raw}/integrations/oauth/callback/{quote(provider_id, safe='')}"

    @classmethod
    def callback_cookie_path(cls) -> str:
        raw = str(tagentic_config.WORKBENCH_PUBLIC_BASE_URL or "").strip().rstrip("/")
        parsed = urlsplit(raw)
        # callback_url performs the complete origin/path validation.
        cls.callback_url("cookie-path-validation")
        base_path = parsed.path.rstrip("/")
        return f"{base_path}/integrations/oauth/callback/" or "/"
