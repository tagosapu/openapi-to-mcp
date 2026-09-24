# OCR Transfer Connector MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** OCR標準データを、登録済みの任意のREST/HTTP JSON APIへ、認証・マッピング・冪等性・再試行・監査付きで非同期転記できるMVPを、このリポジトリに追加する。

**Architecture:** 既存のOpenAPI評価/MCP生成コードとは分離した `src/transfer/` パッケージを作る。FastAPIが標準転記APIと管理APIを提供し、SQLiteが転送状態・冪等性・監査履歴を永続化する。バックグラウンドワーカーは宣言的マッピングとREST/OpenAPIコネクタを使って転記し、送信結果が不明な場合は `reconciliation_required` へ遷移して盲目的な再送を止める。

**Scope decision:** 標準契約、コネクタ、worker、APIは独立した責務だが、どれか一つだけでは受入条件を満たす実行可能なMVPにならない。Task 1-8を一つの計画に保ち、各Taskのテスト可能な境界とコミットを分ける。外部Secret Manager、UI、複数転記先、MCP生成は別計画へ延期する。

**Tech Stack:** Python 3.11+、FastAPI、Pydantic v2、pydantic-settings、httpx、aiosqlite、jsonpointer、jsonref、openapi-spec-validator、jsonschema、PyJWT、uvicorn、prometheus-client、opentelemetry-api、cryptography、pytest、pytest-asyncio。Python環境と依存関係の操作には `uv` だけを使用する。

**Spec:** [docs/OCR_TRANSFER_CONNECTOR_REQUIREMENTS.md](../../OCR_TRANSFER_CONNECTOR_REQUIREMENTS.md)

## Global Constraints

- Python環境管理は `uv` を使用し、`pip`、`conda`、`virtualenv` は使用しない。
- `.env` は直接パースせず、`pydantic-settings` または `python-dotenv` を介して読む。秘密情報や環境変数の値をログ、監査履歴、APIレスポンスへ出力しない。
- MVPの転記対象は、1文書を1つの登録済みREST/HTTP JSON APIへ送る処理に限定する。バッチ、multipart、ファンアウトは実装しない。
- 標準APIの正規契約はOpenAPI 3.1.0とし、入力・転記先リクエスト・転記結果はJSON Schemaで検証する。
- 配送保証は at-least-once とし、転記先の成否が不明な場合に exactly-once を主張しない。
- リクエストごとの任意URLを受け付けず、管理者が登録した許可済み接続先だけへ送信する。SSRF対策としてDNS解決後のloopback、private、link-local、metadata addressを拒否する。
- マッピングは宣言的データだけを受け付け、任意のPython/JavaScript、外部HTTP、ファイルアクセスを実行しない。
- operation単位のOpenAPI `security`、`$ref`、base URL、必須ヘッダー、パラメータ型、成功/エラーSchemaをコネクタ公開前に検証する。
- 各タスクはTDDで進め、失敗テスト、最小実装、対象テスト、全テストの順で確認する。テストコードを実装に合わせて弱めない。
- 既存のOpenAPI評価/MCP生成の公開APIは変更しない。既存テストは各タスク後に `uv run pytest -q` で回帰確認する。

## File Map

### New files

- `src/transfer/__init__.py`: 転記サブシステムのパッケージ公開面。
- `src/transfer/settings.py`: `TRANSFER_` 接頭辞の設定、`.env` 読み込み、許可ホスト、SQLiteパス、JWT設定。
- `src/transfer/models.py`: OCR入力、マッピング、コネクタ、転送状態、結果のPydanticモデル。
- `src/transfer/errors.py`: Problem Detailsと内部エラー分類。
- `src/transfer/store.py`: SQLiteスキーマ、原子的な冪等性登録、転送状態、監査イベント、claim処理。
- `src/transfer/mapping.py`: JSON PointerとHTTP配置場所を扱う宣言的マッピングエンジン。
- `src/transfer/auth.py`: JWT認可、転記先資格情報参照、APIキー/Bearer/Basic/OAuth2認証適用。
- `src/transfer/openapi_contract.py`: OpenAPI検証、ローカル`$ref`解決、operation/認証/schemaの正規化。
- `src/transfer/connector.py`: コネクタProtocolと送受信モデル。
- `src/transfer/rest_connector.py`: REST/OpenAPIコネクタのリクエスト生成、送信、応答解釈、照会。
- `src/transfer/worker.py`: SQLite-backed転送ワーカー、再試行、レビュー、成否不明処理。
- `src/transfer/routes.py`: FastAPIの転送・管理・ヘルスエンドポイント。
- `src/transfer/app.py`: FastAPI app factory、lifespan、ワーカー起動、CLI entrypoint。
- `src/transfer/limits.py`: request rate/payload上限と413/429判定。
- `src/transfer/observability.py`: redaction、metrics、traces、監査補助。
- `schemas/ocr-transfer-v1.json`: OCR標準入力のJSON Schema。
- `schemas/mapping-v1.json`: マッピング定義のJSON Schema。
- `docs/api/ocr-transfer-openapi.yaml`: 標準転記APIの公開契約。
- `tests/transfer/conftest.py`: SQLite、認可コンテキスト、コネクタ、スタブAPIのfixture。
- `tests/transfer/test_contracts.py`: 標準JSON Schemaと公開OpenAPI契約のテスト。
- `tests/transfer/test_models.py`: PydanticモデルとJSON Schemaのテスト。
- `tests/transfer/test_store.py`: 永続化、冪等性、状態遷移、再起動回復のテスト。
- `tests/transfer/test_mapping.py`: マッピング、型変換、信頼度、禁止ヘッダーのテスト。
- `tests/transfer/test_openapi_contract.py`: `$ref`、base URL、operation認証、パラメータ型のテスト。
- `tests/transfer/test_rest_connector.py`: 認証、HTTPリクエスト、応答分類、reconcileのテスト。
- `tests/transfer/test_worker.py`: 成功、再試行、レビュー、結果不明、デッドレターのテスト。
- `tests/transfer/test_api.py`: FastAPIエンドポイント、認可、Problem Details、テナント分離のテスト。
- `tests/transfer/test_integration.py`: スタブ転記先へのcreate/update/upsertと事後条件の統合テスト。
- `tests/transfer/test_operational_safeguards.py`: 上限、暗号化、redaction、監査保持のテスト。
- `tests/transfer/test_generated_contract.py`: 実装routeと公開契約の差分テスト。
- `tests/transfer/fixtures/relative-server.yaml`: 相対base URL拒否fixture。
- `tests/transfer/fixtures/operation-security.yaml`: operation-level securityとローカル`$ref` fixture。
- `tests/transfer/fixtures/target_openapi.yaml`: API key認証の統合用OpenAPI fixture。
- `tests/transfer/fixtures/target_openapi_alt.yaml`: Bearer認証と別operation配置の統合用fixture。
- `tests/transfer/fixtures/invoice_mapping.json`: create/update/upsert用の宣言的mapping fixture。
- `examples/ocr/invoice-transfer.json`: 標準OCR入力のサンプル。
- `examples/ocr/invoice-transfer-alt.json`: 別OCRベンダー形式から正規化した標準入力のサンプル。
- `scripts/export_transfer_contract.py`: FastAPI/Pydantic契約のexportと差分check。

### Modified files

- `docs/OCR_TRANSFER_CONNECTOR_REQUIREMENTS.md`: 管理APIの登録操作と標準API契約の実装前提を補足する。
- `pyproject.toml`: `aiosqlite`、`jsonpointer`、`jsonref`、`PyJWT[crypto]`、`uvicorn` と `ocr-transfer-api` entrypointを追加する。
- `env.example`: `TRANSFER_DATABASE_PATH`、JWT設定、許可ホスト、タイムアウト、ワーカー設定の名前だけを追加する。資格情報の値は記載しない。
- `README.md`: MVPの起動、標準入力、コネクタ登録、状態照会、スタブAPIによるテスト手順を追加する。

---

## Task 1: 契約とスキーマを固定する

**Files:**
- Modify: `docs/OCR_TRANSFER_CONNECTOR_REQUIREMENTS.md: 7.6-7.7`（登録操作を追加）
- Create: `docs/api/ocr-transfer-openapi.yaml`
- Create: `schemas/ocr-transfer-v1.json`
- Create: `schemas/mapping-v1.json`
- Create: `tests/transfer/test_contracts.py`
- Create: `tests/transfer/conftest.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: 要件書の `ocr-transfer/v1`、`POST /v1/transfers`、状態、レビュー、reconcile、契約validate。
- Produces: 後続タスクがそのまま使うJSON Schemaの必須フィールド、`MappingDefinition`/`ConnectorDefinition`のJSON形状、標準APIのパス・status code・Problem Details契約。

- [ ] **Step 1: 管理APIの不足を要件書へ反映する**

要件書の管理APIへ次の2操作を追加する。認証情報の値はAPIで受け取らず、`credential_ref` の存在だけを検証する。

```text
POST /v1/connectors
POST /v1/mappings
```

`POST /v1/connectors` は `connector_id`、`type=rest-openapi`、絶対`base_url`、固定済み`spec`または`spec_ref`、`credential_ref`、許可済み追加ヘッダー、タイムアウトを受け付け、`connector:admin`だけを要求する。`POST /v1/mappings` は `mapping_id`、`version`、`connector_id`、`document_types`、`operations`、`deduplication_key_path`、`target_schema_ref`、`rules` を受け付け、`mapping:write`だけを要求する。connectorとmappingの一覧はそれぞれ`connector:read`、`mapping:read`を要求する。

- [ ] **Step 2: スキーマ検証の失敗テストを書く**

```python
def test_transfer_schema_requires_connector_and_mapping() -> None:
    invalid = {
        "schema_version": "ocr-transfer/v1",
        "document": {"document_id": "doc-1", "document_type": "invoice"},
        "ocr": {"fields": {}},
        "delivery": {"operation": "upsert"},
    }

    errors = list(Draft202012Validator(load_json("schemas/ocr-transfer-v1.json")).iter_errors(invalid))

    assert any(error.validator == "required" for error in errors)


def test_openapi_contract_exposes_transfer_and_admin_paths() -> None:
    paths = load_yaml("docs/api/ocr-transfer-openapi.yaml")["paths"]

    assert "/v1/transfers" in paths
    assert "/v1/transfers/{transfer_id}/reconcile" in paths
    assert "/v1/connectors/{connector_id}/validate" in paths
    assert paths["/v1/transfers"]["post"]["responses"]["202"]
