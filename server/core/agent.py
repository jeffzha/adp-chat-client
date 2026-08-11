import copy
import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from model.agent import AgentConfig
from model.workbench import WorkbenchAgentBinding, WorkbenchIdentity
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_metrics import WORKBENCH_METRICS
from core.workbench_resource_reporter import (
    WorkbenchResourceReporter,
    WorkbenchResourceReportError,
)
from vendor.interface import BaseVendor


class AgentProvisioningError(RuntimeError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class WorkbenchRuntimePrincipal:
    """Local ownership node plus the optional Tencent Kind=1 Agent."""

    ownership_id: str
    provider_agent_id: str | None


class CoreAgent:
    _LOCAL_RUNTIME_PREFIX = "wrp_"

    @staticmethod
    async def _lock_workbench_binding(
        db: AsyncSession,
        account_id: str,
        application_id: str,
    ) -> WorkbenchAgentBinding | None:
        return (
            await db.execute(
                select(WorkbenchAgentBinding)
                .where(
                    WorkbenchAgentBinding.AccountId == account_id,
                    WorkbenchAgentBinding.ApplicationId == application_id,
                )
                .with_for_update()
            )
        ).scalar()

    @staticmethod
    async def get(
        db: AsyncSession, account_id: str, application_id: str
    ) -> Optional[AgentConfig]:
        """根据 AccountId + ApplicationId 查询该用户在该应用下的 agent 配置"""
        record = (
            await db.execute(
                select(AgentConfig)
                .where(
                    AgentConfig.AccountId == account_id,
                    AgentConfig.ApplicationId == application_id,
                )
                .limit(1)
            )
        ).scalar()
        return record

    @staticmethod
    async def upsert(
        db: AsyncSession, account_id: str, application_id: str, agent_id: str
    ) -> AgentConfig:
        """新增或更新指定用户在指定应用下的 agent_id"""
        record = await CoreAgent.get(db, account_id, application_id)
        if record is None:
            record = AgentConfig(
                AccountId=account_id,
                ApplicationId=application_id,
                AgentId=agent_id,
            )
            db.add(record)
        else:
            record.AgentId = agent_id
        await db.commit()
        return record

    @staticmethod
    async def ensure(
        db: AsyncSession,
        account_id: str,
        application_id: str,
        vendor_app: BaseVendor,
        *,
        identity_context: WorkbenchIdentityContext | None = None,
    ) -> AgentConfig:
        """Create exactly one Kind=1 agent for a trusted workbench identity."""
        if (
            identity_context is None
            or identity_context.binding_id == ""
            or identity_context.application_id != application_id
            or int(identity_context.config_version) <= 0
        ):
            raise AgentProvisioningError("exact Agent ownership context is required", 500)
        identity = (
            await db.execute(
                select(WorkbenchIdentity)
                .where(
                    WorkbenchIdentity.AccountId == account_id,
                    WorkbenchIdentity.Status == "active",
                )
                .with_for_update()
            )
        ).scalar()
        if identity is None:
            raise ValueError("active workbench identity is required")

        existing_record = await CoreAgent.get(db, account_id, application_id)

        binding = (
            await db.execute(
                select(WorkbenchAgentBinding)
                .where(
                    WorkbenchAgentBinding.BindingId == identity.BindingId,
                    WorkbenchAgentBinding.ApplicationId == application_id,
                )
                .with_for_update()
            )
        ).scalar()
        if binding is not None:
            if binding.Status == "active" and binding.AgentId:
                expected_profile_id = str(identity_context.app_profile_id)
                if binding.AppProfileId not in {None, expected_profile_id} or binding.ConfigVersion not in {
                    None,
                    int(identity_context.config_version),
                }:
                    raise AgentProvisioningError(
                        "Agent binding App profile or config version changed",
                        409,
                    )
                binding.AppProfileId = expected_profile_id
                binding.ConfigVersion = int(identity_context.config_version)
                db.add(binding)
                record = existing_record
                if record is None:
                    record = AgentConfig(
                        AccountId=account_id,
                        ApplicationId=application_id,
                        AgentId=binding.AgentId,
                    )
                    db.add(record)
                elif record.AgentId != binding.AgentId:
                    raise AgentProvisioningError("local Agent binding conflict")
                await CoreAgent._report_workbench_agent(
                    db,
                    account_id=account_id,
                    identity_context=identity_context,
                    agent_id=binding.AgentId,
                )
                return record
            raise AgentProvisioningError(
                "Agent provisioning requires reconciliation before it can be retried"
            )

        if existing_record is not None:
            raise AgentProvisioningError(
                "legacy Agent configuration requires reconciliation before provisioning"
            )

        provider_app_id = str(vendor_app.config.get("AppId") or "").strip()
        if not provider_app_id:
            raise ValueError("trusted provider AppId is missing")
        binding = WorkbenchAgentBinding(
            BindingId=identity.BindingId,
            AccountId=account_id,
            ApplicationId=application_id,
            AppProfileId=str(identity_context.app_profile_id),
            ConfigVersion=int(identity_context.config_version),
            Status="provisioning",
            AttemptId=uuid.uuid4().hex,
        )
        db.add(binding)
        await db.commit()
        provision_started = time.monotonic()

        try:
            response = await vendor_app.forward_request(
                "CopyAgentFromApp",
                {
                    "AppId": provider_app_id,
                    "Kind": 1,
                },
            )
            agent_id = str(response.get("ParentAgentId") or "").strip()
            if not agent_id or len(agent_id) > 64:
                raise ValueError("CopyAgentFromApp did not return a valid ParentAgentId")
        except Exception as error:
            binding.Status = "provider_unknown"
            binding.ErrorCode = type(error).__name__[:64]
            db.add(binding)
            await db.commit()
            WORKBENCH_METRICS.observe(
                "workbench_agent_provision_seconds",
                time.monotonic() - provision_started,
            )
            raise AgentProvisioningError(
                "Agent provisioning outcome is unknown and requires reconciliation",
                502,
            ) from error

        record = AgentConfig(
            AccountId=account_id,
            ApplicationId=application_id,
            AgentId=agent_id,
        )
        binding.AgentId = agent_id
        binding.Status = "active"
        binding.ErrorCode = None
        db.add(record)
        db.add(binding)
        try:
            await CoreAgent._report_workbench_agent(
                db,
                account_id=account_id,
                identity_context=identity_context,
                agent_id=agent_id,
            )
        finally:
            WORKBENCH_METRICS.observe(
                "workbench_agent_provision_seconds",
                time.monotonic() - provision_started,
            )
        return record

    @classmethod
    async def ensure_runtime_principal(
        cls,
        db: AsyncSession,
        account_id: str,
        app_context: WorkbenchAppContext,
        vendor_app: BaseVendor,
        *,
        max_output_tokens: int,
        max_reasoning_rounds: int,
        identity_context: WorkbenchIdentityContext,
    ) -> WorkbenchRuntimePrincipal:
        """Resolve the server-owned conversation parent for one runtime profile.

        Dynamic Claw keeps the existing Kind=1 provisioning and verified Agent
        limit path. Other profiles use an opaque local ownership principal. The
        latter is deliberately never sent to Tencent and performs no provider
        Agent operation.
        """
        if app_context.runtime.uses_provider_user_agent:
            record = await cls.ensure_turn_limits(
                db,
                account_id,
                app_context.application_id,
                vendor_app,
                max_output_tokens=max_output_tokens,
                max_reasoning_rounds=max_reasoning_rounds,
                identity_context=identity_context,
            )
            if str(record.AgentId).startswith(cls._LOCAL_RUNTIME_PREFIX):
                raise AgentProvisioningError(
                    "runtime profile changed and requires Agent reconciliation",
                    409,
                )
            return WorkbenchRuntimePrincipal(
                ownership_id=str(record.AgentId),
                provider_agent_id=str(record.AgentId),
            )

        if (
            identity_context.binding_id == ""
            or identity_context.application_id != app_context.application_id
            or str(identity_context.app_profile_id) != str(app_context.app_profile_id)
            or int(identity_context.config_version) != int(app_context.config_version)
        ):
            raise AgentProvisioningError("exact runtime ownership context is required", 500)
        identity = (
            await db.execute(
                select(WorkbenchIdentity)
                .where(
                    WorkbenchIdentity.AccountId == account_id,
                    WorkbenchIdentity.Status == "active",
                )
                .with_for_update()
            )
        ).scalar()
        if identity is None or identity.BindingId != identity_context.binding_id:
            raise AgentProvisioningError("active workbench identity is required", 409)

        digest = hashlib.sha256(
            "\x00".join(
                (
                    identity_context.binding_id,
                    app_context.application_id,
                    str(app_context.app_profile_id),
                    str(app_context.config_version),
                    app_context.runtime_profile,
                )
            ).encode("utf-8")
        ).hexdigest()[:48]
        principal_id = cls._LOCAL_RUNTIME_PREFIX + digest
        binding = await cls._lock_workbench_binding(
            db,
            account_id,
            app_context.application_id,
        )
        record = await cls.get(db, account_id, app_context.application_id)
        if binding is not None:
            if (
                binding.Status != "active"
                or binding.AgentId != principal_id
                or binding.AppProfileId != str(app_context.app_profile_id)
                or int(binding.ConfigVersion or 0) != int(app_context.config_version)
                or record is None
                or record.AgentId != principal_id
            ):
                raise AgentProvisioningError(
                    "runtime profile changed and requires Agent reconciliation",
                    409,
                )
        else:
            if record is not None:
                raise AgentProvisioningError(
                    "legacy Agent configuration requires reconciliation before runtime activation",
                    409,
                )
            binding = WorkbenchAgentBinding(
                BindingId=identity.BindingId,
                AccountId=account_id,
                ApplicationId=app_context.application_id,
                AppProfileId=str(app_context.app_profile_id),
                ConfigVersion=int(app_context.config_version),
                AgentId=principal_id,
                Status="active",
                AttemptId=uuid.uuid4().hex,
            )
            record = AgentConfig(
                AccountId=account_id,
                ApplicationId=app_context.application_id,
                AgentId=principal_id,
            )
            db.add(binding)
            db.add(record)
        await cls._report_workbench_agent(
            db,
            account_id=account_id,
            identity_context=identity_context,
            agent_id=principal_id,
        )
        return WorkbenchRuntimePrincipal(
            ownership_id=principal_id,
            provider_agent_id=None,
        )

    @staticmethod
    async def _report_workbench_agent(
        db: AsyncSession,
        *,
        account_id: str,
        identity_context: WorkbenchIdentityContext | None,
        agent_id: str,
    ) -> None:
        if identity_context is None:
            raise AgentProvisioningError("Agent ownership context is required", 500)
        try:
            report = await WorkbenchResourceReporter.enqueue(
                db,
                identity=identity_context,
                resource_type="agent",
                resource_id=agent_id,
                parent_resource_type="account",
                parent_resource_id=str(account_id),
            )
            await db.commit()
            await WorkbenchResourceReporter.deliver_event(
                db,
                report.EventId,
                fail_closed_on_rejection=True,
            )
        except WorkbenchResourceReportError as error:
            raise AgentProvisioningError(str(error), error.status_code) from error

    @staticmethod
    def _verified_limit_state(
        response: dict,
        *,
        agent_id: str,
        max_output_tokens: int,
        max_reasoning_rounds: int,
    ) -> tuple[bool, dict, dict]:
        agent = response.get("Agent") if isinstance(response, dict) else None
        if not isinstance(agent, dict) or str(agent.get("AgentId") or "") != agent_id:
            raise AgentProvisioningError("provider Agent limit response is invalid", 502)
        for field_name in ("ToolList", "PluginList", "SkillList"):
            values = agent.get(field_name)
            if not isinstance(values, list):
                raise AgentProvisioningError(
                    f"provider Agent {field_name} cannot be verified",
                    503,
                )
        # Integration execution is independently bounded and re-read by
        # WorkbenchIntegrations before a Turn is durably submitted. Limit
        # enforcement only updates Model and AdvancedConfig and must not erase
        # the exact PluginList/ToolList maintained by that subsystem.

        model = agent.get("Model")
        advanced_config = agent.get("AdvancedConfig")
        if not isinstance(model, dict) or not isinstance(advanced_config, dict):
            raise AgentProvisioningError("provider Agent limit fields are incomplete", 503)
        model_parameters = model.get("ModelParameters")
        if not isinstance(model_parameters, dict):
            raise AgentProvisioningError("provider Agent model parameters are incomplete", 503)

        actual_max_tokens = model_parameters.get("MaxTokens")
        actual_reasoning_rounds = advanced_config.get("MaxReasoningRound")
        if (
            actual_max_tokens is not None
            and (
                isinstance(actual_max_tokens, bool)
                or not isinstance(actual_max_tokens, int)
            )
        ) or (
            actual_reasoning_rounds is not None
            and (
                isinstance(actual_reasoning_rounds, bool)
                or not isinstance(actual_reasoning_rounds, int)
            )
        ):
            raise AgentProvisioningError("provider Agent limit fields are invalid", 503)
        return (
            actual_max_tokens == max_output_tokens
            and actual_reasoning_rounds == max_reasoning_rounds,
            model,
            advanced_config,
        )

    @classmethod
    async def ensure_turn_limits(
        cls,
        db: AsyncSession,
        account_id: str,
        application_id: str,
        vendor_app: BaseVendor,
        *,
        max_output_tokens: int,
        max_reasoning_rounds: int,
        identity_context: WorkbenchIdentityContext | None = None,
    ) -> AgentConfig:
        """Apply and verify the two documented per-user Agent Turn limits.

        The caller must already hold the per-user runtime lease.  The binding
        state is changed to ``configuring`` before the provider side effect so a
        crash or competing path cannot silently start a Turn with unknown limits.
        """
        for value in (max_output_tokens, max_reasoning_rounds):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise AgentProvisioningError("trusted Agent limits are invalid", 503)

        try:
            record = await cls.ensure(
                db,
                account_id,
                application_id,
                vendor_app,
                identity_context=identity_context,
            )
        except ValueError as error:
            raise AgentProvisioningError(str(error), 503) from error
        binding = await cls._lock_workbench_binding(db, account_id, application_id)
        if (
            binding is None
            or binding.Status != "active"
            or binding.AgentId != record.AgentId
        ):
            raise AgentProvisioningError(
                "Agent limit configuration has a version or lock conflict",
                409,
            )

        provider_app_id = str(vendor_app.config.get("AppId") or "").strip()
        if not provider_app_id:
            raise AgentProvisioningError("trusted provider AppId is missing", 503)
        fingerprint = hashlib.sha256(
            f"{max_output_tokens}:{max_reasoning_rounds}".encode("utf-8")
        ).hexdigest()
        attempt_id = uuid.uuid4().hex
        binding.Status = "configuring"
        binding.AttemptId = attempt_id
        binding.ErrorCode = None
        db.add(binding)
        await db.commit()

        modify_started = False
        try:
            detail = await vendor_app.forward_request(
                "DescribeAgentDetail",
                {"AppId": provider_app_id, "AgentId": record.AgentId},
            )
            matches, model, advanced_config = cls._verified_limit_state(
                detail,
                agent_id=record.AgentId,
                max_output_tokens=max_output_tokens,
                max_reasoning_rounds=max_reasoning_rounds,
            )
            if not matches:
                model = copy.deepcopy(model)
                advanced_config = copy.deepcopy(advanced_config)
                model["ModelParameters"]["MaxTokens"] = max_output_tokens
                advanced_config["MaxReasoningRound"] = max_reasoning_rounds
                modify_started = True
                await vendor_app.forward_request(
                    "ModifyAgent",
                    {
                        "AppId": provider_app_id,
                        "AgentId": record.AgentId,
                        "Agent": {
                            "Model": model,
                            "AdvancedConfig": advanced_config,
                        },
                        "UpdateMask": {"Paths": ["Model", "AdvancedConfig"]},
                    },
                )
                detail = await vendor_app.forward_request(
                    "DescribeAgentDetail",
                    {"AppId": provider_app_id, "AgentId": record.AgentId},
                )
                matches, _, _ = cls._verified_limit_state(
                    detail,
                    agent_id=record.AgentId,
                    max_output_tokens=max_output_tokens,
                    max_reasoning_rounds=max_reasoning_rounds,
                )
                if not matches:
                    raise AgentProvisioningError(
                        "provider Agent limits did not match after modification",
                        502,
                    )
        except Exception as error:
            binding = await cls._lock_workbench_binding(
                db,
                account_id,
                application_id,
            )
            if (
                binding is None
                or binding.Status != "configuring"
                or binding.AttemptId != attempt_id
            ):
                raise AgentProvisioningError(
                    "Agent limit configuration version changed during provider update",
                    409,
                ) from error
            binding.Status = "provider_unknown" if modify_started else "active"
            binding.ErrorCode = type(error).__name__[:64]
            db.add(binding)
            await db.commit()
            if isinstance(error, AgentProvisioningError):
                raise
            raise AgentProvisioningError(
                "Agent limit enforcement failed closed",
                502,
            ) from error

        binding = await cls._lock_workbench_binding(
            db,
            account_id,
            application_id,
        )
        if (
            binding is None
            or binding.Status != "configuring"
            or binding.AttemptId != attempt_id
        ):
            raise AgentProvisioningError(
                "Agent limit configuration version changed before verification commit",
                409,
            )
        binding.Status = "active"
        binding.LimitFingerprint = fingerprint
        binding.LimitsVerifiedAt = datetime.now(UTC).replace(tzinfo=None)
        binding.ErrorCode = None
        db.add(binding)
        await db.commit()
        return record

    @staticmethod
    async def delete(db: AsyncSession, account_id: str, application_id: str) -> None:
        """删除指定用户在指定应用下的 agent 配置"""
        record = await CoreAgent.get(db, account_id, application_id)
        if record is None:
            return
        await db.delete(record)
        await db.commit()
