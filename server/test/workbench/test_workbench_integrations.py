import base64
import hashlib
import json
import stat
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.workbench_integrations as integrations_module
from app_factory import TAgenticApp
from config import tagentic_config
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_integration_crypto import WorkbenchIntegrationCrypto
from core.workbench_integration_policy import (
    WorkbenchIntegrationPolicy,
    WorkbenchIntegrationPolicyError,
)
from core.workbench_integrations import WorkbenchIntegrationError, WorkbenchIntegrations
from core.workbench_identity import CoreWorkbenchIdentity
from model.workbench import (
    WorkbenchAgentBinding,
    WorkbenchBrowserSession,
    WorkbenchIdentity,
    WorkbenchTurn,
)
from model.workbench_integration import (
    WorkbenchConnectorCredential,
    WorkbenchConnectorScope,
    WorkbenchIntegrationAudit,
    WorkbenchIntegrationBinding,
    WorkbenchOAuthRevocation,
    WorkbenchOAuthState,
)


ACCOUNT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ACCOUNT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SESSION_ID = "s" * 43
BROWSER_COOKIE = "c" * 43
AUTH_TIME = int(datetime.now(UTC).timestamp())


class _AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    def add(self, value):
        self.session.add(value)

    async def execute(self, statement, params=None):
        return self.session.execute(statement, params or {})

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def flush(self):
        self.session.flush()

    @asynccontextmanager
    async def begin_nested(self):
        with self.session.begin_nested():
            yield


def _identity(*, binding="binding-a", account=ACCOUNT_A, customer=7, user=9, app="app-a"):
    del account
    return WorkbenchIdentityContext(
        binding_id=binding,
        canonical_subject=f"napi:prod:customer:{customer}:user:{user}",
        customer_id=customer,
        new_api_user_id=user,
        auth_epoch=3,
        display_name=f"User {user}",
        application_id=app,
        app_profile_id="17",
        access_mode="active",
        config_version=5,
    )


def _app_context(*, app="app-a", provider_app="provider-app-a"):
    return WorkbenchAppContext(
        application_id=app,
        app_profile_id="17",
        config_version=5,
        auth_epoch=3,
        vendor="Tencent",
        service_vendor="ChinaTencentCloud",
        app_id=provider_app,
        app_key="provider-key",
        space_id="space-a",
        template_agent_id="template-a",
        secret_id="secret-id",
        secret_key="secret-key",
        capabilities=(
            "chat",
            "skills",
            "tools",
            "connectors",
            "catalog_skills",
            "catalog_plugins",
        ),
        limits={
            "customer_concurrency": 2,
            "user_concurrency": 1,
            "max_runtime_seconds": 120,
            "max_reasoning_rounds": 5,
            "max_output_tokens": 2048,
            "web_search_per_turn": 0,
            "max_file_bytes": 1024,
        },
    )


def _session_claims(*, session_id=SESSION_ID, auth_time: int | None = None):
    return {
        "AccountId": str(ACCOUNT_A),
        "BindingId": "binding-a",
        "Subject": "napi:prod:customer:7:user:9",
        "AuthEpoch": 3,
        "ApplicationId": "app-a",
        "AppProfileId": "17",
        "ConfigVersion": 5,
        "sid": session_id,
        "auth_time": auth_time or AUTH_TIME,
        "token_source": "workbench_sso",
    }


class _ProviderIntegrationVendor:
    def __init__(self):
        self.application_id = "app-a"
        self.config = {
            "Vendor": "Tencent",
            "ServiceVendor": "ChinaTencentCloud",
            "AppId": "provider-app-a",
            "AppKey": "provider-key",
            "SpaceId": "space-a",
            "SecretId": "secret-id",
            "SecretKey": "secret-key",
        }
        self.calls = []
        self.plugin_configs = {}
        self.tool_configs = {}
        self.details = {
            "plugin-safe": self._detail("plugin-safe", "tool-read", 0, 0),
            "connector-safe": self._detail("connector-safe", "connector-read", 1, 0),
        }

    @staticmethod
    def _detail(plugin_id, tool_id, plugin_class, auth_type, oauth_consent=None):
        auth = {"AuthType": auth_type}
        if oauth_consent is not None:
            auth["OAuthConsent"] = oauth_consent
        return {
            "Plugin": {
                "PluginId": plugin_id,
                "Status": 1,
                "Profile": {
                    "Name": plugin_id,
                    "Description": "read-only test integration",
                    "PluginClass": plugin_class,
                },
                "Config": {"MCPPluginConfig": {"AuthConfig": auth}},
                "ToolList": [
                    {
                        "PluginId": plugin_id,
                        "ToolId": tool_id,
                        "Name": tool_id,
                        "Description": "read data",
                        "ToolAccessMode": 1,
                        "ToolConfig": {
                            "MCPToolConfig": {"Inputs": [], "Outputs": []}
                        },
                    }
                ],
            }
        }

    def _agent_detail(self):
        return {
            "Agent": {
                "AgentId": "agent-a",
                "PluginList": [
                    {
                        "Config": config,
                        "AuthConfigStatus": 2,
                        "Status": 1,
                        "PluginClass": self.details[plugin_id]["Plugin"]["Profile"]["PluginClass"],
                    }
                    for plugin_id, config in sorted(self.plugin_configs.items())
                ],
                "ToolList": [
                    {
                        "Config": config,
                        "ToolAccessMode": 1,
                        "Status": 1,
                    }
                    for _, config in sorted(self.tool_configs.items())
                ],
            }
        }

    async def forward_request(self, action, payload):
        self.calls.append((action, payload))
        if action == "DescribePluginSummaryList":
            allowed = set(payload["FilterList"][0]["ValueList"])
            items = [
                {"PluginId": plugin_id}
                for plugin_id in sorted(allowed.intersection(self.details))
            ]
            return {"PluginList": items, "TotalCount": len(items)}
        if action == "DescribePlugin":
            return self.details[payload["PluginId"]]
        if action == "DescribeAgentDetail":
            return self._agent_detail()
        if action == "ModifyAgent":
            assert payload["UpdateMask"] == {"Paths": ["PluginList", "ToolList"]}
            self.plugin_configs = {
                item["PluginId"]: item for item in payload["Agent"]["PluginList"]
            }
            self.tool_configs = {
                (item["Config"]["PluginId"], item["Config"]["ToolId"]): item["Config"]
                for item in payload["Agent"]["ToolList"]
            }
            return {"RequestId": "modify-a"}
        raise AssertionError(action)


