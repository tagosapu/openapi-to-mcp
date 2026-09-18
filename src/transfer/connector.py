from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import AnyHttpUrl, BaseModel, Field

from .models import MappingDefinition, MappingIssue, OperationSelection, TransferRequest, TransferResult
from .openapi_contract import ContractPreflightResult


class OutboundRequest(BaseModel):
    method: str
    url: AnyHttpUrl = Field(repr=False)
    headers: dict[str, str] = Field(repr=False)
    json_body: dict[str, Any] | list[Any] | None = Field(repr=False)


class OutboundOutcome(BaseModel):
    delivery_state: Literal["not_sent", "received", "unknown"]
    status_code: int | None
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    body: dict[str, Any] | list[Any] | None = Field(default=None, repr=False)
    request_id: str | None
    elapsed_ms: int


class ValidationResult(BaseModel):
    valid: bool
    issues: list[MappingIssue] = Field(default_factory=list)


class ErrorClassification(BaseModel):
    code: str
    retryable: bool
    delivery_state: Literal["not_sent", "received", "unknown"]
    retry_after_seconds: int | None = None


class ReconciliationContext(BaseModel):
    transfer_id: str
    idempotency_key: str
    deduplication_value: str
    operation_id: str
    mode: Literal["postcondition", "unknown"]
    target_resource_id: str | None = Field(default=None, min_length=1)


class ReconciliationResult(BaseModel):
    state: Literal["registered", "not_registered", "unknown"]
    target_resource_id: str | None = None


class Connector(Protocol):
    async def validate_config(self) -> ContractPreflightResult: ...

    async def resolve_operation(self, operation_name: str) -> OperationSelection: ...

    async def validate_payload(
        self,
        payload: TransferRequest,
        mapping: MappingDefinition,
        operation: OperationSelection,
    ) -> ValidationResult: ...

    async def build_request(self, parts: Any) -> OutboundRequest: ...

    async def send(self, request: OutboundRequest) -> OutboundOutcome: ...

    def parse_response(self, outcome: OutboundOutcome) -> TransferResult: ...

    def classify_error(self, outcome: OutboundOutcome | Exception) -> ErrorClassification: ...

    async def reconcile(self, context: ReconciliationContext) -> ReconciliationResult: ...