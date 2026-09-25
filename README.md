# OpenAPI to MCP Converter

[![License](https://img.shields.io/github/license/agentic-community/openapi-to-mcp)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-Compatible-green)](https://github.com/modelcontextprotocol)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

OpenAPI / Swagger仕様から、実際にAPIを呼び出せるMCP serverとMCP clientを生成するツールです。LLMによる評価・仕様改善に加えて、同じ入力から同じMCP toolを生成する決定的なコード生成にも対応しています。

> [!CAUTION]
> 付属の例は検証・学習用です。本番利用前に、生成コード、認証、入力検証、レート制限、ログの秘匿性を必ず確認してください。LLMを利用する場合は、プロンプトインジェクション対策も設定してください。

## 目次

- [主な機能](#主な機能)
- [アーキテクチャ](docs/ARCHITECTURE.md)
- [前提条件](#前提条件)
- [インストール](#インストール)
- [基本的な使い方](#基本的な使い方)
- [コマンドラインオプション](#コマンドラインオプション)
- [環境変数](#環境変数)
- [OCR転送API](#ocr転送api)
- [Kintone MCPモック検証](#kintone-mcpモック検証)
- [サンプルAPI](#サンプルapi)
- [設定](#設定)
- [生成物と結果](#生成物と結果)
- [LLMプロバイダー](#llmプロバイダー)
- [開発とテスト](#開発とテスト)
- [セキュリティ](#セキュリティ)
- [ライセンス](#ライセンス)

## 主な機能

- OpenAPI 3.xとSwagger 2.0の読み込み
- OpenAPI operationごとのMCP tool生成
- OpenAPI参照（`$ref`）を含むパラメータ・request bodyの正規化
- API key、Bearer / OAuth、Basic認証の生成
- HTTPエラー、接続エラー、timeout、JSON解析エラーの構造化
- 認証tokenやパスワードをエラー・履歴へ出さない秘匿処理
- Amazon Bedrock、Anthropic、Azure OpenAIを使った評価・仕様改善
- 評価結果、利用量、生成ソース、tool仕様の保存
- ローカルKintone互換mockを使った転記E2E検証

LLM評価を使わない場合でも、仕様からoperationを正規化してMCP server / clientを決定的に生成できます。生成されたserverは各operationを1つのMCP toolとして公開します。

## アーキテクチャ

構成、生成フロー、認証、HTTP runtime、構造化エラー、Kintone転記経路の詳細は
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) にまとめています。

要点は、OpenAPIのAPI操作を整理して操作ごとに1つのMCPツールを生成し、
生成MCPサーバーの共通HTTP呼び出し部分から呼び出し先APIを呼ぶ構成です。通常経路は
決まった手順でコードを生成し、生成後にAPI操作数・MCPツール数・構文・fallback有無を検証します。

## 前提条件

- Python 3.11以上
- [uv](https://github.com/astral-sh/uv)
- LLM評価を使う場合は、選択したプロバイダーの認証情報
  - Amazon Bedrock: AWS profile / region
  - Anthropic: API key
  - Azure OpenAI: endpoint、API key、deployment名、API version

## インストール

### uvのインストール

macOS / Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows PowerShell:

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### プロジェクトのセットアップ

```bash
git clone https://github.com/agentic-community/openapi-to-mcp.git
cd openapi-to-mcp
uv sync --extra dev
cp env.example .env
```

`.env` には実際のsecretをコミットしないでください。アプリケーションコードから`.env`を直接読み込まず、既存の設定ローダーと環境変数を使用します。

## 基本的な使い方

ローカルのOpenAPI仕様を評価・変換します。

```bash
openapi-to-mcp examples/hello.yaml
openapi-to-mcp examples/sample_api.yaml
```

URLから仕様を取得します。

```bash
openapi-to-mcp --url https://raw.githubusercontent.com/agentic-community/openapi-to-mcp/refs/heads/main/examples/hello.yaml
```

評価だけを実行し、MCP生成を省略します。

```bash
openapi-to-mcp examples/sample_api.yaml --eval-only
```

出力先を指定します。

```bash
openapi-to-mcp examples/hello.yaml --output results/my-api-evaluation.json
```

生成後は、既定で固定seedのモックデータを使ったローカルAPI検証を実行します。
検証に失敗した場合は、`server.py`、`client.py`、`runtime.py`だけを対象に最大2回まで
候補を修正し、全operationの再検証に通った候補だけを採用します。元のOpenAPI仕様は
変更しません。検証結果は生成先の`mcpserver/verification/verification_report.json`と
`verification_summary.md`に保存されます。

自動検証・限定修正の設定は`config/config.yml`の
`generated_verification_enabled`、`generated_repair_enabled`、
`generated_repair_max_attempts`で変更できます。

## コマンドラインオプション

```text
openapi-to-mcp [OPTIONS] [FILENAME]

引数:
  FILENAME              OpenAPI仕様ファイル（YAMLまたはJSON）

オプション:
  --url URL              OpenAPI仕様を取得するURL
  --output FILE          結果の出力先
  --eval-only            評価だけを実行し、MCP生成を省略
  --verify-generated     生成物のモック動作検証を有効化
  --repair-generated     生成物の限定修正を有効化（検証も有効化）
  --generated-verification-seed INTEGER
                         モックデータ生成のseed
  --generated-repair-attempts INTEGER
                         限定修正の最大回数（0〜2）
  --verbose              詳細ログを有効化
  --show-env             環境設定を表示
```

## 環境変数

### Amazon Bedrock

```bash
export AWS_PROFILE=your-profile-name
export AWS_REGION=us-east-1
```

### Anthropic

```bash
export ANTHROPIC_API_KEY=your-api-key
```

### Azure OpenAI

```bash
export AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
export AZURE_OPENAI_API_KEY=your-api-key
export VISION_MODEL=your-deployment-name
export AZURE_OPENAI_API_VERSION=2024-10-21
```

`.env`の`MODEL`でモデルを選択できます。Azure OpenAIを直接使う場合は、`AZURE_OPENAI_ENDPOINT`と`AZURE_OPENAI_API_KEY`が設定されているときに`VISION_MODEL`のdeployment名を使います。`MODEL=azure/your-deployment-name`形式も利用できます。

## OCR転送API

OCR転送サービスは、標準化された`ocr-transfer/v1` JSONを受け取り、登録済みのREST / OpenAPI connectorへ1文書ずつ配信します。JWTの検証は既存のidentity providerに対して行い、このリポジトリはtoken issuerを提供しません。

サーバー側の設定例です。

```dotenv
TRANSFER_DATABASE_PATH=./data/transfer.sqlite3
TRANSFER_JWT_ISSUER=https://issuer.example.com/
TRANSFER_JWT_AUDIENCE=ocr-transfer
TRANSFER_JWKS_URL=https://issuer.example.com/.well-known/jwks.json
TRANSFER_ALLOWED_HOSTS=["127.0.0.1","localhost","target.example.com"]
TRANSFER_MAX_PAYLOAD_BYTES=1048576
TRANSFER_MAX_ATTEMPTS=5
TRANSFER_WORKER_ENABLED=true
TRANSFER_WORKER_POLL_SECONDS=1
TRANSFER_REQUESTS_PER_MINUTE=120
TRANSFER_BURST=20
TRANSFER_DATA_ENCRYPTION_KEY_REF=key://transfer/data
TRANSFER_CREDENTIALS_JSON={"tenants":{"tenant-a":{"vault://connectors/invoice-target":{"auth":"REPLACE_WITH_TARGET_SECRET"}}},"global":{"config://headers/target-api-version":{"value":"2026-01-01"},"key://transfer/data":"REPLACE_WITH_FERNET_KEY"}}
```

`TRANSFER_CREDENTIALS_JSON`はcredential resolverの入力です。connectorの`credential_ref`が認証bundleを選択し、`global`内の`config://...`がsecretではないconnector headerを提供します。secretはOpenAPI、mapping、OCR JSONへ入れず、環境変数またはsecret managerで管理してください。

すでに発行済みのJWTを`TRANSFER_TEST_TOKEN`へ設定してサービスを起動します。

```bash
uv run ocr-transfer-api --host 127.0.0.1 --port 8080
```

connector、mapping、transferの登録例は、実際のtarget OpenAPI snapshotとsecret-freeなmappingを使う必要があります。標準OCR入力の例は [examples/ocr/invoice-transfer.json](examples/ocr/invoice-transfer.json) と [examples/ocr/invoice-transfer-alt.json](examples/ocr/invoice-transfer-alt.json) です。

## Kintone MCPモック検証

ローカルKintone互換mockを使い、OCRデータの転記から取得・更新・削除、異常データ拒否、HTTPエラー、通信エラー、token秘匿まで検証できます。実Kintoneには接続しません。

fixtureは [examples/ocr/kintone-transfer.json](examples/ocr/kintone-transfer.json) です。OCR標準形式の帳票2枚を含み、帳票1枚をKintoneの1レコードへまとめて転記します。

`document.document_id` はOCR元の追跡用IDであり、Kintoneの標準フィールドではありません。このモックの請求書アプリ定義にも含めず、重複排除・更新キーにはアプリ側で一意設定した `invoice_number` を使います。実際のアプリフィールドは `getAppFormFields` (`/k/v1/app/form/fields.json`) で取得できます。

| OCR document_id | invoice_number | 明細行 | 転記方法 |
| --- | --- | ---: | --- |
| `doc-invoice-0001` | `INV-0001` | 2 | `postRecords`で一括登録 |
| `doc-invoice-0002` | `INV-0002` | 2 | `postRecords`で一括登録 |

モックの転記先フィールドは `invoice_number`、`invoice_date`、`vendor_name`、`subtotal`、`tax_amount`、`total_amount`、`currency`、`status`、`ocr_confidence`、`source_file`、`ocr_text_ref`、`line_items`（サブテーブル）です。Kintoneの `NUMBER` とサブテーブル内の数値は `value` を文字列で送ります。

異常データもfixtureに3件あります。

| case | 異常内容 | 応答 |
| --- | --- | --- |
| `missing_invoice_number` | Kintone一意キーの請求書番号が空 | `400 / MOCK_RE02` |
| `unsupported_status` | statusが`unknown` | `400 / MOCK_RE03` |
| `confidence_out_of_range` | OCR信頼度が`1.2` | `400 / MOCK_RE03` |

### 3つの端末で実行する場合

端末1でmockを起動します。

```bash
uv run python examples/kintone_stub_server.py --port 9100 --token mock-token
```

端末2で生成MCP serverを起動します。

```bash
API_BASE_URL=http://127.0.0.1:9100 \
KINTONE_API_TOKEN=mock-token \
uv run python results/azure/openapi/mcpserver/server.py \
  --port 9001 --transport streamable-http
```

端末3で帳票1枚分のrecordを送信します。複数帳票をまとめる場合は、同じ `records` 配列へrecordを追加します。

```bash
uv run python results/azure/openapi/mcpserver/client.py \
  --server-url http://127.0.0.1:9001/mcp/ \
  --tool postRecords \
  --arguments '{"body":{"app":1,"records":[{"invoice_number":{"type":"SINGLE_LINE_TEXT","value":"INV-0001"},"invoice_date":{"type":"DATE","value":"2026-09-18"},"vendor_name":{"type":"SINGLE_LINE_TEXT","value":"株式会社サンプル商事"},"subtotal":{"type":"NUMBER","value":"11636"},"tax_amount":{"type":"NUMBER","value":"1164"},"total_amount":{"type":"NUMBER","value":"12800"},"currency":{"type":"DROP_DOWN","value":"JPY"},"status":{"type":"DROP_DOWN","value":"registered"},"line_items":{"type":"SUBTABLE","value":[{"value":{"description":{"type":"SINGLE_LINE_TEXT","value":"クラウド利用料"},"quantity":{"type":"NUMBER","value":"2"},"unit_price":{"type":"NUMBER","value":"4000"},"amount":{"type":"NUMBER","value":"8000"}}}]}}]}}'
```

帳票2件の一括登録、一括更新、フォーム定義取得、異常データ3件、削除、404、接続エラーをまとめて検証する場合は、次のE2Eを実行します。

```bash
uv run pytest tests/test_kintone_stub_server.py tests/test_kintone_mcp_e2e.py -q
```

現在の実測結果は次のとおりです。

```text
13 passed, 1 warning
```

実際の入力JSON、Kintone field形式への変換、登録応答、取得結果、異常応答、404、接続エラーは [docs/KINTONE_MCP_MOCK_RESULTS.md](docs/KINTONE_MCP_MOCK_RESULTS.md) に掲載しています。Pydanticのwarningはなく、残るwarningはStarlette / AnyIO依存関係由来です。

Kintoneの請求書fixtureは動的フォームとOCR固有の一括転記を含むため、汎用のschemaベース
検証とは別にこのE2Eで検証します。生成CLIの自動検証レポートにはOpenAPIで表現できる
operation契約が入り、請求書fixtureの実測結果はこのE2Eと上記ドキュメントで確認します。

request historyは次で取得できます。

```bash
curl http://127.0.0.1:9100/__mock/requests
```

historyには`X-Cybozu-API-Token`が存在したかどうかだけを記録し、token値は記録しません。

## サンプルAPI

### 付属のsample API

`examples/sample_api.yaml`は、宇宙船のtelemetry、crew、mission、scientific experimentを持つサンプルAPIです。stub serverと生成済みMCP serverを使って一連の呼び出しを確認できます。

結果のサンプルは `examples/results/anthropic/sample_api/` にあります。生成されるファイルの意味は [docs/RESULTS_GUIDE.md](docs/RESULTS_GUIDE.md) を参照してください。

```bash
# 端末1: stub server
uv run python examples/stub_server.py --port 9002

# 端末2: 生成MCP server
export AUTH_TOKEN="your-secret-token"
cd examples/results/anthropic/sample_api/mcpserver/
python server.py --port 9001 --base-url http://localhost:9002

# 端末3: MCP client
python client.py --server-url http://localhost:9001/mcp
```

### その他の例

```bash
openapi-to-mcp examples/hello.yaml
openapi-to-mcp examples/sample_api.yaml
openapi-to-mcp --url https://raw.githubusercontent.com/agentic-community/openapi-to-mcp/refs/heads/main/examples/hello.yaml
openapi-to-mcp examples/sample_api.yaml --eval-only
openapi-to-mcp examples/hello.yaml --output results/my-api-evaluation.json
```

## 設定

設定ファイルは [config/config.yml](config/config.yml) です。

```yaml
model: bedrock/us.anthropic.claude-3-5-sonnet-20241022-v2:0
# Anthropicを直接使う場合:
# model: anthropic/claude-3-5-sonnet-20241022
max_tokens: 8192
temperature: 0.0
timeout_seconds: 300
good_evaluation_threshold: 3.0
generate_mcp_threshold: 3.0
debug: false
```

`good_evaluation_threshold`は評価を良好と判定する最低点、`generate_mcp_threshold`はMCP生成を許可する最低点です。通常は完全性とAI利用適性の両方がしきい値以上である必要があります。

## 生成物と結果

通常の出力は次の構成です。

```text
output/
└── results_YYYYMMDD_HHMMSS_<spec-name>_<provider>/
    ├── evaluation_YYYYMMDD_HHMMSS.json
    ├── summary_YYYYMMDD_HHMMSS.md
    ├── usage_YYYYMMDD_HHMMSS.json
    ├── enhanced_spec_YYYYMMDD_HHMMSS.yaml
    ├── original_spec_YYYYMMDD_HHMMSS.yaml
    └── mcpserver/
        ├── server.py
        ├── client.py
        ├── requirements.txt
        ├── README.md
        └── tool_spec.txt
```

- `evaluation_*.json`: completeness、security、AI readinessなどの評価結果
- `summary_*.md`: 人が読むための評価要約
- `usage_*.json`: token使用量とコスト
- `enhanced_spec_*.yaml`: 改善後のOpenAPI仕様
- `mcpserver/`: MCP server、client、tool仕様、依存関係、利用方法

生成済みサンプルの読み方は [docs/RESULTS_GUIDE.md](docs/RESULTS_GUIDE.md) を参照してください。

## LLMプロバイダー

LiteLLMを通じて次のプロバイダーを利用できます。

- Amazon Bedrock: `bedrock/` prefixのモデル
- Anthropic Direct: `anthropic/` prefixのモデル
- Azure OpenAI: `azure/` prefixまたはAzure用の環境変数

実際に利用するモデルとdeployment名は、環境変数と `config/config.yml` の設定に合わせてください。API keyやAWS credentialをログ、仕様ファイル、fixtureへ書き込まないでください。

## 開発とテスト

依存関係を同期します。

```bash
uv sync --extra dev
```

全テスト:

```bash
uv run pytest -q
```

特定のテスト:

```bash
uv run pytest tests/test_spec_loader.py
uv run pytest tests/test_kintone_stub_server.py tests/test_kintone_mcp_e2e.py -q
```

カバレッジ:

```bash
uv run pytest --cov=src --cov-report=html
```

品質チェック:

```bash
uv run black --check src tests
uv run ruff check src tests
```

変更後は、テストと品質チェックが通過してからコミットしてください。実装を変更したときは、対応するテストも追加・更新してください。

## セキュリティ

- OpenAPI仕様、OCR JSON、mappingにAPI key、password、JWT、個人情報を入れない
- `.env`をコミットしない
- tokenは環境変数またはsecret managerから読み込む
- 生成されたserverの認証、入力検証、レート制限、redirect、ログをレビューする
- エラー、request history、評価結果にsecretが含まれないことを確認する
- LLMへ送る仕様に機密情報が含まれる場合は、送信先と保持ポリシーを確認する

脆弱性の報告方法は [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications) を参照してください。

## ロードマップ

大きなOpenAPI仕様に対して、LLMのcontext windowを超えないようpath単位で分割処理する機能を検討しています。

## コントリビューション

変更を提案する場合は [CONTRIBUTING.md](CONTRIBUTING.md) を確認してください。小さな修正でも、再現手順とテスト結果をPull Requestに記載してください。

## ライセンス

このプロジェクトはApache-2.0 Licenseです。詳細は [LICENSE](LICENSE) と [NOTICE](NOTICE) を参照してください。
