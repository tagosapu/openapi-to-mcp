from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import sqlite3

import pytest

from tests.transfer.conftest import (
    sample_connector_definition,
    sample_mapping,
    sample_transfer_request,
    seed_transfer_for_tenant,
)


class PrefixProtector:
    def encrypt(self, plaintext: bytes) -> bytes:
        return b"enc:" + plaintext[::-1]

    def decrypt(self, ciphertext: bytes) -> bytes:
        assert ciphertext.startswith(b"enc:")
        return ciphertext[4:][::-1]


@pytest.fixture
async def store(test_settings, tmp_path):
    from src.transfer.store import SqliteTransferStore

    db_path = tmp_path / "store.sqlite3"
    transfer_store = SqliteTransferStore(db_path)
    await transfer_store.initialize()
    try:
        yield transfer_store
    finally:
        await transfer_store.close()


@pytest.fixture
async def encrypted_store(tmp_path):
    from src.transfer.store import SqliteTransferStore

    db_path = tmp_path / "encrypted.sqlite3"
    transfer_store = SqliteTransferStore(db_path, protector=PrefixProtector())
    await transfer_store.initialize()
    try:
        yield transfer_store, db_path
    finally:
        await transfer_store.close()


@pytest.mark.asyncio
async def test_store_keeps_connector_mapping_review_and_events_tenant_scoped(store) -> None:
    from src.transfer.errors import TenantIsolationError
    from src.transfer.models import ConnectorDefinition, MappingDefinition, TransferStatus

    await store.save_connector(
        ConnectorDefinition.model_validate(sample_connector_definition()),
        tenant_id="tenant-a",
    )
    await store.save_mapping(
        MappingDefinition.model_validate(sample_mapping()),
        tenant_id="tenant-a",
    )
    transfer_id = await seed_transfer_for_tenant(store, tenant_id="tenant-a")

    assert await store.get_connector("tenant-b", "connector-test") is None
    assert await store.get_mapping("tenant-b", "mapping-test") is None
    with pytest.raises(TenantIsolationError):
        await store.save_review_correction(
            "tenant-b",
            transfer_id,
            {"/ocr/fields/x/value": "y"},
            "reviewer",
            "wrong tenant",
        )
    with pytest.raises(TenantIsolationError):
        await store.transition(
            "tenant-b",
            transfer_id,
            TransferStatus.ACCEPTED,
            TransferStatus.CANCELLED,
            {},
        )


@pytest.mark.asyncio
async def test_create_or_get_transfer_is_tenant_scoped_and_detects_hash_conflict(store) -> None:
    from src.transfer.errors import IdempotencyConflict
    from src.transfer.models import ConnectorDefinition, MappingDefinition, TransferRequest

    await store.save_connector(
        ConnectorDefinition.model_validate(sample_connector_definition()),
        tenant_id="tenant-a",
    )
    await store.save_mapping(
        MappingDefinition.model_validate(sample_mapping()),
        tenant_id="tenant-a",
    )
    await store.save_connector(
        ConnectorDefinition.model_validate(sample_connector_definition()),
        tenant_id="tenant-b",
    )
    await store.save_mapping(
        MappingDefinition.model_validate(sample_mapping()),
        tenant_id="tenant-b",
    )
    request = TransferRequest.model_validate(sample_transfer_request())
    created = await store.create_or_get_transfer(
        tenant_id="tenant-a",
        idempotency_key="idem-1",
        request=request,
        correlation_id="corr-001",
        connector_version=7,
        mapping_version=3,
    )

    replay = await store.create_or_get_transfer(
        tenant_id="tenant-a",
        idempotency_key="idem-1",
        request=request,
        correlation_id="corr-should-not-win",
        connector_version=99,
        mapping_version=99,
    )

    other_tenant_request = TransferRequest.model_validate(sample_transfer_request())
    other_tenant_request.metadata.tenant_id = "tenant-b"
    other_tenant_request.metadata.correlation_id = "corr-tenant-b"
    created_other_tenant = await store.create_or_get_transfer(
        tenant_id="tenant-b",
        idempotency_key="idem-1",
        request=other_tenant_request,
        correlation_id="corr-tenant-b",
        connector_version=7,
        mapping_version=3,
    )

    conflicting_payload = sample_transfer_request()
    conflicting_payload["ocr"]["fields"]["invoice_number"]["value"] = "INV-CHANGED"
    with pytest.raises(IdempotencyConflict):
        await store.create_or_get_transfer(
            tenant_id="tenant-a",
            idempotency_key="idem-1",
            request=TransferRequest.model_validate(conflicting_payload),
            correlation_id="corr-001",
            connector_version=7,
            mapping_version=3,
        )

    assert created.idempotent_replay is False
    assert replay.idempotent_replay is True
    assert replay.record.transfer_id == created.record.transfer_id
    assert replay.record.connector_version == 7
    assert replay.record.mapping_version == 3
    assert created.record.connector_id == "connector-test"
    assert created.record.mapping_id == "mapping-test"
    assert created.record.correlation_id == "corr-001"
    assert created.record.document_id == "doc-001"
    assert created.record.document_type == "invoice"
    assert created_other_tenant.record.transfer_id != created.record.transfer_id


