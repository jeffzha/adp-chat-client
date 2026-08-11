from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app_factory import TAgenticApp
from core.workbench_control_events import WorkbenchControlEvent, WorkbenchControlEventError
from model.workbench import WorkbenchAppLineage


class WorkbenchAppLineageStore:
    @staticmethod
    async def activate(event: WorkbenchControlEvent) -> None:
        payload = event.payload
        if event.customer_id != payload["customer_id"]:
            raise WorkbenchControlEventError("customer_scope_mismatch")
        app = TAgenticApp.get_app()
        sessionmaker = app.config.get("sessionmaker")
        if sessionmaker is None:
            raise WorkbenchControlEventError("lineage_database_unavailable")
        db = sessionmaker()
        try:
            existing = (
                await db.execute(
                    select(WorkbenchAppLineage).where(
                        WorkbenchAppLineage.EventKey == event.event_key
                    )
                )
            ).scalar_one_or_none()
            expected = (
                payload["lineage_id"],
                payload["customer_id"],
                payload["migration_job_id"],
                payload["source_application_id"],
                payload["source_provider_app_id"],
                payload["source_app_profile_id"],
                payload["source_config_version"],
                payload["target_application_id"],
                payload["target_provider_app_id"],
                payload["target_app_profile_id"],
                payload["target_config_version"],
                payload["migration_config_fingerprint"],
            )
            if existing is not None:
                actual = (
                    existing.LineageId,
                    existing.CustomerId,
                    existing.MigrationJobId,
                    existing.SourceApplicationId,
                    existing.SourceProviderAppId,
                    existing.SourceAppProfileId,
                    existing.SourceConfigVersion,
                    existing.TargetApplicationId,
                    existing.TargetProviderAppId,
                    existing.TargetAppProfileId,
                    existing.TargetConfigVersion,
                    existing.MigrationConfigFingerprint,
                )
                if actual != expected or existing.Status != "active":
                    raise WorkbenchControlEventError("lineage_replay_mismatch")
                return
            db.add(
                WorkbenchAppLineage(
                    LineageId=payload["lineage_id"],
                    EventKey=event.event_key,
                    CustomerId=payload["customer_id"],
                    MigrationJobId=payload["migration_job_id"],
                    SourceApplicationId=payload["source_application_id"],
                    SourceProviderAppId=payload["source_provider_app_id"],
                    SourceAppProfileId=payload["source_app_profile_id"],
                    SourceConfigVersion=payload["source_config_version"],
                    TargetApplicationId=payload["target_application_id"],
                    TargetProviderAppId=payload["target_provider_app_id"],
                    TargetAppProfileId=payload["target_app_profile_id"],
                    TargetConfigVersion=payload["target_config_version"],
                    MigrationConfigFingerprint=payload["migration_config_fingerprint"],
                    Status="active",
                    ActivatedAt=event.created_at.replace(tzinfo=None),
                )
            )
            try:
                await db.commit()
            except IntegrityError as error:
                await db.rollback()
                raise WorkbenchControlEventError("lineage_tuple_conflict") from error
        finally:
            await db.close()

    @staticmethod
    async def readable_source_tuples(db, identity, current_app_context) -> tuple[tuple[str, str, str, int], ...]:
        rows = tuple(
            (
                await db.execute(
                    select(WorkbenchAppLineage)
                    .where(
                        WorkbenchAppLineage.CustomerId == identity.customer_id,
                        WorkbenchAppLineage.Status == "active",
                    )
                    .order_by(
                        WorkbenchAppLineage.ActivatedAt.desc(),
                        WorkbenchAppLineage.LineageId.asc(),
                    )
                )
            ).scalars()
        )
        predecessors: dict[
            tuple[str, str, str, int],
            list[tuple[str, str, str, int]],
        ] = {}
        for row in rows:
            target = (
                row.TargetApplicationId,
                row.TargetProviderAppId,
                str(row.TargetAppProfileId),
                int(row.TargetConfigVersion),
            )
            source = (
                row.SourceApplicationId,
                row.SourceProviderAppId,
                str(row.SourceAppProfileId),
                int(row.SourceConfigVersion),
            )
            predecessors.setdefault(target, []).append(source)

        current = (
            current_app_context.application_id,
            current_app_context.app_id,
            str(current_app_context.app_profile_id),
            int(current_app_context.config_version),
        )
        visited = {current}
        frontier = [current]
        readable: list[tuple[str, str, str, int]] = []
        while frontier:
            target = frontier.pop(0)
            for source in predecessors.get(target, ()):
                if source in visited:
                    continue
                visited.add(source)
                readable.append(source)
                frontier.append(source)
        return tuple(readable)
