import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml

from src.services.openapi_mcp_codegen import write_generated_artifacts

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "input_data/openapi-spec-1/openapi.yaml"
OCR_FIXTURE_PATH = ROOT / "examples/ocr/kintone-transfer.json"
KINTONE_FIELD_TYPES = {
    "document_id": "SINGLE_LINE_TEXT",
    "text": "MULTI_LINE_TEXT",
    "status": "DROP_DOWN",
    "confidence": "NUMBER",
    "source_file": "SINGLE_LINE_TEXT",
    "note": "MULTI_LINE_TEXT",
}


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


def _to_kintone_record(source_record: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        field_code: {
            "type": KINTONE_FIELD_TYPES[field_code],
            "value": value,
        }
        for field_code, value in source_record.items()
        if field_code in KINTONE_FIELD_TYPES
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
    record_fields = [_to_kintone_record(record) for record in ocr_data["records"]]
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
        assert len(created["ids"]) == len(ocr_data["records"])
        assert len(created["revisions"]) == len(ocr_data["records"])
        record_ids = created["ids"]

        for record_id, source_record in zip(record_ids, ocr_data["records"]):
            record = _run_client(
                artifact_dir,
                server_url,
                "getRecord",
                {"app": ocr_data["app"], "id": int(record_id)},
                client_env,
            )
            for field_code, value in source_record.items():
                if field_code in KINTONE_FIELD_TYPES:
                    assert record["record"][field_code]["value"] == value

        updated = _run_client(
            artifact_dir,
            server_url,
            "putRecord",
            {
                "body": {
                    "app": ocr_data["app"],
                    "updateKey": {
                        "document_id": {"value": ocr_data["records"][0]["document_id"]}
                    },
                    "record": {"status": {"value": "processed"}},
                }
            },
            client_env,
        )
        assert int(updated["revision"]) >= 2

        processed = _run_client(
            artifact_dir,
            server_url,
            "getRecord",
            {"app": ocr_data["app"], "id": int(record_ids[0])},
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
                        "records": [_to_kintone_record(abnormal["record"])],
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