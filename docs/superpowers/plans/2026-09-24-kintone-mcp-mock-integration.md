# Kintone MCP Mock Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `input_data/openapi-spec-1/openapi.yaml` から生成したMCP serverで、OCR標準データをローカルKintone互換モックへ転記し、応答を検証できる状態にする。

**Architecture:** OpenAPIのpath/methodを決定的に走査するcode generatorと、生成toolが共有するHTTP/auth/error runtimeを追加する。生成MCP serverは`API_BASE_URL`へ接続し、生成MCP clientは選択したtoolを呼び出す。外部Kintoneを使わず、既存のKintone stub serverを対象にE2E検証する。

**Tech Stack:** Python 3.11+、FastMCP、httpx、Pydantic、FastAPI、uvicorn、pytest、pytest-asyncio、uv。既存のOpenAPI評価機能と生成CLIの公開入口は維持する。

**Spec:** `input_data/openapi-spec-1/openapi.yaml`、`docs/superpowers/plans/2026-09-24-kintone-mcp-mock-integration.md`

> **Execution status (2026-09-24):** Tasks 1A through 6 are implemented and
> verified. Checkpoint commits were intentionally not created because no commit
> was explicitly requested; `refs/` remains unchanged.

## Global Constraints

- Python環境と依存関係の操作には`uv`だけを使用する。
- `.env`を直接パースせず、環境変数の値をログ、MCP応答、モック履歴へ出力しない。
- 各OpenAPI operationを1つの`@mcp.tool()`へ変換し、operationIdをtool名に優先使用する。
- operation-level `security`のOR条件を保持し、API token、Bearer/OAuth、Basic認証を環境変数から解決する。
- 任意の外部URLへ接続せず、`API_BASE_URL`または`--base-url`で明示された接続先だけを使用する。
- HTTP、接続、timeout、JSON解析エラーを区別し、構造化JSONとしてMCP呼び出し元へ返す。
- 実装はTDDで進め、失敗テスト、最小実装、対象テスト、全回帰、checkpoint commitの順に行う。
- 生成物を手編集して完了扱いにせず、generatorの変更から再生成して検証する。
- 未追跡の`refs/`は変更・削除しない。

---

## Current State

- `results/azure/openapi/mcpserver/server.py` はOpenAPIの204 operationsに対応する204 toolsを持つ決定的生成物で、fallback markerを含まない。
- `runtime.py` は `API_BASE_URL`、Kintone API token、Bearer/OAuth、Basic認証、構造化HTTP/接続/timeout/JSONエラーを扱う。
- `client.py` は `streamable-http` をデフォルトとして、tool一覧と `call_tool` 実行に対応する。
- `examples/kintone_stub_server.py` はAPI token認証、アプリ情報、レコードCRUD、GET-over-POST、エラー再現、redacted request historyを提供する。
- `examples/ocr/kintone-transfer.json` を入力に、mock、generated MCP server、generated clientのsubprocess E2Eが通過する。削除後の404と到達不能転記先のconnection errorも構造化JSONで検証する。
- 全テストは `221 passed`。生成artifactのcontract、compileall、Bandit、VS Code diagnosticsも確認済み。
- `refs/` は既存の未追跡ファイル群であり、この計画では変更しない。

## Scope

### In Scope

- OpenAPIからの決定的なMCP tool生成
- Kintone APIへの接続先設定
- API token、Bearer/OAuth、Basic認証
- path/query/header/bodyのリクエスト生成
- 成功応答とHTTP/接続/JSON解析エラーの構造化
- MCP clientからの任意tool実行
- OCRモックデータからKintoneモックへの登録・更新・取得確認
- 生成物の構文、tool数、認証、エラー、E2Eテスト

### Out of Scope

- 実Kintone環境への書き込み
- Kintoneの全操作に対する業務固有のOCRマッピング自動推論
- UI、複数転記先へのfan-out、バッチ配送
- 認証情報の外部Secret Manager連携
- MCP protocol自体の変更

## Design Decisions

1. LLMの単一巨大プロンプトに生成品質を依存させない。OpenAPIのpath/methodを機械的に走査する決定的generatorを追加する。
2. 各OpenAPI operationを1つの `@mcp.tool()` に変換する。operationIdを優先し、ない場合はHTTP methodとpathから安定したtool名を生成する。
3. 生成serverには共通のHTTP helperを1つ持たせ、全toolがそのhelper経由でAPIを呼び出す。
4. operation-level `security` を優先し、複数security requirementはORとして扱う。認証情報の値は環境変数から読み、ログ・MCP応答・モック履歴へ出さない。
5. 完全生成に失敗したfallbackは成功扱いにしない。fallbackを許容する場合だけ明示的なフラグを要求し、生成完了と実行可能性を区別する。
6. デフォルトtransportは `streamable-http` とし、SSEを明示指定時の互換transportとして残す。

## File Map

### New Files

- `src/services/openapi_mcp_codegen.py`: OpenAPI operationの正規化、tool名生成、server/runtime sourceのrendering。
- `src/services/generated_mcp_runtime.py`: 生成物へコピーするHTTP、認証、レスポンス、エラー処理runtime。
- `src/services/generated_mcp_client.py`: 生成clientへ埋め込む引数解析、transport接続、tool invocation処理。
- `tests/test_openapi_mcp_codegen.py`: operation収集、tool名、引数metadata、生成コードのテスト。
- `tests/test_generated_mcp_runtime.py`: URL、認証、HTTP応答、例外分類、redactionのテスト。
- `tests/test_generated_mcp_client.py`: client引数解析、tool実行、transport、終了結果のテスト。
- `tests/test_kintone_mcp_e2e.py`: mock server、generated server、MCP clientを接続するE2Eテスト。
- `tests/test_generated_contract.py`: Kintone OpenAPIと生成artifactのtool数・runtime・主要tool契約のテスト。
- `examples/ocr/kintone-transfer.json`: E2Eで使用する最小OCR標準データ。
- `docs/KINTONE_MCP_MOCK_RESULTS.md`: 正常転記、HTTPエラー、通信エラー、認証秘匿の人向け検証結果。

### Modified Files

- `src/services/mcp_generator.py`: deterministic generatorの呼び出し、生成物検証、runtime.py出力、fallback可視化。
- `src/cli.py`:生成失敗の終了処理と生成tool数・operation数の報告。
- `examples/kintone_stub_server.py`:認証・CRUDに加え、エラー再現用のmock endpointと履歴確認。
- `tests/test_kintone_stub_server.py`:エラー再現、GET-over-POST、更新キーの回帰テスト。
- `examples/README.md`:mock E2Eの起動・実行手順。
- `README.md`:決定的generatorとKintone mock検証の概要。
- `results/azure/openapi/mcpserver/`: generatorから再生成される`server.py`、`runtime.py`、`client.py`、README等。手編集しない。

### Preserved Files

- `docs/superpowers/plans/2026-09-18-ocr-transfer-connector-mvp.md`:既存OCR転送計画。変更しない。
- `templates/mcp_server_create.txt`: deterministic generatorのデフォルト経路では使用しないlegacy LLM prompt。内容は変更しない。
- `refs/`:既存の未追跡ファイル群。変更・削除しない。

## Implementation Task Summary

以下は実装領域の一覧である。実際の作業は、この後の「Detailed Execution Tasks」にあるcheckbox単位で進める。

### Task 1: Generation Failure Must Be Visible

**Files:**

