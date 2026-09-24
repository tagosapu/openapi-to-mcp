# Kintone MCP モック検証結果

この文書は、OCRデータを決定的に生成されたMCP server経由でローカルの
Kintone互換mockへ転記した結果です。値を追えるように、入力fixture、MCPへの
リクエスト、実際の応答、異常データの応答を掲載しています。

## 1. 検証構成

| 項目 | 内容 |
| --- | --- |
| OCR入力 | [`examples/ocr/kintone-transfer.json`](../examples/ocr/kintone-transfer.json) |
| 生成MCP server | `results/azure/openapi/mcpserver/server.py` |
| MCP client | `results/azure/openapi/mcpserver/client.py` |
| 転記先mock | [`examples/kintone_stub_server.py`](../examples/kintone_stub_server.py) |
| MCP transport | `streamable-http` |
| MCP endpoint | `http://127.0.0.1:9001/mcp/` |
| 転記先API | `http://127.0.0.1:9100` |

転記先データはmock processのメモリ上に保存されます。実Kintoneには接続せず、
mock processを終了するとデータは破棄されます。

## 2. 入力データ

### 正常データ3件

| document_id | textの内容 | status | confidence | 追加項目 |
| --- | --- | --- | ---: | --- |
| `ocr-e2e-001` | 英数字と改行を含む請求書テキスト | `registered` | `0.98` | `invoice-001.png` |
| `ocr-e2e-002` | 日本語と円記号を含む請求書テキスト | `registered` | `0.91` | `invoice-002.png` |
| `ocr-e2e-003` | 認識精度が低いテキスト | `needs_review` | `0.42` | 手動確認メモ |

fixtureの正常データは次の形です。

```json
{
  "app": 1,
  "records": [
    {
      "document_id": "ocr-e2e-001",
      "text": "OCR transfer text\nInvoice total: 12800",
      "status": "registered",
      "confidence": 0.98,
      "source_file": "invoice-001.png"
    },
    {
      "document_id": "ocr-e2e-002",
      "text": "請求書番号: INV-2026-002\n合計金額: 45000円",
      "status": "registered",
      "confidence": 0.91,
      "source_file": "invoice-002.png"
    },
    {
      "document_id": "ocr-e2e-003",
      "text": "Low-confidence OCR result",
      "status": "needs_review",
      "confidence": 0.42,
      "note": "手動確認が必要"
    }
  ]
}
```

### 異常データ3件

| case | 異常内容 | 期待するmock応答 |
| --- | --- | --- |
| `missing_document_id` | `document_id` が空文字 | HTTP `400` / `MOCK_RE02` |
| `unsupported_status` | `status` が `unknown` | HTTP `400` / `MOCK_RE03` |
| `confidence_out_of_range` | `confidence` が `1.2` | HTTP `400` / `MOCK_RE03` |

異常データとして実際に送信したrecordの値は次のとおりです。

```json
[
  {
    "case": "missing_document_id",
    "document_id": "",
    "text": "OCR text without document id",
    "status": "registered",
    "confidence": 0.88
  },
  {
    "case": "unsupported_status",
    "document_id": "ocr-invalid-status",
    "text": "OCR text with an unsupported status",
    "status": "unknown",
    "confidence": 0.77
  },
  {
    "case": "confidence_out_of_range",
    "document_id": "ocr-invalid-confidence",
    "text": "OCR text with an invalid confidence",
    "status": "registered",
    "confidence": 1.2
  }
]
```

異常データは正常3件とは別に1件ずつ送信します。1件でも不正ならそのリクエスト
全体を拒否し、正常に登録済みのレコードは残ります。

## 3. MCPへ送ったデータ

正常3件はKintoneのrecord field形式に変換して、`postRecords`へ送信します。
代表的なリクエストは次の形式です。

```json
{
  "body": {
    "app": 1,
    "records": [
      {
        "document_id": {
          "type": "SINGLE_LINE_TEXT",
          "value": "ocr-e2e-001"
        },
        "text": {
          "type": "MULTI_LINE_TEXT",
          "value": "OCR transfer text\nInvoice total: 12800"
        },
        "status": {
          "type": "DROP_DOWN",
          "value": "registered"
        },
        "confidence": {
          "type": "NUMBER",
          "value": 0.98
        },
        "source_file": {
          "type": "SINGLE_LINE_TEXT",
          "value": "invoice-001.png"
        }
      }
    ]
  }
}
```

