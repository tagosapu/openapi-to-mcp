from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from src.transfer.app import create_app
from src.transfer.errors import PayloadLimitError
from src.transfer.limits import PayloadLimits, RateLimiter
from src.transfer.models import (
    ConnectorDefinition,
    MappingDefinition,
    ProblemDetail,
    TransferRequest,
    TransferResult,
    TransferStatus,
)
from src.transfer.observability import SecretRedactor
from src.transfer.routes import _api_error_from_exception
from src.transfer.store import FernetPayloadProtector, SqliteTransferStore
from tests.transfer.conftest import (
    sample_connector_definition,
    sample_mapping,
    sample_transfer_request,
    settings_factory,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class PrefixProtector:
    def encrypt(self, plaintext: bytes) -> bytes:
        return b"enc:" + plaintext[::-1]

    def decrypt(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"enc:"):
            raise ValueError("invalid ciphertext")
        return ciphertext[4:][::-1]


def _fernet_protector() -> FernetPayloadProtector:
    return FernetPayloadProtector(SecretStr(Fernet.generate_key().decode("ascii")))


async def _seed_transfer(store: SqliteTransferStore, tenant_id: str) -> str:
    await store.save_connector(
        ConnectorDefinition.model_validate(sample_connector_definition()), tenant_id
    )
    await store.save_mapping(
        MappingDefinition.model_validate(sample_mapping()), tenant_id
    )
    payload = sample_transfer_request()
    payload["metadata"]["tenant_id"] = tenant_id
    payload["metadata"]["correlation_id"] = f"corr-{tenant_id}"
    payload["document"]["document_id"] = f"doc-{tenant_id}"
    payload["document"]["content"]["storage_ref"] = f"object://documents/{tenant_id}"
    payload["ocr"]["text_ref"] = f"object://ocr-text/{tenant_id}"
    created = await store.create_or_get_transfer(
        tenant_id=tenant_id,
        idempotency_key=f"idem-{tenant_id}",
        request=TransferRequest.model_validate(payload),
        correlation_id=f"corr-{tenant_id}",
        connector_version=7,
        mapping_version=3,
    )
    return created.record.transfer_id


def test_custom_protector_is_strict_without_legacy_opt_in(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="payload protector"):
        SqliteTransferStore(tmp_path / "missing-protector.sqlite3")

    store = SqliteTransferStore(tmp_path / "strict-custom.sqlite3", protector=PrefixProtector())

    with pytest.raises(ValueError):
        store._decode_json_payload('{"legacy":true}')

    encoded = store._encode_event_detail(
        {"status": "failed", "ocr": {"invoice_number": "INV-SECRET"}}
    )

    assert encoded != '{"ocr":{"invoice_number":"INV-SECRET"},"status":"failed"}'
    assert "INV-SECRET" not in encoded
    assert store._decode_event_detail(encoded) == {
        "ocr": "[REDACTED]",
        "status": "failed",
    }


def test_rate_limiter_returns_retry_after_for_tenant_burst() -> None:
    clock = FakeClock()
    limiter = RateLimiter(requests_per_minute=1, burst=1, clock=clock)

    assert limiter.check("tenant-a", "client-a").allowed is True
    decision = limiter.check("tenant-a", "client-a")

    assert decision.allowed is False
    assert decision.retry_after_seconds >= 1


def test_payload_limits_reject_too_many_lines_and_long_strings() -> None:
    limits = PayloadLimits(max_line_items=1, max_string_length=8)

    with pytest.raises(PayloadLimitError):
        limits.validate(TransferRequest.model_validate(sample_transfer_request(line_items=2)))


def test_payload_limit_error_maps_to_413_problem_details() -> None:
    error = _api_error_from_exception(PayloadLimitError("MAX_STRING_LENGTH_EXCEEDED"))

    assert error.status == 413
    assert error.code == "PAYLOAD_LIMIT_EXCEEDED"


def test_redactor_removes_secret_headers_and_ocr_values() -> None:
    redacted = SecretRedactor().event_detail(
        {
            "headers": {
                "Authorization": "Bearer secret-token",
                "X-Request-ID": "req-1",
            },
            "ocr": {"invoice_number": "INV-0001"},
        }
    )

    encoded = json.dumps(redacted)
    assert "secret-token" not in encoded
    assert "INV-0001" not in encoded
    assert redacted["headers"]["X-Request-ID"] == "req-1"


@pytest.mark.asyncio
async def test_store_encrypts_payloads_and_purges_terminal_payloads(tmp_path: Path) -> None:
    db_path = tmp_path / "encrypted.sqlite3"
    store = SqliteTransferStore(db_path, protector=_fernet_protector())
    await store.initialize()
    try:
        transfer_id = await _seed_transfer(store, "tenant-a")
        await store.transition_state(
            "tenant-a",
            transfer_id,
            TransferStatus.ACCEPTED,
            TransferStatus.CANCELLED,
            {"classification": "cancelled"},
            result=TransferResult(target_resource_id="target-secret"),
            error=ProblemDetail(
                type="https://example.invalid/error",
                title="failure",
                status=422,
                code="VALIDATION_FAILED",
                detail="INV-SECRET",
                retryable=False,
            ),
        )
        await store.save_review_correction(
            "tenant-a",
            transfer_id,
            {"invoice_number": "INV-SECRET"},
            "reviewer",
            "manual correction",
        )

        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT request_json, result_json, error_json FROM transfers WHERE transfer_id = ?",
                (transfer_id,),
            ).fetchone()
            event_detail = connection.execute(
                "SELECT detail_json FROM transfer_events WHERE transfer_id = ? ORDER BY event_order DESC LIMIT 1",
                (transfer_id,),
            ).fetchone()[0]
            correction = connection.execute(
                "SELECT correction_json FROM review_corrections WHERE transfer_id = ?",
                (transfer_id,),
            ).fetchone()[0]

        assert row is not None
        assert "invoice_number" not in " ".join(str(value) for value in row)
        assert "INV-SECRET" not in " ".join(str(value) for value in row)
        assert "INV-SECRET" not in event_detail
        assert "INV-SECRET" not in correction

        purged = await store.purge_expired_payloads(datetime.now(UTC) + timedelta(days=1))

        assert purged == 1
        with sqlite3.connect(db_path) as connection:
            request_blob = connection.execute(
                "SELECT request_json FROM transfers WHERE transfer_id = ?",
                (transfer_id,),
            ).fetchone()[0]
        assert "INV-001" not in request_blob
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_audit_retention_keeps_tenant_specific_checkpoints(tmp_path: Path) -> None:
    store = SqliteTransferStore(tmp_path / "audit.sqlite3", protector=_fernet_protector())
    await store.initialize()
    try:
        await _seed_transfer(store, "tenant-a")
        await _seed_transfer(store, "tenant-b")

        deleted = await store.purge_expired_audit_events(
            datetime.now(UTC) + timedelta(days=1)
        )

        assert deleted >= 2
        checkpoint_a = await store.get_latest_audit_checkpoint("tenant-a")
        checkpoint_b = await store.get_latest_audit_checkpoint("tenant-b")
        assert checkpoint_a is not None
        assert checkpoint_b is not None
        assert checkpoint_a["tenant_id"] == "tenant-a"
        assert checkpoint_b["tenant_id"] == "tenant-b"
        assert checkpoint_a["checkpoint_hash"] != checkpoint_b["checkpoint_hash"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_audit_chain_verifier_checks_checkpoint_anchor_and_event_hash(
    tmp_path: Path,
) -> None:
    store = SqliteTransferStore(tmp_path / "audit-verify.sqlite3", protector=_fernet_protector())
    await store.initialize()
    try:
        transfer_id = await _seed_transfer(store, "tenant-a")
        assert await store.verify_audit_chain("tenant-a") is True

        await store.purge_expired_audit_events(datetime.now(UTC) + timedelta(days=1))
        await store.transition(
            "tenant-a",
            transfer_id,
            TransferStatus.ACCEPTED,
            TransferStatus.VALIDATING,
            {"classification": "validation"},
        )
        assert await store.verify_audit_chain("tenant-a") is True

        connection = store._require_connection()
        await connection.execute(
            "UPDATE transfer_events SET event_hash = ? WHERE tenant_id = ?",
            ("tampered", "tenant-a"),
        )
        await connection.commit()
        assert await store.verify_audit_chain("tenant-a") is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_app_refuses_to_start_without_data_encryption_key(tmp_path: Path) -> None:
    settings = settings_factory(
        database_path=str(tmp_path / "missing-key.sqlite3"),
        data_encryption_key_ref=None,
    )
    app = create_app(settings)

    with pytest.raises(RuntimeError, match="data encryption key"):
        async with app.router.lifespan_context(app):
            pass