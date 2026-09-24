# 生成MCP成果物の契約検証・限定修正 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** OpenAPI仕様を変更せず、生成されたMCP server/client/runtimeを仕様ベースのローカルモックで自動動作検証し、失敗時だけ生成物を最大2回まで限定修正できるワークフローを追加する。

**Architecture:** OpenAPI仕様を不変の契約としてハッシュ化し、既存の`OperationMetadata`を使って操作ごとの入力値・成功応答・エラー応答を決定的に生成する。ローカルモックAPIはリクエストを記録し、生成MCP serverを経由した実際のHTTPリクエストとresponse schemaを検証する。修正はOpenAPIへ適用せず、`server.py`、`client.py`、`runtime.py`の候補コピーだけに対して回数制限付きで行い、再検証に通った候補だけを採用する。

**Tech Stack:** Python 3.11+、uv、Pydantic v2、FastAPI、Uvicorn、httpx、jsonschema、FastMCP、pytest、pytest-asyncio。新しい外部依存は追加せず、既存の`jsonschema`、`httpx`、FastAPI/Uvicorn、MCP clientを再利用する。

**Spec:** `docs/ARCHITECTURE.md` の「仕様の評価と改善案」「MCPサーバー・クライアントを作る」、および本会話で合意した「OpenAPI自動修正なし」「生成物のモック動作確認」「最大1〜2回の限定修正」という要件。

## Global Constraints

- OpenAPI入力は読み取り専用とし、評価・検証・修正のどの段階でも内容、ファイル、`enhanced_openapi_spec`を自動置換しない。
- 生成物の修正対象は`server.py`、`client.py`、`runtime.py`だけに限定し、`requirements.txt`、仕様書、元の生成物を直接破壊しない。
- 自動修正の試行回数は既定値2回、設定可能な上限は2回とし、無制限の再評価ループを作らない。
- 外部API、実運用資格情報、プロキシ経由のインターネットへ接続しない。検証対象はローカルモックだけとする。
- モック値はseedを指定して決定的に生成し、同じ仕様ハッシュ・seed・generator versionから同じ検証ケースを再現できるようにする。
- `example`、`examples`、`default`、`enum`、schema制約、`$ref`を優先し、仕様に表現されない業務ルールを推測しない。
- request schema、response schemaが存在しない場合は検証を成功扱いにせず、`unvalidated`または`skipped`理由を結果へ記録する。
- API token、Bearer token、Basic password、Authorization header、秘密body値をログ、レポート、モック履歴へ出力しない。
- 既存の決定的生成、KintoneモックE2E、既存の構造化エラー分類を壊さない。
- Python環境の操作、テスト、実行には`uv`を使用する。コミットはユーザーの明示依頼がある場合だけ作成する。

## Current State

- `src/services/openapi_mcp_codegen.py` に`OperationMetadata`、`collect_operations()`、`write_generated_artifacts()`があり、OpenAPI operationから決定的な生成物を作れる。
- `src/services/mcp_generator.py` は生成後にAST、FastMCP import、`main()`、operation数とtool数、fallback markerを検証する。
- `src/services/generated_mcp_runtime.py` は認証、HTTP、接続、timeout、invalid JSONを構造化エラーに変換するが、API呼び出しの自動リトライは行わない。
- `tests/test_kintone_mcp_e2e.py` は生成MCP server、生成client、ローカルKintone stubをsubprocessで接続する既存パターンを持つ。
- `pyproject.toml` には今回必要な`jsonschema`、`httpx`、`fastapi`、`uvicorn`、`fastmcp`、pytest関連依存がすでにある。

## File Map

### New Files

