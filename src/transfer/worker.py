from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import random
from time import perf_counter
from typing import Any, Literal

from jsonpointer import JsonPointerException, resolve_pointer
from pydantic import BaseModel, Field

from .connector import Connector, ErrorClassification, OutboundOutcome, ReconciliationContext
from .errors import InvalidTransitionError, MappingValidationError, NotFoundError
from .mapping import MappingEngine
from .models import (
    ConnectorDefinition,
    MappingDefinition,
    MappingIssue,
    OperationSelection,
    ProblemDetail,
    ReconciliationEvidence,
    TransferRecord,
    TransferRequest,
    TransferResult,
    TransferStatus,
)
from .observability import TransferObservability
from .settings import TransferSettings as Settings
from .store import TransferStore


_WORKER_POLL_SECONDS = 1.0
_MAINTENANCE_INTERVAL = timedelta(minutes=5)
_REVIEW_ACTOR = "system"
_RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
_REVIEWABLE_CODES = {"LOW_CONFIDENCE"}
_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _LoadedContext:
    record: TransferRecord
    connector_definition: ConnectorDefinition
    mapping: MappingDefinition
    connector: Connector
    effective_payload: TransferRequest
    operation: OperationSelection


class RetryPolicy(BaseModel):
    max_attempts: int = 3
    initial_delay_seconds: float = 1
    max_delay_seconds: float = 300
    jitter_ratio: float = 0.2
    retry_statuses: set[int] = Field(default_factory=lambda: set(_RETRYABLE_STATUS_CODES))

    @classmethod
    def from_settings(cls, settings: Settings) -> "RetryPolicy":
        return cls()


