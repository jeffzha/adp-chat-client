from core.workbench_control import (
    WORKBENCH_LIMIT_MAXIMUMS,
    WorkbenchAppContext,
    WorkbenchIdentityContext,
)
from core.workbench_metrics import WORKBENCH_METRICS


class WorkbenchPolicyError(RuntimeError):
    def __init__(self, message: str, status_code: int = 403):
        super().__init__(message)
        self.status_code = status_code
        normalized = str(message).lower()
        if "capability" in normalized:
            limit = "capability"
        elif "read-only" in normalized:
            limit = "access_mode"
        elif "limits are" in normalized:
            limit = "configuration"
        elif "web search" in normalized:
            limit = "web_search"
        elif "unbounded" in normalized:
            limit = "unbounded"
        elif "file" in normalized:
            limit = "file_size"
        else:
            limit = "other"
        WORKBENCH_METRICS.inc("workbench_limit_denied_total", limit=limit)
        WORKBENCH_METRICS.inc(
            "workbench_gate_failures_total",
            gate="policy",
            reason="denied" if status_code < 500 else "error",
        )


class WorkbenchPolicy:
    """Central fail-closed interpretation of trusted workbench policy.

    The browser is never consulted for capabilities or limits.  The only source
    is ``request.ctx.workbench_app_context``, populated from claw-control after
    continuous authorization.
    """

    @staticmethod
    def capabilities(app_context: WorkbenchAppContext) -> frozenset[str]:
        return frozenset(
            str(value).strip().lower()
            for value in app_context.capabilities
            if str(value).strip()
        )

    @classmethod
    def require_capability(
        cls,
        app_context: WorkbenchAppContext,
        capability: str,
    ) -> None:
        if capability not in cls.capabilities(app_context):
            raise WorkbenchPolicyError(
                f"workbench capability is disabled: {capability}",
                403,
            )

    @staticmethod
    def require_active(identity: WorkbenchIdentityContext) -> None:
        if str(identity.access_mode).strip().lower() != "active":
            raise WorkbenchPolicyError("workbench is read-only", 403)

    @staticmethod
    def validated_limits(app_context: WorkbenchAppContext) -> dict[str, int]:
        limits = dict(app_context.limits or {})
        maximums = WORKBENCH_LIMIT_MAXIMUMS
        if set(limits) != set(maximums):
            raise WorkbenchPolicyError("workbench limits are incomplete", 503)
        for name, maximum in maximums.items():
            value = limits[name]
            if isinstance(value, bool) or not isinstance(value, int):
                raise WorkbenchPolicyError("workbench limits are invalid", 503)
            minimum = 0 if name in {"web_search_per_turn", "max_file_bytes"} else 1
            if value < minimum or value > maximum:
                raise WorkbenchPolicyError("workbench limits are invalid", 503)
        return limits

    @classmethod
    def validate_new_turn(
        cls,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        *,
        search_network: bool,
    ) -> dict[str, int]:
        cls.require_active(identity)
        if not app_context.execution_enabled:
            raise WorkbenchPolicyError(
                "workbench runtime profile execution is not enabled",
                503,
            )
        cls.require_capability(app_context, "chat")
        limits = cls.validated_limits(app_context)
        capabilities = cls.capabilities(app_context)

        if search_network:
            cls.require_capability(app_context, "web_search")
            if limits["web_search_per_turn"] < 1:
                raise WorkbenchPolicyError("web search limit is zero", 403)
            raise WorkbenchPolicyError(
                "web search is closed because provider search-call count cannot yet be enforced",
                503,
            )

        unbounded = capabilities.intersection({"web_search", "tools", "connectors"})
        if unbounded:
            raise WorkbenchPolicyError(
                "chat is closed while unbounded capabilities are enabled: "
                + ", ".join(sorted(unbounded)),
                503,
            )
        return limits

    @classmethod
    def validate_scheduled_turn(
        cls,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
        *,
        task_max_runtime_seconds: int,
    ) -> dict[str, int]:
        cls.require_active(identity)
        if not app_context.execution_enabled:
            raise WorkbenchPolicyError(
                "workbench runtime profile execution is not enabled",
                503,
            )
        cls.require_capability(app_context, "chat")
        cls.require_capability(app_context, "scheduled_tasks")
        limits = cls.validated_limits(app_context)
        capabilities = cls.capabilities(app_context)
        unsupported = capabilities.intersection({"web_search", "tools", "connectors"})
        if unsupported:
            raise WorkbenchPolicyError(
                "scheduled tasks are closed while unbounded capabilities are enabled: "
                + ", ".join(sorted(unsupported)),
                503,
            )
        if (
            isinstance(task_max_runtime_seconds, bool)
            or not isinstance(task_max_runtime_seconds, int)
            or task_max_runtime_seconds <= 0
        ):
            raise WorkbenchPolicyError("scheduled task runtime limit is invalid", 503)
        limits["max_runtime_seconds"] = min(
            limits["max_runtime_seconds"], task_max_runtime_seconds
        )
        return limits

    @classmethod
    def validate_file_write(
        cls,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> dict[str, int]:
        cls.require_active(identity)
        cls.require_capability(app_context, "files")
        limits = cls.validated_limits(app_context)
        if limits["max_file_bytes"] <= 0:
            raise WorkbenchPolicyError("file uploads are disabled", 403)
        return limits

    @classmethod
    def validate_file_read(cls, app_context: WorkbenchAppContext) -> dict[str, int]:
        cls.require_capability(app_context, "files")
        return cls.validated_limits(app_context)

    @classmethod
    def validate_action(
        cls,
        action: str,
        identity: WorkbenchIdentityContext,
        app_context: WorkbenchAppContext,
    ) -> None:
        if not app_context.execution_enabled:
            raise WorkbenchPolicyError(
                "workbench runtime profile execution is not enabled",
                503,
            )
        # Conversation/application reads and per-user Agent provisioning are the
        # server-side mechanics of chat. Generic Agent mutation is separately a
        # tool capability and remains closed; only the trusted, serialized limit
        # enforcement path may call ModifyAgent.
        catalog_capabilities = {
            "DescribeModelList": "catalog_models",
            "DescribeSkillSummaryList": "catalog_skills",
            "DescribeSkillDetail": "catalog_skills",
            "DescribePluginSummaryList": "catalog_plugins",
            "DescribePlugin": "catalog_plugins",
            "ListDir": "files",
        }
        required = catalog_capabilities.get(
            action,
            "tools" if action == "ModifyAgent" else "chat",
        )
        cls.require_capability(app_context, required)
        if action in {"CopyAgentFromApp", "ModifyAgent", "CreateConversation"}:
            cls.require_active(identity)