def _install_provider_vendor(monkeypatch, vendor=None):
    vendor = vendor or _ProviderIntegrationVendor()
    monkeypatch.setattr(
        TAgenticApp,
        "get_app",
        classmethod(
            lambda cls: SimpleNamespace(
                get_vendor_app=lambda application_id: vendor
            )
        ),
    )
    return vendor


@pytest.fixture
def integration_store(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def register_uuid(dbapi_connection, _connection_record):
        dbapi_connection.create_function("uuid_generate_v4", 0, lambda: uuid.uuid4().hex)

    for model in (
        WorkbenchIdentity,
        WorkbenchBrowserSession,
        WorkbenchAgentBinding,
        WorkbenchTurn,
        WorkbenchIntegrationBinding,
        WorkbenchConnectorCredential,
        WorkbenchConnectorScope,
        WorkbenchOAuthState,
        WorkbenchOAuthRevocation,
        WorkbenchIntegrationAudit,
    ):
        model.__table__.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    authenticated_at = datetime.fromtimestamp(AUTH_TIME, UTC).replace(tzinfo=None)
    with sessions() as session:
        session.add(
            WorkbenchIdentity(
                BindingId="binding-a",
                CanonicalSubject="napi:prod:customer:7:user:9",
                AccountId=ACCOUNT_A,
                CustomerId=7,
                NewApiUserId=9,
                AuthEpoch=3,
                Status="active",
                LastAuthenticatedAt=authenticated_at,
            )
        )
        session.add(
            WorkbenchBrowserSession(
                SessionIdDigest=hashlib.sha256(SESSION_ID.encode("ascii")).hexdigest(),
                BindingId="binding-a",
                AccountId=ACCOUNT_A,
                CustomerId=7,
                NewApiUserId=9,
                CanonicalSubject="napi:prod:customer:7:user:9",
                AuthEpoch=3,
                ApplicationId="app-a",
                AppProfileId="17",
                ConfigVersion=5,
                AuthenticatedAt=authenticated_at,
                ExpiresAt=authenticated_at + timedelta(hours=1),
                Status="active",
            )
        )
        session.add(
            WorkbenchAgentBinding(
                BindingId="binding-a",
                AccountId=ACCOUNT_A,
                ApplicationId="app-a",
                AgentId="agent-a",
                Status="active",
                AttemptId="attempt-a",
            )
        )
        session.commit()

    @asynccontextmanager
    async def connection():
        session = sessions()
        try:
            yield _AsyncSessionAdapter(session)
        finally:
            session.close()

    allowlist = {
        "applications": {
            "app-a": {
                "config_versions": [5],
                "resources": [
                    {
                        "kind": "skill",
                        "id": "skill-safe",
                        "name_zh": "安全 Skill",
                        "name_en": "Safe Skill",
                    },
                    {
                        "kind": "connector",
                        "id": "connector-safe",
                        "name_zh": "安全连接器",
                        "name_en": "Safe Connector",
                        "provider_id": "example",
                        "requires_oauth": True,
                    },
                    {
                        "kind": "plugin",
                        "id": "plugin-safe",
                        "name_zh": "安全插件",
                        "name_en": "Safe Plugin",
                    },
                    {
                        "kind": "tool",
                        "id": "tool-read",
                        "parent_id": "plugin-safe",
                        "name_zh": "只读工具",
                        "name_en": "Read tool",
                    },
                ],
            },
            "app-b": {
                "config_versions": [5],
                "resources": [
                    {
                        "kind": "connector",
                        "id": "connector-safe",
                        "name_zh": "安全连接器",
                        "name_en": "Safe Connector",
                        "provider_id": "example",
                        "requires_oauth": True,
                    }
                ],
            },
        }
    }
    providers = {
        "providers": {
            "example": {
                "authorization_url": "https://oauth.example.com/authorize",
                "token_url": "https://oauth.example.com/token",
                "revocation_url": "https://oauth.example.com/revoke",
                "client_id": "client-a",
                "client_secret_ref": "example-client-secret",
                "token_auth_method": "client_secret_basic",
                "allowed_scopes": ["files.read", "profile"],
                "allowed_hosts": ["oauth.example.com"],
            }
        }
    }
    monkeypatch.setattr(tagentic_config, "WORKBENCH_MODE", True)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_INTEGRATIONS_ENABLED", True)
    monkeypatch.setattr(
        tagentic_config, "WORKBENCH_INTEGRATION_ALLOWLIST_JSON", json.dumps(allowlist)
    )
    monkeypatch.setattr(tagentic_config, "WORKBENCH_INTEGRATION_ALLOWLIST_FILE", "")
    monkeypatch.setattr(
        tagentic_config, "WORKBENCH_OAUTH_PROVIDERS_JSON", json.dumps(providers)
    )
    monkeypatch.setattr(tagentic_config, "WORKBENCH_OAUTH_PROVIDERS_FILE", "")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_PUBLIC_BASE_URL", "https://gateway.example/workbench")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_INTEGRATION_REAUTH_SECONDS", 300)
    monkeypatch.setattr(tagentic_config, "WORKBENCH_OAUTH_STATE_TTL_SECONDS", 300)
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_CONNECTOR_TOKEN_KEY",
        base64.b64encode(b"t" * 32).decode(),
    )
    monkeypatch.setattr(tagentic_config, "WORKBENCH_CONNECTOR_TOKEN_KEY_ID", "token-v2")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_CONNECTOR_TOKEN_PREVIOUS_KEYS_JSON", "{}")
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_OAUTH_STATE_KEY",
        base64.b64encode(b"s" * 32).decode(),
    )
    monkeypatch.setattr(tagentic_config, "WORKBENCH_OAUTH_STATE_KEY_ID", "state-v2")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_OAUTH_STATE_PREVIOUS_KEYS_JSON", "{}")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_CONNECTOR_TOKEN_KEY_FILE", "")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_CONNECTOR_TOKEN_PREVIOUS_KEYS_FILE", "")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_OAUTH_STATE_KEY_FILE", "")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_OAUTH_STATE_PREVIOUS_KEYS_FILE", "")
    (tmp_path / "example-client-secret").write_text("example-secret-value", encoding="utf-8")
    monkeypatch.setattr(tagentic_config, "WORKBENCH_OAUTH_SECRET_DIR", str(tmp_path))
    yield sessions, connection
    engine.dispose()


