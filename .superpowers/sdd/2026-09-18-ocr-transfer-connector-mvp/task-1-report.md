# Task 1 Report

## Implementation Summary

Task 1 established the contract-first foundation for the isolated OCR transfer subsystem without changing the existing Azure/OpenAPI evaluation or MCP-generation APIs.

- Updated the authority requirements document to add the missing management APIs, operation-level scopes, `spec` or `spec_ref` registration semantics, and the concrete `target_schema_ref` syntax `openapi:#/<json-pointer>`.
- Added the canonical public JSON Schemas for OCR transfer payloads and mapping definitions.
- Added the canonical OpenAPI 3.1 contract covering transfer intake, transfer lifecycle actions, mapping preview, connector management, mapping management, and health checks.
- Added focused contract tests that pin required schema fields, mapping constraints, public paths, Problem Details usage, idempotency/correlation headers, and connector registration semantics.
- Updated Python dependencies with `uv` and refreshed `uv.lock`.

## Files Changed

- docs/OCR_TRANSFER_CONNECTOR_REQUIREMENTS.md
- docs/api/ocr-transfer-openapi.yaml
- schemas/ocr-transfer-v1.json
- schemas/mapping-v1.json
- tests/transfer/conftest.py
- tests/transfer/test_contracts.py
- pyproject.toml
- uv.lock

## RED

Command:

```bash
uv run pytest tests/transfer/test_contracts.py -q
```

Output:

```text
FFFFF                                                                    [100%]
=================================== FAILURES ===================================
_____________ test_transfer_schema_requires_connector_and_mapping ______________

tests/transfer/test_contracts.py:30: in test_transfer_schema_requires_connector_and_mapping
    Draft202012Validator(load_json("schemas/ocr-transfer-v1.json")).iter_errors(
tests/transfer/test_contracts.py:14: in load_json
    return json.loads((REPO_ROOT / path).read_text(encoding="utf-8"))
E   FileNotFoundError: [Errno 2] No such file or directory: '/home/aaa103529/project/openapi-to-mcp/schemas/ocr-transfer-v1.json'

________ test_mapping_schema_restricts_target_locations_and_schema_refs ________

tests/transfer/test_contracts.py:39: in test_mapping_schema_restricts_target_locations_and_schema_refs
    schema = load_json("schemas/mapping-v1.json")
tests/transfer/test_contracts.py:14: in load_json
    return json.loads((REPO_ROOT / path).read_text(encoding="utf-8"))
E   FileNotFoundError: [Errno 2] No such file or directory: '/home/aaa103529/project/openapi-to-mcp/schemas/mapping-v1.json'

____________ test_openapi_contract_exposes_transfer_and_admin_paths ____________

tests/transfer/test_contracts.py:85: in test_openapi_contract_exposes_transfer_and_admin_paths
    paths = load_yaml("docs/api/ocr-transfer-openapi.yaml")["paths"]
tests/transfer/test_contracts.py:18: in load_yaml
    return yaml.safe_load((REPO_ROOT / path).read_text(encoding="utf-8"))
E   FileNotFoundError: [Errno 2] No such file or directory: '/home/aaa103529/project/openapi-to-mcp/docs/api/ocr-transfer-openapi.yaml'

___ test_openapi_contract_uses_problem_details_headers_and_operation_scopes ____

tests/transfer/test_contracts.py:107: in test_openapi_contract_uses_problem_details_headers_and_operation_scopes
    document = load_yaml("docs/api/ocr-transfer-openapi.yaml")
tests/transfer/test_contracts.py:18: in load_yaml
    return yaml.safe_load((REPO_ROOT / path).read_text(encoding="utf-8"))
E   FileNotFoundError: [Errno 2] No such file or directory: '/home/aaa103529/project/openapi-to-mcp/docs/api/ocr-transfer-openapi.yaml'

__ test_openapi_connector_definition_accepts_spec_or_spec_ref_without_secrets __

tests/transfer/test_contracts.py:134: in test_openapi_connector_definition_accepts_spec_or_spec_ref_without_secrets
    schema = load_yaml("docs/api/ocr-transfer-openapi.yaml")["components"]["schemas"]["ConnectorDefinition"]
tests/transfer/test_contracts.py:18: in load_yaml
    return yaml.safe_load((REPO_ROOT / path).read_text(encoding="utf-8"))
E   FileNotFoundError: [Errno 2] No such file or directory: '/home/aaa103529/project/openapi-to-mcp/docs/api/ocr-transfer-openapi.yaml'

=========================== short test summary info ============================
FAILED tests/transfer/test_contracts.py::test_transfer_schema_requires_connector_and_mapping
FAILED tests/transfer/test_contracts.py::test_mapping_schema_restricts_target_locations_and_schema_refs
FAILED tests/transfer/test_contracts.py::test_openapi_contract_exposes_transfer_and_admin_paths
FAILED tests/transfer/test_contracts.py::test_openapi_contract_uses_problem_details_headers_and_operation_scopes
FAILED tests/transfer/test_contracts.py::test_openapi_connector_definition_accepts_spec_or_spec_ref_without_secrets
5 failed in 0.14s
```

## GREEN

Command:

```bash
uv run pytest tests/transfer/test_contracts.py -q
```

Output:

```text
.....                                                                    [100%]
5 passed in 0.12s
```

## Full Suite

Command:

```bash
uv run pytest -q
```

Output:

