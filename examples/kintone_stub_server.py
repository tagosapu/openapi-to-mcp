#!/usr/bin/env python3
"""Local Kintone-compatible stub for testing generated MCP servers."""

from __future__ import annotations

import argparse
import os
import re
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Annotated, Any

import uvicorn
from fastapi import Body, FastAPI, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

DEFAULT_APP_ID = "1"
DEFAULT_TOKEN = "mock-token"
EXPECTED_TOKEN = os.environ.get("KINTONE_MOCK_API_TOKEN", DEFAULT_TOKEN)

app = FastAPI(
    title="Kintone API Mock",
    version="0.1.0",
    description="A local, in-memory Kintone-compatible API for MCP smoke tests.",
)

_state_lock = Lock()
_records: dict[str, dict[str, dict[str, Any]]] = {}
_next_ids: dict[str, int] = {}
_revisions: dict[str, int] = {}
_request_log: list[dict[str, Any]] = []

KINTONE_APP_FIELDS: dict[str, dict[str, Any]] = {
    "invoice_number": {
        "type": "SINGLE_LINE_TEXT",
        "code": "invoice_number",
        "label": "請求書番号",
        "required": True,
        "unique": True,
        "noLabel": False,
    },
    "invoice_date": {
        "type": "DATE",
        "code": "invoice_date",
        "label": "請求日",
        "required": True,
        "noLabel": False,
    },
    "vendor_name": {
        "type": "SINGLE_LINE_TEXT",
        "code": "vendor_name",
        "label": "取引先名",
        "required": True,
        "noLabel": False,
    },
    "subtotal": {
        "type": "NUMBER",
        "code": "subtotal",
        "label": "小計",
        "required": True,
        "noLabel": False,
    },
    "tax_amount": {
        "type": "NUMBER",
        "code": "tax_amount",
        "label": "消費税",
        "required": True,
        "noLabel": False,
    },
    "total_amount": {
        "type": "NUMBER",
        "code": "total_amount",
        "label": "合計金額",
        "required": True,
        "noLabel": False,
    },
    "currency": {
        "type": "DROP_DOWN",
        "code": "currency",
        "label": "通貨",
        "required": True,
        "options": {
            "JPY": {"label": "JPY", "index": 0},
            "USD": {"label": "USD", "index": 1},
        },
        "noLabel": False,
    },
    "status": {
        "type": "DROP_DOWN",
        "code": "status",
        "label": "転記ステータス",
        "required": True,
        "options": {
            "registered": {"label": "registered", "index": 0},
            "needs_review": {"label": "needs_review", "index": 1},
            "processed": {"label": "processed", "index": 2},
        },
        "noLabel": False,
    },
    "ocr_confidence": {
        "type": "NUMBER",
        "code": "ocr_confidence",
        "label": "OCR信頼度",
        "minValue": 0,
        "maxValue": 1,
        "noLabel": False,
    },
    "source_file": {
        "type": "SINGLE_LINE_TEXT",
        "code": "source_file",
        "label": "原帳票ファイル",
        "noLabel": False,
    },
    "ocr_text_ref": {
        "type": "SINGLE_LINE_TEXT",
        "code": "ocr_text_ref",
        "label": "OCR本文参照",
        "noLabel": False,
    },
    "line_items": {
        "type": "SUBTABLE",
        "code": "line_items",
        "label": "明細",
        "noLabel": False,
        "fields": {
            "description": {
                "type": "SINGLE_LINE_TEXT",
                "code": "description",
                "label": "品名",
                "noLabel": False,
            },
            "quantity": {
                "type": "NUMBER",
                "code": "quantity",
                "label": "数量",
                "noLabel": False,
            },
            "unit_price": {
                "type": "NUMBER",
                "code": "unit_price",
                "label": "単価",
                "noLabel": False,
            },
            "amount": {
                "type": "NUMBER",
                "code": "amount",
                "label": "金額",
                "noLabel": False,
            },
        },
    },
}


