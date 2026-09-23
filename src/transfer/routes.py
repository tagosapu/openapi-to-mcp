from __future__ import annotations

import json
import re
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import JwtAuthorizer, Principal
from .connector import Connector
from .errors import (
    IdempotencyConflict,
    InvalidTransitionError,
    MappingValidationError,
    NotFoundError,
    PayloadLimitError,
    TenantIsolationError,
)
from .limits import RateLimitDecision
from .mapping import MappingEngine
from .models import (
    ConnectorDefinition,
    ConnectorCreateRequest,
    MappingDefinition,
    MappingIssue,
    MappingPreview,
    OcrDocument,
    OcrResult,
    OperationBinding,
    ProblemDetail,
    ReconciliationEvidence,
    TransferFilters,
    TransferMetadata,
    TransferRecord,
    TransferRequest,
    TransferStatus,
)
from .openapi_contract import ContractPreflight, public_spec_hash
from .observability import SecretRedactor
from .store import TransferStore, encode_transfer_cursor

_ModelT = TypeVar("_ModelT", bound=BaseModel)
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        detail: str,
        *,
        title: str | None = None,
        retryable: bool = False,
        errors: list[dict[str, str]] | None = None,
        headers: dict[str, str] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.title = title or _title_for_status(status)
        self.retryable = retryable
        self.errors = errors or []
        self.headers = headers or {}
        self.correlation_id = correlation_id


class _ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "correct", "reject"]
    correction_reason: str | None = None
    corrected_fields: dict[str, Any] | None = None


class _MappingPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str
    operation: Literal["create", "update", "upsert"]
    document: OcrDocument
    ocr: OcrResult
    metadata: TransferMetadata
    deduplication_key_path: str | None = None


class _ConnectorValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["structural", "dry-run-write", "sandbox-write"] | None = None
    write_validation: bool | None = None


async def current_principal(request: Request) -> Principal:
    authorizer = getattr(request.app.state, "authorizer", None)
    if not isinstance(authorizer, JwtAuthorizer):
        raise ApiError(401, "UNAUTHORIZED", "authentication is required")
    authorization = request.headers.get("Authorization")
    if not authorization:
        raise ApiError(401, "UNAUTHORIZED", "authentication is required")
    try:
        principal = await authorizer.authorize(authorization)
        if not principal.tenant_id or not principal.subject:
            raise ValueError("required claims are missing")
        return principal
    except Exception as exc:
        raise ApiError(401, "UNAUTHORIZED", "authentication is required") from exc


def require_scope(scope: str) -> Callable[..., Any]:
    async def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if scope not in principal.scopes:
            raise ApiError(
                403,
                "FORBIDDEN",
                "required scope is missing",
                correlation_id=None,
            )
        return principal

    dependency.__name__ = f"require_{scope.replace(':', '_')}"
    return dependency


