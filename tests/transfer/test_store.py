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
    transfer_store = SqliteTransferStore(db_path, allow_legacy_plaintext=True)
    await transfer_store.initialize()
    try:
        yield transfer_store
    finally:
        await transfer_store.close()


@pytest.fixture
async def encrypted_store(tmp_path):
    from src.transfer.store import SqliteTransferStore

    db_path = tmp_path / "encrypted.sqlite3"
    transfer_store = SqliteTransferStore(
        db_path, protector=PrefixProtector(), allow_legacy_plaintext=True
    )
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
async def test_create_or_get_transfer_rejects_deduplication_path_mismatch_before_persist(store) -> None:
    from src.transfer.errors import MappingValidationError
    from src.transfer.models import ConnectorDefinition, MappingDefinition, TransferRequest

    await store.save_connector(
        ConnectorDefinition.model_validate(sample_connector_definition()),
        tenant_id="tenant-a",
    )
    mismatched_mapping = sample_mapping()
    mismatched_mapping["deduplication_key_path"] = "/document/document_id"
    await store.save_mapping(
        MappingDefinition.model_validate(mismatched_mapping),
        tenant_id="tenant-a",
    )

    with pytest.raises(MappingValidationError, match="deduplication_key_path"):
        await store.create_or_get_transfer(
            tenant_id="tenant-a",
            idempotency_key="idem-mismatch",
            request=TransferRequest.model_validate(sample_transfer_request()),
            correlation_id="corr-mismatch",
            connector_version=7,
            mapping_version=3,
        )

    connection = store._require_connection()
    cursor = await connection.execute(
        "SELECT COUNT(*) AS count FROM transfers WHERE tenant_id = ?",
        ("tenant-a",),
    )
    row = await cursor.fetchone()
    await cursor.close()

    assert row["count"] == 0