@pytest.mark.asyncio
async def test_claim_cancel_and_recover_follow_required_state_machine(store) -> None:
    from src.transfer.models import (
        ConnectorDefinition,
        MappingDefinition,
        TransferRequest,
        TransferStatus,
    )

    await store.save_connector(
        ConnectorDefinition.model_validate(sample_connector_definition()),
        tenant_id="tenant-a",
    )
    await store.save_mapping(
        MappingDefinition.model_validate(sample_mapping()),
        tenant_id="tenant-a",
    )

    accepted = await store.create_or_get_transfer(
        tenant_id="tenant-a",
        idempotency_key="claim-accepted",
        request=TransferRequest.model_validate(sample_transfer_request()),
        correlation_id="corr-claim",
        connector_version=7,
        mapping_version=3,
    )
    queued_payload = sample_transfer_request()
    queued_payload["document"]["document_id"] = "doc-queued"
    queued_payload["document"]["content"]["storage_ref"] = "object://documents/doc-queued"
    queued_payload["ocr"]["text_ref"] = "object://ocr-text/doc-queued"
    queued_payload["metadata"]["correlation_id"] = "corr-queued"
    queued = await store.create_or_get_transfer(
        tenant_id="tenant-a",
        idempotency_key="claim-queued",
        request=TransferRequest.model_validate(queued_payload),
        correlation_id="corr-queued",
        connector_version=7,
        mapping_version=3,
    )
    await store.transition(
        "tenant-a",
        queued.record.transfer_id,
        TransferStatus.ACCEPTED,
        TransferStatus.VALIDATING,
        {"reason": "validation started"},
    )
    await store.transition(
        "tenant-a",
        queued.record.transfer_id,
        TransferStatus.VALIDATING,
        TransferStatus.QUEUED,
        {"reason": "validated"},
    )

    claimed_validate = await store.claim_due_transfer(datetime(2026, 9, 18, tzinfo=UTC))
    claimed_deliver = await store.claim_due_transfer(datetime(2026, 9, 18, tzinfo=UTC))

    cancel_payload = sample_transfer_request()
    cancel_payload["document"]["document_id"] = "doc-cancel"
    cancel_payload["document"]["content"]["storage_ref"] = "object://documents/doc-cancel"
    cancel_payload["ocr"]["text_ref"] = "object://ocr-text/doc-cancel"
    cancel_payload["metadata"]["correlation_id"] = "corr-cancel"
    cancel_target = await store.create_or_get_transfer(
        tenant_id="tenant-a",
        idempotency_key="claim-cancel",
        request=TransferRequest.model_validate(cancel_payload),
        correlation_id="corr-cancel",
        connector_version=7,
        mapping_version=3,
    )

    assert claimed_validate is not None
    assert claimed_validate.phase == "validate"
    assert claimed_validate.record.transfer_id == accepted.record.transfer_id
    assert claimed_validate.record.status == TransferStatus.VALIDATING
    assert claimed_deliver is not None
    assert claimed_deliver.phase == "deliver"
    assert claimed_deliver.record.status == TransferStatus.DELIVERING

    cancel_accepted = await store.cancel_transfer(
        "tenant-a",
        cancel_target.record.transfer_id,
        {"reason": "user request"},
    )
    cancel_delivering = await store.cancel_transfer(
        "tenant-a",
        claimed_deliver.record.transfer_id,
        {"reason": "user request"},
    )

    assert cancel_accepted.status == TransferStatus.CANCELLED
    assert cancel_delivering.status == TransferStatus.CANCELLATION_REQUESTED

    recovered = await store.recover_inflight()
    recovered_validate = await store.get_transfer("tenant-a", claimed_validate.record.transfer_id)
    recovered_deliver = await store.get_transfer("tenant-a", claimed_deliver.record.transfer_id)

    assert recovered == 0
    assert recovered_validate is not None
    assert recovered_validate.status == TransferStatus.VALIDATING
    assert recovered_deliver is not None
    assert recovered_deliver.status == TransferStatus.CANCELLATION_REQUESTED


