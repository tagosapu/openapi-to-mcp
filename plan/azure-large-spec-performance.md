# Azure 大規模 OpenAPI 評価の性能維持計画

## 目的

Azure OpenAI を使った通常サイズの評価性能を維持したまま、大規模な OpenAPI 仕様も安定して評価できるようにする。

## 現状の根拠

- `aoai_run.log` の大規模リクエストは約 1,121,979 文字、推定 280,494 prompt tokens。
- `gpt-5.4` への送信後 94.9 秒で `APIConnectionError` と `Connection reset by peer` が発生した。
- スタックトレースには `http_proxy.py` が含まれるため、HTTP プロキシまたは Azure 側の大規模本文処理が切断要因と考えられる。
- 同一の Azure 設定による短い `generate_text` は HTTP 200、2.4 秒で成功した。
- 約 60k token 相当の合成 prompt も HTTP 200、実測 52,506 prompt tokens、5.1 秒で成功した。
- 現在の回帰テストは 7 件すべて成功している。

## 実データ検証結果

- Kintone REST API（128 paths、204 operations、481 schemas）は 6 chunks に分割された。
- compact chunk prompt の最大実測入力は約 61,854 tokens で、64k の chunk 上限内だった。
- 6 requests はすべて HTTP 200、`finish_reason=stop` で完了した。
- 合計 usage は prompt 322,280 tokens、completion 16,479 tokens、total 338,759 tokens、約 0.951221 USD だった。
- LLM が返さなかった schema は元仕様から deterministic fallback を生成し、最終結果で 204 operations / 481 schemas を保持する。
- LLM が 1-5 外の overall score を返した場合は reducer で 1-5 に clamp する。
- chunk の 408、409、429、5xx、接続エラー、timeout は最大 2 回まで指数 backoff + jitter で再試行する。JSON/validation エラーは再試行しない。

## 設計方針

### 1. 小さい仕様は単一リクエストのままにする

推定 prompt tokens が設定した閾値以下の場合は、現在の評価テンプレートと単一の LLM 呼び出しをそのまま使う。通常ケースに分割処理、追加の統合呼び出し、不要なシリアライズコストを持ち込まない。

### 2. 大きい仕様だけ適応的に分割する

文字列を任意の位置で切断せず、OpenAPI の構造を使って分割する。

- 仕様を一度だけ YAML/JSON として解析する。
- `/paths` の operation を基本単位にする。
- 各 operation が参照する `$ref` の依存関係を解決し、必要な schema、parameter、response を同じチャンクへ含める。
- API metadata、security scheme、共通ルールは各チャンクへ必要最小限だけ含める。
- 多数の operation から共有される schema は全チャンクへ全文複製せず、別評価またはコンパクトな schema index を利用する。
- チャンク境界は推定トークン数で決定し、completion 用の余裕を予約する。

### 3. Map-Reduce で結果を統合する

- Map: チャンクごとに operation、parameter、response、schema の評価を取得する。
- Reduce: operation と schema の結果をローカルで決定的に統合する。
- 全体スコア、改善優先度、件数はローカル集計を基本にする。
- 自然言語の全体要約が必要な場合だけ、元仕様を含まない小さな要約リクエストを 1 回追加する。
- 既存の単一リクエスト用出力形式は維持し、チャンク結果から同じ `OpenAPIEvaluationResult` を生成する。

## 実施フェーズ

### Phase 0: 通信経路の A/B 確認

1. 現在の `NO_PROXY` 削除を無条件動作にせず、Azure の proxy 使用方針を設定可能にする。
2. 同じ SDK と deployment を使い、8k、32k、64k、128k tokens 相当の合成 prompt を段階送信する。
3. 各試行で HTTP status、経過時間、推定入力 tokens、例外種別だけを記録する。prompt、completion、資格情報は記録しない。
4. 直結経路で大きな prompt が安定するなら、大規模入力では直結経路を優先する。
5. 直結でも一定サイズ以上で失敗する場合は Phase 1 以降の分割方式へ進む。同じ 280k tokens の再送は、閾値確認後まで行わない。

### Phase 1: 高速経路とサイズ判定