class RecordUpdate(BaseModel):
    id: str | int | None = None
    updateKey: dict[str, Any] | None = None
    record: dict[str, Any] = Field(default_factory=dict)


class SingleRecordUpdate(BaseModel):
    app: str | int
    id: str | int | None = None
    updateKey: dict[str, Any] | None = None
    record: dict[str, Any] = Field(default_factory=dict)


class RecordsUpdate(BaseModel):
    app: str | int
    records: list[RecordUpdate]


class RecordsDelete(BaseModel):
    app: str | int
    ids: list[str | int]


def _app_id(value: str | int) -> str:
    return str(value)


def _error(
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "id": "mock-error", "message": message},
        headers=headers,
    )


def _authorize(api_token: str | None) -> JSONResponse | None:
    supplied_tokens = {
        token.strip() for token in (api_token or "").split(",") if token.strip()
    }
    if EXPECTED_TOKEN not in supplied_tokens:
        return _error(401, "CB_AU01", "Invalid API token for the mock Kintone API")
    return None


def _system_fields(record_id: str, revision: int) -> dict[str, dict[str, str]]:
    return {
        "$id": {"type": "__ID__", "value": record_id},
        "$revision": {"type": "__REVISION__", "value": str(revision)},
    }


def _seed_record(
    app_id: str,
    record_id: str,
    fields: dict[str, dict[str, Any]],
) -> None:
    _records.setdefault(app_id, {})[record_id] = {
        **_system_fields(record_id, 1),
        **deepcopy(fields),
    }
    _next_ids[app_id] = max(_next_ids.get(app_id, 1), int(record_id) + 1)
    _revisions[app_id] = max(_revisions.get(app_id, 1), 1)


def _reset_state() -> None:
    with _state_lock:
        _records.clear()
        _next_ids.clear()
        _revisions.clear()
        _request_log.clear()
        _seed_record(
            DEFAULT_APP_ID,
            "1",
            {
                "invoice_number": {
                    "type": "SINGLE_LINE_TEXT",
                    "value": "INV-SEED-001",
                },
                "invoice_date": {"type": "DATE", "value": "2026-09-01"},
                "vendor_name": {
                    "type": "SINGLE_LINE_TEXT",
                    "value": "Seed Vendor",
                },
                "subtotal": {"type": "NUMBER", "value": "10000"},
                "tax_amount": {"type": "NUMBER", "value": "1000"},
                "total_amount": {"type": "NUMBER", "value": "11000"},
                "currency": {"type": "DROP_DOWN", "value": "JPY"},
                "status": {"type": "DROP_DOWN", "value": "registered"},
                "ocr_confidence": {"type": "NUMBER", "value": "0.99"},
                "source_file": {
                    "type": "SINGLE_LINE_TEXT",
                    "value": "seed.pdf",
                },
                "ocr_text_ref": {
                    "type": "SINGLE_LINE_TEXT",
                    "value": "object://ocr-text/seed-001",
                },
                "line_items": {
                    "type": "SUBTABLE",
                    "value": [
                        {
                            "id": "1",
                            "value": {
                                "description": {
                                    "type": "SINGLE_LINE_TEXT",
                                    "value": "Seed item",
                                },
                                "quantity": {"type": "NUMBER", "value": "1"},
                                "unit_price": {
                                    "type": "NUMBER",
                                    "value": "10000",
                                },
                                "amount": {"type": "NUMBER", "value": "10000"},
                            },
                        }
                    ],
                },
            },
        )


_reset_state()


@app.middleware("http")
async def record_request(request, call_next):
    response = await call_next(request)
    with _state_lock:
        _request_log.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": request.url.query,
                "status_code": response.status_code,
                "has_api_token": bool(request.headers.get("X-Cybozu-API-Token")),
            }
        )
    return response


