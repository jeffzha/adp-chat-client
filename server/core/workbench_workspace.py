import base64
import hashlib
import hmac
import json
from typing import Any

from jwcrypto import jwe, jwk
from jwcrypto.common import base64url_encode
from jwcrypto.jwe import InvalidJWEData
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import tagentic_config
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_resource_reporter import (
    WorkbenchResourceReporter,
    WorkbenchResourceReportError,
)
from model.workbench import (
    WorkbenchConversationWorkspace,
    WorkbenchFileWorkspace,
    WorkbenchWorkspace,
)


class WorkbenchWorkspaceError(RuntimeError):
    def __init__(self, message: str, status_code: int = 403):
        super().__init__(message)
        self.status_code = status_code


class CoreWorkbenchWorkspace:
    """Owns local Workspace handles independently of the provider schema.

    Every Conversation gets one distinct local Workspace. Files uploaded before
    a Turn remain account-owned and unassigned until explicitly used with an
    existing owned Conversation.
    """

    CONVERSATION_KIND = "conversation"
    MAX_WORKSPACES_PER_CONVERSATION = 1
    MAX_PROVIDER_LOCATOR_BYTES = 4096

    @staticmethod
    def require_path(value: Any, field_name: str = "workspace path") -> str:
        if not isinstance(value, str):
            raise WorkbenchWorkspaceError(f"{field_name} is invalid", 400)
        path = value.strip()
        if (
            not 1 <= len(path) <= 1024
            or not path.startswith("/workdir")
            or (path != "/workdir" and not path.startswith("/workdir/"))
            or "\\" in path
            or "//" in path
            or any(part in {"", ".", ".."} for part in path.split("/")[1:])
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
        ):
            raise WorkbenchWorkspaceError(
                f"{field_name} must be a canonical /workdir path",
                400,
            )
        return path

    @staticmethod
    def _assert_scope(
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> None:
        if (
            not identity.binding_id
            or identity.customer_id <= 0
            or not identity.application_id
            or identity.application_id != app_context.application_id
            or str(identity.app_profile_id) != str(app_context.app_profile_id)
            or int(identity.config_version) <= 0
            or int(identity.config_version) != int(app_context.config_version)
            or not app_context.app_id
        ):
            raise WorkbenchWorkspaceError("incomplete workbench workspace scope", 500)

    @staticmethod
    def _scope_filters(
        *,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> tuple:
        return (
            WorkbenchWorkspace.BindingId == identity.binding_id,
            WorkbenchWorkspace.AccountId == account_id,
            WorkbenchWorkspace.CustomerId == identity.customer_id,
            WorkbenchWorkspace.ApplicationId == app_context.application_id,
            WorkbenchWorkspace.ProviderAppId == app_context.app_id,
            WorkbenchWorkspace.AppProfileId == str(app_context.app_profile_id),
            WorkbenchWorkspace.ConfigVersion == int(app_context.config_version),
            WorkbenchWorkspace.Kind == CoreWorkbenchWorkspace.CONVERSATION_KIND,
        )

    @staticmethod
    def _conversation_scope_filters(
        *,
        conversation_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> tuple:
        return (
            WorkbenchWorkspace.ConversationId == conversation_id,
            WorkbenchWorkspace.BindingId == identity.binding_id,
            WorkbenchWorkspace.AccountId == account_id,
            WorkbenchWorkspace.CustomerId == identity.customer_id,
            WorkbenchWorkspace.ApplicationId == app_context.application_id,
            WorkbenchWorkspace.AppProfileId == str(app_context.app_profile_id),
            WorkbenchWorkspace.ConfigVersion == int(app_context.config_version),
            WorkbenchWorkspace.Kind == CoreWorkbenchWorkspace.CONVERSATION_KIND,
        )

    @staticmethod
    def _workspace_id(
        *,
        conversation_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> str:
        secret = str(tagentic_config.WORKBENCH_SERVICE_HMAC_SECRET or "")
        if not secret:
            raise WorkbenchWorkspaceError("workbench workspace key is not configured", 500)
        values = (
            str(conversation_id),
            identity.binding_id,
            str(account_id),
            str(identity.customer_id),
            app_context.application_id,
            str(app_context.app_profile_id),
            str(app_context.config_version),
        )
        digest = hmac.new(
            secret.encode("utf-8"),
            "\x1f".join(values).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"ww_{digest[:40]}"

    @staticmethod
    def _decode_locator_key(encoded: str, key_id: str) -> jwk.JWK:
        if not key_id or len(key_id) > 64:
            raise WorkbenchWorkspaceError("workspace locator key id is invalid", 500)
        try:
            key_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise WorkbenchWorkspaceError("workspace locator key is invalid", 500) from error
        if len(key_bytes) != 32:
            raise WorkbenchWorkspaceError(
                "workspace locator key must decode to 32 bytes",
                500,
            )
        return jwk.JWK(kty="oct", k=base64url_encode(key_bytes), kid=key_id)

    @classmethod
    def _active_locator_key(cls) -> tuple[str, jwk.JWK]:
        key_id = str(
            tagentic_config.WORKBENCH_WORKSPACE_LOCATOR_KEY_ID or ""
        ).strip()
        encoded = str(
            tagentic_config.WORKBENCH_WORKSPACE_LOCATOR_KEY or ""
        ).strip()
        return key_id, cls._decode_locator_key(encoded, key_id)

    @classmethod
    def _locator_key_for_id(cls, key_id: str) -> jwk.JWK:
        active_key_id, active_key = cls._active_locator_key()
        if key_id == active_key_id:
            return active_key
        raw_previous = str(
            tagentic_config.WORKBENCH_WORKSPACE_LOCATOR_PREVIOUS_KEYS_JSON or "{}"
        )
        try:
            previous = json.loads(raw_previous)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise WorkbenchWorkspaceError(
                "workspace locator previous keys are invalid",
                500,
            ) from error
        if not isinstance(previous, dict) or any(
            not isinstance(item_key, str) or not isinstance(item_value, str)
            for item_key, item_value in previous.items()
        ):
            raise WorkbenchWorkspaceError(
                "workspace locator previous keys are invalid",
                500,
            )
        encoded = previous.get(key_id)
        if encoded is None:
            raise WorkbenchWorkspaceError(
                "workspace locator decryption key is unavailable",
                500,
            )
        return cls._decode_locator_key(encoded.strip(), key_id)

    @classmethod
    def encrypt_provider_locator(cls, locator: str) -> str:
        """Encrypt a server-resolved provider locator; never call from a route."""

        if not isinstance(locator, str) or not locator.strip():
            raise WorkbenchWorkspaceError("provider workspace locator is invalid", 502)
        encoded = locator.strip().encode("utf-8")
        if len(encoded) > cls.MAX_PROVIDER_LOCATOR_BYTES:
            raise WorkbenchWorkspaceError("provider workspace locator is too large", 502)
        key_id, key = cls._active_locator_key()
        token = jwe.JWE(
            json.dumps(
                {"ProviderWorkspaceLocator": locator.strip()},
                separators=(",", ":"),
            ).encode("utf-8"),
            protected={"alg": "A256KW", "enc": "A256GCM", "kid": key_id},
        )
        token.add_recipient(key)
        return token.serialize(compact=True)

    @classmethod
    def _decrypt_provider_locator(cls, ciphertext: str) -> str:
        try:
            token = jwe.JWE()
            token.deserialize(ciphertext)
            key_id = str(token.jose_header.get("kid") or "").strip()
            token.decrypt(cls._locator_key_for_id(key_id))
            payload = json.loads(token.payload)
        except (InvalidJWEData, TypeError, ValueError, json.JSONDecodeError) as error:
            raise WorkbenchWorkspaceError(
                "stored provider workspace locator is invalid",
                500,
            ) from error
        if not isinstance(payload, dict) or set(payload) != {"ProviderWorkspaceLocator"}:
            raise WorkbenchWorkspaceError("stored provider workspace locator is invalid", 500)
        locator = payload["ProviderWorkspaceLocator"]
        if not isinstance(locator, str) or not locator.strip():
            raise WorkbenchWorkspaceError("stored provider workspace locator is invalid", 500)
        return locator.strip()

    @classmethod
    async def ensure_for_conversation(
        cls,
        db: AsyncSession,
        *,
        conversation_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> WorkbenchWorkspace:
        cls._assert_scope(identity, app_context)
        if not conversation_id:
            raise WorkbenchWorkspaceError("conversation id is required", 500)
        workspace = (
            await db.execute(
                select(WorkbenchWorkspace)
                .where(*cls._conversation_scope_filters(
                    conversation_id=conversation_id,
                    account_id=account_id,
                    identity=identity,
                    app_context=app_context,
                ))
                .with_for_update()
            )
        ).scalar()
        if workspace is not None:
            if workspace.ProviderAppId != app_context.app_id:
                raise WorkbenchWorkspaceError(
                    "workspace is outside the active provider App context",
                    409,
                )
            if workspace.Status not in {"pending", "active"}:
                raise WorkbenchWorkspaceError("workbench workspace is not active", 409)
            return workspace

        workspace = WorkbenchWorkspace(
            WorkspaceId=cls._workspace_id(
                conversation_id=conversation_id,
                account_id=account_id,
                identity=identity,
                app_context=app_context,
            ),
            ConversationId=conversation_id,
            BindingId=identity.binding_id,
            AccountId=account_id,
            CustomerId=identity.customer_id,
            ApplicationId=app_context.application_id,
            ProviderAppId=app_context.app_id,
            AppProfileId=str(app_context.app_profile_id),
            ConfigVersion=int(app_context.config_version),
            Kind=cls.CONVERSATION_KIND,
            Status="pending",
        )
        try:
            async with db.begin_nested():
                db.add(workspace)
                await db.flush()
        except IntegrityError:
            workspace = (
                await db.execute(
                    select(WorkbenchWorkspace).where(
                        *cls._conversation_scope_filters(
                            conversation_id=conversation_id,
                            account_id=account_id,
                            identity=identity,
                            app_context=app_context,
                        )
                    ).limit(1)
                )
            ).scalar()
            if workspace is None:
                raise WorkbenchWorkspaceError(
                    "conversation workspace could not be created idempotently",
                    409,
                )
            if (
                workspace.ProviderAppId != app_context.app_id
                or workspace.Status not in {"pending", "active"}
            ):
                raise WorkbenchWorkspaceError(
                    "workspace is outside the active provider App context",
                    409,
                )
        return workspace

    @classmethod
    async def bind_provider_locator_for_conversation(
        cls,
        db: AsyncSession,
        *,
        conversation_id: str,
        provider_locator: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> WorkbenchWorkspace:
        """Bind the provider Workspace returned by DescribeConversation once.

        The browser only receives the local ``ww_`` handle.  A provider response
        that later attempts to move an owned Conversation to another Workspace
        is treated as an integrity failure instead of silently replacing the
        locator used for file access.
        """

        cls._assert_scope(identity, app_context)
        workspace = (
            await db.execute(
                select(WorkbenchWorkspace)
                .where(
                    *cls._conversation_scope_filters(
                        conversation_id=conversation_id,
                        account_id=account_id,
                        identity=identity,
                        app_context=app_context,
                    ),
                    WorkbenchWorkspace.ProviderAppId == app_context.app_id,
                    WorkbenchWorkspace.Status == "active",
                )
                .with_for_update()
                .limit(1)
            )
        ).scalar()
        if workspace is None:
            raise WorkbenchWorkspaceError("workspace not found", 404)
        normalized = str(provider_locator or "").strip()
        if not normalized:
            raise WorkbenchWorkspaceError("provider workspace locator is invalid", 502)
        if workspace.ProviderLocatorCiphertext:
            existing = cls._decrypt_provider_locator(
                workspace.ProviderLocatorCiphertext
            )
            if not hmac.compare_digest(existing, normalized):
                raise WorkbenchWorkspaceError(
                    "provider returned a different Workspace for the owned Conversation",
                    502,
                )
            return workspace
        workspace.ProviderLocatorCiphertext = cls.encrypt_provider_locator(normalized)
        workspace.ProviderLocatorKeyId = str(
            tagentic_config.WORKBENCH_WORKSPACE_LOCATOR_KEY_ID or ""
        ).strip()
        await db.commit()
        return workspace

    @classmethod
    async def resolve_provider_locator(
        cls,
        db: AsyncSession,
        *,
        workspace_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> tuple[WorkbenchWorkspace, str]:
        """Resolve an owned local Workspace to its encrypted provider handle."""

        workspace = await cls.get_owned(
            db,
            workspace_id=workspace_id,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
        )
        if workspace is None:
            raise WorkbenchWorkspaceError("workspace not found", 404)
        if not workspace.ProviderLocatorCiphertext:
            raise WorkbenchWorkspaceError(
                "provider workspace is not ready; refresh the Conversation first",
                409,
            )
        return workspace, cls._decrypt_provider_locator(
            workspace.ProviderLocatorCiphertext
        )

    @classmethod
    async def get_owned(
        cls,
        db: AsyncSession,
        *,
        workspace_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        allow_pending: bool = False,
    ) -> WorkbenchWorkspace | None:
        cls._assert_scope(identity, app_context)
        statuses = ("active", "pending") if allow_pending else ("active",)
        return (
            await db.execute(
                select(WorkbenchWorkspace).where(
                    WorkbenchWorkspace.WorkspaceId == workspace_id,
                    *cls._scope_filters(
                        account_id=account_id,
                        identity=identity,
                        app_context=app_context,
                    ),
                    WorkbenchWorkspace.Status.in_(statuses),
                ).limit(1)
            )
        ).scalar()

    @classmethod
    async def stage_conversation(
        cls,
        db: AsyncSession,
        *,
        conversation_id: str,
        agent_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> tuple[WorkbenchWorkspace, WorkbenchConversationWorkspace]:
        workspace = await cls.ensure_for_conversation(
            db,
            conversation_id=conversation_id,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
        )
        existing = (
            await db.execute(
                select(WorkbenchConversationWorkspace)
                .where(WorkbenchConversationWorkspace.ConversationId == conversation_id)
                .with_for_update()
            )
        ).scalar()
        expected = {
            "WorkspaceId": workspace.WorkspaceId,
            "BindingId": identity.binding_id,
            "AccountId": str(account_id),
            "CustomerId": identity.customer_id,
            "ApplicationId": app_context.application_id,
            "ProviderAppId": app_context.app_id,
            "AppProfileId": str(app_context.app_profile_id),
            "ConfigVersion": int(app_context.config_version),
            "AgentId": agent_id,
        }
        if existing is not None:
            if any(str(getattr(existing, key)) != str(value) for key, value in expected.items()):
                raise WorkbenchWorkspaceError(
                    "conversation is already bound to a different workspace scope",
                    409,
                )
            return workspace, existing

        mapping = WorkbenchConversationWorkspace(
            ConversationId=conversation_id,
            **expected,
            Status="pending",
        )
        db.add(mapping)
        await db.flush()
        return workspace, mapping

    @classmethod
    async def activate_for_conversation(
        cls,
        db: AsyncSession,
        *,
        workspace: WorkbenchWorkspace,
        mapping: WorkbenchConversationWorkspace,
        identity: WorkbenchIdentityContext,
        report_event_id: str | None = None,
    ) -> None:
        if workspace.Status == "active":
            mapping.Status = "active"
            await db.commit()
            return
        parent_conversation_id = str(workspace.ConversationId or "")
        if not parent_conversation_id:
            raise WorkbenchWorkspaceError("workspace activation parent is missing", 500)
        if parent_conversation_id != str(mapping.ConversationId):
            raise WorkbenchWorkspaceError(
                "workspace activation parent does not match its conversation",
                409,
            )
        try:
            if report_event_id is None:
                report = await WorkbenchResourceReporter.enqueue(
                    db,
                    identity=identity,
                    resource_type="workspace",
                    resource_id=workspace.WorkspaceId,
                    parent_resource_type="conversation",
                    parent_resource_id=parent_conversation_id,
                )
                report_event_id = report.EventId
                await db.commit()
            status = await WorkbenchResourceReporter.deliver_event(
                db,
                report_event_id,
                fail_closed_on_rejection=True,
            )
        except WorkbenchResourceReportError as error:
            raise WorkbenchWorkspaceError(str(error), error.status_code) from error
        if status != "delivered":
            raise WorkbenchWorkspaceError(
                "workspace ownership confirmation is unavailable",
                503,
            )
        workspace.Status = "active"
        mapping.Status = "active"
        await db.commit()

    @classmethod
    async def stage_file(
        cls,
        db: AsyncSession,
        *,
        file_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> WorkbenchFileWorkspace:
        cls._assert_scope(identity, app_context)
        mapping = WorkbenchFileWorkspace(
            FileId=file_id,
            WorkspaceId=None,
            BindingId=identity.binding_id,
            AccountId=account_id,
            CustomerId=identity.customer_id,
            ApplicationId=app_context.application_id,
            ProviderAppId=app_context.app_id,
            AppProfileId=str(app_context.app_profile_id),
            ConfigVersion=int(app_context.config_version),
            Status="pending",
        )
        db.add(mapping)
        await db.flush()
        return mapping

    @classmethod
    async def get_owned_file_workspace(
        cls,
        db: AsyncSession,
        *,
        file_id: str,
        account_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        conversation_id: str | None,
    ) -> tuple[WorkbenchWorkspace | None, WorkbenchFileWorkspace] | None:
        cls._assert_scope(identity, app_context)
        file_mapping = (
            await db.execute(
                select(WorkbenchFileWorkspace).where(
                    WorkbenchFileWorkspace.FileId == file_id,
                    WorkbenchFileWorkspace.BindingId == identity.binding_id,
                    WorkbenchFileWorkspace.AccountId == account_id,
                    WorkbenchFileWorkspace.CustomerId == identity.customer_id,
                    WorkbenchFileWorkspace.ApplicationId == app_context.application_id,
                    WorkbenchFileWorkspace.ProviderAppId == app_context.app_id,
                    WorkbenchFileWorkspace.AppProfileId == str(app_context.app_profile_id),
                    WorkbenchFileWorkspace.ConfigVersion == int(app_context.config_version),
                    WorkbenchFileWorkspace.Status == "active",
                ).with_for_update().limit(1)
            )
        ).scalar()
        if file_mapping is None:
            return None
        if conversation_id:
            row = (
                await db.execute(
                    select(WorkbenchConversationWorkspace, WorkbenchWorkspace)
                    .join(
                        WorkbenchWorkspace,
                        WorkbenchWorkspace.WorkspaceId
                        == WorkbenchConversationWorkspace.WorkspaceId,
                    )
                    .where(
                        WorkbenchConversationWorkspace.ConversationId == conversation_id,
                        WorkbenchConversationWorkspace.BindingId == identity.binding_id,
                        WorkbenchConversationWorkspace.AccountId == account_id,
                        WorkbenchConversationWorkspace.CustomerId == identity.customer_id,
                        WorkbenchConversationWorkspace.ApplicationId == app_context.application_id,
                        WorkbenchConversationWorkspace.ProviderAppId == app_context.app_id,
                        WorkbenchConversationWorkspace.AppProfileId == str(app_context.app_profile_id),
                        WorkbenchConversationWorkspace.ConfigVersion == int(app_context.config_version),
                        WorkbenchConversationWorkspace.Status == "active",
                        WorkbenchWorkspace.ConversationId == conversation_id,
                        *cls._scope_filters(
                            account_id=account_id,
                            identity=identity,
                            app_context=app_context,
                        ),
                        WorkbenchWorkspace.Status == "active",
                    ).limit(1)
                )
            ).first()
            if row is None:
                return None
            conversation_mapping, workspace = row
            if file_mapping.WorkspaceId is None:
                file_mapping.WorkspaceId = workspace.WorkspaceId
                await db.flush()
            elif file_mapping.WorkspaceId != workspace.WorkspaceId:
                return None
            return workspace, file_mapping

        if file_mapping.WorkspaceId is None:
            return None, file_mapping
        workspace = await cls.get_owned(
            db,
            workspace_id=file_mapping.WorkspaceId,
            account_id=account_id,
            identity=identity,
            app_context=app_context,
            allow_pending=True,
        )
        if workspace is None:
            return None
        return workspace, file_mapping

    @staticmethod
    def public_descriptor(workspace: WorkbenchWorkspace) -> dict[str, Any]:
        return {
            "WorkspaceId": workspace.WorkspaceId,
            "ConversationId": str(workspace.ConversationId),
            "Kind": workspace.Kind,
            "Status": workspace.Status,
        }