- `src/models/generated_validation.py`: モックケース、操作別検証結果、修正試行、全体レポートのPydanticモデル。
- `src/services/openapi_mock_data.py`: OpenAPI schemaから決定的なtool引数、成功応答、エラー応答を生成する値ファクトリ。
- `src/services/openapi_mock_server.py`: 生成されたケースを返し、受信リクエストを秘匿化して記録するローカルHTTPモック。
- `src/services/generated_artifact_verifier.py`: 生成MCP server/clientを起動して各toolを実行し、request/response契約を検証するオーケストレーター。
- `src/services/generated_artifact_repair.py`: 許可ファイルだけを候補ディレクトリへ修正し、再検証に成功した場合だけ採用する修正戦略と履歴。
- `tests/test_generated_validation_models.py`: 結果モデルのシリアライズ、ステータス、秘匿情報の回帰テスト。
- `tests/test_openapi_mock_data.py`: schema値、`$ref`、examples、制約、seed再現性のテスト。
- `tests/test_openapi_mock_server.py`: method/path matching、記録、成功・エラー応答、redactionのテスト。
- `tests/test_generated_artifact_verifier.py`: 最小OpenAPIから生成した成果物のtool実行と契約検証のテスト。
- `tests/test_generated_artifact_repair.py`: 修正候補の隔離、最大試行回数、成功候補採用、失敗時の復元テスト。

### Modified Files

- `src/cli.py`: `--verify-generated`、`--repair-generated`、seed、修正試行上限のCLI統合と結果保存。
- `src/services/mcp_generator.py`: 生成後検証結果と動作検証を連携できるmetadata拡張。生成処理自体はOpenAPIを変更しない。
- `config/config.yml`: 自動検証の既定値、timeout、seed、修正試行数を設定可能にする。
- `docs/ARCHITECTURE.md`: 生成後のモック動作検証と限定修正のフローを追加し、OpenAPI自動修正を行わないことを明記する。
- `README.md`: CLI実行例、検証レポートの場所、修正ループの安全制約を追記する。
- `docs/RESULTS_GUIDE.md`: 検証結果の`passed`、`failed`、`unvalidated`、`repair_attempts`の読み方を追記する。
- `tests/test_cli.py`: CLIオプション、検証失敗時の終了状態、レポート保存をテストする。
- `tests/test_mcp_generator.py`: 既存の静的生成検証と新しい動作検証metadataの後方互換性をテストする。

### Preserved Files

- OpenAPI入力ファイルと`enhanced_openapi_spec`は変更しない。
- `examples/kintone_stub_server.py`と`tests/test_kintone_mcp_e2e.py`は既存業務シナリオの回帰基盤として維持する。必要な共有ヘルパーの抽出以外は変更しない。
- `results/**/mcpserver/`の生成物は手編集せず、テストでは`tmp_path`へ生成する。
- `refs/`は既存の未追跡資料としてコミット対象に含めない。

## Interfaces

後続タスクが先行タスクの実装を参照できるよう、次の公開境界を固定する。

```python
# src/services/openapi_mock_data.py
@dataclass(frozen=True)
class MockOperationScenario:
    operation_id: str
    tool_name: str
    method: str
    path: str
    tool_arguments: dict[str, Any]
    status_code: int
    response_headers: dict[str, str]
    response_body: Any | None
    scenario_kind: Literal["success", "http_error"]


def build_mock_scenarios(
    openapi_spec: Mapping[str, Any],
    *,
    seed: int = 0,
) -> list[MockOperationScenario]: ...


def generate_schema_value(
    schema: Mapping[str, Any],
    *,
    openapi_spec: Mapping[str, Any],
    seed: int,
    path: str = "$",
) -> Any: ...
```

```python
# src/services/openapi_mock_server.py
@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: Any | None


class OpenAPIMockServer:
    def __init__(
        self,
        scenarios: Sequence[MockOperationScenario],
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None: ...

    def start(self) -> str: ...
    def stop(self) -> None: ...
    def requests(self) -> list[RecordedRequest]: ...
```

```python
# src/services/generated_artifact_verifier.py
async def verify_generated_artifacts(
    openapi_spec: Mapping[str, Any],
    artifact_dir: Path,
    *,
    seed: int = 0,
    timeout_seconds: float = 30.0,
) -> GeneratedArtifactVerificationResult: ...

async def verify_with_repair(
    openapi_spec: Mapping[str, Any],
    artifact_dir: Path,
    *,
    repairer: GeneratedArtifactRepairer,
    seed: int = 0,
    timeout_seconds: float = 30.0,
    max_repair_attempts: int = 2,
) -> GeneratedArtifactVerificationResult: ...
```