def test_allowlist_intersects_package_and_rejects_cross_app_ids(integration_store):
    resources = WorkbenchIntegrationPolicy.catalog(_app_context())
    assert {resource.kind for resource in resources} == {"skill", "plugin", "tool", "connector"}

    reduced = _app_context()
    reduced = WorkbenchAppContext(**{**reduced.__dict__, "capabilities": ("catalog_skills",)})
    assert WorkbenchIntegrationPolicy.catalog(reduced) == ()

    with pytest.raises(WorkbenchIntegrationPolicyError, match="not allowed"):
        WorkbenchIntegrationPolicy.resource(_app_context(app="app-b"), "skill", "skill-safe")


def test_oauth_metadata_forbids_literal_secret_and_non_https(integration_store, monkeypatch):
    bad = {
        "providers": {
            "example": {
                "authorization_url": "http://127.0.0.1/authorize",
                "token_url": "https://oauth.example.com/token",
                "client_id": "client-a",
                "client_secret_ref": "literal secret value",
                "allowed_scopes": ["profile"],
                "allowed_hosts": ["oauth.example.com"],
            }
        }
    }
    monkeypatch.setattr(tagentic_config, "WORKBENCH_OAUTH_PROVIDERS_JSON", json.dumps(bad))
    with pytest.raises(WorkbenchIntegrationPolicyError):
        WorkbenchIntegrationPolicy.provider("example")


def test_oauth_client_secret_rejects_file_and_directory_symlinks(
    integration_store, monkeypatch
):
    provider = WorkbenchIntegrationPolicy.provider("example")
    root = Path(tagentic_config.WORKBENCH_OAUTH_SECRET_DIR)
    secret_path = root / provider.client_secret_ref
    original_lstat = Path.lstat

    def file_symlink_lstat(path):
        if path == secret_path:
            return SimpleNamespace(st_mode=stat.S_IFLNK, st_size=20)
        return original_lstat(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "lstat", file_symlink_lstat)
        with pytest.raises(WorkbenchIntegrationPolicyError, match="unavailable"):
            WorkbenchIntegrationPolicy.client_secret(provider)

    def directory_symlink_lstat(path):
        if path == root:
            return SimpleNamespace(st_mode=stat.S_IFLNK, st_size=0)
        return original_lstat(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "lstat", directory_symlink_lstat)
        with pytest.raises(WorkbenchIntegrationPolicyError, match="unavailable"):
            WorkbenchIntegrationPolicy.client_secret(provider)


def test_token_jwe_uses_active_and_previous_keys_without_plaintext(integration_store, monkeypatch):
    ciphertext, key_id = WorkbenchIntegrationCrypto.encrypt(
        "connector-token", b'{"access_token":"never-return-this"}'
    )
    assert key_id == "token-v2"
    assert "never-return-this" not in ciphertext
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_CONNECTOR_TOKEN_KEY",
        base64.b64encode(b"n" * 32).decode(),
    )
    monkeypatch.setattr(tagentic_config, "WORKBENCH_CONNECTOR_TOKEN_KEY_ID", "token-v3")
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_CONNECTOR_TOKEN_PREVIOUS_KEYS_JSON",
        json.dumps({"token-v2": base64.b64encode(b"t" * 32).decode()}),
    )
    assert WorkbenchIntegrationCrypto.decrypt("connector-token", ciphertext) == (
        b'{"access_token":"never-return-this"}'
    )


