import base64
import json
import uuid

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from config import tagentic_config
from core.workbench_control import WorkbenchAppContext, WorkbenchIdentityContext
from core.workbench_usage_telemetry import (
    WorkbenchUsageTelemetry,
    WorkbenchUsageTelemetryError,
)
from model.workbench import (
    WorkbenchTurn,
    WorkbenchTurnEvidence,
    WorkbenchTurnUsageDatum,
)


ACCOUNT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


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


def _identity():
    return WorkbenchIdentityContext(
        binding_id="binding-7",
        canonical_subject="napi:prod:customer:7:user:9",
        customer_id=7,
        new_api_user_id=9,
        auth_epoch=1,
        display_name="User 9",
        application_id="customer-app-7",
        app_profile_id="17",
        access_mode="active",
        config_version=3,
    )


def _app_context():
    return WorkbenchAppContext(
        application_id="customer-app-7",
        app_profile_id="17",
        config_version=3,
        auth_epoch=1,
        vendor="Tencent",
        service_vendor="ChinaTencentCloud",
        app_id="provider-app-7",
        app_key="provider-key",
        space_id="",
        template_agent_id="template-7",
        secret_id="secret-id",
        secret_key="secret-key",
    )


@pytest.fixture
def telemetry_store(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def register_uuid(dbapi_connection, _connection_record):
        dbapi_connection.create_function("uuid_generate_v4", 0, lambda: uuid.uuid4().hex)

    WorkbenchTurn.__table__.create(engine)
    WorkbenchTurnEvidence.__table__.create(engine)
    WorkbenchTurnUsageDatum.__table__.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as session:
        session.add(
            WorkbenchTurn(
                TurnId="wt_test",
                BindingId="binding-7",
                AccountId=ACCOUNT_ID,
                CustomerId=7,
                ApplicationId="customer-app-7",
                ClientRequestId=str(uuid.uuid4()),
                RequestDigest="a" * 64,
                Status="running",
                OwnerInstanceId="instance-a",
                OwnerLifecycleId="lifecycle-a",
                EventCount=1,
                EventBytes=1,
            )
        )
        session.commit()
    monkeypatch.setattr(
        tagentic_config,
        "WORKBENCH_USAGE_EVIDENCE_KEY",
        base64.b64encode(b"u" * 32).decode("ascii"),
    )
    monkeypatch.setattr(tagentic_config, "WORKBENCH_USAGE_EVIDENCE_KEY_ID", "usage-v1")
    yield sessions
    engine.dispose()


def _payload():
    return {
        "Type": "response.completed",
        "Response": {
            "RecordId": "record-9",
            "ConversationId": "conversation-9",
            "Messages": [{"Type": "reply", "Text": "sensitive customer answer"}],
            "ExtraInfo": {
                "RequestId": "request-9",
                "TraceId": "trace-9",
            },
            "StatInfo": {
                "InputTokens": 11,
                "OutputTokens": 7,
                "TotalTokens": 31,
                "FirstTokenCost": 123,
                "TotalCost": 456,
                "ModelName": "model-a",
            },
            "Procedures": [
                {
                    "StatInfos": [{"InputTokens": 5, "OutputTokens": 3}],
                    "Workflow": {
                        "WorkflowRunId": "run-9",
                        "RunNodes": [
                            {
                                "NodeId": "node-2",
                                "StatInfos": [
                                    {
                                        "InputTokens": 2,
                                        "OutputTokens": 1,
                                        "CacheReadTokens": 4,
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_completion_evidence_is_encrypted_and_usage_remains_non_additive(
    telemetry_store,
    caplog,
):
    session = telemetry_store()
    try:
        evidence_id = await WorkbenchUsageTelemetry.capture_completed_event(
            _AsyncSessionAdapter(session),
            turn_id="wt_test",
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            payload=_payload(),
        )
    finally:
        session.close()

    with telemetry_store() as session:
        evidence = session.execute(
            select(WorkbenchTurnEvidence).where(
                WorkbenchTurnEvidence.EvidenceId == evidence_id
            )
        ).scalar_one()
        rows = list(
            session.execute(
                select(WorkbenchTurnUsageDatum).order_by(
                    WorkbenchTurnUsageDatum.SourcePath
                )
            ).scalars()
        )
        assert evidence.ProviderRequestId == "request-9"
        assert evidence.ProviderTraceId == "trace-9"
        assert evidence.ProviderRecordId == "record-9"
        assert evidence.ConversationId == "conversation-9"
        assert evidence.EncryptionKeyId == "usage-v1"
        assert "sensitive customer answer" not in evidence.EvidenceCiphertext
        assert WorkbenchUsageTelemetry.decrypt_for_audit(
            evidence.EvidenceCiphertext,
            evidence.EvidenceSha256,
        ) == _payload()

        assert len(rows) == 3
        top = next(row for row in rows if row.SourcePath == "$.Response.StatInfo")
        top_metrics = json.loads(top.MetricsJson)
        assert top.DedupeConfidence == "unknown"
        assert top.StableUsageKey is None
        assert top_metrics["InputTokens"] == 11
        assert top_metrics["OutputTokens"] == 7
        assert top_metrics["TotalTokens"] == 31
        assert top_metrics["AggregationPolicy"] == "non_additive"
        assert top_metrics["CacheTokensStatus"] == "unknown"
        assert top_metrics["FirstTokenCostMeaning"] == "first_token_latency"
        assert top_metrics["TotalCostMeaning"] == "model_total_latency"

        node = next(row for row in rows if ".RunNodes[0]." in row.SourcePath)
        node_metrics = json.loads(node.MetricsJson)
        assert node.DedupeConfidence == "strong"
        assert len(node.StableUsageKey) == 64
        assert node_metrics["CacheReadTokens"] == 4
        assert node_metrics["CacheTokensStatus"] == "reported"

    assert "sensitive customer answer" not in caplog.text


@pytest.mark.asyncio
async def test_completion_capture_is_idempotent_but_rejects_conflicting_evidence(
    telemetry_store,
):
    session = telemetry_store()
    adapter = _AsyncSessionAdapter(session)
    try:
        first = await WorkbenchUsageTelemetry.capture_completed_event(
            adapter,
            turn_id="wt_test",
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            payload=_payload(),
        )
        second = await WorkbenchUsageTelemetry.capture_completed_event(
            adapter,
            turn_id="wt_test",
            account_id=ACCOUNT_ID,
            identity=_identity(),
            app_context=_app_context(),
            payload=_payload(),
        )
        assert first == second

        conflicting = _payload()
        conflicting["Response"]["RecordId"] = "record-other"
        with pytest.raises(WorkbenchUsageTelemetryError, match="conflicting"):
            await WorkbenchUsageTelemetry.capture_completed_event(
                adapter,
                turn_id="wt_test",
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                payload=conflicting,
            )
    finally:
        session.close()

    with telemetry_store() as session:
        assert session.execute(select(func.count(WorkbenchTurnEvidence.Id))).scalar_one() == 1


@pytest.mark.asyncio
async def test_completion_capture_fails_closed_without_independent_key(
    telemetry_store,
    monkeypatch,
):
    monkeypatch.setattr(tagentic_config, "WORKBENCH_USAGE_EVIDENCE_KEY", "")
    session = telemetry_store()
    try:
        with pytest.raises(WorkbenchUsageTelemetryError, match="not configured") as error:
            await WorkbenchUsageTelemetry.capture_completed_event(
                _AsyncSessionAdapter(session),
                turn_id="wt_test",
                account_id=ACCOUNT_ID,
                identity=_identity(),
                app_context=_app_context(),
                payload=_payload(),
            )
        assert error.value.status_code == 503
    finally:
        session.close()


def test_migration_registers_usage_evidence_tables():
    from unittest.mock import patch

    with patch("app_factory.TAgenticApp.get_app", return_value=type("App", (), {})()):
        from core.migration import Migration

    assert WorkbenchTurnEvidence in Migration.tables()
    assert WorkbenchTurnUsageDatum in Migration.tables()