def create_router() -> APIRouter:
    router = APIRouter()

    @router.post("/v1/transfers", name="create_transfer", status_code=202)
    async def create_transfer(
        request: Request,
        principal: Principal = Depends(require_scope("transfer:write")),
        body: dict[str, Any] = Depends(_required_json_body),
    ) -> JSONResponse:
        _check_rate_limit(request, principal)
        payload = _parse_model(TransferRequest, body)
        try:
            _payload_limits(request).validate(payload)
        except PayloadLimitError as exc:
            raise _api_error_from_exception(
                exc,
                correlation_id=payload.metadata.correlation_id,
            ) from exc
        _require_tenant(principal, payload.metadata.tenant_id)
        idempotency_key = request.headers.get("Idempotency-Key", "")
        if not 1 <= len(idempotency_key) <= 256:
            raise ApiError(400, "IDEMPOTENCY_KEY_INVALID", "Idempotency-Key must contain 1 to 256 characters")

        store = _store(request)
        try:
            connector, mapping = await _resolve_transfer_dependencies(
                request,
                principal.tenant_id,
                payload,
            )
            created = await store.create_or_get_transfer(
                tenant_id=principal.tenant_id,
                idempotency_key=idempotency_key,
                request=payload,
                correlation_id=_correlation_id(request, payload.metadata.correlation_id),
                connector_version=connector.version,
                mapping_version=mapping.version,
            )
        except Exception as exc:
            raise _api_error_from_exception(exc, correlation_id=payload.metadata.correlation_id) from exc

        _observability(request).record_event(
            status=created.record.status.value,
            classification="accepted",
            connector_id=created.record.connector_id,
        )

        headers = {"X-Correlation-ID": created.record.correlation_id}
        if created.idempotent_replay:
            headers["X-Idempotent-Replay"] = "true"
        return JSONResponse(
            status_code=202,
            content=_accepted_response(request, created.record),
            headers=headers,
        )

    @router.get("/v1/transfers", name="list_transfers")
    async def list_transfers(
        request: Request,
        status: TransferStatus | None = None,
        connector_id: str | None = None,
        document_id: str | None = None,
        correlation_id: str | None = None,
        cursor: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        principal: Principal = Depends(require_scope("transfer:read")),
    ) -> dict[str, Any]:
        filters = TransferFilters(
            status=status,
            connector_id=connector_id,
            document_id=document_id,
            correlation_id=correlation_id,
            cursor=cursor,
            limit=limit,
            created_after=created_after,
            created_before=created_before,
        )
        try:
            records = await _store(request).list_transfers(principal.tenant_id, filters)
        except Exception as exc:
            raise _api_error_from_exception(exc) from exc
        has_more = len(records) > limit
        page = records[:limit]
        return {
            "items": [_summary_response(record) for record in page],
            "next_cursor": encode_transfer_cursor(page[-1]) if has_more and page else None,
        }

    @router.get("/v1/transfers/{transfer_id}", name="get_transfer")
    async def get_transfer(
        transfer_id: str,
        request: Request,
        principal: Principal = Depends(require_scope("transfer:read")),
    ) -> dict[str, Any]:
        record = await _store(request).get_transfer(principal.tenant_id, transfer_id)
        if record is None:
            raise ApiError(404, "NOT_FOUND", "transfer not found")
        return _detail_response(request, record)

    @router.post("/v1/transfers/{transfer_id}/retry", name="retry_transfer", status_code=202)
    async def retry_transfer(
        transfer_id: str,
        request: Request,
        principal: Principal = Depends(require_scope("transfer:retry")),
    ) -> JSONResponse:
        try:
            record = await _worker(request).retry_transfer(principal.tenant_id, transfer_id)
        except Exception as exc:
            raise _api_error_from_exception(exc) from exc
        return JSONResponse(status_code=202, content=_action_response(record, "retry accepted"))

    @router.post("/v1/transfers/{transfer_id}/cancel", name="cancel_transfer", status_code=202)
    async def cancel_transfer(
        transfer_id: str,
        request: Request,
        principal: Principal = Depends(require_scope("transfer:cancel")),
    ) -> JSONResponse:
        try:
            record = await _worker(request).cancel_transfer(principal.tenant_id, transfer_id)
        except Exception as exc:
            raise _api_error_from_exception(exc) from exc
        return JSONResponse(status_code=202, content=_action_response(record, "cancellation accepted"))

    @router.post("/v1/transfers/{transfer_id}/review", name="review_transfer")
    async def review_transfer(
        transfer_id: str,
        request: Request,
        principal: Principal = Depends(require_scope("transfer:review")),
        body: dict[str, Any] = Depends(_required_json_body),
    ) -> dict[str, Any]:
        review = _parse_model(_ReviewRequest, body)
        try:
            record = await _worker(request).review_transfer(
                principal.tenant_id,
                transfer_id,
                review.action,
                review.corrected_fields,
                review.correction_reason,
                actor=principal.subject,
            )
        except Exception as exc:
            raise _api_error_from_exception(exc) from exc
        return _detail_response(request, record)

    @router.post("/v1/transfers/{transfer_id}/reconcile", name="reconcile_transfer")
    async def reconcile_transfer(
        transfer_id: str,
        request: Request,
        principal: Principal = Depends(require_scope("transfer:reconcile")),
        body: dict[str, Any] = Depends(_required_json_body),
    ) -> dict[str, Any]:
        evidence = _parse_model(ReconciliationEvidence, body)
        try:
            record = await _worker(request).reconcile_transfer(
                principal.tenant_id,
                transfer_id,
                evidence=evidence,
            )
        except Exception as exc:
            raise _api_error_from_exception(exc) from exc
        return _detail_response(request, record)

    @router.post("/v1/mappings/{mapping_id}/preview", name="preview_mapping")
    async def preview_mapping(
        mapping_id: str,
        request: Request,
        principal: Principal = Depends(require_scope("mapping:read")),
        body: dict[str, Any] = Depends(_required_json_body),
    ) -> dict[str, Any]:
        preview_request = _parse_model(_MappingPreviewRequest, body)
        _require_tenant(principal, preview_request.metadata.tenant_id)
        store = _store(request)
        mapping = await store.get_mapping(principal.tenant_id, mapping_id)
        if mapping is None:
            raise ApiError(404, "NOT_FOUND", "mapping not found")
        if mapping.connector_id != preview_request.connector_id:
            raise ApiError(422, "MAPPING_CONNECTOR_MISMATCH", "mapping and connector do not match")
        if preview_request.document.document_type not in mapping.document_types:
            raise ApiError(422, "DOCUMENT_TYPE_UNSUPPORTED", "document type is not supported by the mapping")
        if preview_request.operation not in mapping.operations:
            raise ApiError(422, "OPERATION_NOT_ALLOWED", "operation is not allowed by the mapping")
        if (
            preview_request.deduplication_key_path is not None
            and preview_request.deduplication_key_path != mapping.deduplication_key_path
        ):
            raise ApiError(422, "DEDUPLICATION_KEY_MISMATCH", "deduplication key path does not match the mapping")
        connector_definition = await store.get_connector(
            principal.tenant_id,
            preview_request.connector_id,
        )
        if connector_definition is None:
            raise ApiError(404, "NOT_FOUND", "connector not found")
        payload = TransferRequest(
            schema_version="ocr-transfer/v1",
            document=preview_request.document,
            ocr=preview_request.ocr,
            delivery={
                "connector_id": preview_request.connector_id,
                "mapping_id": mapping_id,
                "operation": preview_request.operation,
                "deduplication_key_path": preview_request.deduplication_key_path
                or mapping.deduplication_key_path,
            },
            metadata=preview_request.metadata,
        )
        try:
            _payload_limits(request).validate(payload)
        except PayloadLimitError as exc:
            raise _api_error_from_exception(exc) from exc
        connector: Connector | None = None
        try:
            connector, operation = await _load_connector_operation(
                request,
                principal.tenant_id,
                connector_definition.connector_id,
                connector_definition.version,
                preview_request.operation,
            )
            preview = _mapping_engine(request).preview(payload, mapping, operation)
            validation = await connector.validate_payload(payload, mapping, operation)
            issues = _merge_issues(preview.issues, validation.issues)
        except Exception as exc:
            raise _api_error_from_exception(exc) from exc
        finally:
            await _close_connector(connector)
        return _preview_response(preview, issues)

    @router.get("/v1/connectors", name="list_connectors")
    async def list_connectors(
        request: Request,
        principal: Principal = Depends(require_scope("connector:read")),
    ) -> dict[str, Any]:
        connectors = await _store(request).list_connectors(principal.tenant_id)
        return {"items": [_connector_summary(connector) for connector in connectors]}

    @router.post("/v1/connectors", name="create_connector", status_code=201)
    async def create_connector(
        request: Request,
        principal: Principal = Depends(require_scope("connector:admin")),
        body: dict[str, Any] = Depends(_required_json_body),
    ) -> JSONResponse:
        try:
            public_definition = _parse_model(ConnectorCreateRequest, body)
            definition = await _translate_connector_definition(
                request,
                principal.tenant_id,
                public_definition,
            )
            await _registry(request).register(principal.tenant_id, definition)
            stored = await _store(request).get_connector(
                principal.tenant_id,
                definition.connector_id,
                definition.version,
            )
        except Exception as exc:
            raise _api_error_from_exception(exc) from exc
        if stored is None:
            raise ApiError(500, "INTERNAL_ERROR", "connector registration failed")
        return JSONResponse(status_code=201, content=_connector_registered_response(stored))

    @router.post("/v1/connectors/{connector_id}/validate", name="validate_connector", status_code=202)
    async def validate_connector(
        connector_id: str,
        request: Request,
        principal: Principal = Depends(require_scope("connector:admin")),
        body: dict[str, Any] = Depends(_optional_json_body),
    ) -> dict[str, Any]:
        validation_request = _parse_model(_ConnectorValidationRequest, body)
        if validation_request.mode in {"dry-run-write", "sandbox-write"} or validation_request.write_validation:
            raise ApiError(
                422,
                "WRITE_VALIDATION_UNSUPPORTED",
                "write validation is not supported by this connector",
            )
        connector: Connector | None = None
        try:
            connector = await _registry(request).get(principal.tenant_id, connector_id)
            result = await connector.validate_config()
            if not result.valid:
                raise ApiError(422, "CONTRACT_INVALID", "connector contract validation failed")
        except ApiError:
            raise
        except Exception as exc:
            raise _api_error_from_exception(exc) from exc
        finally:
            await _close_connector(connector)
        return {
            "validation_id": f"validation:{principal.tenant_id}:{connector_id}",
            "status": "accepted",
            "status_url": None,
        }

    @router.get("/v1/mappings", name="list_mappings")
    async def list_mappings(
        request: Request,
        principal: Principal = Depends(require_scope("mapping:read")),
    ) -> dict[str, Any]:
        mappings = await _store(request).list_mappings(principal.tenant_id)
        return {"items": [_mapping_summary(mapping) for mapping in mappings]}

    @router.post("/v1/mappings", name="create_mapping", status_code=201)
    async def create_mapping(
        request: Request,
        principal: Principal = Depends(require_scope("mapping:write")),
        body: dict[str, Any] = Depends(_required_json_body),
    ) -> JSONResponse:
        raw = dict(body)
        raw.setdefault("status", "draft")
        mapping = _parse_model(MappingDefinition, raw)
        if mapping.connector_id == "":
            raise ApiError(422, "MAPPING_INVALID", "connector_id is required")
        try:
            connector = await _store(request).get_connector(principal.tenant_id, mapping.connector_id)
            if connector is None:
                raise ApiError(422, "CONNECTOR_NOT_FOUND", "mapping connector is not registered")
            _validate_mapping_definition(mapping, connector)
            existing = await _store(request).get_mapping(
                principal.tenant_id,
                mapping.mapping_id,
                mapping.version,
            )
            if existing is not None and existing.model_dump(mode="json") != mapping.model_dump(mode="json"):
                raise ApiError(409, "MAPPING_VERSION_IMMUTABLE", "mapping version is immutable")
            await _store(request).save_mapping(mapping, principal.tenant_id)
        except ApiError:
            raise
        except Exception as exc:
            raise _api_error_from_exception(exc) from exc
        return JSONResponse(status_code=201, content=_mapping_registered_response(mapping))

    @router.get("/v1/health/live", name="health_live")
    async def health_live() -> dict[str, Any]:
        return {"status": "ok", "checked_at": _utc_now().isoformat()}

    @router.get("/v1/health/ready", name="health_ready")
    async def health_ready(request: Request) -> JSONResponse:
        dependencies = {"sqlite": "ok", "credentials": "ok", "worker": "ok"}
        try:
            connection = _store(request)._require_connection()  # type: ignore[attr-defined]
            cursor = await connection.execute("SELECT 1")
            await cursor.fetchone()
            await cursor.close()
            resolver = getattr(request.app.state, "credential_resolver")
            settings = getattr(request.app.state, "settings")
            key_ref = settings.data_encryption_key_ref
            if key_ref is None:
                raise RuntimeError("encryption key is not configured")
            await resolver.resolve(key_ref.get_secret_value() if hasattr(key_ref, "get_secret_value") else str(key_ref))
            if settings.worker_enabled:
                worker_task = getattr(_worker(request), "_task", None)
                if worker_task is None or worker_task.done():
                    dependencies["worker"] = "failed"
        except Exception:
            if dependencies["sqlite"] == "ok":
                dependencies["sqlite"] = "failed"
            if dependencies["credentials"] == "ok":
                dependencies["credentials"] = "failed"
        ready = all(value == "ok" for value in dependencies.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ok" if ready else "degraded",
                "checked_at": _utc_now().isoformat(),
                "dependencies": dependencies,
            },
        )

    return router


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected_exception)