```

- [ ] **Step 3: テストが実装不足で失敗することを確認する**

Run: `uv run pytest tests/transfer/test_contracts.py -q`

Expected: FAIL because the schema and OpenAPI contract files do not exist yet.

- [ ] **Step 4: 依存関係とJSON Schemaを追加する**

```bash
uv add aiosqlite jsonpointer jsonref 'PyJWT[crypto]' uvicorn cryptography
uv add --dev pytest pytest-asyncio
```

`schemas/ocr-transfer-v1.json` は `schema_version`、`document.document_id`、`document.document_type`、`document.source_system`、`ocr.fields`/`line_items`、`delivery.connector_id`、`delivery.mapping_id`、`delivery.operation`、`delivery.deduplication_key_path`、`metadata.tenant_id`、`metadata.correlation_id`を必須にする。`confidence`を`0.0..1.0`、`status`を`extracted|missing|ambiguous|invalid|manually_corrected`、`value_type`を`string|integer|number|boolean|date|datetime|currency|object|array`に制限し、date/datetime形式、currencyのISO unit、bboxの0.0..1.0範囲、`storage_ref`/`text_ref`の不透明な参照形式を検証する。`schemas/mapping-v1.json` は `target.location` を `path|query|header|body` に制限し、`body` だけにJSON Pointerを許可する。`schemas/connector-v1.json` は作らず、コネクタ登録のJSON SchemaはOpenAPI契約内の `ConnectorDefinition` として管理する。

`tests/transfer/conftest.py`には、相対パスからJSON/YAMLを読む`load_json(path)`と`load_yaml(path)`、および後続テストが共有する一時SQLite設定fixtureを定義する。

- [ ] **Step 5: OpenAPI 3.1契約を作成する**

契約には次の操作を含める。

```text
POST   /v1/transfers
GET    /v1/transfers
GET    /v1/transfers/{transfer_id}
POST   /v1/transfers/{transfer_id}/retry
POST   /v1/transfers/{transfer_id}/cancel
POST   /v1/transfers/{transfer_id}/review
POST   /v1/transfers/{transfer_id}/reconcile
POST   /v1/mappings/{mapping_id}/preview
GET    /v1/connectors
POST   /v1/connectors
POST   /v1/connectors/{connector_id}/validate
GET    /v1/mappings
POST   /v1/mappings
GET    /v1/health/live
GET    /v1/health/ready
```

`POST /v1/transfers` は `Idempotency-Key` と `X-Correlation-ID` を定義し、初回は `202`、同一キー・同一ハッシュの再送は既存受付結果と `X-Idempotent-Replay: true` を返す。エラーのContent-Typeは `application/problem+json` とする。`POST /v1/connectors` と `POST /v1/mappings` は秘密情報本体を受け取らず、`credential_ref`だけを契約に含める。

OpenAPIのsecurity scopesはroute実装と一致させ、`transfer:retry`、`transfer:cancel`、`transfer:review`、`transfer:reconcile`、`connector:admin`、`mapping:write`などの操作単位scopeを各operationへ記載する。

- [ ] **Step 6: 契約テストを実行する**

Run: `uv run pytest tests/transfer/test_contracts.py -q`

Expected: PASS。入力Schema、マッピングSchema、全エンドポイント、Problem Details、認証要求が確認できる。続けて`uv run pytest -q`を実行し、既存テストを含む全テストが成功することを確認する。

- [ ] **Step 7: コミットする**

```bash
git add docs/OCR_TRANSFER_CONNECTOR_REQUIREMENTS.md docs/api/ocr-transfer-openapi.yaml schemas/ocr-transfer-v1.json schemas/mapping-v1.json tests/transfer/test_contracts.py tests/transfer/conftest.py pyproject.toml
git commit -m "feat: define OCR transfer connector contracts"
```

## Task 2: ドメインモデルと永続ストアを実装する

**Files:**
- Create: `src/transfer/__init__.py`
- Create: `src/transfer/settings.py`
- Create: `src/transfer/models.py`
- Create: `src/transfer/errors.py`
- Create: `src/transfer/store.py`
- Modify: `tests/transfer/conftest.py`
- Create: `tests/transfer/test_models.py`
- Create: `tests/transfer/test_store.py`
- Modify: `env.example`

**Interfaces:**
- Consumes: Task 1のJSON SchemaとAPIフィールド。
- Produces: `TransferStore` Protocol、`SqliteTransferStore`、`TransferStatus`、`TransferRequest`、`TransferRecord`、`ReviewCorrection`、`MappingDefinition`、`ConnectorDefinition`、`OperationSelection`、`OutboundRequestParts`、`ProblemDetail`。後続タスクはここで定義した型名と署名を変更しない。

`errors.py`には`IdempotencyConflict`、`TenantIsolationError`、`MappingValidationError`、`PayloadLimitError`など、routeがProblem Detailsへ変換する内部例外を定義する。storeはtenant不一致を`TenantIsolationError`として返し、存在しない別tenantのIDを404相当に隠す。

- [ ] **Step 1: Pydanticモデルの失敗テストを書く**

```python
def test_confidence_and_field_status_are_bounded() -> None:
    with pytest.raises(ValidationError):
        OcrField.model_validate({
            "value": "x",
            "value_type": "string",
            "confidence": 1.1,
            "status": "extracted",
        })


def test_transfer_request_uses_deduplication_key_path() -> None:
    request = TransferRequest.model_validate(sample_transfer_request())

    assert request.delivery.deduplication_key_path == "/ocr/fields/invoice_number/value"


@pytest.mark.asyncio
async def test_store_keeps_connector_mapping_review_and_events_tenant_scoped(store):
    await store.save_connector(sample_connector_definition(), tenant_id="tenant-a")
    await store.save_mapping(sample_mapping(), tenant_id="tenant-a")
    transfer_id = await seed_transfer_for_tenant(store, tenant_id="tenant-a")

    assert await store.get_connector("tenant-b", "connector-test") is None
    assert await store.get_mapping("tenant-b", "mapping-test") is None
    with pytest.raises(TenantIsolationError):
        await store.save_review_correction("tenant-b", transfer_id, {"/ocr/fields/x/value": "y"}, "reviewer", "wrong tenant")
    with pytest.raises(TenantIsolationError):
        await store.transition("tenant-b", transfer_id, TransferStatus.ACCEPTED, TransferStatus.CANCELLED, {})
```

- [ ] **Step 2: モデルテストが実装不足で失敗することを確認する**

Run: `uv run pytest tests/transfer/test_models.py -q`

Expected: FAIL with an import error because `src/transfer/models.py` does not exist yet.

- [ ] **Step 3: モデルと設定を実装する**

`models.py` に次の公開型を定義する。

```python
class TransferStatus(str, Enum):
    ACCEPTED = "accepted"
    VALIDATING = "validating"
    WAITING_REVIEW = "waiting_review"
    QUEUED = "queued"
    DELIVERING = "delivering"
    RETRYING = "retrying"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CANCELLATION_REQUESTED = "cancellation_requested"
    SUCCEEDED = "succeeded"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TransferRequest(BaseModel):
    schema_version: Literal["ocr-transfer/v1"]
    document: OcrDocument
    ocr: OcrResult
    delivery: DeliveryRequest
    metadata: TransferMetadata
```

同じファイルに次の補助型と公開型を追加する。

```python
class MappingTarget(BaseModel):
    location: Literal["path", "query", "header", "body"]
    name: str | None = None
    pointer: str | None = None


class MappingCondition(BaseModel):
    source: str
    operator: Literal["exists", "equals", "not_equals", "in"]
    value: Any | None = None


class MappingRule(BaseModel):
    rule_id: str
    source: str
    target: MappingTarget
    required: bool = False
    on_missing: Literal["error", "omit", "null"] = "error"
    transforms: list[str] = Field(default_factory=list)
    default: Any | None = None
    enum_map: dict[str, str | int | float | bool] = Field(default_factory=dict)
    condition: MappingCondition | None = None


class MappingIssue(BaseModel):
    source_path: str
    target_path: str
    code: str
    message: str
    retryable: bool


class MappingPreview(BaseModel):
    method: str
    path: str
    path_params: dict[str, str]
    query_params: dict[str, str]
    headers: dict[str, str]
    json_body: dict[str, Any] | list[Any] | None
    issues: list[MappingIssue] = Field(default_factory=list)
    requires_review: bool


class AdditionalHeader(BaseModel):
    name: str
    value_ref: str


class ConnectorPolicy(BaseModel):
    connect_timeout_seconds: float = 5
    read_timeout_seconds: float = 30
    total_timeout_seconds: float = 60
    max_response_bytes: int = 10 * 1024 * 1024
    max_redirects: int = 0


class PostconditionDefinition(BaseModel):
    reconcile_operation_id: str
    result_id_pointer: str
    not_found_statuses: set[int] = Field(default_factory=lambda: {404})
    registered_statuses: set[int] = Field(default_factory=lambda: {200, 201})


class OperationBinding(BaseModel):
    operation_id: str
    idempotency_header: str | None = None
    postcondition: PostconditionDefinition | None = None
    lookup_operation_id: str | None = None
    lookup_parameter_name: str | None = None
    lookup_parameter_location: Literal["path", "query"] | None = None
    conflict_policy: Literal["reconcile", "fail"] = "reconcile"


class OperationSelection(BaseModel):
    name: Literal["create", "update", "upsert"]
    operation_id: str
    method: str
    path: str


class MappingDefinition(BaseModel):
    mapping_id: str
    version: int
    status: Literal["draft", "published", "retired"]
    connector_id: str
    document_types: list[str]
    operations: list[Literal["create", "update", "upsert"]]
    deduplication_key_path: str
    target_schema_ref: str
    rules: list[MappingRule]


class ConnectorDefinition(BaseModel):
    connector_id: str
    version: int
    type: Literal["rest-openapi"]
    base_url: AnyHttpUrl
    spec_ref: str
    spec: dict[str, Any]
    credential_ref: str
    additional_headers: list[AdditionalHeader] = Field(default_factory=list)
    operation_bindings: dict[str, OperationBinding]
    policy: ConnectorPolicy


class OutboundRequestParts(BaseModel):
    operation_id: str
    method: str
    path: str
    path_params: dict[str, str]
    query_params: dict[str, str]
    headers: dict[str, str]
    json_body: dict[str, Any] | list[Any] | None
    idempotency_key: str


class TransferResult(BaseModel):
    target_resource_id: str | None
    target_request_id: str | None
    postcondition_verified: bool


class ProblemDetail(BaseModel):
    type: str
    title: str
    status: int
    code: str
    detail: str
    instance: str | None = None
    correlation_id: str | None = None
    retryable: bool
    errors: list[dict[str, str]] = Field(default_factory=list)


class ReviewCorrection(BaseModel):
    correction_ref: str
    values: dict[str, Any]
    actor: str
    reason: str