@pytest.mark.asyncio
async def test_create_or_get_transfer_honors_retention_and_allows_reuse_after_expiry(
    tmp_path, monkeypatch
) -> None:
    from src.transfer import store as store_module
    from src.transfer.errors import IdempotencyConflict
    from src.transfer.models import ConnectorDefinition, MappingDefinition, TransferRequest
    from src.transfer.store import SqliteTransferStore

    now = datetime(2026, 9, 18, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(store_module, "_utc_now", lambda: now)
    store = SqliteTransferStore(
        tmp_path / "retention.sqlite3",
        idempotency_retention=timedelta(hours=1),
        allow_legacy_plaintext=True,
    )
    await store.initialize()
    try:
        await store.save_connector(
            ConnectorDefinition.model_validate(sample_connector_definition()),
            tenant_id="tenant-a",
        )
        await store.save_mapping(
            MappingDefinition.model_validate(sample_mapping()),
            tenant_id="tenant-a",
        )
        request = TransferRequest.model_validate(sample_transfer_request())

        created = await store.create_or_get_transfer(
            tenant_id="tenant-a",
            idempotency_key="idem-retention",
            request=request,
            correlation_id="corr-retention",
            connector_version=7,
            mapping_version=3,
        )

        now = now + timedelta(minutes=30)
        replay = await store.create_or_get_transfer(
            tenant_id="tenant-a",
            idempotency_key="idem-retention",
            request=request,
            correlation_id="corr-retention-replay",
            connector_version=8,
            mapping_version=4,
        )

        conflicting_payload = sample_transfer_request()
        conflicting_payload["ocr"]["fields"]["invoice_number"]["value"] = "INV-CONFLICT"
        with pytest.raises(IdempotencyConflict):
            await store.create_or_get_transfer(
                tenant_id="tenant-a",
                idempotency_key="idem-retention",
                request=TransferRequest.model_validate(conflicting_payload),
                correlation_id="corr-retention-conflict",
                connector_version=7,
                mapping_version=3,
            )

        now = now + timedelta(hours=2)
        recreated = await store.create_or_get_transfer(
            tenant_id="tenant-a",
            idempotency_key="idem-retention",
            request=request,
            correlation_id="corr-retention-new",
            connector_version=7,
            mapping_version=3,
        )

        connection = store._require_connection()
        cursor = await connection.execute(
            "SELECT idempotency_expires_at FROM transfers WHERE tenant_id = ? AND transfer_id = ?",
            ("tenant-a", created.record.transfer_id),
        )
        row = await cursor.fetchone()
        await cursor.close()

        assert created.idempotent_replay is False
        assert replay.idempotent_replay is True
        assert replay.record.transfer_id == created.record.transfer_id
        assert recreated.idempotent_replay is False
        assert recreated.record.transfer_id != created.record.transfer_id
        assert row["idempotency_expires_at"] == "2026-09-18T01:00:00Z"
    finally:
        await store.close()


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

    assert recovered == 2
    assert recovered_validate is not None
    assert recovered_validate.status == TransferStatus.ACCEPTED
    assert recovered_deliver is not None
    assert recovered_deliver.status == TransferStatus.RECONCILIATION_REQUIRED


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
async def test_transition_rejects_stale_update_without_appending_phantom_event(
    store, monkeypatch
) -> None:
    from src.transfer.errors import InvalidTransitionError
    from src.transfer.models import ConnectorDefinition, MappingDefinition, TransferRequest, TransferStatus

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
        idempotency_key="race-transition",
        request=TransferRequest.model_validate(sample_transfer_request()),
        correlation_id="corr-race-transition",
        connector_version=7,
        mapping_version=3,
    )
    connection = store._require_connection()
    before_count = (
        await (await connection.execute("SELECT COUNT(*) FROM transfer_events WHERE tenant_id = ?", ("tenant-a",))).fetchone()
    )[0]
    original = store._update_transfer_status

    async def competing_update(*args, **kwargs):
        await connection.execute(
            "UPDATE transfers SET status = ?, updated_at = ? WHERE tenant_id = ? AND transfer_id = ?",
            (
                TransferStatus.QUEUED.value,
                "2026-09-18T00:00:00Z",
                "tenant-a",
                created.record.transfer_id,
            ),
        )
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "_update_transfer_status", competing_update)

    with pytest.raises(InvalidTransitionError):
        await store.transition(
            "tenant-a",
            created.record.transfer_id,
            TransferStatus.ACCEPTED,
            TransferStatus.VALIDATING,
            {"reason": "race"},
        )

    after_count = (
        await (await connection.execute("SELECT COUNT(*) FROM transfer_events WHERE tenant_id = ?", ("tenant-a",))).fetchone()
    )[0]
    refreshed = await store.get_transfer("tenant-a", created.record.transfer_id)

    assert after_count == before_count
    assert refreshed is not None
    assert refreshed.status == TransferStatus.ACCEPTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_status", "target_status"),
    [
        ("failed", "queued"),
        ("waiting_review", "queued"),
    ],
)
async def test_transition_state_clears_stale_result_error_and_completed_at_when_reactivating(
    store, initial_status: str, target_status: str
) -> None:
    from src.transfer.models import ConnectorDefinition, MappingDefinition, TransferRequest, TransferStatus

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
        idempotency_key=f"reactivate-{initial_status}",
        request=TransferRequest.model_validate(sample_transfer_request()),
        correlation_id=f"corr-reactivate-{initial_status}",
        connector_version=7,
        mapping_version=3,
    )
    transfer_id = created.record.transfer_id
    expected = TransferStatus(initial_status)
    target = TransferStatus(target_status)
    await store.transition(
        "tenant-a",
        transfer_id,
        TransferStatus.ACCEPTED,
        TransferStatus.VALIDATING,
        {"reason": "validation started"},
    )
    if expected == TransferStatus.WAITING_REVIEW:
        await store.transition(
            "tenant-a",
            transfer_id,
            TransferStatus.VALIDATING,
            TransferStatus.WAITING_REVIEW,
            {"reason": "needs review"},
        )
    else:
        await store.transition(
            "tenant-a",
            transfer_id,
            TransferStatus.VALIDATING,
            TransferStatus.QUEUED,
            {"reason": "validated"},
        )
        claimed = await store.claim_due_transfer(datetime(2026, 9, 18, tzinfo=UTC))
        assert claimed is not None
        await store.transition_state(
            "tenant-a",
            transfer_id,
            TransferStatus.DELIVERING,
            TransferStatus.FAILED,
            {"reason": "delivery failed"},
            result={
                "target_resource_id": "stale-target",
                "target_request_id": "stale-request",
                "postcondition_verified": False,
                "completed_at": "2026-09-18T00:00:00Z",
                "response_ref": None,
            },
            error={
                "type": "https://openapi-to-mcp/errors/stale",
                "title": "stale error",
                "status": 422,
                "code": "STALE_ERROR",
                "detail": "stale detail",
                "correlation_id": "corr-reactivate-failed",
                "retryable": False,
                "errors": [],
            },
        )

    connection = store._require_connection()
    await connection.execute(
        """
        UPDATE transfers
        SET result_json = ?, error_json = ?, completed_at = ?
        WHERE tenant_id = ? AND transfer_id = ?
        """,
        (
            json.dumps(
                {
                    "target_resource_id": "stale-target",
                    "target_request_id": "stale-request",
                    "postcondition_verified": False,
                    "completed_at": "2026-09-18T00:00:00Z",
                    "response_ref": None,
                }
            ),
            json.dumps(
                {
                    "type": "https://openapi-to-mcp/errors/stale",
                    "title": "stale error",
                    "status": 422,
                    "code": "STALE_ERROR",
                    "detail": "stale detail",
                    "correlation_id": f"corr-reactivate-{initial_status}",
                    "retryable": False,
                    "errors": [],
                }
            ),
            "2026-09-18T00:00:00Z",
            "tenant-a",
            transfer_id,
        ),
    )
    await connection.commit()

    updated = await store.transition_state(
        "tenant-a",
        transfer_id,
        expected,
        target,
        {"reason": "reactivate"},
        clear_result=True,
        clear_error=True,
        clear_completed_at=True,
    )

    assert updated.status == TransferStatus.QUEUED
    assert updated.result is None
    assert updated.error is None
    assert updated.completed_at is None


