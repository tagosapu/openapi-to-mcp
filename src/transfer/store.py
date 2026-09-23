from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import aiosqlite
from cryptography.fernet import Fernet
from pydantic import SecretStr

from .errors import (
    IdempotencyConflict,
    InvalidTransitionError,
    MappingValidationError,
    NotFoundError,
    TenantIsolationError,
)
from .models import (
    ClaimedTransfer,
    ConnectorDefinition,
    CreateTransferResult,
    MappingDefinition,
    ProblemDetail,
    ReviewCorrection,
    TransferFilters,
    TransferRecord,
    TransferRequest,
    TransferResult,
    TransferStatus,
)
from .openapi_contract import public_spec_hash
from .observability import SecretRedactor


ALLOWED_TRANSITIONS = {
    TransferStatus.ACCEPTED: {
        TransferStatus.VALIDATING,
        TransferStatus.CANCELLED,
    },
    TransferStatus.VALIDATING: {
        TransferStatus.ACCEPTED,
        TransferStatus.QUEUED,
        TransferStatus.WAITING_REVIEW,
        TransferStatus.FAILED,
        TransferStatus.CANCELLED,
    },
    TransferStatus.WAITING_REVIEW: {
        TransferStatus.QUEUED,
        TransferStatus.FAILED,
        TransferStatus.CANCELLED,
    },
    TransferStatus.QUEUED: {
        TransferStatus.DELIVERING,
        TransferStatus.CANCELLED,
    },
    TransferStatus.DELIVERING: {
        TransferStatus.WAITING_REVIEW,
        TransferStatus.SUCCEEDED,
        TransferStatus.PARTIALLY_SUCCEEDED,
        TransferStatus.RETRYING,
        TransferStatus.FAILED,
        TransferStatus.RECONCILIATION_REQUIRED,
        TransferStatus.CANCELLATION_REQUESTED,
    },
    TransferStatus.RETRYING: {
        TransferStatus.DELIVERING,
        TransferStatus.FAILED,
        TransferStatus.RECONCILIATION_REQUIRED,
        TransferStatus.CANCELLED,
    },
    TransferStatus.RECONCILIATION_REQUIRED: {
        TransferStatus.SUCCEEDED,
        TransferStatus.FAILED,
        TransferStatus.DELIVERING,
        TransferStatus.QUEUED,
    },
    TransferStatus.CANCELLATION_REQUESTED: {
        TransferStatus.SUCCEEDED,
        TransferStatus.FAILED,
        TransferStatus.RECONCILIATION_REQUIRED,
        TransferStatus.CANCELLED,
    },
    TransferStatus.FAILED: {
        TransferStatus.QUEUED,
    },
}

TERMINAL_STATUSES = {
    TransferStatus.SUCCEEDED,
    TransferStatus.PARTIALLY_SUCCEEDED,
    TransferStatus.FAILED,
    TransferStatus.CANCELLED,
}


class _StaleTransferStateError(RuntimeError):
    pass


class PayloadProtector(Protocol):
    def encrypt(self, plaintext: bytes) -> bytes: ...

    def decrypt(self, ciphertext: bytes) -> bytes: ...


class FernetPayloadProtector:
    def __init__(self, key: SecretStr) -> None:
        self._fernet = Fernet(key.get_secret_value().encode("ascii"))

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return self._fernet.decrypt(ciphertext)


class TransferStore(Protocol):
    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def create_or_get_transfer(
        self,
        tenant_id: str,
        idempotency_key: str,
        request: TransferRequest,
        correlation_id: str,
        connector_version: int,
        mapping_version: int,
    ) -> CreateTransferResult: ...

    async def get_transfer(self, tenant_id: str, transfer_id: str) -> TransferRecord | None: ...

    async def list_transfers(self, tenant_id: str, filters: TransferFilters) -> list[TransferRecord]: ...

    async def record_reconciliation_evidence(
        self, tenant_id: str, transfer_id: str, detail: dict[str, Any]
    ) -> TransferRecord: ...

    async def claim_due_transfer(self, now: datetime) -> ClaimedTransfer | None: ...

    async def transition(
        self,
        tenant_id: str,
        transfer_id: str,
        expected: TransferStatus,
        target: TransferStatus,
        detail: dict[str, Any],
    ) -> TransferRecord: ...

    async def transition_state(
        self,
        tenant_id: str,
        transfer_id: str,
        expected: TransferStatus,
        target: TransferStatus,
        detail: dict[str, Any],
        *,
        next_retry_at: datetime | None = None,
        result: TransferResult | dict[str, Any] | None = None,
        error: ProblemDetail | dict[str, Any] | None = None,
        clear_result: bool = False,
        clear_error: bool = False,
        clear_completed_at: bool = False,
    ) -> TransferRecord: ...

    async def recover_inflight(self) -> int: ...

    async def cancel_transfer(
        self, tenant_id: str, transfer_id: str, detail: dict[str, Any]
    ) -> TransferRecord: ...

    async def save_connector(self, connector: ConnectorDefinition, tenant_id: str) -> None: ...

    async def get_connector(
        self, tenant_id: str, connector_id: str, version: int | None = None
    ) -> ConnectorDefinition | None: ...

    async def save_mapping(self, mapping: MappingDefinition, tenant_id: str) -> None: ...

    async def get_mapping(
        self, tenant_id: str, mapping_id: str, version: int | None = None
    ) -> MappingDefinition | None: ...

    async def list_connectors(self, tenant_id: str) -> list[ConnectorDefinition]: ...

    async def list_mappings(self, tenant_id: str) -> list[MappingDefinition]: ...

    async def save_review_correction(
        self,
        tenant_id: str,
        transfer_id: str,
        values: dict[str, Any],
        actor: str,
        reason: str,
    ) -> ReviewCorrection: ...

    async def save_review_correction_and_transition(
        self,
        tenant_id: str,
        transfer_id: str,
        expected: TransferStatus,
        target: TransferStatus,
        values: dict[str, Any],
        actor: str,
        reason: str,
        detail: dict[str, Any],
        *,
        next_retry_at: datetime | None = None,
        result: TransferResult | dict[str, Any] | None = None,
        error: ProblemDetail | dict[str, Any] | None = None,
        clear_result: bool = False,
        clear_error: bool = False,
        clear_completed_at: bool = False,
    ) -> TransferRecord: ...

    async def get_latest_review_correction(
        self, tenant_id: str, transfer_id: str
    ) -> ReviewCorrection | None: ...

    async def purge_expired_payloads(self, before: datetime) -> int: ...

    async def purge_expired_idempotency_keys(self, before: datetime) -> int: ...

    async def purge_expired_audit_events(
        self, before: datetime, tenant_id: str | None = None
    ) -> int: ...

    async def get_latest_audit_checkpoint(
        self, tenant_id: str | None = None
    ) -> dict[str, Any] | None: ...

    async def verify_audit_chain(self, tenant_id: str | None = None) -> bool: ...