- Modify: `src/services/mcp_generator.py`
- Modify: `src/cli.py`
- Create or modify: `tests/test_mcp_generator.py`
- Create or modify: `tests/test_cli.py`

**Work:**

- fallback serverの生成状態をmetadataまたは生成結果に記録する。
- fallback marker、tool数不足、usage未記録を検出する。
- fallbackを通常の成功結果として保存しない。
- `--allow-fallback` を追加する場合は明示指定時だけ許可する。
- 生成ログに、OpenAPI operation数、生成tool数、fallback状態、検証結果を記録する。

**Acceptance:**

- LLM生成失敗時に成功扱いにならない。
- fallbackを完全生成物と誤認できない。
- 既存の評価-only処理と公開CLI APIを壊さない。

**Checkpoint:**

```text
test: expose MCP generation fallback failures
```

### Task 2: Add Deterministic OpenAPI-to-MCP Code Generation

**Files:**

- Create: `src/services/openapi_mcp_codegen.py`
- Modify: `src/services/mcp_generator.py`
- Create or modify: `tests/test_openapi_mcp_codegen.py`

**Work:**

- OpenAPIの`paths`を走査し、各methodをoperation metadataへ正規化する。
- operationIdをtool名へ変換する。operationIdがない場合はpath/methodからPython identifierを作る。
- 重複tool名には安定したsuffixを付与する。
- path parameter、query parameter、header parameter、request bodyをtool引数へ反映する。
- `$ref`、required、default、schema type、descriptionを可能な範囲で保持する。
- 204 operationsに対して204 toolsを生成できることを確認する。
- 生成コードには `from mcp.server.fastmcp import FastMCP` を使用する。
- 生成serverへAPI metadataとtool一覧のresourceを追加する。

**Acceptance:**

- Kintone OpenAPIからfallback markerなしのserver.pyを生成できる。
- ASTで数えたMCP tool数がOpenAPI operation数と一致する。
- tool名がPython identifierで重複しない。
- 生成serverとclientがcompileallを通過する。

**Checkpoint:**

```text
feat: add deterministic OpenAPI MCP code generator
```

### Task 3: Add HTTP, Authentication, and Error Handling

**Files:**

- Create: `src/services/generated_mcp_runtime.py`
- Modify: `src/services/openapi_mcp_codegen.py`
- Create or modify: `tests/test_generated_mcp_runtime.py`
- Modify: `examples/kintone_stub_server.py`

**Work:**

- `API_BASE_URL` と `--base-url` を解決する。
- OpenAPI server URLとbase URLを結合し、path parameterを安全に展開する。
- query/header/bodyをOpenAPIの配置場所に従って組み立てる。
- JSON body、空body、JSON以外のレスポンスを扱う。
- API tokenを `X-Cybozu-API-Token` に設定する。
- Bearer/OAuthを `Authorization: Bearer ...` に設定する。
- Basic認証を環境変数から設定する。
- operation-level securityのOR条件に対応する。
- timeout、connection error、HTTP status、JSON decode errorを分類する。
- 401/403/404/409/429/5xxを、status、operation、target error code/messageを含む構造化結果として返す。
- 認証値、Authorization header、API token、全文の秘密bodyをログへ出さない。
- mock serverで認証失敗、404、429、500を再現できるfixtureを追加する。

**Acceptance:**

- 正しいAPI tokenでmock APIへ到達する。
- tokenなし・不正tokenはKintone APIへ送信される前に、またはmockの401として明確に返る。
- HTTPエラーが例外文字列だけでなく構造化JSONで返る。
- timeoutと接続失敗がHTTPエラーと区別される。
- secretsがログと応答に含まれない。

**Checkpoint:**

```text
feat: add generated MCP HTTP authentication and errors
```

### Task 4: Implement Tool Invocation in the Generated Client

**Files:**

- Modify: `results/azure/openapi/mcpserver/client.py` through the generator, not only by hand-editing one artifact
- Modify: `src/services/mcp_generator.py`
- Create or modify: `tests/test_generated_mcp_client.py`

**Work:**

- `--server-url`、`--transport`、`--tool`、`--arguments`、`--output-file`を提供する。
- tool指定なしでは一覧、tool指定時は実行結果を取得する。
- JSON argumentsを検証してMCP `call_tool`へ渡す。
- 成功・tool error・接続エラーを終了コードとJSON出力へ反映する。
- `streamable-http`をデフォルトにし、SSE指定時はSSE clientを使う。
- localhost接続時に環境プロキシの影響を受けないよう、ドキュメントと実行例を整える。

**Acceptance:**

- clientからKintoneのレコード登録toolを呼び出せる。
- `ids`と`revisions`を含む応答を取得できる。
- tool errorを握りつぶさず、再利用可能なJSONとして保存できる。

**Checkpoint:**

```text
feat: allow generated MCP client tool invocation
```

### Task 5: Complete Kintone Mock E2E Flow

**Files:**

- Modify: `examples/kintone_stub_server.py`
- Modify: `tests/test_kintone_stub_server.py`
- Create: `tests/test_kintone_mcp_e2e.py`
- Create: `examples/ocr/kintone-transfer.json`
- Modify: `examples/README.md`

**Interfaces:**

- `GET /__mock/errors/{status_code}`: returns a fixed Kintone-shaped error for `401`, `404`, `409`, `429`, or `500`; returns `MOCK_400` for unsupported statuses.
- `_error(status_code: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse`.
- `/__mock/requests` continues to return only `method`, `path`, `query`, `status_code`, and `has_api_token`.

**Work:**

- OCR標準形式の最小入力を用意する。
- Kintone MCP toolへ渡すrecord bodyを作る。
- mock serverを起動するfixtureを用意する。
- MCP serverをstreamable-httpで起動し、clientまたはMCP SDKからtoolを呼び出す。
- 登録後にrecord id/revisionを検証する。
- 登録レコードを取得し、OCR document_id、text、statusが保持されていることを検証する。
- update、duplicate/updateKey、deleteを検証する。
- mock request historyでAPI tokenが付与されたことだけを確認し、値は確認・出力しない。

**Acceptance:**

```text
OCR mock data
  -> generated MCP tool
  -> local Kintone mock
  -> ids/revisions response
  -> GET verification
```

この一連の経路が外部Kintoneなしで再現できること。

**Checkpoint:**

```text
test: verify Kintone MCP mock end-to-end flow
```

### Task 6: Documentation and Final Verification

**Files:**

- Modify: `README.md`
- Modify: `examples/README.md`
- Modify: generated `results/azure/openapi/mcpserver/README.md` template
- Create or modify: `tests/test_generated_contract.py`

**Work:**

- server起動、mock起動、token設定、client tool実行の3 terminal手順を記載する。
- `API_BASE_URL`、`KINTONE_API_TOKEN`、transport、MCP endpointを記載する。
- fallback成果物と完全生成成果物をREADMEで区別する。
- generated READMEの日時、transport、endpointを実際の生成内容と一致させる。
- 生成物に対して次を自動検証する。
  - fallback markerがない。
  - tool数がoperation数と一致する。
  - server/clientがcompileできる。
  - 主要Kintone operationsが存在する。

**Acceptance:**

- 新しい開発者がREADMEだけでmock E2Eを再現できる。
- 生成成功とfallbackを誤認できない。

**Checkpoint:**

```text
docs: document Kintone MCP mock verification
```

## Detailed Execution Tasks

