# SDD ledger — plan: docs/superpowers/plans/2026-09-24-generated-artifact-behavior-validation.md

## Setup

- Worktree: `/home/aaa103529/project/openapi-to-mcp/.worktrees/generated-artifact-behavior-validation`
- Branch: `feat/generated-artifact-behavior-validation`
- Base before Task 1: `d1cb8df` (`chore: ignore local worktrees`)
- The parent checkout contains an unrelated untracked `refs/` directory; it is not copied or modified here.
- Existing ignored `input_data/` is exposed through a worktree-local symlink so the baseline fixtures remain read-only and available.
- Baseline after restoring the ignored fixture paths: `223 passed, 1 warning` with `uv run pytest -q -W error::pydantic.warnings.PydanticDeprecatedSince20`. The warning is the known external Starlette/AnyIO `BlockingPortal` deprecation.

## Preflight conflict scan

The plan and the architecture document were read before dispatch. The following table records every pair of tasks sharing a file or interface.

| Tasks | Shared file/interface | Finding | Ruling |
| --- | --- | --- | --- |
| 1 -> 2 | Verification status model vs mock scenarios | Task 2 needs to report schema gaps as `unvalidated`, but the original scenario shape had no status/reason fields. | Add `validation_status` and `validation_reason` to `MockOperationScenario`; Task 2 maps `MockDataGenerationError` to that state. |
| 1 -> 4 | `GeneratedArtifactVerificationResult` | Task 4 consumes the result model produced by Task 1. | Sequential execution; Task 1 fixes the Pydantic field names before Task 4. |
| 1 -> 6 | `GeneratedArtifactVerificationResult`, `RepairAttemptResult` | Task 6 records repair outcomes using Task 1 models. | Sequential execution; no duplicate result models. |
| 1 -> 7 | Result serialization | Task 7 persists the Task 1 model as JSON/Markdown. | Reports use `model_dump(mode="json")` and redacted fields only. |
| 2 -> 3 | `MockOperationScenario` | Task 3 routes and responds from Task 2 scenarios. | Task 3 consumes the extended scenario fields without changing generation semantics. |
| 2 -> 4 | `build_mock_scenarios()` | Task 4 uses deterministic inputs and expected responses from Task 2. | Scenario order and seed are stable and treated as part of the interface. |
| 2 -> 5 | `openapi_mock_data.py`, error scenarios | Task 5 adds negative scenarios to the generator from Task 2. | Task 5 extends the existing builder instead of duplicating schema generation. |
| 3 -> 4 | `OpenAPIMockServer` and `RecordedRequest` | Task 4 needs the server URL and redacted request history. | Task 3 owns lifecycle and redaction; Task 4 only consumes its public methods. |
| 4 -> 5 | `generated_artifact_verifier.py` | Task 5 adds error/transport classifications to Task 4 orchestration. | Task 5 follows Task 4 and preserves success-path behavior. |
| 4 -> 6 | verifier orchestration | Task 6 reruns the full verifier against each candidate. | Candidate verification uses the same public verifier; no second validation implementation. |
| 4 -> 7 | verifier public functions | Task 7 invokes verification from the CLI. | CLI receives a structured result and does not inspect subprocess internals. |
| 5 -> 6 | failure classifications | Task 6 passes redacted, classified failures to the repairer. | Repair prompts receive only report data and allowlisted source files. |
| 6 -> 7 | `verify_with_repair()` and repairer | Task 7 wires the bounded loop into CLI execution. | `--repair-generated` instantiates the production LLM repairer; default remains disabled. |
| 7 -> 8 | verification reports and `docs/RESULTS_GUIDE.md` | Task 8 documents report fields added by Task 7. | Documentation is updated after the interface is stable. |

The following self-consistency rows cover each task's own files, tests, and interfaces.

| Task | Self-consistency check | Finding / ruling |
| --- | --- | --- |
| 1 | Pydantic models and serialization tests | Consistent; tests exercise enum serialization and redacted payloads. |
| 2 | Value factory, scenario builder, fixture, and tests | Original text had no way to represent `unvalidated`; ruling above adds explicit fields and exception mapping. |
| 3 | FastAPI/Uvicorn server, recorder, lifecycle tests | Consistent; server owns redaction and test uses only public lifecycle methods. |
| 4 | Subprocess MCP path and verifier tests | Consistent for specs with schemas; schema gaps are reported explicitly by the Task 2 extension. |
| 5 | Negative scenarios and runtime error kinds | Consistent; documented HTTP errors are expected test outcomes, transport errors remain classified failures. |
| 6 | Candidate isolation, repair protocol, and tests | Original `artifact_dir` argument was ambiguous and production repair was deferred; ruling changes it to `candidate_dir` and includes an LLM adapter with strict JSON/allowlist parsing. |
| 7 | CLI flags, config keys, report paths, and CLI tests | Consistent; verification is opt-in and repair implies verification. |
| 8 | Architecture/README/results docs and final commands | CLI positional filename syntax was verified against `src/cli.py` and existing README examples. |

## Rulings

- **Ruling:** Use a symlink to the existing ignored `input_data/` inside the isolated worktree — **why:** the baseline tests require a local fixture that is intentionally ignored and the specification must remain read-only; **cost if wrong:** the worktree would need a separate fixture copy and could diverge from the baseline input.
- **Ruling:** Extend the plan's mock scenario interface with explicit validation status/reason and `MockDataGenerationError` — **why:** schema absence must never silently pass and the original interface could not carry that result; **cost if wrong:** Task 2 and downstream reports would need an incompatible result channel.
- **Ruling:** Make `candidate_dir` explicit and implement an opt-in LLM repairer in Task 6 — **why:** a protocol plus `NoOpRepairer` alone would not satisfy the requested automatic evaluation/repair workflow; **cost if wrong:** the repair prompt/parser adds implementation surface and must be kept behind the disabled-by-default CLI flag.

