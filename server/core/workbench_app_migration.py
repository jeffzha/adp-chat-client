import asyncio
import hashlib
import logging
import uuid

import ujson
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app_factory import TAgenticApp
from config import tagentic_config
from core.workbench_app_resolver import WorkbenchAppResolver
from core.workbench_control import (
    WorkbenchAppMigrationTask,
    WorkbenchControlClient,
    WorkbenchControlError,
)
from model.agent import AgentConfig
from model.workbench import WorkbenchAgentBinding, WorkbenchIdentity


class WorkbenchAppMigrationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class WorkbenchAppMigrationWorker:
    _task: asyncio.Task | None = None
    _instance_id = f"adp-{uuid.uuid4().hex}"

    @classmethod
    async def _locked_identity(
        cls,
        db: AsyncSession,
        task: WorkbenchAppMigrationTask,
    ) -> WorkbenchIdentity:
        identity = (
            await db.execute(
                select(WorkbenchIdentity)
                .where(
                    WorkbenchIdentity.BindingId == task.binding_id,
                    WorkbenchIdentity.AccountId == task.adp_account_id,
                    WorkbenchIdentity.CustomerId == task.customer_id,
                    WorkbenchIdentity.Status == "active",
                )
                .with_for_update()
            )
        ).scalar()
        if identity is None:
            raise WorkbenchAppMigrationError("identity_binding_mismatch")
        return identity

    @classmethod
    async def _locked_binding(
        cls,
        db: AsyncSession,
        task: WorkbenchAppMigrationTask,
    ) -> WorkbenchAgentBinding | None:
        return (
            await db.execute(
                select(WorkbenchAgentBinding)
                .where(
                    WorkbenchAgentBinding.BindingId == task.binding_id,
                    WorkbenchAgentBinding.AccountId == task.adp_account_id,
                    WorkbenchAgentBinding.ApplicationId
                    == task.target_application_id,
                )
                .with_for_update()
            )
        ).scalar()

    @staticmethod
    def _readback_hash(response: dict, agent_id: str) -> str:
        agent = response.get("Agent") if isinstance(response, dict) else None
        if not isinstance(agent, dict) or str(agent.get("AgentId") or "").strip() != agent_id:
            raise WorkbenchAppMigrationError("target_agent_readback_mismatch")
        canonical = ujson.dumps(
            {"Agent": agent},
            ensure_ascii=False,
            sort_keys=True,
            escape_forward_slashes=False,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _local_runtime_principal_id(task: WorkbenchAppMigrationTask) -> str:
        digest = hashlib.sha256(
            "\x00".join(
                (
                    task.binding_id,
                    task.target_application_id,
                    str(task.target_app_profile_id),
                    str(task.target_config_version),
                    task.runtime_profile,
                )
            ).encode("utf-8")
        ).hexdigest()[:48]
        return "wrp_" + digest

    @classmethod
    async def _activate_local_runtime_principal(
        cls,
        db: AsyncSession,
        task: WorkbenchAppMigrationTask,
        binding: WorkbenchAgentBinding,
        existing_principal_id: str | None,
    ) -> None:
        principal_id = cls._local_runtime_principal_id(task)
        if existing_principal_id is not None and existing_principal_id != principal_id:
            raise WorkbenchAppMigrationError(
                "target_runtime_principal_requires_reconciliation"
            )
        if existing_principal_id is None:
            binding = await cls._locked_binding(db, task)
            if (
                binding is None
                or binding.Status != "provisioning"
                or binding.AttemptId != task.attempt_id
            ):
                raise WorkbenchAppMigrationError(
                    "target_runtime_binding_attempt_changed"
                )

        config = (
            await db.execute(
                select(AgentConfig)
                .where(
                    AgentConfig.AccountId == task.adp_account_id,
                    AgentConfig.ApplicationId == task.target_application_id,
                )
                .with_for_update()
            )
        ).scalar()
        if config is None:
            config = AgentConfig(
                AccountId=task.adp_account_id,
                ApplicationId=task.target_application_id,
                AgentId=principal_id,
            )
            db.add(config)
        elif config.AgentId != principal_id:
            raise WorkbenchAppMigrationError("target_agent_config_conflict")
        binding.AgentId = principal_id
        binding.Status = "active"
        binding.ErrorCode = None
        db.add(binding)
        await db.commit()

    @classmethod
    async def _prepare_local_binding(
        cls,
        db: AsyncSession,
        task: WorkbenchAppMigrationTask,
    ) -> tuple[WorkbenchAgentBinding, str | None]:
        await cls._locked_identity(db, task)
        binding = await cls._locked_binding(db, task)
        if binding is not None:
            if task.mode == "readback":
                allowed_status = {"provider_unknown", "verifying", "active"}
                expected_agent_id = task.known_target_agent_id
            else:
                allowed_status = {"active"}
                expected_agent_id = str(binding.AgentId or "")
            if (
                binding.Status not in allowed_status
                or not expected_agent_id
                or binding.AgentId != expected_agent_id
                or binding.AppProfileId != str(task.target_app_profile_id)
                or binding.ConfigVersion != task.target_config_version
                or binding.ConfigFingerprint != task.target_config_fingerprint
                or binding.MigrationMemberId != task.migration_member_id
            ):
                raise WorkbenchAppMigrationError(
                    "target_agent_binding_requires_reconciliation"
                )
            if task.mode == "readback":
                binding.Status = "verifying"
                binding.AttemptId = task.attempt_id
                binding.ErrorCode = None
                db.add(binding)
                await db.commit()
            return binding, expected_agent_id

        existing_config = (
            await db.execute(
                select(AgentConfig)
                .where(
                    AgentConfig.AccountId == task.adp_account_id,
                    AgentConfig.ApplicationId == task.target_application_id,
                )
                .with_for_update()
            )
        ).scalar()
        if existing_config is not None:
            raise WorkbenchAppMigrationError("target_agent_config_conflict")

        binding = WorkbenchAgentBinding(
            BindingId=task.binding_id,
            AccountId=task.adp_account_id,
            ApplicationId=task.target_application_id,
            AppProfileId=str(task.target_app_profile_id),
            ConfigVersion=task.target_config_version,
            ConfigFingerprint=task.target_config_fingerprint,
            MigrationMemberId=task.migration_member_id,
            Status="provisioning",
            AttemptId=task.attempt_id,
        )
        db.add(binding)
        await db.commit()
        return binding, None

    @classmethod
    async def process_one(
        cls,
        db: AsyncSession,
        task: WorkbenchAppMigrationTask,
    ) -> None:
        if task.provider.application_id != task.target_application_id:
            raise WorkbenchAppMigrationError("target_provider_context_mismatch")
        try:
            runtime = task.provider.runtime
        except ValueError as error:
            raise WorkbenchAppMigrationError("target_runtime_profile_invalid") from error
        if (
            runtime.provider_app_mode != task.provider_app_mode
            or runtime.name != task.runtime_profile
            or runtime.execution_enabled != task.execution_enabled
        ):
            raise WorkbenchAppMigrationError("target_runtime_profile_mismatch")
        provider_context_installed = False
        if runtime.uses_provider_user_agent:
            await WorkbenchAppResolver.ensure_vendor(task.provider)
            provider_context_installed = True
        provider_side_effect_started = False
        locally_verified = False
        known_agent_id = task.known_target_agent_id
        try:
            binding, existing_agent_id = await cls._prepare_local_binding(db, task)
            if not runtime.uses_provider_user_agent:
                await cls._activate_local_runtime_principal(
                    db,
                    task,
                    binding,
                    existing_agent_id,
                )
                locally_verified = True
                await WorkbenchControlClient.report_app_migration_task(
                    task,
                    status="succeeded",
                    target_agent_id="",
                    target_readback_hash="",
                )
                return
            vendor = TAgenticApp.get_app().get_vendor_app(task.target_application_id)
            agent_id = existing_agent_id
            if agent_id is None:
                provider_side_effect_started = True
                copied = await vendor.forward_request(
                    "CopyAgentFromApp",
                    {"AppId": task.provider.app_id, "Kind": 1},
                )
                agent_id = str(copied.get("ParentAgentId") or "").strip()
                if not agent_id or len(agent_id) > 64:
                    raise WorkbenchAppMigrationError("copy_agent_response_invalid")
                known_agent_id = agent_id
                binding = await cls._locked_binding(db, task)
                if binding is None or binding.Status != "provisioning" or binding.AttemptId != task.attempt_id:
                    raise WorkbenchAppMigrationError("target_agent_binding_attempt_changed")
                binding.AgentId = agent_id
                binding.Status = "verifying"
                db.add(binding)
                await db.commit()
            described = await vendor.forward_request(
                "DescribeAgentDetail",
                {"AppId": task.provider.app_id, "AgentId": agent_id},
            )
            readback_hash = cls._readback_hash(described, agent_id)

            binding = await cls._locked_binding(db, task)
            if binding is None:
                raise WorkbenchAppMigrationError("target_agent_binding_disappeared")
            if binding.Status not in {"verifying", "active"} or binding.AttemptId != task.attempt_id:
                raise WorkbenchAppMigrationError("target_agent_binding_attempt_changed")
            config = (
                await db.execute(
                    select(AgentConfig)
                    .where(
                        AgentConfig.AccountId == task.adp_account_id,
                        AgentConfig.ApplicationId == task.target_application_id,
                    )
                    .with_for_update()
                )
            ).scalar()
            if config is None:
                config = AgentConfig(
                    AccountId=task.adp_account_id,
                    ApplicationId=task.target_application_id,
                    AgentId=agent_id,
                )
                db.add(config)
            elif config.AgentId != agent_id:
                raise WorkbenchAppMigrationError("target_agent_config_conflict")
            binding.AgentId = agent_id
            binding.Status = "active"
            binding.ErrorCode = None
            db.add(binding)
            await db.commit()
            locally_verified = True
            for report_attempt in range(3):
                try:
                    await WorkbenchControlClient.report_app_migration_task(
                        task,
                        status="succeeded",
                        target_agent_id=agent_id,
                        target_readback_hash=readback_hash,
                    )
                    break
                except WorkbenchControlError:
                    if report_attempt == 2:
                        raise
                    await asyncio.sleep(0.2 * (report_attempt + 1))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if locally_verified:
                raise
            if provider_side_effect_started:
                current = await cls._locked_binding(db, task)
                if current is not None and current.AttemptId == task.attempt_id:
                    current.Status = "provider_unknown"
                    current.ErrorCode = type(error).__name__[:64]
                    db.add(current)
                    await db.commit()
                code = "provider_outcome_unknown"
            elif isinstance(error, WorkbenchAppMigrationError):
                code = error.code
            else:
                code = "target_agent_readback_failed"
            try:
                await WorkbenchControlClient.report_app_migration_task(
                    task,
                    status="failed",
                    target_agent_id=known_agent_id,
                    error_code=code[:80],
                )
            except WorkbenchControlError:
                logging.error(
                    "[workbench_app_migration] terminal report failed error_type=WorkbenchControlError"
                )
            raise
        finally:
            if provider_context_installed:
                WorkbenchAppResolver.revoke(task.target_application_id)

    @classmethod
    async def run(cls, sessionmaker) -> None:
        empty_delay = tagentic_config.WORKBENCH_APP_MIGRATION_POLL_SECONDS
        lease_seconds = tagentic_config.WORKBENCH_APP_MIGRATION_LEASE_SECONDS
        while True:
            task = None
            try:
                task = await WorkbenchControlClient.claim_app_migration_task(
                    worker_id=cls._instance_id,
                    lease_seconds=lease_seconds,
                )
                if task is None:
                    await asyncio.sleep(empty_delay)
                    continue
                db = sessionmaker()
                try:
                    async with asyncio.timeout(max(1, lease_seconds - 2)):
                        await cls.process_one(db, task)
                finally:
                    await db.close()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # pylint: disable=broad-except
                logging.error(
                    "[workbench_app_migration] task failed error_type=%s",
                    type(error).__name__,
                )
                await asyncio.sleep(empty_delay)

    @classmethod
    def start(cls, sessionmaker) -> None:
        if cls._task is None or cls._task.done():
            cls._task = asyncio.create_task(
                cls.run(sessionmaker),
                name="workbench-app-migration",
            )

    @classmethod
    async def stop(cls) -> None:
        if cls._task is None:
            return
        cls._task.cancel()
        await asyncio.gather(cls._task, return_exceptions=True)
        cls._task = None