### Task 1A: Make Fallback Generation Observable

**Files:**

- Modify: `src/services/mcp_generator.py`
- Modify: `src/cli.py`
- Test: `tests/test_mcp_generator.py`
- Test: `tests/test_cli.py`

**Interfaces:**

- `MCPGenerationError(RuntimeError)`: raised when a requested complete generation produces fallback code or an invalid artifact.
- `_count_openapi_operations(openapi_spec: Mapping[str, Any]) -> int`: counts HTTP operations under `paths`.
- `MCPServerGenerator._validate_generated_server(server_code: str, expected_operation_count: int) -> dict[str, Any]`: validates syntax, fallback marker, and tool count.
- The validation result contains `operation_count`, `tool_count`, `fallback`, `valid`, and `errors` fields.

- [ ] **Step 1: Add the failing fallback test**

```python
from src.services.mcp_generator import MCPServerGenerator


def test_validate_generated_server_rejects_fallback_code():
  code = MCPServerGenerator._generate_fallback_server(
    MCPServerGenerator.__new__(MCPServerGenerator),
    {"info": {"title": "Test"}, "paths": {}},
  )

  result = MCPServerGenerator._validate_generated_server(code, expected_operation_count=1)

  assert result["fallback"] is True
  assert result["valid"] is False
  assert any("fallback" in error.lower() for error in result["errors"])
```

- [ ] **Step 2: Run the focused test and confirm the missing validation**

Run: `uv run pytest tests/test_mcp_generator.py::test_validate_generated_server_rejects_fallback_code -q`

Expected: FAIL with `AttributeError` because `_validate_generated_server` does not exist yet.

- [ ] **Step 3: Add the validation error and report**

Implement `MCPGenerationError`, `_count_openapi_operations`, and `_validate_generated_server` in `src/services/mcp_generator.py`. Count only keys in `{"get", "post", "put", "patch", "delete", "head", "options", "trace"}`. Reject code containing `Fallback implementation` or a tool count different from the operation count. Return the exact keys `valid`, `fallback`, `operation_count`, `tool_count`, and `errors`.

```python
class MCPGenerationError(RuntimeError):
  """Raised when generated MCP code is not a complete executable artifact."""


def _count_openapi_operations(openapi_spec: Mapping[str, Any]) -> int:
  methods = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
  return sum(
    1
    for path_item in openapi_spec.get("paths", {}).values()
    for method in path_item
    if method.lower() in methods
  )


@staticmethod
def _validate_generated_server(code: str, expected_operation_count: int) -> dict[str, Any]:
  errors: list[str] = []
  try:
    ast.parse(code)
  except SyntaxError as exc:
    errors.append(f"syntax error: {exc.msg}")

  tools = _parse_mcp_tools_from_code(code)
  fallback = "Fallback implementation" in code or "fallback mode" in code.lower()
  if "from mcp.server.fastmcp import FastMCP" not in code:
    errors.append("missing required FastMCP import")
  if "def main(" not in code:
    errors.append("missing main function")
  if fallback:
    errors.append("generated code is a fallback implementation")
  if len(tools) != expected_operation_count:
    errors.append(
      f"tool count {len(tools)} does not match operation count {expected_operation_count}"
    )

  return {
    "valid": not errors,
    "fallback": fallback,
    "operation_count": expected_operation_count,
    "tool_count": len(tools),
    "errors": errors,
  }
```

- [ ] **Step 4: Reject fallback before writing generated files**

Call `_validate_generated_server` immediately after server code is returned and before writing `server.py`. Preserve the existing `generate_server_code(evaluation_result, openapi_spec, output_dir)` tuple return for callers, but raise `MCPGenerationError` for invalid complete generation. Add `generation_status="complete"` and the validation counts to the usage dictionary only after validation succeeds.

- [ ] **Step 5: Add the CLI failure test**

```python
@pytest.mark.asyncio
async def test_generate_mcp_server_returns_failed_usage(monkeypatch, tmp_path):
  from types import SimpleNamespace
  from unittest.mock import MagicMock

  from src import cli
  from src.services.mcp_generator import MCPGenerationError
  from src.services.output_config import OutputConfig

  async def fail_generation(_openapi_spec, _output_dir):
    raise MCPGenerationError("generated code is a fallback implementation")

  monkeypatch.setattr(
    cli,
    "MCPServerGenerator",
    lambda **_kwargs: SimpleNamespace(generate_mcp_server=fail_generation),
  )
  output_config = OutputConfig(provider_name="test", spec_name="api", timestamp="now")
  output_config.get_results_dir = lambda: tmp_path

  path, usage = await cli._generate_mcp_server(
    MagicMock(), {"info": {"title": "Test"}, "paths": {}}, output_config
  )

  assert path is None
  assert usage["generation_status"] == "failed"
  assert "fallback" in usage["validation_errors"][0].lower()
```

The test asserts on the returned failure metadata and the sanitized error category, not only on a mocked call count.

- [ ] **Step 6: Surface failure status in CLI output and usage JSON**

Update `_generate_mcp_server` so a failed generator returns `(None, usage)` with `generation_status="failed"`, `validation_errors`, `operation_count`, and `tool_count`, does not claim that all artifacts were generated, and never writes a successful usage record. After `_handle_mcp_generation` persists that failure usage, raise `MCPGenerationError` with the sanitized first validation error; add an `except MCPGenerationError` branch in `_execute_cli_workflow` that prints `MCP generation failed` and returns `False`, so `cli_main()` exits with status 1. Keep `--eval-only` behavior unchanged.

Extend the CLI test with:

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

args = SimpleNamespace(eval_only=False)
monkeypatch.setattr(cli, "_check_mcp_generation_criteria", lambda _evaluation: True)
monkeypatch.setattr(
  cli,
  "_generate_mcp_server",
  AsyncMock(
    return_value=(
      None,
      {
        "generation_status": "failed",
        "validation_errors": ["generated code is a fallback implementation"],
      },
    )
  ),
)
monkeypatch.setattr(cli, "_update_evaluation_with_mcp_usage", lambda *_args: None)

with pytest.raises(MCPGenerationError, match="MCP generation failed"):
  await cli._handle_mcp_generation(
    args, MagicMock(), "{}", MagicMock(), MagicMock()
  )