@pytest.mark.asyncio
async def test_oauth_start_has_pkce_state_nonce_and_persists_only_digests(
    integration_store, monkeypatch
):
    _, connection = integration_store
    monkeypatch.setattr(
        WorkbenchIntegrations,
        "require_recent_reauthentication",
        AsyncMock(return_value=None),
    )
    async with connection() as db:
        response = await WorkbenchIntegrations.start_oauth(
            db,
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            connector_id="connector-safe",
            session_claims=_session_claims(),
        )
        state_row = (await db.execute(WorkbenchOAuthState.__table__.select())).first()

    query = parse_qs(urlsplit(response["authorization_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43
    assert query["nonce"][0]
    assert query["state"][0].startswith("wbo1.")
    assert query["redirect_uri"] == [
        "https://gateway.example/workbench/integrations/oauth/callback/example"
    ]
    assert query["state"][0] not in state_row.StateDigest
    assert query["nonce"][0] not in state_row.NonceDigest
    assert "code_verifier" not in response


@pytest.mark.asyncio
async def test_oauth_callback_requires_same_browser_session_and_transaction_cookie(
    integration_store,
):
    _, connection = integration_store
    async with connection() as db:
        started = await WorkbenchIntegrations.start_oauth(
            db,
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            connector_id="connector-safe",
            session_claims=_session_claims(),
        )
        state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
        rejected = await WorkbenchIntegrations.finish_oauth(
            db,
            provider_id="example",
            state=state,
            code="",
            provider_error="access_denied",
            browser_cookie="x" * 43,
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            session_claims=_session_claims(),
        )
        state_row = (
            await db.execute(select(WorkbenchOAuthState).where(WorkbenchOAuthState.Status == "pending"))
        ).scalar()

    assert rejected.location.endswith("integration_oauth=invalid_state")
    assert state_row is not None


@pytest.mark.asyncio
async def test_oauth_callback_persists_only_after_full_scope_reauthorization(
    integration_store, monkeypatch
):
    _, connection = integration_store
    authorize_offline = AsyncMock(return_value=(_identity(), _app_context()))
    token_request = AsyncMock(
        return_value={
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "id_token": None,
            "token_type": "Bearer",
            "scopes": ["files.read", "profile"],
            "expires_in": 3600,
        }
    )
    monkeypatch.setattr(
        CoreWorkbenchIdentity, "authorize_offline_scope", authorize_offline
    )
    monkeypatch.setattr(WorkbenchIntegrations, "_token_request", token_request)

    async with connection() as db:
        started = await WorkbenchIntegrations.start_oauth(
            db,
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            connector_id="connector-safe",
            session_claims=_session_claims(),
        )
        state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
        result = await WorkbenchIntegrations.finish_oauth(
            db,
            provider_id="example",
            state=state,
            code="provider-code",
            provider_error="",
            browser_cookie=started["_browser_cookie"],
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            session_claims=_session_claims(),
        )
        credential = (await db.execute(select(WorkbenchConnectorCredential))).scalar()
        state_row = (await db.execute(select(WorkbenchOAuthState))).scalar()

    assert result.location.endswith("integration_oauth=success")
    assert credential is not None
    assert credential.Status == "active"
    assert credential.TokenCiphertext not in {"access-secret", "refresh-secret"}
    assert state_row.Status == "consumed"
    assert authorize_offline.await_count == 2
    token_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_oauth_callback_revokes_exchanged_token_when_reauthorization_fails(
    integration_store, monkeypatch
):
    _, connection = integration_store
    async with connection() as db:
        started = await WorkbenchIntegrations.start_oauth(
            db,
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            connector_id="connector-safe",
            session_claims=_session_claims(),
        )
        state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]

        monkeypatch.setattr(
            CoreWorkbenchIdentity,
            "authorize_offline_scope",
            AsyncMock(
                side_effect=[
                    (_identity(), _app_context()),
                    RuntimeError("membership changed"),
                ]
            ),
        )
        monkeypatch.setattr(
            WorkbenchIntegrations,
            "_token_request",
            AsyncMock(
                return_value={
                    "access_token": "new-access-secret",
                    "refresh_token": "new-refresh-secret",
                    "token_type": "Bearer",
                    "scopes": ["files.read"],
                    "expires_in": 3600,
                }
            ),
        )
        revoke = AsyncMock(return_value=None)
        monkeypatch.setattr(WorkbenchIntegrations, "_revoke_request", revoke)

        result = await WorkbenchIntegrations.finish_oauth(
            db,
            provider_id="example",
            state=state,
            code="provider-code",
            provider_error="",
            browser_cookie=started["_browser_cookie"],
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            session_claims=_session_claims(),
        )
        outbox = (
            await db.execute(
                select(WorkbenchOAuthRevocation).execution_options(populate_existing=True)
            )
        ).scalar()
        credential = (await db.execute(select(WorkbenchConnectorCredential))).scalar()

    assert result.location.endswith("integration_oauth=oauth_failed_closed")
    assert credential is None
    assert outbox.Status == "completed"
    assert outbox.TokenCiphertext == ""
    revoke.assert_awaited_once()
    assert revoke.await_args.args[1] == "new-refresh-secret"


@pytest.mark.asyncio
async def test_oauth_callback_directly_revokes_when_durable_staging_fails(
    integration_store, monkeypatch
):
    _, connection = integration_store
    async with connection() as db:
        started = await WorkbenchIntegrations.start_oauth(
            db,
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            connector_id="connector-safe",
            session_claims=_session_claims(),
        )
        state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
        monkeypatch.setattr(
            CoreWorkbenchIdentity,
            "authorize_offline_scope",
            AsyncMock(return_value=(_identity(), _app_context())),
        )
        monkeypatch.setattr(
            WorkbenchIntegrations,
            "_token_request",
            AsyncMock(
                return_value={
                    "access_token": "stage-access-secret",
                    "refresh_token": "stage-refresh-secret",
                    "token_type": "Bearer",
                    "scopes": ["files.read"],
                    "expires_in": 3600,
                }
            ),
        )
        monkeypatch.setattr(
            WorkbenchIntegrations,
            "_stage_revocation",
            AsyncMock(side_effect=RuntimeError("database unavailable")),
        )
        revoke = AsyncMock(return_value=None)
        monkeypatch.setattr(WorkbenchIntegrations, "_revoke_request", revoke)

        result = await WorkbenchIntegrations.finish_oauth(
            db,
            provider_id="example",
            state=state,
            code="provider-code",
            provider_error="",
            browser_cookie=started["_browser_cookie"],
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            session_claims=_session_claims(),
        )

    assert result.location.endswith("integration_oauth=oauth_failed_closed")
    revoke.assert_awaited_once()
    assert revoke.await_args.args[1] == "stage-refresh-secret"


@pytest.mark.asyncio
async def test_disconnect_supersedes_pending_callback_before_it_can_reconnect(
    integration_store,
):
    _, connection = integration_store
    async with connection() as db:
        started = await WorkbenchIntegrations.start_oauth(
            db,
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            connector_id="connector-safe",
            session_claims=_session_claims(),
        )
        state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
        disconnected = await WorkbenchIntegrations.disconnect(
            db,
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            connector_id="connector-safe",
            session_claims=_session_claims(),
        )
        callback = await WorkbenchIntegrations.finish_oauth(
            db,
            provider_id="example",
            state=state,
            code="provider-code",
            provider_error="",
            browser_cookie=started["_browser_cookie"],
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            session_claims=_session_claims(),
        )
        state_row = (await db.execute(select(WorkbenchOAuthState))).scalar()
        scope = (await db.execute(select(WorkbenchConnectorScope))).scalar()
        credential = (await db.execute(select(WorkbenchConnectorCredential))).scalar()

    assert disconnected["status"] == "disconnected"
    assert state_row.Status == "superseded"
    assert scope.Generation == 2
    assert callback.location.endswith("integration_oauth=invalid_state")
    assert credential is None


@pytest.mark.asyncio
async def test_disconnect_durably_revokes_provider_token(
    integration_store, monkeypatch
):
    sessions, connection = integration_store
    now = datetime.now(UTC).replace(tzinfo=None)
    ciphertext, key_id = WorkbenchIntegrationCrypto.encrypt(
        "connector-token",
        b'{"access_token":"disconnect-access","refresh_token":"disconnect-refresh"}',
    )
    with sessions() as session:
        session.add(
            WorkbenchConnectorCredential(
                CredentialId="wcc_disconnect",
                BindingId="binding-a",
                AccountId=ACCOUNT_A,
                CustomerId=7,
                NewApiUserId=9,
                CanonicalSubject="napi:prod:customer:7:user:9",
                ApplicationId="app-a",
                ProviderAppId="provider-app-a",
                AppProfileId="17",
                ConfigVersion=5,
                AuthEpoch=3,
                ProviderId="example",
                ConnectorId="connector-safe",
                TokenCiphertext=ciphertext,
                EncryptionKeyId=key_id,
                TokenDigest="d" * 64,
                GrantedScopesJson='["files.read"]',
                TokenType="Bearer",
                Status="active",
                RevocationStatus="not_requested",
                ConnectedAt=now,
            )
        )
        session.commit()
    revoke = AsyncMock(return_value=None)
    monkeypatch.setattr(WorkbenchIntegrations, "_revoke_request", revoke)

    async with connection() as db:
        result = await WorkbenchIntegrations.disconnect(
            db,
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            connector_id="connector-safe",
            session_claims=_session_claims(),
        )
        outbox = (
            await db.execute(
                select(WorkbenchOAuthRevocation).execution_options(populate_existing=True)
            )
        ).scalar()

    assert result["status"] == "revoked"
    assert result["revocation_status"] == "provider_revoked"
    assert outbox.Status == "completed"
    assert outbox.TokenCiphertext == ""
    revoke.assert_awaited_once()
    assert revoke.await_args.args[1] == "disconnect-refresh"


@pytest.mark.asyncio
async def test_new_oauth_start_supersedes_older_start_for_same_connector(
    integration_store,
):
    _, connection = integration_store
    async with connection() as db:
        first = await WorkbenchIntegrations.start_oauth(
            db,
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            connector_id="connector-safe",
            session_claims=_session_claims(),
        )
        second = await WorkbenchIntegrations.start_oauth(
            db,
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            connector_id="connector-safe",
            session_claims=_session_claims(),
        )
        first_state = parse_qs(urlsplit(first["authorization_url"]).query)["state"][0]
        second_state = parse_qs(urlsplit(second["authorization_url"]).query)["state"][0]
        old_callback = await WorkbenchIntegrations.finish_oauth(
            db,
            provider_id="example",
            state=first_state,
            code="",
            provider_error="access_denied",
            browser_cookie=first["_browser_cookie"],
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            session_claims=_session_claims(),
        )
        newest_callback = await WorkbenchIntegrations.finish_oauth(
            db,
            provider_id="example",
            state=second_state,
            code="",
            provider_error="access_denied",
            browser_cookie=second["_browser_cookie"],
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            session_claims=_session_claims(),
        )

    assert old_callback.location.endswith("integration_oauth=invalid_state")
    assert newest_callback.location.endswith("integration_oauth=provider_denied")


@pytest.mark.asyncio
async def test_recent_auth_is_bound_to_the_presented_session_not_identity_last_login(
    integration_store,
):
    _, connection = integration_store
    old_session_id = "o" * 43
    old_auth_time = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10)
    async with connection() as db:
        db.add(
            WorkbenchBrowserSession(
                SessionIdDigest=hashlib.sha256(old_session_id.encode("ascii")).hexdigest(),
                BindingId="binding-a",
                AccountId=ACCOUNT_A,
                CustomerId=7,
                NewApiUserId=9,
                CanonicalSubject="napi:prod:customer:7:user:9",
                AuthEpoch=3,
                ApplicationId="app-a",
                AppProfileId="17",
                ConfigVersion=5,
                AuthenticatedAt=old_auth_time,
                ExpiresAt=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
                Status="active",
            )
        )
        await db.commit()
        with pytest.raises(WorkbenchIntegrationError) as exc_info:
            await WorkbenchIntegrations.require_recent_reauthentication(
                db,
                account_id=str(ACCOUNT_A),
                identity=_identity(),
                session_claims=_session_claims(
                    session_id=old_session_id,
                    auth_time=int(old_auth_time.replace(tzinfo=UTC).timestamp()),
                ),
            )

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "reauth_required"


@pytest.mark.asyncio
async def test_oauth_state_is_one_time_even_when_provider_denies(integration_store):
    _, connection = integration_store
    state_part = "a" * 43
    nonce_part = "b" * 43
    async with connection() as db:
        db.add(
            WorkbenchOAuthState(
                OAuthStateId="wos_replay",
                StateDigest=__import__("hashlib").sha256(state_part.encode()).hexdigest(),
                NonceDigest=__import__("hashlib").sha256(nonce_part.encode()).hexdigest(),
                BrowserCookieDigest=hashlib.sha256(BROWSER_COOKIE.encode()).hexdigest(),
                SessionIdDigest=hashlib.sha256(SESSION_ID.encode()).hexdigest(),
                VerifierCiphertext="not-used",
                EncryptionKeyId="state-v2",
                BindingId="binding-a",
                AccountId=ACCOUNT_A,
                CustomerId=7,
                NewApiUserId=9,
                CanonicalSubject="napi:prod:customer:7:user:9",
                ApplicationId="app-a",
                ProviderAppId="provider-app-a",
                AppProfileId="17",
                ConfigVersion=5,
                AuthEpoch=3,
                ProviderId="example",
                ConnectorId="connector-safe",
                ConnectionGeneration=1,
                RedirectUri="https://gateway.example/workbench/integrations/oauth/callback/example",
                RequestedScopesJson='["profile"]',
                Status="pending",
                ExpiresAt=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5),
            )
        )
        await db.commit()
        state = f"wbo1.{state_part}.{nonce_part}"
        db.add(
            WorkbenchConnectorScope(
                ScopeId="wcs_replay",
                BindingId="binding-a",
                AccountId=ACCOUNT_A,
                CustomerId=7,
                NewApiUserId=9,
                CanonicalSubject="napi:prod:customer:7:user:9",
                ApplicationId="app-a",
                ProviderAppId="provider-app-a",
                AppProfileId="17",
                ConfigVersion=5,
                AuthEpoch=3,
                ProviderId="example",
                ConnectorId="connector-safe",
                Generation=1,
            )
        )
        await db.commit()
        first = await WorkbenchIntegrations.finish_oauth(
            db,
            provider_id="example",
            state=state,
            code="",
            provider_error="access_denied",
            browser_cookie=BROWSER_COOKIE,
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            session_claims=_session_claims(),
        )
        second = await WorkbenchIntegrations.finish_oauth(
            db,
            provider_id="example",
            state=state,
            code="",
            provider_error="access_denied",
            browser_cookie=BROWSER_COOKIE,
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            session_claims=_session_claims(),
        )
    assert first.location.endswith("integration_oauth=provider_denied")
    assert second.location.endswith("integration_oauth=invalid_state")