class SqliteTransferStore:
    def __init__(
        self,
        path: Path,
        protector: PayloadProtector | None = None,
        *,
        idempotency_retention: timedelta | None = None,
        allow_legacy_plaintext: bool = False,
    ) -> None:
        if protector is None and not allow_legacy_plaintext:
            raise RuntimeError(
                "payload protector is required unless legacy plaintext is explicitly enabled"
            )
        self._path = path
        self._protector = protector
        self._allow_legacy_plaintext = allow_legacy_plaintext
        self._redactor = SecretRedactor()
        self._connection: aiosqlite.Connection | None = None
        self._idempotency_retention = idempotency_retention or timedelta(hours=24)

    async def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self._path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA foreign_keys = ON")
        await self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS connectors (
                connector_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                config_json TEXT NOT NULL,
                spec_json TEXT NOT NULL,
                spec_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, connector_id, version)
            );

            CREATE TABLE IF NOT EXISTS mappings (
                mapping_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                definition_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, mapping_id, version)
            );

            CREATE TABLE IF NOT EXISTS transfers (
                transfer_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                connector_id TEXT NOT NULL,
                mapping_id TEXT NOT NULL,
                connector_version INTEGER NOT NULL,
                mapping_version INTEGER NOT NULL,
                correlation_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                document_type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                idempotency_expires_at TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                request_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                next_retry_at TEXT,
                result_json TEXT,
                error_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                PRIMARY KEY (tenant_id, transfer_id)
            );

            CREATE TABLE IF NOT EXISTS idempotency_keys (
                tenant_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                transfer_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, idempotency_key),
                FOREIGN KEY (tenant_id, transfer_id) REFERENCES transfers (tenant_id, transfer_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS transfer_events (
                event_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                transfer_id TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT NOT NULL,
                event_type TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                correction_ref TEXT,
                event_order INTEGER NOT NULL,
                event_hash TEXT NOT NULL,
                previous_event_hash TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, event_id),
                UNIQUE (tenant_id, event_order),
                FOREIGN KEY (tenant_id, transfer_id) REFERENCES transfers (tenant_id, transfer_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS review_corrections (
                correction_ref TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                transfer_id TEXT NOT NULL,
                correction_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, correction_ref),
                FOREIGN KEY (tenant_id, transfer_id) REFERENCES transfers (tenant_id, transfer_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS audit_chain_checkpoints (
                checkpoint_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                cutoff_at TEXT NOT NULL,
                deleted_through_hash TEXT,
                checkpoint_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, checkpoint_id)
            );
            """
        )
        await self._connection.commit()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def save_connector(self, connector: ConnectorDefinition, tenant_id: str) -> None:
        connection = self._require_connection()
        now = _utc_now()
        payload_object = connector.model_dump(mode="json")
        cursor = await connection.execute(
            """
            SELECT config_json FROM connectors
            WHERE tenant_id = ? AND connector_id = ? AND version = ?
            LIMIT 1
            """,
            (tenant_id, connector.connector_id, connector.version),
        )
        existing = await cursor.fetchone()
        await cursor.close()
        if existing is not None:
            if _normalize_connector_payload(json.loads(existing["config_json"])) != _normalize_connector_payload(payload_object):
                raise ValueError("CONNECTOR_VERSION_IMMUTABLE")
            return
        payload = _json_dumps(payload_object)
        spec_json = _json_dumps(connector.spec)
        spec_hash = public_spec_hash(connector.spec)
        await connection.execute(
            """
            INSERT INTO connectors (
                connector_id, tenant_id, version, config_json, spec_json, spec_hash, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                connector.connector_id,
                tenant_id,
                connector.version,
                payload,
                spec_json,
                spec_hash,
                "active",
                _isoformat(now),
                _isoformat(now),
            ),
        )
        await connection.commit()

    async def get_connector(
        self, tenant_id: str, connector_id: str, version: int | None = None
    ) -> ConnectorDefinition | None:
        connection = self._require_connection()
        if version is None:
            cursor = await connection.execute(
                """
                SELECT config_json FROM connectors
                WHERE tenant_id = ? AND connector_id = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (tenant_id, connector_id),
            )
        else:
            cursor = await connection.execute(
                """
                SELECT config_json FROM connectors
                WHERE tenant_id = ? AND connector_id = ? AND version = ?
                LIMIT 1
                """,
                (tenant_id, connector_id, version),
            )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return ConnectorDefinition.model_validate(json.loads(row["config_json"]))

    async def save_mapping(self, mapping: MappingDefinition, tenant_id: str) -> None:
        connection = self._require_connection()
        now = _utc_now()
        await connection.execute(
            """
            INSERT INTO mappings (
                mapping_id, tenant_id, version, definition_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tenant_id, mapping_id, version) DO UPDATE SET
                definition_json = excluded.definition_json,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                mapping.mapping_id,
                tenant_id,
                mapping.version,
                _json_dumps(mapping.model_dump(mode="json")),
                mapping.status,
                _isoformat(now),
                _isoformat(now),
            ),
        )
        await connection.commit()

    async def get_mapping(
        self, tenant_id: str, mapping_id: str, version: int | None = None
    ) -> MappingDefinition | None:
        connection = self._require_connection()
        if version is None:
            cursor = await connection.execute(
                """
                SELECT definition_json FROM mappings
                WHERE tenant_id = ? AND mapping_id = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (tenant_id, mapping_id),
            )
        else:
            cursor = await connection.execute(
                """
                SELECT definition_json FROM mappings
                WHERE tenant_id = ? AND mapping_id = ? AND version = ?
                LIMIT 1
                """,
                (tenant_id, mapping_id, version),
            )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return MappingDefinition.model_validate(json.loads(row["definition_json"]))

    async def list_connectors(self, tenant_id: str) -> list[ConnectorDefinition]:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            SELECT c.config_json
            FROM connectors c
            JOIN (
                SELECT tenant_id, connector_id, MAX(version) AS version
                FROM connectors
                WHERE tenant_id = ?
                GROUP BY tenant_id, connector_id
            ) latest
              ON latest.tenant_id = c.tenant_id
             AND latest.connector_id = c.connector_id
             AND latest.version = c.version
            ORDER BY c.connector_id ASC
            """,
            (tenant_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [ConnectorDefinition.model_validate(json.loads(row["config_json"])) for row in rows]

    async def list_mappings(self, tenant_id: str) -> list[MappingDefinition]:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            SELECT m.definition_json
            FROM mappings m
            JOIN (
                SELECT tenant_id, mapping_id, MAX(version) AS version
                FROM mappings
                WHERE tenant_id = ?
                GROUP BY tenant_id, mapping_id
            ) latest
              ON latest.tenant_id = m.tenant_id
             AND latest.mapping_id = m.mapping_id
             AND latest.version = m.version
            ORDER BY m.mapping_id ASC
            """,
            (tenant_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [MappingDefinition.model_validate(json.loads(row["definition_json"])) for row in rows]

    async def create_or_get_transfer(
        self,
        tenant_id: str,
        idempotency_key: str,
        request: TransferRequest,
        correlation_id: str,
        connector_version: int,
        mapping_version: int,
    ) -> CreateTransferResult:
        connection = self._require_connection()
        now = _utc_now()
        request_json = _json_dumps(request.model_dump(mode="json"))
        stored_request_json = self._encode_json_payload(json.loads(request_json))
        request_hash = _sha256_hex(request_json)
        await connection.execute("BEGIN IMMEDIATE")
        try:
            existing_row = await self._get_active_idempotency_row(connection, tenant_id, idempotency_key)
            if existing_row is not None:
                expires_at = _parse_datetime(existing_row["expires_at"])
                if expires_at is not None and expires_at > now:
                    if existing_row["request_hash"] != request_hash:
                        raise IdempotencyConflict(
                            "idempotency key is already bound to a different request"
                        )
                    record = await self.get_transfer(tenant_id, existing_row["transfer_id"])
                    if record is None:
                        raise NotFoundError("transfer not found for idempotency key")
                    await connection.commit()
                    return CreateTransferResult(record=record, idempotent_replay=True)
                delete_cursor = await connection.execute(
                    """
                    DELETE FROM idempotency_keys
                    WHERE tenant_id = ? AND idempotency_key = ? AND expires_at <= ?
                    """,
                    (tenant_id, idempotency_key, _isoformat(now)),
                )
                await delete_cursor.close()

            connector = await self.get_connector(
                tenant_id, request.delivery.connector_id, connector_version
            )
            if connector is None:
                raise NotFoundError("connector version not found for tenant")
            mapping = await self.get_mapping(tenant_id, request.delivery.mapping_id, mapping_version)
            if mapping is None:
                raise NotFoundError("mapping version not found for tenant")
            if request.delivery.deduplication_key_path != mapping.deduplication_key_path:
                raise MappingValidationError("deduplication_key_path does not match mapping")

            transfer_id = str(uuid4())
            idempotency_expires_at = now + self._idempotency_retention
            record = TransferRecord(
                transfer_id=transfer_id,
                tenant_id=tenant_id,
                status=TransferStatus.ACCEPTED,
                request=request,
                connector_id=request.delivery.connector_id,
                mapping_id=request.delivery.mapping_id,
                connector_version=connector_version,
                mapping_version=mapping_version,
                correlation_id=correlation_id,
                document_id=request.document.document_id,
                document_type=request.document.document_type,
                idempotency_key=idempotency_key,
                attempt=0,
                next_retry_at=None,
                created_at=now,
                updated_at=now,
                completed_at=None,
                result=None,
                error=None,
            )

            await connection.execute(
                """
                INSERT INTO transfers (
                    transfer_id, tenant_id, connector_id, mapping_id, connector_version, mapping_version,
                    correlation_id, document_id, document_type, idempotency_key, idempotency_expires_at,
                    request_hash, request_json, status, attempt, next_retry_at, result_json, error_json,
                    created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transfer_id,
                    tenant_id,
                    record.connector_id,
                    record.mapping_id,
                    connector_version,
                    mapping_version,
                    correlation_id,
                    record.document_id,
                    record.document_type,
                    idempotency_key,
                    _isoformat(idempotency_expires_at),
                    request_hash,
                    stored_request_json,
                    record.status.value,
                    record.attempt,
                    None,
                    None,
                    None,
                    _isoformat(now),
                    _isoformat(now),
                    None,
                ),
            )
            await connection.execute(
                """
                INSERT INTO idempotency_keys (
                    tenant_id, idempotency_key, transfer_id, request_hash, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant_id,
                    idempotency_key,
                    transfer_id,
                    request_hash,
                    _isoformat(idempotency_expires_at),
                    _isoformat(now),
                ),
            )
            await self._append_event(
                connection,
                tenant_id=tenant_id,
                transfer_id=transfer_id,
                from_status=None,
                to_status=TransferStatus.ACCEPTED,
                event_type="created",
                detail={"correlation_id": correlation_id},
                created_at=now,
            )
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise
        return CreateTransferResult(record=record, idempotent_replay=False)

    async def get_transfer(self, tenant_id: str, transfer_id: str) -> TransferRecord | None:
        connection = self._require_connection()
        cursor = await connection.execute(
            "SELECT * FROM transfers WHERE tenant_id = ? AND transfer_id = ? LIMIT 1",
            (tenant_id, transfer_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return self._transfer_from_row(row)

    async def list_transfers(self, tenant_id: str, filters: TransferFilters) -> list[TransferRecord]:
        connection = self._require_connection()
        query = ["SELECT * FROM transfers WHERE tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if filters.status is not None:
            query.append("AND status = ?")
            params.append(filters.status.value)
        if filters.connector_id is not None:
            query.append("AND connector_id = ?")
            params.append(filters.connector_id)
        if filters.document_id is not None:
            query.append("AND document_id = ?")
            params.append(filters.document_id)
        if filters.correlation_id is not None:
            query.append("AND correlation_id = ?")
            params.append(filters.correlation_id)
        if filters.created_after is not None:
            query.append("AND created_at > ?")
            params.append(_isoformat(filters.created_after))
        if filters.created_before is not None:
            query.append("AND created_at < ?")
            params.append(_isoformat(filters.created_before))
        if filters.cursor is not None:
            cursor_created_at, cursor_transfer_id = decode_transfer_cursor(filters.cursor)
            query.append(
                "AND (created_at < ? OR (created_at = ? AND transfer_id < ?))"
            )
            params.extend(
                [
                    _isoformat(cursor_created_at),
                    _isoformat(cursor_created_at),
                    cursor_transfer_id,
                ]
            )
        query.append("ORDER BY created_at DESC, transfer_id DESC LIMIT ?")
        params.append(filters.limit + 1)
        cursor = await connection.execute(" ".join(query), tuple(params))
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._transfer_from_row(row) for row in rows]

    async def record_reconciliation_evidence(
        self, tenant_id: str, transfer_id: str, detail: dict[str, Any]
    ) -> TransferRecord:
        connection = self._require_connection()
        await connection.execute("BEGIN IMMEDIATE")
        try:
            row = await self._get_transfer_row_any_tenant(connection, transfer_id)
            if row is None:
                raise NotFoundError("transfer not found")
            if row["tenant_id"] != tenant_id:
                raise TenantIsolationError("transfer belongs to another tenant")
            if TransferStatus(row["status"]) != TransferStatus.RECONCILIATION_REQUIRED:
                raise InvalidTransitionError("reconciliation evidence requires reconciliation_required status")
            now = _utc_now()
            await self._append_event(
                connection,
                tenant_id=tenant_id,
                transfer_id=transfer_id,
                from_status=TransferStatus.RECONCILIATION_REQUIRED,
                to_status=TransferStatus.RECONCILIATION_REQUIRED,
                event_type="reconciliation_evidence",
                detail=_safe_reconciliation_detail(detail),
                created_at=now,
            )
            refreshed = await self.get_transfer(tenant_id, transfer_id)
            if refreshed is None:
                raise NotFoundError("transfer disappeared during reconciliation evidence")
            await connection.commit()
            return refreshed
        except Exception:
            await connection.rollback()
            raise

    async def claim_due_transfer(self, now: datetime) -> ClaimedTransfer | None:
        connection = self._require_connection()
        await connection.execute("BEGIN IMMEDIATE")
        try:
            accepted_cursor = await connection.execute(
                """
                SELECT * FROM transfers
                WHERE status = ?
                ORDER BY created_at ASC, transfer_id ASC
                LIMIT 1
                """,
                (TransferStatus.ACCEPTED.value,),
            )
            accepted_row = await accepted_cursor.fetchone()
            await accepted_cursor.close()
            if accepted_row is not None:
                updated = await self._update_transfer_status(
                    connection,
                    row=accepted_row,
                    target=TransferStatus.VALIDATING,
                    event_type="claim",
                    detail={"phase": "validate"},
                    now=now,
                    increment_attempt=False,
                )
                await connection.commit()
                return ClaimedTransfer(record=updated, phase="validate")

            deliver_cursor = await connection.execute(
                """
                SELECT * FROM transfers
                WHERE status = ?
                   OR (status = ? AND next_retry_at IS NOT NULL AND next_retry_at <= ?)
                ORDER BY COALESCE(next_retry_at, created_at) ASC, created_at ASC, transfer_id ASC
                LIMIT 1
                """,
                (
                    TransferStatus.QUEUED.value,
                    TransferStatus.RETRYING.value,
                    _isoformat(now),
                ),
            )
            deliver_row = await deliver_cursor.fetchone()
            await deliver_cursor.close()
            if deliver_row is not None:
                updated = await self._update_transfer_status(
                    connection,
                    row=deliver_row,
                    target=TransferStatus.DELIVERING,
                    event_type="claim",
                    detail={"phase": "deliver"},
                    now=now,
                    increment_attempt=True,
                    next_retry_at=None,
                )
                await connection.commit()
                return ClaimedTransfer(record=updated, phase="deliver")

            await connection.commit()
            return None
        except Exception:
            await connection.rollback()
            raise

    async def transition(
        self,
        tenant_id: str,
        transfer_id: str,
        expected: TransferStatus,
        target: TransferStatus,
        detail: dict[str, Any],
    ) -> TransferRecord:
        return await self.transition_state(
            tenant_id,
            transfer_id,
            expected,
            target,
            detail,
            next_retry_at=detail.get("next_retry_at"),
            result=detail.get("result"),
            error=detail.get("error"),
        )

    async def transition_state(
        self,
        tenant_id: str,
        transfer_id: str,
        expected: TransferStatus,
        target: TransferStatus,
        detail: dict[str, Any],
        *,
        next_retry_at: datetime | None = None,
        result: TransferResult | dict[str, Any] | None = None,
        error: ProblemDetail | dict[str, Any] | None = None,
        clear_result: bool = False,
        clear_error: bool = False,
        clear_completed_at: bool = False,
    ) -> TransferRecord:
        connection = self._require_connection()
        await connection.execute("BEGIN IMMEDIATE")
        try:
            row = await self._get_transfer_row_any_tenant(connection, transfer_id)
            if row is None:
                raise NotFoundError("transfer not found")
            if row["tenant_id"] != tenant_id:
                raise TenantIsolationError("transfer belongs to another tenant")
            actual = TransferStatus(row["status"])
            if actual != expected:
                raise InvalidTransitionError(
                    f"expected {expected.value}, found {actual.value}"
                )
            if target not in ALLOWED_TRANSITIONS.get(expected, set()):
                raise InvalidTransitionError(
                    f"transition {expected.value} -> {target.value} is not allowed"
                )
            now = _utc_now()
            updated = await self._update_transfer_status(
                connection,
                row=row,
                target=target,
                event_type="transition",
                detail=detail,
                now=now,
                increment_attempt=False,
                next_retry_at=next_retry_at,
                result=result,
                error=error,
                clear_result=clear_result,
                clear_error=clear_error,
                clear_completed_at=clear_completed_at,
            )
            await connection.commit()
            return updated
        except _StaleTransferStateError as exc:
            await connection.rollback()
            raise InvalidTransitionError("transfer changed state during transition") from exc
        except Exception:
            await connection.rollback()
            raise

    async def recover_inflight(self) -> int:
        connection = self._require_connection()
        await connection.execute("BEGIN IMMEDIATE")
        try:
            rows_cursor = await connection.execute(
                """
                SELECT * FROM transfers
                WHERE status IN (?, ?, ?)
                ORDER BY created_at ASC, transfer_id ASC
                """,
                (
                    TransferStatus.DELIVERING.value,
                    TransferStatus.CANCELLATION_REQUESTED.value,
                    TransferStatus.VALIDATING.value,
                ),
            )
            rows = await rows_cursor.fetchall()
            await rows_cursor.close()
            if not rows:
                await connection.commit()
                return 0
            now = _utc_now()
            recovered = 0
            for row in rows:
                current_status = TransferStatus(row["status"])
                if current_status == TransferStatus.VALIDATING:
                    target = TransferStatus.ACCEPTED
                    detail = {"reason": "startup recovery", "phase": "validate"}
                else:
                    target = TransferStatus.RECONCILIATION_REQUIRED
                    detail = {"reason": "startup recovery", "phase": "deliver"}
                try:
                    await self._update_transfer_status(
                        connection,
                        row=row,
                        target=target,
                        event_type="recover",
                        detail=detail,
                        now=now,
                        increment_attempt=False,
                    )
                except _StaleTransferStateError:
                    continue
                recovered += 1
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise
        return recovered

    async def cancel_transfer(
        self, tenant_id: str, transfer_id: str, detail: dict[str, Any]
    ) -> TransferRecord:
        connection = self._require_connection()
        row = await self._get_transfer_row_any_tenant(connection, transfer_id)
        if row is None:
            raise NotFoundError("transfer not found")
        if row["tenant_id"] != tenant_id:
            raise TenantIsolationError("transfer belongs to another tenant")
        actual = TransferStatus(row["status"])
        if actual in {
            TransferStatus.ACCEPTED,
            TransferStatus.VALIDATING,
            TransferStatus.QUEUED,
            TransferStatus.WAITING_REVIEW,
            TransferStatus.RETRYING,
        }:
            return await self.transition(
                tenant_id,
                transfer_id,
                actual,
                TransferStatus.CANCELLED,
                detail,
            )
        if actual == TransferStatus.DELIVERING:
            return await self.transition(
                tenant_id,
                transfer_id,
                actual,
                TransferStatus.CANCELLATION_REQUESTED,
                detail,
            )
        raise InvalidTransitionError(f"cannot cancel transfer from {actual.value}")

    async def save_review_correction(
        self,
        tenant_id: str,
        transfer_id: str,
        values: dict[str, Any],
        actor: str,
        reason: str,
    ) -> ReviewCorrection:
        connection = self._require_connection()
        row = await self._get_transfer_row_any_tenant(connection, transfer_id)
        if row is None:
            raise NotFoundError("transfer not found")
        if row["tenant_id"] != tenant_id:
            raise TenantIsolationError("transfer belongs to another tenant")
        correction_ref = str(uuid4())
        correction = ReviewCorrection(
            correction_ref=correction_ref,
            values=values,
            actor=actor,
            reason=reason,
        )
        now = _utc_now()
        await connection.execute(
            """
            INSERT INTO review_corrections (
                correction_ref, tenant_id, transfer_id, correction_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                correction_ref,
                tenant_id,
                transfer_id,
                self._encode_correction_payload(values),
                _isoformat(now),
            ),
        )
        await self._append_event(
            connection,
            tenant_id=tenant_id,
            transfer_id=transfer_id,
            from_status=TransferStatus(row["status"]),
            to_status=TransferStatus(row["status"]),
            event_type="review_correction",
            detail={
                "actor": actor,
                "reason": reason,
                "correction_ref": correction_ref,
            },
            correction_ref=correction_ref,
            created_at=now,
        )
        await connection.commit()
        return correction

    async def save_review_correction_and_transition(
        self,
        tenant_id: str,
        transfer_id: str,
        expected: TransferStatus,
        target: TransferStatus,
        values: dict[str, Any],
        actor: str,
        reason: str,
        detail: dict[str, Any],
        *,
        next_retry_at: datetime | None = None,
        result: TransferResult | dict[str, Any] | None = None,
        error: ProblemDetail | dict[str, Any] | None = None,
        clear_result: bool = False,
        clear_error: bool = False,
        clear_completed_at: bool = False,
    ) -> TransferRecord:
        connection = self._require_connection()
        await connection.execute("BEGIN IMMEDIATE")
        try:
            row = await self._get_transfer_row_any_tenant(connection, transfer_id)
            if row is None:
                raise NotFoundError("transfer not found")
            if row["tenant_id"] != tenant_id:
                raise TenantIsolationError("transfer belongs to another tenant")
            actual = TransferStatus(row["status"])
            if actual != expected:
                raise InvalidTransitionError(
                    f"expected {expected.value}, found {actual.value}"
                )
            if target not in ALLOWED_TRANSITIONS.get(expected, set()):
                raise InvalidTransitionError(
                    f"transition {expected.value} -> {target.value} is not allowed"
                )

            correction_ref = str(uuid4())
            correction = ReviewCorrection(
                correction_ref=correction_ref,
                values=values,
                actor=actor,
                reason=reason,
            )
            now = _utc_now()
            await connection.execute(
                """
                INSERT INTO review_corrections (
                    correction_ref, tenant_id, transfer_id, correction_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    correction_ref,
                    tenant_id,
                    transfer_id,
                    self._encode_correction_payload(values),
                    _isoformat(now),
                ),
            )
            await self._append_event(
                connection,
                tenant_id=tenant_id,
                transfer_id=transfer_id,
                from_status=actual,
                to_status=actual,
                event_type="review_correction",
                detail={
                    "actor": actor,
                    "reason": reason,
                    "correction_ref": correction_ref,
                },
                correction_ref=correction.correction_ref,
                created_at=now,
            )
            updated = await self._update_transfer_status(
                connection,
                row=row,
                target=target,
                event_type="transition",
                detail=detail,
                now=now,
                increment_attempt=False,
                next_retry_at=next_retry_at,
                result=result,
                error=error,
                clear_result=clear_result,
                clear_error=clear_error,
                clear_completed_at=clear_completed_at,
            )
            await connection.commit()
            return updated
        except _StaleTransferStateError as exc:
            await connection.rollback()
            raise InvalidTransitionError("transfer changed state during transition") from exc
        except Exception:
            await connection.rollback()
            raise

    async def get_latest_review_correction(
        self, tenant_id: str, transfer_id: str
    ) -> ReviewCorrection | None:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            SELECT correction_ref, correction_json, created_at
            FROM review_corrections
            WHERE tenant_id = ? AND transfer_id = ?
            ORDER BY created_at DESC, correction_ref DESC
            LIMIT 1
            """,
            (tenant_id, transfer_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        values = self._decode_correction_payload(row["correction_json"])
        event_cursor = await connection.execute(
            """
            SELECT detail_json FROM transfer_events
            WHERE tenant_id = ? AND transfer_id = ? AND correction_ref = ?
            ORDER BY created_at DESC, event_id DESC
            LIMIT 1
            """,
            (tenant_id, transfer_id, row["correction_ref"]),
        )
        event_row = await event_cursor.fetchone()
        await event_cursor.close()
        actor = ""
        reason = ""
        if event_row is not None:
            detail = self._decode_event_detail(event_row["detail_json"])
            actor = detail.get("actor", "")
            reason = detail.get("reason", "")
        return ReviewCorrection(
            correction_ref=row["correction_ref"],
            values=values,
            actor=actor,
            reason=reason,
        )

    async def purge_expired_payloads(self, before: datetime) -> int:
        connection = self._require_connection()
        await connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = await connection.execute(
                """
                SELECT tenant_id, transfer_id
                FROM transfers
                WHERE status IN (?, ?, ?, ?)
                  AND completed_at IS NOT NULL
                  AND completed_at < ?
                """,
                tuple(status.value for status in TERMINAL_STATUSES) + (_isoformat(before),),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                tombstone = self._encode_json_payload({"payload_purged": True})
                await connection.execute(
                    """
                    UPDATE transfers
                    SET request_json = ?, result_json = NULL, error_json = NULL
                    WHERE tenant_id = ? AND transfer_id = ?
                    """,
                    (tombstone, row["tenant_id"], row["transfer_id"]),
                )
                await connection.execute(
                    """
                    UPDATE review_corrections
                    SET correction_json = ?
                    WHERE tenant_id = ? AND transfer_id = ?
                    """,
                    (tombstone, row["tenant_id"], row["transfer_id"]),
                )
            await connection.commit()
            return len(rows)
        except Exception:
            await connection.rollback()
            raise

    async def purge_expired_idempotency_keys(self, before: datetime) -> int:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            SELECT tenant_id, idempotency_key
            FROM idempotency_keys
            WHERE expires_at < ?
            """,
            (_isoformat(before),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        await connection.execute(
            "DELETE FROM idempotency_keys WHERE expires_at < ?",
            (_isoformat(before),),
        )
        await connection.commit()
        return len(rows)

    async def purge_expired_audit_events(
        self, before: datetime, tenant_id: str | None = None
    ) -> int:
        connection = self._require_connection()
        tenants = [tenant_id] if tenant_id is not None else await self._list_event_tenants(connection)
        total_deleted = 0
        for tenant in tenants:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    SELECT * FROM transfer_events
                    WHERE tenant_id = ? AND created_at < ?
                    ORDER BY created_at ASC, event_order ASC
                    """,
                    (tenant, _isoformat(before)),
                )
                rows = await cursor.fetchall()
                await cursor.close()
                if not rows:
                    await connection.commit()
                    continue
                last_hash = rows[-1]["event_hash"]
                previous_checkpoint = await self._latest_checkpoint_hash(connection, tenant)
                checkpoint_id = str(uuid4())
                created_at = _utc_now()
                checkpoint_hash = _sha256_hex(
                    _json_dumps(
                        {
                            "tenant_id": tenant,
                            "cutoff_at": _isoformat(before),
                            "deleted_through_hash": last_hash,
                            "previous_checkpoint_hash": previous_checkpoint,
                        }
                    )
                )
                await connection.execute(
                    """
                    INSERT INTO audit_chain_checkpoints (
                        checkpoint_id, tenant_id, cutoff_at, deleted_through_hash, checkpoint_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint_id,
                        tenant,
                        _isoformat(before),
                        last_hash,
                        checkpoint_hash,
                        _isoformat(created_at),
                    ),
                )
                await connection.execute(
                    "DELETE FROM transfer_events WHERE tenant_id = ? AND created_at < ?",
                    (tenant, _isoformat(before)),
                )
                await self._reanchor_remaining_events(connection, tenant, checkpoint_hash)
                await connection.commit()
                total_deleted += len(rows)
            except Exception:
                await connection.rollback()
                raise
        return total_deleted

    async def get_latest_audit_checkpoint(
        self, tenant_id: str | None = None
    ) -> dict[str, Any] | None:
        connection = self._require_connection()
        if tenant_id is None:
            cursor = await connection.execute(
                """
                SELECT * FROM audit_chain_checkpoints
                ORDER BY created_at DESC, checkpoint_id DESC
                LIMIT 1
                """
            )
        else:
            cursor = await connection.execute(
                """
                SELECT * FROM audit_chain_checkpoints
                WHERE tenant_id = ?
                ORDER BY created_at DESC, checkpoint_id DESC
                LIMIT 1
                """,
                (tenant_id,),
            )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return dict(row)

    async def verify_audit_chain(self, tenant_id: str | None = None) -> bool:
        connection = self._require_connection()
        tenants = (
            [tenant_id]
            if tenant_id is not None
            else await self._list_audit_tenants(connection)
        )
        for tenant in tenants:
            cursor = await connection.execute(
                """
                SELECT * FROM transfer_events
                WHERE tenant_id = ?
                ORDER BY event_order ASC
                """,
                (tenant,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            if not rows:
                continue
            previous_hash = await self._latest_checkpoint_hash(connection, tenant)
            for row in rows:
                if row["previous_event_hash"] != previous_hash:
                    return False
                expected_hash = _event_hash(
                    previous_hash=previous_hash,
                    event_order=int(row["event_order"]),
                    event_id=row["event_id"],
                    tenant_id=row["tenant_id"],
                    transfer_id=row["transfer_id"],
                    from_status=row["from_status"] or "",
                    to_status=row["to_status"],
                    event_type=row["event_type"],
                    detail_json=row["detail_json"],
                    created_at=row["created_at"],
                )
                if row["event_hash"] != expected_hash:
                    return False
                previous_hash = row["event_hash"]
        return True

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("store is not initialized")
        return self._connection

    async def _get_transfer_row_any_tenant(
        self, connection: aiosqlite.Connection, transfer_id: str
    ) -> aiosqlite.Row | None:
        cursor = await connection.execute(
            "SELECT * FROM transfers WHERE transfer_id = ? LIMIT 1",
            (transfer_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def _update_transfer_status(
        self,
        connection: aiosqlite.Connection,
        row: aiosqlite.Row,
        target: TransferStatus,
        event_type: str,
        detail: dict[str, Any],
        now: datetime,
        increment_attempt: bool,
        next_retry_at: datetime | str | None = None,
        result: TransferResult | dict[str, Any] | None = None,
        error: ProblemDetail | dict[str, Any] | None = None,
        clear_result: bool = False,
        clear_error: bool = False,
        clear_completed_at: bool = False,
    ) -> TransferRecord:
        current_status = TransferStatus(row["status"])
        attempt = int(row["attempt"]) + (1 if increment_attempt else 0)
        next_retry_value = row["next_retry_at"]
        if next_retry_at is not None:
            next_retry_value = (
                _isoformat(next_retry_at)
                if isinstance(next_retry_at, datetime)
                else str(next_retry_at)
            )
        elif target != TransferStatus.RETRYING:
            next_retry_value = None

        existing_result = self._decode_json_payload(row["result_json"]) if row["result_json"] else None
        existing_error = self._decode_json_payload(row["error_json"]) if row["error_json"] else None
        if clear_result:
            existing_result = None
        if clear_error:
            existing_error = None
        if result is not None:
            result_model = (
                result if isinstance(result, TransferResult) else TransferResult.model_validate(result)
            )
            existing_result = result_model.model_dump(mode="json")
        if error is not None:
            error_model = (
                error if isinstance(error, ProblemDetail) else ProblemDetail.model_validate(error)
            )
            existing_error = error_model.model_dump(mode="json")

        completed_at = row["completed_at"]
        if clear_completed_at:
            completed_at = None
        elif target in TERMINAL_STATUSES:
            completed_at = _isoformat(now)

        cursor = await connection.execute(
            """
            UPDATE transfers
            SET status = ?, attempt = ?, next_retry_at = ?, result_json = ?, error_json = ?, updated_at = ?, completed_at = ?
            WHERE tenant_id = ? AND transfer_id = ? AND status = ?
            """,
            (
                target.value,
                attempt,
                next_retry_value,
                self._encode_json_payload(existing_result) if existing_result is not None else None,
                self._encode_json_payload(existing_error) if existing_error is not None else None,
                _isoformat(now),
                completed_at,
                row["tenant_id"],
                row["transfer_id"],
                current_status.value,
            ),
        )
        updated_count = cursor.rowcount
        await cursor.close()
        if updated_count != 1:
            raise _StaleTransferStateError(
                f"transfer {row['transfer_id']} is no longer in {current_status.value}"
            )
        await self._append_event(
            connection,
            tenant_id=row["tenant_id"],
            transfer_id=row["transfer_id"],
            from_status=current_status,
            to_status=target,
            event_type=event_type,
            detail=detail,
            created_at=now,
        )
        refreshed = await self.get_transfer(row["tenant_id"], row["transfer_id"])
        if refreshed is None:
            raise NotFoundError("transfer disappeared during update")
        return refreshed

    async def _append_event(
        self,
        connection: aiosqlite.Connection,
        tenant_id: str,
        transfer_id: str,
        from_status: TransferStatus | None,
        to_status: TransferStatus,
        event_type: str,
        detail: dict[str, Any],
        created_at: datetime,
        correction_ref: str | None = None,
    ) -> None:
        event_order = await self._next_event_order(connection, tenant_id)
        previous_hash = await self._latest_event_hash(connection, tenant_id)
        event_id = str(uuid4())
        detail_json = self._encode_event_detail(detail)
        event_hash = _event_hash(
            previous_hash=previous_hash,
            event_order=event_order,
            event_id=event_id,
            tenant_id=tenant_id,
            transfer_id=transfer_id,
            from_status=from_status.value if from_status is not None else "",
            to_status=to_status.value,
            event_type=event_type,
            detail_json=detail_json,
            created_at=_isoformat(created_at),
        )
        await connection.execute(
            """
            INSERT INTO transfer_events (
                event_id, tenant_id, transfer_id, from_status, to_status, event_type,
                detail_json, correction_ref, event_order, event_hash, previous_event_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                tenant_id,
                transfer_id,
                from_status.value if from_status is not None else None,
                to_status.value,
                event_type,
                detail_json,
                correction_ref,
                event_order,
                event_hash,
                previous_hash,
                _isoformat(created_at),
            ),
        )

    async def _reanchor_remaining_events(
        self,
        connection: aiosqlite.Connection,
        tenant_id: str,
        checkpoint_hash: str,
    ) -> None:
        cursor = await connection.execute(
            """
            SELECT * FROM transfer_events
            WHERE tenant_id = ?
            ORDER BY event_order ASC
            """,
            (tenant_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        previous_hash: str | None = checkpoint_hash
        for row in rows:
            event_hash = _event_hash(
                previous_hash=previous_hash,
                event_order=int(row["event_order"]),
                event_id=row["event_id"],
                tenant_id=row["tenant_id"],
                transfer_id=row["transfer_id"],
                from_status=row["from_status"] or "",
                to_status=row["to_status"],
                event_type=row["event_type"],
                detail_json=row["detail_json"],
                created_at=row["created_at"],
            )
            await connection.execute(
                """
                UPDATE transfer_events
                SET previous_event_hash = ?, event_hash = ?
                WHERE tenant_id = ? AND event_id = ?
                """,
                (previous_hash, event_hash, tenant_id, row["event_id"]),
            )
            previous_hash = event_hash

    async def _get_active_idempotency_row(
        self, connection: aiosqlite.Connection, tenant_id: str, idempotency_key: str
    ) -> aiosqlite.Row | None:
        cursor = await connection.execute(
            """
            SELECT tenant_id, idempotency_key, transfer_id, request_hash, expires_at
            FROM idempotency_keys
            WHERE tenant_id = ? AND idempotency_key = ?
            LIMIT 1
            """,
            (tenant_id, idempotency_key),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def _latest_event_hash(
        self, connection: aiosqlite.Connection, tenant_id: str
    ) -> str | None:
        cursor = await connection.execute(
            """
            SELECT event_hash FROM transfer_events
            WHERE tenant_id = ?
            ORDER BY created_at DESC, event_order DESC
            LIMIT 1
            """,
            (tenant_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is not None:
            return row["event_hash"]
        return await self._latest_checkpoint_hash(connection, tenant_id)

    async def _next_event_order(
        self, connection: aiosqlite.Connection, tenant_id: str
    ) -> int:
        cursor = await connection.execute(
            "SELECT COALESCE(MAX(event_order), 0) AS event_order FROM transfer_events WHERE tenant_id = ?",
            (tenant_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["event_order"]) + 1

    async def _list_event_tenants(self, connection: aiosqlite.Connection) -> list[str]:
        cursor = await connection.execute("SELECT DISTINCT tenant_id FROM transfer_events")
        rows = await cursor.fetchall()
        await cursor.close()
        return [row["tenant_id"] for row in rows]

    async def _list_audit_tenants(self, connection: aiosqlite.Connection) -> list[str]:
        cursor = await connection.execute(
            """
            SELECT tenant_id FROM transfer_events
            UNION
            SELECT tenant_id FROM audit_chain_checkpoints
            ORDER BY tenant_id ASC
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [row["tenant_id"] for row in rows]

    def _transfer_from_row(self, row: aiosqlite.Row) -> TransferRecord:
        request_payload = self._decode_json_payload(row["request_json"])
        if request_payload.get("payload_purged"):
            request_payload = _purged_request_payload(row)
        result_payload = self._decode_json_payload(row["result_json"]) if row["result_json"] else None
        error_payload = self._decode_json_payload(row["error_json"]) if row["error_json"] else None
        result = TransferResult.model_validate(result_payload) if result_payload else None
        error = ProblemDetail.model_validate(error_payload) if error_payload else None
        return TransferRecord(
            transfer_id=row["transfer_id"],
            tenant_id=row["tenant_id"],
            status=TransferStatus(row["status"]),
            request=TransferRequest.model_validate(request_payload),
            connector_id=row["connector_id"],
            mapping_id=row["mapping_id"],
            connector_version=int(row["connector_version"]),
            mapping_version=int(row["mapping_version"]),
            correlation_id=row["correlation_id"],
            document_id=row["document_id"],
            document_type=row["document_type"],
            idempotency_key=row["idempotency_key"],
            attempt=int(row["attempt"]),
            next_retry_at=_parse_datetime(row["next_retry_at"]),
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
            completed_at=_parse_datetime(row["completed_at"]),
            result=result,
            error=error,
        )

    def _encode_correction_payload(self, values: dict[str, Any]) -> str:
        return self._encode_json_payload(values)

    def _decode_correction_payload(self, stored: str) -> dict[str, Any]:
        return self._decode_json_payload(stored)

    def _encode_json_payload(self, value: Any) -> str:
        plaintext = _json_dumps(value).encode("utf-8")
        if self._protector is None:
            return plaintext.decode("utf-8")
        return base64.b64encode(self._protector.encrypt(plaintext)).decode("ascii")

    def _decode_json_payload(self, stored: str) -> dict[str, Any]:
        if self._protector is None:
            return json.loads(stored)
        try:
            ciphertext = base64.b64decode(stored.encode("ascii"))
            plaintext = self._protector.decrypt(ciphertext)
        except Exception:
            if not self._allow_legacy_plaintext or isinstance(self._protector, FernetPayloadProtector):
                raise
            return json.loads(stored)
        return json.loads(plaintext.decode("utf-8"))

    def _encode_event_detail(self, detail: dict[str, Any]) -> str:
        if self._protector is None:
            return _json_dumps(detail)
        return self._encode_json_payload(self._redactor.event_detail(detail))

    def _decode_event_detail(self, stored: str) -> dict[str, Any]:
        if self._protector is None:
            return json.loads(stored)
        return self._decode_json_payload(stored)

    async def _latest_checkpoint_hash(
        self, connection: aiosqlite.Connection, tenant_id: str
    ) -> str | None:
        cursor = await connection.execute(
            """
            SELECT checkpoint_hash FROM audit_chain_checkpoints
            WHERE tenant_id = ?
            ORDER BY created_at DESC, checkpoint_id DESC
            LIMIT 1
            """,
            (tenant_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else row["checkpoint_hash"]


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _isoformat(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default)


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _event_hash(
    *,
    previous_hash: str | None,
    event_order: int,
    event_id: str,
    tenant_id: str,
    transfer_id: str,
    from_status: str,
    to_status: str,
    event_type: str,
    detail_json: str,
    created_at: str,
) -> str:
    return _sha256_hex(
        "|".join(
            [
                previous_hash or "",
                str(event_order),
                event_id,
                tenant_id,
                transfer_id,
                from_status,
                to_status,
                event_type,
                detail_json,
                created_at,
            ]
        )
    )


def encode_transfer_cursor(record: TransferRecord) -> str:
    payload = {
        "v": 1,
        "created_at": _isoformat(record.created_at),
        "transfer_id": record.transfer_id,
    }
    encoded = base64.urlsafe_b64encode(_json_dumps(payload).encode("utf-8"))
    return encoded.rstrip(b"=").decode("ascii")


def decode_transfer_cursor(cursor: str) -> tuple[datetime, str]:
    if not cursor or len(cursor) > 512:
        raise ValueError("CURSOR_INVALID")
    try:
        encoded = cursor.encode("ascii")
        padded = encoded + b"=" * (-len(encoded) % 4)
        payload = json.loads(
            base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
        )
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ValueError
        created_at = _parse_datetime(payload.get("created_at"))
        transfer_id = payload.get("transfer_id")
        if (
            created_at is None
            or created_at.tzinfo is None
            or not isinstance(transfer_id, str)
            or not transfer_id
        ):
            raise ValueError
        return created_at, transfer_id
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("CURSOR_INVALID") from exc


def _safe_reconciliation_detail(detail: dict[str, Any]) -> dict[str, Any]:
    allowed = {"resolution", "target_resource_id", "notes_present", "notes_sha256"}
    return {key: detail[key] for key in allowed if key in detail}


def _purged_request_payload(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "schema_version": "ocr-transfer/v1",
        "document": {
            "document_id": row["document_id"],
            "document_type": row["document_type"],
            "source_system": "redacted",
        },
        "ocr": {
            "fields": {
                "payload_purged": {
                    "value_type": "string",
                    "status": "missing",
                }
            }
        },
        "delivery": {
            "connector_id": row["connector_id"],
            "mapping_id": row["mapping_id"],
            "operation": "upsert",
            "deduplication_key_path": "",
        },
        "metadata": {
            "tenant_id": row["tenant_id"],
            "correlation_id": row["correlation_id"],
            "labels": {},
        },
    }


def _normalize_connector_payload(value: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(value)
    spec = normalized.get("spec")
    if isinstance(spec, dict):
        spec.pop("x-openapi-to-mcp-registration-hosts", None)
    return normalized


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def create_payload_protector(settings: Any, resolver: Any) -> PayloadProtector:
    if settings.data_encryption_key_ref is None:
        raise RuntimeError("data encryption key reference is required")
    key_ref = settings.data_encryption_key_ref
    if isinstance(key_ref, SecretStr):
        resolved_ref = key_ref.get_secret_value()
    else:
        resolved_ref = str(key_ref)
    bundle = await resolver.resolve(resolved_ref)
    key = None
    for candidate in ("value", "key", "fernet_key"):
        if candidate in bundle.values:
            key = bundle.values[candidate].get_secret_value()
            break
    if key is None and len(bundle.values) == 1:
        key = next(iter(bundle.values.values())).get_secret_value()
    if key is None:
        raise RuntimeError("encryption key ref did not resolve to a value")
    try:
        return FernetPayloadProtector(SecretStr(key))
    except Exception as exc:
        raise RuntimeError("invalid Fernet key") from exc