async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
    return _problem_response(request, exc)


async def _handle_request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    errors = [_validation_error_item(error) for error in exc.errors()]
    return _problem_response(
        request,
        ApiError(
            422,
            "REQUEST_INVALID",
            "request validation failed",
            errors=errors,
        ),
    )


async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    status = exc.status_code
    detail = "request failed" if status >= 500 else str(exc.detail)
    return _problem_response(request, ApiError(status, _code_for_status(status), detail))


async def _handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
    return _problem_response(request, ApiError(500, "INTERNAL_ERROR", "internal server error"))


def _problem_response(request: Request, error: ApiError) -> JSONResponse:
    correlation_id = error.correlation_id or _correlation_id(request, None)
    problem = ProblemDetail(
        type=f"https://openapi-to-mcp/errors/{error.code.lower()}",
        title=error.title,
        status=error.status,
        code=error.code,
        detail=error.detail,
        instance=request.url.path,
        correlation_id=correlation_id,
        retryable=error.retryable,
        errors=error.errors,
    )
    return JSONResponse(
        status_code=error.status,
        content=problem.model_dump(mode="json"),
        media_type="application/problem+json",
        headers=error.headers,
    )


async def _required_json_body(request: Request) -> dict[str, Any]:
    return await _read_json_body(request, required=True)


