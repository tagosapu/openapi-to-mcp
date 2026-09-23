from __future__ import annotations

from scripts.export_transfer_contract import contract_differences
from src.transfer.app import create_app
from src.transfer.models import MappingDefinition, TransferRequest
from tests.transfer.conftest import load_json, load_yaml, settings_factory


def test_documented_paths_match_fastapi_paths(tmp_path) -> None:
    documented = load_yaml("docs/api/ocr-transfer-openapi.yaml")["paths"]
    settings = settings_factory(database_path=str(tmp_path / "contract.sqlite3"))
    generated = create_app(settings).openapi()["paths"]

    assert set(documented) == set(generated)
    assert set(documented["/v1/transfers"]) >= {"post", "get"}
    assert generated["/v1/transfers"]["post"]["responses"]["202"]


def test_generated_contract_matches_security_statuses_and_model_shapes(tmp_path) -> None:
    documented = load_yaml("docs/api/ocr-transfer-openapi.yaml")
    generated = create_app(
        settings_factory(database_path=str(tmp_path / "contract.sqlite3"))
    ).openapi()

    differences = contract_differences(
        documented,
        generated,
        load_json("schemas/ocr-transfer-v1.json"),
        TransferRequest.model_json_schema(),
        load_json("schemas/mapping-v1.json"),
        MappingDefinition.model_json_schema(),
    )

    assert differences == []


def test_generated_contract_detects_nested_schema_drift(tmp_path) -> None:
    documented = load_yaml("docs/api/ocr-transfer-openapi.yaml")
    generated = create_app(
        settings_factory(database_path=str(tmp_path / "contract.sqlite3"))
    ).openapi()
    canonical_ocr = load_json("schemas/ocr-transfer-v1.json")
    canonical_ocr["$defs"]["documentContent"]["properties"]["filename"]["minLength"] = 2

    differences = contract_differences(
        documented,
        generated,
        canonical_ocr,
        TransferRequest.model_json_schema(),
        load_json("schemas/mapping-v1.json"),
        MappingDefinition.model_json_schema(),
    )

    assert "nested schema semantics mismatch for TransferRequest" in differences


def test_generated_contract_detects_operation_metadata_drift(tmp_path) -> None:
    documented = load_yaml("docs/api/ocr-transfer-openapi.yaml")
    generated = create_app(
        settings_factory(database_path=str(tmp_path / "contract.sqlite3"))
    ).openapi()
    documented["paths"]["/v1/transfers"]["post"]["parameters"][0]["schema"]["minLength"] = 2
    documented["paths"]["/v1/transfers"]["post"]["requestBody"]["content"] = {
        "application/problem+json": {}
    }
    documented["paths"]["/v1/transfers"]["post"]["responses"]["202"]["content"] = {
        "application/problem+json": {}
    }

    differences = contract_differences(
        documented,
        generated,
        load_json("schemas/ocr-transfer-v1.json"),
        TransferRequest.model_json_schema(),
        load_json("schemas/mapping-v1.json"),
        MappingDefinition.model_json_schema(),
    )

    assert "parameters mismatch for POST /v1/transfers" in differences
    assert "request body mismatch for POST /v1/transfers" in differences
    assert "response media type mismatch for POST /v1/transfers 202" in differences