from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.agent import AgentProvisioningError, CoreAgent
from core.conversation import CoreConversation
from core.workbench_catalog_policy import (
    PreparedCatalogAction,
    WorkbenchCatalogPolicy,
    WorkbenchCatalogPolicyError,
)
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_secure_file import WorkbenchSecureFilePipeline
from core.workbench_workspace import CoreWorkbenchWorkspace, WorkbenchWorkspaceError
from vendor.interface import BaseVendor


class WorkbenchActionPolicyError(RuntimeError):
    def __init__(self, message: str, status_code: int = 403):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PreparedWorkbenchAction:
    action: str
    application_id: str
    payload: dict[str, Any]
    provider_app_id: str
    agent_id: str | None = None
    ownership_principal_id: str | None = None
    conversation_id: str | None = None
    local_title: str | None = None
    local_response: dict[str, Any] | None = None
    identity: WorkbenchIdentityContext | None = None
    app_context: WorkbenchAppContext | None = None
    catalog: PreparedCatalogAction | None = None


class WorkbenchActionPolicy:
    """Fail-closed policy for the generic /adp/<Action> workbench endpoint."""

    _ALLOWED_ACTIONS = frozenset(
        {
            "DescribeApp",
            "CopyAgentFromApp",
            "DescribeAgentDetail",
            "ModifyAgent",
            "CreateConversation",
            "DescribeConversation",
            "DescribeConversationList",
            "DescribeConversationMessageList",
            "ListDir",
            "DescribeModelList",
            "DescribeSkillSummaryList",
            "DescribeSkillDetail",
            "DescribePluginSummaryList",
            "DescribePlugin",
        }
    )
    _MUTATING_ACTIONS = frozenset(
        {
            "CopyAgentFromApp",
            "ModifyAgent",
            "CreateConversation",
        }
    )
    _SUPPORTED_SERVICE_VENDORS = frozenset(
        {
            "ChinaTencentCloud",
            "ChinaTencentADP",
        }
    )
    _SENSITIVE_RESPONSE_KEYS = frozenset(
        {
            "appkey",
            "appid",
            "spaceid",
            "secretinfo",
            "secretid",
            "secretkey",
            "accesskey",
            "accesstoken",
            "refreshtoken",
            "authorization",
            "password",
            "passwordsalt",
            "credential",
            "credentials",
            "headerparameterlist",
            "queryparameterlist",
        }
    )

    @classmethod
    async def prepare(
        cls,
        db: AsyncSession,
        *,
        action: str,
        request_body: Any,
        account_id: str,
        identity: WorkbenchIdentityContext,
        vendor_app: BaseVendor,
        app_context: WorkbenchAppContext | None = None,
    ) -> PreparedWorkbenchAction:
        if action not in cls._ALLOWED_ACTIONS:
            raise WorkbenchActionPolicyError("Action is not available in workbench mode")

        envelope = cls._require_dict(request_body, "request body")
        cls._reject_unknown_fields(envelope, {"ApplicationId", "Payload"}, "request body")
        application_id = cls._require_string(
            envelope.get("ApplicationId"),
            "ApplicationId",
            max_length=64,
        )
        if application_id != identity.application_id:
            raise WorkbenchActionPolicyError("ApplicationId is outside the active workbench context")

        payload = cls._require_dict(envelope.get("Payload", {}), "Payload")
        access_mode = str(identity.access_mode).strip().lower()
        if access_mode not in {"active", "read_only", "readonly"}:
            raise WorkbenchActionPolicyError("workbench access mode is not valid")
        if action in cls._MUTATING_ACTIONS and access_mode != "active":
            raise WorkbenchActionPolicyError("workbench is read-only")

        vendor_application_id = str(getattr(vendor_app, "application_id", "") or "").strip()
        vendor_name = str(vendor_app.config.get("Vendor") or "").strip()
        if not vendor_name and hasattr(vendor_app, "get_vendor"):
            vendor_name = str(vendor_app.get_vendor() or "").strip()
        service_vendor = str(
            vendor_app.config.get("ServiceVendor") or "ChinaTencentCloud"
        ).strip()
        if vendor_application_id != application_id or vendor_name != "Tencent":
            raise WorkbenchActionPolicyError("trusted Tencent application context is invalid", 503)
        if service_vendor not in cls._SUPPORTED_SERVICE_VENDORS:
            raise WorkbenchActionPolicyError(
                "the configured provider environment has no workbench Action policy",
                503,
            )

        provider_app_id = cls._require_string(
            vendor_app.config.get("AppId"),
            "trusted AppId",
            max_length=128,
            status_code=503,
        )

        if app_context is not None:
            if not app_context.execution_enabled:
                raise WorkbenchActionPolicyError(
                    "workbench runtime profile execution is not enabled",
                    503,
                )
            if (
                action in {"CopyAgentFromApp", "DescribeAgentDetail", "ModifyAgent"}
                and not app_context.runtime.uses_provider_user_agent
            ):
                raise WorkbenchActionPolicyError(
                    "provider user Agent operations are unavailable for this runtime profile",
                    403,
                )

        if action in WorkbenchCatalogPolicy.ACTION_CAPABILITIES:
            if app_context is None:
                raise WorkbenchActionPolicyError(
                    "trusted workbench catalog context is unavailable", 503
                )
            try:
                upstream_payload, catalog = WorkbenchCatalogPolicy.prepare(
                    action=action,
                    payload=payload,
                    account_id=account_id,
                    identity=identity,
                    app_context=app_context,
                    vendor_app=vendor_app,
                )
            except WorkbenchCatalogPolicyError as error:
                raise WorkbenchActionPolicyError(
                    str(error), error.status_code
                ) from error
            return PreparedWorkbenchAction(
                action=action,
                application_id=application_id,
                provider_app_id=provider_app_id,
                payload=upstream_payload,
                catalog=catalog,
            )

        if action == "DescribeApp":
            cls._reject_unknown_fields(payload, set(), "DescribeApp Payload")
            return PreparedWorkbenchAction(
                action=action,
                application_id=application_id,
                provider_app_id=provider_app_id,
                payload={
                    "AppId": provider_app_id,
                    "FieldMask": {"Paths": ["AppConfig"]},
                },
            )

        if action == "ModifyAgent":
            # Limit changes are an internal, serialized server operation. Keeping
            # the generic endpoint closed prevents a browser from racing a Turn or
            # replacing trusted Model/AdvancedConfig values.
            raise WorkbenchActionPolicyError(
                "ModifyAgent is restricted to trusted server limit enforcement",
                409,
            )

        if action == "CopyAgentFromApp":
            cls._reject_unknown_fields(
                payload,
                {"AppId", "Kind"},
                "CopyAgentFromApp Payload",
            )
            if "AppId" in payload:
                supplied_app_id = cls._require_string(
                    payload["AppId"],
                    "CopyAgentFromApp AppId",
                    max_length=64,
                )
                if supplied_app_id != application_id:
                    raise WorkbenchActionPolicyError(
                        "CopyAgentFromApp AppId is outside the trusted context",
                        403,
                    )
            if "Kind" in payload:
                supplied_kind = cls._bounded_int(
                    payload["Kind"],
                    "CopyAgentFromApp Kind",
                    1,
                    1,
                )
                if supplied_kind != 1:
                    raise WorkbenchActionPolicyError(
                        "CopyAgentFromApp Kind is not supported",
                        400,
                    )
            ownership_principal_id, agent_id = await cls._resolve_runtime_principal(
                db,
                account_id=account_id,
                application_id=application_id,
                vendor_app=vendor_app,
                allow_provision=True,
                identity=identity,
                app_context=app_context,
            )
            return PreparedWorkbenchAction(
                action=action,
                application_id=application_id,
                provider_app_id=provider_app_id,
                agent_id=agent_id,
                ownership_principal_id=ownership_principal_id,
                payload={},
                local_response={"ParentAgentId": agent_id},
            )

        if action == "DescribeAgentDetail":
            cls._reject_unknown_fields(
                payload,
                {"AppId", "AgentId", "Domain"},
                "DescribeAgentDetail Payload",
            )
            if "AppId" in payload:
                supplied_app_id = cls._require_string(
                    payload["AppId"],
                    "DescribeAgentDetail AppId",
                    max_length=64,
                )
                if supplied_app_id != application_id:
                    raise WorkbenchActionPolicyError(
                        "DescribeAgentDetail AppId is outside the trusted context",
                        403,
                    )
            if "Domain" in payload:
                supplied_domain = cls._bounded_int(
                    payload["Domain"],
                    "DescribeAgentDetail Domain",
                    2,
                    2,
                )
                if supplied_domain != 2:
                    raise WorkbenchActionPolicyError(
                        "DescribeAgentDetail Domain is not supported",
                        400,
                    )
            ownership_principal_id, agent_id = await cls._resolve_runtime_principal(
                db,
                account_id=account_id,
                application_id=application_id,
                vendor_app=vendor_app,
                allow_provision=access_mode == "active",
                identity=identity,
                app_context=app_context,
            )
            if "AgentId" in payload:
                supplied_agent_id = cls._require_string(
                    payload["AgentId"],
                    "DescribeAgentDetail AgentId",
                    max_length=128,
                )
                if supplied_agent_id != agent_id:
                    raise WorkbenchActionPolicyError(
                        "DescribeAgentDetail AgentId is outside the trusted context",
                        403,
                    )
            return PreparedWorkbenchAction(
                action=action,
                application_id=application_id,
                provider_app_id=provider_app_id,
                agent_id=agent_id,
                ownership_principal_id=ownership_principal_id,
                payload={"AppId": provider_app_id, "AgentId": agent_id},
            )

        if action == "ListDir":
            cls._require_workspace_context(identity, app_context, provider_app_id)
            cls._reject_unknown_fields(
                payload,
                {"app_id", "path", "depth", "workspace_id"},
                "ListDir Payload",
            )
            supplied_application_id = cls._require_string(
                payload.get("app_id"),
                "ListDir app_id",
                max_length=64,
            )
            if supplied_application_id != application_id:
                raise WorkbenchActionPolicyError(
                    "ListDir app_id is outside the active workbench context",
                    403,
                )
            local_workspace_id = cls._require_string(
                payload.get("workspace_id"),
                "ListDir workspace_id",
                max_length=64,
            )
            if not local_workspace_id.startswith("ww_"):
                raise WorkbenchActionPolicyError("ListDir workspace_id is invalid", 400)
            try:
                path = CoreWorkbenchWorkspace.require_path(
                    payload.get("path"), "ListDir path"
                )
            except WorkbenchWorkspaceError as error:
                raise WorkbenchActionPolicyError(
                    str(error), error.status_code
                ) from error
            depth = cls._bounded_int(payload.get("depth", 1), "ListDir depth", 1, 1)
            try:
                _, provider_workspace_id = (
                    await CoreWorkbenchWorkspace.resolve_provider_locator(
                        db,
                        workspace_id=local_workspace_id,
                        account_id=account_id,
                        identity=identity,
                        app_context=app_context,
                    )
                )
                response = await vendor_app.list_dir(
                    app_id=provider_app_id,
                    path=path,
                    depth=depth,
                    workspace_id=provider_workspace_id,
                    user_id=identity.canonical_subject,
                )
            except WorkbenchWorkspaceError as error:
                raise WorkbenchActionPolicyError(
                    str(error), error.status_code
                ) from error
            except Exception as error:
                raise WorkbenchActionPolicyError(
                    "provider workspace directory request failed",
                    502,
                ) from error
            return PreparedWorkbenchAction(
                action=action,
                application_id=application_id,
                provider_app_id=provider_app_id,
                identity=identity,
                app_context=app_context,
                payload={},
                local_response=cls._project_directory(response, requested_path=path),
            )

        app_key = cls._require_string(
            vendor_app.config.get("AppKey"),
            "trusted AppKey",
            max_length=4096,
            status_code=503,
        )
        user_id = cls._require_string(
            identity.canonical_subject,
            "trusted UserId",
            max_length=255,
            status_code=503,
        )

        if action == "CreateConversation":
            cls._reject_unknown_fields(
                payload,
                {"Title", "Type", "AppId", "AgentId"},
                "CreateConversation Payload",
            )
            title = payload.get("Title", "new conversation")
            title = cls._require_string(title, "Title", max_length=255)
            ownership_principal_id, agent_id = await cls._resolve_runtime_principal(
                db,
                account_id=account_id,
                application_id=application_id,
                vendor_app=vendor_app,
                allow_provision=True,
                identity=identity,
                app_context=app_context,
            )
            if "Type" in payload:
                cls._bounded_int(
                    payload["Type"],
                    "CreateConversation Type",
                    5,
                    5,
                )
            if "AppId" in payload:
                supplied_app_id = cls._require_string(
                    payload["AppId"],
                    "CreateConversation AppId",
                    max_length=64,
                )
                if supplied_app_id != application_id:
                    raise WorkbenchActionPolicyError(
                        "CreateConversation AppId is outside the trusted context",
                        403,
                    )
            if "AgentId" in payload:
                if agent_id is None:
                    raise WorkbenchActionPolicyError(
                        "CreateConversation AgentId is unavailable for this runtime profile",
                        403,
                    )
                supplied_agent_id = cls._require_string(
                    payload["AgentId"],
                    "CreateConversation AgentId",
                    max_length=128,
                )
                if supplied_agent_id != agent_id:
                    raise WorkbenchActionPolicyError(
                        "CreateConversation AgentId is outside the trusted context",
                        403,
                    )
            cls._require_workspace_context(identity, app_context, provider_app_id)
            return PreparedWorkbenchAction(
                action=action,
                application_id=application_id,
                provider_app_id=provider_app_id,
                agent_id=agent_id,
                ownership_principal_id=ownership_principal_id,
                local_title=title,
                identity=identity,
                app_context=app_context,
                payload={
                    "Type": 5,
                    "AppId": provider_app_id,
                    "AppKey": app_key,
                    "UserId": user_id,
                    **({"AgentId": agent_id} if agent_id is not None else {}),
                },
            )

        if action == "DescribeConversationList":
            cls._reject_unknown_fields(
                payload,
                {"Keyword", "Limit", "Offset"},
                "DescribeConversationList Payload",
            )
            limit = cls._bounded_int(payload.get("Limit", 20), "Limit", 1, 50)
            offset = cls._bounded_int(payload.get("Offset", 0), "Offset", 0, 10000)
            keyword = None
            if "Keyword" in payload:
                keyword = cls._require_string(
                    payload["Keyword"],
                    "Keyword",
                    max_length=200,
                    allow_empty=True,
                )
            ownership_principal_id, agent_id = await cls._resolve_runtime_principal(
                db,
                account_id=account_id,
                application_id=application_id,
                vendor_app=vendor_app,
                allow_provision=access_mode == "active",
                identity=identity,
                app_context=app_context,
            )
            upstream_payload: dict[str, Any] = {
                "Type": 5,
                "AppId": provider_app_id,
                "AppKey": app_key,
                "UserId": user_id,
                "Limit": limit,
                "Offset": offset,
            }
            if agent_id is not None:
                upstream_payload["AgentId"] = agent_id
            if keyword is not None:
                upstream_payload["Keyword"] = keyword
            cls._require_workspace_context(identity, app_context, provider_app_id)
            return PreparedWorkbenchAction(
                action=action,
                application_id=application_id,
                provider_app_id=provider_app_id,
                agent_id=agent_id,
                ownership_principal_id=ownership_principal_id,
                identity=identity,
                app_context=app_context,
                payload=upstream_payload,
            )

        if action in {"DescribeConversation", "DescribeConversationMessageList"}:
            allowed_fields = {"ConversationId", "Type"}
            if action == "DescribeConversationMessageList":
                allowed_fields.update({"Limit", "RecordId", "RecordQueryDirection"})
            cls._reject_unknown_fields(payload, allowed_fields, f"{action} Payload")
            if "Type" in payload:
                cls._bounded_int(payload["Type"], f"{action} Type", 5, 5)
            conversation_id = cls._uuid_string(payload.get("ConversationId"), "ConversationId")
            await cls._assert_conversation_owner(
                db,
                account_id=account_id,
                application_id=application_id,
                conversation_id=conversation_id,
                identity=identity,
                app_context=app_context,
                provider_app_id=provider_app_id,
            )
            ownership_principal_id, agent_id = await cls._resolve_runtime_principal(
                db,
                account_id=account_id,
                application_id=application_id,
                vendor_app=vendor_app,
                allow_provision=access_mode == "active",
                identity=identity,
                app_context=app_context,
            )
            upstream_payload = {
                "ConversationId": conversation_id,
                "Type": 5,
                "AppKey": app_key,
                "UserId": user_id,
            }
            if action == "DescribeConversationMessageList":
                upstream_payload["Limit"] = cls._bounded_int(
                    payload.get("Limit", 10),
                    "Limit",
                    1,
                    50,
                )
                upstream_payload["RecordQueryDirection"] = cls._bounded_int(
                    payload.get("RecordQueryDirection", 1),
                    "RecordQueryDirection",
                    0,
                    3,
                )
                if "RecordId" in payload:
                    upstream_payload["RecordId"] = cls._require_string(
                        payload["RecordId"],
                        "RecordId",
                        max_length=255,
                        allow_empty=True,
                    )
            return PreparedWorkbenchAction(
                action=action,
                application_id=application_id,
                provider_app_id=provider_app_id,
                agent_id=agent_id,
                ownership_principal_id=ownership_principal_id,
                conversation_id=conversation_id,
                identity=identity,
                app_context=app_context,
                payload=upstream_payload,
            )

        raise WorkbenchActionPolicyError("Action is not implemented in workbench mode")

    @classmethod
    async def project_response(
        cls,
        db: AsyncSession,
        *,
        prepared: PreparedWorkbenchAction,
        account_id: str,
        response: Any,
    ) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise WorkbenchActionPolicyError("provider returned an invalid Action response", 502)
        if "Error" in response:
            provider_error = response.get("Error")
            error_code = "ProviderError"
            if prepared.catalog is None and isinstance(provider_error, dict):
                error_code = provider_error.get("Code")
            projected_error = {
                "Error": {
                    "Code": str(error_code or "ProviderError")[:128],
                    "Message": "provider request failed",
                }
            }
            if response.get("RequestId") is not None:
                projected_error["RequestId"] = str(response["RequestId"])[:255]
            return projected_error

        if prepared.catalog is not None:
            try:
                return WorkbenchCatalogPolicy.project_response(
                    prepared.catalog,
                    response,
                )
            except WorkbenchCatalogPolicyError as error:
                raise WorkbenchActionPolicyError(
                    str(error), error.status_code
                ) from error

        if prepared.action == "CreateConversation":
            conversation_id = cls._uuid_string(
                response.get("ConversationId"),
                "provider ConversationId",
                status_code=502,
            )
            existing = await CoreConversation.get(db, conversation_id)
            if existing is not None:
                raise WorkbenchActionPolicyError("provider ConversationId already exists", 409)
            if prepared.identity is None or prepared.app_context is None:
                raise WorkbenchActionPolicyError(
                    "trusted Conversation ownership context is unavailable",
                    503,
                )
            await CoreConversation.create(
                db,
                account_id,
                prepared.application_id,
                title=prepared.local_title or "new conversation",
                conversation_id=conversation_id,
                workbench_identity=prepared.identity,
                workbench_app_context=prepared.app_context,
                workbench_agent_id=prepared.ownership_principal_id,
            )

        public_workspace = None
        if prepared.action == "DescribeConversation":
            cls._validate_conversation_item(response, prepared, exact_id=True)
            provider_workspace = response.get("Workspace")
            if provider_workspace is not None:
                if not isinstance(provider_workspace, dict):
                    raise WorkbenchActionPolicyError(
                        "provider Conversation Workspace is invalid",
                        502,
                    )
                # Tencent documents WorkspaceId as an opaque String. Current
                # examples are UUIDs, but authorization must not depend on an
                # undocumented representation; the value remains server-only,
                # bounded and encrypted before persistence.
                provider_workspace_id = cls._require_string(
                    provider_workspace.get("WorkspaceId"),
                    "provider WorkspaceId",
                    max_length=256,
                    status_code=502,
                )
                if prepared.identity is None or prepared.app_context is None:
                    raise WorkbenchActionPolicyError(
                        "trusted Workspace ownership context is unavailable",
                        503,
                    )
                try:
                    local_workspace = (
                        await CoreWorkbenchWorkspace.bind_provider_locator_for_conversation(
                            db,
                            conversation_id=prepared.conversation_id,
                            provider_locator=provider_workspace_id,
                            account_id=account_id,
                            identity=prepared.identity,
                            app_context=prepared.app_context,
                        )
                    )
                except WorkbenchWorkspaceError as error:
                    raise WorkbenchActionPolicyError(
                        str(error), error.status_code
                    ) from error
                public_workspace = {"WorkspaceId": local_workspace.WorkspaceId}
                storage_type = provider_workspace.get("StorageType")
                if storage_type is not None:
                    public_workspace["StorageType"] = cls._require_string(
                        storage_type,
                        "provider Workspace StorageType",
                        max_length=64,
                        status_code=502,
                    )
                response = dict(response)
                response.pop("Workspace", None)

        if prepared.action == "DescribeAgentDetail":
            agent = response.get("Agent")
            if not isinstance(agent, dict):
                raise WorkbenchActionPolicyError("provider Agent response is invalid", 502)
            provider_agent_id = str(agent.get("AgentId") or "").strip()
            if not provider_agent_id or provider_agent_id != prepared.agent_id:
                raise WorkbenchActionPolicyError(
                    "provider returned an Agent outside the trusted context",
                    502,
                )

        if prepared.action == "DescribeConversationList":
            for key in ("ConversationList", "Conversations"):
                items = response.get(key)
                if items is None:
                    continue
                if not isinstance(items, list):
                    raise WorkbenchActionPolicyError("provider conversation list is invalid", 502)
                for item in items:
                    if not isinstance(item, dict):
                        raise WorkbenchActionPolicyError(
                            "provider conversation item is invalid",
                            502,
                        )
                    cls._require_provider_conversation_identifiers(item, prepared)
                filtered_items = []
                for item in items:
                    if not cls._conversation_item_is_owned(item, prepared):
                        continue
                    if prepared.identity is None or prepared.app_context is None:
                        raise WorkbenchActionPolicyError(
                            "trusted Conversation ownership context is unavailable",
                            503,
                        )
                    conversation_id = cls._uuid_string(
                        item.get("ConversationId"),
                        "provider ConversationId",
                        status_code=502,
                    )
                    local = await CoreConversation.get_owned(
                        db,
                        account_id,
                        prepared.application_id,
                        conversation_id,
                        workbench_identity=prepared.identity,
                        workbench_app_context=prepared.app_context,
                    )
                    if local is not None:
                        filtered_items.append(item)
                response[key] = filtered_items
                if len(filtered_items) != len(items) and "TotalCount" in response:
                    response["TotalCount"] = (
                        str(len(filtered_items))
                        if isinstance(response["TotalCount"], str)
                        else len(filtered_items)
                    )

        if prepared.action == "DescribeConversationMessageList":
            for key in ("MessageList", "Messages"):
                items = response.get(key)
                if items is None:
                    continue
                if not isinstance(items, list):
                    raise WorkbenchActionPolicyError("provider message list is invalid", 502)
                for item in items:
                    if not isinstance(item, dict):
                        raise WorkbenchActionPolicyError("provider message item is invalid", 502)
                    item_conversation_id = cls._uuid_string(
                        item.get("ConversationId"),
                        "provider message ConversationId",
                        status_code=502,
                    )
                    if item_conversation_id != prepared.conversation_id:
                        raise WorkbenchActionPolicyError(
                            "provider returned a message for another conversation",
                            502,
                        )

        # Generic Action forwarding can return message bodies too.  Apply the
        # same private-file projection used by the dedicated chat/history
        # routes so a provider echo cannot expose a presigned COS locator.
        projected = WorkbenchSecureFilePipeline.redact_private_urls(
            cls._redact(response),
            (),
        )
        if public_workspace is not None:
            projected["Workspace"] = public_workspace
        return projected

    @classmethod
    async def _resolve_runtime_principal(
        cls,
        db: AsyncSession,
        *,
        account_id: str,
        application_id: str,
        vendor_app: BaseVendor,
        allow_provision: bool,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext | None,
    ) -> tuple[str, str | None]:
        uses_provider_user_agent = (
            app_context is None or app_context.runtime.uses_provider_user_agent
        )
        try:
            if uses_provider_user_agent and allow_provision:
                record = await CoreAgent.ensure(
                    db,
                    account_id,
                    application_id,
                    vendor_app,
                    identity_context=identity,
                )
            elif uses_provider_user_agent:
                record = await CoreAgent.get(db, account_id, application_id)
            elif allow_provision:
                if app_context is None:
                    raise WorkbenchActionPolicyError(
                        "trusted runtime profile is unavailable",
                        503,
                    )
                limits = dict(app_context.limits or {})
                principal = await CoreAgent.ensure_runtime_principal(
                    db,
                    account_id,
                    app_context,
                    vendor_app,
                    max_output_tokens=int(limits.get("max_output_tokens") or 1),
                    max_reasoning_rounds=int(
                        limits.get("max_reasoning_rounds") or 1
                    ),
                    identity_context=identity,
                )
                if principal.provider_agent_id is not None:
                    raise WorkbenchActionPolicyError(
                        "non-dynamic runtime resolved a provider Agent",
                        503,
                    )
                return (
                    cls._require_string(
                        principal.ownership_id,
                        "trusted runtime ownership principal",
                        max_length=128,
                        status_code=409,
                    ),
                    None,
                )
            else:
                record = await CoreAgent.get(db, account_id, application_id)
        except AgentProvisioningError as error:
            raise WorkbenchActionPolicyError(str(error), error.status_code) from error
        except ValueError as error:
            raise WorkbenchActionPolicyError(str(error), 503) from error
        if record is None:
            raise WorkbenchActionPolicyError(
                "workbench runtime principal is not provisioned",
                409,
            )
        principal_id = cls._require_string(
            record.AgentId,
            "trusted runtime principal",
            max_length=128,
            status_code=409,
        )
        if uses_provider_user_agent:
            if principal_id.startswith(CoreAgent._LOCAL_RUNTIME_PREFIX):
                raise WorkbenchActionPolicyError(
                    "runtime profile changed and requires Agent reconciliation",
                    409,
                )
            return principal_id, principal_id
        if not principal_id.startswith(CoreAgent._LOCAL_RUNTIME_PREFIX):
            raise WorkbenchActionPolicyError(
                "runtime profile changed and requires Agent reconciliation",
                409,
            )
        return principal_id, None

    @staticmethod
    async def _assert_conversation_owner(
        db: AsyncSession,
        *,
        account_id: str,
        application_id: str,
        conversation_id: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext | None,
        provider_app_id: str,
    ) -> None:
        WorkbenchActionPolicy._require_workspace_context(
            identity,
            app_context,
            provider_app_id,
        )
        conversation = await CoreConversation.get_owned(
            db,
            account_id,
            application_id,
            conversation_id,
            workbench_identity=identity,
            workbench_app_context=app_context,
        )
        if conversation is None:
            raise WorkbenchActionPolicyError("conversation not found", 404)

    @staticmethod
    def _require_workspace_context(
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext | None,
        provider_app_id: str,
    ) -> None:
        if (
            app_context is None
            or identity.application_id != app_context.application_id
            or str(identity.app_profile_id) != str(app_context.app_profile_id)
            or int(identity.config_version) <= 0
            or int(identity.config_version) != int(app_context.config_version)
            or not provider_app_id
            or app_context.app_id != provider_app_id
        ):
            raise WorkbenchActionPolicyError(
                "trusted Conversation ownership context is unavailable",
                503,
            )

    @classmethod
    def _validate_conversation_item(
        cls,
        item: dict[str, Any],
        prepared: PreparedWorkbenchAction,
        *,
        exact_id: bool,
    ) -> None:
        cls._require_provider_conversation_identifiers(item, prepared)
        if not cls._conversation_item_is_owned(item, prepared):
            raise WorkbenchActionPolicyError(
                "provider returned a conversation outside the trusted context",
                502,
            )
        if exact_id and str(item.get("ConversationId") or "") != prepared.conversation_id:
            raise WorkbenchActionPolicyError("provider returned a different conversation", 502)

    @staticmethod
    def _conversation_item_is_owned(
        item: dict[str, Any],
        prepared: PreparedWorkbenchAction,
    ) -> bool:
        item_type = item.get("Type")
        if isinstance(item_type, bool):
            return False
        try:
            if int(item_type) != 5:
                return False
        except (TypeError, ValueError):
            return False
        if str(item.get("AppId") or "") != prepared.provider_app_id:
            return False
        item_agent_id = str(item.get("AgentId") or "")
        if prepared.agent_id is None:
            if item_agent_id:
                return False
        elif item_agent_id != prepared.agent_id:
            return False
        return True

    @classmethod
    def _require_provider_conversation_identifiers(
        cls,
        item: dict[str, Any],
        prepared: PreparedWorkbenchAction,
    ) -> None:
        cls._uuid_string(
            item.get("ConversationId"),
            "provider ConversationId",
            status_code=502,
        )
        if prepared.agent_id is not None:
            cls._require_string(
                item.get("AgentId"),
                "provider AgentId",
                max_length=128,
                status_code=502,
            )
        elif item.get("AgentId") not in {None, ""}:
            raise WorkbenchActionPolicyError(
                "provider returned an unexpected AgentId for this runtime profile",
                502,
            )

    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: cls._redact(item)
                for key, item in value.items()
                if str(key).replace("_", "").lower() not in cls._SENSITIVE_RESPONSE_KEYS
            }
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        return value

    @staticmethod
    def _require_dict(value: Any, field_name: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise WorkbenchActionPolicyError(f"{field_name} must be a JSON object", 400)
        return dict(value)

    @classmethod
    def _project_directory(
        cls,
        response: Any,
        *,
        requested_path: str,
    ) -> dict[str, Any]:
        if not isinstance(response, dict) or set(response).difference({"entries"}):
            raise WorkbenchActionPolicyError(
                "provider workspace directory response is invalid",
                502,
            )
        entries = response.get("entries")
        if not isinstance(entries, list) or len(entries) > 1000:
            raise WorkbenchActionPolicyError(
                "provider workspace directory response is invalid",
                502,
            )
        prefix = requested_path.rstrip("/") + "/"
        projected = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise WorkbenchActionPolicyError(
                    "provider workspace directory entry is invalid",
                    502,
                )
            name = cls._require_string(
                entry.get("name"),
                "provider directory entry name",
                max_length=255,
                status_code=502,
            )
            if (
                name in {".", ".."}
                or "/" in name
                or "\\" in name
                or any(ord(character) < 32 or ord(character) == 127 for character in name)
            ):
                raise WorkbenchActionPolicyError(
                    "provider workspace directory entry name is invalid",
                    502,
                )
            entry_type = entry.get("type")
            if entry_type not in {"FILE_TYPE_DIRECTORY", "FILE_TYPE_FILE"}:
                raise WorkbenchActionPolicyError(
                    "provider workspace directory entry type is invalid",
                    502,
                )
            try:
                entry_path = CoreWorkbenchWorkspace.require_path(
                    entry.get("path"),
                    "provider directory entry path",
                )
            except WorkbenchWorkspaceError as error:
                raise WorkbenchActionPolicyError(
                    str(error), 502
                ) from error
            if not entry_path.startswith(prefix) or "/" in entry_path[len(prefix):]:
                raise WorkbenchActionPolicyError(
                    "provider workspace directory entry escaped the requested directory",
                    502,
                )
            item = {"name": name, "type": entry_type, "path": entry_path}
            if entry_type == "FILE_TYPE_FILE" and entry.get("size") is not None:
                try:
                    size = int(entry["size"])
                except (TypeError, ValueError) as error:
                    raise WorkbenchActionPolicyError(
                        "provider workspace file size is invalid",
                        502,
                    ) from error
                if size < 0 or size > 1099511627776:
                    raise WorkbenchActionPolicyError(
                        "provider workspace file size is invalid",
                        502,
                    )
                item["size"] = str(size)
            projected.append(item)
        return {"entries": projected}

    @staticmethod
    def _reject_unknown_fields(
        value: dict[str, Any],
        allowed: set[str],
        field_name: str,
    ) -> None:
        unknown = sorted(set(value).difference(allowed))
        if unknown:
            raise WorkbenchActionPolicyError(
                f"{field_name} contains unsupported fields: {', '.join(unknown)}",
                400,
            )

    @staticmethod
    def _require_string(
        value: Any,
        field_name: str,
        *,
        max_length: int,
        allow_empty: bool = False,
        status_code: int = 400,
    ) -> str:
        if not isinstance(value, str):
            raise WorkbenchActionPolicyError(f"{field_name} must be a string", status_code)
        normalized = value.strip()
        if (not normalized and not allow_empty) or len(normalized) > max_length:
            raise WorkbenchActionPolicyError(f"{field_name} is invalid", status_code)
        if any(ord(character) < 32 for character in normalized):
            raise WorkbenchActionPolicyError(f"{field_name} contains control characters", status_code)
        return normalized

    @staticmethod
    def _bounded_int(value: Any, field_name: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise WorkbenchActionPolicyError(f"{field_name} must be an integer", 400)
        if value < minimum or value > maximum:
            raise WorkbenchActionPolicyError(
                f"{field_name} must be between {minimum} and {maximum}",
                400,
            )
        return value

    @staticmethod
    def _uuid_string(
        value: Any,
        field_name: str,
        *,
        status_code: int = 400,
    ) -> str:
        if not isinstance(value, str):
            raise WorkbenchActionPolicyError(f"{field_name} must be a UUID string", status_code)
        try:
            return str(UUID(value))
        except (ValueError, AttributeError) as error:
            raise WorkbenchActionPolicyError(f"{field_name} must be a UUID string", status_code) from error