```python
# src/services/generated_artifact_repair.py
class GeneratedArtifactRepairer(Protocol):
    async def repair(
        self,
        artifact_dir: Path,
        openapi_spec: Mapping[str, Any],
        report: GeneratedArtifactVerificationResult,
        attempt: int,
    ) -> RepairAttemptResult: ...
```

## Detailed Execution Tasks

### Task 1: Define Verification Result Contracts

**Files:**
- Create: `src/models/generated_validation.py`
- Create: `tests/test_generated_validation_models.py`

**Interfaces:**
- `VerificationStatus`: `passed`、`failed`、`unvalidated`、`skipped`の文字列enum。
- `OperationVerificationResult`: `operation_id`、`tool_name`、`scenario_kind`、`status`、`request_valid`、`response_valid`、`error_kind`、`failure_code`、`message`、`redacted_input`、`redacted_output`を持つ。
- `RepairAttemptResult`: `attempt`、`changed_files`、`candidate_dir`、`accepted`、`failure_codes`を持つ。
- `GeneratedArtifactVerificationResult`: `artifact_dir`、`spec_sha256`、`generator_version`、`seed`、`status`、`attempts`、`operations`、`repair_attempts`、`failures`を持つ。

- [ ] **Step 1: Write the failing tests**

```python

def test_verification_result_serializes_status_and_operation_failures():
    result = GeneratedArtifactVerificationResult(
        artifact_dir="/tmp/mcpserver",
        spec_sha256="abc",
        generator_version="test",
        seed=7,
        status=VerificationStatus.FAILED,
        attempts=1,
        operations=[
            OperationVerificationResult(
                operation_id="getUser",
                tool_name="getUser",
                scenario_kind="success",
                status=VerificationStatus.FAILED,
                request_valid=False,
                response_valid=False,
                failure_code="request_schema_mismatch",
                message="request body does not match schema",
            )
        ],
    )

    payload = result.model_dump(mode="json")
    assert payload["status"] == "failed"
    assert payload["operations"][0]["failure_code"] == "request_schema_mismatch"
```

```python

def test_result_rejects_secret_values_in_redacted_fields():
    result = OperationVerificationResult(
        operation_id="listUsers",
        tool_name="listUsers",
        scenario_kind="success",
        status=VerificationStatus.PASSED,
        request_valid=True,
        response_valid=True,
        redacted_input={"headers": {"Authorization": "[REDACTED]"}},
    )
    assert "[REDACTED]" in str(result.model_dump())
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `uv run pytest -q tests/test_generated_validation_models.py`

Expected: FAIL because the new models do not exist.

- [ ] **Step 3: Implement the models**

Use Pydantic v2 models and a string enum. Keep optional message and payload fields nullable. Do not store raw request headers or raw exception text when it can contain credentials. Add a `summary()` method only if it returns redacted, deterministic data.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `uv run pytest -q tests/test_generated_validation_models.py`

Expected: PASS.

- [ ] **Step 5: Run model diagnostics**

Run: `uv run python -m compileall -q src/models/generated_validation.py`

Expected: exit code 0.

**Checkpoint:** Result schema is stable before any generator or mock implementation depends on it.

### Task 2: Build Deterministic Schema-Based Mock Data

**Files:**
- Create: `src/services/openapi_mock_data.py`
- Create: `tests/test_openapi_mock_data.py`
- Test fixture: `examples/minimal_users_api.yaml`

**Interfaces:**
- Consume: `collect_operations()` and `OperationMetadata` from `src/services/openapi_mcp_codegen.py`.
- Produce: `MockOperationScenario` and `build_mock_scenarios()` from the Interfaces section.

- [ ] **Step 1: Write failing tests for primitive and example precedence**

```python

def test_schema_value_prefers_example_then_enum_then_default():
    spec = {"openapi": "3.0.3", "info": {}, "paths": {}}
    assert generate_schema_value(
        {"type": "string", "example": "from-example", "enum": ["first"]},
        openapi_spec=spec,
        seed=0,
    ) == "from-example"
    assert generate_schema_value(
        {"type": "string", "enum": ["first", "second"]},
        openapi_spec=spec,
        seed=0,
    ) == "first"
    assert generate_schema_value(
        {"type": "integer", "default": 4},
        openapi_spec=spec,
        seed=0,
    ) == 4
