# Task 3 Report: Declarative Mapping And Preview Transform

## Files

- src/transfer/mapping.py
- src/transfer/__init__.py
- tests/transfer/test_mapping.py

## TDD Execution

### RED

Command:

```bash
uv run pytest tests/transfer/test_mapping.py -q
```

Output:

```text
ERROR tests/transfer/test_mapping.py
ModuleNotFoundError: No module named 'src.transfer.mapping'
```

### GREEN

Command:

```bash
uv run pytest tests/transfer/test_mapping.py -q
```

Output:

```text
23 passed, 2 warnings in 1.15s
```

### Full Suite

Command:

```bash
uv run pytest -q
```

Output:

```text
64 passed, 2 warnings in 1.87s
```

## What Changed

- Added MappingEngine with three required entry points: apply_corrections, preview, and apply.
- Implemented declarative JSON Pointer resolution over TransferRequest.model_dump(mode="json").
- Implemented allowed transforms: trim, lower, upper, to_string, to_integer, to_number, to_date, to_datetime, currency_amount, and concat.
- Implemented conditional rules, default fallback, enum_map application before type conversion, duplicate target rejection, and operation mismatch validation.
- Implemented HTTP placement for path, query, header, and body targets with forbidden-header enforcement.
- Implemented review issue generation for low confidence and reviewable OCR statuses.
- Implemented correction overlay via deep copy without mutating the original TransferRequest.
- Implemented deduplication key path matching plus canonical JSON SHA-256 idempotency key generation from document_id and deduplication_value.
- Added focused tests for placement, forbidden headers, missing/default/enum/condition behavior, concat, line item arrays, type conversion, duplicate targets, unknown pointers, low-confidence issues, correction overlay, deduplication rejection, and canonical idempotency hashing.

## Self-Review

- The implementation stays within the Task 2 public models and does not change existing Azure/OpenAPI or MCP generation APIs.
- The engine is declarative only: no arbitrary code execution, no template execution, no HTTP access, and no filesystem access.
- Preview omits secret-bearing headers by design and rejects mapped auth or proxy-control headers before request construction.
- apply() reuses the same rule evaluation path as preview() and refuses to proceed when review issues remain.

## Concerns

- The brief specified the allowed concat function but not its string serialization format, so this task fixed the contract as concat:[...] in tests and implementation.
- Existing pytest output still includes two upstream Pydantic deprecation warnings unrelated to this task.

## Commit

- Feature commit SHA: a12119ffdca3ba266238ef228c4696da26620b37

## Reviewer Fixes

### Addressed Findings

- Added registration-time deduplication_key_path validation in the store before persisting accepted transfers, while preserving the apply-time validation in MappingEngine.
- Fixed apply_corrections so root JSON Pointer "" replacements take effect by threading the updated document returned from _set_pointer.
- Wrapped invalid numeric conversions from to_integer and to_number as MappingValidationError with rule context.
- Removed the concat:[...] string DSL and implemented concat as a normal allowlisted transform over list or tuple source values.
- Added focused regression tests for store-level deduplication validation, root-pointer corrections, invalid numeric transforms, and concat input validation.

### Verification

Command:

```bash
uv run pytest tests/transfer/test_mapping.py tests/transfer/test_store.py -q
```

Output:

```text
38 passed, 2 warnings in 2.32s
```

Command:

```bash
uv run pytest tests/transfer/test_mapping.py tests/transfer/test_store.py tests/transfer/test_models.py tests/transfer/test_contracts.py -q
```

Output:

```text
52 passed, 2 warnings in 1.82s
```

Command:

```bash
uv run pytest -q
```

Output:

```text
69 passed, 2 warnings in 1.85s
```