class TransferRecord(BaseModel):
    transfer_id: str
    tenant_id: str
    status: TransferStatus
    request: TransferRequest
    connector_version: int
    mapping_version: int
    idempotency_key: str
    attempt: int
    next_retry_at: datetime | None
    result: TransferResult | None = None
    error: ProblemDetail | None = None


class TransferFilters(BaseModel):
    status: TransferStatus | None = None
    connector_id: str | None = None
    document_id: str | None = None
    correlation_id: str | None = None
    cursor: str | None = None
    limit: int = 50


class CreateTransferResult(BaseModel):
    record: TransferRecord
    idempotent_replay: bool


class ClaimedTransfer(BaseModel):
    record: TransferRecord
    phase: Literal["validate", "deliver"]
```

`MappingRule` は `source`、`target`、`required`、`on_missing`、`transforms` を持ち、`target.location` を `path|query|header|body`、`target.pointer`をbody限定とする。`ConnectorDefinition.operation_bindings` は業務操作名をOpenAPI `operation_id`へ結び付け、`postcondition`、`lookup_operation_id`、検索キーのパラメータ位置を保持する。Pydanticのmutable defaultは避け、リストは `Field(default_factory=list)` で定義する。

`settings.py` は `BaseSettings` と `SettingsConfigDict(env_prefix="TRANSFER_", env_file=".env", extra="ignore")` を使う。設定値には `database_path`、`jwt_issuer`、`jwt_audience`、`jwks_url`、`allowed_hosts`、`worker_poll_seconds`、`worker_enabled=True`、`max_attempts`、`max_payload_bytes`、`idempotency_retention_hours=24`、`payload_retention_days=30`、`audit_retention_days=90`、`requests_per_minute`、`burst`、`credentials_json`、`data_encryption_key_ref` を持たせる。`credentials_json`と`data_encryption_key_ref`はSecretStrまたは参照文字列として扱い、値をログ出力しない。

`tests/transfer/conftest.py`には、`sample_transfer_request(line_items: int | None = None)`、`sample_mapping()`、`sample_mapping_with_target_header(name)`、`sample_operation_selection(name)`、`sample_connector_definition()`、`seed_transfer_for_tenant(store, tenant_id)`、`settings_factory(**overrides)`、`test_settings` fixture、`fake_clock` fixtureを定義し、テストごとに独自の認証情報や外部URLを埋め込まない。`test_settings`は暗号化テスト鍵を含む設定を返し、`settings_factory`は指定overrideだけを適用する。

- [ ] **Step 4: 永続化テーブルを実装する**

`store.py` は起動時に次のSQLiteテーブルを作成する。

```text
connectors(connector_id, tenant_id, version, config_json, spec_json, spec_hash, status, created_at, updated_at)
mappings(mapping_id, tenant_id, version, definition_json, status, created_at, updated_at)
transfers(transfer_id, tenant_id, idempotency_key, idempotency_expires_at, request_hash, request_json, status, attempt, next_retry_at, result_json, error_json, created_at, updated_at)
transfer_events(event_id, tenant_id, transfer_id, from_status, to_status, event_type, detail_json, correction_ref, event_hash, previous_event_hash, created_at)
review_corrections(correction_ref, tenant_id, transfer_id, correction_json, created_at)
audit_chain_checkpoints(checkpoint_id, tenant_id, cutoff_at, deleted_through_hash, checkpoint_hash, created_at)
```

`transfer_events`と`review_corrections`は`UNIQUE(tenant_id, event_id)`または`UNIQUE(tenant_id, correction_ref)`と`FOREIGN KEY(tenant_id, transfer_id)`で同一tenantのtransferだけを参照できるようにする。イベント・訂正追加時はtransferからtenantを再取得し、引数のtenantと不一致なら拒否する。tenant内のchain順序は`created_at, event_id`の昇順で固定し、checkpoint対象も同じtenantかつ`created_at < cutoff_at`のイベントに限定する。

`transfers` に `UNIQUE(tenant_id, transfer_id)` と `UNIQUE(tenant_id, idempotency_key)` を付ける。`create_or_get_transfer(tenant_id, idempotency_key, request, correlation_id, connector_version, mapping_version) -> CreateTransferResult` は保持期限内の同一キー・同一ハッシュなら元レコードと元versionを返し、同一キー・異なるハッシュなら `IdempotencyConflict` を返す。期限を超えたキーはpurge後に再利用できる。`request_hash` はPydanticのJSONをキー順でシリアライズしたSHA-256とする。connectorとmappingはtenant/versionで取得し、転送レコードに使用バージョンを固定保存する。connector登録時は正規化OpenAPIのSHA-256を`spec_hash`へ保存する。

- [ ] **Step 5: 状態遷移とclaimを実装する**

```python
ALLOWED_TRANSITIONS = {
    TransferStatus.ACCEPTED: {
        TransferStatus.VALIDATING,
        TransferStatus.CANCELLED,
    },
    TransferStatus.VALIDATING: {
        TransferStatus.ACCEPTED,
        TransferStatus.QUEUED,
        TransferStatus.WAITING_REVIEW,
        TransferStatus.FAILED,
        TransferStatus.CANCELLED,
    },
    TransferStatus.WAITING_REVIEW: {
        TransferStatus.QUEUED,
        TransferStatus.FAILED,
        TransferStatus.CANCELLED,
    },
    TransferStatus.QUEUED: {
        TransferStatus.DELIVERING,
        TransferStatus.CANCELLED,
    },
    TransferStatus.DELIVERING: {
        TransferStatus.SUCCEEDED,
        TransferStatus.RETRYING,
        TransferStatus.FAILED,
        TransferStatus.RECONCILIATION_REQUIRED,
        TransferStatus.CANCELLATION_REQUESTED,
    },
    TransferStatus.RETRYING: {
        TransferStatus.DELIVERING,
        TransferStatus.FAILED,
        TransferStatus.RECONCILIATION_REQUIRED,
        TransferStatus.CANCELLED,
    },
    TransferStatus.RECONCILIATION_REQUIRED: {
        TransferStatus.SUCCEEDED,
        TransferStatus.FAILED,
        TransferStatus.DELIVERING,
    },
    TransferStatus.CANCELLATION_REQUESTED: {
        TransferStatus.SUCCEEDED,
        TransferStatus.FAILED,
        TransferStatus.RECONCILIATION_REQUIRED,
        TransferStatus.CANCELLED,
    },
}
```

`claim_due_transfer() -> ClaimedTransfer | None` はSQLite transaction内で、`accepted`を`validating`へ、`queued`または期限到来した`retrying`を`delivering`へ、それぞれ1件だけ変更する。戻り値の `phase` は `validate` または `deliver` とし、同時workerが同じジョブを取れないようにする。起動時に残った`delivering`は`reconciliation_required`に変更し、再起動で新規作成を二重送信しない。`cancel_transfer()` は未送信の `accepted|validating|queued|waiting_review|retrying` を `cancelled` にし、`delivering` は `cancellation_requested` にする。

`TransferStore` Protocolは少なくとも次の署名を公開する。

```python
class TransferStore(Protocol):
    async def initialize(self) -> None: ...
    async def close(self) -> None: ...
    async def create_or_get_transfer(self, tenant_id: str, idempotency_key: str, request: TransferRequest, correlation_id: str, connector_version: int, mapping_version: int) -> CreateTransferResult: ...
    async def get_transfer(self, tenant_id: str, transfer_id: str) -> TransferRecord | None: ...
    async def list_transfers(self, tenant_id: str, filters: TransferFilters) -> list[TransferRecord]: ...
    async def claim_due_transfer(self, now: datetime) -> ClaimedTransfer | None: ...
    async def transition(self, tenant_id: str, transfer_id: str, expected: TransferStatus, target: TransferStatus, detail: dict[str, Any]) -> TransferRecord: ...
    async def recover_inflight(self) -> int: ...
    async def cancel_transfer(self, tenant_id: str, transfer_id: str, detail: dict[str, Any]) -> TransferRecord: ...
    async def save_connector(self, connector: ConnectorDefinition, tenant_id: str) -> None: ...
    async def get_connector(self, tenant_id: str, connector_id: str, version: int | None = None) -> ConnectorDefinition | None: ...
    async def save_mapping(self, mapping: MappingDefinition, tenant_id: str) -> None: ...
    async def get_mapping(self, tenant_id: str, mapping_id: str, version: int | None = None) -> MappingDefinition | None: ...
    async def list_connectors(self, tenant_id: str) -> list[ConnectorDefinition]: ...
    async def list_mappings(self, tenant_id: str) -> list[MappingDefinition]: ...
    async def save_review_correction(self, tenant_id: str, transfer_id: str, values: dict[str, Any], actor: str, reason: str) -> ReviewCorrection: ...
    async def get_latest_review_correction(self, tenant_id: str, transfer_id: str) -> ReviewCorrection | None: ...
    async def purge_expired_payloads(self, before: datetime) -> int: ...
    async def purge_expired_audit_events(self, before: datetime, tenant_id: str | None = None) -> int: ...
    async def get_latest_audit_checkpoint(self, tenant_id: str | None = None) -> dict[str, Any] | None: ...
```

`SqliteTransferStore` がこのProtocolを実装し、Task 6のapp factoryはこの具体クラスを生成してProtocolとして注入する。

`recover_inflight()`は外部routeへ公開しない起動専用操作で、tenantごとの行を独立して回復する。外部から呼ぶ状態更新は常に`transition(tenant_id, ...)`を通す。

```python
class PayloadProtector(Protocol):
    def encrypt(self, plaintext: bytes) -> bytes: ...
    def decrypt(self, ciphertext: bytes) -> bytes: ...


class SqliteTransferStore:
    def __init__(self, path: Path, protector: PayloadProtector | None = None) -> None: ...