```

- [ ] **Step 2: Add failing tests for object, array, `$ref`, and constraints**

```python

def test_schema_value_resolves_refs_and_respects_constraints():
    spec = {
        "openapi": "3.0.3",
        "info": {},
        "paths": {},
        "components": {
            "schemas": {
                "User": {
                    "type": "object",
                    "required": ["name", "age"],
                    "properties": {
                        "name": {"type": "string", "minLength": 3},
                        "age": {"type": "integer", "minimum": 18},
                    },
                }
            }
        },
    }
    value = generate_schema_value(
        {"$ref": "#/components/schemas/User"},
        openapi_spec=spec,
        seed=3,
    )
    assert len(value["name"]) >= 3
    assert value["age"] >= 18
```

- [ ] **Step 3: Run the focused tests to confirm the expected failures**

Run: `uv run pytest -q tests/test_openapi_mock_data.py`

Expected: FAIL for missing value factory and scenario builder.

- [ ] **Step 4: Implement the value factory**

Implement `generate_schema_value()` with a stable local `random.Random(seed)` derived from the schema path. Resolve local `$ref` values from `components.schemas`. Support string, integer, number, boolean, object, array, nullable, `oneOf`/`anyOf` first valid branch, `minLength`, `maxLength`, `minimum`, `maximum`, `minItems`, `maxItems`, `pattern` only for simple literal-safe patterns, and `additionalProperties` when explicitly described. Use fixed values for `date`, `date-time`, `uuid`, and email formats. When a schema cannot be represented, return a structured generation failure rather than inventing an unconstrained value.

- [ ] **Step 5: Implement operation scenarios**

For every `OperationMetadata`, locate the corresponding raw operation and create one success scenario. Select the first documented `2xx` response, generate a response body from its schema, and create flat MCP tool arguments using the normalized Python parameter names. For required request bodies, generate a body from the request schema. For documented `4xx`/`5xx` responses, create separate error scenarios with a deterministic error body and `scenario_kind="http_error"`. Include a `204` scenario with `response_body=None`.

- [ ] **Step 6: Add reproducibility and missing-contract tests**

```python

def test_same_seed_produces_identical_scenarios():
    spec = yaml.safe_load(Path("examples/minimal_users_api.yaml").read_text())
    first = build_mock_scenarios(spec, seed=11)
    second = build_mock_scenarios(spec, seed=11)
    assert first == second
```

Also assert that an operation without a request or response schema produces an explicit `unvalidated` reason, not a successful schema validation.

- [ ] **Step 7: Run the focused tests**

Run: `uv run pytest -q tests/test_openapi_mock_data.py`

Expected: PASS.

### Task 3: Implement the Local Mock API and Redacted Request Recorder

**Files:**
- Create: `src/services/openapi_mock_server.py`
- Create: `tests/test_openapi_mock_server.py`

**Interfaces:**
- Consume: `MockOperationScenario` from `src/services/openapi_mock_data.py`.
- Produce: `OpenAPIMockServer.start() -> str`, `stop() -> None`, and `requests() -> list[RecordedRequest]`.

- [ ] **Step 1: Write failing tests for route matching and response selection**

```python

def test_mock_server_returns_scenario_response_and_records_request():
    scenario = MockOperationScenario(
        operation_id="getUser",
        tool_name="getUser",
        method="GET",
        path="/users/{id}",
        tool_arguments={"id": 7},
        status_code=200,
        response_headers={"content-type": "application/json"},
        response_body={"id": 7, "name": "mock-user"},
        scenario_kind="success",
    )
    server = OpenAPIMockServer([scenario])
    base_url = server.start()
    try:
        response = httpx.get(f"{base_url}/users/7", timeout=2)
        assert response.status_code == 200
        assert response.json()["id"] == 7
        assert server.requests()[0].path == "/users/7"
    finally:
        server.stop()