E2Eではこの形式のrecordを3件まとめて送信しています。

## 4. 正常系の実際の出力

### 登録 (`postRecords`)

mockには初期レコードID `1` があるため、今回の実行では次の応答になります。

```json
{
  "ids": ["2", "3", "4"],
  "revisions": ["2", "3", "4"]
}
```

### 取得 (`getRecord`)

`ocr-e2e-001` の取得結果は次のようになります。`$id` と `$revision` はmockが
付与するシステムフィールドです。

```json
{
  "record": {
    "$id": {"type": "__ID__", "value": "2"},
    "$revision": {"type": "__REVISION__", "value": "2"},
    "document_id": {
      "type": "SINGLE_LINE_TEXT",
      "value": "ocr-e2e-001"
    },
    "text": {
      "type": "MULTI_LINE_TEXT",
      "value": "OCR transfer text\nInvoice total: 12800"
    },
    "status": {"type": "DROP_DOWN", "value": "registered"},
    "confidence": {"type": "NUMBER", "value": 0.98},
    "source_file": {
      "type": "SINGLE_LINE_TEXT",
      "value": "invoice-001.png"
    }
  }
}
```

### 更新 (`putRecord`)

`document_id = ocr-e2e-001` をキーに `status` を `processed` へ更新しました。

```json
{
  "revision": "5"
}
```

更新後の取得では次の値になります。

```json
{
  "status": {"value": "processed"}
}
```

### 削除 (`deleteRecords`)

ID `2`, `3`, `4` を削除し、空オブジェクトが返りました。

```json
{}
```

## 5. 異常データの実際の出力

異常データを `postRecords`へ送信すると、MCP clientは終了コード `1` で終了し、
次の構造化エラーを標準出力へ返します。

`missing_document_id`:

```json
{
  "error": {
    "kind": "http",
    "operation_id": "postRecords",
    "status_code": 400,
    "target_code": "MOCK_RE02",
    "message": "document_id is required"
  }
}
```

`unsupported_status`:

```json
{
  "error": {
    "kind": "http",
    "operation_id": "postRecords",
    "status_code": 400,
    "target_code": "MOCK_RE03",
    "message": "status must be one of: registered, needs_review, processed"
  }
}
```

`confidence_out_of_range`:

```json
{
  "error": {
    "kind": "http",
    "operation_id": "postRecords",
    "status_code": 400,
    "target_code": "MOCK_RE03",
    "message": "confidence must be between 0 and 1"
  }
}
```

## 6. 意図的な通信・HTTPエラー

### 削除済みレコードの取得

削除後にID `2` を取得すると、MCP clientの終了コードは `1` になり、次が返ります。

```json
{
  "error": {
    "kind": "http",
    "operation_id": "getRecord",
    "status_code": 404,
    "target_code": "GAIA_RE01",
    "message": "Record was not found"
  }
}
```

### 到達不能な転記先

未使用portを転記先に指定すると、次のエラーになります。

```json
{
  "error": {
    "kind": "connection",
    "operation_id": "getRecord",
    "message": "could not connect to target API"
  }
}
```

この場合もMCP clientの終了コードは `1` です。接続先のURL、token、リクエスト
本文はエラーメッセージに含めません。

## 7. 認証情報の秘匿

mockのrequest historyは、次のようにtoken headerの有無だけを記録します。

```json
{
  "method": "POST",
  "path": "/k/v1/records.json",
  "status_code": 200,
  "has_api_token": true
}
```

`KINTONE_API_TOKEN` の値そのものはhistoryにも構造化エラーにも出力されません。

## 8. 再現方法と結果

リポジトリのルートから、mockとE2Eを実行します。

```bash
uv run pytest tests/test_kintone_stub_server.py tests/test_kintone_mcp_e2e.py -q
```

実行結果:

```text
11 passed, 1 warning
```

Pydantic由来のdeprecation warningは発生しません。残る1件はStarlette/AnyIOの
依存関係にある `BlockingPortal` aliasのdeprecation warningです。