```

`protector`が未指定のTask 2テストでは暗号化なしのfixtureを使い、Task 8で本番設定を必須化して本文保存を暗号化する。reviewの訂正値は`review_corrections.correction_json`へ保存し、`transfer_events.detail_json`にはactor、reason、`correction_ref`だけを残す。

- [ ] **Step 6: ストアテストを実行する**

Run: `uv run pytest tests/transfer/test_models.py tests/transfer/test_store.py -q`

Expected: PASS。異なるテナントの同一キーを許可し、同一テナントの異なるpayloadを409相当で拒否し、connector/mapping version固定、cancel可能状態、`validating`/`delivering`の再起動回復、状態遷移・イベント・暗号化対象フィールドを検証できる。続けて`uv run pytest -q`を実行する。

- [ ] **Step 7: コミットする**

```bash
git add src/transfer/__init__.py src/transfer/settings.py src/transfer/models.py src/transfer/errors.py src/transfer/store.py tests/transfer/conftest.py tests/transfer/test_models.py tests/transfer/test_store.py env.example
git commit -m "feat: add durable transfer domain and store"
```

## Task 3: 宣言的マッピングとプレビュー変換を実装する

**Files:**
- Create: `src/transfer/mapping.py`
- Create: `tests/transfer/test_mapping.py`

**Interfaces:**
- Consumes: `TransferRequest`、`MappingDefinition`、Task 2のJSON Schema。
- Produces: `MappingEngine.apply_corrections(payload, correction) -> TransferRequest`、`MappingEngine.preview(payload, mapping, operation) -> MappingPreview`、`MappingEngine.apply(payload, mapping, operation) -> OutboundRequestParts`。`operation`はTask 2の`OperationSelection`で、`delivery.operation`、`MappingDefinition.operations`、`ConnectorDefinition.operation_bindings`を解決済みの値として持つ。`apply_corrections()`はJSON Pointerごとの訂正値をdeep copyへ適用するが、保存済みの元`TransferRequest`は変更しない。`OutboundRequestParts`の実体はTask 2のモデルで固定し、Task 4のRESTコネクタとTask 6のpreview endpointが使用する。

- [ ] **Step 1: HTTP配置場所の失敗テストを書く**

```python
def test_mapping_builds_path_query_header_and_body() -> None:
    preview = MappingEngine().preview(
        sample_transfer_request(), sample_mapping(), sample_operation_selection("upsert")
    )

    assert preview.path_params == {"external_id": "INV-0001"}
    assert preview.query_params == {"dry_run": "false"}
    assert preview.headers == {"X-Document-Type": "invoice"}
    assert preview.json_body["amount"] == 12000


def test_mapping_rejects_auth_and_control_headers() -> None:
    mapping = sample_mapping_with_target_header("Authorization")

    with pytest.raises(MappingValidationError, match="forbidden header"):
        MappingEngine().preview(
            sample_transfer_request(), mapping, sample_operation_selection("upsert")
        )
```

- [ ] **Step 2: マッピングテストが実装不足で失敗することを確認する**

Run: `uv run pytest tests/transfer/test_mapping.py -q`

Expected: FAIL with an import error because `src/transfer/mapping.py` does not exist yet.

- [ ] **Step 3: JSON Pointerと許可変換を実装する**

`MappingEngine` は次の署名を公開し、`jsonpointer.resolve_pointer()` で入力値を取得する。

```python
class MappingEngine:
    def apply_corrections(self, payload: TransferRequest, correction: ReviewCorrection | None) -> TransferRequest: ...
    def preview(self, payload: TransferRequest, mapping: MappingDefinition, operation: OperationSelection) -> MappingPreview: ...
    def apply(self, payload: TransferRequest, mapping: MappingDefinition, operation: OperationSelection) -> OutboundRequestParts: ...
```

`operation.name`が`mapping.operations`に含まれない場合は`MappingValidationError`を返す。選択済みoperationの`method`、`path`、`operation_id`をpreviewと`OutboundRequestParts`へコピーし、engine内で業務操作やOpenAPI operationを推測しない。`body`だけに`pointer`を書き込み、`path`、`query`、`header` は `name`へ書き込む。`condition`がfalseならそのruleを出力せず、値が欠損していて`default`があればdefaultを使い、`enum_map`があれば変換してから型変換する。許可する変換は `trim`、`lower`、`upper`、`to_string`、`to_integer`、`to_number`、`to_date`、`to_datetime`、`currency_amount`、`concat` とする。入力はPydanticの`model_dump(mode="json")`でJSONへ正規化してから解決する。

`on_missing` は `error`、`omit`、`null` のいずれかとし、`required=true` で値が欠ける場合は `MappingValidationError` を返す。`Host`、`Content-Length`、`Authorization`、`Cookie`、`Proxy-*` は常に拒否する。

- [ ] **Step 4: 信頼度とレビュー理由を実装する**

必須フィールドの `confidence < 0.90`、任意フィールドの `confidence < 0.70`、または `status in {missing, ambiguous, invalid}` のとき、マッピング結果へ次の形式のissueを追加する。

```python
MappingIssue(
    source_path="/ocr/fields/invoice_number",
    target_path="/body/external_id",
    code="LOW_CONFIDENCE",
    message="required field confidence is below threshold",
    retryable=False,
)
```

issueがある場合は`preview()`では結果を返せるが、workerは転記先を呼ばず`waiting_review`へ遷移する。

- [ ] **Step 5: プレビューと適用結果を検証する**

`MappingPreview`は、平文の認証値を含まない`method`、`path`、`path_params`、`query_params`、安全なマッピング値を含む`headers`、`json_body`、`issues`、`requires_review`を返す。認証・Cookie・proxy制御ヘッダーはengineの禁止リストで拒否し、connectorが後から付与するsecret headerはpreviewに含めない。`apply()`は同じルールから `OutboundRequestParts` を作り、`payload.delivery.deduplication_key_path`と`mapping.deduplication_key_path`が一致することを登録時・実行時に検証する。pointerの解決値はnull、空文字、空配列、空オブジェクト、bool以外の非スカラーなら`DEDUPLICATION_KEY_INVALID`として拒否する。値は型を保持したcanonical JSON（キー順、固定separator）へ正規化し、`document.document_id`と組み合わせた`{"document_id": ..., "deduplication_value": ...}`のSHA-256を`OutboundRequestParts.idempotency_key`へ設定する。raw値をHTTPヘッダー、ログ、監査eventへ出力しない。

サンプルテストには、`default`、`enum_map`、`condition`、`concat`、明細配列の各ケースを含め、条件falseのruleがJSON bodyに残らないことと、同一targetへの複数ruleを拒否することを確認する。

- [ ] **Step 6: マッピングテストを実行する**

Run: `uv run pytest tests/transfer/test_mapping.py -q`

Expected: PASS。明細配列、型変換、欠損、低信頼度、禁止ヘッダー、未知のsource pointer、重複target、空の必須値を検証できる。転記先schema違反はTask 4の`validate_payload()`で検証する。続けて`uv run pytest -q`を実行する。

- [ ] **Step 7: コミットする**

```bash
git add src/transfer/mapping.py tests/transfer/test_mapping.py
git commit -m "feat: add declarative OCR mapping engine"
```

## Task 4: OpenAPI契約プリフライトとRESTコネクタを実装する

**Files:**
- Create: `src/transfer/auth.py`
- Create: `src/transfer/openapi_contract.py`
- Create: `src/transfer/connector.py`
- Create: `src/transfer/rest_connector.py`
- Modify: `src/transfer/store.py`
- Create: `tests/transfer/test_openapi_contract.py`
- Create: `tests/transfer/test_rest_connector.py`
- Create: `tests/transfer/fixtures/relative-server.yaml`
- Create: `tests/transfer/fixtures/operation-security.yaml`

**Interfaces:**
- Consumes: Task 1のコネクタ/マッピング契約、Task 3の`OutboundRequestParts`。
- Produces: `Connector` Protocol、`RestOpenApiConnector`、`ConnectorRegistry`、`ContractPreflightResult`、`CredentialResolver`、`ValidationResult`、`OutboundRequest`、`OutboundOutcome`、`ErrorClassification`、`ReconciliationContext`、`ReconciliationResult`。`TransferResult`と`ProblemDetail`はTask 2のモデルを再利用する。Task 5のworkerとTask 6の管理APIがこれらの型名・署名を使用する。

- [ ] **Step 1: 契約プリフライトの失敗テストを書く**

```python
def test_preflight_rejects_relative_base_url() -> None:
    spec = load_fixture("relative-server.yaml")

    result = ContractPreflight().run(spec)

    assert result.valid is False
    assert "BASE_URL_NOT_ABSOLUTE" in {issue.code for issue in result.issues}


def test_preflight_requires_operation_level_security_and_resolves_refs() -> None:
    result = ContractPreflight().run(load_fixture("operation-security.yaml"))

    assert result.valid is False
    assert result.operations["createInvoice"].request_schema["type"] == "object"
    assert "SECURITY_UNDEFINED" in {issue.code for issue in result.issues}
```

`tests/transfer/test_openapi_contract.py`は`load_fixture(name)`で`tests/transfer/fixtures/{name}`を読み、`ContractPreflight`へ渡す。`openapi_contract.py`は次の入口を公開する。

```python
class ContractPreflight:
    def run(self, spec: dict[str, Any]) -> ContractPreflightResult: ...
```

- [ ] **Step 2: コネクタテストが実装不足で失敗することを確認する**

Run: `uv run pytest tests/transfer/test_openapi_contract.py tests/transfer/test_rest_connector.py -q`

Expected: FAIL with an import error because `src/transfer/openapi_contract.py` and `src/transfer/rest_connector.py` do not exist yet.

- [ ] **Step 3: OpenAPIを正規化する**

`openapi_contract.py` はOpenAPI 3.0/3.1を受け取り、`openapi-spec-validator`で構文を検証する。`jsonref`はローカルdocument内の`$ref`だけを解決し、`http://`、`https://`、file pathの外部参照を拒否する。次を正規化する。

```python
class NormalizedOperation(BaseModel):
    operation_id: str
    method: str
    path: str
    parameters: list[ParameterDefinition]
    request_schema: dict[str, Any] | None
    success_schema: dict[str, Any] | None
    error_statuses: list[int]
    security_options: list[SecurityOption]
    required_headers: list[str]


class ParameterDefinition(BaseModel):
    name: str
    location: Literal["path", "query", "header", "cookie"]
    required: bool
    schema: dict[str, Any]


class SecurityOption(BaseModel):
    schemes: dict[str, list[str]]


class ContractIssue(BaseModel):
    code: str
    location: str
    message: str
    severity: Literal["error", "warning"]


class ContractPreflightResult(BaseModel):
    valid: bool
    base_url: AnyHttpUrl | None
    operations: dict[str, NormalizedOperation]
    issues: list[ContractIssue] = Field(default_factory=list)
    spec_hash: str

    def resolve_target_schema(self, schema_ref: str, operation_id: str) -> dict[str, Any]: ...
```

`operationId`がないoperationは、`{method}_{pathの英数字}` に変換し、同名が2つあれば公開を拒否する。`servers.url`、パスパラメータ、`security`、必須ヘッダー、成功レスポンス、代表的な4xx/5xxをoperation単位で検査する。`target_schema_ref`はsnapshot内の`components.schemas`または対象operationのrequest schemaを指す登録済み参照だけを許可し、解決できない参照はconnectorを公開しない。`ContractPreflightResult.resolve_target_schema()`はこの解決を行い、mapping登録時に呼び出す。