```

- [ ] **Step 2: Add failing tests for errors and credential redaction**

Verify that a scenario with status `404` returns the configured error body, while a request containing `Authorization: Bearer secret-token` is recorded as `Authorization: [REDACTED]` or as a boolean presence marker. Assert that the secret value is absent from serialized request history.

- [ ] **Step 3: Run the focused tests to confirm failure**

Run: `uv run pytest -q tests/test_openapi_mock_server.py`

Expected: FAIL because the server and recorder do not exist.

- [ ] **Step 4: Implement the local server**

Use FastAPI and Uvicorn on `127.0.0.1` with port `0`, expose a catch-all route, expand path templates into a safe matcher, select a scenario by method and normalized path, parse JSON bodies when possible, and return the scenario status, headers, and body. Start Uvicorn in a daemon thread and wait for the socket before returning the base URL. `stop()` must be idempotent and join the server thread.

- [ ] **Step 5: Implement request redaction**

Redact case-insensitive `authorization`, `proxy-authorization`, `x-api-key`, `x-cybozu-api-token`, and any configured secret header. Replace sensitive header values with `[REDACTED]`; preserve only `has_api_token`-style booleans when a test needs presence verification. Recursively redact keys matching `token`, `secret`, `password`, `api_key`, and `authorization` in JSON bodies.

- [ ] **Step 6: Run the focused tests**

Run: `uv run pytest -q tests/test_openapi_mock_server.py`

Expected: PASS.

### Task 4: Verify Generated Artifacts Through the Real MCP Path

**Files:**
- Create: `src/services/generated_artifact_verifier.py`
- Create: `tests/test_generated_artifact_verifier.py`
- Modify: `src/services/mcp_generator.py` only if verification metadata needs a backward-compatible field

**Interfaces:**
- Consume: `write_generated_artifacts()`, `MockOperationScenario`, `OpenAPIMockServer`, `GeneratedArtifactVerificationResult`.
- Produce: `verify_generated_artifacts()` from the Interfaces section.

- [ ] **Step 1: Write a failing end-to-end verification test**

```python
@pytest.mark.asyncio
async def test_verifier_calls_each_generated_tool_against_local_mock(tmp_path):
    spec = yaml.safe_load(Path("examples/minimal_users_api.yaml").read_text())
    artifact_dir = tmp_path / "mcpserver"
    write_generated_artifacts(spec, artifact_dir)

    result = await verify_generated_artifacts(spec, artifact_dir, seed=5)

    assert result.status == VerificationStatus.PASSED
    assert result.spec_sha256
    assert {item.tool_name for item in result.operations} == {
        operation.tool_name for operation in collect_operations(spec)
    }
    assert all(item.request_valid for item in result.operations)
    assert all(item.response_valid for item in result.operations)
