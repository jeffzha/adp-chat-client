from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import ipaddress
import json
import secrets
import socket
import ssl
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app_factory import TAgenticApp
from config import tagentic_config
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_identity import CoreWorkbenchIdentity, WorkbenchIdentityError
from core.workbench_integration_crypto import WorkbenchIntegrationCrypto
from core.workbench_integration_policy import (
    IntegrationResource,
    OAuthProvider,
    WorkbenchIntegrationPolicy,
    WorkbenchIntegrationPolicyError,
)
from core.workbench_provider_integrations import (
    ProviderAgentIntegrationState,
    ProviderIntegrationContractError,
    ProviderPluginDefinition,
    exact_modify_lists,
    parse_agent_state,
    parse_plugin_detail,
    validate_executable_state,
)
from model.workbench import WorkbenchAgentBinding, WorkbenchTurn
from model.workbench_integration import (
    WorkbenchConnectorCredential,
    WorkbenchConnectorScope,
    WorkbenchIntegrationAudit,
    WorkbenchIntegrationBinding,
    WorkbenchOAuthRevocation,
    WorkbenchOAuthState,
)
from util.database import db_connection


class WorkbenchIntegrationError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400, code: str = "invalid_request"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class OAuthRedirect:
    location: str


class _PinnedResolver(AbstractResolver):
    def __init__(self, pins: dict[str, tuple[str, ...]]):
        self._pins = pins

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
        addresses = self._pins.get(host.lower().rstrip("."))
        if not addresses:
            raise OSError("OAuth DNS host was not pinned")
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": socket.AF_INET6 if ":" in address else socket.AF_INET,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST,
            }
            for address in addresses
            if family in {socket.AF_UNSPEC, socket.AF_INET6 if ":" in address else socket.AF_INET}
        ]

    async def close(self) -> None:
        return None