@pytest.mark.asyncio
async def test_skill_binding_uses_trusted_scope_complete_set_and_narrow_mask(
    integration_store, monkeypatch
):
    _, connection = integration_store
    monkeypatch.setattr(
        WorkbenchIntegrations,
        "require_recent_reauthentication",
        AsyncMock(return_value=None),
    )

    class Vendor:
        def __init__(self):
            self.skills = set()
            self.calls = []

        async def forward_request(self, action, payload):
            self.calls.append((action, payload))
            if action == "ModifyAgent":
                self.skills = {item["SkillId"] for item in payload["Agent"]["SkillList"]}
                return {"RequestId": "request-a"}
            return {
                "Agent": {
                    "AgentId": "agent-a",
                    "SkillList": [{"SkillId": value} for value in sorted(self.skills)],
                }
            }

    vendor = Vendor()
    monkeypatch.setattr(
        TAgenticApp,
        "get_app",
        classmethod(lambda cls: SimpleNamespace(get_vendor_app=lambda application_id: vendor)),
    )
    async with connection() as db:
        result = await WorkbenchIntegrations.bind_skill(
            db,
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            kind="skill",
            resource_id="skill-safe",
            parent_id="",
            bind=True,
            session_claims=_session_claims(),
        )

    modify = next(payload for action, payload in vendor.calls if action == "ModifyAgent")
    assert modify == {
        "AppId": "provider-app-a",
        "AgentId": "agent-a",
        "Agent": {"SkillList": [{"SkillId": "skill-safe"}]},
        "UpdateMask": {"Paths": ["SkillList"]},
    }
    assert result["status"] == "active"
    assert "AppId" not in result