```

- [ ] **Step 7: Run the focused tests and the existing generator tests**

Run: `uv run pytest tests/test_mcp_generator.py tests/test_cli.py -q`

Expected: PASS, with existing tests unchanged except for assertions that now distinguish complete generation from fallback.

- [ ] **Step 8: Run the full regression suite**

Run: `uv run pytest -q`

Expected: PASS, with the existing 188-test baseline preserved plus the new fallback and CLI assertions.

- [ ] **Step 9: Commit the failure visibility change**

```bash
git add src/services/mcp_generator.py src/cli.py tests/test_mcp_generator.py tests/test_cli.py
git commit -m "test: expose MCP generation fallback failures"
```

### Task 2A: Normalize OpenAPI Operations and Names

**Files:**

- Create: `src/services/openapi_mcp_codegen.py`
- Test: `tests/test_openapi_mcp_codegen.py`

**Interfaces:**

- `ParameterMetadata(wire_name: str, python_name: str, location: Literal["path", "query", "header", "cookie"], required: bool, schema: Mapping[str, Any], description: str | None)`.
- `RequestBodyMetadata(required: bool, content_type: str, schema: Mapping[str, Any] | None)`.
- `OperationMetadata(operation_id: str, tool_name: str, method: str, path: str, parameters: tuple[ParameterMetadata, ...], request_body: RequestBodyMetadata | None, security: tuple[dict[str, tuple[str, ...]], ...], security_schemes: Mapping[str, Mapping[str, Any]])`.
- `collect_operations(openapi_spec: Mapping[str, Any]) -> list[OperationMetadata]`.
- `count_operations(openapi_spec: Mapping[str, Any]) -> int`.
- `make_tool_name(operation_id: str | None, method: str, path: str, used_names: set[str]) -> str`.

- [ ] **Step 1: Create a minimal OpenAPI fixture in the test**

```python
MINIMAL_SPEC = {
  "openapi": "3.0.3",
  "info": {"title": "Test API", "version": "1"},
  "paths": {
    "/records/{recordId}": {
      "get": {
        "operationId": "getRecord",
        "parameters": [
          {"name": "recordId", "in": "path", "required": True, "schema": {"type": "integer"}},
          {"name": "fields", "in": "query", "required": False, "schema": {"type": "array", "items": {"type": "string"}}},
        ],
      },
      "post": {
        "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
      },
    }
  },
}
```

- [ ] **Step 2: Write failing tests for operation count and stable names**

```python
def test_collect_operations_extracts_path_query_and_body_metadata():
  operations = collect_operations(MINIMAL_SPEC)

  assert len(operations) == 2
  assert operations[0].tool_name == "getRecord"
  assert operations[0].parameters[0].python_name == "record_id"
  assert operations[0].parameters[1].location == "query"
  assert operations[1].request_body.required is True


def test_tool_name_is_unique_when_operation_ids_are_missing_or_duplicated():
  names = {
    make_tool_name("same", "get", "/a", set()),
    make_tool_name("same", "get", "/b", {"same"}),
  }

  assert names == {"same", "same_2"}
```

- [ ] **Step 3: Run the tests and confirm missing implementation**

Run: `uv run pytest tests/test_openapi_mcp_codegen.py -q`

Expected: FAIL because `src/services/openapi_mcp_codegen.py` does not exist.

- [ ] **Step 4: Implement operation collection**

Walk `paths` in insertion order, process HTTP methods in sorted order, ignore path-level keys, accept the eight HTTP methods, resolve local `$ref` schemas before storing metadata, and preserve each operation's `security` list. If an operation has no `security`, inherit the top-level `security`. Copy `components.securitySchemes` into `security_schemes`. Convert invalid Python parameter names to snake case and append `_value`, `_header`, or `_query` when two parameters collide.

- [ ] **Step 5: Verify the real Kintone operation inventory**

```python
spec = yaml.safe_load(Path("input_data/openapi-spec-1/openapi.yaml").read_text())
operations = collect_operations(spec)
assert count_operations(spec) == 204
assert len({operation.tool_name for operation in operations}) == 204
assert {operation.tool_name for operation in operations} >= {
  "getRecord", "postRecord", "putRecord", "getRecords", "postRecords", "putRecords", "deleteRecords"
}
```

- [ ] **Step 6: Run the focused tests**

Run: `uv run pytest tests/test_openapi_mcp_codegen.py -q`

Expected: PASS with two collected operations, stable unique names, resolved body schema, and the 204-operation inventory assertion.

- [ ] **Step 7: Run the full regression suite**

Run: `uv run pytest -q`

Expected: PASS; no existing evaluation, loader, or mock tests regress.

- [ ] **Step 8: Commit metadata normalization**

```bash
git add src/services/openapi_mcp_codegen.py tests/test_openapi_mcp_codegen.py
git commit -m "feat: normalize OpenAPI operations for MCP generation"
```

### Task 2B: Render a Deterministic MCP Server

**Files:**

- Modify: `src/services/openapi_mcp_codegen.py`
- Modify: `src/services/mcp_generator.py`
- Test: `tests/test_openapi_mcp_codegen.py`
- Test: `tests/test_mcp_generator.py`

**Interfaces:**

- `render_server_source(api_title: str, operations: Sequence[OperationMetadata]) -> str`.
- `render_client_source(api_title: str) -> str`.
- `render_runtime_source() -> str`.
- `write_generated_artifacts(openapi_spec: Mapping[str, Any], output_dir: Path) -> dict[str, Path]`.

- [ ] **Step 1: Write the failing render test**

```python
def test_render_server_contains_one_tool_per_operation():
  source = render_server_source("Test API", collect_operations(MINIMAL_SPEC))

  assert "from mcp.server.fastmcp import FastMCP" in source
  assert source.count("@mcp.tool()") == 2
  assert "def getRecord(" in source
  assert "def post_records_record_id(" in source
  assert "Fallback implementation" not in source
```

- [ ] **Step 2: Run the render test and confirm it fails**

Run: `uv run pytest tests/test_openapi_mcp_codegen.py::test_render_server_contains_one_tool_per_operation -q`

Expected: FAIL because the deterministic renderer is not implemented.

- [ ] **Step 3: Render the server skeleton and operation metadata**

Generate imports, argument annotations, `FastMCP` initialization, `parse_arguments`, `main`, `@mcp.prompt`, `@mcp.resource`, and one function per `OperationMetadata`. Each tool must pass a dictionary of its arguments and its operation metadata to `GeneratedMcpRuntime.request`. On `StructuredApiError`, return `exc.as_dict()` so the public response shape is exactly one top-level `error` object.

- [ ] **Step 4: Render request-body annotations without losing arbitrary fields**

Use `dict[str, Any]` for object request bodies and `list[Any]` for array bodies. Required parameters must have no Python default; optional parameters must default to `None` or the OpenAPI default. Preserve the OpenAPI description in `Field(description=...)`.

- [ ] **Step 5: Render the generated runtime and client as sibling files**

Write `server.py`, `runtime.py`, `client.py`, `tool_spec.txt`, `requirements.txt`, and `README.md` in the output directory. The generated `server.py` imports `runtime.py` by sibling module name so it can run from the output directory without importing the source repository. Render `requirements.txt` with `fastmcp`, `httpx`, `pydantic`, `PyYAML`, and the MCP SDK; do not add `requests` to the deterministic artifact.

- [ ] **Step 6: Replace the LLM-first generation path**

Make `write_generated_artifacts(openapi_spec, output_dir)` the default implementation of `MCPServerGenerator.generate_server_code` and remove the normal-path calls to `_generate_server_file_with_llm` and `_generate_client_file_with_llm`; the 1.1M-character prompt must not be built during complete generation. Preserve the current public `generate_mcp_server(openapi_spec, output_dir)` entry point and keep the legacy LLM methods only behind an explicitly named opt-in if existing callers require them. Do not edit `results/azure/openapi/mcpserver/server.py` directly.

- [ ] **Step 7: Validate generated Kintone artifacts**

Run:

```bash
uv run openapi-to-mcp input_data/openapi-spec-1/openapi.yaml --verbose
uv run python -m compileall results/azure/openapi/mcpserver
```

Then assert with AST that `server.py` has 204 `@mcp.tool()` functions and no fallback marker.

- [ ] **Step 8: Run the focused tests**

Run: `uv run pytest tests/test_openapi_mcp_codegen.py tests/test_mcp_generator.py -q`

Expected: PASS, including one generated tool per fixture operation and deterministic artifact writing.

- [ ] **Step 9: Run the full regression suite**

Run: `uv run pytest -q`

Expected: PASS; existing generator compatibility tests still pass with deterministic generation as the default.

- [ ] **Step 10: Commit deterministic rendering**

```bash
git add src/services/openapi_mcp_codegen.py src/services/mcp_generator.py tests/test_openapi_mcp_codegen.py tests/test_mcp_generator.py
git commit -m "feat: generate deterministic OpenAPI MCP server"
```

### Task 3A: Implement Generated HTTP Runtime and Authentication

**Files:**

- Create: `src/services/generated_mcp_runtime.py`
- Modify: `src/services/openapi_mcp_codegen.py`
- Test: `tests/test_generated_mcp_runtime.py`

**Interfaces:**

- `StructuredApiError(kind: str, operation_id: str, message: str, status_code: int | None = None, target_code: str | None = None)` with `as_dict() -> dict[str, Any]`.
- `GeneratedMcpRuntime(base_url: str, timeout_seconds: float = 30.0, environ: Mapping[str, str] | None = None, transport: httpx.BaseTransport | None = None)`.
- `GeneratedMcpRuntime.request(operation: Mapping[str, Any], arguments: Mapping[str, Any]) -> Any`.
- `resolve_auth_headers(security: Sequence[Mapping[str, Sequence[str]]], schemes: Mapping[str, Mapping[str, Any]], environ: Mapping[str, str]) -> dict[str, str]`.

`StructuredApiError.as_dict()` returns `{"error": {"kind": ..., "operation_id": ..., "status_code": ..., "target_code": ..., "message": ...}}`; omit `status_code` and `target_code` when their values are `None`.

- [ ] **Step 1: Write failing authentication tests**

```python
from src.services.generated_mcp_runtime import resolve_auth_headers