@pytest.mark.asyncio
async def test_recover_inflight_skips_stale_rows_without_appending_phantom_event(
    store, monkeypatch
) -> None:
    from src.transfer.models import ConnectorDefinition, MappingDefinition, TransferRequest, TransferStatus

    await store.save_connector(
        ConnectorDefinition.model_validate(sample_connector_definition()),
        tenant_id="tenant-a",
    )
    await store.save_mapping(
        MappingDefinition.model_validate(sample_mapping()),
        tenant_id="tenant-a",
    )
    payload = sample_transfer_request()
    payload["document"]["document_id"] = "doc-race-recover"
    payload["document"]["content"]["storage_ref"] = "object://documents/doc-race-recover"
    payload["ocr"]["text_ref"] = "object://ocr-text/doc-race-recover"
    payload["metadata"]["correlation_id"] = "corr-race-recover"
    created = await store.create_or_get_transfer(
        tenant_id="tenant-a",
        idempotency_key="race-recover",
        request=TransferRequest.model_validate(payload),
        correlation_id="corr-race-recover",
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
    assert claimed.record.status == TransferStatus.DELIVERING

    connection = store._require_connection()
    before_count = (
        await (await connection.execute("SELECT COUNT(*) FROM transfer_events WHERE tenant_id = ?", ("tenant-a",))).fetchone()
    )[0]
    original = store._update_transfer_status

    async def competing_recover(*args, **kwargs):
        await connection.execute(
            "UPDATE transfers SET status = ?, updated_at = ? WHERE tenant_id = ? AND transfer_id = ?",
            (
                TransferStatus.CANCELLATION_REQUESTED.value,
                "2026-09-18T00:00:00Z",
                "tenant-a",
                created.record.transfer_id,
            ),
        )
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "_update_transfer_status", competing_recover)

    recovered = await store.recover_inflight()
    after_count = (
        await (await connection.execute("SELECT COUNT(*) FROM transfer_events WHERE tenant_id = ?", ("tenant-a",))).fetchone()
    )[0]
    refreshed = await store.get_transfer("tenant-a", created.record.transfer_id)

    assert recovered == 0
    assert after_count == before_count
    assert refreshed is not None
    assert refreshed.status == TransferStatus.CANCELLATION_REQUESTED