@pytest.mark.asyncio
async def test_active_turn_blocks_skill_change_before_provider_call(
    integration_store, monkeypatch
):
    _, connection = integration_store
    monkeypatch.setattr(
        WorkbenchIntegrations,
        "require_recent_reauthentication",
        AsyncMock(return_value=None),
    )
    async with connection() as db:
        db.add(
            WorkbenchTurn(
                TurnId="wt_active",
                BindingId="binding-a",
                AccountId=ACCOUNT_A,
                CustomerId=7,
                ApplicationId="app-a",
                ClientRequestId=str(uuid.uuid4()),
                RequestDigest="0" * 64,
                Status="running",
                OwnerInstanceId="test",
                OwnerLifecycleId="test",
                EventCount=0,
                EventBytes=0,
            )
        )
        await db.commit()
        with pytest.raises(WorkbenchIntegrationError) as exc_info:
            await WorkbenchIntegrations.bind_skill(
                db,
                account_id=str(ACCOUNT_A),
                identity=_identity(),
                app_context=_app_context(),
                kind="skill",
                resource_id="skill-safe",
                parent_id="",
                bind=True,
                session_claims=_session_claims(),
            )
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "active_turn"


@pytest.mark.asyncio
async def test_plugin_binding_submits_both_complete_lists_and_unbind_disables_tool(
    integration_store, monkeypatch
):
    _, connection = integration_store
    vendor = _install_provider_vendor(monkeypatch)
    monkeypatch.setattr(
        WorkbenchIntegrations,
        "require_recent_reauthentication",
        AsyncMock(return_value=None),
    )
    async with connection() as db:
        bound = await WorkbenchIntegrations.bind_skill(
            db,
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            kind="plugin",
            resource_id="plugin-safe",
            parent_id="",
            bind=True,
            session_claims=_session_claims(),
        )
        disabled = await WorkbenchIntegrations.bind_skill(
            db,
            account_id=str(ACCOUNT_A),
            identity=_identity(),
            app_context=_app_context(),
            kind="plugin",
            resource_id="plugin-safe",
            parent_id="",
            bind=False,
            session_claims=_session_claims(),
        )

    modifies = [payload for action, payload in vendor.calls if action == "ModifyAgent"]
    assert bound["status"] == "active"
    assert disabled["status"] == "revoked"
    assert len(modifies) == 2
    assert set(modifies[0]["Agent"]) == {"PluginList", "ToolList"}
    assert modifies[0]["Agent"]["ToolList"][0]["Config"]["IsDisabled"] is False
    assert modifies[1]["Agent"]["PluginList"] == modifies[0]["Agent"]["PluginList"]
    assert modifies[1]["Agent"]["ToolList"][0]["Config"]["IsDisabled"] is True
    assert vendor.plugin_configs  # physical PluginList was not cleared