def test_kintone_api_token_is_added_to_the_expected_header():
  headers = resolve_auth_headers(
    [{"apiToken": []}],
    {"apiToken": {"type": "apiKey", "in": "header", "name": "X-Cybozu-API-Token"}},
    {"KINTONE_API_TOKEN": "mock-token"},
  )

  assert headers == {"X-Cybozu-API-Token": "mock-token"}


def test_security_requirements_are_or_alternatives():
  headers = resolve_auth_headers(
    [{"apiToken": []}, {"oauth2": ["k:app_record:read"]}],
    {
      "apiToken": {"type": "apiKey", "in": "header", "name": "X-Cybozu-API-Token"},
      "oauth2": {"type": "oauth2"},
    },
    {"OPENAPI_OAUTH_TOKEN_OAUTH2": "bearer-token"},
  )

  assert headers == {"Authorization": "Bearer bearer-token"}


def test_basic_auth_requires_both_credentials_and_returns_basic_header():
  headers = resolve_auth_headers(
    [{"basicAuth": []}],
    {"basicAuth": {"type": "http", "scheme": "basic"}},
    {
      "OPENAPI_BASIC_USERNAME_BASICAUTH": "user",
      "OPENAPI_BASIC_PASSWORD_BASICAUTH": "pass",
    },
  )

  assert headers["Authorization"].startswith("Basic ")
```

- [ ] **Step 2: Write failing request construction tests**

Patch `httpx.Client.request` and assert that `POST /k/v1/records.json` receives the resolved URL, query parameters, headers, JSON body, and configured timeout. Assert that a path value containing `/` is URL-encoded as one path segment.

```python
def test_request_assembles_httpx_arguments(monkeypatch):
  import httpx

  captured = {}

  def fake_request(_client, method, url, **kwargs):
    captured.update(method=method, url=url, kwargs=kwargs)
    return httpx.Response(200, json={"ok": True})

  monkeypatch.setattr(httpx.Client, "request", fake_request)
  runtime = GeneratedMcpRuntime("http://127.0.0.1:9100", timeout_seconds=7.5)

  result = runtime.request(
    {"method": "POST", "path": "/records/{recordId}", "operation_id": "postRecord",
     "parameters": [], "request_body": None, "security": [], "security_schemes": {}},
    {"path": {"recordId": "a/b"}, "query": {"fields": ["name"]},
     "headers": {"X-Trace": "trace"}, "body": {"name": "Ada"}},
  )

  assert result == {"ok": True}
  assert captured["method"] == "POST"
  assert captured["url"] == "http://127.0.0.1:9100/records/a%2Fb"
  assert captured["kwargs"]["params"] == {"fields": ["name"]}
  assert captured["kwargs"]["headers"]["X-Trace"] == "trace"
  assert captured["kwargs"]["json"] == {"name": "Ada"}
  assert captured["kwargs"]["timeout"] == 7.5