```

- [ ] **Step 2: Run the focused test to confirm failure**

Run: `uv run pytest -q tests/test_generated_artifact_verifier.py::test_verifier_calls_each_generated_tool_against_local_mock`

Expected: FAIL because the verifier orchestration is not implemented.

- [ ] **Step 3: Implement process orchestration**

Generate scenarios, start the mock API, choose free localhost ports, start the generated `server.py` with `--transport streamable-http` and `--base-url`, wait for its MCP endpoint, and invoke each generated tool through the generated `client.py`. Use the existing subprocess pattern from `tests/test_kintone_mcp_e2e.py`. Set `trust_env=False` behavior and `NO_PROXY` for localhost. Always terminate child processes and the mock server in `finally` blocks.

- [ ] **Step 4: Validate outbound requests**

For each recorded request, compare method and expanded path to the scenario. Validate query and path parameters against their normalized metadata. Validate JSON request bodies with `jsonschema` when a request schema exists. Record `request_schema_mismatch`, `unexpected_request`, `missing_request`, or `unvalidated_request_schema` rather than raising an unclassified exception.

- [ ] **Step 5: Validate responses**

Validate the returned MCP result against the selected response schema. Treat `204` and empty responses explicitly. For response validation failures, record `response_schema_mismatch`; for MCP error results, preserve only structured `kind`, status code, operation id, and target error code. Never store raw subprocess output when it may include secrets.

- [ ] **Step 6: Add operation-level tests**

Cover a successful GET, a request-body POST, a path/query parameter operation, a 204 response, and an operation with no response schema. Assert that one operation failure does not erase results for other operations and that the final status is `failed` if any required verification fails.

- [ ] **Step 7: Run focused and existing contract tests**

Run: `uv run pytest -q tests/test_generated_artifact_verifier.py tests/test_generated_contract.py tests/test_openapi_mcp_codegen.py`

Expected: PASS.

### Task 5: Add Error and Negative-Behavior Verification

**Files:**
- Modify: `src/services/generated_artifact_verifier.py`
- Modify: `src/services/openapi_mock_data.py`
- Create or modify: `tests/test_generated_artifact_verifier.py`
- Modify: `tests/test_generated_mcp_runtime.py` only for missing regression coverage

**Interfaces:**
- Consume: `MockOperationScenario.scenario_kind="http_error"` and existing `StructuredApiError` kinds.
- Produce: operation reports with `http`, `connection`, `timeout`, `invalid_json`, and `configuration` classifications.

- [ ] **Step 1: Write failing tests for documented HTTP errors**

Create mock scenarios for `401`, `404`, `409`, `429`, and `500`. Call the corresponding generated tool and assert that the report contains the expected status code and `error_kind="http"`, without treating an expected documented error response as a verifier crash.

- [ ] **Step 2: Write failing tests for transport and payload failures**

Add verifier scenarios for an unreachable upstream, a short timeout, and a response with invalid JSON. Assert that the generated runtime maps these to `connection`, `timeout`, and `invalid_json` respectively. Do not add automatic HTTP retries in this task; the purpose is classification and observability.

- [ ] **Step 3: Run focused tests and verify failure**

Run: `uv run pytest -q tests/test_generated_artifact_verifier.py tests/test_generated_mcp_runtime.py`

Expected: FAIL only for the newly required verifier classifications.

- [ ] **Step 4: Implement negative scenario execution**

Run error scenarios separately from success scenarios. A documented error scenario passes when the generated MCP result contains the expected structured error and status. An unexpected error, missing error mapping, leaked credential, or wrong status fails the operation. Keep success and error assertions distinct so a 404 test does not make a successful response test pass.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest -q tests/test_generated_artifact_verifier.py tests/test_generated_mcp_runtime.py`

Expected: PASS.

### Task 6: Implement the Bounded Generated-Artifact Repair Loop

**Files:**
- Create: `src/services/generated_artifact_repair.py`
- Create: `tests/test_generated_artifact_repair.py`
- Modify: `src/services/generated_artifact_verifier.py`

**Interfaces:**
- `GeneratedArtifactRepairer.repair()` accepts only the immutable OpenAPI mapping, the failed report, an attempt number, and an artifact directory.
- `verify_with_repair()` returns the same `GeneratedArtifactVerificationResult` shape regardless of whether repair was attempted.

- [ ] **Step 1: Write failing tests for candidate isolation and acceptance**

```python
@pytest.mark.asyncio
async def test_repair_candidate_is_adopted_only_after_full_reverification(tmp_path):
    original = tmp_path / "mcpserver"
    original.mkdir()
    (original / "server.py").write_text("original", encoding="utf-8")

    repairer = FakeRepairer(replacement="verified candidate")
    result = await verify_with_repair(
        SPEC,
        original,
        repairer=repairer,
        seed=0,
        max_repair_attempts=1,
    )

    assert result.status == VerificationStatus.PASSED
    assert (original / "server.py").read_text(encoding="utf-8") == "verified candidate"
    assert result.repair_attempts[0].accepted is True
```

Add a second test where the candidate still fails; assert that the original file is restored and the result is `failed`.

- [ ] **Step 2: Run focused tests to confirm failure**

Run: `uv run pytest -q tests/test_generated_artifact_repair.py`

Expected: FAIL because candidate directories, repair protocol, and orchestration do not exist.

- [ ] **Step 3: Implement safe candidate handling**

Before the first repair, snapshot the allowlisted generated files and compute their hashes. For each attempt, copy artifacts to `.verification/attempt-{n}`. A repairer may write only `server.py`, `client.py`, or `runtime.py` inside that candidate directory. Reject path traversal, new executable files, changes to `requirements.txt` or documentation, and changes to the OpenAPI input.

- [ ] **Step 4: Implement a deterministic fake-compatible repair interface**

