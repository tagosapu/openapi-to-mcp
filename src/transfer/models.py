from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, StringConstraints, model_validator


JsonPointer = StringConstraints(pattern=r"^(?:|/(?:[^/~]|~0|~1)*)+$")
OpaqueRef = StringConstraints(
    pattern=r"^object://[A-Za-z0-9._-]+(?:/[A-Za-z0-9._~!$&'()*+,;=:@%-]+)+$"
)


class TransferBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OcrSource(TransferBaseModel):
    page: int | None = Field(default=None, ge=1)
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    polygon: list[list[float]] | None = None


class OcrField(TransferBaseModel):
    raw_value: str | None = None
    value: Any | None = None
    value_type: Literal[
        "string",
        "integer",
        "number",
        "boolean",
        "date",
        "datetime",
        "currency",
        "object",
        "array",
    ]
    unit: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    status: Literal[
        "extracted",
        "missing",
        "ambiguous",
        "invalid",
        "manually_corrected",
    ]
    source: OcrSource | None = None

    @model_validator(mode="after")
    def validate_value_shape(self) -> OcrField:
        if self.value_type == "currency":
            if self.value is None or not isinstance(self.value, int | float):
                raise ValueError("currency fields require a numeric value")
            if self.unit is None:
                raise ValueError("currency fields require a unit")
        if self.value_type == "integer" and self.value is not None and not isinstance(self.value, int):
            raise ValueError("integer fields require an integer value")
        if self.value_type == "number" and self.value is not None and not isinstance(self.value, int | float):
            raise ValueError("number fields require a numeric value")
        if self.value_type == "boolean" and self.value is not None and not isinstance(self.value, bool):
            raise ValueError("boolean fields require a boolean value")
        if self.value_type in {"string", "date", "datetime"} and self.value is not None and not isinstance(self.value, str):
            raise ValueError(f"{self.value_type} fields require a string value")
        if self.value_type == "object" and self.value is not None and not isinstance(self.value, dict):
            raise ValueError("object fields require an object value")
        if self.value_type == "array" and self.value is not None and not isinstance(self.value, list):
            raise ValueError("array fields require an array value")
        return self


class OcrLineItem(TransferBaseModel):
    fields: dict[str, OcrField]


class DocumentContent(TransferBaseModel):
    media_type: str | None = None
    filename: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^sha256:[A-Fa-f0-9]{64}$")
    storage_ref: str | None = Field(default=None, min_length=1)


class OcrDocument(TransferBaseModel):
    document_id: str = Field(min_length=1)
    document_type: str = Field(min_length=1)
    source_system: str = Field(min_length=1)
    occurred_at: datetime | None = None
    content: DocumentContent | None = None


class OcrResult(TransferBaseModel):
    languages: list[str] = Field(default_factory=list)
    text_ref: str | None = Field(default=None, min_length=1)
    fields: dict[str, OcrField] | None = None
    line_items: list[OcrLineItem] | None = None

    @model_validator(mode="after")
    def require_fields_or_line_items(self) -> OcrResult:
        if not self.fields and not self.line_items:
            raise ValueError("ocr must contain fields or line_items")
        return self


class DeliveryRequest(TransferBaseModel):
    connector_id: str = Field(min_length=1)
    mapping_id: str = Field(min_length=1)
    operation: Literal["create", "update", "upsert"]
    deduplication_key_path: str = Field(pattern=r"^(?:|/(?:[^/~]|~0|~1)*)+$")


class TransferMetadata(TransferBaseModel):
    tenant_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    labels: dict[str, str] = Field(default_factory=dict)


class TransferStatus(str, Enum):
    ACCEPTED = "accepted"
    VALIDATING = "validating"
    WAITING_REVIEW = "waiting_review"
    QUEUED = "queued"
    DELIVERING = "delivering"
    RETRYING = "retrying"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CANCELLATION_REQUESTED = "cancellation_requested"
    SUCCEEDED = "succeeded"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TransferRequest(TransferBaseModel):
    schema_version: Literal["ocr-transfer/v1"]
    document: OcrDocument
    ocr: OcrResult
    delivery: DeliveryRequest
    metadata: TransferMetadata


class MappingTarget(TransferBaseModel):
    location: Literal["path", "query", "header", "body"]
    name: str | None = None
    pointer: str | None = Field(default=None, pattern=r"^(?:|/(?:[^/~]|~0|~1)*)+$")

    @model_validator(mode="after")
    def validate_target_shape(self) -> MappingTarget:
        if self.location == "body":
            if self.pointer is None or self.name is not None:
                raise ValueError("body targets require pointer and must not define name")
        else:
            if self.name is None or self.pointer is not None:
                raise ValueError("path/query/header targets require name and must not define pointer")
        return self


class MappingCondition(TransferBaseModel):
    source: str = Field(pattern=r"^(?:|/(?:[^/~]|~0|~1)*)+$")
    operator: Literal["exists", "equals", "not_equals", "in"]
    value: Any | None = None


