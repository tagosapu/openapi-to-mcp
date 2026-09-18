from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import sqlite3
from typing import Any

import pytest

from src.transfer.connector import (
    ErrorClassification,
    OutboundOutcome,
    OutboundRequest,
    ReconciliationContext,
    ReconciliationResult,
    ValidationResult,
)
from src.transfer.mapping import MappingEngine
from src.transfer.models import (
    ConnectorDefinition,
    MappingDefinition,
    MappingIssue,
    OperationSelection,
    OutboundRequestParts,
    TransferRequest,
    TransferResult,
    TransferStatus,
)
from src.transfer.store import SqliteTransferStore
from src.transfer.worker import RetryPolicy, TransferWorker
from tests.transfer.conftest import sample_connector_definition, sample_mapping, sample_transfer_request, settings_factory


FIXED_NOW = datetime(2026, 9, 18, tzinfo=UTC)


class PrefixProtector:
    def encrypt(self, plaintext: bytes) -> bytes:
        return b"enc:" + plaintext[::-1]

    def decrypt(self, ciphertext: bytes) -> bytes:
        assert ciphertext.startswith(b"enc:")
        return ciphertext[4:][::-1]


class FakeConnector:
    def __init__(
        self,
        *,
        operation: OperationSelection | None = None,
        validation_result: ValidationResult | None = None,
        outcome: OutboundOutcome | Exception | None = None,
        classification: ErrorClassification | None = None,
        parsed_result: TransferResult | None = None,
        reconcile_result: ReconciliationResult | None = None,
        on_build_request: Any | None = None,
        on_send: Any | None = None,
        parse_error: Exception | None = None,
    ) -> None:
        self.operation = operation or OperationSelection.model_validate(
            {
                "name": "upsert",
                "operation_id": "upsertInvoice",
                "method": "PUT",
                "path": "/invoices/{invoiceId}",
            }
        )
        self.validation_result = validation_result or ValidationResult(valid=True, issues=[])
        self.outcome = outcome or OutboundOutcome(
            delivery_state="received",
            status_code=201,
            headers={},
            body={"id": "target-001"},
            request_id="req-001",
            elapsed_ms=12,
        )
        self.classification = classification
        self.parsed_result = parsed_result
        self.reconcile_result = reconcile_result or ReconciliationResult(
            state="registered",
            target_resource_id="target-001",
        )
        self.on_build_request = on_build_request
        self.on_send = on_send
        self.parse_error = parse_error
        self.send_calls = 0
        self.validate_payloads: list[TransferRequest] = []
        self.validate_mappings: list[MappingDefinition] = []
        self.build_parts: list[OutboundRequestParts] = []
        self.sent_requests: list[OutboundRequest] = []
        self.reconcile_contexts: list[ReconciliationContext] = []

    async def validate_config(self):
        return None

    async def resolve_operation(self, operation_name: str) -> OperationSelection:
        assert operation_name == self.operation.name
        return self.operation

    async def validate_payload(
        self,
        payload: TransferRequest,
        mapping: MappingDefinition,
        operation: OperationSelection,
    ) -> ValidationResult:
        self.validate_payloads.append(payload.model_copy(deep=True))
        self.validate_mappings.append(mapping.model_copy(deep=True))
        assert operation.operation_id == self.operation.operation_id
        return self.validation_result

    async def build_request(self, parts: OutboundRequestParts) -> OutboundRequest:
        self.build_parts.append(parts.model_copy(deep=True))
        if self.on_build_request is not None:
            await self.on_build_request(parts)
        return OutboundRequest(
            method=parts.method,
            url="https://api.example.com/outbound",
            headers={"Idempotency-Key": parts.idempotency_key},
            json_body=parts.json_body,
        )

    async def send(self, request: OutboundRequest) -> OutboundOutcome:
        self.send_calls += 1
        self.sent_requests.append(request)
        if self.on_send is not None:
            return await self.on_send(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def parse_response(self, outcome: OutboundOutcome) -> TransferResult:
        if self.parse_error is not None:
            raise self.parse_error
        if self.parsed_result is not None:
            return self.parsed_result.model_copy(deep=True)
        target_resource_id = None
        if isinstance(outcome.body, dict):
            target_resource_id = outcome.body.get("id")
        return TransferResult(
            target_resource_id=target_resource_id,
            target_request_id=outcome.request_id,
            postcondition_verified=False,
            completed_at=FIXED_NOW,
            response_ref=None,
        )

    def classify_error(self, outcome: OutboundOutcome | Exception) -> ErrorClassification:
        if self.classification is not None:
            return self.classification
        if isinstance(outcome, Exception):
            return ErrorClassification(code="UNEXPECTED_ERROR", retryable=False, delivery_state="unknown")
        if outcome.delivery_state == "unknown":
            return ErrorClassification(code="DELIVERY_UNKNOWN", retryable=False, delivery_state="unknown")
        if outcome.status_code is not None and outcome.status_code >= 400:
            return ErrorClassification(
                code=f"HTTP_{outcome.status_code}",
                retryable=False,
                delivery_state=outcome.delivery_state,
            )
        return ErrorClassification(code="OK", retryable=False, delivery_state=outcome.delivery_state)

    async def reconcile(self, context: ReconciliationContext) -> ReconciliationResult:
        self.reconcile_contexts.append(context.model_copy(deep=True))
        return self.reconcile_result.model_copy(deep=True)


class FakeRegistry:
    def __init__(self, connectors: dict[tuple[str, str, int], FakeConnector]) -> None:
        self._connectors = connectors
        self.requests: list[tuple[str, str, int | None]] = []

    async def get(self, tenant_id: str, connector_id: str, version: int | None = None) -> FakeConnector:
        self.requests.append((tenant_id, connector_id, version))
        assert version is not None
        return self._connectors[(tenant_id, connector_id, version)]


def _request_payload(suffix: str, *, tenant_id: str = "tenant-a") -> dict[str, Any]:
    payload = sample_transfer_request()
    payload["document"]["document_id"] = f"doc-{suffix}"
    payload["document"]["content"]["storage_ref"] = f"object://documents/doc-{suffix}"
    payload["ocr"]["text_ref"] = f"object://ocr-text/doc-{suffix}"
    payload["ocr"]["fields"]["invoice_number"]["value"] = f"INV-{suffix}"
    payload["metadata"]["tenant_id"] = tenant_id
    payload["metadata"]["correlation_id"] = f"corr-{suffix}"
    return payload


def _connector_definition(*, version: int = 7, operation_id: str = "upsertInvoice") -> ConnectorDefinition:
    payload = sample_connector_definition()
    payload["version"] = version
    payload["operation_bindings"]["upsert"]["operation_id"] = operation_id
    return ConnectorDefinition.model_validate(payload)


def _mapping_definition(
    *,
    version: int = 3,
    operations: list[str] | None = None,
    document_types: list[str] | None = None,
) -> MappingDefinition:
    payload = sample_mapping()
    payload["version"] = version
    if operations is not None:
        payload["operations"] = operations
    if document_types is not None:
        payload["document_types"] = document_types
    return MappingDefinition.model_validate(payload)


async def _save_prereqs(
    store: SqliteTransferStore,
    *,
    tenant_id: str = "tenant-a",
    connectors: list[ConnectorDefinition] | None = None,
    mappings: list[MappingDefinition] | None = None,
) -> None:
    for connector in connectors or [_connector_definition()]:
        await store.save_connector(connector, tenant_id=tenant_id)
    for mapping in mappings or [_mapping_definition()]:
        await store.save_mapping(mapping, tenant_id=tenant_id)


async def _seed_transfer(
    store: SqliteTransferStore,
    *,
    suffix: str,
    status: TransferStatus = TransferStatus.ACCEPTED,
    tenant_id: str = "tenant-a",
    connector_version: int = 7,
    mapping_version: int = 3,
    request_payload: dict[str, Any] | None = None,
    next_retry_at: datetime | None = None,
) -> str:
    request = TransferRequest.model_validate(request_payload or _request_payload(suffix, tenant_id=tenant_id))
    created = await store.create_or_get_transfer(
        tenant_id=tenant_id,
        idempotency_key=f"idem-{suffix}",
        request=request,
        correlation_id=request.metadata.correlation_id,
        connector_version=connector_version,
        mapping_version=mapping_version,
    )
    transfer_id = created.record.transfer_id
    if status == TransferStatus.ACCEPTED:
        return transfer_id

    await store.transition(
        tenant_id,
        transfer_id,
        TransferStatus.ACCEPTED,
        TransferStatus.VALIDATING,
        {"reason": "validation started"},
    )
    if status == TransferStatus.VALIDATING:
        return transfer_id
    if status == TransferStatus.WAITING_REVIEW:
        await store.transition(
            tenant_id,
            transfer_id,
            TransferStatus.VALIDATING,
            TransferStatus.WAITING_REVIEW,
            {"reason": "needs review"},
        )
        return transfer_id

    await store.transition(
        tenant_id,
        transfer_id,
        TransferStatus.VALIDATING,
        TransferStatus.QUEUED,
        {"reason": "validated"},
    )
    if status == TransferStatus.QUEUED:
        return transfer_id

    claimed = await store.claim_due_transfer(FIXED_NOW)
    assert claimed is not None
    assert claimed.record.transfer_id == transfer_id
    if status == TransferStatus.DELIVERING:
        return transfer_id
    if status == TransferStatus.RETRYING:
        await store.transition(
            tenant_id,
            transfer_id,
            TransferStatus.DELIVERING,
            TransferStatus.RETRYING,
            {"reason": "retry", "next_retry_at": next_retry_at or FIXED_NOW},
        )
        return transfer_id
    if status == TransferStatus.CANCELLATION_REQUESTED:
        await store.transition(
            tenant_id,
            transfer_id,
            TransferStatus.DELIVERING,
            TransferStatus.CANCELLATION_REQUESTED,
            {"reason": "cancel"},
        )
        return transfer_id
    if status == TransferStatus.RECONCILIATION_REQUIRED:
        await store.transition(
            tenant_id,
            transfer_id,
            TransferStatus.DELIVERING,
            TransferStatus.RECONCILIATION_REQUIRED,
            {"reason": "unknown"},
        )
        return transfer_id
    raise AssertionError(f"unsupported status {status}")


async def seed_transfer_with_low_confidence_field(store: SqliteTransferStore) -> str:
    payload = _request_payload("low-confidence")
    payload["ocr"]["fields"]["invoice_number"]["confidence"] = 0.32
    await _save_prereqs(store)
    return await _seed_transfer(store, suffix="low-confidence", request_payload=payload)


async def seed_queued_transfer(store: SqliteTransferStore, *, suffix: str = "queued") -> str:
    await _save_prereqs(store)
    return await _seed_transfer(store, suffix=suffix, status=TransferStatus.QUEUED)


async def seed_retrying_transfer(store: SqliteTransferStore, *, suffix: str = "retrying") -> str:
    await _save_prereqs(store)
    return await _seed_transfer(
        store,
        suffix=suffix,
        status=TransferStatus.RETRYING,
        next_retry_at=FIXED_NOW - timedelta(seconds=1),
    )


async def _last_transition_detail(store: SqliteTransferStore, transfer_id: str) -> dict[str, Any]:
    connection = store._require_connection()
    cursor = await connection.execute(
        """
        SELECT detail_json FROM transfer_events
        WHERE transfer_id = ? AND event_type = 'transition'
        ORDER BY event_order DESC
        LIMIT 1
        """,
        (transfer_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    return json.loads(row["detail_json"])


@pytest.fixture
async def store(tmp_path):
    db_path = tmp_path / "worker.sqlite3"
    transfer_store = SqliteTransferStore(db_path)
    await transfer_store.initialize()
    try:
        yield transfer_store
    finally:
        await transfer_store.close()


@pytest.fixture
async def encrypted_store(tmp_path):
    db_path = tmp_path / "worker-encrypted.sqlite3"
    transfer_store = SqliteTransferStore(db_path, protector=PrefixProtector())
    await transfer_store.initialize()
    try:
        yield transfer_store, db_path
    finally:
        await transfer_store.close()


def test_retry_policy_defaults_are_fixed() -> None:
    policy = RetryPolicy.from_settings(settings_factory(max_attempts=99))

    assert policy.max_attempts == 3
    assert policy.initial_delay_seconds == 1
    assert policy.max_delay_seconds == 300
    assert policy.jitter_ratio == 0.2
    assert policy.retry_statuses == {408, 425, 429, 500, 502, 503, 504}


@pytest.mark.asyncio
async def test_worker_moves_low_confidence_payload_to_review_without_http_call(store, monkeypatch) -> None:
    issue = MappingIssue(
        source_path="/ocr/fields/invoice_number",
        target_path="/body/external_id",
        code="LOW_CONFIDENCE",
        message="required field confidence is below threshold",
        retryable=False,
    )
    connector = FakeConnector(validation_result=ValidationResult(valid=False, issues=[issue]))
    registry = FakeRegistry({("tenant-a", "connector-test", 7): connector})
    worker = TransferWorker(store, registry, MappingEngine(), RetryPolicy(jitter_ratio=0.0))
    transfer_id = await seed_transfer_with_low_confidence_field(store)
    monkeypatch.setattr("src.transfer.worker._utc_now", lambda: FIXED_NOW)

    worked = await worker.run_once()

    record = await store.get_transfer("tenant-a", transfer_id)
    assert worked is True
    assert record is not None
    assert record.status == TransferStatus.WAITING_REVIEW
    assert connector.send_calls == 0


@pytest.mark.asyncio
async def test_worker_fails_mapping_operation_mismatch_without_http_call(store, monkeypatch) -> None:
    mapping = _mapping_definition(operations=["create"])
    await _save_prereqs(store, mappings=[mapping])
    connector = FakeConnector()
    registry = FakeRegistry({("tenant-a", "connector-test", 7): connector})
    worker = TransferWorker(store, registry, MappingEngine(), RetryPolicy(jitter_ratio=0.0))
    transfer_id = await _seed_transfer(store, suffix="mapping-fail")
    monkeypatch.setattr("src.transfer.worker._utc_now", lambda: FIXED_NOW)

    await worker.run_once()

    record = await store.get_transfer("tenant-a", transfer_id)
    assert record is not None
    assert record.status == TransferStatus.FAILED
    assert connector.send_calls == 0


@pytest.mark.asyncio
async def test_worker_uses_pinned_connector_and_mapping_versions(store, monkeypatch) -> None:
    await _save_prereqs(
        store,
        connectors=[_connector_definition(version=7), _connector_definition(version=8, operation_id="upsertInvoiceV2")],
        mappings=[_mapping_definition(version=3), _mapping_definition(version=4)],
    )
    pinned_connector = FakeConnector()
    latest_connector = FakeConnector(
        operation=OperationSelection.model_validate(
            {
                "name": "upsert",
                "operation_id": "upsertInvoiceV2",
                "method": "PUT",
                "path": "/v2/invoices/{invoiceId}",
            }
        )
    )
    registry = FakeRegistry(
        {
            ("tenant-a", "connector-test", 7): pinned_connector,
            ("tenant-a", "connector-test", 8): latest_connector,
        }
    )
    worker = TransferWorker(store, registry, MappingEngine(), RetryPolicy(jitter_ratio=0.0))
    transfer_id = await _seed_transfer(
        store,
        suffix="pinned",
        connector_version=7,
        mapping_version=3,
    )
    monkeypatch.setattr("src.transfer.worker._utc_now", lambda: FIXED_NOW)

    await worker.run_once()

    record = await store.get_transfer("tenant-a", transfer_id)
    assert record is not None
    assert record.status == TransferStatus.QUEUED
    assert registry.requests == [("tenant-a", "connector-test", 7)]
    assert pinned_connector.validate_mappings[0].version == 3
    assert latest_connector.validate_payloads == []


@pytest.mark.asyncio
async def test_worker_overlays_review_correction_on_deep_copy_before_delivery(store, monkeypatch) -> None:
    connector = FakeConnector()
    registry = FakeRegistry({("tenant-a", "connector-test", 7): connector})
    worker = TransferWorker(store, registry, MappingEngine(), RetryPolicy(jitter_ratio=0.0))
    transfer_id = await seed_queued_transfer(store, suffix="corrected")
    await store.save_review_correction(
        "tenant-a",
        transfer_id,
        {
            "/ocr/fields/invoice_number/value": "INV-CORRECTED",
            "/ocr/fields/invoice_number/status": "manually_corrected",
        },
        "reviewer@example.com",
        "verified",
    )
    monkeypatch.setattr("src.transfer.worker._utc_now", lambda: FIXED_NOW)

    await worker.run_once()

    record = await store.get_transfer("tenant-a", transfer_id)
    assert record is not None
    assert record.status == TransferStatus.SUCCEEDED
    assert record.request.ocr.fields["invoice_number"].value == "INV-corrected"
    assert connector.build_parts[0].json_body == {"external_id": "INV-CORRECTED"}


@pytest.mark.asyncio
async def test_worker_validates_then_delivers_successfully(store, monkeypatch) -> None:
    await _save_prereqs(store)
    connector = FakeConnector()
    registry = FakeRegistry({("tenant-a", "connector-test", 7): connector})
    worker = TransferWorker(store, registry, MappingEngine(), RetryPolicy(jitter_ratio=0.0))
    transfer_id = await _seed_transfer(store, suffix="success")
    monkeypatch.setattr("src.transfer.worker._utc_now", lambda: FIXED_NOW)

    assert await worker.run_once() is True
    assert await worker.run_once() is True

    record = await store.get_transfer("tenant-a", transfer_id)
    assert record is not None
    assert record.status == TransferStatus.SUCCEEDED
    assert record.result is not None
    assert record.result.target_resource_id == "target-001"
    assert record.result.postcondition_verified is True
    assert connector.send_calls == 1


@pytest.mark.asyncio
async def test_worker_retries_not_sent_retryable_errors_with_retry_after_and_redacted_event(
    store, monkeypatch
) -> None:
    await _save_prereqs(store)
    connector = FakeConnector(
        outcome=OutboundOutcome(
            delivery_state="not_sent",
            status_code=429,
            headers={"Retry-After": "7"},
            body=None,
            request_id="req-429",
            elapsed_ms=11,
        ),
        classification=ErrorClassification(
            code="HTTP_429",
            retryable=True,
            delivery_state="not_sent",
            retry_after_seconds=7,
        ),
    )
    registry = FakeRegistry({("tenant-a", "connector-test", 7): connector})
    worker = TransferWorker(store, registry, MappingEngine(), RetryPolicy(jitter_ratio=0.0))
    transfer_id = await seed_queued_transfer(store, suffix="retry-after")
    monkeypatch.setattr("src.transfer.worker._utc_now", lambda: FIXED_NOW)

    await worker.run_once()

    record = await store.get_transfer("tenant-a", transfer_id)
    detail = await _last_transition_detail(store, transfer_id)
    assert record is not None
    assert record.status == TransferStatus.RETRYING
    assert record.next_retry_at == FIXED_NOW + timedelta(seconds=7)
    assert detail == {
        "attempt": 1,
        "classification": "HTTP_429",
        "duration_ms": 11,
        "request_id": "req-429",
    }


@pytest.mark.asyncio
async def test_worker_stops_retrying_at_max_attempts(store, monkeypatch) -> None:
    await _save_prereqs(store)
    connector = FakeConnector(
        outcome=OutboundOutcome(
            delivery_state="not_sent",
            status_code=503,
            headers={},
            body=None,
            request_id="req-503",
            elapsed_ms=9,
        ),
        classification=ErrorClassification(
            code="HTTP_5XX",
            retryable=True,
            delivery_state="not_sent",
        ),
    )
    registry = FakeRegistry({("tenant-a", "connector-test", 7): connector})
    worker = TransferWorker(
        store,
        registry,
        MappingEngine(),
        RetryPolicy(max_attempts=2, jitter_ratio=0.0),
    )
    transfer_id = await seed_queued_transfer(store, suffix="max-attempts")
    current_now = FIXED_NOW
    monkeypatch.setattr("src.transfer.worker._utc_now", lambda: current_now)

    await worker.run_once()
    current_now = FIXED_NOW + timedelta(seconds=1)
    await worker.run_once()

    record = await store.get_transfer("tenant-a", transfer_id)
    assert record is not None
    assert record.status == TransferStatus.FAILED
    assert record.attempt == 2


@pytest.mark.asyncio
async def test_worker_marks_send_timeout_as_reconciliation_required(store, monkeypatch) -> None:
    await _save_prereqs(store)
    connector = FakeConnector(
        outcome=OutboundOutcome(
            delivery_state="unknown",
            status_code=None,
            headers={},
            body=None,
            request_id=None,
            elapsed_ms=100,
        ),
        classification=ErrorClassification(
            code="DELIVERY_UNKNOWN",
            retryable=False,
            delivery_state="unknown",
        ),
    )
    registry = FakeRegistry({("tenant-a", "connector-test", 7): connector})
    worker = TransferWorker(store, registry, MappingEngine(), RetryPolicy(jitter_ratio=0.0))
    transfer_id = await seed_queued_transfer(store, suffix="timeout")
    monkeypatch.setattr("src.transfer.worker._utc_now", lambda: FIXED_NOW)

    await worker.run_once()

    record = await store.get_transfer("tenant-a", transfer_id)
    assert record is not None
    assert record.status == TransferStatus.RECONCILIATION_REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reconcile_state", "expected_status"),
    [
        ("registered", TransferStatus.SUCCEEDED),
        ("not_registered", TransferStatus.RECONCILIATION_REQUIRED),
        ("unknown", TransferStatus.RECONCILIATION_REQUIRED),
    ],
)
async def test_worker_received_response_requires_registered_postcondition(
    store, monkeypatch, reconcile_state: str, expected_status: TransferStatus
) -> None:
    await _save_prereqs(store)
    connector = FakeConnector(
        outcome=OutboundOutcome(
            delivery_state="received",
            status_code=201,
            headers={},
            body={"id": "target-postcondition"},
            request_id="req-postcondition",
            elapsed_ms=13,
        ),
        reconcile_result=ReconciliationResult(
            state=reconcile_state,
            target_resource_id="target-postcondition" if reconcile_state == "registered" else None,
        ),
    )
    registry = FakeRegistry({("tenant-a", "connector-test", 7): connector})
    worker = TransferWorker(store, registry, MappingEngine(), RetryPolicy(jitter_ratio=0.0))
    transfer_id = await seed_queued_transfer(store, suffix=f"postcondition-{reconcile_state}")
    monkeypatch.setattr("src.transfer.worker._utc_now", lambda: FIXED_NOW)

    await worker.run_once()

    record = await store.get_transfer("tenant-a", transfer_id)
    assert record is not None
    assert record.status == expected_status
    assert connector.reconcile_contexts[0].target_resource_id == "target-postcondition"


@pytest.mark.asyncio
async def test_worker_recovers_inflight_states_on_restart(store) -> None:
    await _save_prereqs(store)
    validating_id = await _seed_transfer(store, suffix="recover-validating", status=TransferStatus.VALIDATING)
    cancelling_id = await _seed_transfer(
        store,
        suffix="recover-cancelling",
        status=TransferStatus.CANCELLATION_REQUESTED,
    )
    delivering_id = await _seed_transfer(store, suffix="recover-delivering", status=TransferStatus.DELIVERING)
    worker = TransferWorker(
        store,
        FakeRegistry({("tenant-a", "connector-test", 7): FakeConnector()}),
        MappingEngine(),
        RetryPolicy(jitter_ratio=0.0),
    )

    await worker.recover_inflight()

    validating = await store.get_transfer("tenant-a", validating_id)
    cancelling = await store.get_transfer("tenant-a", cancelling_id)
    delivering = await store.get_transfer("tenant-a", delivering_id)
    assert validating is not None
    assert validating.status == TransferStatus.ACCEPTED
    assert cancelling is not None
    assert cancelling.status == TransferStatus.RECONCILIATION_REQUIRED
    assert delivering is not None
    assert delivering.status == TransferStatus.RECONCILIATION_REQUIRED


@pytest.mark.asyncio
async def test_worker_review_approve_revalidates_latest_correction(store, monkeypatch) -> None:
    await _save_prereqs(store)
    connector = FakeConnector()
    registry = FakeRegistry({("tenant-a", "connector-test", 7): connector})
    worker = TransferWorker(store, registry, MappingEngine(), RetryPolicy(jitter_ratio=0.0))
    transfer_id = await _seed_transfer(store, suffix="approve", status=TransferStatus.WAITING_REVIEW)
    await store.save_review_correction(
        "tenant-a",
        transfer_id,
        {
            "/ocr/fields/invoice_number/value": "INV-APPROVED",
            "/ocr/fields/invoice_number/status": "manually_corrected",
        },
        "reviewer@example.com",
        "verified",
    )
    monkeypatch.setattr("src.transfer.worker._utc_now", lambda: FIXED_NOW)

    record = await worker.review_transfer("tenant-a", transfer_id, "approve", None, "approved")

    assert record.status == TransferStatus.QUEUED
    assert connector.validate_payloads[0].ocr.fields["invoice_number"].value == "INV-APPROVED"


@pytest.mark.asyncio
async def test_worker_review_correct_saves_encrypted_correction_and_redacts_events(
    encrypted_store, monkeypatch
) -> None:
    store, db_path = encrypted_store
    await _save_prereqs(store)
    connector = FakeConnector()
    registry = FakeRegistry({("tenant-a", "connector-test", 7): connector})
    worker = TransferWorker(store, registry, MappingEngine(), RetryPolicy(jitter_ratio=0.0))
    transfer_id = await _seed_transfer(store, suffix="correct", status=TransferStatus.WAITING_REVIEW)
    monkeypatch.setattr("src.transfer.worker._utc_now", lambda: FIXED_NOW)

    record = await worker.review_transfer(
        "tenant-a",
        transfer_id,
        "correct",
        {
            "/ocr/fields/invoice_number/value": "INV-REVIEWED",
            "/ocr/fields/invoice_number/status": "manually_corrected",
        },
        "fix OCR",
    )
    latest_correction = await store.get_latest_review_correction("tenant-a", transfer_id)

    assert record.status == TransferStatus.QUEUED
    assert latest_correction is not None
    assert latest_correction.values["/ocr/fields/invoice_number/value"] == "INV-REVIEWED"

    connection = sqlite3.connect(db_path)
    try:
        stored_correction_json = connection.execute(
            "SELECT correction_json FROM review_corrections WHERE transfer_id = ?",
            (transfer_id,),
        ).fetchone()[0]
        stored_event_details = [
            row[0]
            for row in connection.execute(
                "SELECT detail_json FROM transfer_events WHERE transfer_id = ? ORDER BY event_order ASC",
                (transfer_id,),
            ).fetchall()
        ]
    finally:
        connection.close()

    assert "INV-REVIEWED" not in stored_correction_json
    assert all("INV-REVIEWED" not in detail for detail in stored_event_details)
    assert any(
        json.loads(detail) == {
            "actor": "system",
            "reason": "fix OCR",
            "correction_ref": latest_correction.correction_ref,
        }
        for detail in stored_event_details
    )


@pytest.mark.asyncio
async def test_worker_review_reject_fails_transfer(store, monkeypatch) -> None:
    await _save_prereqs(store)
    worker = TransferWorker(
        store,
        FakeRegistry({("tenant-a", "connector-test", 7): FakeConnector()}),
        MappingEngine(),
        RetryPolicy(jitter_ratio=0.0),
    )
    transfer_id = await _seed_transfer(store, suffix="reject", status=TransferStatus.WAITING_REVIEW)
    monkeypatch.setattr("src.transfer.worker._utc_now", lambda: FIXED_NOW)

    record = await worker.review_transfer("tenant-a", transfer_id, "reject", None, "not approved")

    assert record.status == TransferStatus.FAILED


@pytest.mark.asyncio
async def test_worker_cancel_transfer_cancels_queued_job(store) -> None:
    await _save_prereqs(store)
    worker = TransferWorker(
        store,
        FakeRegistry({("tenant-a", "connector-test", 7): FakeConnector()}),
        MappingEngine(),
        RetryPolicy(jitter_ratio=0.0),
    )
    transfer_id = await seed_queued_transfer(store, suffix="cancel-route")

    record = await worker.cancel_transfer("tenant-a", transfer_id)

    assert record.status == TransferStatus.CANCELLED


@pytest.mark.asyncio
async def test_worker_cancels_before_send_without_http_call(store, monkeypatch) -> None:
    await _save_prereqs(store)
    transfer_id = await seed_queued_transfer(store, suffix="cancel-before-send")

    async def cancel_during_build(parts: OutboundRequestParts) -> None:
        await store.cancel_transfer("tenant-a", transfer_id, {"reason": "user request"})

    connector = FakeConnector(on_build_request=cancel_during_build)
    registry = FakeRegistry({("tenant-a", "connector-test", 7): connector})
    worker = TransferWorker(store, registry, MappingEngine(), RetryPolicy(jitter_ratio=0.0))
    monkeypatch.setattr("src.transfer.worker._utc_now", lambda: FIXED_NOW)

    await worker.run_once()

    record = await store.get_transfer("tenant-a", transfer_id)
    assert record is not None
    assert record.status == TransferStatus.CANCELLED
    assert connector.send_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        (
            OutboundOutcome(
                delivery_state="unknown",
                status_code=None,
                headers={},
                body=None,
                request_id=None,
                elapsed_ms=18,
            ),
            TransferStatus.RECONCILIATION_REQUIRED,
        ),
        (
            OutboundOutcome(
                delivery_state="received",
                status_code=201,
                headers={},
                body={"id": "target-cancelled-success"},
                request_id="req-cancel-success",
                elapsed_ms=18,
            ),
            TransferStatus.SUCCEEDED,
        ),
    ],
)
async def test_worker_handles_inflight_cancellation_after_send(
    store, monkeypatch, outcome: OutboundOutcome, expected_status: TransferStatus
) -> None:
    await _save_prereqs(store)
    transfer_id = await seed_queued_transfer(store, suffix=expected_status.value)

    async def cancel_while_sending(request: OutboundRequest) -> OutboundOutcome:
        await store.cancel_transfer("tenant-a", transfer_id, {"reason": "user request"})
        return outcome

    connector = FakeConnector(
        on_send=cancel_while_sending,
        reconcile_result=ReconciliationResult(
            state="registered",
            target_resource_id="target-cancelled-success",
        ),
    )
    registry = FakeRegistry({("tenant-a", "connector-test", 7): connector})
    worker = TransferWorker(store, registry, MappingEngine(), RetryPolicy(jitter_ratio=0.0))
    monkeypatch.setattr("src.transfer.worker._utc_now", lambda: FIXED_NOW)

    await worker.run_once()

    record = await store.get_transfer("tenant-a", transfer_id)
    assert record is not None
    assert record.status == expected_status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reconcile_state", "expected_status"),
    [
        ("registered", TransferStatus.SUCCEEDED),
        ("not_registered", TransferStatus.QUEUED),
        ("unknown", TransferStatus.RECONCILIATION_REQUIRED),
    ],
)
async def test_worker_reconcile_transfer_updates_same_record(
    store, reconcile_state: str, expected_status: TransferStatus
) -> None:
    await _save_prereqs(store)
    connector = FakeConnector(
        reconcile_result=ReconciliationResult(
            state=reconcile_state,
            target_resource_id="target-reconciled" if reconcile_state == "registered" else None,
        )
    )
    registry = FakeRegistry({("tenant-a", "connector-test", 7): connector})
    worker = TransferWorker(store, registry, MappingEngine(), RetryPolicy(jitter_ratio=0.0))
    transfer_id = await _seed_transfer(
        store,
        suffix=f"reconcile-{reconcile_state}",
        status=TransferStatus.RECONCILIATION_REQUIRED,
    )

    record = await worker.reconcile_transfer("tenant-a", transfer_id)

    assert record.transfer_id == transfer_id
    assert record.status == expected_status