`PostconditionDefinition.result_id_pointer`はsuccess response schemaで解決でき、最終値が空でないstringになることを登録時に検証する。`parse_response()`も同じJSON Pointerを実体レスポンスへ適用し、missing、null、空文字、string以外なら`RESPONSE_ID_INVALID`として恒久エラーにする。`reconcile()`へ渡す`target_resource_id`はこの検証済みstringを使い、path/query lookupへはOpenAPI parameter schemaに従ってエンコードする。

- [ ] **Step 4: 資格情報と認証適用を実装する**

`auth.py`は次の型を実装する。

```python
class SecretBundle(BaseModel):
    values: dict[str, SecretStr]


class CredentialResolver(Protocol):
    async def resolve(self, credential_ref: str) -> SecretBundle: ...


class EnvironmentCredentialResolver:
    def __init__(self, settings: Settings) -> None: ...
    async def resolve(self, credential_ref: str) -> SecretBundle: ...


class Principal(BaseModel):
    tenant_id: str
    subject: str
    scopes: frozenset[str]


class JwtAuthorizer:
    async def authorize(self, authorization_header: str) -> Principal: ...
```

MVPの転記先secret実装は`EnvironmentCredentialResolver`とし、`TRANSFER_CREDENTIALS_JSON`を`pydantic-settings`で読み込み、`credential_ref`で引く。値はログとPydanticの`repr`から除外する。認証方式はAPI key header、Bearer、Basic、OAuth2 Client Credentialsを実装し、OAuth tokenは有効期限のあるメモリキャッシュを使う。送信先が401を返し、期限切れと判定できる場合だけcacheを破棄してtokenを1回更新し、同じリクエストを1回だけ再送する。2回目の401は恒久エラーとして`failed`にする。query API keyは仕様で明示された場合だけ許可し、ログとURLに値を出さない。`JwtAuthorizer`は標準APIのJWTを検証し、Task 6のroute dependencyから呼び出す。

Task 2の`PayloadProtector` Protocolを使い、`store.py`へ`FernetPayloadProtector(SecretStr)`と次のfactoryを追加する。`create_payload_protector()`は`settings.data_encryption_key_ref`をresolverで解決し、Fernet key形式を検証して返す。参照未設定・未解決・不正形式は`RuntimeError`とし、暗号化なしの本番storeを生成しない。

```python
class FernetPayloadProtector(PayloadProtector):
    def __init__(self, key: SecretStr) -> None: ...


async def create_payload_protector(settings: Settings, resolver: CredentialResolver) -> PayloadProtector: ...
```

- [ ] **Step 5: RESTリクエストと応答を実装する**

`connector.py` に次のProtocolを定義する。

```python
class Connector(Protocol):
    async def validate_config(self) -> ContractPreflightResult: ...
    async def resolve_operation(self, operation_name: str) -> OperationSelection: ...
    async def validate_payload(self, payload: TransferRequest, mapping: MappingDefinition, operation: OperationSelection) -> ValidationResult: ...
    async def build_request(self, parts: OutboundRequestParts) -> OutboundRequest: ...
    async def send(self, request: OutboundRequest) -> OutboundOutcome: ...
    def parse_response(self, outcome: OutboundOutcome) -> TransferResult: ...
    def classify_error(self, outcome: OutboundOutcome | Exception) -> ErrorClassification: ...
    async def reconcile(self, context: ReconciliationContext) -> ReconciliationResult: ...
```

`connector.py`に次のデータ型も定義する。

```python
class OutboundRequest(BaseModel):
    method: str
    url: AnyHttpUrl
    headers: dict[str, str]
    json_body: dict[str, Any] | list[Any] | None


class OutboundOutcome(BaseModel):
    delivery_state: Literal["not_sent", "received", "unknown"]
    status_code: int | None
    headers: dict[str, str]
    body: dict[str, Any] | list[Any] | None
    request_id: str | None
    elapsed_ms: int


class ValidationResult(BaseModel):
    valid: bool
    issues: list[MappingIssue] = Field(default_factory=list)


class ErrorClassification(BaseModel):
    code: str
    retryable: bool
    delivery_state: Literal["not_sent", "received", "unknown"]
    retry_after_seconds: int | None = None


class ReconciliationContext(BaseModel):
    transfer_id: str
    idempotency_key: str
    deduplication_value: str
    operation_id: str
    mode: Literal["postcondition", "unknown"]
    target_resource_id: str | None = None


class ReconciliationResult(BaseModel):
    state: Literal["registered", "not_registered", "unknown"]
    target_resource_id: str | None = None
```

`RestOpenApiConnector` は `__init__(definition, mapping_engine, credential_resolver, http_client)` を受け取る。`resolve_operation(operation_name)`は`ConnectorDefinition.operation_bindings[operation_name]`をlookupし、`ContractPreflightResult.operations[binding.operation_id]`のmethod/pathと結合した`OperationSelection`を返す。未登録の業務操作、未解決operation_id、mappingで許可されていないoperationは公開・実行時に拒否する。`validate_payload()`はmappingと選択済みoperationを適用し、正規化operationのrequest schemaへ`jsonschema.Draft202012Validator`を適用する。`build_request(parts)`はasyncでcredentialをresolveし、`parts.operation_id`に対応するoperation binding、認証、固定追加ヘッダーを合成してから`OutboundRequest`を返す。`Authorization`などのマッピング値は上書きできないようにする。`ConnectorRegistry`は次のconstructorとメソッドを提供する。

```python
class ConnectorRegistry:
    def __init__(self, store: TransferStore, credential_resolver: CredentialResolver, settings: Settings) -> None: ...
    async def register(self, tenant_id: str, definition: ConnectorDefinition) -> None: ...
    async def get(self, tenant_id: str, connector_id: str, version: int | None = None) -> Connector: ...
    async def validate(self, tenant_id: str, connector_id: str) -> ContractPreflightResult: ...
    async def resolve_operation(self, tenant_id: str, connector_id: str, version: int, operation_name: str) -> OperationSelection: ...
```

`register()`は`ContractPreflightResult.spec_hash`とconnector versionを保存し、operationId、request/response schema、security方式、追加必須ヘッダーの差分をversion変更として扱う。MVPでは登録時に保存したspec snapshotだけを使用し、実行中に外部URLからspecを再取得しない。

`register()`は、公開する`create`/`upsert` bindingに`postcondition`とlookup operation（または同等の外部ID照会）がない場合、`RECONCILIATION_NOT_CONFIGURED`で拒否する。`update`も対象IDを特定できるlookupまたはpath parameterを必須とし、成否不明のまま盲目的に再送できるbindingを保存しない。

`RestOpenApiConnector` は`httpx.AsyncClient(follow_redirects=False)`を使用する。タイムアウト、最大レスポンスサイズ、TLS検証、許可ホストを設定から受け取り、登録時と各送信直前にDNSを再解決してloopback/private/link-local/metadata addressを拒否する。接続ごとの解決結果を検証し、DNS rebindingで登録時と異なる禁止アドレスへ変化した場合も送信しない。MVPではredirectを許可せず、将来許可する場合もLocationのhostを同じ検証器で再検証する。レスポンスは2xx、許可済み3xx、4xx、5xxに分類し、bodyは設定した上限までだけ読み込む。

- [ ] **Step 6: 結果不明とreconcileを実装する**

`OutboundOutcome.delivery_state` は `not_sent`、`received`、`unknown` の3値とする。送信後の接続断、全体timeout、応答形式不明は`unknown`であり、workerは自動retryではなく`reconciliation_required`へ遷移する。コネクタには登録済みの照会operationを設定し、`Idempotency-Key`、deduplication key、外部IDのいずれかで照会する。未登録と確認できた場合だけ再送を許可する。

`reconcile(context)`は`context.target_resource_id`があればそのIDでlookupし、未指定の場合だけ`context.deduplication_value`とbindingのlookup parameterで照会する。`deduplication_value`はTask 3で検証済みの非null scalarをcanonical JSON文字列表現にした値であり、path/query parameter schemaへ適用してから送る。`deduplication_value`とtarget IDは内部照会にだけ使い、監査detail・ログ・metricsへ出力しない。

- [ ] **Step 7: コネクタテストを実行する**

Run: `uv run pytest tests/transfer/test_openapi_contract.py tests/transfer/test_rest_connector.py -q`

Expected: PASS。`$ref`、base URL、operation認証、API key/Bearer/Basic/OAuth2、追加ヘッダー、パス/クエリ/ボディ、target request schema、登録時と送信直前のSSRF、DNS rebinding、429/5xx、送信後timeout、reconcile、ConnectorRegistryのtenant isolationを検証できる。続けて`uv run pytest -q`を実行する。

- [ ] **Step 8: コミットする**

```bash
git add src/transfer/auth.py src/transfer/openapi_contract.py src/transfer/connector.py src/transfer/rest_connector.py src/transfer/store.py tests/transfer/fixtures tests/transfer/test_openapi_contract.py tests/transfer/test_rest_connector.py
git commit -m "feat: add validated REST connector"
```

## Task 5: 永続ワーカーとエラー処理を実装する

**Files:**
- Create: `src/transfer/worker.py`
- Create: `tests/transfer/test_worker.py`

**Interfaces:**
- Consumes: Task 2の`TransferStore`、Task 3の`MappingEngine`、Task 4の`Connector`。
- Produces: `RetryPolicy`、`TransferWorker.run_once()`、`TransferWorker.start()`、`TransferWorker.stop()`、`TransferWorker.recover_inflight()`、`TransferWorker.retry_transfer()`、`TransferWorker.cancel_transfer()`、`TransferWorker.review_transfer()`、`TransferWorker.reconcile_transfer()`。Task 6のFastAPI lifespanとretry/cancel/review/reconcile routeがこれらを使用する。

- [ ] **Step 1: 状態別の失敗テストを書く**

```python
@pytest.mark.asyncio
async def test_worker_moves_low_confidence_payload_to_review_without_http_call():
    connector = FakeConnector()
    transfer_id = await seed_transfer_with_low_confidence_field()

    await worker.run_once()

    record = await store.get_transfer(transfer_id)
    assert record.status == TransferStatus.WAITING_REVIEW
    assert connector.send_calls == 0


@pytest.mark.asyncio
async def test_worker_marks_send_timeout_as_reconciliation_required():
    connector = FakeConnector(outcome=OutboundOutcome(
        delivery_state="unknown",
        status_code=None,
        headers={},
        body=None,
        request_id=None,
        elapsed_ms=100,
    ))
    transfer_id = await seed_queued_transfer()

    await worker.run_once()

    assert (await store.get_transfer(transfer_id)).status == TransferStatus.RECONCILIATION_REQUIRED
```

- [ ] **Step 2: workerテストが実装不足で失敗することを確認する**

Run: `uv run pytest tests/transfer/test_worker.py -q`

Expected: FAIL with an import error because `src/transfer/worker.py` does not exist yet.