async def _optional_json_body(request: Request) -> dict[str, Any]:
    return await _read_json_body(request, required=False)


async def _read_json_body(request: Request, *, required: bool) -> dict[str, Any]:
    content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    max_bytes = _payload_limits(request).max_payload_bytes
    content_length = request.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise ApiError(413, "PAYLOAD_TOO_LARGE", "request payload exceeds the configured limit")
        except ValueError:
            pass
    if content_length == "0" and not required:
        return {}
    if content_type != "application/json" and (required or content_length is not None):
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json")

    chunks: list[bytes] = []
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise ApiError(413, "PAYLOAD_TOO_LARGE", "request payload exceeds the configured limit")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw and not required:
        return {}
    if content_type != "application/json":
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json")
    if not raw:
        raise ApiError(400, "INVALID_JSON", "request body is required")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(400, "INVALID_JSON", "request body is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ApiError(422, "REQUEST_INVALID", "request body must be a JSON object")
    return value


def _parse_model(model: type[_ModelT], value: Any) -> _ModelT:
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise ApiError(
            422,
            "REQUEST_INVALID",
            "request validation failed",
            errors=[_validation_error_item(error) for error in exc.errors()],
        ) from exc


def _validation_error_item(error: dict[str, Any]) -> dict[str, str]:
    location = "/" + "/".join(str(part) for part in error.get("loc", ()))
    return {
        "source_path": location,
        "message": str(error.get("msg", "invalid value")),
    }


def _check_rate_limit(request: Request, principal: Principal) -> None:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return
    decision: RateLimitDecision = limiter.check(principal.tenant_id, principal.subject)
    if not decision.allowed:
        raise ApiError(
            429,
            "RATE_LIMITED",
            "request rate limit exceeded",
            retryable=True,
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )


def _require_tenant(principal: Principal, tenant_id: str) -> None:
    if tenant_id != principal.tenant_id:
        raise ApiError(403, "TENANT_MISMATCH", "payload tenant does not match authenticated tenant")


async def _resolve_transfer_dependencies(
    request: Request,
    tenant_id: str,
    payload: TransferRequest,
) -> tuple[ConnectorDefinition, MappingDefinition]:
    store = _store(request)
    connector = await store.get_connector(tenant_id, payload.delivery.connector_id)
    if connector is None:
        raise NotFoundError("connector not found")
    mapping = await store.get_mapping(tenant_id, payload.delivery.mapping_id)
    if mapping is None:
        raise NotFoundError("mapping not found")
    if mapping.status != "published":
        raise MappingValidationError("MAPPING_NOT_PUBLISHED")
    if mapping.connector_id != connector.connector_id or mapping.connector_id != payload.delivery.connector_id:
        raise MappingValidationError("MAPPING_CONNECTOR_MISMATCH")
    if payload.document.document_type not in mapping.document_types:
        raise MappingValidationError("DOCUMENT_TYPE_UNSUPPORTED")
    if payload.delivery.operation not in mapping.operations:
        raise MappingValidationError("OPERATION_NOT_ALLOWED")
    if payload.delivery.deduplication_key_path != mapping.deduplication_key_path:
        raise MappingValidationError("DEDUPLICATION_KEY_MISMATCH")
    connector_instance: Connector | None = None
    try:
        connector_instance, _ = await _load_connector_operation(
            request,
            tenant_id,
            connector.connector_id,
            connector.version,
            payload.delivery.operation,
        )
    finally:
        await _close_connector(connector_instance)
    return connector, mapping


_OPERATION_BINDINGS_EXTENSION = "x-openapi-to-mcp-operation-bindings"


async def _translate_connector_definition(
    request: Request,
    tenant_id: str,
    public_definition: ConnectorCreateRequest,
) -> ConnectorDefinition:
    store = _store(request)
    existing = await store.get_connector(tenant_id, public_definition.connector_id)
    if public_definition.spec is not None:
        spec = deepcopy(public_definition.spec)
        operation_bindings = _resolve_public_operation_bindings(spec)
        spec_ref = f"object://openapi/{public_spec_hash(spec)}"
    else:
        if existing is None or existing.spec_ref != public_definition.spec_ref:
            raise ApiError(
                422,
                "SPEC_SNAPSHOT_UNRESOLVED",
                "the referenced OpenAPI snapshot could not be resolved",
            )
        spec = deepcopy(existing.spec)
        operation_bindings = existing.operation_bindings
        spec_ref = existing.spec_ref

    return ConnectorDefinition(
        connector_id=public_definition.connector_id,
        version=(existing.version + 1) if existing is not None else 1,
        type=public_definition.type,
        display_name=public_definition.display_name,
        base_url=public_definition.base_url,
        spec_ref=spec_ref,
        spec=spec,
        credential_ref=public_definition.credential_ref,
        additional_headers=public_definition.additional_headers,
        operation_bindings=operation_bindings,
        policy=public_definition.policy,
    )


def _resolve_public_operation_bindings(spec: dict[str, Any]) -> dict[str, OperationBinding]:
    raw_bindings = spec.get(_OPERATION_BINDINGS_EXTENSION)
    if not isinstance(raw_bindings, dict) or not raw_bindings:
        raise ApiError(
            422,
            "OPERATION_BINDINGS_UNRESOLVED",
            "inline OpenAPI specs must declare operation bindings",
        )
    bindings: dict[str, OperationBinding] = {}
    for operation_name, raw_binding in raw_bindings.items():
        if operation_name not in {"create", "update", "upsert"} or not isinstance(raw_binding, dict):
            raise ApiError(
                422,
                "OPERATION_BINDINGS_INVALID",
                "inline OpenAPI operation bindings are invalid",
            )
        try:
            bindings[operation_name] = OperationBinding.model_validate(raw_binding)
        except ValidationError as exc:
            raise ApiError(
                422,
                "OPERATION_BINDINGS_INVALID",
                "inline OpenAPI operation bindings are invalid",
            ) from exc
    return bindings


async def _load_connector_operation(
    request: Request,
    tenant_id: str,
    connector_id: str,
    version: int,
    operation_name: str,
) -> tuple[Connector, Any]:
    registry = _registry(request)
    connector = await registry.get(tenant_id, connector_id, version)
    try:
        operation = await connector.resolve_operation(operation_name)
    except Exception:
        await _close_connector(connector)
        raise
    return connector, operation


async def _close_connector(connector: Connector | None) -> None:
    if connector is None:
        return
    client = getattr(connector, "_http_client", None)
    if client is not None and hasattr(client, "aclose"):
        await client.aclose()


def _validate_mapping_definition(mapping: MappingDefinition, connector: ConnectorDefinition) -> None:
    binding_names = set(connector.operation_bindings)
    if not set(mapping.operations).issubset(binding_names):
        raise ApiError(422, "OPERATION_UNBOUND", "mapping operation is not bound by the connector")
    seen: set[str] = set()
    for rule in mapping.rules:
        target = rule.target
        key = f"body:{target.pointer}" if target.location == "body" else f"{target.location}:{target.name}"
        if key in seen:
            raise ApiError(422, "DUPLICATE_TARGET", "mapping contains duplicate targets")
        seen.add(key)
        if target.location == "header" and (
            target.name is None
            or target.name.lower() in {"authorization", "content-length", "cookie", "host"}
            or target.name.lower().startswith("proxy-")
        ):
            raise ApiError(422, "FORBIDDEN_HEADER", "mapping contains a protected header")
    preflight = ContractPreflight().run(connector.spec)
    if not preflight.valid:
        raise ApiError(422, "CONTRACT_INVALID", "connector contract validation failed")
    for operation_name in mapping.operations:
        binding = connector.operation_bindings.get(operation_name)
        if binding is None:
            raise ApiError(422, "OPERATION_UNBOUND", "mapping operation is not bound by the connector")
        try:
            preflight.resolve_target_schema(mapping.target_schema_ref, binding.operation_id)
        except (ValueError, JsonSchemaValidationError) as exc:
            raise ApiError(422, "TARGET_SCHEMA_UNRESOLVED", "mapping target schema could not be resolved") from exc


def _merge_issues(first: list[MappingIssue], second: list[MappingIssue]) -> list[MappingIssue]:
    merged: list[MappingIssue] = []
    seen: set[tuple[str, str, str, str]] = set()
    for issue in [*first, *second]:
        key = (issue.source_path, issue.target_path, issue.code, issue.message)
        if key not in seen:
            seen.add(key)
            merged.append(issue)
    return merged


def _preview_response(preview: MappingPreview, issues: list[MappingIssue]) -> dict[str, Any]:
    errors = [
        {
            "source_path": issue.source_path,
            "target_path": issue.target_path,
            "message": issue.message,
            "retryable": issue.retryable,
        }
        for issue in issues
        if not issue.retryable
    ]
    warnings = [issue.message for issue in issues if issue.retryable]
    return {
        "method": preview.method,
        "path": preview.path,
        "headers": SecretRedactor().headers(preview.headers),
        "query": preview.query_params,
        "body": preview.json_body if preview.json_body is not None else {},
        "validation": {
            "passed": not issues,
            "warnings": warnings,
            "errors": errors,
        },
    }


def _accepted_response(request: Request, record: TransferRecord) -> dict[str, Any]:
    return {
        "transfer_id": record.transfer_id,
        "status": record.status.value,
        "status_url": str(request.url_for("get_transfer", transfer_id=record.transfer_id)),
        "connector_id": record.connector_id,
        "mapping_id": record.mapping_id,
        "mapping_version": record.mapping_version,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "correlation_id": record.correlation_id,
        "attempt": record.attempt,
        "result": _result_response(record),
        "error": _error_response(record),
    }


def _summary_response(record: TransferRecord) -> dict[str, Any]:
    return {
        "transfer_id": record.transfer_id,
        "status": record.status.value,
        "connector_id": record.connector_id,
        "mapping_id": record.mapping_id,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "correlation_id": record.correlation_id,
        "attempt": record.attempt,
    }


def _detail_response(request: Request, record: TransferRecord) -> dict[str, Any]:
    return {
        **_summary_response(record),
        "status_url": str(request.url_for("get_transfer", transfer_id=record.transfer_id)),
        "mapping_version": record.mapping_version,
        "validation_errors": _validation_errors_response(record),
        "result": _result_response(record),
        "error": _error_response(record),
    }


def _validation_errors_response(record: TransferRecord) -> list[dict[str, Any]]:
    if record.error is None:
        return []
    return [
        {
            "source_path": error.get("source_path", ""),
            "target_path": error.get("target_path", ""),
            "message": error.get("message", "validation failed"),
            "retryable": bool(error.get("retryable", False)),
        }
        for error in record.error.errors
    ]


def _result_response(record: TransferRecord) -> dict[str, Any] | None:
    if record.result is None:
        return None
    return {
        "target_resource_id": record.result.target_resource_id,
        "target_request_id": record.result.target_request_id,
        "completed_at": record.result.completed_at.isoformat() if record.result.completed_at else None,
        "response_ref": record.result.response_ref,
    }


def _error_response(record: TransferRecord) -> dict[str, Any] | None:
    if record.error is None:
        return None
    first_error = record.error.errors[0] if record.error.errors else {}
    result: dict[str, Any] = {
        "code": record.error.code,
        "message": _safe_stored_detail(record.error.code),
        "retryable": record.error.retryable,
    }
    target_path = first_error.get("target_path")
    if isinstance(target_path, str):
        result["target_path"] = target_path
    return result


def _action_response(record: TransferRecord, message: str) -> dict[str, Any]:
    return {
        "transfer_id": record.transfer_id,
        "status": record.status.value,
        "correlation_id": record.correlation_id,
        "message": message,
    }


def _connector_summary(connector: ConnectorDefinition) -> dict[str, Any]:
    return {
        "connector_id": connector.connector_id,
        "type": connector.type,
        "display_name": connector.display_name,
        "base_url": str(connector.base_url),
        "capabilities": sorted(connector.operation_bindings),
    }


def _connector_registered_response(connector: ConnectorDefinition) -> dict[str, Any]:
    return {
        **_connector_summary(connector),
        "spec_ref": connector.spec_ref,
    }


def _mapping_summary(mapping: MappingDefinition) -> dict[str, Any]:
    return {
        "mapping_id": mapping.mapping_id,
        "version": mapping.version,
        "status": mapping.status,
        "connector_id": mapping.connector_id,
        "document_types": mapping.document_types,
        "operations": mapping.operations,
    }


def _mapping_registered_response(mapping: MappingDefinition) -> dict[str, Any]:
    return {
        **_mapping_summary(mapping),
        "target_schema_ref": mapping.target_schema_ref,
    }


def _api_error_from_exception(exc: Exception, *, correlation_id: str | None = None) -> ApiError:
    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, IdempotencyConflict):
        return ApiError(409, "IDEMPOTENCY_CONFLICT", "Idempotency-Key is already bound to another request", correlation_id=correlation_id)
    if isinstance(exc, (NotFoundError, TenantIsolationError)):
        return ApiError(404, "NOT_FOUND", "resource not found", correlation_id=correlation_id)
    if isinstance(exc, InvalidTransitionError):
        return ApiError(409, "INVALID_TRANSITION", "transfer state does not allow this action", correlation_id=correlation_id)
    if isinstance(exc, PayloadLimitError):
        return ApiError(
            413,
            "PAYLOAD_LIMIT_EXCEEDED",
            "request payload exceeds an operational limit",
            correlation_id=correlation_id,
        )
    if isinstance(exc, (MappingValidationError, ValueError)):
        code = _safe_exception_code(exc)
        if code == "CONNECTOR_VERSION_IMMUTABLE":
            return ApiError(409, code, "connector version is immutable", correlation_id=correlation_id)
        return ApiError(422, code, _safe_exception_detail(code), correlation_id=correlation_id)
    return ApiError(500, "INTERNAL_ERROR", "internal server error", correlation_id=correlation_id)