```text
......................                                                   [100%]
=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298
.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298
  /home/aaa103529/project/openapi-to-mcp/.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298: PydanticDeprecatedSince20: `json_encoders` is deprecated. See https://docs.pydantic.dev/2.11/concepts/serialization/#custom-serializers for alternatives. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.11/migration/
    warnings.warn(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
22 passed, 2 warnings in 1.69s
```

## Self-Review

- The public contract remains isolated to docs and schema artifacts; no existing runtime API surfaces under src were changed.
- `ConnectorDefinition` stays inside the OpenAPI components, matching the requirement that `schemas/connector-v1.json` must not exist.
- `target_schema_ref` is now explicit and machine-checkable as `openapi:#/<json-pointer>` in both the authority spec and the mapping JSON Schema.
- Operation-level OAuth scopes, `application/problem+json`, and idempotency/correlation headers are all pinned by focused tests.
- The contract intentionally accepts `spec` or `spec_ref` for connector registration and only `credential_ref` for secrets, preserving the public/stored-definition separation required by preflight.

## Concerns

- The full suite still emits two pre-existing Pydantic deprecation warnings unrelated to this task.
- The future export/check implementation must preserve these hand-authored canonical artifacts by default; Task 1 defines the artifacts but does not implement overwrite protection.

## Commit SHA

9aa541a94fa0a8c4b1f623555d5d9bed8dca877f

## Fix Round 1

### Findings Addressed

1. Tightened `ConnectorDefinition.additional_headers` so the MVP public contract accepts only the explicit non-secret header `X-Api-Version` via `value_ref`; inline `value` is no longer part of the public schema, and secret-bearing names such as `Authorization`, `Cookie`, `Proxy-Authorization`, and `X-Api-Key` are rejected by contract tests.
2. Removed `batch_upsert` from all Task 1 public operation enums in the OCR transfer schema, mapping schema, OpenAPI preview request, mapping list item contract, and the authority requirements text; the requirements now keep it only as a future extension note outside the MVP enums.
3. Relaxed the OCR payload schema so `ocr.fields` or `ocr.line_items` is sufficient at intake time via `anyOf`, while preserving later transfer-time validation as the place where empty extracted content is rejected.
4. Narrowed `storage_ref` and `text_ref` to the explicit approved opaque form `object://<namespace>/<opaque-id>` and added negative coverage for `file://` and `ftp://`.
5. Consolidated duplicated loader helpers into `tests/transfer/conftest.py` and made the contract tests consume the shared pytest fixture/helper surface.

### Files Changed

- docs/OCR_TRANSFER_CONNECTOR_REQUIREMENTS.md
- docs/api/ocr-transfer-openapi.yaml
- schemas/mapping-v1.json
- schemas/ocr-transfer-v1.json
- tests/transfer/conftest.py
- tests/transfer/test_contracts.py
- .superpowers/sdd/2026-09-18-ocr-transfer-connector-mvp/task-1-report.md

### Covering Tests

- `test_transfer_schema_allows_fields_only_or_line_items_only`
- `test_transfer_schema_rejects_non_opaque_storage_and_text_refs`
- `test_mapping_schema_restricts_target_locations_schema_refs_and_operations`
- `test_openapi_public_operation_enums_only_allow_mvp_operations`
- `test_openapi_connector_definition_accepts_spec_or_spec_ref_without_inline_secrets`

### Exact Commands And Outputs

Command:

```bash
uv run pytest tests/transfer/test_contracts.py -q
```

Output:

```text
.FFF..FF                                                                 [100%]
FAILED tests/transfer/test_contracts.py::test_transfer_schema_allows_fields_only_or_line_items_only
FAILED tests/transfer/test_contracts.py::test_transfer_schema_rejects_non_opaque_storage_and_text_refs
FAILED tests/transfer/test_contracts.py::test_mapping_schema_restricts_target_locations_schema_refs_and_operations
FAILED tests/transfer/test_contracts.py::test_openapi_public_operation_enums_only_allow_mvp_operations
FAILED tests/transfer/test_contracts.py::test_openapi_connector_definition_accepts_spec_or_spec_ref_without_inline_secrets
5 failed, 3 passed in 0.19s
```

Command:

```bash
uv run pytest tests/transfer/test_contracts.py -q
```

Output:

```text
........                                                                 [100%]
8 passed in 0.16s
```

Command:

```bash
uv run pytest -q
```

Output:

```text
.........................                                                [100%]
=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298
.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298
    /home/aaa103529/project/openapi-to-mcp/.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298: PydanticDeprecatedSince20: `json_encoders` is deprecated. See https://docs.pydantic.dev/2.11/concepts/serialization/#custom-serializers for alternatives. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.11/migration/
        warnings.warn(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
25 passed, 2 warnings in 1.29s
```

### Self-Review

- The fix stays entirely in hand-authored contract artifacts and transfer contract tests; no unrelated runtime code under `src` was changed.
- The secret-handling fix closes the public-schema gap rather than trying to blacklist a few bad literal values; the contract no longer exposes inline header values for connector registration.
- The OCR payload change now matches the authority spec: presence of either `fields` or `line_items` is an intake concern, while semantic emptiness remains a later validation concern.
- The opaque ref regex is intentionally narrow to the explicit approved form documented in the requirements, which blocks accidental external URL fetch semantics in the public intake contract.
- No finding conflicted with the authority specification after the requirements wording was tightened to the intended MVP surface.