## Task status

- Task 1: complete (commits d1cb8df..12ad1e2, review clean).
- Task 2: fix round 1/5 (1 addressed, 0 open; commits 1f2aee9..a387c95).
- Task 2: minor (deferred): no dedicated `allOf` focused test; the reviewer did not find this to be a blocking spec gap.
- Task 2: complete (commits 12ad1e2..a387c95, review clean).
- Task 3: review finding — same method/path scenarios use first-match routing, which hides documented error scenarios.
- Task 3: Ruling: add `OpenAPIMockServer.activate_scenario()` and require the verifier to select each scenario before invocation — why: success and HTTP-error cases for one operation share the same wire route, so implicit first-match routing cannot test both; cost if wrong: a small public mock-server state API is added and callers must activate scenarios explicitly.
- Task 3: fix round 1/5 (1 addressed, 0 open; commits e08c3d3..a057356).
- Task 3: complete (commits a387c95..a057356, review clean).
- Task 4: fix round 1/5 (2 addressed, 0 open; commits 3a68d26..e8c7c70).
- Task 4: minor (deferred): generated tool signatures currently constrain real MCP success handling for 204/non-dict payloads; the verifier exposes this artifact defect and the final review must triage whether generator changes are required.
- Task 4: minor (deferred): request-side `unvalidated_request_schema` is not yet exercised by a focused case.
- Task 4: complete (commits a057356..e8c7c70, review clean).
- Task 5: fix round 1/1 (HTTP error scenarios 401/404/409/429/500 still pass as structured `http` errors while success is evaluated separately; malformed documented error scenarios now stay unvalidated instead of silently passing; verifier-level connection/timeout/invalid_json coverage now runs through the generated artifact path; focused verification and compile checks passed with `uv run pytest -q tests/test_generated_artifact_verifier.py tests/test_generated_mcp_runtime.py` and `uv run python -m compileall -q src/services/generated_artifact_verifier.py src/services/openapi_mock_data.py src/services/openapi_mock_server.py tests/test_generated_artifact_verifier.py`).
- Task 5: review follow-up (documented HTTP error responses with bodies must not pass on status alone; they now stay unvalidated with a clear reason, while status-only 401/404/409/429/500 cases remain passable and request validation/redaction stay intact).
- Task 5: complete (commits 286fe1e..89f165a, review clean; residual gap: invalid JSON lacks a separate fixture-based E2E regression).
- Task 6: complete (commit be7495c; focused repair/verifier tests passed with `uv run pytest -q tests/test_generated_artifact_repair.py tests/test_generated_artifact_verifier.py`; compile check passed with `uv run python -m compileall -q src/services/generated_artifact_verifier.py src/services/generated_artifact_repair.py tests/test_generated_artifact_repair.py`).
- Task 6: review fix round 2/5 (spec isolation now passes a deep-copied read-only mapping to repairers so caller state cannot be mutated in place; candidate validation now rejects symlinks before static validation/full verification; focused repair/verifier tests passed with `uv run pytest -q tests/test_generated_artifact_repair.py tests/test_generated_artifact_verifier.py`; compileall passed on touched files).
- Task 6: review fix round 3/5 (repair-spec freezing now recursively immutabilizes nested mappings/sequences over a deep copy; candidate validation now snapshots files, directories, and symlinks without following links; traversal cleanup now detects live artifact tree escapes outside `.verification` and restores them; focused repair/verifier tests passed with `uv run pytest -q tests/test_generated_artifact_repair.py tests/test_generated_artifact_verifier.py`; compileall passed on touched files).
- Task 6: critical fix (repairers can no longer poison `.verification/original` into the final restore path; verification now restores from an in-memory pristine tree snapshot and refreshes candidate preparation from that snapshot each attempt; focused repair tests passed with `uv run pytest -q tests/test_generated_artifact_repair.py`; Task 6 remains not review-clean until final review closes the loop).
- Task 6: critical follow-up (the repair loop now resets the controlled `.verification` root at the beginning of every attempt, so a poisoned sibling from attempt 1 cannot leak into attempt 2; added a regression that poisons `.verification/original` on attempt 1 and accepts a clean candidate on attempt 2; review remains open).
- Task 6: complete (commits be7495c..2f8606c, review clean; focused repair/verifier tests passed; residual limitation: arbitrary OS-level writes outside the supplied artifact boundary are not sandboxed).
- Task 7: implemented (pending independent review). Added `--verify-generated`, `--repair-generated`, `--generated-verification-seed`, and `--generated-repair-attempts` with config-backed defaults; verification now runs after successful MCP generation, writes deterministic reports under `mcpserver/verification/verification_report.json` and `verification_summary.md`, attaches only redacted verification metadata to evaluation usage, and marks generation failed when verification fails. Focused checks passed with `uv run pytest -q tests/test_cli.py tests/test_mcp_generator.py` and `uv run python -m compileall -q src tests`; `ruff` was not available in the environment.
- Task 7: fix round 1/1 (review findings addressed: canonical verification report sanitization now strips free-form messages and raw bodies from JSON/Markdown while supporting both legacy dicts and the real `GeneratedArtifactVerificationResult`; `--repair-generated` now uses the production `LLMGeneratedArtifactRepairer` only on the repair path and the CLI calls `src.services.generated_artifact_verifier` directly. Focused checks passed with `uv run pytest -q tests/test_cli.py tests/test_mcp_generator.py tests/test_generated_artifact_verifier.py tests/test_generated_artifact_repair.py` and `uv run python -m compileall -q src tests`; review remains open).