- [ ] **Step 3: エラー分類とretry policyを実装する**

次の既定値を`RetryPolicy`に固定する。

```python
RetryPolicy(
    max_attempts=3,
    initial_delay_seconds=1,
    max_delay_seconds=300,
    jitter_ratio=0.2,
    retry_statuses={408, 425, 429, 500, 502, 503, 504},
)
```

`Retry-After`を上限内で優先する。送信前の明確なDNS/接続失敗だけをretry対象にし、送信後の成否不明は`reconciliation_required`にする。400、401、403、404、422、資格情報未設定、mapping validationは自動retryしない。

`tests/transfer/test_worker.py`には、Task 4の`Connector` Protocolを実装する`FakeConnector`と、`seed_transfer_with_low_confidence_field()`、`seed_queued_transfer()`、`seed_retrying_transfer()`のhelperを定義する。FakeConnectorは`send_calls`、`outcome`、`reconcile_result`を公開し、HTTP呼び出しなしで各状態を再現する。

- [ ] **Step 4: workerのclaimと状態更新を実装する**

`worker.py`に次の公開型とconstructorを実装する。

```python
class RetryPolicy(BaseModel):
    max_attempts: int = 3
    initial_delay_seconds: float = 1
    max_delay_seconds: float = 300
    jitter_ratio: float = 0.2
    retry_statuses: set[int] = Field(default_factory=lambda: {408, 425, 429, 500, 502, 503, 504})

    @classmethod
    def from_settings(cls, settings: Settings) -> "RetryPolicy": ...


class TransferWorker:
    def __init__(
        self,
        store: TransferStore,
        registry: ConnectorRegistry,
        mapping_engine: MappingEngine,
        retry_policy: RetryPolicy,
    ) -> None: ...

    async def run_once(self) -> bool: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def recover_inflight(self) -> None: ...
    async def retry_transfer(self, tenant_id: str, transfer_id: str) -> TransferRecord: ...
    async def cancel_transfer(self, tenant_id: str, transfer_id: str) -> TransferRecord: ...
    async def review_transfer(self, tenant_id: str, transfer_id: str, decision: Literal["approve", "correct", "reject"], correction: dict[str, Any] | None, reason: str | None) -> TransferRecord: ...
    async def reconcile_transfer(self, tenant_id: str, transfer_id: str) -> TransferRecord: ...
```

`run_once()` は次の順序で処理する。

```text
1. store.claim_due_transfer()
2. claim後に`record.connector_version`と`record.mapping_version`を指定してconnectorとmappingを読み込む。最新版を参照しない
3. `store.get_latest_review_correction()`を読み、存在すれば`MappingEngine.apply_corrections()`でeffective payloadを作る。元`record.request`は変更しない
4. effective payload.delivery.operationをconnector.resolve_operation()へ渡し、mapping.operationsに含まれるOperationSelectionを得る
5. `operation.name in mapping.operations`、`operation.operation_id == connector.operation_bindings[operation.name].operation_id`、document type/tenantを検証する。どれかが不一致ならfailedまたはwaiting_reviewとし、送信しない
6. effective payload/mapping/target schemaを検証する
7. connector.validate_payload(effective payload, mapping, operation)でoperation単位のrequest schemaも検証する
8. issueがあればwaiting_reviewまたはfailedへ遷移し、転記先を呼ばない
9. claim.phase == "validate"で検証成功ならvalidating -> queuedへ遷移する
10. claim.phase == "deliver"ならMappingEngine.apply(effective payload, mapping, operation) -> build_request() -> send()
11. not_sentのretryable errorはretrying、恒久エラーはfailedへ遷移する
12. unknownはreconciliation_requiredへ遷移する
13. receivedはparse_response()後に抽出済み`target_resource_id`を`ReconciliationContext`へ渡して`reconcile(mode="postcondition")`を実行し、registeredだけをsucceededにする
14. transfer_eventsへattempt、分類、duration、request idだけを記録する
```

`recover_inflight()` は未完了の`delivering`と`cancellation_requested`を`reconciliation_required`へ移し、検証中にプロセスが落ちた`validating`は`accepted`へ戻す。`accepted`、`queued`、期限到来した`retrying`だけをclaim対象にする。新しい送信を`cancellation_requested`から開始しない。workerが実行中の送信から戻った時点でキャンセル要求を検知し、送信前なら`cancelled`、送信後の結果が不明なら`reconciliation_required`、成功応答なら`succeeded`へ遷移させる。SQLiteのclaimと状態更新を原子的に行う。

- [ ] **Step 5: review、cancel、reconcileの処理を実装する**

`approve`は最新の`ReviewCorrection`を`apply_corrections()`でeffective payloadへ適用し、同じmapping/connector versionとtarget schemaで再検証してから`queued`へ戻す。`correct`は元OCR値を上書きせず、tenant付き`save_review_correction()`で訂正値を暗号化した`review_corrections`レコードへ保存し、review event、訂正保存、review状態からの遷移を同一SQLite transactionで行う。review eventには`correction_ref`、actor、reasonだけを残し、次回claim時にも同じoverlayを適用する。`reject`は`failed`へ遷移する。`reconcile_transfer()`は`Connector.reconcile(ReconciliationContext(mode="unknown", ...))`を呼び、転記先で登録済みなら`succeeded`、未登録なら`queued`、確認不能なら`reconciliation_required`を維持する。未登録の確認後に再送する場合も同じtransferのattemptを増やし、別ジョブを作らない。

- [ ] **Step 6: workerテストを実行する**

Run: `uv run pytest tests/transfer/test_worker.py -q`

Expected: PASS。成功、低信頼度、マッピング不備、review訂正値のoverlay反映、固定connector/mapping version、429/5xx retry、Retry-After、retry上限、送信後timeout、`validating`/`delivering`再起動、review、cancel、reconcile、監査イベントを検証できる。続けて`uv run pytest -q`を実行する。

- [ ] **Step 7: コミットする**

```bash
git add src/transfer/worker.py tests/transfer/test_worker.py
git commit -m "feat: add durable transfer worker"
```

## Task 6: FastAPI、認可、Problem Detailsを実装する

**Files:**
- Create: `src/transfer/routes.py`
- Create: `src/transfer/app.py`
- Create: `src/transfer/limits.py`
- Create: `tests/transfer/test_api.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: Task 2のモデル/ストア、Task 3のpreview、Task 4のconnector registry、Task 5のworker。
- Produces: `create_router() -> APIRouter`、`create_app(settings: Settings | None = None) -> FastAPI`、`RateLimiter`、標準API全エンドポイント、`ocr-transfer-api` entrypoint。

- [ ] **Step 1: 認可とProblem Detailsの失敗テストを書く**

```python
def test_transfer_write_requires_scope(client):
    response = client.post("/v1/transfers", headers=token_without("transfer:write"), json=valid_payload)

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "FORBIDDEN"


def test_transfer_status_cannot_cross_tenant(client):
    response = client.get("/v1/transfers/tr-other-tenant", headers=tenant_token("tenant-a"))

    assert response.status_code == 404
```

- [ ] **Step 2: APIテストが実装不足で失敗することを確認する**

Run: `uv run pytest tests/transfer/test_api.py -q`

Expected: FAIL with an import error because `src/transfer/app.py` and `src/transfer/routes.py` do not exist yet.

- [ ] **Step 3: JWT authorizerとscope dependencyを実装する**

`auth.py`の`JwtAuthorizer`は署名、issuer、audience、期限を検証し、claimsから`tenant_id`、`sub`、`scope`を取り出す。ルートは次のdependencyを使う。

```python
def require_scope(scope: str) -> Callable[[Principal], Principal]:
    async def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if scope not in principal.scopes:
            raise ForbiddenError(code="FORBIDDEN", detail="required scope is missing")
        return principal
    return dependency
```

テストではFastAPI dependency overrideで固定Principalを注入し、本番の認可経路を弱める`X-Tenant-ID`認証を実装しない。scopeは `transfer:write`、`transfer:read`、`transfer:retry`、`transfer:cancel`、`transfer:review`、`transfer:reconcile`、`mapping:read`、`mapping:write`、`connector:read`、`connector:admin` をそのまま使用する。retry/cancel/review/reconcile endpointはそれぞれ同名のscopeを要求し、status/listは`transfer:read`、previewは`mapping:read`、connector validateは`connector:admin`を要求する。

`tests/transfer/test_api.py`は`test_settings`、`settings_factory`、`client`、`token_without(scope)`、`tenant_token(tenant_id)`、`valid_payload`を`conftest.py`から受け取り、FastAPIの`current_principal` dependencyだけをoverrideする。JWT検証そのものはTask 4の`JwtAuthorizer`テストで検証し、APIテストで認証を偽装する範囲をscope/tenant認可に限定する。

- [ ] **Step 4: 転送・管理・ヘルスルートを実装する**

`routes.py`は次の挙動を実装する。

- `POST /v1/transfers`: payloadをPydantic/JSON Schema検証し、テナントをPrincipalから決定し、公開済みmappingとconnectorのversionを解決する。そのversionを`create_or_get_transfer(..., connector_version, mapping_version)`へ渡して原子的に受付し、`202`を返す。idempotent replay時は保存済みversionを再利用する。
- `GET /v1/transfers`: `status`、connector、期間、document、correlation、cursor、limitで絞り、本文や秘密情報を返さない。
- `GET /v1/transfers/{id}`: tenant scopeで取得し、成功結果・エラー・review issueを返す。
- retry/cancel/review/reconcile:各scopeと状態遷移を検証し、監査eventを作る。
- mapping preview:入力payloadと`operation`を受け取り、tenantのconnector versionから`OperationSelection`を解決して転記先を呼ばずに`MappingPreview`を返す。previewは`MappingEngine.preview(payload, mapping, operation)`を使い、workerと同じoperation binding・mapping rule・target schemaを検証する。
- connector一覧: `connector:read`、connector登録・validate: `connector:admin`、mapping一覧: `mapping:read`、mapping登録: `mapping:write`を要求し、資格情報値を受け取らない。
- health live:依存先を見ずに`200`、health ready:SQLite・credential resolver・workerを確認して`200`または`503`。
- 受付時は `Idempotency-Key` の1-256文字、`Content-Type`、`max_payload_bytes`、JSON Schema、利用可能なconnector/mapping/document_typeを同期検証する。テナント単位・client単位の `RateLimiter.check()` が拒否した場合は `429` と `Retry-After` を返す。
- `POST /v1/connectors` はspec snapshotとcredential_refだけを保存し、`POST /v1/mappings` は公開前のrule重複・対象schema・document_type・connector versionを検証する。
- `limits.py`の`RateLimiter`はtenant/clientごとのtoken bucketをメモリに保持し、`RateLimitDecision(allowed, retry_after_seconds)`を返す。設定変更とプロセス再起動でリセットされるMVP実装であり、分散rate limitはMVP後に外部ストアへ置き換える。

```python
class RateLimitDecision(BaseModel):
    allowed: bool
    retry_after_seconds: int