class TransferWorker:
    def __init__(
        self,
        store: TransferStore,
        registry: Any,
        mapping_engine: MappingEngine,
        retry_policy: RetryPolicy,
        observability: TransferObservability | None = None,
        payload_retention_days: int | None = None,
        audit_retention_days: int | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._mapping_engine = mapping_engine
        self._retry_policy = retry_policy
        self._observability = observability or TransferObservability()
        self._payload_retention_days = payload_retention_days
        self._audit_retention_days = audit_retention_days
        self._last_maintenance_at: datetime | None = None
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> bool:
        claim = await self._store.claim_due_transfer(_utc_now())
        if claim is None:
            return False

        phase = "validation" if claim.phase == "validate" else "delivery"
        started = perf_counter()
        observed_status = claim.record.status.value
        try:
            with self._observability.span(
                f"transfer.{phase}",
                status=claim.record.status.value,
                connector_id=claim.record.connector_id,
            ):
                if claim.phase == "validate":
                    await self._handle_validate_claim(claim.record)
                else:
                    await self._handle_deliver_claim(claim.record)
            updated_record = await self._store.get_transfer(
                claim.record.tenant_id, claim.record.transfer_id
            )
            if updated_record is not None:
                observed_status = updated_record.status.value
        finally:
            self._observability.record_event(
                status=observed_status,
                classification=phase,
                connector_id=claim.record.connector_id,
                duration_ms=int((perf_counter() - started) * 1000),
            )
        return True

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        await self.run_maintenance()
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def run_maintenance(self) -> None:
        now = _utc_now()
        if (
            self._last_maintenance_at is not None
            and now - self._last_maintenance_at < _MAINTENANCE_INTERVAL
        ):
            return
        purge_idempotency = getattr(self._store, "purge_expired_idempotency_keys", None)
        if purge_idempotency is not None:
            await purge_idempotency(now)
        if self._payload_retention_days is not None:
            await self._store.purge_expired_payloads(
                now - timedelta(days=self._payload_retention_days)
            )
        if self._audit_retention_days is not None:
            await self._store.purge_expired_audit_events(
                now - timedelta(days=self._audit_retention_days)
            )
        self._last_maintenance_at = now

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def recover_inflight(self) -> None:
        await self._store.recover_inflight()

    async def retry_transfer(self, tenant_id: str, transfer_id: str) -> TransferRecord:
        record = await self._require_record(tenant_id, transfer_id)
        if record.status == TransferStatus.FAILED:
            return await self._store.transition_state(
                tenant_id,
                transfer_id,
                TransferStatus.FAILED,
                TransferStatus.QUEUED,
                {"reason": "manual retry"},
                clear_result=True,
                clear_error=True,
                clear_completed_at=True,
            )
        if record.status == TransferStatus.RECONCILIATION_REQUIRED:
            return await self._store.transition_state(
                tenant_id,
                transfer_id,
                TransferStatus.RECONCILIATION_REQUIRED,
                TransferStatus.QUEUED,
                {"reason": "manual retry"},
                clear_result=True,
                clear_error=True,
                clear_completed_at=True,
            )
        raise InvalidTransitionError(f"cannot retry transfer from {record.status.value}")

    async def cancel_transfer(self, tenant_id: str, transfer_id: str) -> TransferRecord:
        return await self._store.cancel_transfer(
            tenant_id,
            transfer_id,
            {"reason": "manual cancel"},
        )

    async def review_transfer(
        self,
        tenant_id: str,
        transfer_id: str,
        decision: Literal["approve", "correct", "reject"],
        correction: dict[str, Any] | None,
        reason: str | None,
        *,
        actor: str = _REVIEW_ACTOR,
    ) -> TransferRecord:
        record = await self._require_record(tenant_id, transfer_id)
        if record.status != TransferStatus.WAITING_REVIEW:
            raise InvalidTransitionError("review actions require waiting_review status")

        if decision == "reject":
            return await self._store.transition_state(
                tenant_id,
                transfer_id,
                TransferStatus.WAITING_REVIEW,
                TransferStatus.FAILED,
                {
                    "actor": _REVIEW_ACTOR,
                    "actor": actor,
                    "decision": decision,
                    "reason": reason,
                },
                error=_problem_detail(
                    code="REVIEW_REJECTED",
                    title="Transfer rejected during review",
                    detail=reason or "review rejected",
                    retryable=False,
                    correlation_id=record.correlation_id,
                ),
            )

        if decision == "correct":
            if not correction:
                raise MappingValidationError("correction values are required")
            effective_payload = self._mapping_engine.apply_corrections(
                record.request,
                _review_correction_model(correction, reason, actor),
            )
            loaded = await self._load_context(record, override_payload=effective_payload)
            issues = await self._collect_validation_issues(loaded)
            if issues:
                raise MappingValidationError(_issues_summary(issues))
            return await self._store.save_review_correction_and_transition(
                tenant_id,
                transfer_id,
                TransferStatus.WAITING_REVIEW,
                TransferStatus.QUEUED,
                correction,
                actor,
                reason or "manual correction",
                {
                    "actor": actor,
                    "decision": decision,
                    "reason": reason,
                },
                clear_result=True,
                clear_error=True,
                clear_completed_at=True,
            )

        latest_correction = await self._store.get_latest_review_correction(tenant_id, transfer_id)
        effective_payload = self._mapping_engine.apply_corrections(record.request, latest_correction)
        loaded = await self._load_context(record, override_payload=effective_payload)
        issues = await self._collect_validation_issues(loaded)
        if issues:
            raise MappingValidationError(_issues_summary(issues))
        return await self._store.transition_state(
            tenant_id,
            transfer_id,
            TransferStatus.WAITING_REVIEW,
            TransferStatus.QUEUED,
            {
                "actor": actor,
                "decision": decision,
                "reason": reason,
            },
            clear_result=True,
            clear_error=True,
            clear_completed_at=True,
        )

    async def reconcile_transfer(
        self,
        tenant_id: str,
        transfer_id: str,
        evidence: ReconciliationEvidence | None = None,
    ) -> TransferRecord:
        record = await self._require_record(tenant_id, transfer_id)
        if record.status != TransferStatus.RECONCILIATION_REQUIRED:
            raise InvalidTransitionError("reconcile requires reconciliation_required status")

        if evidence is not None:
            return await self._apply_reconciliation_evidence(record, evidence)

        loaded = await self._load_context(record)
        try:
            with self._observability.span(
                "transfer.reconciliation",
                status=record.status.value,
                connector_id=record.connector_id,
            ):
                result = await loaded.connector.reconcile(
                    ReconciliationContext(
                        transfer_id=record.transfer_id,
                        idempotency_key=record.idempotency_key,
                        deduplication_value=_canonical_deduplication_value(
                            loaded.effective_payload,
                            loaded.mapping.deduplication_key_path,
                        ),
                        operation_id=loaded.operation.operation_id,
                        mode="unknown",
                        target_resource_id=_optional_target_resource_id(record.result),
                    )
                )
        finally:
            await _close_connector(loaded.connector)

        if result.state == "registered":
            transfer_result = (record.result or TransferResult()).model_copy(deep=True)
            transfer_result.target_resource_id = result.target_resource_id or transfer_result.target_resource_id
            transfer_result.postcondition_verified = True
            transfer_result.completed_at = _utc_now()
            return await self._store.transition_state(
                tenant_id,
                transfer_id,
                TransferStatus.RECONCILIATION_REQUIRED,
                TransferStatus.SUCCEEDED,
                {"reason": "manual reconciliation"},
                result=transfer_result,
            )
        if result.state == "not_registered":
            return await self._store.transition_state(
                tenant_id,
                transfer_id,
                TransferStatus.RECONCILIATION_REQUIRED,
                TransferStatus.QUEUED,
                {"reason": "manual reconciliation"},
            )
        return record

    async def _apply_reconciliation_evidence(
        self, record: TransferRecord, evidence: ReconciliationEvidence
    ) -> TransferRecord:
        detail = _reconciliation_event_detail(evidence)
        if evidence.resolution == "confirmed_present":
            if evidence.target_resource_id is None or not evidence.target_resource_id.strip():
                raise MappingValidationError("TARGET_RESOURCE_ID_REQUIRED")
            transfer_result = (record.result or TransferResult()).model_copy(deep=True)
            transfer_result.target_resource_id = evidence.target_resource_id
            transfer_result.postcondition_verified = True
            transfer_result.completed_at = _utc_now()
            return await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                TransferStatus.RECONCILIATION_REQUIRED,
                TransferStatus.SUCCEEDED,
                detail,
                result=transfer_result,
                clear_error=True,
            )
        if evidence.resolution == "confirmed_absent":
            return await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                TransferStatus.RECONCILIATION_REQUIRED,
                TransferStatus.QUEUED,
                detail,
                clear_result=True,
                clear_error=True,
                clear_completed_at=True,
            )
        return await self._store.record_reconciliation_evidence(
            record.tenant_id,
            record.transfer_id,
            detail,
        )

    async def _run_loop(self) -> None:
        await self.recover_inflight()
        while not self._stop_event.is_set():
            try:
                await self.run_maintenance()
            except Exception as exc:
                _LOGGER.warning(
                    "transfer maintenance failed",
                    extra={"error_type": type(exc).__name__},
                )
                await self._wait_for_poll_interval()
                continue
            try:
                worked = await self.run_once()
            except Exception as exc:
                _LOGGER.warning(
                    "transfer worker iteration failed",
                    extra={"error_type": type(exc).__name__},
                )
                await self._wait_for_poll_interval()
                continue
            if worked:
                continue
            await self._wait_for_poll_interval()

    async def _wait_for_poll_interval(self) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=_WORKER_POLL_SECONDS)
        except TimeoutError:
            return

    async def _handle_validate_claim(self, record: TransferRecord) -> None:
        try:
            loaded = await self._load_context(record)
            issues = await self._collect_validation_issues(loaded)
            if issues:
                target = (
                    TransferStatus.WAITING_REVIEW
                    if _all_reviewable(issues)
                    else TransferStatus.FAILED
                )
                await self._store.transition_state(
                    record.tenant_id,
                    record.transfer_id,
                    TransferStatus.VALIDATING,
                    target,
                    {
                        "classification": "validation",
                        "issue_count": len(issues),
                    },
                    error=_issues_problem_detail(record, issues),
                )
                return
            await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                TransferStatus.VALIDATING,
                TransferStatus.QUEUED,
                {"classification": "validated"},
            )
        except (MappingValidationError, NotFoundError, ValueError) as exc:
            await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                TransferStatus.VALIDATING,
                TransferStatus.FAILED,
                {"classification": "validation"},
                error=_problem_detail(
                    code=_error_code(exc),
                    title="Transfer validation failed",
                    detail=str(exc),
                    retryable=False,
                    correlation_id=record.correlation_id,
                ),
            )
        finally:
            if "loaded" in locals():
                await _close_connector(loaded.connector)

    async def _handle_deliver_claim(self, record: TransferRecord) -> None:
        outcome: OutboundOutcome | None = None
        classification: ErrorClassification | None = None
        duration_ms = 0
        request_id: str | None = None
        loaded: _LoadedContext | None = None
        send_started = False
        try:
            loaded = await self._load_context(record)
            parts = self._mapping_engine.apply(
                loaded.effective_payload,
                loaded.mapping,
                loaded.operation,
            )
            request = await loaded.connector.build_request(parts)
            current = await self._require_record(record.tenant_id, record.transfer_id)
            if current.status == TransferStatus.CANCELLATION_REQUESTED:
                await self._store.transition_state(
                    record.tenant_id,
                    record.transfer_id,
                    TransferStatus.CANCELLATION_REQUESTED,
                    TransferStatus.CANCELLED,
                    _delivery_event_detail(record.attempt, "CANCELLED", 0, None),
                )
                return
            send_started = True
            outcome = await loaded.connector.send(request)
            classification = loaded.connector.classify_error(outcome)
            duration_ms = outcome.elapsed_ms
            request_id = outcome.request_id
        except Exception as exc:
            if not send_started:
                await self._finalize_pre_send_failure(record, exc)
                return
            if loaded is None:
                loaded = await self._load_context(record)
            classification = loaded.connector.classify_error(exc)
            await self._finalize_delivery_failure(
                loaded,
                classification,
                None,
                duration_ms,
                request_id,
                exception=exc,
            )
            return

        try:
            await self._handle_delivery_outcome(
                loaded,
                outcome,
                classification,
                duration_ms,
                request_id,
            )
        finally:
            await _close_connector(loaded.connector)

    async def _handle_delivery_outcome(
        self,
        loaded: _LoadedContext,
        outcome: OutboundOutcome,
        classification: ErrorClassification,
        duration_ms: int,
        request_id: str | None,
    ) -> None:
        record = loaded.record
        current = await self._require_record(record.tenant_id, record.transfer_id)
        expected = (
            TransferStatus.CANCELLATION_REQUESTED
            if current.status == TransferStatus.CANCELLATION_REQUESTED
            else TransferStatus.DELIVERING
        )
        detail = _delivery_event_detail(record.attempt, classification.code, duration_ms, request_id)

        if expected == TransferStatus.CANCELLATION_REQUESTED and outcome.delivery_state == "not_sent":
            await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                expected,
                TransferStatus.CANCELLED,
                detail,
            )
            return

        if self._should_retry(record.attempt, outcome.status_code, classification):
            if expected == TransferStatus.CANCELLATION_REQUESTED:
                await self._store.transition_state(
                    record.tenant_id,
                    record.transfer_id,
                    expected,
                    TransferStatus.FAILED,
                    detail,
                    error=_problem_detail(
                        code=classification.code,
                        title="Transfer delivery failed",
                        detail=classification.code,
                        retryable=False,
                        correlation_id=record.correlation_id,
                    ),
                )
                return
            delay_seconds = self._retry_delay_seconds(record.attempt, classification)
            await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                expected,
                TransferStatus.RETRYING,
                detail,
                next_retry_at=_utc_now() + timedelta(seconds=delay_seconds),
                error=_problem_detail(
                    code=classification.code,
                    title="Transfer delivery will retry",
                    detail=classification.code,
                    retryable=True,
                    correlation_id=record.correlation_id,
                ),
            )
            return

        if classification.delivery_state == "not_sent":
            target = TransferStatus.CANCELLED if expected == TransferStatus.CANCELLATION_REQUESTED else TransferStatus.FAILED
            await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                expected,
                target,
                detail,
                error=_problem_detail(
                    code=classification.code,
                    title="Transfer delivery failed",
                    detail=classification.code,
                    retryable=False,
                    correlation_id=record.correlation_id,
                ),
            )
            return

        if classification.delivery_state == "unknown":
            await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                expected,
                TransferStatus.RECONCILIATION_REQUIRED,
                detail,
                error=_problem_detail(
                    code=classification.code,
                    title="Transfer delivery outcome is unknown",
                    detail=classification.code,
                    retryable=False,
                    correlation_id=record.correlation_id,
                ),
            )
            return

        if classification.code != "OK":
            target = TransferStatus.RECONCILIATION_REQUIRED if classification.retryable else TransferStatus.FAILED
            await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                expected,
                target,
                detail,
                error=_problem_detail(
                    code=classification.code,
                    title="Transfer delivery failed",
                    detail=classification.code,
                    retryable=False,
                    correlation_id=record.correlation_id,
                ),
            )
            return

        try:
            parsed = loaded.connector.parse_response(outcome)
            binding = loaded.connector_definition.operation_bindings.get(loaded.operation.name)
            if loaded.operation.name == "update" and binding is not None and binding.postcondition is None:
                target_resource_id = _optional_target_resource_id(parsed)
            else:
                target_resource_id = _validated_target_resource_id(parsed)
        except Exception as exc:
            await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                expected,
                TransferStatus.FAILED,
                detail,
                error=_problem_detail(
                    code=_error_code(exc),
                    title="Transfer response parsing failed",
                    detail=str(exc),
                    retryable=False,
                    correlation_id=record.correlation_id,
                ),
            )
            return

        binding = loaded.connector_definition.operation_bindings.get(loaded.operation.name)
        if loaded.operation.name == "update" and binding is not None and binding.postcondition is None:
            parsed.completed_at = _utc_now()
            await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                expected,
                TransferStatus.SUCCEEDED,
                detail,
                result=parsed,
            )
            return

        try:
            reconcile_result = await loaded.connector.reconcile(
                ReconciliationContext(
                    transfer_id=record.transfer_id,
                    idempotency_key=record.idempotency_key,
                    deduplication_value=_canonical_deduplication_value(
                        loaded.effective_payload,
                        loaded.mapping.deduplication_key_path,
                    ),
                    operation_id=loaded.operation.operation_id,
                    mode="postcondition",
                    target_resource_id=target_resource_id,
                )
            )
        except Exception as exc:
            parsed.completed_at = _utc_now()
            await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                expected,
                TransferStatus.RECONCILIATION_REQUIRED,
                detail,
                result=parsed,
                error=_problem_detail(
                    code=_error_code(exc),
                    title="Transfer requires reconciliation",
                    detail=str(exc),
                    retryable=False,
                    correlation_id=record.correlation_id,
                ),
            )
            return

        parsed.target_resource_id = reconcile_result.target_resource_id or target_resource_id
        parsed.postcondition_verified = reconcile_result.state == "registered"
        parsed.completed_at = _utc_now()

        if reconcile_result.state == "registered":
            await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                expected,
                TransferStatus.SUCCEEDED,
                detail,
                result=parsed,
            )
            return

        await self._store.transition_state(
            record.tenant_id,
            record.transfer_id,
            expected,
            TransferStatus.RECONCILIATION_REQUIRED,
            detail,
            result=parsed,
            error=_problem_detail(
                code="POSTCONDITION_UNVERIFIED",
                title="Transfer requires reconciliation",
                detail=reconcile_result.state,
                retryable=False,
                correlation_id=record.correlation_id,
            ),
        )

    async def _finalize_delivery_failure(
        self,
        loaded: _LoadedContext,
        classification: ErrorClassification,
        outcome: OutboundOutcome | None,
        duration_ms: int,
        request_id: str | None,
        *,
        exception: Exception,
    ) -> None:
        record = loaded.record
        current = await self._require_record(record.tenant_id, record.transfer_id)
        expected = (
            TransferStatus.CANCELLATION_REQUESTED
            if current.status == TransferStatus.CANCELLATION_REQUESTED
            else TransferStatus.DELIVERING
        )
        detail = _delivery_event_detail(record.attempt, classification.code, duration_ms, request_id)

        if classification.delivery_state == "not_sent" and expected == TransferStatus.CANCELLATION_REQUESTED:
            await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                expected,
                TransferStatus.CANCELLED,
                detail,
            )
            return

        if classification.delivery_state == "not_sent" and self._should_retry(record.attempt, None, classification):
            delay_seconds = self._retry_delay_seconds(record.attempt, classification)
            await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                expected,
                TransferStatus.RETRYING,
                detail,
                next_retry_at=_utc_now() + timedelta(seconds=delay_seconds),
                error=_problem_detail(
                    code=classification.code,
                    title="Transfer delivery will retry",
                    detail=str(exception),
                    retryable=True,
                    correlation_id=record.correlation_id,
                ),
            )
            return

        if classification.delivery_state == "unknown":
            await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                expected,
                TransferStatus.RECONCILIATION_REQUIRED,
                detail,
                error=_problem_detail(
                    code=classification.code,
                    title="Transfer delivery outcome is unknown",
                    detail=str(exception),
                    retryable=False,
                    correlation_id=record.correlation_id,
                ),
            )
            return

        target = TransferStatus.CANCELLED if expected == TransferStatus.CANCELLATION_REQUESTED else TransferStatus.FAILED
        await self._store.transition_state(
            record.tenant_id,
            record.transfer_id,
            expected,
            target,
            detail,
            error=_problem_detail(
                code=classification.code,
                title="Transfer delivery failed",
                detail=str(exception),
                retryable=False,
                correlation_id=record.correlation_id,
            ),
        )

    async def _finalize_pre_send_failure(
        self,
        record: TransferRecord,
        exception: Exception,
    ) -> None:
        current = await self._require_record(record.tenant_id, record.transfer_id)
        expected = (
            TransferStatus.CANCELLATION_REQUESTED
            if current.status == TransferStatus.CANCELLATION_REQUESTED
            else TransferStatus.DELIVERING
        )
        code = _error_code(exception)
        detail = _delivery_event_detail(record.attempt, code, 0, None)

        if expected == TransferStatus.CANCELLATION_REQUESTED:
            await self._store.transition_state(
                record.tenant_id,
                record.transfer_id,
                expected,
                TransferStatus.CANCELLED,
                detail,
            )
            return

        target = (
            TransferStatus.WAITING_REVIEW
            if _is_reviewable_pre_send_failure(exception)
            else TransferStatus.FAILED
        )
        title = (
            "Transfer requires review before delivery"
            if target == TransferStatus.WAITING_REVIEW
            else "Transfer delivery preparation failed"
        )
        await self._store.transition_state(
            record.tenant_id,
            record.transfer_id,
            expected,
            target,
            detail,
            error=_problem_detail(
                code=code,
                title=title,
                detail=str(exception),
                retryable=False,
                correlation_id=record.correlation_id,
            ),
        )

    async def _load_context(
        self,
        record: TransferRecord,
        *,
        override_payload: TransferRequest | None = None,
    ) -> _LoadedContext:
        connector_definition = await self._store.get_connector(
            record.tenant_id,
            record.connector_id,
            record.connector_version,
        )
        if connector_definition is None:
            raise NotFoundError("connector version not found for tenant")
        mapping = await self._store.get_mapping(
            record.tenant_id,
            record.mapping_id,
            record.mapping_version,
        )
        if mapping is None:
            raise NotFoundError("mapping version not found for tenant")
        connector = await self._registry.get(
            record.tenant_id,
            record.connector_id,
            record.connector_version,
        )
        effective_payload = override_payload
        if effective_payload is None:
            correction = await self._store.get_latest_review_correction(record.tenant_id, record.transfer_id)
            effective_payload = self._mapping_engine.apply_corrections(record.request, correction)
        operation = await connector.resolve_operation(effective_payload.delivery.operation)

        issues = _context_issues(record, connector_definition, mapping, effective_payload, operation)
        if issues:
            raise MappingValidationError(_issues_summary(issues))
        return _LoadedContext(
            record=record,
            connector_definition=connector_definition,
            mapping=mapping,
            connector=connector,
            effective_payload=effective_payload,
            operation=operation,
        )

    async def _collect_validation_issues(self, loaded: _LoadedContext) -> list[MappingIssue]:
        validation = await loaded.connector.validate_payload(
            loaded.effective_payload,
            loaded.mapping,
            loaded.operation,
        )
        return list(validation.issues)

    async def _require_record(self, tenant_id: str, transfer_id: str) -> TransferRecord:
        record = await self._store.get_transfer(tenant_id, transfer_id)
        if record is None:
            raise NotFoundError("transfer not found")
        return record

    def _retry_delay_seconds(self, attempt: int, classification: ErrorClassification) -> float:
        if classification.retry_after_seconds is not None:
            return min(self._retry_policy.max_delay_seconds, float(classification.retry_after_seconds))
        base = min(
            self._retry_policy.max_delay_seconds,
            self._retry_policy.initial_delay_seconds * (2 ** max(attempt - 1, 0)),
        )
        jitter = self._retry_policy.jitter_ratio
        if jitter <= 0:
            return base
        factor = 1 + random.uniform(-jitter, jitter)
        return min(self._retry_policy.max_delay_seconds, max(0.0, base * factor))

    def _should_retry(
        self,
        attempt: int,
        status_code: int | None,
        classification: ErrorClassification,
    ) -> bool:
        if not classification.retryable:
            return False
        if attempt >= self._retry_policy.max_attempts:
            return False
        if classification.delivery_state == "not_sent":
            return status_code is None or status_code in self._retry_policy.retry_statuses
        if classification.delivery_state == "received":
            return status_code is not None and status_code in self._retry_policy.retry_statuses
        return False