@pytest.mark.asyncio
async def test_transition_allows_partial_success_as_terminal_delivery_result(store) -> None:
    from src.transfer.models import ConnectorDefinition, MappingDefinition, TransferRequest, TransferStatus

    await store.save_connector(
        ConnectorDefinition.model_validate(sample_connector_definition()),
        tenant_id="tenant-a",
    )
    await store.save_mapping(
        MappingDefinition.model_validate(sample_mapping()),
        tenant_id="tenant-a",
    )
    payload = sample_transfer_request()
    payload["document"]["document_id"] = "doc-partial"
    payload["document"]["content"]["storage_ref"] = "object://documents/doc-partial"
    payload["ocr"]["text_ref"] = "object://ocr-text/doc-partial"
    payload["metadata"]["correlation_id"] = "corr-partial"
    created = await store.create_or_get_transfer(
        tenant_id="tenant-a",
        idempotency_key="partial-1",
        request=TransferRequest.model_validate(payload),
        correlation_id="corr-partial",
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

    updated = await store.transition(
        "tenant-a",
        created.record.transfer_id,
        TransferStatus.DELIVERING,
        TransferStatus.PARTIALLY_SUCCEEDED,
        {
            "result": {
                "target_resource_id": "target-123",
                "target_request_id": "request-123",
                "postcondition_verified": True,
            },
            "line_item_summary": {"succeeded": 1, "failed": 1},
        },
    )

    assert updated.status == TransferStatus.PARTIALLY_SUCCEEDED
    assert updated.completed_at is not None
    assert updated.result is not None
    assert updated.result.target_resource_id == "target-123"


@pytest.mark.asyncio
async def test_audit_chain_uses_stable_order_for_same_timestamp_events(store, monkeypatch) -> None:
    from src.transfer import store as store_module
    from src.transfer.models import ConnectorDefinition, MappingDefinition, TransferRequest, TransferStatus

    fixed_now = datetime(2026, 9, 18, 1, 0, tzinfo=UTC)
    monkeypatch.setattr(store_module, "_utc_now", lambda: fixed_now)
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
        idempotency_key="audit-order",
        request=TransferRequest.model_validate(sample_transfer_request()),
        correlation_id="corr-audit-order",
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

    connection = store._require_connection()
    cursor = await connection.execute(
        "SELECT event_order, event_hash, previous_event_hash FROM transfer_events WHERE tenant_id = ? ORDER BY created_at ASC, event_order ASC",
        ("tenant-a",),
    )
    rows = await cursor.fetchall()
    await cursor.close()

    deleted = await store.purge_expired_audit_events(fixed_now + timedelta(seconds=1), tenant_id="tenant-a")
    checkpoint = await store.get_latest_audit_checkpoint("tenant-a")

    assert [row["event_order"] for row in rows] == [1, 2, 3]
    assert rows[0]["previous_event_hash"] is None
    assert rows[1]["previous_event_hash"] == rows[0]["event_hash"]
    assert rows[2]["previous_event_hash"] == rows[1]["event_hash"]
    assert deleted == 3
    assert checkpoint is not None
    assert checkpoint["deleted_through_hash"] == rows[-1]["event_hash"]


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
    detail = store._decode_event_detail(stored_detail_json)
    assert detail == {
        "actor": "reviewer-1",
        "reason": "fix OCR",
        "correction_ref": correction.correction_ref,
    }