```

Also add two tests using the same `monkeypatch` technique: make `httpx.Client.request` raise `httpx.TimeoutException` and assert `StructuredApiError.kind == "timeout"`; return a 200 response with `content=b"not-json"` and `Content-Type: application/json`, then assert `StructuredApiError.kind == "invalid_json"` and that the message does not contain the response bytes.

- [ ] **Step 3: Run the focused tests and confirm they fail**

Run: `uv run pytest tests/test_generated_mcp_runtime.py -q`

Expected: FAIL because the runtime module and authentication resolver are not implemented.

- [ ] **Step 4: Implement deterministic environment-variable resolution**

Use these exact names for generated Kintone-compatible operations: `KINTONE_API_TOKEN` for an `apiToken` scheme, `OPENAPI_BEARER_TOKEN_<SCHEME_NAME>` for HTTP bearer schemes, `OPENAPI_OAUTH_TOKEN_<SCHEME_NAME>` for OAuth2 schemes, `OPENAPI_API_KEY_<SCHEME_NAME>` for other apiKey schemes, and `OPENAPI_BASIC_USERNAME_<SCHEME_NAME>` plus `OPENAPI_BASIC_PASSWORD_<SCHEME_NAME>` for Basic schemes. Normalize `<SCHEME_NAME>` to uppercase ASCII with non-alphanumeric characters replaced by `_`. The generated source must pass the OpenAPI security scheme name into this resolver so the environment key is deterministic.

- [ ] **Step 5: Implement security OR and AND semantics**

Treat the security list as OR alternatives and the scheme names within one object as AND requirements. Select the first fully configured alternative. Raise `StructuredApiError(kind="configuration", ...)` when no alternative has complete credentials.

- [ ] **Step 6: Implement URL and request assembly**

Resolve `{pathParameter}` with `urllib.parse.quote(..., safe="")`, put query parameters in `params`, header parameters in `headers`, and the request body in `json`. Reject an operation when a required parameter or required body is missing. Use the OpenAPI method in uppercase.

- [ ] **Step 7: Implement response and error classification**

Return decoded JSON for JSON responses, `None` for empty responses, and text for other content types. Convert errors to this shape without credentials:

```json
{
  "error": {
  "kind": "http|connection|timeout|invalid_json|configuration",
  "operation_id": "postRecords",
  "status_code": 401,
  "target_code": "CB_AU01",
  "message": "sanitized message"
  }
}
```

Never include `Authorization`, `X-Cybozu-API-Token`, request headers, or full request bodies in the returned message or log record. The runtime raises `StructuredApiError`; generated tools convert it to `exc.as_dict()`.

- [ ] **Step 8: Run the focused runtime tests**

Run: `uv run pytest tests/test_generated_mcp_runtime.py -q`

Expected: PASS for API-token auth, OR/AND security selection, URL encoding, timeout forwarding, HTTP errors, connection errors, timeout errors, invalid JSON, and redaction.

- [ ] **Step 9: Run the full regression suite**

Run: `uv run pytest -q`

Expected: PASS; the runtime addition does not alter existing OpenAPI evaluation behavior.

- [ ] **Step 10: Commit runtime and authentication**

```bash
git add src/services/generated_mcp_runtime.py src/services/openapi_mcp_codegen.py tests/test_generated_mcp_runtime.py
git commit -m "feat: add generated MCP HTTP authentication and errors"
```

### Task 3B: Extend the Kintone Mock for Error Paths

**Files:**

- Modify: `examples/kintone_stub_server.py`
- Modify: `tests/test_kintone_stub_server.py`

**Interfaces:**

- `GET /__mock/errors/{status_code}`: returns a fixed Kintone-shaped error for `401`, `404`, `409`, `429`, or `500`; returns `MOCK_400` for unsupported statuses.
- `_error(status_code: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse`.
- `/__mock/requests` continues to return only `method`, `path`, `query`, `status_code`, and `has_api_token`.

- [ ] **Step 1: Add failing mock error tests**

Add these tests to `tests/test_kintone_stub_server.py`:

```python
import json

import pytest


@pytest.mark.parametrize("status_code", [401, 404, 409, 429, 500])
def test_mock_can_reproduce_http_error(status_code: int) -> None:
  response = client.get(f"/__mock/errors/{status_code}")

  assert response.status_code == status_code
  assert response.json() == {
    "code": f"MOCK_{status_code}",
    "id": "mock-error",
    "message": f"simulated status {status_code}",
  }
  if status_code == 429:
    assert response.headers["Retry-After"] == "1"


def test_mock_request_history_redacts_invalid_token() -> None:
  client.get(
    "/k/v1/records.json",
    params={"app": 1},
    headers={"X-Cybozu-API-Token": "wrong-token"},
  )

  history = json.dumps(client.get("/__mock/requests").json())
  assert "wrong-token" not in history


def test_get_over_post_matches_get_records() -> None:
  expected = client.get(
    "/k/v1/records.json",
    params={"app": 1, "query": 'status = "registered"', "totalCount": True},
    headers=HEADERS,
  ).json()
  response = client.post(
    "/k/v1/records.json",
    json={"app": 1, "query": 'status = "registered"', "totalCount": True},
    headers={**HEADERS, "X-HTTP-Method-Override": "GET"},
  )

  assert response.status_code == 200
  assert response.json() == expected
```

- [ ] **Step 2: Run the focused mock tests and confirm the new cases fail**

Run: `uv run pytest tests/test_kintone_stub_server.py::test_mock_can_reproduce_http_error -q`

Expected: FAIL with 404 responses because `/__mock/errors/{status_code}` does not exist yet.

- [ ] **Step 3: Implement deterministic error endpoints**

Extend `_error` with an optional headers mapping and add this route after the mock health route:

```python
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
```

Keep existing CRUD behavior unchanged and ensure `_request_log` records only the existing method/path/query/status/boolean fields.

- [ ] **Step 4: Add GET-over-POST regression coverage**

The test in Step 1 must remain next to the existing CRUD test. Do not replace the mock's current method-override branch; the regression must compare its `records` and `totalCount` values with the existing GET result.

- [ ] **Step 5: Run the focused mock tests**

Run: `uv run pytest tests/test_kintone_stub_server.py -q`

Expected: PASS for all existing CRUD/auth tests plus the 401/404/409/429/500, GET-over-POST, and secret-redaction cases.

- [ ] **Step 6: Run the full regression suite**

Run: `uv run pytest -q`

Expected: PASS; existing mock CRUD behavior remains unchanged.

- [ ] **Step 7: Commit the mock error contract**

```bash
git add examples/kintone_stub_server.py tests/test_kintone_stub_server.py
git commit -m "test: cover Kintone mock error responses"
```

### Task 4: Make the Generated Client Invoke Tools

**Files:**

- Create: `src/services/generated_mcp_client.py`
- Modify: `src/services/openapi_mcp_codegen.py`
- Modify: `src/services/mcp_generator.py`
- Test: `tests/test_generated_mcp_client.py`

**Interfaces:**

- `parse_client_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace`.
- `parse_tool_arguments(raw: str | None) -> dict[str, Any]`.
- `async def invoke_tool(server_url: str, transport: str, tool_name: str | None, arguments: dict[str, Any]) -> Any`.
- `serialize_call_result(result: Any) -> Any`.

- [ ] **Step 1: Write failing client argument tests**

```python
import json
from contextlib import asynccontextmanager

import pytest

from src.services.generated_mcp_client import parse_client_arguments, parse_tool_arguments


def test_client_requires_arguments_when_a_tool_is_selected():
  args = parse_client_arguments(["--tool", "postRecords", "--arguments", '{"body": {}}'])

  assert args.tool == "postRecords"
  assert json.loads(args.arguments) == {"body": {}}
  assert args.transport == "streamable-http"


def test_parse_tool_arguments_requires_a_json_object():
  with pytest.raises(ValueError, match="JSON object"):
    parse_tool_arguments("[1, 2]")
```

- [ ] **Step 2: Write failing tool invocation test**

Patch the transport context manager with a fake read/write pair and patch `ClientSession` with a fake async context manager whose `call_tool` returns a `CallToolResult` containing `{"ids": ["2"], "revisions": ["2"]}`. Assert that `invoke_tool` returns that JSON and does not only list tools.

```python
@asynccontextmanager
async def fake_streamable_transport(*_args, **_kwargs):
  yield object(), object(), lambda: "test-session"


@pytest.mark.asyncio
async def test_invoke_tool_calls_call_tool(monkeypatch):
  from types import SimpleNamespace
  from src.services.generated_mcp_client import invoke_tool

  class FakeSession:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      return False

    async def initialize(self):
      return None

    async def call_tool(self, name, arguments):
      assert name == "postRecords"
      assert arguments == {"body": {}}
      return SimpleNamespace(content=[SimpleNamespace(text='{"ids":["2"],"revisions":["2"]}')], isError=False)

  monkeypatch.setattr("src.services.generated_mcp_client.ClientSession", lambda *_args, **_kwargs: FakeSession())
  monkeypatch.setattr("src.services.generated_mcp_client.streamablehttp_client", fake_streamable_transport)

  assert await invoke_tool("http://127.0.0.1:9001/mcp/", "streamable-http", "postRecords", {"body": {}}) == {"ids": ["2"], "revisions": ["2"]}
```

- [ ] **Step 3: Run the focused client tests and confirm the invocation path fails**

Run: `uv run pytest tests/test_generated_mcp_client.py -q`

Expected: FAIL because `src/services/generated_mcp_client.py` and `invoke_tool` are not implemented.

- [ ] **Step 4: Implement transport-specific connection**

Use `streamablehttp_client` for `streamable-http` and `sse_client` for `sse`. Normalize the endpoint by preserving `/mcp/` for streamable HTTP and `/sse/` for SSE. Set localhost `NO_PROXY` only in the child client process environment and never print proxy credentials.

- [ ] **Step 5: Implement selected tool execution and output**

When `--tool` is absent, list tools. When it is present, parse `--arguments` as a JSON object, call `session.call_tool`, serialize text/JSON content, write `--output-file` if provided, and return a nonzero exit status for MCP tool errors or connection errors. Keep argument parsing in `src/services/generated_mcp_client.py`; `render_client_source` must embed the same implementation so generated artifacts do not import the repository.

- [ ] **Step 6: Regenerate artifacts through the generator**

Run the generator against the Kintone spec and confirm that `results/azure/openapi/mcpserver/client.py` contains `--tool`, `--arguments`, and `call_tool`. Do not hand-edit the generated artifact.

- [ ] **Step 7: Run the focused client tests**

Run: `uv run pytest tests/test_generated_mcp_client.py -q`

Expected: PASS for argument parsing, streamable-http tool invocation, JSON result serialization, list-only mode, output-file writing, and nonzero tool-error status.

- [ ] **Step 8: Run the full regression suite**

Run: `uv run pytest -q`

Expected: PASS; generated client changes do not affect evaluation or server generation tests outside this slice.

- [ ] **Step 9: Commit client invocation**

```bash
git add src/services/generated_mcp_client.py src/services/openapi_mcp_codegen.py src/services/mcp_generator.py tests/test_generated_mcp_client.py
git commit -m "feat: allow generated MCP client tool invocation"
```

### Task 5: Verify the Full Kintone Mock Transfer

**Files:**

- Create: `examples/ocr/kintone-transfer.json`
- Create: `tests/test_kintone_mcp_e2e.py`
- Modify: `examples/kintone_stub_server.py`
- Modify: `tests/test_kintone_stub_server.py`

**Interfaces:**

- E2E fixture starts the mock with token `mock-token` on a dynamically selected localhost port.
- Generated server receives `API_BASE_URL=http://127.0.0.1:<mock-port>` and `KINTONE_API_TOKEN=mock-token`.
- The E2E invokes generated tools by these operationIds: `postRecords`, `getRecord`, `putRecord`, and `deleteRecords`.

- [ ] **Step 1: Add the OCR transfer fixture**

Create `examples/ocr/kintone-transfer.json` with this synthetic, non-secret payload:

```json
{
  "schema_version": "ocr-transfer/v1",
  "document": {
    "document_id": "doc-kintone-001",
    "document_type": "invoice",
    "source_system": "mock-ocr"
  },
  "ocr": {
    "fields": {
      "invoice_number": {"value": "INV-001", "value_type": "string", "confidence": 0.99, "status": "extracted"},
      "total_amount": {"value": 12500, "value_type": "currency", "confidence": 0.97, "status": "extracted"}
    },
    "line_items": []
  },
  "delivery": {"connector_id": "kintone-mock"},
  "metadata": {"tenant_id": "mock-tenant", "correlation_id": "corr-kintone-001"}
}
```

- [ ] **Step 2: Write the failing subprocess E2E test**

Add this test to `tests/test_kintone_mcp_e2e.py`; it must fail before the generated server, runtime, and mock wiring are complete because the subprocesses cannot yet provide a working `postRecords` call:

```python
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from src.services.openapi_mcp_codegen import write_generated_artifacts


def free_tcp_port() -> int:
  with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    return int(sock.getsockname()[1])


def wait_for_http(url: str) -> None:
  deadline = time.monotonic() + 15
  while time.monotonic() < deadline:
    try:
      response = httpx.get(url, trust_env=False, timeout=0.5)
      if response.status_code < 500:
        return
    except httpx.HTTPError:
      pass
    time.sleep(0.1)
  raise AssertionError(f"service did not become ready: {url}")


@pytest.mark.asyncio
async def test_ocr_record_round_trip(tmp_path: Path) -> None:
  spec = yaml.safe_load(Path("input_data/openapi-spec-1/openapi.yaml").read_text())
  transfer = json.loads(Path("examples/ocr/kintone-transfer.json").read_text())
  record = {
    "ocr_id": {"value": transfer["document"]["document_id"]},
    "text": {"value": transfer["ocr"]["fields"]["invoice_number"]["value"]},
    "status": {"value": "registered"},
  }
  generated_dir = tmp_path / "mcpserver"
  write_generated_artifacts(spec, generated_dir)
  mock_port = free_tcp_port()
  mcp_port = free_tcp_port()
  environment = {**os.environ, "API_BASE_URL": f"http://127.0.0.1:{mock_port}", "KINTONE_API_TOKEN": "mock-token", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
  mock_process = subprocess.Popen([sys.executable, "examples/kintone_stub_server.py", "--port", str(mock_port), "--token", "mock-token"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
  server_process = subprocess.Popen([sys.executable, "server.py", "--port", str(mcp_port), "--transport", "streamable-http"], cwd=generated_dir, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

  try:
    wait_for_http(f"http://127.0.0.1:{mock_port}/__mock/health")
    wait_for_http(f"http://127.0.0.1:{mcp_port}/mcp/")
    async with streamablehttp_client(url=f"http://127.0.0.1:{mcp_port}/mcp/") as (read, write, _):
      async with ClientSession(read, write) as session:
        await session.initialize()
        created = await session.call_tool("postRecords", {"body": {"app": 1, "records": [record]}})
        assert created.isError is False
        create_payload = json.loads(created.content[0].text)
        record_id = create_payload["ids"][0]
        fetched = await session.call_tool("getRecord", {"app": 1, "id": record_id})
        assert json.loads(fetched.content[0].text)["record"]["ocr_id"]["value"] == transfer["document"]["document_id"]
  finally:
    for process in (server_process, mock_process):
      process.terminate()
    for process in (server_process, mock_process):
      process.wait(timeout=5)
```

- [ ] **Step 3: Run the failing E2E test**

Run: `uv run pytest tests/test_kintone_mcp_e2e.py::test_ocr_record_round_trip -q`

Expected: FAIL because the generated artifact cannot yet complete the mock-backed `postRecords` call.

- [ ] **Step 4: Implement the process readiness fixture**

Use the `free_tcp_port()` and `wait_for_http()` helpers from Step 2, poll with `httpx` and `trust_env=False`, and terminate processes with a bounded wait. If a process exits early, include captured stdout/stderr in the assertion message only after replacing every occurrence of `mock-token` with `***redacted***`.

- [ ] **Step 5: Verify create, read, update, and delete**

Extend the same MCP session with these calls and assertions:

```python
updated = await session.call_tool(
  "putRecord",
  {"body": {"app": 1, "id": record_id, "record": {"status": {"value": "processed"}}}},
)
assert json.loads(updated.content[0].text)["revision"]
read_after_update = await session.call_tool("getRecord", {"app": 1, "id": record_id})
assert json.loads(read_after_update.content[0].text)["record"]["status"]["value"] == "processed"
deleted = await session.call_tool("deleteRecords", {"body": {"app": 1, "ids": [record_id]}})
assert deleted.isError is False
missing = await session.call_tool("getRecord", {"app": 1, "id": record_id})
assert json.loads(missing.content[0].text)["error"]["status_code"] == 404
```

- [ ] **Step 6: Verify auth and request history without leaking the token**

Run one generated-server process with `KINTONE_API_TOKEN=wrong-token`, call `getRecord`, and assert `error.kind == "http"`, `error.status_code == 401`, and `error.target_code == "CB_AU01"`. Query `/__mock/requests` with `httpx.get(..., trust_env=False)`, assert `has_api_token is True` for successful requests, and assert the string `mock-token` is absent from serialized logs, generated tool results, and captured process output.

- [ ] **Step 7: Run the focused E2E test**

Run: `uv run pytest tests/test_kintone_mcp_e2e.py -q`

Expected: PASS for create/read/update/delete, API-token authentication, structured 404, request history, and secret redaction.

- [ ] **Step 8: Run the full regression suite**

Run: `uv run pytest -q`

Expected: PASS without external network access.

- [ ] **Step 9: Commit the E2E flow**

```bash
git add examples/ocr/kintone-transfer.json examples/kintone_stub_server.py tests/test_kintone_stub_server.py tests/test_kintone_mcp_e2e.py
git commit -m "test: verify Kintone MCP mock end-to-end flow"
```

### Task 6: Contract, Documentation, and Final Verification

**Files:**

- Modify: `README.md`
- Modify: `examples/README.md`
- Modify: `src/services/mcp_generator.py`
- Create: `tests/test_generated_contract.py`
- Regenerate: `results/azure/openapi/mcpserver/*`

**Interfaces:**

- `tests/test_generated_contract.py` compares `count_operations(spec)` with `MCPServerGenerator._validate_generated_server(...)` and requires `server.py`, `runtime.py`, and the Kintone token header.
- `write_generated_artifacts(spec, output_dir)` is the only supported path for regenerating tracked artifacts.

- [ ] **Step 1: Add generated contract assertions**

Create `tests/test_generated_contract.py` with this executable contract check:

```python
from pathlib import Path

import yaml

from src.services.mcp_generator import MCPServerGenerator
from src.services.openapi_mcp_codegen import count_operations


def test_generated_kintone_artifacts_match_openapi() -> None:
  spec = yaml.safe_load(Path("input_data/openapi-spec-1/openapi.yaml").read_text())
  server_path = Path("results/azure/openapi/mcpserver/server.py")
  runtime_path = server_path.with_name("runtime.py")
  source = server_path.read_text()
  result = MCPServerGenerator._validate_generated_server(source, count_operations(spec))

  assert result["valid"] is True
  assert result["fallback"] is False
  assert result["operation_count"] == 204
  assert result["tool_count"] == 204
  assert "def postRecords(" in source
  assert "def getRecord(" in source
  assert runtime_path.exists()
  assert "X-Cybozu-API-Token" in runtime_path.read_text()
```

- [ ] **Step 2: Run the contract test before regeneration and confirm failure**

Run: `uv run pytest tests/test_generated_contract.py -q`

Expected: FAIL because the current checked-out artifact has one fallback tool and no generated `runtime.py`.

- [ ] **Step 3: Update generated README rendering**

Document the exact commands below with the generated transport and endpoint:

```bash
# terminal 1
uv run python examples/kintone_stub_server.py --port 9100 --token mock-token

# terminal 2
API_BASE_URL=http://127.0.0.1:9100 \
KINTONE_API_TOKEN=mock-token \
uv run python results/azure/openapi/mcpserver/server.py --port 9001 --transport streamable-http

# terminal 3
NO_PROXY=127.0.0.1,localhost \
uv run python results/azure/openapi/mcpserver/client.py \
  --server-url http://127.0.0.1:9001/mcp/ \
  --tool postRecords \
  --arguments '{"body":{"app":1,"records":[]}}'
```

Replace the illustrative empty record list in the actual documentation with the synthetic payload from `examples/ocr/kintone-transfer.json`.

- [ ] **Step 4: Regenerate and validate all artifacts**

Run:

```bash
uv run openapi-to-mcp input_data/openapi-spec-1/openapi.yaml --verbose
uv run python -m compileall results/azure/openapi/mcpserver
uv run pytest tests/test_generated_contract.py -q
```

The command must produce `server.py`, `runtime.py`, `client.py`, `tool_spec.txt`, `requirements.txt`, and `README.md` without fallback status.

- [ ] **Step 5: Run focused and full verification**

```bash
uv run pytest tests/test_openapi_mcp_codegen.py tests/test_generated_mcp_runtime.py tests/test_generated_mcp_client.py tests/test_kintone_stub_server.py tests/test_kintone_mcp_e2e.py -q
uv run ruff check src/services tests examples
uv run bandit -r src/services
uv run pytest -q
```

- [ ] **Step 6: Review the diff and generated artifact inventory**

Confirm only intended source, test, documentation, and generated artifact files changed. Do not stage or alter `refs/`. Confirm no API key, token, proxy password, or environment value appears in `git diff`.

- [ ] **Step 7: Commit documentation and final generated artifacts**

```bash
git add README.md examples/README.md src/services/mcp_generator.py tests/test_generated_contract.py results/azure/openapi/mcpserver
git commit -m "docs: document Kintone MCP mock verification"
```

## Task Dependency Order

```text
Task 1A
  -> Task 2A
  -> Task 2B
  -> Task 3A
  -> Task 3B
  -> Task 4
  -> Task 5
  -> Task 6
```

Task 3B can be implemented in parallel with Task 3A after Task 2A, but its commit must be complete before Task 5's E2E test. Task 4 depends on the generated server metadata and transport contract from Task 2B. Task 6 is the only task allowed to regenerate and stage the final `results/` artifacts after all source changes are complete.

## Review Gates

- After Task 1A: fallback failures are visible and no false-success artifact is reported.
- After Task 2B: the Kintone spec produces 204 tools and the generated code compiles.
- After Task 3A/3B: auth and error behavior is covered without secret leakage.
- After Task 4: a selected MCP tool can be called rather than only listed.
- After Task 5: OCR data crosses the complete mock MCP path and is read back.
- After Task 6: all tests, lint, security scan, and generated contract checks pass.

## Test Matrix

### Unit Tests

- operationIdからtool名への変換
- path/query/header/bodyの抽出
- `$ref`解決
- required/default/typeの保持
- security requirementの解決
- URL結合とpath parameter展開
- HTTP status分類
- response JSON/empty/text処理
- secret redaction

### Integration Tests

- API token付きGET/POST/PUT/DELETE
- tokenなし・不正token
- 404/409/429/500
- timeout/connection error
- streamable-httpとSSE
- clientからのtool invocation

### End-to-End Test

```text
1. Start examples/kintone_stub_server.py on 127.0.0.1:9100
2. Start generated server with API_BASE_URL=http://127.0.0.1:9100
3. Connect generated client over streamable-http
4. Invoke record creation tool with OCR mock data
5. Assert ids/revisions response
6. Fetch the created record
7. Assert OCR fields and status
8. Inspect request history without exposing token value
```

## Verification Commands

```bash
uv run pytest tests/test_openapi_mcp_codegen.py -q
uv run pytest tests/test_generated_mcp_runtime.py -q
uv run pytest tests/test_generated_mcp_client.py -q
uv run pytest tests/test_kintone_stub_server.py tests/test_kintone_mcp_e2e.py -q
uv run python -m compileall results/azure/openapi/mcpserver
uv run ruff check src/services tests examples
uv run bandit -r src/services
uv run pytest -q
```

## Definition of Done

- `server.py` is not a fallback implementation.
- Kintone OpenAPI's 204 operations produce 204 MCP tools.
- Generated tools can connect to `API_BASE_URL`.
- API token authentication works against the local Kintone mock.
- OCR mock data can be registered and the response contains record id/revision.
- A follow-up GET returns the registered data.
- HTTP, connection, timeout, and JSON errors are structured and distinguishable.
- Authentication values are absent from logs, errors, and test output.
- Generated client can invoke a selected tool.
- All existing tests and new tests pass.
- Each milestone is checkpointed with a commit.
