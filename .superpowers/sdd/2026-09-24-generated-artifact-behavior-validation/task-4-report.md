# Task 4 Report

## Summary

Implemented `verify_generated_artifacts()` in [src/services/generated_artifact_verifier.py](src/services/generated_artifact_verifier.py) and added focused coverage in [tests/test_generated_artifact_verifier.py](tests/test_generated_artifact_verifier.py).

The verifier now:

- Generates deterministic success scenarios from the OpenAPI spec.
- Starts the local `OpenAPIMockServer` and the generated `server.py` over `streamable-http`.
- Invokes each generated tool through the generated `client.py`.
- Activates the selected scenario before each invocation.
- Validates outbound method, expanded path, query, and request body.
- Validates success responses with `jsonschema`.
- Marks missing response schemas as `unvalidated` rather than passing.
- Preserves per-operation results even when another operation fails.
- Redacts MCP error output down to structured metadata.

## RED Evidence

Initial failing test command:

```bash
uv run pytest -q tests/test_generated_artifact_verifier.py::test_verifier_calls_each_generated_tool_against_local_mock
```

Observed failure:

```text
ModuleNotFoundError: No module named 'src.services.generated_artifact_verifier'
```

During iteration, focused verifier tests also exposed two important behavior mismatches that were then codified in the tests:

- `examples/minimal_users_api.yaml` has no documented response schema, so the correct Task 4 result is `unvalidated`, not `passed`.
- Generated artifacts currently fail real MCP execution for `204` and non-dict return payloads because generated tool functions are annotated as returning `dict[str, Any]`; the verifier now reports that breakage instead of dropping other operation results.

## GREEN Evidence

Focused verifier suite:

```bash
uv run pytest -q tests/test_generated_artifact_verifier.py
```

Result:

```text
4 passed in 6.37s
```

Task 4 required regression suite:

```bash
uv run pytest -q tests/test_generated_artifact_verifier.py tests/test_generated_contract.py tests/test_openapi_mcp_codegen.py
```

Result:

```text
10 passed in 8.93s
```

Static check:

- `get_errors` reported no diagnostics for [src/services/generated_artifact_verifier.py](src/services/generated_artifact_verifier.py) and [tests/test_generated_artifact_verifier.py](tests/test_generated_artifact_verifier.py).

## Files Changed

- [src/services/generated_artifact_verifier.py](src/services/generated_artifact_verifier.py)
- [tests/test_generated_artifact_verifier.py](tests/test_generated_artifact_verifier.py)
- [.superpowers/sdd/2026-09-24-generated-artifact-behavior-validation/task-4-report.md](.superpowers/sdd/2026-09-24-generated-artifact-behavior-validation/task-4-report.md)

## Self-Review

- Kept the change isolated to verifier orchestration and focused tests.
- Reused the existing subprocess lifecycle pattern from [tests/test_kintone_mcp_e2e.py](tests/test_kintone_mcp_e2e.py).
- Ensured mock server and child processes are always cleaned up in `finally` blocks.
- Avoided storing raw subprocess stdout/stderr in the verification result.
- Used operation-level result objects so one failure does not erase other successful verifications.

## Concerns

- Real MCP execution currently cannot successfully return `204` or non-dict success payloads from generated tools because generated tool signatures return `dict[str, Any]`. Task 4 verifier coverage now exposes that behavior, but the generator/runtime path itself remains unchanged.
- Request-side `unvalidated_request_schema` coverage is not exercised yet; Task 4 only needed the focused orchestration cases from the brief.

## Fix Report

### Review Finding 1: Importing the verifier still performed eager package work

Root cause:

- [src/__init__.py](src/__init__.py) eagerly imported [src/cli.py](src/cli.py), which pulled the CLI dependency graph into any `src.*` import.
- Importing [src.services.generated_artifact_verifier](src/services/generated_artifact_verifier.py) also initialized [src/services/__init__.py](src/services/__init__.py), which eagerly imported [src/services/llm_client.py](src/services/llm_client.py). That transitively imported `litellm`, and `litellm` attempted an HTTP GET to `raw.githubusercontent.com` at import time.

Fix:

- Made [src/__init__.py](src/__init__.py) export `main_cli` lazily via `__getattr__`.
- Made [src/services/__init__.py](src/services/__init__.py) lazily export LLM/enhancer symbols so importing verifier/models/codegen does not initialize LiteLLM.
- Added a subprocess regression test in [tests/test_generated_artifact_verifier.py](tests/test_generated_artifact_verifier.py) that blocks socket calls, imports `src.services.generated_artifact_verifier`, and asserts both `src.cli` and network attempts stay absent.

### Review Finding 2: Raw secret values could persist in recorded query/body data

Root cause:

- [src/services/openapi_mock_server.py](src/services/openapi_mock_server.py) only redacted by sensitive key names, so exact secret values under opaque keys and ordinary query names could still be serialized into `RecordedRequest` and verifier reports.

Fix:

- Extended `OpenAPIMockServer(..., secret_values=())` to redact:
	- sensitive query keys by name
	- exact matching secret values in query values, header values, and JSON bodies
	- exact matching secret values recursively under opaque/non-sensitive body keys
- Split mock-server request storage into raw requests for internal validation and redacted requests for persisted/reportable data.
- Updated [src/services/generated_artifact_verifier.py](src/services/generated_artifact_verifier.py) to pass only secret-like environment values whose names match `TOKEN|SECRET|PASSWORD|API_KEY|AUTH` into the mock server.
- Added focused regressions in [tests/test_openapi_mock_server.py](tests/test_openapi_mock_server.py) and [tests/test_generated_artifact_verifier.py](tests/test_generated_artifact_verifier.py) proving serialized `RecordedRequest` and verification report output do not contain the raw query/body secret values.

### Validation

Focused regression command:

```bash
uv run pytest -q tests/test_generated_artifact_verifier.py::test_importing_verifier_does_not_import_cli_or_attempt_network tests/test_generated_artifact_verifier.py::test_verifier_redacts_secret_query_and_opaque_body_values_in_report tests/test_openapi_mock_server.py::test_mock_server_redacts_secret_values_in_query_headers_and_opaque_body_fields
```

Output:

```text
...                                                                      [100%]
3 passed in 2.19s
```

Covering focused suite:

```bash
uv run pytest -q tests/test_generated_artifact_verifier.py tests/test_openapi_mock_server.py tests/test_generated_contract.py tests/test_openapi_mcp_codegen.py
```

Output:

```text
..................                                                       [100%]
18 passed in 10.00s
```

Diagnostics:

- `get_errors` reported no diagnostics for the modified files.