def _safe_exception_code(exc: Exception) -> str:
    value = str(exc).split(":", 1)[0].strip().replace("-", "_").replace(" ", "_").upper()
    return value if _SAFE_CODE.fullmatch(value) else "VALIDATION_ERROR"


def _safe_exception_detail(code: str) -> str:
    details = {
        "MAPPING_NOT_PUBLISHED": "mapping is not published",
        "MAPPING_CONNECTOR_MISMATCH": "mapping and connector do not match",
        "DOCUMENT_TYPE_UNSUPPORTED": "document type is not supported by the mapping",
        "OPERATION_NOT_ALLOWED": "operation is not allowed by the mapping",
        "OPERATION_UNBOUND": "operation is not bound by the connector",
        "DEDUPLICATION_KEY_MISMATCH": "deduplication key path does not match the mapping",
        "TARGET_SCHEMA_UNRESOLVED": "mapping target schema could not be resolved",
        "CURSOR_INVALID": "cursor is invalid",
        "SPEC_SNAPSHOT_UNRESOLVED": "the referenced OpenAPI snapshot could not be resolved",
        "OPERATION_BINDINGS_UNRESOLVED": "inline OpenAPI specs must declare operation bindings",
        "OPERATION_BINDINGS_INVALID": "inline OpenAPI operation bindings are invalid",
        "WRITE_VALIDATION_UNSUPPORTED": "write validation is not supported by this connector",
        "TARGET_RESOURCE_ID_REQUIRED": "confirmed_present requires a target resource id",
    }
    return details.get(code, "request or mapping validation failed")