def _get_app_records(app_id: str) -> dict[str, dict[str, Any]] | None:
    return _records.get(app_id)


def _record_matches_query(record: dict[str, Any], query: str) -> bool:
    where_clause = re.split(
        r"\border\s+by\b|\blimit\b|\boffset\b",
        query,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    if not where_clause.strip():
        return True
    match = re.fullmatch(
        r'\s*([A-Za-z0-9_$-]+)\s*=\s*"([^"]*)"\s*', where_clause
    )
    if match is None:
        return False
    field_code, expected = match.groups()
    field = record.get(field_code, {})
    return str(field.get("value", "")) == expected


def _select_records(
    app_id: str,
    query: str = "",
    fields: list[str] | None = None,
) -> list[dict[str, Any]] | None:
    records = _get_app_records(app_id)
    if records is None:
        return None
    selected = [
        deepcopy(record)
        for record in records.values()
        if _record_matches_query(record, query)
    ]

    order_match = re.search(
        r"\border\s+by\s+([A-Za-z0-9_$-]+)\s+(asc|desc)",
        query,
        flags=re.IGNORECASE,
    )
    if order_match:
        field_code, direction = order_match.groups()
        selected.sort(
            key=lambda record: str(record.get(field_code, {}).get("value", "")),
            reverse=direction.lower() == "desc",
        )

    offset_match = re.search(
        r"\boffset\s+(\d+)", query, flags=re.IGNORECASE
    )
    limit_match = re.search(r"\blimit\s+(\d+)", query, flags=re.IGNORECASE)
    offset = int(offset_match.group(1)) if offset_match else 0
    limit = int(limit_match.group(1)) if limit_match else None
    selected = selected[offset : offset + limit if limit is not None else None]

    if fields:
        selected = [
            {
                field_code: record[field_code]
                for field_code in ["$id", "$revision", *fields]
                if field_code in record
            }
            for record in selected
        ]
    return selected


def _next_record_id(app_id: str) -> str:
    record_id = str(_next_ids.get(app_id, 1))
    _next_ids[app_id] = int(record_id) + 1
    return record_id


def _bump_revision(app_id: str) -> int:
    revision = _revisions.get(app_id, 1) + 1
    _revisions[app_id] = revision
    return revision


def _record_update_target(
    app_id: str,
    update: RecordUpdate,
) -> tuple[dict[str, Any] | None, str | None]:
    records = _get_app_records(app_id)
    if records is None:
        return None, None
    if update.id is not None:
        record_id = str(update.id)
        return records.get(record_id), record_id
    if update.updateKey:
        field_code = update.updateKey.get("field")
        expected_value = update.updateKey.get("value")
        if not isinstance(field_code, str) or expected_value is None:
            return None, None
        for record_id, record in records.items():
            if str(record.get(field_code, {}).get("value", "")) == str(expected_value):
                return record, record_id
    return None, None


def _is_empty_value(value: Any) -> bool:
    return value is None or value == "" or value == []


def _validate_number(field_code: str, value: Any) -> JSONResponse | None:
    if not isinstance(value, str):
        return _error(400, "MOCK_RE03", f"{field_code} must be a string number")
    try:
        numeric_value = Decimal(value)
    except InvalidOperation:
        return _error(400, "MOCK_RE03", f"{field_code} must be a string number")
    if not numeric_value.is_finite():
        return _error(400, "MOCK_RE03", f"{field_code} must be a string number")
    if field_code == "ocr_confidence" and not 0 <= numeric_value <= 1:
        return _error(400, "MOCK_RE03", "ocr_confidence must be between 0 and 1")
    return None


def _validate_writable_field(
    field_code: str,
    field: Any,
    definition: dict[str, Any],
) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    if not isinstance(field, dict) or "value" not in field:
        return None, _error(
            400,
            "MOCK_RE02",
            f"{field_code} must be an object with a value",
        )
    expected_type = definition["type"]
    supplied_type = field.get("type")
    if supplied_type is not None and supplied_type != expected_type:
        return None, _error(
            400,
            "MOCK_RE03",
            f"{field_code} type must be {expected_type}",
        )
    value = field["value"]
    if expected_type == "NUMBER":
        validation_error = _validate_number(field_code, value)
    elif expected_type in {"SINGLE_LINE_TEXT", "MULTI_LINE_TEXT"}:
        validation_error = (
            None
            if isinstance(value, str)
            else _error(400, "MOCK_RE03", f"{field_code} must be a string")
        )
    elif expected_type == "DATE":
        try:
            date.fromisoformat(value)
            validation_error = None
        except (TypeError, ValueError):
            validation_error = _error(
                400, "MOCK_RE03", f"{field_code} must be an ISO date"
            )
    elif expected_type == "DROP_DOWN":
        options = definition.get("options", {})
        validation_error = (
            None
            if value is None or (isinstance(value, str) and value in options)
            else _error(400, "MOCK_RE03", f"{field_code} has an invalid option")
        )
    elif expected_type == "SUBTABLE":
        validation_error = None
        if not isinstance(value, list):
            validation_error = _error(
                400, "MOCK_RE03", f"{field_code} must be an array"
            )
    else:
        validation_error = None

    if validation_error:
        return None, validation_error

    if expected_type != "SUBTABLE":
        return {"type": expected_type, "value": value}, None

    rows: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, dict) or not isinstance(row.get("value"), dict):
            return None, _error(
                400,
                "MOCK_RE03",
                f"{field_code} rows must contain a value object",
            )
        normalized_row: dict[str, Any] = {
            "id": str(row.get("id", index + 1)),
            "value": {},
        }
        for nested_code, nested_field in row["value"].items():
            nested_definition = definition["fields"].get(nested_code)
            if nested_definition is None:
                continue
            normalized_field, nested_error = _validate_writable_field(
                nested_code,
                nested_field,
                nested_definition,
            )
            if nested_error:
                return None, nested_error
            normalized_row["value"][nested_code] = normalized_field
        rows.append(normalized_row)
    return {"type": expected_type, "value": rows}, None