The default production workflow must be able to use a repair strategy without coupling the verifier to an LLM. Define a `GeneratedArtifactRepairer` protocol and a `NoOpRepairer` that records why it cannot repair. Tests use `FakeRepairer`; later an LLM adapter can implement the same protocol. The repair prompt/adapter, if enabled later, must receive the redacted report and allowlisted file contents only.

- [ ] **Step 5: Implement bounded retry and acceptance rules**

For attempt values `1..max_repair_attempts`, run the repairer, compile and statically validate the candidate, run the entire behavior verifier, and adopt the candidate only if every required operation passes and no secret-redaction check fails. Stop immediately on success. On exhaustion, delete candidates and retain the original artifact. Clamp configuration to `0..2`; never interpret a negative or unbounded value as unlimited.

- [ ] **Step 6: Add tests for no OpenAPI mutation and max attempts**

Hash the input spec before and after `verify_with_repair()`. Assert equality for both success and failure. Use a repairer that always fails and assert exactly two calls when `max_repair_attempts=2`, zero calls when it is `0`, and no third call for any larger configured value.

- [ ] **Step 7: Run focused tests**

Run: `uv run pytest -q tests/test_generated_artifact_repair.py tests/test_generated_artifact_verifier.py`

Expected: PASS.

### Task 7: Integrate Verification and Repair into the CLI and Reports

**Files:**
- Modify: `src/cli.py`
- Modify: `config/config.yml`
- Modify: `src/services/mcp_generator.py`
- Modify: `tests/test_cli.py`
- Modify: `docs/RESULTS_GUIDE.md`

**Interfaces:**
- Add CLI options:
  - `--verify-generated`: run mock behavior verification after generation.
  - `--repair-generated`: enable the bounded artifact repair loop; implies verification.
  - `--generated-verification-seed INTEGER`: default from config, initially `0`.
  - `--generated-repair-attempts INTEGER`: default `2`, clamped to `0..2`.
- Add config keys with safe defaults:
  - `generated_verification_enabled: false`
  - `generated_verification_seed: 0`
  - `generated_verification_timeout_seconds: 30`
  - `generated_repair_enabled: false`
  - `generated_repair_max_attempts: 2`
- Save reports under `<results_dir>/mcpserver/verification/verification_report.json` and `verification_summary.md`.

- [ ] **Step 1: Write failing CLI tests**

Test that `--verify-generated` calls the verifier after successful generation, that `--repair-generated` also enables verification, and that a failed verification returns `generation_status="failed"` without replacing the original OpenAPI input. Test that default behavior remains unchanged when neither flag nor config enables verification.

- [ ] **Step 2: Run focused CLI tests to verify failure**

Run: `uv run pytest -q tests/test_cli.py`

Expected: FAIL only for the new options and report integration.

- [ ] **Step 3: Add parser and config wiring**

Parse the new options beside the existing MCP generation options. Resolve CLI values before config values, clamp repair attempts to `0..2`, and reject `--repair-generated` only when the generated artifact directory does not exist. Do not change the existing evaluation threshold or automatically use `enhanced_spec` in generation.

- [ ] **Step 4: Run verification after static generation**

Call `verify_generated_artifacts()` after `MCPServerGenerator.generate_mcp_server()` and generated-file checks succeed. If repair is enabled, call `verify_with_repair()` with a configured repairer. Attach only redacted verification metadata to the existing MCP usage/evaluation result.

- [ ] **Step 5: Persist deterministic reports**

Write JSON using stable key ordering and UTF-8. Write Markdown containing spec hash, seed, generator version, operation statuses, failure codes, repair attempts, and changed file names. Never include raw authorization headers, tokens, passwords, raw subprocess logs, or full request bodies containing secret-like fields.

- [ ] **Step 6: Run focused CLI tests**

Run: `uv run pytest -q tests/test_cli.py tests/test_mcp_generator.py`

Expected: PASS.

### Task 8: Document the Workflow and Run Full Regression Verification

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `README.md`
- Modify: `docs/RESULTS_GUIDE.md`
- Add tests only if documentation examples expose an uncovered behavior.

- [ ] **Step 1: Add the architecture flow**

Document this exact policy:

