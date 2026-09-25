# Kintone MCP モック検証結果

この文書は、OCR標準データの帳票を決定的に生成されたMCP server経由でローカルの
Kintone互換mockへまとめて転記した結果です。実Kintoneには接続していません。

## 1. 検証構成

| 項目 | 内容 |
| --- | --- |
| OCR入力 | [`examples/ocr/kintone-transfer.json`](../examples/ocr/kintone-transfer.json) |
| 生成MCP server | `results/azure/openapi/mcpserver/server.py` |
| MCP client | `results/azure/openapi/mcpserver/client.py` |
| 転記先mock | [`examples/kintone_stub_server.py`](../examples/kintone_stub_server.py) |
| MCP transport | `streamable-http` |
| 転記先API | `http://127.0.0.1:9100` |

mockのデータはプロセス内メモリに保存され、mockを終了すると破棄されます。

## 2. 転記先のKintoneアプリ定義

KintoneのレコードフィールドはOpenAPIで固定されず、アプリごとに異なります。mockは
`GET /k/v1/app/form/fields.json` で次の請求書アプリ定義を返します。

| フィールドコード | Kintone type | 必須/用途 |
| --- | --- | --- |
| `invoice_number` | `SINGLE_LINE_TEXT` | 必須・重複禁止・更新キー |
| `invoice_date` | `DATE` | 必須 |
| `vendor_name` | `SINGLE_LINE_TEXT` | 必須 |
| `subtotal` | `NUMBER` | 必須。`value`は文字列 |
| `tax_amount` | `NUMBER` | 必須。`value`は文字列 |
| `total_amount` | `NUMBER` | 必須。`value`は文字列 |
| `currency` | `DROP_DOWN` | 必須。`JPY`/`USD` |
| `status` | `DROP_DOWN` | 必須。`registered`/`needs_review`/`processed` |
| `ocr_confidence` | `NUMBER` | 0から1。`value`は文字列 |
| `source_file` | `SINGLE_LINE_TEXT` | OCR元ファイル名 |
| `ocr_text_ref` | `SINGLE_LINE_TEXT` | OCR本文のobject参照 |
| `line_items` | `SUBTABLE` | `description`、`quantity`、`unit_price`、`amount` |

`document_id` はOCR標準データの `document.document_id` であり、Kintone標準フィールド
でもmockアプリのカスタムフィールドでもありません。転記recordには含めず、帳票の
追跡と監査のために入力fixture側で保持します。Kintone側の重複排除・更新キーには
一意設定した `invoice_number` を使います。

## 3. 入力データ

正常データは帳票2枚です。各帳票を1件のKintone recordへ変換し、2件を同じ
`postRecords` の `records` 配列で送信します。

| OCR document_id | invoice_number | 合計 | 明細行 |
| --- | --- | ---: | ---: |
| `doc-invoice-0001` | `INV-0001` | `12800` JPY | 2 |
| `doc-invoice-0002` | `INV-0002` | `45000` JPY | 2 |

OCRの `raw_value` / `value` / `confidence` は転記マッピングで使い分けます。Kintoneの
NUMBERフィールドには正規化した数値を文字列で送り、OCRの明細行はKintoneの
SUBTABLE行へまとめます。

## 4. MCPへ送ったデータ

代表的な1件は次の形です。実際のE2Eでは、同じ形の2件を `records` 配列に入れます。

```json
{
  "body": {
    "app": 1,
    "records": [
      {
        "invoice_number": {"type": "SINGLE_LINE_TEXT", "value": "INV-0001"},
        "invoice_date": {"type": "DATE", "value": "2026-09-18"},
        "vendor_name": {"type": "SINGLE_LINE_TEXT", "value": "株式会社サンプル商事"},
        "subtotal": {"type": "NUMBER", "value": "11636"},
        "tax_amount": {"type": "NUMBER", "value": "1164"},
        "total_amount": {"type": "NUMBER", "value": "12800"},
        "currency": {"type": "DROP_DOWN", "value": "JPY"},
        "status": {"type": "DROP_DOWN", "value": "registered"},
        "ocr_confidence": {"type": "NUMBER", "value": "0.91"},
        "source_file": {"type": "SINGLE_LINE_TEXT", "value": "invoice-0001.pdf"},
        "ocr_text_ref": {"type": "SINGLE_LINE_TEXT", "value": "object://ocr-text/doc-invoice-0001"},
        "line_items": {
          "type": "SUBTABLE",
          "value": [
            {"value": {
              "description": {"type": "SINGLE_LINE_TEXT", "value": "クラウド利用料"},
              "quantity": {"type": "NUMBER", "value": "2"},
              "unit_price": {"type": "NUMBER", "value": "4000"},
              "amount": {"type": "NUMBER", "value": "8000"}
            }}
          ]
        }
      }
    ]
  }
}
```

`document_id` はこのrecordにありません。未知のフィールドコードを送った場合も、
Kintone仕様に合わせてmockはそのフィールドを無視します。

## 5. 正常系の実測結果

### フォーム定義 (`getAppFormFields`)

生成MCP serverからフォーム定義を取得でき、`document_id` が存在しないこと、
`line_items` が `SUBTABLE` であることを確認しました。

### 一括登録 (`postRecords`)

初期レコードID `1` があるため、帳票2件の登録結果は次の形になります。

```json
{
  "ids": ["2", "3"],
  "revisions": ["2", "3"]
}
```

取得結果には `$id`、`$revision`、定義済みの請求書フィールド、Kintoneが生成した
サブテーブル行IDが含まれます。`document_id` は保存されません。

### 一括更新 (`putRecords`)

`invoice_number` を正式なKintone `updateKey` として、2件の `status` を同じリクエストで
`processed` へ更新しました。

```json
{
  "records": [
    {"id": "2", "revision": "4"},
    {"id": "3", "revision": "5"}
  ]
}
```

送信した更新キーは次の形です。以前の `{"document_id": {"value": ...}}` 形式は
Kintoneの `RecordsPutUpdateKey` 契約ではありません。

```json
{"updateKey": {"field": "invoice_number", "value": "INV-0001"}}
```

### 削除 (`deleteRecords`)

登録したIDをまとめて削除すると、Kintone APIと同じく空オブジェクトを返します。

```json
{}
```

## 6. 異常データ

fixtureには正常な帳票とは別に、次の3ケースを含みます。各リクエストは一括登録全体を
拒否し、既存レコードを部分的に変更しません。

| case | 異常内容 | mock応答 |
| --- | --- | --- |
| `missing_invoice_number` | 必須かつ一意キーの請求書番号が空 | HTTP `400` / `MOCK_RE02` |
| `unsupported_status` | `status` が選択肢外 | HTTP `400` / `MOCK_RE03` |
| `confidence_out_of_range` | `ocr_confidence` が `1.2` | HTTP `400` / `MOCK_RE03` |

NUMBERの `value` にJSON数値を直接送るケースも拒否し、Kintone仕様どおり文字列を要求
します。

## 7. 認証情報と通信エラー

API tokenはmockのrequest historyに値を記録せず、headerの有無だけを保存します。
削除済みrecordの取得はHTTP `404`、到達不能な転記先は構造化されたconnection errorに
なり、tokenや機密本文をエラーへ含めません。

## 8. 再現コマンド

```bash
uv run pytest tests/test_kintone_stub_server.py tests/test_kintone_mcp_e2e.py -q
```

実測結果:

```text
13 passed, 1 warning
```

残るwarningはStarlette / AnyIO依存関係由来のものです。