@pytest.mark.asyncio
async def test_plugin_post_read_mismatch_marks_agent_and_binding_provider_unknown(
    integration_store, monkeypatch
):
    class MismatchingVendor(_ProviderIntegrationVendor):
        async def forward_request(self, action, payload):
            if action == "ModifyAgent":
                self.calls.append((action, payload))
                return {"RequestId": "modify-without-applying"}
            return await super().forward_request(action, payload)

    sessions, connection = integration_store
    _install_provider_vendor(monkeypatch, MismatchingVendor())
    monkeypatch.setattr(
        WorkbenchIntegrations,
        "require_recent_reauthentication",
        AsyncMock(return_value=None),
    )
    async with connection() as db:
        with pytest.raises(WorkbenchIntegrationError) as exc_info:
            await WorkbenchIntegrations.bind_skill(
                db,
                account_id=str(ACCOUNT_A),
                identity=_identity(),
                app_context=_app_context(),
                kind="plugin",
                resource_id="plugin-safe",
                parent_id="",
                bind=True,
                session_claims=_session_claims(),
            )
    assert exc_info.value.code == "provider_unknown"
    with sessions() as session:
        assert session.query(WorkbenchAgentBinding).one().Status == "provider_unknown"
        row = session.query(WorkbenchIntegrationBinding).one()
        assert row.Status == "provider_unknown"
        assert row.ProviderSyncStatus == "provider_unknown"


@pytest.mark.asyncio
async def test_token_response_is_reduced_and_metadata_projection_never_returns_tokens(
    integration_store, monkeypatch
):
    monkeypatch.setattr(
        WorkbenchIntegrations,
        "_oauth_post",
        AsyncMock(
            return_value=(
                200,
                json.dumps(
                    {
                        "access_token": "access-secret-value",
                        "refresh_token": "refresh-secret-value",
                        "token_type": "Bearer",
                        "scope": "profile files.read",
                        "expires_in": 3600,
                        "provider_private_field": "must-not-persist",
                    }
                ).encode(),
                "application/json",
            )
        ),
    )
    token = await WorkbenchIntegrations._token_request(
        WorkbenchIntegrationPolicy.provider("example"),
        {"grant_type": "authorization_code"},
    )
    assert "provider_private_field" not in token
    row = SimpleNamespace(
        CredentialId="wcc_a",
        ProviderId="example",
        ConnectorId="connector-safe",
        Status="active",
        RevocationStatus="not_requested",
        GrantedScopesJson='["files.read","profile"]',
        TokenType="Bearer",
        ExpiresAt=None,
        ConnectedAt=datetime.now(UTC).replace(tzinfo=None),
        TokenCiphertext="contains-secrets-but-must-not-project",
    )
    projected = WorkbenchIntegrations._project_credential(row)
    assert not {"access_token", "refresh_token", "id_token", "TokenCiphertext"}.intersection(projected)