def _context_issues(
    record: TransferRecord,
    connector_definition: ConnectorDefinition,
    mapping: MappingDefinition,
    payload: TransferRequest,
    operation: OperationSelection,
) -> list[MappingIssue]:
    issues: list[MappingIssue] = []
    binding = connector_definition.operation_bindings.get(operation.name)
    if payload.delivery.connector_id != record.connector_id or mapping.connector_id != record.connector_id:
        issues.append(
            _issue("/delivery/connector_id", "/connector_id", "CONNECTOR_MISMATCH", "connector does not match pinned record")
        )
    if payload.delivery.mapping_id != record.mapping_id:
        issues.append(
            _issue("/delivery/mapping_id", "/mapping_id", "MAPPING_MISMATCH", "mapping does not match pinned record")
        )
    if operation.name not in mapping.operations:
        issues.append(
            _issue("/delivery/operation", "/operations", "OPERATION_NOT_ALLOWED", "operation is not allowed by mapping")
        )
    if binding is None:
        issues.append(
            _issue("/delivery/operation", "/operation_bindings", "OPERATION_UNBOUND", "operation binding is missing")
        )
    elif binding.operation_id != operation.operation_id:
        issues.append(
            _issue(
                "/delivery/operation",
                "/operation_bindings",
                "OPERATION_BINDING_MISMATCH",
                "resolved operation does not match pinned binding",
            )
        )
    if payload.document.document_type not in mapping.document_types:
        issues.append(
            _issue("/document/document_type", "/document_types", "DOCUMENT_TYPE_UNSUPPORTED", "document type is not allowed by mapping")
        )
    if payload.metadata.tenant_id != record.tenant_id:
        issues.append(
            _issue("/metadata/tenant_id", "/tenant_id", "TENANT_MISMATCH", "payload tenant does not match record tenant")
        )
    return issues