```text
OpenAPI読み込み（不変）
  -> 決定的MCP生成
  -> 静的検証
  -> 仕様ベースのモック値生成
  -> ローカルモックAPI経由のMCP動作検証
  -> 失敗時のみ生成物候補を最大2回修正
  -> 全操作成功なら採用、失敗なら元の生成物とレポートを保持
```

State that the loop does not edit or automatically re-evaluate a modified OpenAPI specification. Explain that business state transitions not represented in OpenAPI require explicit scenario fixtures such as the existing Kintone OCR E2E.

- [ ] **Step 2: Add a reproducible CLI example**

Document a command using a local example spec and a fixed seed:

```bash
uv run openapi-to-mcp examples/minimal_users_api.yaml \
  --verify-generated \
  --generated-verification-seed 0
```

Document the optional bounded repair form and the report paths. Do not document real credentials or an external target URL for this workflow.

- [ ] **Step 3: Run Markdown and static checks**

Run:

```bash
git diff --check
uv run python -m compileall -q src tests
```

Expected: no whitespace errors and exit code 0.

- [ ] **Step 4: Run focused feature tests**

Run:

```bash
uv run pytest -q \
  tests/test_generated_validation_models.py \
  tests/test_openapi_mock_data.py \
  tests/test_openapi_mock_server.py \
  tests/test_generated_artifact_verifier.py \
  tests/test_generated_artifact_repair.py \
  tests/test_cli.py
```

Expected: all new and directly affected tests pass.

- [ ] **Step 5: Run the full regression suite**

Run:

```bash
uv run pytest -q -W error::pydantic.warnings.PydanticDeprecatedSince20
```

Expected: all existing tests and new tests pass. The known external Starlette/AnyIO deprecation warning may remain if it is not emitted as a project Pydantic warning; record it without changing unrelated dependencies.

- [ ] **Step 6: Run final artifact and security checks**

Run:

```bash
uv run python -m compileall -q src
uv run bandit -r src -q
git diff --check
```

Inspect one successful report and one failed report to verify that the OpenAPI hash is unchanged and no credential value appears. Do not commit unless the user explicitly requests it.

## Acceptance Criteria

- OpenAPI入力ファイルのSHA-256が処理前後で一致する。
- 仕様から生成した固定seedのmock scenariosが再実行で一致する。
- 生成MCP serverを実際に起動し、各operationに対応するtoolをローカルモックへ到達させられる。
- request method、path、parameters、bodyが検証される。
- 成功response、4xx/5xx、timeout、connection、invalid JSONが区別される。
- schemaがない箇所は成功扱いにせず、`unvalidated`または`skipped`として記録される。
- credentialsがログ、MCP結果、mock履歴、Markdown/JSONレポートへ漏れない。
- 修正対象は生成物のallowlistだけで、修正候補は全検証成功時だけ採用される。
- 修正試行は最大2回で停止し、失敗時には元の生成物が残る。
- `--verify-generated`未指定時の既存CLI動作と、Kintone OCR E2Eが維持される。
- 全テスト、compileall、Bandit、`git diff --check`が通過する。

## Self-Review

- **Spec coverage:** OpenAPI不変、schemaベースmock、実MCP経路検証、成功・異常系、限定修正、redaction、レポート、CLI、既存E2E回帰をTasks 1〜8でカバーした。
- **Scope check:** OpenAPI自動修正・自動置換・無制限ループ・実API接続はGlobal ConstraintsとPreserved Filesで明確に除外した。
- **Type consistency:** `MockOperationScenario`はTasks 2〜5、`GeneratedArtifactVerificationResult`はTasks 1、4、6、7、`GeneratedArtifactRepairer`はTasks 6〜7で同じ名前と引数を使用している。
- **Placeholder scan:** 実装対象、関数名、引数、テストコマンド、期待結果を各タスクに記載し、`TODO`、`TBD`、未定義の「適切に実装」は使用していない。
- **Risk boundary:** 業務状態遷移はOpenAPIから推測せず、既存Kintone E2Eや明示的シナリオで検証する。生成物の自動修正は初期実装ではfake-compatible protocolとallowlistを先に固定し、LLM adapterを暗黙に必須化しない。