1. `azure_max_single_prompt_tokens` を設定として追加する。
2. prompt 作成後、送信前に推定 tokens を計算する。
3. 閾値以下は従来経路を使用する。
4. 閾値超過時はチャンク評価へ切り替え、サイズ超過を接続リセットとして待ち続けない。
5. 設定値は deployment と proxy の実測結果から変更できるようにする。

### Phase 2: 構造化チャンクプランナー

1. `OpenAPIEnhancer` からチャンク作成処理を分離する。
2. operation ごとの `$ref` 依存 closure を構築する。
3. `azure_chunk_prompt_tokens` を上限として、関連 operation を deterministic に束ねる。
4. 1 operation だけでも上限を超える場合は、operation の description、schema、response 評価を段階分割できるフォールバックを用意する。
5. チャンク ID、含まれる paths、依存 schema 名、推定 tokens を実行ログへ記録する。

### Phase 3: 並列評価と統合

1. チャンク評価用テンプレートを追加し、各チャンクが全体評価の JSON を要求しないようにする。
2. `asyncio.Semaphore` で `azure_max_concurrency` を制限する。
3. 推定 tokens を使った簡易 TPM/RPM レート制御を追加する。
4. 408、409、429、5xx、接続エラー、timeout は指数バックオフと jitter で最大 2 回再試行する。JSON/validation エラーは再試行しない。
5. connection reset、413、context limit は同じサイズで再送せず、チャンクを半分に縮小して再試行する。
6. すべてのチャンク完了後、重複 schema を名前で統合し、operation の元順序を復元する。

### Phase 4: 再開性と運用性

1. spec hash、prompt template version、model/deployment、chunk ID をキーにしたチャンク結果キャッシュを追加する。
2. 一部チャンク失敗時は完了済みチャンクを再利用する。
3. ログには request ID、chunk ID、token 推定値、経過時間、status、retry 回数のみを残す。
4. 大規模評価の途中結果を一時ファイルへ保存し、プロセス終了後も再開可能にする。

## 設定候補

```yaml
azure_max_single_prompt_tokens: 60000
azure_chunk_prompt_tokens: 64000
azure_chunk_max_tokens: 16384
azure_max_concurrency: 3
azure_proxy_mode: auto
azure_chunk_retry_limit: 2
azure_chunk_cache_dir: ./results/.cache
```

現時点の初期値は Phase 0 の 60k token canary をもとに設定している。Azure deployment の TPM/RPM 制限と実データの結果を見て調整する。

## 出力先ポリシー

- 診断ログは `./logs/aoai_run.log` に保存する。
- LLM 応答などのデバッグ用成果物は `./results/runtime/` に保存する。
- アプリケーションコードと検証コードで OS の既定 temporary directory を出力先にしない。
- CLI 実行ログを保存する場合も、`logs/` などワークスペース内のパスを使用する。

## テスト計画

- prompt token 推定と閾値判定の単体テスト
- `$ref` closure が必要な schema だけを含むことのテスト
- operation 順序、重複 schema、missing chunk の統合テスト
- 1 operation が上限を超える場合の縮小フォールバックテスト
- semaphore と retry/backoff のモックテスト
- 小さい仕様が従来どおり 1 回の LLM 呼び出しになる回帰テスト
- 128 paths、481 schemas 相当の synthetic fixture によるチャンク数・token 量テスト
- Azure canary で小・中・大 prompt の成功率、レイテンシ、入力 tokens、コストを比較
- 最後に対象の大規模仕様を実行し、`results/` 生成と全 operation/schema 件数を確認

## 成功条件

- 小さい仕様の呼び出し回数と出力形式が変わらない。
- 大規模仕様が接続リセットなしで完了し、全 paths と schemas が結果に反映される。
- 同じ大規模チャンクを無制限に並列送信せず、TPM/RPM 制限内で動作する。
- 共有 schema の重複送信を抑え、分割による入力 token 増加を測定・管理できる。
- 途中失敗後に全体を最初から再実行せず再開できる。

## 実装順序

1. Phase 0 の proxy A/B とサイズ閾値計測
2. 高速経路を維持したサイズ判定
3. 構造化チャンクプランナーと専用モデル出力
4. 並列実行とローカル統合
5. 再試行、キャッシュ、再開性
6. 実データ検証と運用結果の反映
