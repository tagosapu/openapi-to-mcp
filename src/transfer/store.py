from __future__ import annotations

import base64
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import aiosqlite

from .errors import IdempotencyConflict, InvalidTransitionError, NotFoundError, TenantIsolationError
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
    },
    TransferStatus.CANCELLATION_REQUESTED: {
        TransferStatus.SUCCEEDED,
        TransferStatus.FAILED,
        TransferStatus.RECONCILIATION_REQUIRED,
        TransferStatus.CANCELLED,
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

    async def claim_due_transfer(self, now: datetime) -> ClaimedTransfer | None: ...

    async def transition(
        self,
        tenant_id: str,
        transfer_id: str,
        expected: TransferStatus,
        target: TransferStatus,
        detail: dict[str, Any],
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

    async def get_latest_review_correction(
        self, tenant_id: str, transfer_id: str
    ) -> ReviewCorrection | None: ...

    async def purge_expired_payloads(self, before: datetime) -> int: ...

    async def purge_expired_audit_events(
        self, before: datetime, tenant_id: str | None = None
    ) -> int: ...

    async def get_latest_audit_checkpoint(
        self, tenant_id: str | None = None
    ) -> dict[str, Any] | None: ...


class SqliteTransferStore:
    def __init__(
        self,
        path: Path,
        protector: PayloadProtector | None = None,
        *,
        idempotency_retention: timedelta | None = None,
    ) -> None:
        self._path = path
        self._protector = protector
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
        payload = _json_dumps(connector.model_dump(mode="json"))
        spec_json = _json_dumps(connector.spec)
        spec_hash = _sha256_hex(spec_json)
        await connection.execute(
            """
            INSERT INTO connectors (
                connector_id, tenant_id, version, config_json, spec_json, spec_hash, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tenant_id, connector_id, version) DO UPDATE SET
                config_json = excluded.config_json,
                spec_json = excluded.spec_json,
                spec_hash = excluded.spec_hash,
                status = excluded.status,
                updated_at = excluded.updated_at
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
                    request_json,
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
        query.append("ORDER BY created_at DESC, transfer_id DESC LIMIT ?")
        params.append(filters.limit)
        cursor = await connection.execute(" ".join(query), tuple(params))
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._transfer_from_row(row) for row in rows]

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
                next_retry_at=detail.get("next_retry_at"),
                result=detail.get("result"),
                error=detail.get("error"),
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
                "SELECT * FROM transfers WHERE status = ? ORDER BY created_at ASC, transfer_id ASC",
                (TransferStatus.DELIVERING.value,),
            )
            rows = await rows_cursor.fetchall()
            await rows_cursor.close()
            if not rows:
                await connection.commit()
                return 0
            now = _utc_now()
            recovered = 0
            for row in rows:
                try:
                    await self._update_transfer_status(
                        connection,
                        row=row,
                        target=TransferStatus.RECONCILIATION_REQUIRED,
                        event_type="recover",
                        detail={"reason": "startup recovery"},
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
            detail = json.loads(event_row["detail_json"])
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
                continue
            last_hash = rows[-1]["event_hash"]
            checkpoint_id = str(uuid4())
            created_at = _utc_now()
            checkpoint_hash = _sha256_hex(
                "|".join([tenant, _isoformat(before), last_hash or "", _isoformat(created_at)])
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
            total_deleted += len(rows)
        await connection.commit()
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

        existing_result = json.loads(row["result_json"]) if row["result_json"] else None
        existing_error = json.loads(row["error_json"]) if row["error_json"] else None
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
        if target in TERMINAL_STATUSES:
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
                _json_dumps(existing_result) if existing_result is not None else None,
                _json_dumps(existing_error) if existing_error is not None else None,
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
        detail_json = _json_dumps(detail)
        event_hash = _sha256_hex(
            "|".join(
                [
                    previous_hash or "",
                    str(event_order),
                    event_id,
                    tenant_id,
                    transfer_id,
                    from_status.value if from_status is not None else "",
                    to_status.value,
                    event_type,
                    detail_json,
                    _isoformat(created_at),
                ]
            )
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
        return None if row is None else row["event_hash"]

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

    def _transfer_from_row(self, row: aiosqlite.Row) -> TransferRecord:
        result = TransferResult.model_validate(json.loads(row["result_json"])) if row["result_json"] else None
        error = ProblemDetail.model_validate(json.loads(row["error_json"])) if row["error_json"] else None
        return TransferRecord(
            transfer_id=row["transfer_id"],
            tenant_id=row["tenant_id"],
            status=TransferStatus(row["status"]),
            request=TransferRequest.model_validate(json.loads(row["request_json"])),
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
        plaintext = _json_dumps(values).encode("utf-8")
        if self._protector is None:
            return plaintext.decode("utf-8")
        ciphertext = self._protector.encrypt(plaintext)
        return base64.b64encode(ciphertext).decode("ascii")

    def _decode_correction_payload(self, stored: str) -> dict[str, Any]:
        if self._protector is None:
            return json.loads(stored)
        plaintext = self._protector.decrypt(base64.b64decode(stored.encode("ascii")))
        return json.loads(plaintext.decode("utf-8"))


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


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _utc_now() -> datetime:
    return datetime.now(UTC)