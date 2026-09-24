# サンプルAPIのテスト

このREADMEでは、付属の `sample_api.yaml` とAPIのバックエンドを提供するstub
serverを使って、OpenAPI to MCP converterを確認する方法を説明します。生成された
MCP serverとclientで一連の動作を検証できます。

## 事前準備

ルートのREADMEにあるquickstartを完了し、次の状態にしてください。

- プロジェクトの依存関係をインストール済み
- API認証情報を設定済み
- `sample_api.yaml` からMCP serverを生成済み

## サンプルAPIの実行

次のコマンドを別々の端末で実行します。

### 端末1: stub serverを起動

```bash
uv run python examples/stub_server.py
```

### 端末2: 認証tokenを設定してMCP serverを起動

```bash
export MISSION_AUTH_TOKEN=secret-token
uv run python results/anthropic/sample_api/mcpserver/server.py
```

### 端末3: MCP clientを起動

```bash
uv run python results/anthropic/sample_api/mcpserver/client.py
```

この確認では、stub serverがサンプルAPIの応答を返し、MCP serverがAPI endpointを
MCP toolとして公開し、MCP clientがtool一覧と連携結果を取得します。

## ローカルKintoneモックへの転記確認

`kintone_stub_server.py` は、メモリ上で動作するKintone互換バックエンドです。
API token認証、アプリ情報、レコードの一覧・取得・登録・更新・削除、長いquery向け
GET-over-POST、request historyを提供します。

`input_data/openapi-spec-1/openapi.yaml` から決定的に生成されたartifactは
`results/azure/openapi/mcpserver/` にあります。OpenAPI operationごとにMCP toolを
1つ公開し、デフォルトtransportは `streamable-http`、MCP endpointは `/mcp/` です。
リポジトリのルートから、3つの端末で実行してください。

### 端末1: モックを起動

```bash
uv run python examples/kintone_stub_server.py --port 9100 --token mock-token
```

### 端末2: 生成MCP serverを起動

```bash
API_BASE_URL=http://127.0.0.1:9100 \
KINTONE_API_TOKEN=mock-token \
uv run python results/azure/openapi/mcpserver/server.py \
  --port 9001 --transport streamable-http
```

### 端末3: `postRecords` toolを呼び出す

```bash
uv run python results/azure/openapi/mcpserver/client.py \
  --server-url http://127.0.0.1:9001/mcp/ \
  --tool postRecords \
  --arguments '{"body":{"app":1,"records":[{"document_id":{"type":"SINGLE_LINE_TEXT","value":"ocr-e2e-001"},"text":{"type":"MULTI_LINE_TEXT","value":"OCR transfer text\nInvoice total: 12800"},"status":{"type":"DROP_DOWN","value":"registered"},"confidence":{"type":"NUMBER","value":0.98},"source_file":{"type":"SINGLE_LINE_TEXT","value":"invoice-001.png"}}]}}'
```

送信する値は [`ocr/kintone-transfer.json`](ocr/kintone-transfer.json) にあります。
fixtureには正常データ3件と、必須ID欠損・許可外status・信頼度範囲外の異常データ3件を
含みます。`--tool` を省略するとtool一覧を取得できます。`API_BASE_URL` と
`KINTONE_API_TOKEN` は生成serverが読み込みます。モックのrequest historyにはtoken値を
記録せず、headerが存在したかどうかだけを記録します。

MCP tool数とOpenAPI operation数が一致し、`server.py` にfallback markerがなければ
完全生成物です。LLM生成失敗時のfallbackは `api_info` だけを公開し、モックAPIを
呼び出さないため、この確認には使用しません。request historyは
`GET http://127.0.0.1:9100/__mock/requests` で確認できます。

subprocessによる確認テストを実行します。

```bash
uv run pytest tests/test_kintone_mcp_e2e.py tests/test_generated_contract.py -q
```

正常転記、異常データ拒否、HTTP 404、接続失敗、認証情報の秘匿を含む実測結果は
[`docs/KINTONE_MCP_MOCK_RESULTS.md`](../docs/KINTONE_MCP_MOCK_RESULTS.md) を参照してください。