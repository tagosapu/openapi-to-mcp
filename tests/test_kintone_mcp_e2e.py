import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml

from examples.kintone_stub_server import KINTONE_APP_FIELDS
from src.services.openapi_mcp_codegen import write_generated_artifacts

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "input_data/openapi-spec-1/openapi.yaml"
OCR_FIXTURE_PATH = ROOT / "examples/ocr/kintone-transfer.json"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output, _ = process.communicate()
            raise AssertionError(
                f"process exited before port opened: {output[-2000:]}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"port {port} did not open")


def _start_process(command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return process


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if process.stdout is not None:
        process.stdout.read()


def _field_payload(field_type: str, value: object) -> dict[str, object]:
    normalized_value = str(value) if field_type == "NUMBER" else value
    return {"type": field_type, "value": normalized_value}


def _kintone_field(field_code: str, value: object) -> dict[str, object]:
    return _field_payload(KINTONE_APP_FIELDS[field_code]["type"], value)


def _source_value(fields: dict[str, dict[str, object]], field_code: str) -> object:
    return fields[field_code]["value"]


def _to_kintone_record(document: dict[str, object]) -> dict[str, dict[str, object]]:
    ocr = document["ocr"]
    source_fields = ocr["fields"]
    line_items = []
    line_item_fields = KINTONE_APP_FIELDS["line_items"]["fields"]
    for source_item in ocr["line_items"]:
        source_item_fields = source_item["fields"]
        line_items.append(
            {
                "value": {
                    field_code: _field_payload(
                        line_item_fields[field_code]["type"],
                        _source_value(source_item_fields, field_code),
                    )
                    for field_code in line_item_fields
                }
            }
        )

    confidence = min(
        float(field["confidence"])
        for field in source_fields.values()
        if "confidence" in field
    )
    return {
        "invoice_number": _kintone_field(
            "invoice_number", _source_value(source_fields, "invoice_number")
        ),
        "invoice_date": _kintone_field(
            "invoice_date", _source_value(source_fields, "invoice_date")
        ),
        "vendor_name": _kintone_field(
            "vendor_name", _source_value(source_fields, "vendor_name")
        ),
        "subtotal": _kintone_field(
            "subtotal", _source_value(source_fields, "subtotal")
        ),
        "tax_amount": _kintone_field(
            "tax_amount", _source_value(source_fields, "tax_amount")
        ),
        "total_amount": _kintone_field(
            "total_amount", _source_value(source_fields, "total_amount")
        ),
        "currency": _kintone_field(
            "currency", _source_value(source_fields, "currency")
        ),
        "status": _kintone_field("status", "registered"),
        "ocr_confidence": _kintone_field("ocr_confidence", confidence),
        "source_file": _kintone_field(
            "source_file", document["document"]["content"]["filename"]
        ),
        "ocr_text_ref": _kintone_field("ocr_text_ref", ocr["text_ref"]),
        "line_items": {
            "type": "SUBTABLE",
            "value": line_items,
        },
    }


def _to_kintone_record_values(
    values: dict[str, object],
) -> dict[str, dict[str, object]]:
    return {
        field_code: _kintone_field(field_code, value)
        for field_code, value in values.items()
        if field_code in KINTONE_APP_FIELDS and field_code != "line_items"
    }


def _run_client(
    artifact_dir: Path,
    server_url: str,
    tool: str,
    arguments: dict[str, object],
    env: dict[str, str],
    expected_returncode: int = 0,
) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            "client.py",
            "--server-url",
            server_url,
            "--tool",
            tool,
            "--arguments",
            json.dumps(arguments),
        ],
        cwd=artifact_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == expected_returncode, (
        completed.stdout + completed.stderr
    )
    return json.loads(completed.stdout)


def test_generated_kintone_mcp_server_transfers_ocr_data(tmp_path):
    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    ocr_data = json.loads(OCR_FIXTURE_PATH.read_text(encoding="utf-8"))
    documents = ocr_data["documents"]
    record_fields = [_to_kintone_record(document) for document in documents]
    artifact_dir = tmp_path / "mcpserver"
    write_generated_artifacts(spec, artifact_dir)

    mock_port = _free_port()
    mcp_port = _free_port()
    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "PYTHONUNBUFFERED": "1",
    }
    token = "mock-token"

    mock_process = _start_process(
        [
            sys.executable,
            str(ROOT / "examples/kintone_stub_server.py"),
            "--host",
            "127.0.0.1",
            "--port",
            str(mock_port),
            "--token",
            token,
        ],
        ROOT,
        base_env,
    )
    _wait_for_port(mock_process, mock_port)

    server_env = {
        **base_env,
        "API_BASE_URL": f"http://127.0.0.1:{mock_port}",
        "KINTONE_API_TOKEN": token,
        "MCP_SERVER_LISTEN_PORT": str(mcp_port),
    }
    mcp_process = _start_process(
        [
            sys.executable,
            "server.py",
            "--port",
            str(mcp_port),
            "--transport",
            "streamable-http",
            "--base-url",
            f"http://127.0.0.1:{mock_port}",
        ],
        artifact_dir,
        server_env,
    )
    try:
        _wait_for_port(mcp_process, mcp_port)
        client_env = {**base_env, "NO_PROXY": "127.0.0.1,localhost,::1"}
        server_url = f"http://127.0.0.1:{mcp_port}/mcp/"

        target_schema = _run_client(
            artifact_dir,
            server_url,
            "getAppFormFields",
            {"app": ocr_data["app"]},
            client_env,
        )
        assert "document_id" not in target_schema["properties"]
        assert target_schema["properties"]["line_items"]["type"] == "SUBTABLE"

        created = _run_client(
            artifact_dir,
            server_url,
            "postRecords",
            {
                "body": {
                    "app": ocr_data["app"],
                    "records": record_fields,
                }
            },
            client_env,
        )
        assert len(created["ids"]) == len(documents)
        assert len(created["revisions"]) == len(documents)
        record_ids = created["ids"]

        for record_id, source_document, expected_record in zip(
            record_ids,
            documents,
            record_fields,
        ):
            record = _run_client(
                artifact_dir,
                server_url,
                "getRecord",
                {"app": ocr_data["app"], "id": int(record_id)},
                client_env,
            )
            for field_code, expected_field in expected_record.items():
                actual_field = record["record"][field_code]
                if field_code == "line_items":
                    actual_rows = actual_field["value"]
                    expected_rows = expected_field["value"]
                    assert len(actual_rows) == len(expected_rows)
                    for actual_row, expected_row in zip(actual_rows, expected_rows):
                        assert actual_row["id"].isdigit()
                        assert actual_row["value"] == expected_row["value"]
                else:
                    assert actual_field == expected_field
            assert "document_id" not in record["record"]
            assert (
                record["record"]["invoice_number"]["value"]
                == source_document["ocr"]["fields"]["invoice_number"]["value"]
            )

        updated = _run_client(
            artifact_dir,
            server_url,
            "putRecords",
            {
                "body": {
                    "app": ocr_data["app"],
                    "records": [
                        {
                            "updateKey": {
                                "field": "invoice_number",
                                "value": document["ocr"]["fields"][
                                    "invoice_number"
                                ]["value"],
                            },
                            "record": {"status": {"value": "processed"}},
                        }
                        for document in documents
                    ],
                }
            },
            client_env,
        )
        assert [item["id"] for item in updated["records"]] == record_ids
        assert all(int(item["revision"]) >= 2 for item in updated["records"])

        for record_id in record_ids:
            processed = _run_client(
                artifact_dir,
                server_url,
                "getRecord",
                {"app": ocr_data["app"], "id": int(record_id)},
                client_env,
            )
            assert processed["record"]["status"]["value"] == "processed"

        for abnormal in ocr_data["abnormal_records"]:
            invalid = _run_client(
                artifact_dir,
                server_url,
                "postRecords",
                {
                    "body": {
                        "app": ocr_data["app"],
                        "records": [
                            _to_kintone_record_values(abnormal["record"])
                        ],
                    }
                },
                client_env,
                expected_returncode=1,
            )
            expected = abnormal["expected_error"]
            assert invalid == {
                "error": {
                    "kind": "http",
                    "operation_id": "postRecords",
                    "status_code": expected["status_code"],
                    "target_code": expected["target_code"],
                    "message": expected["message"],
                }
            }

        deleted = _run_client(
            artifact_dir,
            server_url,
            "deleteRecords",
            {"body": {"app": ocr_data["app"], "ids": [int(record_id) for record_id in record_ids]}},
            client_env,
        )
        assert deleted == {}

        missing = _run_client(
            artifact_dir,
            server_url,
            "getRecord",
            {"app": ocr_data["app"], "id": int(record_ids[0])},
            client_env,
            expected_returncode=1,
        )
        assert missing == {
            "error": {
                "kind": "http",
                "operation_id": "getRecord",
                "status_code": 404,
                "target_code": "GAIA_RE01",
                "message": "Record was not found",
            }
        }

        unreachable_port = _free_port()
        error_mcp_port = _free_port()
        while error_mcp_port == unreachable_port:
            error_mcp_port = _free_port()
        error_process = _start_process(
            [
                sys.executable,
                "server.py",
                "--port",
                str(error_mcp_port),
                "--transport",
                "streamable-http",
                "--base-url",
                f"http://127.0.0.1:{unreachable_port}",
            ],
            artifact_dir,
            {**server_env, "MCP_SERVER_LISTEN_PORT": str(error_mcp_port)},
        )
        try:
            _wait_for_port(error_process, error_mcp_port)
            connection_error = _run_client(
                artifact_dir,
                f"http://127.0.0.1:{error_mcp_port}/mcp/",
                "getRecord",
                {"app": 1, "id": 1},
                client_env,
                expected_returncode=1,
            )
        finally:
            _stop_process(error_process)

        assert connection_error == {
            "error": {
                "kind": "connection",
                "operation_id": "getRecord",
                "message": "could not connect to target API",
            }
        }

        history = httpx.get(
            f"http://127.0.0.1:{mock_port}/__mock/requests",
            timeout=5,
            trust_env=False,
        ).json()["requests"]
        assert any(item["has_api_token"] for item in history)
        assert token not in json.dumps(history)
    finally:
        _stop_process(mcp_process)
        _stop_process(mock_process)