@pytest.mark.asyncio
async def test_recover_inflight_moves_delivering_to_reconciliation_required(store) -> None:
    from src.transfer.models import (
        ConnectorDefinition,
        MappingDefinition,
        TransferRequest,
        TransferStatus,
    )

    await store.save_connector(
        ConnectorDefinition.model_validate(sample_connector_definition()),
        tenant_id="tenant-a",
    )
    await store.save_mapping(
        MappingDefinition.model_validate(sample_mapping()),
        tenant_id="tenant-a",
    )
    queued_payload = sample_transfer_request()
    queued_payload["document"]["document_id"] = "doc-recover"
    queued_payload["document"]["content"]["storage_ref"] = "object://documents/doc-recover"
    queued_payload["ocr"]["text_ref"] = "object://ocr-text/doc-recover"
    queued_payload["metadata"]["correlation_id"] = "corr-recover"
    created = await store.create_or_get_transfer(
        tenant_id="tenant-a",
        idempotency_key="recover-1",
        request=TransferRequest.model_validate(queued_payload),
        correlation_id="corr-recover",
        connector_version=7,
        mapping_version=3,
    )
    await store.transition(
        "tenant-a",
        created.record.transfer_id,
        TransferStatus.ACCEPTED,
        TransferStatus.VALIDATING,
        {"reason": "validation started"},
    )
    await store.transition(
        "tenant-a",
        created.record.transfer_id,
        TransferStatus.VALIDATING,
        TransferStatus.QUEUED,
        {"reason": "validated"},
    )
    claimed = await store.claim_due_transfer(datetime(2026, 9, 18, tzinfo=UTC))

    assert claimed is not None
    assert claimed.phase == "deliver"

    recovered = await store.recover_inflight()
    recovered_record = await store.get_transfer("tenant-a", created.record.transfer_id)

    assert recovered == 1
    assert recovered_record is not None
    assert recovered_record.status == TransferStatus.RECONCILIATION_REQUIRED


@pytest.mark.asyncio
async def test_review_corrections_are_encrypted_and_events_are_redacted(encrypted_store) -> None:
    store, db_path = encrypted_store
    from src.transfer.models import ConnectorDefinition, MappingDefinition, TransferRequest

    await store.save_connector(
        ConnectorDefinition.model_validate(sample_connector_definition()),
        tenant_id="tenant-a",
    )
    await store.save_mapping(
        MappingDefinition.model_validate(sample_mapping()),
        tenant_id="tenant-a",
    )
    created = await store.create_or_get_transfer(
        tenant_id="tenant-a",
        idempotency_key="review-1",
        request=TransferRequest.model_validate(sample_transfer_request()),
        correlation_id="corr-review",
        connector_version=7,
        mapping_version=3,
    )

    correction = await store.save_review_correction(
        "tenant-a",
        created.record.transfer_id,
        {"/ocr/fields/invoice_number/value": "INV-UPDATED"},
        "reviewer-1",
        "fix OCR",
    )
    loaded = await store.get_latest_review_correction("tenant-a", created.record.transfer_id)

    assert loaded is not None
    assert loaded.values == correction.values

    connection = sqlite3.connect(db_path)
    try:
        stored_correction_json = connection.execute(
            "SELECT correction_json FROM review_corrections WHERE correction_ref = ?",
            (correction.correction_ref,),
        ).fetchone()[0]
        stored_detail_json = connection.execute(
            "SELECT detail_json FROM transfer_events WHERE correction_ref = ?",
            (correction.correction_ref,),
        ).fetchone()[0]
    finally:
        connection.close()

    assert "INV-UPDATED" not in stored_correction_json
    detail = json.loads(stored_detail_json)
    assert detail == {
        "actor": "reviewer-1",
        "reason": "fix OCR",
        "correction_ref": correction.correction_ref,
    }