def _normalize_record(
    record: Any,
    *,
    require_required_fields: bool,
) -> tuple[dict[str, dict[str, Any]] | None, JSONResponse | None]:
    if not isinstance(record, dict):
        return None, _error(400, "MOCK_RE02", "record must be an object")

    normalized: dict[str, dict[str, Any]] = {}
    for field_code, field in record.items():
        definition = KINTONE_APP_FIELDS.get(field_code)
        if definition is None:
            continue
        normalized_field, validation_error = _validate_writable_field(
            field_code,
            field,
            definition,
        )
        if validation_error:
            return None, validation_error
        normalized[field_code] = normalized_field

    if require_required_fields:
        for field_code, definition in KINTONE_APP_FIELDS.items():
            if not definition.get("required"):
                continue
            field = normalized.get(field_code)
            if field is None or _is_empty_value(field["value"]):
                return None, _error(400, "MOCK_RE02", f"{field_code} is required")
    return normalized, None


async def _check_token(
    api_token: str | None = Header(default=None, alias="X-Cybozu-API-Token"),
) -> JSONResponse | None:
    return _authorize(api_token)


@app.get("/k/v1/record.json")
async def get_record(
    app: str = Query(...),
    id: str = Query(...),
    api_token: str | None = Header(default=None, alias="X-Cybozu-API-Token"),
):
    auth_error = _authorize(api_token)
    if auth_error:
        return auth_error
    records = _get_app_records(app)
    if records is None or id not in records:
        return _error(404, "GAIA_RE01", "Record was not found")
    return {"record": deepcopy(records[id])}