def _issue(source_path: str, target_path: str, code: str, message: str) -> MappingIssue:
    return MappingIssue(
        source_path=source_path,
        target_path=target_path,
        code=code,
        message=message,
        retryable=False,
    )


def _issues_problem_detail(record: TransferRecord, issues: list[MappingIssue]) -> ProblemDetail:
    return _problem_detail(
        code="VALIDATION_FAILED",
        title="Transfer validation failed",
        detail=_issues_summary(issues),
        retryable=False,
        correlation_id=record.correlation_id,
        errors=[
            {
                "code": issue.code,
                "message": issue.message,
                "source_path": issue.source_path,
                "target_path": issue.target_path,
            }
            for issue in issues
        ],
    )


def _problem_detail(
    *,
    code: str,
    title: str,
    detail: str,
    retryable: bool,
    correlation_id: str | None,
    errors: list[dict[str, str]] | None = None,
) -> ProblemDetail:
    return ProblemDetail(
        type=f"https://openapi-to-mcp/errors/{code.lower()}",
        title=title,
        status=422,
        code=code,
        detail=detail,
        correlation_id=correlation_id,
        retryable=retryable,
        errors=errors or [],
    )


def _issues_summary(issues: list[MappingIssue]) -> str:
    return "; ".join(f"{issue.code}: {issue.message}" for issue in issues)


