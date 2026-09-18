# Task 4 Report

## Implementation Summary

Task 4 added the OpenAPI contract preflight, credential/auth helpers, connector protocol models, a REST OpenAPI connector, and registry orchestration for validated connector registration and execution. The implementation keeps mapping syntax unchanged, reuses Task 2 and Task 3 public models, blocks external `$ref`, enforces operation-level security metadata, validates request schemas, extracts postcondition result IDs, supports API key/Bearer/Basic/OAuth2 auth including one expired-401 token refresh, redacts secrets from repr output, adds Fernet payload protection factory support, and enforces SSRF checks at registration and immediately before send.

## Files

- src/transfer/auth.py
- src/transfer/connector.py
- src/transfer/openapi_contract.py
- src/transfer/rest_connector.py
- src/transfer/store.py
- src/transfer/__init__.py
- tests/transfer/fixtures/relative-server.yaml
- tests/transfer/fixtures/operation-security.yaml
- tests/transfer/test_openapi_contract.py
- tests/transfer/test_rest_connector.py

## RED Output

Command:

```text
uv run pytest tests/transfer/test_openapi_contract.py tests/transfer/test_rest_connector.py -q
```

Output:

```text
no tests ran in 0.00s
ERROR: file or directory not found: tests/transfer/test_openapi_contract.py
```

## GREEN Output

Command:

```text
uv run pytest tests/transfer/test_openapi_contract.py tests/transfer/test_rest_connector.py -q
```

Output:

```text
.....................                                                    [100%]
=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298
.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298
  /home/aaa103529/project/openapi-to-mcp/.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298: PydanticDeprecatedSince20: `json_encoders` is deprecated. See https://docs.pydantic.dev/2.11/concepts/serialization/#custom-serializers for alternatives. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.11/migration/
    warnings.warn(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
21 passed, 2 warnings in 1.51s
```

## Full Suite Output

Command:

```text
uv run pytest -q
```

Output:

```text
........................................................................ [ 80%]
..................                                                       [100%]
=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298
.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298
  /home/aaa103529/project/openapi-to-mcp/.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298: PydanticDeprecatedSince20: `json_encoders` is deprecated. See https://docs.pydantic.dev/2.11/concepts/serialization/#custom-serializers for alternatives. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.11/migration/
    warnings.warn(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
90 passed, 2 warnings in 2.20s
```

## Self-Review

- Contract preflight stores normalized operation metadata and allows mapping-time schema resolution for component and operation request schema targets.
- REST send paths keep at-least-once semantics by classifying post-send timeouts as unknown and avoiding blind automatic retries.
- Reconciliation uses registered lookup metadata only and extracts validated string IDs via the configured postcondition pointer.
- Secret-bearing values are hidden from repr output for the new request and secret bundle models.
- Payload protection remains optional for Task 2 tests, while create_payload_protector enforces a valid Fernet key when used for production wiring.

## Concerns

- JwtAuthorizer is implemented to the required interface but is not yet exercised by the current Task 4 tests.
- ConnectorRegistry currently creates a fresh AsyncClient per get call; that is acceptable for this unit-tested MVP surface, but Task 6 app wiring should own lifecycle reuse.

## Commit SHA

Implementation commit: 6e256ec61cae342d16691939aff22d869f2965a6

## Reviewer Fix Round

### Findings Addressed

1. Same tenant/connector/version is now immutable for materially different normalized spec/config, while identical re-registration is idempotent. Stored `spec_hash` now matches the canonical public OpenAPI snapshot and excludes internal registration metadata.
2. Outbound URL construction now preserves any base path prefix from the configured base URL, so `/v1` style server prefixes are retained when joining operation paths.
3. Response bodies are now read with a byte bound during streaming instead of materializing the full body first, and `total_timeout_seconds` is enforced as a real end-to-end deadline across request dispatch and body read. Total/read timeout remains classified as `unknown`.
4. Successful responses with malformed JSON or schema-incompatible bodies now become `delivery_state=unknown` with internal code `RESPONSE_FORMAT_UNKNOWN` instead of flowing into `RESPONSE_ID_INVALID` as a received response.
5. Reconcile lookup now resolves the OpenAPI parameter definition, decodes the canonical scalar, validates/coerces it against primitive schemas including boolean and integer, rejects invalid/object/array values, and only dispatches correctly encoded path/query parameters.
6. Contract preflight now rejects operations that omit effective operation-level security, omit a success response schema, or omit representative 4xx/5xx responses.

### Commands

Command:

```text
uv run pytest tests/transfer/test_openapi_contract.py tests/transfer/test_rest_connector.py tests/transfer/test_store.py -q
```

Output:

```text
.............................................                            [100%]
=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298
.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298
  /home/aaa103529/project/openapi-to-mcp/.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298: PydanticDeprecatedSince20: `json_encoders` is deprecated. See https://docs.pydantic.dev/2.11/concepts/serialization/#custom-serializers for alternatives. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.11/migration/
    warnings.warn(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
45 passed, 2 warnings in 2.59s
```

Command:

```text
uv run pytest -q
```

Output:

```text
........................................................................ [ 69%]
...............................                                          [100%]
=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298
.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298
  /home/aaa103529/project/openapi-to-mcp/.venv/lib/python3.13/site-packages/pydantic/_internal/_generate_schema.py:298: PydanticDeprecatedSince20: `json_encoders` is deprecated. See https://docs.pydantic.dev/2.11/concepts/serialization/#custom-serializers for alternatives. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.11/migration/
    warnings.warn(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
103 passed, 2 warnings in 2.57s
```

### Files

- src/transfer/openapi_contract.py
- src/transfer/rest_connector.py
- src/transfer/store.py
- tests/transfer/test_openapi_contract.py
- tests/transfer/test_rest_connector.py
- .superpowers/sdd/2026-09-18-ocr-transfer-connector-mvp/task-4-report.md

### Self-Review

- Canonical contract hashing and version immutability now share the same normalization rule, so internal registration metadata no longer changes the public contract identity.
- The connector now distinguishes between a delivered response that is intentionally truncated due to size policy and a response whose success body is too malformed or incompatible to safely classify as `received`.
- Reconcile coercion is intentionally limited to primitive OpenAPI parameter schemas; object and array lookups still fail closed.

### Concerns

- Existing Pydantic `json_encoders` deprecation warnings remain in the suite and were not changed in this fix round.
- Response schema compatibility is currently validated against the top-level normalized success schema; broader pointer/schema traversal beyond the reviewer findings remains deferred as requested.