def _safe_stored_detail(code: str) -> str:
    return {
        "VALIDATION_FAILED": "transfer validation failed",
        "POSTCONDITION_UNVERIFIED": "transfer requires reconciliation",
        "DELIVERY_UNKNOWN": "transfer delivery outcome is unknown",
    }.get(code, "transfer processing failed")


def _store(request: Request) -> TransferStore:
    return request.app.state.store


def _registry(request: Request) -> Any:
    return request.app.state.registry


def _worker(request: Request) -> Any:
    return request.app.state.worker


def _mapping_engine(request: Request) -> MappingEngine:
    return request.app.state.mapping_engine


def _payload_limits(request: Request) -> Any:
    limits = request.app.state.payload_limits
    configured_max_bytes = int(request.app.state.settings.max_payload_bytes)
    if limits.max_payload_bytes != configured_max_bytes:
        limits.max_payload_bytes = configured_max_bytes
    return limits


def _observability(request: Request) -> Any:
    return request.app.state.observability


def _correlation_id(request: Request, fallback: str | None) -> str:
    header = request.headers.get("X-Correlation-ID")
    return header or fallback or str(uuid4())


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _title_for_status(status: int) -> str:
    return {
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        409: "Conflict",
        413: "Payload Too Large",
        415: "Unsupported Media Type",
        422: "Unprocessable Entity",
        429: "Too Many Requests",
        500: "Internal Server Error",
        503: "Service Unavailable",
    }.get(status, "Request Failed")


def _code_for_status(status: int) -> str:
    return {
        400: "BAD_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        409: "CONFLICT",
        413: "PAYLOAD_TOO_LARGE",
        415: "UNSUPPORTED_MEDIA_TYPE",
        422: "REQUEST_INVALID",
        429: "RATE_LIMITED",
        500: "INTERNAL_ERROR",
        503: "NOT_READY",
    }.get(status, "REQUEST_FAILED")