@app.get("/k/v1/records.json")
async def get_records(
    app: str = Query(...),
    query: str = "",
    fields: Annotated[list[str] | None, Query()] = None,
    total_count: Annotated[bool, Query(alias="totalCount")] = False,
    api_token: str | None = Header(default=None, alias="X-Cybozu-API-Token"),
):
    auth_error = _authorize(api_token)
    if auth_error:
        return auth_error
    records = _select_records(app, query, fields)
    if records is None:
        return _error(404, "GAIA_AP01", "App was not found")
    response: dict[str, Any] = {"records": records}
    if total_count:
        response["totalCount"] = str(len(records))
    return response


@app.post("/k/v1/records.json")
async def post_records(
    payload: Annotated[dict[str, Any], Body()],
    x_http_method_override: str | None = Header(
        default=None, alias="X-HTTP-Method-Override"
    ),
    api_token: str | None = Header(default=None, alias="X-Cybozu-API-Token"),
):
    auth_error = _authorize(api_token)
    if auth_error:
        return auth_error
    if (x_http_method_override or "").upper() == "GET":
        app_id = _app_id(payload.get("app", ""))
        records = _select_records(
            app_id,
            str(payload.get("query", "")),
            payload.get("fields"),
        )
        if records is None:
            return _error(404, "GAIA_AP01", "App was not found")
        response: dict[str, Any] = {"records": records}
        if payload.get("totalCount"):
            response["totalCount"] = str(len(records))
        return response

    app_id = _app_id(payload.get("app", ""))
    if app_id not in _records:
        _records[app_id] = {}
        _next_ids[app_id] = 1
        _revisions[app_id] = 1
    source_records = payload.get("records", [])
    if not isinstance(source_records, list):
        return _error(400, "MOCK_RE02", "records must be an array")
    normalized_records: list[dict[str, dict[str, Any]]] = []
    for source_record in source_records:
        normalized_record, validation_error = _normalize_record(
            source_record,
            require_required_fields=True,
        )
        if validation_error:
            return validation_error
        normalized_records.append(normalized_record or {})
    ids: list[str] = []
    revisions: list[str] = []
    for normalized_record in normalized_records:
        record_id = _next_record_id(app_id)
        revision = _bump_revision(app_id)
        _records[app_id][record_id] = {
            **_system_fields(record_id, revision),
            **deepcopy(normalized_record),
        }
        ids.append(record_id)
        revisions.append(str(revision))
    return {"ids": ids, "revisions": revisions}


@app.put("/k/v1/record.json")
async def update_record(
    payload: SingleRecordUpdate,
    api_token: str | None = Header(default=None, alias="X-Cybozu-API-Token"),
):
    auth_error = _authorize(api_token)
    if auth_error:
        return auth_error
    app_id = _app_id(payload.app)
    record, record_id = _record_update_target(
        app_id,
        RecordUpdate(
            id=payload.id,
            updateKey=payload.updateKey,
            record=payload.record,
        ),
    )
    if record is None or record_id is None:
        return _error(404, "GAIA_RE01", "Record was not found")
    normalized_record, validation_error = _normalize_record(
        payload.record,
        require_required_fields=False,
    )
    if validation_error:
        return validation_error
    revision = _bump_revision(app_id)
    record.update(deepcopy(normalized_record or {}))
    record.update(_system_fields(record_id, revision))
    return {"revision": str(revision)}


@app.put("/k/v1/records.json")
async def update_records(
    payload: RecordsUpdate,
    api_token: str | None = Header(default=None, alias="X-Cybozu-API-Token"),
):
    auth_error = _authorize(api_token)
    if auth_error:
        return auth_error
    app_id = _app_id(payload.app)
    if _get_app_records(app_id) is None:
        return _error(404, "GAIA_AP01", "App was not found")
    normalized_updates: list[
        tuple[dict[str, Any], str, dict[str, dict[str, Any]]]
    ] = []
    for update in payload.records:
        record, record_id = _record_update_target(app_id, update)
        if record is None or record_id is None:
            return _error(404, "GAIA_RE01", "Record was not found")
        normalized_record, validation_error = _normalize_record(
            update.record,
            require_required_fields=False,
        )
        if validation_error:
            return validation_error
        normalized_updates.append((record, record_id, normalized_record or {}))

    updated_records: list[dict[str, str]] = []
    for record, record_id, normalized_record in normalized_updates:
        revision = _bump_revision(app_id)
        record.update(deepcopy(normalized_record))
        record.update(_system_fields(record_id, revision))
        updated_records.append({"id": record_id, "revision": str(revision)})
    return {"records": updated_records}