@pytest.mark.asyncio
async def test_oauth_dns_rejects_private_addresses(integration_store, monkeypatch):
    class FakeLoop:
        async def getaddrinfo(self, *_args, **_kwargs):
            return [
                (
                    __import__("socket").AF_INET,
                    __import__("socket").SOCK_STREAM,
                    __import__("socket").IPPROTO_TCP,
                    "",
                    ("127.0.0.1", 443),
                )
            ]

    monkeypatch.setattr(integrations_module.asyncio, "get_running_loop", lambda: FakeLoop())
    with pytest.raises(WorkbenchIntegrationError) as exc_info:
        await WorkbenchIntegrations._resolve_public("oauth.example.com", 443)
    assert exc_info.value.code == "provider_unavailable"
    assert "127.0.0.1" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_provider_oauth_error_body_is_trimmed(integration_store, monkeypatch):
    monkeypatch.setattr(
        WorkbenchIntegrations,
        "_oauth_post",
        AsyncMock(
            return_value=(
                400,
                b'{"error_description":"secret provider diagnostic"}',
                "application/json",
            )
        ),
    )
    with pytest.raises(WorkbenchIntegrationError) as exc_info:
        await WorkbenchIntegrations._token_request(
            WorkbenchIntegrationPolicy.provider("example"),
            {"grant_type": "authorization_code"},
        )
    assert exc_info.value.code == "token_exchange_rejected"
    assert "diagnostic" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_control_event_revokes_credentials_bindings_and_pending_oauth(
    integration_store, monkeypatch
):
    sessions, connection = integration_store
    now = datetime.now(UTC).replace(tzinfo=None)
    token_ciphertext, _ = WorkbenchIntegrationCrypto.encrypt(
        "connector-token",
        b'{"access_token":"event-access","refresh_token":"event-refresh"}',
    )
    with sessions() as session:
        session.add(
            WorkbenchConnectorCredential(
                CredentialId="wcc_event",
                BindingId="binding-a",
                AccountId=ACCOUNT_A,
                CustomerId=7,
                NewApiUserId=9,
                CanonicalSubject="napi:prod:customer:7:user:9",
                ApplicationId="app-a",
                ProviderAppId="provider-app-a",
                AppProfileId="17",
                ConfigVersion=5,
                AuthEpoch=3,
                ProviderId="example",
                ConnectorId="connector-safe",
                TokenCiphertext=token_ciphertext,
                EncryptionKeyId="token-v2",
                TokenDigest="0" * 64,
                GrantedScopesJson='["profile"]',
                TokenType="Bearer",
                Status="active",
                RevocationStatus="not_requested",
                ConnectedAt=now,
            )
        )
        session.add(
            WorkbenchIntegrationBinding(
                IntegrationBindingId="wib_event",
                BindingId="binding-a",
                AccountId=ACCOUNT_A,
                CustomerId=7,
                NewApiUserId=9,
                CanonicalSubject="napi:prod:customer:7:user:9",
                ApplicationId="app-a",
                ProviderAppId="provider-app-a",
                AppProfileId="17",
                ConfigVersion=5,
                AuthEpoch=3,
                AgentId="agent-a",
                ResourceKind="skill",
                ResourceId="skill-safe",
                ParentResourceId="",
                Status="active",
                ProviderSyncStatus="verified",
                UserConsentedAt=now,
            )
        )
        session.add(
            WorkbenchOAuthState(
                OAuthStateId="wos_event",
                StateDigest="1" * 64,
                NonceDigest="2" * 64,
                BrowserCookieDigest="3" * 64,
                SessionIdDigest=hashlib.sha256(SESSION_ID.encode()).hexdigest(),
                VerifierCiphertext="encrypted",
                EncryptionKeyId="state-v2",
                BindingId="binding-a",
                AccountId=ACCOUNT_A,
                CustomerId=7,
                NewApiUserId=9,
                CanonicalSubject="napi:prod:customer:7:user:9",
                ApplicationId="app-a",
                ProviderAppId="provider-app-a",
                AppProfileId="17",
                ConfigVersion=5,
                AuthEpoch=3,
                ProviderId="example",
                ConnectorId="connector-safe",
                ConnectionGeneration=1,
                RedirectUri="https://gateway.example/callback",
                RequestedScopesJson='["profile"]',
                Status="pending",
                ExpiresAt=now + timedelta(minutes=5),
            )
        )
        session.commit()
    monkeypatch.setattr(integrations_module, "db_connection", connection)
    event = SimpleNamespace(
        event_type="SESSION_REVOKE",
        payload={
            "binding_id": "binding-a",
            "customer_id": 7,
            "new_api_user_id": 9,
            "auth_epoch": 4,
        },
    )
    await WorkbenchIntegrations.revoke_for_control_event(event)
    with sessions() as session:
        credential = session.query(WorkbenchConnectorCredential).one()
        binding = session.query(WorkbenchIntegrationBinding).one()
        state = session.query(WorkbenchOAuthState).one()
        revocation = session.query(WorkbenchOAuthRevocation).one()
        assert credential.Status == "revoked"
        assert credential.RevocationStatus == "revocation_pending_control_event"
        assert revocation.Status == "pending"
        assert "event-refresh" not in revocation.TokenCiphertext
        assert binding.Status == "revoked"
        assert state.Status == "revoked"


@pytest.mark.asyncio
async def test_catalog_idor_does_not_project_another_account_binding(
    integration_store, monkeypatch,
):
    _install_provider_vendor(monkeypatch)
    sessions, connection = integration_store
    now = datetime.now(UTC).replace(tzinfo=None)
    with sessions() as session:
        session.add(
            WorkbenchIntegrationBinding(
                IntegrationBindingId="wib_owner",
                BindingId="binding-a",
                AccountId=ACCOUNT_A,
                CustomerId=7,
                NewApiUserId=9,
                CanonicalSubject="napi:prod:customer:7:user:9",
                ApplicationId="app-a",
                ProviderAppId="provider-app-a",
                AppProfileId="17",
                ConfigVersion=5,
                AuthEpoch=3,
                AgentId="agent-a",
                ResourceKind="skill",
                ResourceId="skill-safe",
                ParentResourceId="",
                Status="active",
                ProviderSyncStatus="verified",
                UserConsentedAt=now,
            )
        )
        session.commit()
    attacker = _identity(binding="binding-b", account=ACCOUNT_B, customer=8, user=10)
    async with connection() as db:
        catalog = await WorkbenchIntegrations.catalog(
            db,
            account_id=str(ACCOUNT_B),
            identity=attacker,
            app_context=_app_context(),
        )
    skill = next(item for item in catalog["resources"] if item["resource_id"] == "skill-safe")
    assert skill["binding_status"] == "unbound"