def _all_reviewable(issues: list[MappingIssue]) -> bool:
    return bool(issues) and all(issue.code in _REVIEWABLE_CODES for issue in issues)


def _error_code(error: Exception) -> str:
    return str(error) if str(error) else error.__class__.__name__


def _is_reviewable_pre_send_failure(error: Exception) -> bool:
    if not isinstance(error, MappingValidationError):
        return False
    detail = str(error)
    return "requires review" in detail or "LOW_CONFIDENCE" in detail


def _delivery_event_detail(
    attempt: int,
    classification: str,
    duration_ms: int,
    request_id: str | None,
) -> dict[str, Any]:
    return {
        "attempt": attempt,
        "classification": classification,
        "duration_ms": duration_ms,
        "request_id": request_id,
    }


def _reconciliation_event_detail(evidence: ReconciliationEvidence) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "resolution": evidence.resolution,
        "target_resource_id": evidence.target_resource_id,
        "notes_present": evidence.notes is not None,
    }
    if evidence.notes is not None:
        detail["notes_sha256"] = hashlib.sha256(evidence.notes.encode("utf-8")).hexdigest()
    return detail


def _validated_target_resource_id(result: TransferResult) -> str:
    value = result.target_resource_id
    if not isinstance(value, str) or not value:
        raise ValueError("TARGET_RESOURCE_ID_INVALID")
    return value


def _optional_target_resource_id(result: TransferResult | None) -> str | None:
    if result is None:
        return None
    value = result.target_resource_id
    if isinstance(value, str) and value:
        return value
    return None


def _canonical_deduplication_value(payload: TransferRequest, pointer: str) -> str:
    document = payload.model_dump(mode="json")
    try:
        value = resolve_pointer(document, pointer)
    except JsonPointerException as exc:
        raise MappingValidationError("DEDUPLICATION_KEY_INVALID") from exc
    if isinstance(value, (dict, list)) or value is None or value == "":
        raise MappingValidationError("DEDUPLICATION_KEY_INVALID")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _review_correction_model(values: dict[str, Any], reason: str | None, actor: str):
    from .models import ReviewCorrection

    return ReviewCorrection(
        correction_ref="review://pending",
        values=values,
        actor=actor,
        reason=reason or "manual correction",
    )


async def _close_connector(connector: Connector) -> None:
    client = getattr(connector, "_http_client", None)
    if client is not None and hasattr(client, "aclose"):
        await client.aclose()


def _utc_now() -> datetime:
    return datetime.now(UTC)