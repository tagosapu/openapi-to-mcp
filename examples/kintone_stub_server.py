#!/usr/bin/env python3
"""Local Kintone-compatible stub for testing generated MCP servers."""

from __future__ import annotations

import argparse
import os
import re
from copy import deepcopy
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
_TRANSFER_STATUSES = {"registered", "needs_review", "processed"}


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
                "ocr_id": {"type": "SINGLE_LINE_TEXT", "value": "ocr-001"},
                "status": {"type": "DROP_DOWN", "value": "registered"},
                "text": {"type": "MULTI_LINE_TEXT", "value": "Mock OCR text"},
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
        field_code, expected = next(iter(update.updateKey.items()))
        expected_value = expected.get("value") if isinstance(expected, dict) else expected
        for record_id, record in records.items():
            if str(record.get(field_code, {}).get("value", "")) == str(expected_value):
                return record, record_id
    return None, None


def _validate_transfer_record(record: Any) -> JSONResponse | None:
    if not isinstance(record, dict):
        return _error(400, "MOCK_RE02", "record must be an object")
    if not any(
        field_code in record
        for field_code in ("document_id", "text", "confidence", "source_file", "note")
    ):
        return None

    for field_code in ("document_id", "text", "status"):
        field = record.get(field_code)
        value = field.get("value") if isinstance(field, dict) else None
        if not isinstance(value, str) or not value.strip():
            return _error(400, "MOCK_RE02", f"{field_code} is required")

    status_value = record["status"]["value"]
    if status_value not in _TRANSFER_STATUSES:
        allowed = ", ".join(sorted(_TRANSFER_STATUSES - {"processed"}, key=lambda value: (value != "registered", value)))
        allowed = f"{allowed}, processed"
        return _error(
            400,
            "MOCK_RE03",
            f"status must be one of: {allowed}",
        )

    confidence = record.get("confidence")
    if confidence is not None:
        confidence_value = confidence.get("value") if isinstance(confidence, dict) else None
        if isinstance(confidence_value, bool):
            return _error(400, "MOCK_RE03", "confidence must be between 0 and 1")
        try:
            numeric_confidence = float(confidence_value)
        except (TypeError, ValueError):
            return _error(400, "MOCK_RE03", "confidence must be between 0 and 1")
        if not 0 <= numeric_confidence <= 1:
            return _error(400, "MOCK_RE03", "confidence must be between 0 and 1")
    return None


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
    for source_record in source_records:
        validation_error = _validate_transfer_record(source_record)
        if validation_error:
            return validation_error
    ids: list[str] = []
    revisions: list[str] = []
    for source_record in source_records:
        record_id = _next_record_id(app_id)
        revision = _bump_revision(app_id)
        _records[app_id][record_id] = {
            **_system_fields(record_id, revision),
            **deepcopy(source_record),
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
    revision = _bump_revision(app_id)
    record.update(deepcopy(payload.record))
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
    revisions: list[str] = []
    for update in payload.records:
        record, record_id = _record_update_target(app_id, update)
        if record is None or record_id is None:
            return _error(404, "GAIA_RE01", "Record was not found")
        revision = _bump_revision(app_id)
        record.update(deepcopy(update.record))
        record.update(_system_fields(record_id, revision))
        revisions.append(str(revision))
    return {"revisions": revisions}


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