@app.delete("/k/v1/records.json")
async def delete_records(
    payload: RecordsDelete,
    api_token: str | None = Header(default=None, alias="X-Cybozu-API-Token"),
):
    auth_error = _authorize(api_token)
    if auth_error:
        return auth_error
    app_id = _app_id(payload.app)
    records = _get_app_records(app_id)
    if records is None or any(str(record_id) not in records for record_id in payload.ids):
        return _error(404, "GAIA_RE01", "Record was not found")
    for record_id in payload.ids:
        records.pop(str(record_id))
    return {}


@app.get("/k/v1/app.json")
async def get_app(
    app: str = Query(...),
    api_token: str | None = Header(default=None, alias="X-Cybozu-API-Token"),
):
    auth_error = _authorize(api_token)
    if auth_error:
        return auth_error
    if _get_app_records(app) is None:
        return _error(404, "GAIA_AP01", "App was not found")
    return {
        "app": app,
        "name": "OCR Mock App",
        "description": "Local Kintone-compatible app for MCP smoke tests",
        "modifiedAt": "2026-01-01T00:00:00Z",
        "isGuest": False,
    }


@app.get("/k/v1/app/form/fields.json")
async def get_app_form_fields(
    app: str = Query(...),
    api_token: str | None = Header(default=None, alias="X-Cybozu-API-Token"),
):
    auth_error = _authorize(api_token)
    if auth_error:
        return auth_error
    if _get_app_records(app) is None:
        return _error(404, "GAIA_AP01", "App was not found")
    return {"properties": deepcopy(KINTONE_APP_FIELDS), "revision": "1"}


@app.get("/k/v1/apps.json")
async def get_apps(
    offset: int = 0,
    limit: int = 100,
    api_token: str | None = Header(default=None, alias="X-Cybozu-API-Token"),
):
    auth_error = _authorize(api_token)
    if auth_error:
        return auth_error
    app_ids = sorted(_records)[offset : offset + limit]
    return {
        "apps": [
            {"appId": app_id, "code": "ocr_mock", "name": "OCR Mock App"}
            for app_id in app_ids
        ]
    }


@app.get("/__mock/health")
def mock_health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/__mock/errors/{status_code}")
def mock_error(status_code: int) -> JSONResponse:
    if status_code not in {401, 404, 409, 429, 500}:
        return _error(400, "MOCK_400", "unsupported simulated status")
    headers = {"Retry-After": "1"} if status_code == 429 else None
    return _error(
        status_code,
        f"MOCK_{status_code}",
        f"simulated status {status_code}",
        headers=headers,
    )


@app.get("/__mock/requests")
def mock_requests() -> dict[str, Any]:
    with _state_lock:
        return {"requests": deepcopy(_request_log)}


@app.post("/__mock/reset")
def mock_reset() -> dict[str, str]:
    _reset_state()
    return {"status": "reset"}


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local Kintone API mock server")
    parser.add_argument(
        "--host",
        default=os.environ.get("KINTONE_MOCK_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("KINTONE_MOCK_PORT", "9100")),
    )
    parser.add_argument(
        "--token",
        default=EXPECTED_TOKEN,
        help="API token accepted by the mock server",
    )
    return parser.parse_args()


def main() -> None:
    global EXPECTED_TOKEN
    args = parse_arguments()
    EXPECTED_TOKEN = args.token
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