class MappingRule(TransferBaseModel):
    rule_id: str = Field(min_length=1)
    source: str = Field(pattern=r"^(?:|/(?:[^/~]|~0|~1)*)+$")
    target: MappingTarget
    required: bool = False
    on_missing: Literal["error", "omit", "null"] = "error"
    transforms: list[str] = Field(default_factory=list)
    default: Any | None = None
    enum_map: dict[str, str | int | float | bool] = Field(default_factory=dict)
    condition: MappingCondition | None = None


class MappingIssue(TransferBaseModel):
    source_path: str
    target_path: str
    code: str
    message: str
    retryable: bool


class MappingPreview(TransferBaseModel):
    method: str
    path: str
    path_params: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: dict[str, Any] | list[Any] | None
    issues: list[MappingIssue] = Field(default_factory=list)
    requires_review: bool


class AdditionalHeader(TransferBaseModel):
    name: str = Field(min_length=1)
    value_ref: str = Field(min_length=1)


class ConnectorPolicy(TransferBaseModel):
    connect_timeout_seconds: float = 5
    read_timeout_seconds: float = 30
    total_timeout_seconds: float = 60
    max_response_bytes: int = 10 * 1024 * 1024
    max_redirects: int = 0


class PostconditionDefinition(TransferBaseModel):
    reconcile_operation_id: str
    result_id_pointer: str = Field(pattern=r"^(?:|/(?:[^/~]|~0|~1)*)+$")
    not_found_statuses: set[int] = Field(default_factory=lambda: {404})
    registered_statuses: set[int] = Field(default_factory=lambda: {200, 201})


class OperationBinding(TransferBaseModel):
    operation_id: str
    idempotency_header: str | None = None
    postcondition: PostconditionDefinition | None = None
    lookup_operation_id: str | None = None
    lookup_parameter_name: str | None = None
    lookup_parameter_location: Literal["path", "query"] | None = None
    conflict_policy: Literal["reconcile", "fail"] = "reconcile"


class OperationSelection(TransferBaseModel):
    name: Literal["create", "update", "upsert"]
    operation_id: str
    method: str
    path: str


class MappingDefinition(TransferBaseModel):
    mapping_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    status: Literal["draft", "published", "retired"]
    connector_id: str = Field(min_length=1)
    document_types: list[str] = Field(min_length=1)
    operations: list[Literal["create", "update", "upsert"]] = Field(min_length=1)
    deduplication_key_path: str = Field(pattern=r"^(?:|/(?:[^/~]|~0|~1)*)+$")
    target_schema_ref: str = Field(pattern=r"^openapi:#(?:/(?:[^/~]|~0|~1)*)+$")
    rules: list[MappingRule] = Field(default_factory=list)


class ConnectorDefinition(TransferBaseModel):
    connector_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    type: Literal["rest-openapi"]
    display_name: str | None = None
    base_url: AnyHttpUrl
    spec_ref: str = Field(min_length=1)
    spec: dict[str, Any]
    credential_ref: str = Field(min_length=1)
    additional_headers: list[AdditionalHeader] = Field(default_factory=list)
    operation_bindings: dict[str, OperationBinding]
    policy: ConnectorPolicy


class OutboundRequestParts(TransferBaseModel):
    operation_id: str
    method: str
    path: str
    path_params: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: dict[str, Any] | list[Any] | None
    idempotency_key: str


class TransferResult(TransferBaseModel):
    target_resource_id: str | None = None
    target_request_id: str | None = None
    postcondition_verified: bool = False
    completed_at: datetime | None = None
    response_ref: str | None = None


class ProblemDetail(TransferBaseModel):
    type: str
    title: str
    status: int
    code: str
    detail: str
    instance: str | None = None
    correlation_id: str | None = None
    retryable: bool
    errors: list[dict[str, str]] = Field(default_factory=list)


class ReviewCorrection(TransferBaseModel):
    correction_ref: str
    values: dict[str, Any]
    actor: str
    reason: str


class TransferRecord(TransferBaseModel):
    transfer_id: str
    tenant_id: str
    status: TransferStatus
    request: TransferRequest
    connector_id: str
    mapping_id: str
    connector_version: int
    mapping_version: int
    correlation_id: str
    document_id: str
    document_type: str
    idempotency_key: str
    attempt: int
    next_retry_at: datetime | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    result: TransferResult | None = None
    error: ProblemDetail | None = None


class TransferFilters(TransferBaseModel):
    status: TransferStatus | None = None
    connector_id: str | None = None
    document_id: str | None = None
    correlation_id: str | None = None
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class CreateTransferResult(TransferBaseModel):
    record: TransferRecord
    idempotent_replay: bool


class ClaimedTransfer(TransferBaseModel):
    record: TransferRecord
    phase: Literal["validate", "deliver"]