class WorkbenchIntegrations:
    """Safe integration foundation bounded by the published ADP 2026-05-20 contract."""

    _RESERVED_AUTH_QUERY = frozenset(
        {
            "client_id",
            "redirect_uri",
            "response_type",
            "scope",
            "state",
            "nonce",
            "code_challenge",
            "code_challenge_method",
        }
    )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    @staticmethod
    def _canonical(value: Any) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _account_key(account_id: object) -> uuid.UUID:
        try:
            return uuid.UUID(str(account_id))
        except (ValueError, AttributeError, TypeError) as error:
            raise WorkbenchIntegrationError(
                "workbench account scope is invalid", 403, "scope_invalid"
            ) from error

    @staticmethod
    def _scope_filters(
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> tuple[Any, ...]:
        return (
            WorkbenchIntegrationBinding.BindingId == identity.binding_id,
            WorkbenchIntegrationBinding.AccountId == WorkbenchIntegrations._account_key(account_id),
            WorkbenchIntegrationBinding.CustomerId == identity.customer_id,
            WorkbenchIntegrationBinding.NewApiUserId == identity.new_api_user_id,
            WorkbenchIntegrationBinding.CanonicalSubject == identity.canonical_subject,
            WorkbenchIntegrationBinding.ApplicationId == app_context.application_id,
            WorkbenchIntegrationBinding.ProviderAppId == app_context.app_id,
            WorkbenchIntegrationBinding.AppProfileId == app_context.app_profile_id,
            WorkbenchIntegrationBinding.ConfigVersion == app_context.config_version,
            WorkbenchIntegrationBinding.AuthEpoch == app_context.auth_epoch,
        )

    @staticmethod
    def _session_id_digest(claims: dict[str, Any]) -> str:
        session_id = str(claims.get("sid") or "")
        if (
            len(session_id) < 40
            or len(session_id) > 128
            or not all(char.isalnum() or char in "-_" for char in session_id)
        ):
            raise WorkbenchIntegrationError(
                "workbench browser session is invalid", 401, "session_invalid"
            )
        return hashlib.sha256(session_id.encode("ascii")).hexdigest()

    @classmethod
    async def _lock_connector_scope(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        provider_id: str,
        connector_id: str,
    ) -> WorkbenchConnectorScope:
        base_filters = (
            WorkbenchConnectorScope.BindingId == identity.binding_id,
            WorkbenchConnectorScope.ApplicationId == app_context.application_id,
            WorkbenchConnectorScope.AppProfileId == app_context.app_profile_id,
            WorkbenchConnectorScope.ConfigVersion == app_context.config_version,
            WorkbenchConnectorScope.ProviderId == provider_id,
            WorkbenchConnectorScope.ConnectorId == connector_id,
        )
        row = (
            await db.execute(
                select(WorkbenchConnectorScope).where(*base_filters).with_for_update()
            )
        ).scalar()
        if row is None:
            candidate = WorkbenchConnectorScope(
                ScopeId=f"wcs_{uuid.uuid4().hex}",
                BindingId=identity.binding_id,
                AccountId=cls._account_key(account_id),
                CustomerId=identity.customer_id,
                NewApiUserId=identity.new_api_user_id,
                CanonicalSubject=identity.canonical_subject,
                ApplicationId=app_context.application_id,
                ProviderAppId=app_context.app_id,
                AppProfileId=app_context.app_profile_id,
                ConfigVersion=app_context.config_version,
                AuthEpoch=app_context.auth_epoch,
                ProviderId=provider_id,
                ConnectorId=connector_id,
                Generation=0,
            )
            try:
                async with db.begin_nested():
                    db.add(candidate)
                    await db.flush()
                row = candidate
            except IntegrityError:
                row = (
                    await db.execute(
                        select(WorkbenchConnectorScope)
                        .where(*base_filters)
                        .with_for_update()
                    )
                ).scalar()
                if row is None:
                    raise WorkbenchIntegrationError(
                        "connector scope could not be locked", 503, "scope_unavailable"
                    )
        current = (
            row.AccountId == cls._account_key(account_id)
            and row.CustomerId == identity.customer_id
            and row.NewApiUserId == identity.new_api_user_id
            and row.CanonicalSubject == identity.canonical_subject
            and row.ProviderAppId == app_context.app_id
            and row.AuthEpoch == app_context.auth_epoch
        )
        if not current:
            row.Generation += 1
            row.AccountId = cls._account_key(account_id)
            row.CustomerId = identity.customer_id
            row.NewApiUserId = identity.new_api_user_id
            row.CanonicalSubject = identity.canonical_subject
            row.ProviderAppId = app_context.app_id
            row.AuthEpoch = app_context.auth_epoch
            db.add(row)
        return row

    @staticmethod
    async def _revoke_pending_oauth(
        db: AsyncSession,
        scope: WorkbenchConnectorScope,
        *,
        now: datetime,
    ) -> None:
        await db.execute(
            update(WorkbenchOAuthState)
            .where(
                WorkbenchOAuthState.BindingId == scope.BindingId,
                WorkbenchOAuthState.ApplicationId == scope.ApplicationId,
                WorkbenchOAuthState.AppProfileId == scope.AppProfileId,
                WorkbenchOAuthState.ConfigVersion == scope.ConfigVersion,
                WorkbenchOAuthState.ProviderId == scope.ProviderId,
                WorkbenchOAuthState.ConnectorId == scope.ConnectorId,
                WorkbenchOAuthState.Status == "pending",
            )
            .values(Status="superseded", ConsumedAt=now)
        )

    @classmethod
    async def require_recent_reauthentication(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        session_claims: dict[str, Any],
    ) -> None:
        try:
            await CoreWorkbenchIdentity.require_browser_session(
                db,
                claims=session_claims,
                account_id=account_id,
                identity=identity,
                maximum_age_seconds=tagentic_config.WORKBENCH_INTEGRATION_REAUTH_SECONDS,
            )
        except WorkbenchIdentityError as error:
            raise WorkbenchIntegrationError(
                "recent workbench authentication is required", 401, "reauth_required"
            ) from error
        if identity.access_mode != "active":
            raise WorkbenchIntegrationError(
                "read-only workbench access cannot change integrations", 403, "read_only"
            )

    @classmethod
    async def catalog(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> dict[str, Any]:
        resources = WorkbenchIntegrationPolicy.catalog(app_context)
        provider_definitions = await cls._provider_definitions(
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            resources=resources,
        )
        bindings = (
            await db.execute(
                select(WorkbenchIntegrationBinding).where(
                    *cls._scope_filters(account_id, identity, app_context)
                )
            )
        ).scalars().all()
        credentials = (
            await db.execute(
                select(WorkbenchConnectorCredential).where(
                    WorkbenchConnectorCredential.BindingId == identity.binding_id,
                    WorkbenchConnectorCredential.AccountId == cls._account_key(account_id),
                    WorkbenchConnectorCredential.CustomerId == identity.customer_id,
                    WorkbenchConnectorCredential.NewApiUserId == identity.new_api_user_id,
                    WorkbenchConnectorCredential.CanonicalSubject == identity.canonical_subject,
                    WorkbenchConnectorCredential.ApplicationId == app_context.application_id,
                    WorkbenchConnectorCredential.ProviderAppId == app_context.app_id,
                    WorkbenchConnectorCredential.AppProfileId == app_context.app_profile_id,
                    WorkbenchConnectorCredential.ConfigVersion == app_context.config_version,
                    WorkbenchConnectorCredential.AuthEpoch == app_context.auth_epoch,
                )
            )
        ).scalars().all()
        binding_map = {
            (row.ResourceKind, row.ResourceId, row.ParentResourceId): row for row in bindings
        }
        credential_map = {(row.ProviderId, row.ConnectorId): row for row in credentials}
        return {
            "contract_version": "adp-2026-05-20",
            "provider_contract_retrieved_at": "2026-08-10",
            "resources": [
                cls._project_resource(
                    resource,
                    binding_map.get((resource.kind, resource.resource_id, resource.parent_id)),
                    credential_map.get((resource.provider_id, resource.resource_id)),
                    provider_definitions,
                )
                for resource in resources
            ],
            "execution_policy": {
                "skill": "provider_binding_supported_execution_blocked_by_limits",
                "plugin": "provider_managed_read_only_tools",
                "tool": "provider_managed_read_only_tools",
                "connector": "provider_managed_read_only_tools",
            },
        }

    @classmethod
    def _project_resource(
        cls,
        resource: IntegrationResource,
        binding: WorkbenchIntegrationBinding | None,
        credential: WorkbenchConnectorCredential | None,
        provider_definitions: dict[str, ProviderPluginDefinition],
    ) -> dict[str, Any]:
        definition = provider_definitions.get(
            resource.parent_id if resource.kind == "tool" else resource.resource_id
        )
        blocked_reason = cls._definition_blocked_reason(resource, definition)
        connected = credential is not None and credential.Status == "active"
        return {
            "kind": resource.kind,
            "resource_id": resource.resource_id,
            "parent_resource_id": resource.parent_id or None,
            "name_zh": resource.name_zh,
            "name_en": resource.name_en,
            "provider_id": resource.provider_id or None,
            "requires_oauth": False,
            "authorization_mode": (
                "developer_oauth"
                if definition is not None and definition.auth_type == 3
                else "provider_managed"
            ),
            "binding_status": binding.Status if binding is not None else "unbound",
            "provider_sync_status": (
                binding.ProviderSyncStatus if binding is not None else "not_synced"
            ),
            "connection": cls._project_credential(credential) if connected else {
                "status": credential.Status if credential is not None else "disconnected",
                "execution_status": "not_used_for_agent_binding",
            },
            "can_bind": blocked_reason is None,
            "blocked_reason": blocked_reason,
        }

    @classmethod
    async def _provider_definitions(
        cls,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        resources: tuple[IntegrationResource, ...],
    ) -> dict[str, ProviderPluginDefinition]:
        del account_id, identity
        plugin_ids = sorted(
            {
                resource.parent_id if resource.kind == "tool" else resource.resource_id
                for resource in resources
                if resource.kind in {"plugin", "tool", "connector"}
            }
        )
        if not plugin_ids:
            return {}
        vendor = TAgenticApp.get_app().get_vendor_app(app_context.application_id)
        available: set[str] = set()
        for offset in range(0, len(plugin_ids), 8):
            batch = plugin_ids[offset : offset + 8]
            response = await vendor.forward_request(
                "DescribePluginSummaryList",
                {
                    "SpaceId": app_context.space_id,
                    "Module": 3,
                    "FilterList": [{"Name": "PluginId", "ValueList": batch}],
                    "PageNumber": 0,
                    "PageSize": 50,
                },
            )
            items = response.get("PluginList") if isinstance(response, dict) else None
            total = response.get("TotalCount") if isinstance(response, dict) else None
            if (
                not isinstance(items, list)
                or isinstance(total, bool)
                or not isinstance(total, int)
                or total != len(items)
            ):
                raise WorkbenchIntegrationError(
                    "provider Plugin summary is invalid", 502, "provider_contract_invalid"
                )
            for item in items:
                plugin_id = item.get("PluginId") if isinstance(item, dict) else None
                if plugin_id not in batch or plugin_id in available:
                    raise WorkbenchIntegrationError(
                        "provider Plugin summary escaped the allowlist",
                        502,
                        "provider_contract_invalid",
                    )
                available.add(plugin_id)
        result: dict[str, ProviderPluginDefinition] = {}
        for plugin_id in sorted(available):
            try:
                detail = await vendor.forward_request(
                    "DescribePlugin",
                    {
                        "PluginId": plugin_id,
                        "SpaceId": app_context.space_id,
                        "FieldMask": {
                            "Paths": [
                                "PluginId",
                                "Profile",
                                "Config",
                                "Status",
                                "ToolList",
                            ]
                        },
                    },
                )
                result[plugin_id] = parse_plugin_detail(detail, plugin_id)
            except ProviderIntegrationContractError:
                # Structurally incomplete provider resources stay visible as
                # blocked catalog entries, but can never reach ModifyAgent.
                continue
        return result

    @staticmethod
    def _definition_blocked_reason(
        resource: IntegrationResource,
        definition: ProviderPluginDefinition | None,
    ) -> str | None:
        if resource.kind == "skill":
            return None
        if definition is None or definition.status != 1:
            return "provider_config_incomplete"
        if definition.auth_type not in {0, 1, 2}:
            return "oauth_execution_unsupported"
        if resource.kind == "connector" and definition.plugin_class != 1:
            return "provider_kind_mismatch"
        if resource.kind in {"plugin", "tool"} and definition.plugin_class != 0:
            return "provider_kind_mismatch"
        tools = definition.tools
        if resource.kind == "tool":
            selected = definition.tool(resource.resource_id)
            if selected is None:
                return "provider_config_incomplete"
            tools = (selected,)
        if not tools:
            return "provider_config_incomplete"
        if any(tool.access_mode != 1 for tool in tools):
            return "tool_access_mode_unsupported"
        return None

    @staticmethod
    def _project_credential(row: WorkbenchConnectorCredential) -> dict[str, Any]:
        return {
            "credential_id": row.CredentialId,
            "provider_id": row.ProviderId,
            "connector_id": row.ConnectorId,
            "status": row.Status,
            "revocation_status": row.RevocationStatus,
            "granted_scopes": json.loads(row.GrantedScopesJson),
            "token_type": row.TokenType,
            "expires_at": row.ExpiresAt.isoformat() + "Z" if row.ExpiresAt else None,
            "connected_at": row.ConnectedAt.isoformat() + "Z",
            "execution_status": "execution_blocked_contract",
        }

    @classmethod
    async def start_oauth(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        connector_id: object,
        session_claims: dict[str, Any],
    ) -> dict[str, Any]:
        await cls.require_recent_reauthentication(
            db,
            account_id=account_id,
            identity=identity,
            session_claims=session_claims,
        )
        resource = WorkbenchIntegrationPolicy.resource(
            app_context, "connector", connector_id
        )
        if not resource.requires_oauth:
            raise WorkbenchIntegrationError(
                "connector does not use user OAuth", 409, "oauth_not_required"
            )
        provider = WorkbenchIntegrationPolicy.provider(resource.provider_id)
        if not provider.revocation_url:
            raise WorkbenchIntegrationError(
                "OAuth provider revocation contract is required",
                503,
                "revocation_contract_missing",
            )
        redirect_uri = WorkbenchIntegrationPolicy.callback_url(provider.provider_id)
        session_id_digest = cls._session_id_digest(session_claims)
        parsed = urlsplit(provider.authorization_url)
        existing = parse_qsl(parsed.query, keep_blank_values=True)
        if any(key in cls._RESERVED_AUTH_QUERY for key, _ in existing):
            raise WorkbenchIntegrationError(
                "OAuth authorization URL conflicts with required parameters",
                503,
                "provider_config_invalid",
            )
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        state_part = secrets.token_urlsafe(32)
        nonce_part = secrets.token_urlsafe(32)
        browser_cookie = secrets.token_urlsafe(32)
        state = f"wbo1.{state_part}.{nonce_part}"
        verifier_ciphertext, key_id = WorkbenchIntegrationCrypto.encrypt(
            "oauth-state", verifier.encode("ascii")
        )
        now = cls._now()
        expires_at = now + timedelta(seconds=tagentic_config.WORKBENCH_OAUTH_STATE_TTL_SECONDS)
        scope = await cls._lock_connector_scope(
            db,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            provider_id=provider.provider_id,
            connector_id=resource.resource_id,
        )
        scope.Generation += 1
        db.add(scope)
        await cls._revoke_pending_oauth(db, scope, now=now)
        row = WorkbenchOAuthState(
            OAuthStateId=f"wos_{uuid.uuid4().hex}",
            StateDigest=hashlib.sha256(state_part.encode("ascii")).hexdigest(),
            NonceDigest=hashlib.sha256(nonce_part.encode("ascii")).hexdigest(),
            BrowserCookieDigest=hashlib.sha256(browser_cookie.encode("ascii")).hexdigest(),
            SessionIdDigest=session_id_digest,
            VerifierCiphertext=verifier_ciphertext,
            EncryptionKeyId=key_id,
            BindingId=identity.binding_id,
            AccountId=cls._account_key(account_id),
            CustomerId=identity.customer_id,
            NewApiUserId=identity.new_api_user_id,
            CanonicalSubject=identity.canonical_subject,
            ApplicationId=app_context.application_id,
            ProviderAppId=app_context.app_id,
            AppProfileId=app_context.app_profile_id,
            ConfigVersion=app_context.config_version,
            AuthEpoch=app_context.auth_epoch,
            ProviderId=provider.provider_id,
            ConnectorId=resource.resource_id,
            ConnectionGeneration=scope.Generation,
            RedirectUri=redirect_uri,
            RequestedScopesJson=json.dumps(list(provider.allowed_scopes), separators=(",", ":")),
            Status="pending",
            ExpiresAt=expires_at,
        )
        db.add(row)
        cls._audit(
            db,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            action="oauth_start",
            resource_kind="connector",
            resource_id=resource.resource_id,
            outcome="issued",
            metadata={"provider_id": provider.provider_id},
        )
        await db.commit()
        query = existing + [
            ("client_id", provider.client_id),
            ("redirect_uri", redirect_uri),
            ("response_type", "code"),
            ("scope", " ".join(provider.allowed_scopes)),
            ("state", state),
            ("nonce", nonce_part),
            ("code_challenge", challenge),
            ("code_challenge_method", "S256"),
        ]
        authorization_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")
        )
        return {
            "authorization_url": authorization_url,
            "expires_at": expires_at.isoformat() + "Z",
            "provider_id": provider.provider_id,
            "connector_id": resource.resource_id,
            "_browser_cookie": browser_cookie,
        }

    @classmethod
    async def finish_oauth(
        cls,
        db: AsyncSession,
        *,
        provider_id: str,
        state: str,
        code: str,
        provider_error: str,
        browser_cookie: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        session_claims: dict[str, Any],
    ) -> OAuthRedirect:
        staged_revocation_id = ""
        exchanged_token: dict[str, Any] | None = None
        exchange_provider: OAuthProvider | None = None
        try:
            try:
                await CoreWorkbenchIdentity.require_browser_session(
                    db,
                    claims=session_claims,
                    account_id=account_id,
                    identity=identity,
                )
            except WorkbenchIdentityError as error:
                raise WorkbenchIntegrationError(
                    "OAuth callback does not belong to the active browser session",
                    400,
                    "invalid_state",
                ) from error
            session_id_digest = cls._session_id_digest(session_claims)
            if (
                not isinstance(browser_cookie, str)
                or len(browser_cookie) < 40
                or len(browser_cookie) > 128
            ):
                raise WorkbenchIntegrationError(
                    "OAuth browser transaction is invalid", 400, "invalid_state"
                )
            state_part, nonce_part = cls._parse_state(state)
            state_digest = hashlib.sha256(state_part.encode("ascii")).hexdigest()
            nonce_digest = hashlib.sha256(nonce_part.encode("ascii")).hexdigest()
            row = (
                await db.execute(
                    select(WorkbenchOAuthState)
                    .where(
                        WorkbenchOAuthState.StateDigest == state_digest,
                        WorkbenchOAuthState.ProviderId == provider_id,
                    )
                    .with_for_update()
                )
            ).scalar()
            now = cls._now()
            if (
                row is None
                or row.Status != "pending"
                or row.ExpiresAt <= now
                or not secrets.compare_digest(row.NonceDigest, nonce_digest)
                or not secrets.compare_digest(
                    row.BrowserCookieDigest,
                    hashlib.sha256(browser_cookie.encode("ascii")).hexdigest(),
                )
                or not secrets.compare_digest(row.SessionIdDigest, session_id_digest)
                or row.BindingId != identity.binding_id
                or row.AccountId != cls._account_key(account_id)
                or row.CustomerId != identity.customer_id
                or row.NewApiUserId != identity.new_api_user_id
                or row.CanonicalSubject != identity.canonical_subject
                or row.ApplicationId != app_context.application_id
                or row.ProviderAppId != app_context.app_id
                or row.AppProfileId != app_context.app_profile_id
                or row.ConfigVersion != app_context.config_version
                or row.AuthEpoch != app_context.auth_epoch
            ):
                raise WorkbenchIntegrationError(
                    "OAuth state is invalid or expired", 400, "invalid_state"
                )
            scope = await cls._lock_connector_scope(
                db,
                account_id=account_id,
                identity=identity,
                app_context=app_context,
                provider_id=row.ProviderId,
                connector_id=row.ConnectorId,
            )
            if scope.Generation != row.ConnectionGeneration:
                raise WorkbenchIntegrationError(
                    "OAuth transaction was superseded", 409, "state_superseded"
                )
            row.Status = "consumed"
            row.ConsumedAt = now
            db.add(row)
            await db.commit()
            if provider_error:
                await cls._record_callback_failure(db, row, "provider_denied")
                return OAuthRedirect(cls._result_redirect("provider_denied"))
            if not code or len(code) > 8192 or any(ord(char) < 32 for char in code):
                raise WorkbenchIntegrationError(
                    "OAuth authorization code is invalid", 400, "invalid_code"
                )
            identity, app_context = await CoreWorkbenchIdentity.authorize_offline_scope(
                db,
                account_id=str(row.AccountId),
                binding_id=row.BindingId,
                canonical_subject=row.CanonicalSubject,
                customer_id=row.CustomerId,
                new_api_user_id=row.NewApiUserId,
                auth_epoch=row.AuthEpoch,
                application_id=row.ApplicationId,
                app_profile_id=row.AppProfileId,
                config_version=row.ConfigVersion,
                method="GET",
                resource_path="/workbench/integrations/oauth/callback",
                purpose="integration_refresh",
            )
            resource = WorkbenchIntegrationPolicy.resource(
                app_context, "connector", row.ConnectorId
            )
            if resource.provider_id != row.ProviderId or app_context.app_id != row.ProviderAppId:
                raise WorkbenchIntegrationError(
                    "OAuth connector scope is stale", 409, "scope_stale"
                )
            provider = WorkbenchIntegrationPolicy.provider(row.ProviderId)
            exchange_provider = provider
            if not provider.revocation_url:
                raise WorkbenchIntegrationError(
                    "OAuth provider revocation contract is required",
                    503,
                    "revocation_contract_missing",
                )
            verifier = WorkbenchIntegrationCrypto.decrypt(
                "oauth-state", row.VerifierCiphertext
            ).decode("ascii")
            token = await cls._token_request(
                provider,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": row.RedirectUri,
                    "client_id": provider.client_id,
                    "code_verifier": verifier,
                },
            )
            exchanged_token = token
            staged_revocation_id = await cls._stage_revocation(
                db,
                binding_id=row.BindingId,
                customer_id=row.CustomerId,
                application_id=row.ApplicationId,
                credential_id="",
                provider_id=row.ProviderId,
                token=token,
            )
            # A membership/App/plan revocation between exchange and persistence
            # must still prevent a usable local credential from being committed.
            await CoreWorkbenchIdentity.authorize_offline_scope(
                db,
                account_id=str(row.AccountId),
                binding_id=row.BindingId,
                canonical_subject=row.CanonicalSubject,
                customer_id=row.CustomerId,
                new_api_user_id=row.NewApiUserId,
                auth_epoch=row.AuthEpoch,
                application_id=row.ApplicationId,
                app_profile_id=row.AppProfileId,
                config_version=row.ConfigVersion,
                method="POST",
                resource_path="/workbench/integrations/oauth/persist",
                purpose="integration_refresh",
            )
            await CoreWorkbenchIdentity.require_browser_session(
                db,
                claims=session_claims,
                account_id=account_id,
                identity=identity,
            )
            await cls._persist_credential(
                db,
                row,
                identity,
                app_context,
                token,
                expected_generation=row.ConnectionGeneration,
                staged_revocation_id=staged_revocation_id,
            )
            return OAuthRedirect(cls._result_redirect("success"))
        except WorkbenchIntegrationError as error:
            await db.rollback()
            if staged_revocation_id:
                await cls._attempt_staged_revocation(db, staged_revocation_id)
            elif exchanged_token is not None and exchange_provider is not None:
                await cls._attempt_direct_revocation(exchange_provider, exchanged_token)
            return OAuthRedirect(cls._result_redirect(error.code))
        except Exception:
            await db.rollback()
            if staged_revocation_id:
                await cls._attempt_staged_revocation(db, staged_revocation_id)
            elif exchanged_token is not None and exchange_provider is not None:
                await cls._attempt_direct_revocation(exchange_provider, exchanged_token)
            return OAuthRedirect(cls._result_redirect("oauth_failed_closed"))

    @staticmethod
    def _parse_state(state: str) -> tuple[str, str]:
        if not isinstance(state, str) or len(state) > 256:
            raise WorkbenchIntegrationError("OAuth state is invalid", 400, "invalid_state")
        parts = state.split(".")
        if len(parts) != 3 or parts[0] != "wbo1":
            raise WorkbenchIntegrationError("OAuth state is invalid", 400, "invalid_state")
        for part in parts[1:]:
            if len(part) < 40 or len(part) > 64 or not all(
                char.isalnum() or char in "-_" for char in part
            ):
                raise WorkbenchIntegrationError("OAuth state is invalid", 400, "invalid_state")
        return parts[1], parts[2]

    @classmethod
    async def _persist_credential(
        cls,
        db: AsyncSession,
        state: WorkbenchOAuthState,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        token: dict[str, Any],
        *,
        expected_generation: int,
        staged_revocation_id: str,
    ) -> None:
        scope = await cls._lock_connector_scope(
            db,
            account_id=str(state.AccountId),
            identity=identity,
            app_context=app_context,
            provider_id=state.ProviderId,
            connector_id=state.ConnectorId,
        )
        if scope.Generation != expected_generation:
            raise WorkbenchIntegrationError(
                "OAuth transaction was superseded", 409, "state_superseded"
            )
        plaintext = cls._canonical(token)
        ciphertext, key_id = WorkbenchIntegrationCrypto.encrypt(
            "connector-token", plaintext
        )
        now = cls._now()
        expires_at = (
            now + timedelta(seconds=token["expires_in"])
            if token.get("expires_in") is not None
            else None
        )
        row = (
            await db.execute(
                select(WorkbenchConnectorCredential)
                .where(
                    WorkbenchConnectorCredential.BindingId == state.BindingId,
                    WorkbenchConnectorCredential.ApplicationId == state.ApplicationId,
                    WorkbenchConnectorCredential.AppProfileId == state.AppProfileId,
                    WorkbenchConnectorCredential.ConfigVersion == state.ConfigVersion,
                    WorkbenchConnectorCredential.ProviderId == state.ProviderId,
                    WorkbenchConnectorCredential.ConnectorId == state.ConnectorId,
                )
                .with_for_update()
            )
        ).scalar()
        values = {
            "AccountId": state.AccountId,
            "CustomerId": state.CustomerId,
            "NewApiUserId": state.NewApiUserId,
            "CanonicalSubject": state.CanonicalSubject,
            "ProviderAppId": state.ProviderAppId,
            "AuthEpoch": state.AuthEpoch,
            "TokenCiphertext": ciphertext,
            "EncryptionKeyId": key_id,
            "TokenDigest": hashlib.sha256(plaintext).hexdigest(),
            "GrantedScopesJson": json.dumps(token["scopes"], separators=(",", ":")),
            "TokenType": token["token_type"],
            "ExpiresAt": expires_at,
            "Status": "active",
            "RevocationStatus": "not_requested",
            "ConnectedAt": now,
            "RevokedAt": None,
        }
        if row is None:
            row = WorkbenchConnectorCredential(
                CredentialId=f"wcc_{uuid.uuid4().hex}",
                BindingId=state.BindingId,
                ApplicationId=state.ApplicationId,
                AppProfileId=state.AppProfileId,
                ConfigVersion=state.ConfigVersion,
                ProviderId=state.ProviderId,
                ConnectorId=state.ConnectorId,
                **values,
            )
        else:
            if row.TokenDigest != values["TokenDigest"]:
                old_token = json.loads(
                    WorkbenchIntegrationCrypto.decrypt(
                        "connector-token", row.TokenCiphertext
                    )
                )
                await cls._enqueue_revocation(
                    db,
                    binding_id=state.BindingId,
                    customer_id=state.CustomerId,
                    application_id=state.ApplicationId,
                    credential_id=row.CredentialId,
                    provider_id=state.ProviderId,
                    token=old_token,
                )
            for name, value in values.items():
                setattr(row, name, value)
        db.add(row)
        if staged_revocation_id:
            await db.execute(
                delete(WorkbenchOAuthRevocation).where(
                    WorkbenchOAuthRevocation.RevocationId == staged_revocation_id,
                    WorkbenchOAuthRevocation.Status == "pending",
                )
            )
        cls._audit(
            db,
            account_id=str(state.AccountId),
            identity=identity,
            app_context=app_context,
            action="oauth_connected",
            resource_kind="connector",
            resource_id=state.ConnectorId,
            outcome="active",
            metadata={"provider_id": state.ProviderId, "scopes": token["scopes"]},
        )
        await db.commit()

    @classmethod
    async def disconnect(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        connector_id: object,
        session_claims: dict[str, Any],
    ) -> dict[str, Any]:
        await cls.require_recent_reauthentication(
            db,
            account_id=account_id,
            identity=identity,
            session_claims=session_claims,
        )
        resource = WorkbenchIntegrationPolicy.resource(
            app_context, "connector", connector_id
        )
        provider = WorkbenchIntegrationPolicy.provider(resource.provider_id)
        now = cls._now()
        scope = await cls._lock_connector_scope(
            db,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            provider_id=provider.provider_id,
            connector_id=resource.resource_id,
        )
        scope.Generation += 1
        db.add(scope)
        await cls._revoke_pending_oauth(db, scope, now=now)
        row = (
            await db.execute(
                select(WorkbenchConnectorCredential)
                .where(
                    WorkbenchConnectorCredential.BindingId == identity.binding_id,
                    WorkbenchConnectorCredential.AccountId == cls._account_key(account_id),
                    WorkbenchConnectorCredential.CustomerId == identity.customer_id,
                    WorkbenchConnectorCredential.NewApiUserId == identity.new_api_user_id,
                    WorkbenchConnectorCredential.CanonicalSubject == identity.canonical_subject,
                    WorkbenchConnectorCredential.ApplicationId == app_context.application_id,
                    WorkbenchConnectorCredential.ProviderAppId == app_context.app_id,
                    WorkbenchConnectorCredential.AppProfileId == app_context.app_profile_id,
                    WorkbenchConnectorCredential.ConfigVersion == app_context.config_version,
                    WorkbenchConnectorCredential.AuthEpoch == app_context.auth_epoch,
                    WorkbenchConnectorCredential.ProviderId == resource.provider_id,
                    WorkbenchConnectorCredential.ConnectorId == resource.resource_id,
                )
                .with_for_update()
            )
        ).scalar()
        if row is None:
            await db.commit()
            return {"status": "disconnected", "execution_status": "execution_blocked_contract"}
        revocation_id = ""
        revocation_status = "provider_unknown"
        try:
            token = json.loads(
                WorkbenchIntegrationCrypto.decrypt(
                    "connector-token", row.TokenCiphertext
                )
            )
            revocation_id = await cls._enqueue_revocation(
                db,
                binding_id=row.BindingId,
                customer_id=row.CustomerId,
                application_id=row.ApplicationId,
                credential_id=row.CredentialId,
                provider_id=row.ProviderId,
                token=token,
            )
            revocation_status = "provider_revoke_pending"
        except Exception:
            revocation_status = "provider_unknown"
        row.Status = "revoked"
        row.RevokedAt = now
        row.RevocationStatus = revocation_status
        db.add(row)
        cls._audit(
            db,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            action="oauth_disconnected",
            resource_kind="connector",
            resource_id=resource.resource_id,
            outcome=revocation_status,
            metadata={"provider_id": provider.provider_id},
        )
        await db.commit()
        if revocation_id:
            await cls._attempt_staged_revocation(db, revocation_id)
            row = (
                await db.execute(
                    select(WorkbenchConnectorCredential)
                    .where(WorkbenchConnectorCredential.Id == row.Id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
        return cls._project_credential(row)

    @classmethod
    async def bind_skill(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        kind: object,
        resource_id: object,
        parent_id: object,
        bind: bool,
        session_claims: dict[str, Any],
    ) -> dict[str, Any]:
        await cls.require_recent_reauthentication(
            db,
            account_id=account_id,
            identity=identity,
            session_claims=session_claims,
        )
        resource = WorkbenchIntegrationPolicy.resource(
            app_context, kind, resource_id, parent_id
        )
        if resource.kind != "skill":
            return await cls._bind_provider_resource(
                db,
                account_id=account_id,
                identity=identity,
                app_context=app_context,
                resource=resource,
                bind=bind,
            )
        agent_binding = (
            await db.execute(
                select(WorkbenchAgentBinding)
                .where(
                    WorkbenchAgentBinding.BindingId == identity.binding_id,
                    WorkbenchAgentBinding.AccountId == cls._account_key(account_id),
                    WorkbenchAgentBinding.ApplicationId == app_context.application_id,
                )
                .with_for_update()
            )
        ).scalar()
        if (
            agent_binding is None
            or agent_binding.Status != "active"
            or not agent_binding.AgentId
        ):
            raise WorkbenchIntegrationError(
                "active user Agent is required before binding a Skill",
                409,
                "agent_unavailable",
            )
        # Turn submission takes this same row lock before it durably writes the
        # submitted Turn. The lock order is always AgentBinding -> WorkbenchTurn,
        # so this second-stage check closes the check/use window across blue and
        # green processes without relying on an in-process mutex. AttemptId is
        # the mutation epoch used by the provider-side verification below.
        active_turn = (
            await db.execute(
                select(WorkbenchTurn.TurnId).where(
                    WorkbenchTurn.BindingId == identity.binding_id,
                    WorkbenchTurn.AccountId == cls._account_key(account_id),
                    WorkbenchTurn.CustomerId == identity.customer_id,
                    WorkbenchTurn.ApplicationId == app_context.application_id,
                    WorkbenchTurn.Status.in_({"submitted", "running", "cancel_requested"}),
                ).limit(1)
            )
        ).scalar()
        if active_turn is not None:
            raise WorkbenchIntegrationError(
                "integration binding cannot change during an active Turn",
                409,
                "active_turn",
            )
        rows = (
            await db.execute(
                select(WorkbenchIntegrationBinding)
                .where(
                    *cls._scope_filters(account_id, identity, app_context),
                    WorkbenchIntegrationBinding.ResourceKind == "skill",
                )
                .with_for_update()
            )
        ).scalars().all()
        row = next((item for item in rows if item.ResourceId == resource.resource_id), None)
        active_ids = {item.ResourceId for item in rows if item.Status == "active"}
        if bind:
            target_ids = active_ids | {resource.resource_id}
            if row is not None and row.Status == "active":
                return cls._project_binding(row)
        else:
            target_ids = active_ids - {resource.resource_id}
            if row is None or row.Status == "revoked":
                return {
                    "kind": "skill",
                    "resource_id": resource.resource_id,
                    "status": "revoked",
                    "provider_sync_status": "verified",
                }
        vendor = TAgenticApp.get_app().get_vendor_app(app_context.application_id)
        detail = await vendor.forward_request(
            "DescribeAgentDetail",
            {"AppId": app_context.app_id, "AgentId": agent_binding.AgentId},
        )
        current_ids = cls._provider_skill_ids(detail, agent_binding.AgentId)
        if current_ids != active_ids:
            raise WorkbenchIntegrationError(
                "provider SkillList differs from the locally authorized complete set",
                409,
                "provider_reconciliation_required",
            )
        now = cls._now()
        if row is None:
            row = WorkbenchIntegrationBinding(
                IntegrationBindingId=f"wib_{uuid.uuid4().hex}",
                BindingId=identity.binding_id,
                AccountId=cls._account_key(account_id),
                CustomerId=identity.customer_id,
                NewApiUserId=identity.new_api_user_id,
                CanonicalSubject=identity.canonical_subject,
                ApplicationId=app_context.application_id,
                ProviderAppId=app_context.app_id,
                AppProfileId=app_context.app_profile_id,
                ConfigVersion=app_context.config_version,
                AuthEpoch=app_context.auth_epoch,
                AgentId=agent_binding.AgentId,
                ResourceKind="skill",
                ResourceId=resource.resource_id,
                ParentResourceId="",
                UserConsentedAt=now,
            )
        row.Status = "configuring"
        row.ProviderSyncStatus = "pending"
        row.RevokedAt = None
        agent_binding.Status = "configuring_integrations"
        agent_binding.AttemptId = uuid.uuid4().hex
        db.add(row)
        db.add(agent_binding)
        await db.commit()
        modify_started = False
        try:
            modify_started = True
            await vendor.forward_request(
                "ModifyAgent",
                {
                    "AppId": app_context.app_id,
                    "AgentId": agent_binding.AgentId,
                    "Agent": {
                        "SkillList": [
                            {"SkillId": skill_id} for skill_id in sorted(target_ids)
                        ]
                    },
                    "UpdateMask": {"Paths": ["SkillList"]},
                },
            )
            verified = await vendor.forward_request(
                "DescribeAgentDetail",
                {"AppId": app_context.app_id, "AgentId": agent_binding.AgentId},
            )
            if cls._provider_skill_ids(verified, agent_binding.AgentId) != target_ids:
                raise WorkbenchIntegrationError(
                    "provider SkillList did not match the complete target set",
                    502,
                    "provider_verification_failed",
                )
        except Exception as error:
            await db.rollback()
            locked_agent = (
                await db.execute(
                    select(WorkbenchAgentBinding)
                    .where(WorkbenchAgentBinding.Id == agent_binding.Id)
                    .with_for_update()
                )
            ).scalar()
            locked_row = (
                await db.execute(
                    select(WorkbenchIntegrationBinding)
                    .where(WorkbenchIntegrationBinding.Id == row.Id)
                    .with_for_update()
                )
            ).scalar()
            if locked_agent is not None:
                locked_agent.Status = "provider_unknown" if modify_started else "active"
                locked_agent.ErrorCode = type(error).__name__[:64]
                db.add(locked_agent)
            if locked_row is not None:
                locked_row.Status = "provider_unknown"
                locked_row.ProviderSyncStatus = "provider_unknown"
                db.add(locked_row)
            await db.commit()
            if isinstance(error, WorkbenchIntegrationError):
                raise
            raise WorkbenchIntegrationError(
                "Skill binding outcome is unknown and requires reconciliation",
                502,
                "provider_unknown",
            ) from error
        await db.rollback()
        locked_agent = (
            await db.execute(
                select(WorkbenchAgentBinding)
                .where(WorkbenchAgentBinding.Id == agent_binding.Id)
                .with_for_update()
            )
        ).scalar()
        locked_row = (
            await db.execute(
                select(WorkbenchIntegrationBinding)
                .where(WorkbenchIntegrationBinding.Id == row.Id)
                .with_for_update()
            )
        ).scalar()
        if (
            locked_agent is None
            or locked_agent.Status != "configuring_integrations"
            or locked_row is None
            or locked_row.Status != "configuring"
        ):
            raise WorkbenchIntegrationError(
                "integration binding version changed during provider update",
                409,
                "binding_conflict",
            )
        locked_agent.Status = "active"
        locked_agent.ErrorCode = None
        locked_row.Status = "active" if bind else "revoked"
        locked_row.ProviderSyncStatus = "verified"
        locked_row.RevokedAt = None if bind else cls._now()
        db.add(locked_agent)
        db.add(locked_row)
        cls._audit(
            db,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            action="skill_bind" if bind else "skill_unbind",
            resource_kind="skill",
            resource_id=resource.resource_id,
            outcome="verified",
            metadata={"update_mask": ["SkillList"], "target_count": len(target_ids)},
        )
        await db.commit()
        return cls._project_binding(locked_row)

    @classmethod
    async def _bind_provider_resource(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        resource: IntegrationResource,
        bind: bool,
    ) -> dict[str, Any]:
        agent_binding = (
            await db.execute(
                select(WorkbenchAgentBinding)
                .where(
                    WorkbenchAgentBinding.BindingId == identity.binding_id,
                    WorkbenchAgentBinding.AccountId == cls._account_key(account_id),
                    WorkbenchAgentBinding.ApplicationId == app_context.application_id,
                )
                .with_for_update()
            )
        ).scalar()
        if agent_binding is None or agent_binding.Status != "active" or not agent_binding.AgentId:
            raise WorkbenchIntegrationError(
                "active user-level Agent is required before binding an integration",
                409,
                "agent_unavailable",
            )
        active_turn = (
            await db.execute(
                select(WorkbenchTurn.TurnId).where(
                    WorkbenchTurn.BindingId == identity.binding_id,
                    WorkbenchTurn.AccountId == cls._account_key(account_id),
                    WorkbenchTurn.CustomerId == identity.customer_id,
                    WorkbenchTurn.ApplicationId == app_context.application_id,
                    WorkbenchTurn.Status.in_({"submitted", "running", "cancel_requested"}),
                ).limit(1)
            )
        ).scalar()
        if active_turn is not None:
            raise WorkbenchIntegrationError(
                "integration binding cannot change during an active Turn", 409, "active_turn"
            )

        resources = WorkbenchIntegrationPolicy.catalog(app_context)
        definitions = await cls._provider_definitions(
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            resources=resources,
        )
        blocked_reason = cls._definition_blocked_reason(resource, definitions.get(
            resource.parent_id if resource.kind == "tool" else resource.resource_id
        ))
        if blocked_reason is not None:
            status = 403 if blocked_reason in {"user_oauth_unsupported", "write_tool_unsupported"} else 503
            raise WorkbenchIntegrationError(
                "integration is outside the bounded provider execution contract",
                status,
                blocked_reason,
            )
        rows = (
            await db.execute(
                select(WorkbenchIntegrationBinding)
                .where(
                    *cls._scope_filters(account_id, identity, app_context),
                    WorkbenchIntegrationBinding.ResourceKind.in_({"plugin", "tool", "connector"}),
                )
                .with_for_update()
            )
        ).scalars().all()
        if any(item.Status in {"configuring", "provider_unknown"} for item in rows):
            raise WorkbenchIntegrationError(
                "integration state requires provider reconciliation", 409, "provider_reconciliation_required"
            )
        row = next(
            (
                item
                for item in rows
                if item.ResourceKind == resource.kind
                and item.ResourceId == resource.resource_id
                and item.ParentResourceId == resource.parent_id
            ),
            None,
        )
        if bind and row is not None and row.Status == "active":
            return cls._project_binding(row)
        if not bind and (row is None or row.Status == "revoked"):
            return {
                "kind": resource.kind,
                "resource_id": resource.resource_id,
                "parent_resource_id": resource.parent_id or None,
                "status": "revoked",
                "provider_sync_status": "verified",
            }

        vendor = TAgenticApp.get_app().get_vendor_app(app_context.application_id)
        now = cls._now()
        if row is None:
            row = WorkbenchIntegrationBinding(
                IntegrationBindingId=f"wib_{uuid.uuid4().hex}",
                BindingId=identity.binding_id,
                AccountId=cls._account_key(account_id),
                CustomerId=identity.customer_id,
                NewApiUserId=identity.new_api_user_id,
                CanonicalSubject=identity.canonical_subject,
                ApplicationId=app_context.application_id,
                ProviderAppId=app_context.app_id,
                AppProfileId=app_context.app_profile_id,
                ConfigVersion=app_context.config_version,
                AuthEpoch=app_context.auth_epoch,
                AgentId=agent_binding.AgentId,
                ResourceKind=resource.kind,
                ResourceId=resource.resource_id,
                ParentResourceId=resource.parent_id,
                UserConsentedAt=now,
            )

        pre_detail = await vendor.forward_request(
            "DescribeAgentDetail",
            {"AppId": app_context.app_id, "AgentId": agent_binding.AgentId},
        )
        try:
            pre_state = parse_agent_state(pre_detail, agent_binding.AgentId)
            validate_executable_state(pre_state)
            expected_plugins, expected_physical, expected_active = cls._expected_provider_sets(
                rows, resources, definitions
            )
            if (
                set(pre_state.plugin_configs) != expected_plugins
                or set(pre_state.tool_configs) != expected_physical
                or pre_state.active_tool_keys != expected_active
            ):
                raise ProviderIntegrationContractError(
                    "provider Agent differs from the exact locally authorized set"
                )
        except ProviderIntegrationContractError as error:
            agent_binding.Status = "provider_unknown"
            agent_binding.ErrorCode = type(error).__name__[:64]
            row.Status = "provider_unknown"
            row.ProviderSyncStatus = "provider_unknown"
            db.add(agent_binding)
            db.add(row)
            await db.commit()
            raise WorkbenchIntegrationError(
                "provider integration state is unknown and requires reconciliation",
                502,
                "provider_unknown",
            ) from error

        row.Status = "configuring"
        row.ProviderSyncStatus = "pending"
        row.RevokedAt = None
        agent_binding.Status = "configuring_integrations"
        agent_binding.AttemptId = uuid.uuid4().hex
        db.add(row)
        db.add(agent_binding)
        await db.commit()

        target_rows = list(rows)
        if row not in target_rows:
            target_rows.append(row)
        final_status = "active" if bind else "revoked"
        row.Status = final_status
        target_plugins, target_physical, target_active = cls._expected_provider_sets(
            target_rows, resources, definitions
        )
        target_plugin_configs = copy.deepcopy(pre_state.plugin_configs)
        target_tool_configs = copy.deepcopy(pre_state.tool_configs)
        for plugin_id in target_plugins:
            target_plugin_configs.setdefault(
                plugin_id, copy.deepcopy(definitions[plugin_id].modify_config)
            )
        for key in target_physical:
            definition = definitions[key[0]]
            tool = definition.tool(key[1])
            if tool is None:
                raise WorkbenchIntegrationError(
                    "allowlisted Tool definition disappeared", 503, "provider_config_incomplete"
                )
            target_tool_configs.setdefault(key, copy.deepcopy(tool.modify_config))
            target_tool_configs[key]["IsDisabled"] = key not in target_active
        target_state = ProviderAgentIntegrationState(
            agent_binding.AgentId,
            target_plugin_configs,
            {key: 2 for key in target_plugin_configs},
            {key: 1 for key in target_plugin_configs},
            {key: definitions[key].plugin_class for key in target_plugin_configs},
            target_tool_configs,
            {
                key: definitions[key[0]].tool(key[1]).access_mode
                for key in target_tool_configs
            },
            {key: 1 for key in target_tool_configs},
        )
        plugin_list, tool_list = exact_modify_lists(target_state)
        modify_started = False
        try:
            modify_started = True
            await vendor.forward_request(
                "ModifyAgent",
                {
                    "AppId": app_context.app_id,
                    "AgentId": agent_binding.AgentId,
                    "Agent": {"PluginList": plugin_list, "ToolList": tool_list},
                    "UpdateMask": {"Paths": ["PluginList", "ToolList"]},
                },
            )
            post_detail = await vendor.forward_request(
                "DescribeAgentDetail",
                {"AppId": app_context.app_id, "AgentId": agent_binding.AgentId},
            )
            post_state = parse_agent_state(post_detail, agent_binding.AgentId)
            validate_executable_state(post_state)
            if (
                post_state.plugin_configs != target_plugin_configs
                or post_state.tool_configs != target_tool_configs
                or post_state.active_tool_keys != target_active
            ):
                raise ProviderIntegrationContractError("provider post-read is not exact")
        except Exception as error:
            await db.rollback()
            locked_agent = (
                await db.execute(
                    select(WorkbenchAgentBinding)
                    .where(WorkbenchAgentBinding.Id == agent_binding.Id)
                    .with_for_update()
                )
            ).scalar()
            locked_row = (
                await db.execute(
                    select(WorkbenchIntegrationBinding)
                    .where(WorkbenchIntegrationBinding.Id == row.Id)
                    .with_for_update()
                )
            ).scalar()
            if locked_agent is not None:
                locked_agent.Status = "provider_unknown" if modify_started else "active"
                locked_agent.ErrorCode = type(error).__name__[:64]
                db.add(locked_agent)
            if locked_row is not None:
                locked_row.Status = "provider_unknown"
                locked_row.ProviderSyncStatus = "provider_unknown"
                db.add(locked_row)
            await db.commit()
            raise WorkbenchIntegrationError(
                "provider binding outcome is unknown and requires reconciliation",
                502,
                "provider_unknown",
            ) from error

        await db.rollback()
        locked_agent = (
            await db.execute(
                select(WorkbenchAgentBinding)
                .where(WorkbenchAgentBinding.Id == agent_binding.Id)
                .with_for_update()
            )
        ).scalar()
        locked_row = (
            await db.execute(
                select(WorkbenchIntegrationBinding)
                .where(WorkbenchIntegrationBinding.Id == row.Id)
                .with_for_update()
            )
        ).scalar()
        if (
            locked_agent is None
            or locked_agent.Status != "configuring_integrations"
            or locked_row is None
            or locked_row.Status != "configuring"
        ):
            raise WorkbenchIntegrationError(
                "integration binding version changed during provider update",
                409,
                "binding_conflict",
            )
        locked_agent.Status = "active"
        locked_agent.ErrorCode = None
        locked_row.Status = final_status
        locked_row.ProviderSyncStatus = "verified"
        locked_row.RevokedAt = None if bind else cls._now()
        db.add(locked_agent)
        db.add(locked_row)
        cls._audit(
            db,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            action=f"{resource.kind}_{'bind' if bind else 'disable'}",
            resource_kind=resource.kind,
            resource_id=resource.resource_id,
            outcome="verified",
            metadata={
                "update_mask": ["PluginList", "ToolList"],
                "physical_unbind": False,
                "disabled_tool_count": len(target_physical - target_active),
            },
        )
        await db.commit()
        return cls._project_binding(locked_row)

    @classmethod
    def _expected_provider_sets(
        cls,
        rows: list[WorkbenchIntegrationBinding],
        resources: tuple[IntegrationResource, ...],
        definitions: dict[str, ProviderPluginDefinition],
    ) -> tuple[set[str], set[tuple[str, str]], set[tuple[str, str]]]:
        resource_map = {
            (item.kind, item.resource_id, item.parent_id): item for item in resources
        }
        plugins: set[str] = set()
        physical: set[tuple[str, str]] = set()
        active: set[tuple[str, str]] = set()
        for row in rows:
            if row.Status not in {"active", "revoked"}:
                continue
            resource = resource_map.get(
                (row.ResourceKind, row.ResourceId, row.ParentResourceId)
            )
            if resource is None:
                raise ProviderIntegrationContractError("local integration escaped the allowlist")
            plugin_id = resource.parent_id if resource.kind == "tool" else resource.resource_id
            definition = definitions.get(plugin_id)
            if definition is None:
                raise ProviderIntegrationContractError("allowlisted provider Plugin is unavailable")
            selected = definition.tools
            if resource.kind == "tool":
                tool = definition.tool(resource.resource_id)
                if tool is None:
                    raise ProviderIntegrationContractError("allowlisted provider Tool is unavailable")
                selected = (tool,)
            plugins.add(plugin_id)
            keys = {(plugin_id, tool.tool_id) for tool in selected}
            physical.update(keys)
            if row.Status == "active":
                active.update(keys)
        return plugins, physical, active

    @staticmethod
    def _provider_skill_ids(response: Any, agent_id: str) -> set[str]:
        agent = response.get("Agent") if isinstance(response, dict) else None
        skills = agent.get("SkillList") if isinstance(agent, dict) else None
        if (
            not isinstance(agent, dict)
            or str(agent.get("AgentId") or "") != agent_id
            or not isinstance(skills, list)
        ):
            raise WorkbenchIntegrationError(
                "provider Agent SkillList cannot be verified", 503, "provider_contract_invalid"
            )
        result: set[str] = set()
        for item in skills:
            skill_id = item.get("SkillId") if isinstance(item, dict) else None
            if not isinstance(skill_id, str) or not skill_id or len(skill_id) > 128:
                raise WorkbenchIntegrationError(
                    "provider Agent SkillList is invalid", 502, "provider_contract_invalid"
                )
            if skill_id in result:
                raise WorkbenchIntegrationError(
                    "provider Agent SkillList contains duplicates", 502, "provider_contract_invalid"
                )
            result.add(skill_id)
        return result

    @staticmethod
    def _project_binding(row: WorkbenchIntegrationBinding) -> dict[str, Any]:
        return {
            "integration_binding_id": row.IntegrationBindingId,
            "kind": row.ResourceKind,
            "resource_id": row.ResourceId,
            "parent_resource_id": row.ParentResourceId or None,
            "status": row.Status,
            "provider_sync_status": row.ProviderSyncStatus,
            "user_consented_at": row.UserConsentedAt.isoformat() + "Z",
            "revoked_at": row.RevokedAt.isoformat() + "Z" if row.RevokedAt else None,
        }

    @classmethod
    async def revalidate_turn(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> None:
        """Every Turn re-reads the exact bounded provider integration state."""

        WorkbenchIntegrationPolicy.require_enabled()
        rows = (
            await db.execute(
                select(WorkbenchIntegrationBinding).where(
                    *cls._scope_filters(account_id, identity, app_context),
                    WorkbenchIntegrationBinding.Status.in_({"active", "revoked"}),
                )
            )
        ).scalars().all()
        if any(row.ResourceKind == "skill" and row.Status == "active" for row in rows):
            raise WorkbenchIntegrationError(
                "Skill execution remains outside the bounded Turn contract",
                503,
                "execution_blocked_contract",
            )
        provider_rows = [row for row in rows if row.ResourceKind != "skill"]
        if not provider_rows:
            return
        resources = WorkbenchIntegrationPolicy.catalog(app_context)
        definitions = await cls._provider_definitions(
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            resources=resources,
        )
        expected_plugins, expected_physical, expected_active = cls._expected_provider_sets(
            provider_rows, resources, definitions
        )
        agent = (
            await db.execute(
                select(WorkbenchAgentBinding).where(
                    WorkbenchAgentBinding.BindingId == identity.binding_id,
                    WorkbenchAgentBinding.AccountId == cls._account_key(account_id),
                    WorkbenchAgentBinding.ApplicationId == app_context.application_id,
                    WorkbenchAgentBinding.Status == "active",
                )
            )
        ).scalar()
        if agent is None or not agent.AgentId:
            raise WorkbenchIntegrationError(
                "user-level Agent is unavailable", 409, "agent_unavailable"
            )
        vendor = TAgenticApp.get_app().get_vendor_app(app_context.application_id)
        try:
            detail = await vendor.forward_request(
                "DescribeAgentDetail",
                {"AppId": app_context.app_id, "AgentId": agent.AgentId},
            )
            state = parse_agent_state(detail, agent.AgentId)
            validate_executable_state(state)
            if (
                set(state.plugin_configs) != expected_plugins
                or set(state.tool_configs) != expected_physical
                or state.active_tool_keys != expected_active
            ):
                raise ProviderIntegrationContractError("provider execution state is not exact")
        except Exception as error:
            raise WorkbenchIntegrationError(
                "provider integration execution state cannot be verified",
                503,
                "provider_unknown",
            ) from error

    @classmethod
    async def revoke_for_control_event(cls, event: Any) -> None:
        payload = event.payload
        now = cls._now()
        async with db_connection() as db:
            filters = [
                WorkbenchConnectorCredential.CustomerId == payload["customer_id"],
            ]
            binding_id = str(payload.get("binding_id") or "")
            if binding_id:
                filters.append(WorkbenchConnectorCredential.BindingId == binding_id)
            if event.event_type == "SESSION_REVOKE":
                filters.extend(
                    [
                        WorkbenchConnectorCredential.NewApiUserId == payload["new_api_user_id"],
                        WorkbenchConnectorCredential.AuthEpoch < payload["auth_epoch"],
                    ]
                )
            elif event.event_type == "CACHE_INVALIDATE":
                filters.append(
                    WorkbenchConnectorCredential.ApplicationId == payload["application_id"]
                )
            credential_rows = (
                await db.execute(
                    select(WorkbenchConnectorCredential)
                    .where(*filters, WorkbenchConnectorCredential.Status == "active")
                    .with_for_update()
                )
            ).scalars().all()
            for credential in credential_rows:
                outcome = "revocation_pending_control_event"
                try:
                    token = json.loads(
                        WorkbenchIntegrationCrypto.decrypt(
                            "connector-token", credential.TokenCiphertext
                        )
                    )
                    await cls._enqueue_revocation(
                        db,
                        binding_id=credential.BindingId,
                        customer_id=credential.CustomerId,
                        application_id=credential.ApplicationId,
                        credential_id=credential.CredentialId,
                        provider_id=credential.ProviderId,
                        token=token,
                    )
                except Exception:  # local access is still revoked; remote state is unknown
                    outcome = "provider_unknown"
                credential.Status = "revoked"
                credential.RevocationStatus = outcome
                credential.RevokedAt = now
                db.add(credential)
                db.add(
                    WorkbenchIntegrationAudit(
                        AuditId=f"wia_{uuid.uuid4().hex}",
                        BindingId=credential.BindingId,
                        AccountId=credential.AccountId,
                        CustomerId=credential.CustomerId,
                        ApplicationId=credential.ApplicationId,
                        AppProfileId=credential.AppProfileId,
                        ConfigVersion=credential.ConfigVersion,
                        Action="oauth_control_event_revoke",
                        ResourceKind="connector",
                        ResourceId=credential.ConnectorId,
                        Outcome=outcome,
                        ErrorCode=(
                            "" if outcome != "provider_unknown" else "revocation_enqueue_failed"
                        ),
                        MetadataJson=json.dumps(
                            {"provider_id": credential.ProviderId},
                            separators=(",", ":"),
                        ),
                    )
                )
            binding_filters = [
                WorkbenchIntegrationBinding.CustomerId == payload["customer_id"]
            ]
            if binding_id:
                binding_filters.append(WorkbenchIntegrationBinding.BindingId == binding_id)
            if event.event_type == "SESSION_REVOKE":
                binding_filters.extend(
                    [
                        WorkbenchIntegrationBinding.NewApiUserId == payload["new_api_user_id"],
                        WorkbenchIntegrationBinding.AuthEpoch < payload["auth_epoch"],
                    ]
                )
            elif event.event_type == "CACHE_INVALIDATE":
                binding_filters.append(
                    WorkbenchIntegrationBinding.ApplicationId == payload["application_id"]
                )
            await db.execute(
                update(WorkbenchIntegrationBinding)
                .where(*binding_filters, WorkbenchIntegrationBinding.Status == "active")
                .values(
                    Status="revoked",
                    ProviderSyncStatus="revoked_control_event",
                    RevokedAt=now,
                )
            )
            state_filters = [WorkbenchOAuthState.CustomerId == payload["customer_id"]]
            if binding_id:
                state_filters.append(WorkbenchOAuthState.BindingId == binding_id)
            if event.event_type == "SESSION_REVOKE":
                state_filters.extend(
                    [
                        WorkbenchOAuthState.NewApiUserId == payload["new_api_user_id"],
                        WorkbenchOAuthState.AuthEpoch < payload["auth_epoch"],
                    ]
                )
            elif event.event_type == "CACHE_INVALIDATE":
                state_filters.append(
                    WorkbenchOAuthState.ApplicationId == payload["application_id"]
                )
            await db.execute(
                update(WorkbenchOAuthState)
                .where(*state_filters, WorkbenchOAuthState.Status == "pending")
                .values(Status="revoked", ConsumedAt=now)
            )
            await db.commit()

    @classmethod
    async def _record_callback_failure(
        cls, db: AsyncSession, row: WorkbenchOAuthState, code: str
    ) -> None:
        db.add(
            WorkbenchIntegrationAudit(
                AuditId=f"wia_{uuid.uuid4().hex}",
                BindingId=row.BindingId,
                AccountId=row.AccountId,
                CustomerId=row.CustomerId,
                ApplicationId=row.ApplicationId,
                AppProfileId=row.AppProfileId,
                ConfigVersion=row.ConfigVersion,
                Action="oauth_callback",
                ResourceKind="connector",
                ResourceId=row.ConnectorId,
                Outcome="denied",
                ErrorCode=code,
                MetadataJson=json.dumps(
                    {"provider_id": row.ProviderId}, separators=(",", ":")
                ),
            )
        )
        await db.commit()

    @staticmethod
    def _audit(
        db: AsyncSession,
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        action: str,
        resource_kind: str,
        resource_id: str,
        outcome: str,
        metadata: dict[str, Any],
        error_code: str = "",
    ) -> None:
        db.add(
            WorkbenchIntegrationAudit(
                AuditId=f"wia_{uuid.uuid4().hex}",
                BindingId=identity.binding_id,
                AccountId=WorkbenchIntegrations._account_key(account_id),
                CustomerId=identity.customer_id,
                ApplicationId=app_context.application_id,
                AppProfileId=app_context.app_profile_id,
                ConfigVersion=app_context.config_version,
                Action=action,
                ResourceKind=resource_kind,
                ResourceId=resource_id,
                Outcome=outcome,
                ErrorCode=error_code,
                MetadataJson=json.dumps(metadata, separators=(",", ":")),
            )
        )

    @classmethod
    def _result_redirect(cls, result: str) -> str:
        base = str(tagentic_config.WORKBENCH_PUBLIC_BASE_URL or "").strip().rstrip("/")
        if not base:
            return "/workbench/?integration_oauth=oauth_failed_closed"
        return f"{base}/?integration_oauth={urlencode({'value': result})[6:]}"

    @classmethod
    async def _resolve_public(cls, host: str, port: int) -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        try:
            records = await loop.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except OSError as error:
            raise WorkbenchIntegrationError(
                "OAuth provider DNS resolution failed", 503, "provider_unavailable"
            ) from error
        addresses = tuple(sorted({record[4][0] for record in records}))
        if not addresses or len(addresses) > 16:
            raise WorkbenchIntegrationError(
                "OAuth provider DNS response is invalid", 503, "provider_unavailable"
            )
        for address in addresses:
            parsed = ipaddress.ip_address(address)
            if not parsed.is_global:
                raise WorkbenchIntegrationError(
                    "OAuth provider resolved to a non-public address",
                    503,
                    "provider_unavailable",
                )
        return addresses

    @classmethod
    async def _oauth_post(
        cls,
        provider: OAuthProvider,
        url: str,
        form: dict[str, str],
    ) -> tuple[int, bytes, str]:
        parsed = urlsplit(url)
        host = str(parsed.hostname or "").lower().rstrip(".")
        if host not in provider.allowed_hosts:
            raise WorkbenchIntegrationError(
                "OAuth endpoint host is not allowed", 503, "provider_config_invalid"
            )
        pins = await cls._resolve_public(host, 443)
        resolver = _PinnedResolver({host: pins})
        connector = aiohttp.TCPConnector(
            resolver=resolver,
            use_dns_cache=False,
            ttl_dns_cache=0,
            ssl=ssl.create_default_context(),
        )
        headers = {"Accept": "application/json"}
        secret = WorkbenchIntegrationPolicy.client_secret(provider)
        if provider.token_auth_method == "client_secret_basic":
            basic = base64.b64encode(
                f"{provider.client_id}:{secret}".encode("utf-8")
            ).decode("ascii")
            headers["Authorization"] = f"Basic {basic}"
        elif provider.token_auth_method == "client_secret_post":
            form = {**form, "client_secret": secret}
        timeout = aiohttp.ClientTimeout(
            total=tagentic_config.WORKBENCH_OAUTH_HTTP_TIMEOUT_SECONDS
        )
        try:
            async with aiohttp.ClientSession(
                connector=connector, timeout=timeout
            ) as session:
                async with session.post(
                    url,
                    data=form,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    max_bytes = tagentic_config.WORKBENCH_OAUTH_MAX_RESPONSE_BYTES
                    if response.content_length is not None and response.content_length > max_bytes:
                        raise WorkbenchIntegrationError(
                            "OAuth provider response is too large", 502, "provider_response_invalid"
                        )
                    body = await response.content.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        raise WorkbenchIntegrationError(
                            "OAuth provider response is too large", 502, "provider_response_invalid"
                        )
                    return response.status, body, str(response.headers.get("Content-Type") or "")
        except WorkbenchIntegrationError:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError) as error:
            raise WorkbenchIntegrationError(
                "OAuth provider is unavailable", 503, "provider_unavailable"
            ) from error

    @classmethod
    async def _token_request(
        cls, provider: OAuthProvider, form: dict[str, str]
    ) -> dict[str, Any]:
        status, body, content_type = await cls._oauth_post(
            provider, provider.token_url, form
        )
        if status < 200 or status >= 300:
            raise WorkbenchIntegrationError(
                "OAuth token exchange was rejected", 502, "token_exchange_rejected"
            )
        try:
            if "application/json" in content_type.lower():
                value = json.loads(body)
            else:
                value = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
        except (UnicodeDecodeError, TypeError, ValueError) as error:
            raise WorkbenchIntegrationError(
                "OAuth token response is invalid", 502, "provider_response_invalid"
            ) from error
        if not isinstance(value, dict):
            raise WorkbenchIntegrationError(
                "OAuth token response is invalid", 502, "provider_response_invalid"
            )
        access_token = value.get("access_token")
        refresh_token = value.get("refresh_token")
        id_token = value.get("id_token")
        token_type = str(value.get("token_type") or "Bearer").strip()
        expires_in = value.get("expires_in")
        if isinstance(expires_in, str) and expires_in.isdigit():
            expires_in = int(expires_in)
        if (
            not isinstance(access_token, str)
            or len(access_token) < 8
            or len(access_token) > 65536
            or token_type.lower() != "bearer"
            or (refresh_token is not None and (not isinstance(refresh_token, str) or len(refresh_token) > 65536))
            or (id_token is not None and (not isinstance(id_token, str) or len(id_token) > 131072))
            or (
                expires_in is not None
                and (
                    isinstance(expires_in, bool)
                    or not isinstance(expires_in, int)
                    or expires_in <= 0
                    or expires_in > 366 * 24 * 60 * 60
                )
            )
        ):
            raise WorkbenchIntegrationError(
                "OAuth token response is invalid", 502, "provider_response_invalid"
            )
        raw_scope = value.get("scope")
        if raw_scope is None:
            scopes = list(provider.allowed_scopes)
        elif isinstance(raw_scope, str):
            scopes = [scope for scope in raw_scope.split() if scope]
        elif isinstance(raw_scope, list):
            scopes = [str(scope) for scope in raw_scope]
        else:
            scopes = []
        if not scopes or not set(scopes).issubset(provider.allowed_scopes):
            raise WorkbenchIntegrationError(
                "OAuth granted scopes exceed the configured allowlist",
                502,
                "scope_invalid",
            )
        token = {
            "access_token": access_token,
            "token_type": "Bearer",
            "scopes": sorted(set(scopes)),
            "expires_in": expires_in,
        }
        if refresh_token:
            token["refresh_token"] = refresh_token
        if id_token:
            token["id_token"] = id_token
        return token

    @classmethod
    async def _revoke_request(cls, provider: OAuthProvider, token: str) -> None:
        status, _, _ = await cls._oauth_post(
            provider,
            provider.revocation_url,
            {
                "token": token,
                "client_id": provider.client_id,
            },
        )
        if status < 200 or status >= 300:
            raise WorkbenchIntegrationError(
                "OAuth provider revocation was rejected", 502, "revocation_rejected"
            )

    @classmethod
    async def _enqueue_revocation(
        cls,
        db: AsyncSession,
        *,
        binding_id: str,
        customer_id: int,
        application_id: str,
        credential_id: str,
        provider_id: str,
        token: dict[str, Any],
    ) -> str:
        provider = WorkbenchIntegrationPolicy.provider(provider_id)
        revoke_token = token.get("refresh_token") or token.get("access_token")
        if not provider.revocation_url or not isinstance(revoke_token, str) or not revoke_token:
            raise WorkbenchIntegrationError(
                "OAuth token cannot be safely revoked", 503, "revocation_contract_missing"
            )
        plaintext = cls._canonical({"token": revoke_token})
        ciphertext, key_id = WorkbenchIntegrationCrypto.encrypt(
            "connector-token", plaintext
        )
        revocation_id = f"wor_{uuid.uuid4().hex}"
        db.add(
            WorkbenchOAuthRevocation(
                RevocationId=revocation_id,
                BindingId=binding_id,
                CustomerId=customer_id,
                ApplicationId=application_id,
                CredentialId=credential_id,
                ProviderId=provider_id,
                TokenCiphertext=ciphertext,
                EncryptionKeyId=key_id,
                TokenDigest=hashlib.sha256(plaintext).hexdigest(),
                Status="pending",
                AttemptCount=0,
                AvailableAt=cls._now(),
                LeaseExpiresAt=None,
                LastErrorCode="",
                CompletedAt=None,
            )
        )
        await db.flush()
        return revocation_id

    @classmethod
    async def _stage_revocation(
        cls,
        db: AsyncSession,
        *,
        binding_id: str,
        customer_id: int,
        application_id: str,
        credential_id: str,
        provider_id: str,
        token: dict[str, Any],
    ) -> str:
        revocation_id = await cls._enqueue_revocation(
            db,
            binding_id=binding_id,
            customer_id=customer_id,
            application_id=application_id,
            credential_id=credential_id,
            provider_id=provider_id,
            token=token,
        )
        await db.commit()
        return revocation_id

    @classmethod
    async def _attempt_staged_revocation(
        cls, db: AsyncSession, revocation_id: str
    ) -> None:
        try:
            await cls.drain_revocations(db, 1, revocation_id=revocation_id)
        except Exception:  # fail closed; the encrypted durable row is retried by the worker
            await db.rollback()

    @classmethod
    async def _attempt_direct_revocation(
        cls, provider: OAuthProvider, token: dict[str, Any]
    ) -> None:
        revoke_token = token.get("refresh_token") or token.get("access_token")
        if not isinstance(revoke_token, str) or not revoke_token:
            return
        try:
            await cls._revoke_request(provider, revoke_token)
        except Exception:
            # This path is reached only when the durable database staging itself
            # failed. Never surface token material while preserving fail-closed
            # browser behavior.
            return

    @classmethod
    async def drain_revocations(
        cls,
        db: AsyncSession,
        batch_size: int,
        *,
        revocation_id: str = "",
    ) -> int:
        now = cls._now()
        filters = [
            or_(
                WorkbenchOAuthRevocation.Status == "pending",
                (
                    (WorkbenchOAuthRevocation.Status == "processing")
                    & (WorkbenchOAuthRevocation.LeaseExpiresAt <= now)
                ),
            ),
            WorkbenchOAuthRevocation.AvailableAt <= now,
        ]
        if revocation_id:
            filters.append(WorkbenchOAuthRevocation.RevocationId == revocation_id)
        rows = (
            await db.execute(
                select(WorkbenchOAuthRevocation)
                .where(*filters)
                .order_by(WorkbenchOAuthRevocation.AvailableAt.asc())
                .limit(max(1, min(int(batch_size), 100)))
                .with_for_update()
            )
        ).scalars().all()
        lease_until = now + timedelta(
            seconds=tagentic_config.WORKBENCH_OAUTH_REVOCATION_LEASE_SECONDS
        )
        claimed: list[tuple[Any, str, str, str, str, int]] = []
        for row in rows:
            row.Status = "processing"
            row.AttemptCount += 1
            row.LeaseExpiresAt = lease_until
            row.LastErrorCode = ""
            db.add(row)
            claimed.append(
                (
                    row.Id,
                    row.CredentialId,
                    row.ProviderId,
                    row.TokenCiphertext,
                    row.TokenDigest,
                    int(row.AttemptCount),
                )
            )
        await db.commit()
        completed = 0
        for (
            row_id,
            credential_id,
            provider_id,
            ciphertext,
            token_digest,
            attempt_count,
        ) in claimed:
            status = "completed"
            error_code = ""
            try:
                provider = WorkbenchIntegrationPolicy.provider(provider_id)
                value = json.loads(
                    WorkbenchIntegrationCrypto.decrypt(
                        "connector-token", ciphertext
                    )
                )
                token = value.get("token") if isinstance(value, dict) else None
                if not isinstance(token, str) or not token:
                    raise WorkbenchIntegrationError(
                        "OAuth revocation token is invalid", 409, "revocation_token_invalid"
                    )
                await cls._revoke_request(provider, token)
            except Exception as error:  # provider errors are projected to a bounded code
                status = "failed" if attempt_count >= 12 else "pending"
                error_code = getattr(error, "code", "provider_revoke_failed")
                if not isinstance(error_code, str) or len(error_code) > 64:
                    error_code = "provider_revoke_failed"
            values: dict[str, Any] = {
                "Status": status,
                "LeaseExpiresAt": None,
                "LastErrorCode": error_code,
            }
            if status == "completed":
                values.update(
                    {
                        "TokenCiphertext": "",
                        "EncryptionKeyId": "",
                        "CompletedAt": cls._now(),
                    }
                )
                completed += 1
            else:
                delay = min(3600, 2 ** min(attempt_count, 11))
                values["AvailableAt"] = cls._now() + timedelta(seconds=delay)
            await db.execute(
                update(WorkbenchOAuthRevocation)
                .where(
                    WorkbenchOAuthRevocation.Id == row_id,
                    WorkbenchOAuthRevocation.Status == "processing",
                    WorkbenchOAuthRevocation.TokenDigest == token_digest,
                )
                .values(**values)
            )
            if credential_id and status in {"completed", "failed"}:
                await db.execute(
                    update(WorkbenchConnectorCredential)
                    .where(
                        WorkbenchConnectorCredential.CredentialId == credential_id,
                        WorkbenchConnectorCredential.Status == "revoked",
                        WorkbenchConnectorCredential.RevocationStatus.in_(
                            {"provider_revoke_pending", "revocation_pending_control_event"}
                        ),
                    )
                    .values(
                        RevocationStatus=(
                            "provider_revoked"
                            if status == "completed"
                            else "provider_unknown"
                        )
                    )
                )
            await db.commit()
        return completed