class Clock(Protocol):
    def monotonic(self) -> float: ...


class RateLimiter:
    def __init__(self, requests_per_minute: int, burst: int, clock: Clock) -> None: ...
    def check(self, tenant_id: str, client_id: str) -> RateLimitDecision: ...
```

`POST /v1/transfers`は本文の`metadata.tenant_id`が存在する場合、Principalのtenant_idと一致するか検証する。一致しない場合は403を返し、本文のtenant_idだけで認可コンテキストを上書きしない。

- [ ] **Step 5: app factoryとlifespanを実装する**

```python
def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        credential_resolver = EnvironmentCredentialResolver(resolved)
        protector = await create_payload_protector(resolved, credential_resolver)
        store = SqliteTransferStore(resolved.database_path, protector=protector)
        registry = ConnectorRegistry(store, credential_resolver, resolved)
        worker = TransferWorker(
            store=store,
            registry=registry,
            mapping_engine=MappingEngine(),
            retry_policy=RetryPolicy.from_settings(resolved),
        )
        await store.initialize()
        await worker.recover_inflight()
        task = asyncio.create_task(worker.start()) if resolved.worker_enabled else None
        app.state.store = store
        app.state.registry = registry
        app.state.worker = worker
        try:
            yield
        finally:
            if task is not None:
                await worker.stop()
                task.cancel()
            await store.close()

    app = FastAPI(title="OCR Transfer Connector API", version="1.0.0", lifespan=lifespan)
    app.include_router(create_router())
    return app
```

`routes.py`のdependencyはrequest時に`request.app.state.store`、`registry`、`worker`を参照し、lifespan前にplaintext storeを生成するclosureを持たない。Task 4で実装した`create_payload_protector(settings, resolver)`を起動時に呼び、`data_encryption_key_ref`未設定・未解決・不正形式を必ず`RuntimeError`にする。Task 6のAPI単体テストは暗号化test settingsを使い、Task 8で鍵なし起動拒否と監査列への適用を追加検証する。

`src/transfer/app.py`に `main() -> None` を定義し、`argparse`で`--host`（既定`127.0.0.1`）と`--port`（既定`8080`）を受け取り、`uvicorn.run("src.transfer.app:create_app", factory=True, host=args.host, port=args.port)`を実行する。`pyproject.toml`のentrypointは `ocr-transfer-api = "src.transfer.app:main"` とする。例外handlerは`ProblemDetail`だけを返し、内部traceback、token、OCR本文、転記先レスポンス全文を返さない。

- [ ] **Step 6: APIテストを実行する**

Run: `uv run pytest tests/transfer/test_api.py -q`

Expected: PASS。受付202、idempotent replay、409 conflict、status/list、review/cancel/reconcile、操作単位scope（retry/cancel/review/reconcileを含む）、tenant isolation、Problem Details、health、413/415/429、scopeごとのconnector/mapping制限を検証できる。続けて`uv run pytest -q`を実行する。

- [ ] **Step 7: コミットする**

```bash
git add src/transfer/routes.py src/transfer/app.py tests/transfer/test_api.py pyproject.toml
git commit -m "feat: expose OCR transfer API"
```

## Task 7: スタブAPIでエンドツーエンド動作を検証する

**Files:**
- Create: `tests/transfer/test_integration.py`
- Create: `tests/transfer/fixtures/target_openapi.yaml`
- Create: `tests/transfer/fixtures/target_openapi_alt.yaml`
- Create: `tests/transfer/fixtures/invoice_mapping.json`
- Create: `examples/ocr/invoice-transfer.json`
- Create: `examples/ocr/invoice-transfer-alt.json`
- Modify: `README.md`
- Modify: `env.example`

**Interfaces:**
- Consumes: Task 1-6の公開契約と実装。
- Produces: 実際のcreate/update/upsert、認証、事後照会、再起動回復を通る受入テストと起動手順。

- [ ] **Step 1: スタブ転記先を定義する**

`tests/transfer/test_integration.py`内のASGIスタブは次のoperationを提供する。

```text
POST /target/invoices
GET  /target/invoices/{external_id}
PUT  /target/invoices/{external_id}
GET  /target/invoices?external_id=...
```

API key header `X-Api-Key: test-target-key`、`X-Api-Version: 2026-01-01`、`external_id`の一意制約、初回429、接続断、400検証エラーをテスト制御できるようにする。

`target_openapi_alt.yaml`と2つ目のスタブ設定は同じcore codeを別のパス、必須ヘッダー、Bearer認証で実行する。2つの標準OCR入力fixtureは異なるOCRベンダーの正規化結果として同じ`TransferRequest`モデルに通し、受入テストは両方でcreate/update/upsertを検証する。

`tests/transfer/conftest.py`は`app_client`（`worker_enabled=False`のhttpx ASGI transport）、`target_api`（stub状態と呼び出し回数）、`invoice_payload()`、`wait_for_status(transfer_id, status)`を提供する。待機helperはsleepを固定時間で繰り返さず、`app.state.worker.run_once()`を明示的に呼び出して状態を進めるため、lifespanのbackground workerと競合しない。

- [ ] **Step 2: create/update/upsertの受入テストを書く**

```python
@pytest.mark.asyncio
async def test_invoice_upsert_returns_target_id_and_is_idempotent(app_client, target_api):
    first = await app_client.post_transfer(invoice_payload(), key="key-1")
    replay = await app_client.post_transfer(invoice_payload(), key="key-1")

    assert first.status_code == 202
    assert replay.headers["X-Idempotent-Replay"] == "true"
    completed = await app_client.wait_for_status(first.json()["transfer_id"], "succeeded")
    assert completed["result"]["target_resource_id"]
    assert target_api.create_count == 1
```

- [ ] **Step 3: 受入テストがfixture不足で失敗することを確認する**

Run: `uv run pytest tests/transfer/test_integration.py -q`

Expected: FAIL because the target OpenAPI fixture, mapping fixture, and registered test connector are not configured yet.

- [ ] **Step 4: 認証と契約プリフライトを通す**

登録したfixture OpenAPIに対してvalidate endpointを呼び、絶対base URL、`$ref`、operation security、required header、request/response schemaが合格することを確認する。資格情報はテストresolverから供給し、ログcapturing fixtureで秘密値が出ないことを確認する。API key、Bearer、OAuth2 Client Credentialsをfixtureごとに切り替え、期限切れOAuth tokenは1回更新後に成功することを検証する。create、update、upsertを同じcore codeで異なるoperation bindingとして実行する。

- [ ] **Step 5: 障害復旧の受入テストを書く**

429は`retrying`から成功へ進み、送信後接続断は`reconciliation_required`で止まり、GET照会で未登録を確認した後だけ再送して成功することを検証する。workerを停止・再生成したとき、`delivering`が自動的に`reconciliation_required`へ移ることも確認する。

- [ ] **Step 6: 起動手順と運用設定をREADMEへ追加する**

```bash
uv run ocr-transfer-api --host 127.0.0.1 --port 8080
curl -H "Authorization: Bearer $TRANSFER_TEST_TOKEN" \\
     -H "Idempotency-Key: invoice-0001" \\
     -H "Content-Type: application/json" \\
     -d @examples/ocr/invoice-transfer.json \\
     http://127.0.0.1:8080/v1/transfers
```

READMEには、connector/mapping登録、secret ref設定、状態照会、`reconciliation_required`の運用、`uv run pytest -q`を記載する。`examples/ocr/invoice-transfer.json`は秘密情報を含まない標準入力として追加し、実際のsecret値はサンプルに書かない。

- [ ] **Step 7: 統合テストと全テストを実行する**

Run: `uv run pytest tests/transfer/test_integration.py -q`

Expected: PASS。続けて `uv run pytest -q` を実行し、既存OpenAPI/MCPテストを含めて全テストが成功する。

- [ ] **Step 8: コミットする**

```bash
git add tests/transfer/test_integration.py tests/transfer/fixtures examples/ocr/invoice-transfer.json README.md env.example
git commit -m "test: verify OCR transfer connector end to end"
```

## Task 8: 運用保護・可観測性・契約検証を完了する

**Files:**
- Modify: `src/transfer/limits.py`
- Create: `src/transfer/observability.py`
- Create: `scripts/export_transfer_contract.py`
- Create: `tests/transfer/test_operational_safeguards.py`
- Create: `tests/transfer/test_generated_contract.py`
- Modify: `src/transfer/routes.py`
- Modify: `src/transfer/worker.py`
- Modify: `src/transfer/store.py`
- Modify: `src/transfer/settings.py`
- Modify: `env.example`
- Modify: `pyproject.toml`
- Modify: `docs/api/ocr-transfer-openapi.yaml`
- Modify: `schemas/ocr-transfer-v1.json`
- Modify: `schemas/mapping-v1.json`

**Interfaces:**
- Consumes: Task 2の`SqliteTransferStore`、Task 5のworker、Task 6の`create_app()`とPydanticモデル。
- Produces: `RateLimiter`、`PayloadLimits`、`TransferObservability`、監査保持処理、実装ルートと公開OpenAPI/JSON Schemaの差分を検出する再現可能な検証コマンド。Task 4の`PayloadProtector`と`create_payload_protector()`を運用配線へ組み込む。

- [ ] **Step 1: 運用保護の失敗テストを書く**

```python
def test_rate_limiter_returns_retry_after_for_tenant_burst(fake_clock) -> None:
    limiter = RateLimiter(requests_per_minute=1, burst=1, clock=fake_clock)

    assert limiter.check("tenant-a", "client-a").allowed is True
    decision = limiter.check("tenant-a", "client-a")

    assert decision.allowed is False
    assert decision.retry_after_seconds >= 1


def test_payload_limits_reject_too_many_lines_and_long_strings() -> None:
    limits = PayloadLimits(max_line_items=1, max_string_length=8)

    with pytest.raises(PayloadLimitError):
        limits.validate(sample_transfer_request(line_items=2))


def test_redactor_removes_secret_headers_and_ocr_values() -> None:
    redacted = SecretRedactor().event_detail({
        "headers": {"Authorization": "Bearer secret-token", "X-Request-ID": "req-1"},
        "ocr": {"invoice_number": "INV-0001"},
    })

    assert "secret-token" not in json.dumps(redacted)
    assert "INV-0001" not in json.dumps(redacted)
    assert redacted["headers"]["X-Request-ID"] == "req-1"


