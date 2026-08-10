import json
import hashlib
import hmac
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from core.workbench_retention import CoreWorkbenchRetention, WorkbenchRetentionError
from model.workbench_retention import WorkbenchRetentionReceipt
from core.workbench_file_ownership import WorkbenchFileOwnership


class _Scalars:
    def __init__(self, values):
        self._values = values

    def __iter__(self):
        return iter(self._values)


class _Result:
    def __init__(self, *, scalar=None, values=None, rowcount=1):
        self._scalar = scalar
        self._values = values or []
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return _Scalars(self._values)


class _DB:
    def __init__(self, select_results):
        self.select_results = list(select_results)
        self.added = []
        self.commits = 0

    async def execute(self, _statement):
        if self.select_results:
            return self.select_results.pop(0)
        return _Result(rowcount=1)

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.commits += 1



def _intent(**changes):
    payload = {
        "intent_id": "rti_exact_scope_1",
        "customer_id": 7,
        "policy_version": 4,
        "cutoff_at": "2026-08-10T00:00:00Z",
        "legal_hold": False,
    }
    payload.update(changes)
    return payload


def test_retention_intent_requires_explicit_false_legal_hold():
    for value in (True, None, 0, "false"):
        with pytest.raises(WorkbenchRetentionError, match="legal hold"):
            CoreWorkbenchRetention.parse_intent(_intent(legal_hold=value))


@pytest.mark.asyncio
async def test_retention_deletes_only_ids_discovered_from_exact_customer_scope(monkeypatch):
    locator = {
        "Storage": "private_cos_v1",
        "ObjectKey": "workbench/customer-7/binding-7/file.pdf",
        "Bucket": "private-bucket",
        "Region": "ap-guangzhou",
        "ContentSha256": "a" * 64,
    }
    file_binding = SimpleNamespace(
        LocatorCiphertext="encrypted-locator",
        CosUrlHash=WorkbenchFileOwnership._digest(locator["ObjectKey"]),
        CosBucketHash=WorkbenchFileOwnership._digest(locator["Bucket"]),
        FileUrlHash=locator["ContentSha256"],
    )
    deleted = []

    class _Storage:
        async def delete(self, object_key, *, bucket, region):
            deleted.append((object_key, bucket, region))

    monkeypatch.setattr(
        CoreWorkbenchRetention,
        "_file_storage_factory",
        _Storage,
    )
    monkeypatch.setattr(
        WorkbenchFileOwnership,
        "_decrypt_locator",
        staticmethod(lambda _ciphertext: locator),
    )
    db = _DB(
        [
            _Result(scalar=None),
            _Result(values=[]),
            _Result(values=[]),
            _Result(values=["binding-7"]),
            _Result(values=["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"]),
            _Result(values=["file-7"]),
            _Result(values=[file_binding]),
            _Result(values=["turn-7"]),
            _Result(values=["task-7"]),
            _Result(values=["sandbox-7"]),
        ]
    )

    receipt = await CoreWorkbenchRetention.execute(db, _intent())

    assert receipt["status"] == "completed"
    assert receipt["intent_id"] == "rti_exact_scope_1"
    assert receipt["counts"]["connector_credentials"] == 1
    assert receipt["counts"]["oauth_revocations"] == 1
    assert receipt["counts"]["private_cos_objects_deleted"] == 1
    assert deleted == [
        (
            "workbench/customer-7/binding-7/file.pdf",
            "private-bucket",
            "ap-guangzhou",
        )
    ]
    assert db.commits == 1
    assert len(db.added) == 1
    stored = db.added[0]
    assert isinstance(stored, WorkbenchRetentionReceipt)
    assert stored.CustomerId == 7
    serialized = json.dumps(receipt, sort_keys=True)
    assert "ciphertext" not in serialized.lower()
    assert "secret" not in serialized.lower()


@pytest.mark.asyncio
async def test_retention_receipt_is_idempotent_and_changed_payload_fails_closed():
    _, digest = CoreWorkbenchRetention.parse_intent(_intent())
    existing = WorkbenchRetentionReceipt(
        ReceiptId="rtr_existing",
        IntentId="rti_exact_scope_1",
        RequestDigest=digest,
        CustomerId=7,
        PolicyVersion=4,
        CutoffAt=datetime(2026, 8, 10),
        Status="completed",
        CountsJson='{"conversations":1}',
        CompletedAt=datetime(2026, 8, 10),
    )
    same = await CoreWorkbenchRetention.execute(_DB([_Result(scalar=existing)]), _intent())
    assert same == {
        "receipt_id": "rtr_existing",
        "intent_id": "rti_exact_scope_1",
        "status": "completed",
        "counts": {"conversations": 1},
    }

    with pytest.raises(WorkbenchRetentionError, match="payload changed"):
        await CoreWorkbenchRetention.execute(
            _DB([_Result(scalar=existing)]), _intent(policy_version=5)
        )


@pytest.mark.asyncio
async def test_retention_fails_closed_until_oauth_provider_revocation_completes():
    active_credential = SimpleNamespace(Status="revoked", RevocationStatus="provider_unknown")
    with pytest.raises(WorkbenchRetentionError, match="revocation"):
        await CoreWorkbenchRetention.execute(
            _DB(
                [
                    _Result(scalar=None),
                    _Result(values=[active_credential]),
                    _Result(values=[]),
                ]
            ),
            _intent(),
        )


@pytest.mark.asyncio
async def test_retention_internal_endpoint_authenticates_exact_body():
    secret = "retention-endpoint-secret-0123456789"
    body = json.dumps(_intent(), separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = "retention-nonce-1"
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(
        (
            "2",
            "POST",
            "/api/internal/workbench/retention/intents",
            timestamp,
            nonce,
            body_hash,
        )
    )
    signature = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    headers = {
            "X-Workbench-Contract-Version": "2",
            "X-Workbench-Service": "claw-control",
            "X-Workbench-Timestamp": timestamp,
            "X-Workbench-Nonce": nonce,
            "X-Workbench-Signature": signature,
        }

    assert CoreWorkbenchRetention.verify_signed_request(
        "POST",
        "/api/internal/workbench/retention/intents",
        body,
        headers,
        secret,
        30,
        now=int(timestamp),
    ) == nonce

    headers["X-Workbench-Signature"] = "0" * 64
    with pytest.raises(WorkbenchRetentionError, match="signature"):
        CoreWorkbenchRetention.verify_signed_request(
            "POST",
            "/api/internal/workbench/retention/intents",
            body,
            headers,
            secret,
            30,
            now=int(timestamp),
        )