@pytest.mark.asyncio
async def test_store_encrypts_payload_and_purges_expired_audit_events() -> None:
    store = SqliteTransferStore(path, protector=FernetPayloadProtector(fernet_test_key))
    await store.initialize()
    await seed_completed_transfer(store)

    assert b"invoice_number" not in await read_raw_request_blob(path)
    assert await store.purge_expired_audit_events(before=old_timestamp) == 1


@pytest.mark.asyncio
async def test_app_refuses_to_start_without_data_encryption_key(settings_factory):
    settings = settings_factory(data_encryption_key_ref=None)
    app = create_app(settings)

    with pytest.raises(RuntimeError, match="data encryption key"):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.asyncio
async def test_audit_retention_persists_chain_checkpoint(store, old_timestamp):
    await seed_completed_transfer(store)

    await store.purge_expired_audit_events(before=old_timestamp)

    assert await store.get_latest_audit_checkpoint() is not None


@pytest.mark.asyncio
async def test_audit_checkpoints_are_isolated_per_tenant(store, old_timestamp):
    await seed_completed_transfer(store, tenant_id="tenant-a")
    await seed_completed_transfer(store, tenant_id="tenant-b")

    await store.purge_expired_audit_events(before=old_timestamp)

    checkpoint_a = await store.get_latest_audit_checkpoint("tenant-a")
    checkpoint_b = await store.get_latest_audit_checkpoint("tenant-b")
    assert checkpoint_a["tenant_id"] == "tenant-a"
    assert checkpoint_b["tenant_id"] == "tenant-b"
    assert checkpoint_a["checkpoint_hash"] != checkpoint_b["checkpoint_hash"]
```

- [ ] **Step 2: 運用保護テストが実装不足で失敗することを確認する**

Run: `uv run pytest tests/transfer/test_operational_safeguards.py -q`

Expected: FAIL with an import error because `src/transfer/observability.py` does not exist yet or because the new safeguard APIs are not wired.

- [ ] **Step 3: 上限、redaction、暗号化、監査保持を実装する**

Task 6で作成した`limits.py`へフィールド上限検証を追加し、次の型を実装する。

```python
class PayloadLimits:
    def __init__(self, max_payload_bytes: int = 10 * 1024 * 1024, max_fields: int = 500, max_line_items: int = 200, max_string_length: int = 10_000) -> None: ...
    def validate(self, payload: TransferRequest) -> None: ...
```

`observability.py`に、秘密情報を値として保持しない`SecretRedactor`、固定ラベルだけを使う`TransferObservability`、OpenTelemetry span生成を実装する。Task 4のFernet protectorをstoreの本文・監査列へ適用し、保持処理とhash chainを`store.py`へ追加する。

Task 2の`errors.py`に`PayloadLimitError(ValueError)`を定義し、`PayloadLimits.validate()`はこの例外だけを投げる。routesはこれを413 Problem Detailsへ変換し、worker内の既存payload/schemaエラーとは混同しない。

`request_json`、`result_json`、`error_json`、`transfer_events.detail_json`、`review_corrections.correction_json`はFernet暗号文として保存し、起動時に`settings.data_encryption_key_ref`を`CredentialResolver.resolve()`で解決した鍵を`FernetPayloadProtector`へ渡す。鍵の解決・形式検証に失敗した場合、または本番設定で`protector`が未指定の場合は起動を失敗させ、plaintext fallbackを許可しない。テストでは生成鍵を使い、本番はSecret Managerまたは同等の保護保管先を必須とする。tenantごとの`transfer_events`には`event_hash`と`previous_event_hash`を保存し、イベントの追加をSQLite transaction内で行う。hashはtenant、イベントmetadata、暗号文detail、直前hashのcanonical表現から計算する。`payload_retention_days`を超えた終端transferの暗号化本文は`purge_expired_payloads()`で削除し、メタデータとハッシュは残す。`audit_retention_days`を超えたイベントをtenant単位で削除する前に、削除範囲の終端hashから`checkpoint_hash`を計算して`audit_chain_checkpoints`へ保存する。`tenant_id`を省略したpurgeはdistinct tenantごとに独立transactionを実行し、checkpointとchainをtenant間で共有しない。保持後の最初のイベントの`previous_event_hash`はそのtenantの`checkpoint_hash`とし、検証器はイベントhashまたは同一tenantのcheckpoint hashのどちらかへ連鎖できることを確認する。

次のコマンドで依存関係を追加し、`env.example`へ値のない設定名だけを追加する。`bandit`は既存依存なので重複追加しない。

```bash
uv add prometheus-client opentelemetry-api
uv add --dev bandit
```

追加する設定名は`TRANSFER_PAYLOAD_RETENTION_DAYS`、`TRANSFER_AUDIT_RETENTION_DAYS`、`TRANSFER_DATA_ENCRYPTION_KEY_REF`、`TRANSFER_WORKER_ENABLED`とし、秘密値や実値は記載しない。SQLite backupの保持・削除はアプリ外の運用手順としてREADMEに明記する。

`tests/transfer/conftest.py`へ`fernet_test_key`、`sqlite_path`、`old_timestamp`、`store` fixture、`read_raw_request_blob(path)`、`seed_completed_transfer(store, tenant_id="tenant-test")` helper、`FakeClock` classと`fake_clock` fixtureを追加し、テスト用鍵とテスト用時刻以外の秘密値をfixtureへ書かない。`store`は暗号化fixtureを使い、Task 2のstore単体テストだけが明示的に`protector=None`を使用する。

- [ ] **Step 4: APIとworkerへ運用保護を配線する**

`create_app()`で`RateLimiter`、`PayloadLimits`、`TransferObservability`を生成し、lifespanで`settings.data_encryption_key_ref`を解決して`PayloadProtector`付きのstoreを初期化する。routeとworkerへ依存を注入する。`worker_enabled=False`のテスト設定ではbackground taskを起動せず、`app.state.worker`をfixtureから明示的に実行する。受付前に`Content-Length`と実体サイズを確認し、Pydantic検証後にフィールド数・明細行数・文字列長を検証する。状態照会・一覧・エラーのAPIレスポンス、ログ、監査event、metric label、trace attributeにはOCR値、request/response body、token、passwordを渡さない。認可済みmapping previewだけは、転記先へ送らない変換結果を返してよいが、認証値・Cookie・秘密ヘッダーは除外する。workerは`transfer.accepted`、`transfer.validation`、`transfer.delivery`、`transfer.reconciliation` spanと、状態・分類・connector_idだけのcounter/histogramを記録する。

- [ ] **Step 5: 運用保護テストを実行する**

Run: `uv run pytest tests/transfer/test_operational_safeguards.py tests/transfer/test_api.py tests/transfer/test_worker.py -q`

Expected: PASS。tenant/client rate limit、413、フィールド/明細/文字列上限、鍵未設定時の起動拒否、request/result/errorとreview correctionの暗号化保存、終端payload retention、監査のhash chain・checkpoint・retention、ログ/metric/traceのredactionを確認できる。

- [ ] **Step 6: 公開契約の差分テストを書く**

```python
def test_documented_paths_match_fastapi_paths() -> None:
    documented = load_yaml("docs/api/ocr-transfer-openapi.yaml")["paths"]
    generated = create_app(settings_factory()).openapi()["paths"]

    assert set(documented) == set(generated)
    assert set(documented["/v1/transfers"]) >= {"post", "get"}
    assert generated["/v1/transfers"]["post"]["responses"]["202"]
```

- [ ] **Step 7: 契約差分テストが実装不足で失敗することを確認する**

Run: `uv run pytest tests/transfer/test_generated_contract.py -q`

Expected: FAIL until the generated contract exporter and route/schema synchronization are implemented.

- [ ] **Step 8: 契約export scriptを実装する**

`uv run python scripts/export_transfer_contract.py` は`create_app(Settings())`からOpenAPI 3.1 JSONを生成し、YAMLへ書き出す。Pydanticの`model_json_schema()`から標準入力とマッピングSchemaを出力する。書き込みは一時ファイルへ行い、成功後に同一ディレクトリへ置換する。

- [ ] **Step 9: 契約・静的検査とセキュリティ検査を実行する**

Run:

```bash
uv run python scripts/export_transfer_contract.py --check
uv run python -m compileall src/transfer tests/transfer
uv run bandit -r src/transfer
uv run pytest -q
git diff --check
```

Expected: 契約差分なし、compile成功、秘密情報のハードコードなし、全テスト成功、空白エラーなし。

- [ ] **Step 10: 完了条件を確認してコミットする**

```bash
git add src/transfer/limits.py src/transfer/observability.py src/transfer/routes.py src/transfer/worker.py src/transfer/store.py src/transfer/settings.py tests/transfer/test_operational_safeguards.py scripts/export_transfer_contract.py tests/transfer/test_generated_contract.py docs/api/ocr-transfer-openapi.yaml schemas/ocr-transfer-v1.json schemas/mapping-v1.json env.example pyproject.toml
git commit -m "chore: verify transfer API contracts"
```

## Verification Checklist

実装完了時に、次をすべて確認してから完了とする。

- `uv run pytest -q` が既存テストと転記テストを含めて成功する。
- `POST /v1/transfers` が必ず `Idempotency-Key`、認可、tenant context、標準Schemaを検証する。
- 同一tenant・同一key・同一hashの再送は新規ジョブを作らず、異なるhashは409になる。
- `create`、`update`、`upsert` がスタブREST APIで成功し、転記先IDと事後照会結果を保存する。
- operation単位のsecurity、API key/Bearer/Basic/OAuth2、追加必須ヘッダーが正しく適用される。
- 未解決`$ref`、相対base URL、欠落operation auth、パラメータ型不一致、missing success schemaが公開前に拒否される。
- 低信頼度・mapping errorは転記先へ送信せず、`waiting_review`とissueを保存する。
- 429/5xxは指数backoff、送信後timeout/connection resetは`reconciliation_required`になる。
- `reconciliation_required`は照会成功まで新規作成を再送しない。
- worker再起動後、`delivering`を自動再送せず照会待ちにする。
- 状態変更、mapping version、connector version、attempt、correlation idが監査イベントに残る。
- OCR本文、token、password、API key、秘密ヘッダーがログ・レスポンス・監査に出ない。
- `uv run python scripts/export_transfer_contract.py --check` と `git diff --check` が成功する。

## Scope Deferred After MVP

- 複数文書のbatch upsertと部分成功の詳細モデル。
- multipart、ファイル参照、画像転送。
- 複数転記先へのfan-outと補償トランザクション。
- mTLS、OAuth Authorization Code、ユーザー委譲。
- Webhook通知、人手レビュー画面、外部Secret Managerの実装アダプタ。
- SFTP、SOAP、RDB直接接続。
- MCPアダプタの自動生成。MVPのRESTコネクタと標準APIが安定した後、別計画で追加する。