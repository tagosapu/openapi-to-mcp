# Task 5 Report

## Status

- Task: Task 5 永続ワーカーとエラー処理を実装する
- Date: 2026-09-18
- Result: Complete

## Changed Files

- src/transfer/worker.py
- src/transfer/store.py
- src/transfer/__init__.py
- tests/transfer/test_worker.py
- tests/transfer/test_store.py

## Implementation Summary

- Added TransferWorker with pinned connector and mapping version loading, validate/deliver claim handling, startup recovery, manual retry, cancel, review, and reconcile operations.
- Added RetryPolicy with the exact Task 5 defaults and Retry-After precedence capped by max_delay_seconds.
- Enforced no blind retry after possible send: not_sent plus explicitly retryable errors move to retrying, unknown moves to reconciliation_required, received success requires parse_response plus postcondition reconciliation == registered before succeeded.
- Applied review corrections on a deep copy of the request, preserved the original OCR payload, and used tenant-aware store APIs only.
- Extended SqliteTransferStore with worker-facing transition_state and atomic save_review_correction_and_transition helpers so result/error persistence can be decoupled from redacted audit-event detail.
- Updated restart recovery semantics so validating returns to accepted and delivering or cancellation_requested move to reconciliation_required.
- Exported RetryPolicy and TransferWorker from the transfer package public surface.

## RED

Command:

```bash
uv run pytest tests/transfer/test_worker.py -q
```

Output:

```text
ERROR collecting tests/transfer/test_worker.py
ModuleNotFoundError: No module named 'src.transfer.worker'
```

## GREEN

Focused worker command:

```bash
uv run pytest tests/transfer/test_worker.py -q
```

Focused worker output:

```text
23 passed, 2 warnings in 2.42s
```

Focused store regression after store changes:

```bash
uv run pytest tests/transfer/test_store.py -q
```

Focused store output:

```text
11 passed, 2 warnings in 1.71s
```

## Full Suite

Command:

```bash
uv run pytest -q
```

Output:

```text
126 passed, 2 warnings in 3.75s
```

## Test Coverage Added

- success path with validate then deliver and postcondition registration
- low confidence review routing with no HTTP call
- mapping operation mismatch with no HTTP call
- pinned connector and mapping version usage
- correction overlay on deep copy before delivery
- retry handling with Retry-After and max-attempt cutoff
- unknown delivery to reconciliation_required
- postcondition states registered, not_registered, unknown
- restart recovery for validating, delivering, cancellation_requested
- review approve, correct, reject
- cancel route plus before-send and in-flight cancellation outcomes
- manual reconcile states registered, not_registered, unknown
- redacted audit-event detail and encrypted correction persistence

## Self-Review

- The worker never loads latest connector or mapping versions for claimed records; it always uses the pinned versions on the transfer row.
- Delivery transitions persist result and error data separately from transfer_events detail, which keeps audit details limited to attempt, classification, duration_ms, and request_id.
- The correct review action is atomic at the store layer: encrypted correction insert, redacted review_correction event, and state transition share one SQLite transaction.
- Startup recovery behavior now matches the task brief and full regression remains green.

## Concerns

- review_transfer has no actor parameter in the required public interface, so review actor is currently recorded as system. If Task 6 needs end-user attribution, the caller contract will need an authenticated actor source without widening the current worker API.
- RetryPolicy.from_settings intentionally returns the task-mandated fixed defaults and does not consume settings.max_attempts. This matches the brief, but it overrides the broader repository settings shape.
- Existing test runs emit unrelated Pydantic deprecation warnings from json_encoders in dependencies and pre-existing code paths.

## Commit

- Commit SHA: dd5d3962ee93a2dcdb8b9fc164a4f4b25303007e

## Fix Round 2026-09-18

### Reviewer Findings Addressed

1. received の 429/5xx が retryable でも reconciliation_required に落ちていた問題を修正。
	Files: src/transfer/worker.py, tests/transfer/test_worker.py
	Change: retry 判定を delivery_state == not_sent 限定から、明示的な retryable HTTP status を持つ received にも拡張した。unknown は引き続き retry しない。

2. MappingEngine.apply、build_request、required header、credential resolution の送信前失敗が unknown 扱いになっていた問題を修正。
	Files: src/transfer/worker.py, src/transfer/store.py, tests/transfer/test_worker.py
	Change: 送信前例外を delivery preparation failure として別経路で処理し、reviewable な mapping validation は waiting_review、それ以外は failed に遷移させた。

3. 1 レコードの例外で persistent worker loop が停止する問題を修正。
	Files: src/transfer/worker.py, tests/transfer/test_worker.py
	Change: run loop で per-job 例外を捕捉し、parse_response failure は failed、reconcile failure は reconciliation_required に永続化してから次ジョブへ進むようにした。

4. retry / review 再アクティブ化で stale な result / error / completed_at が残る問題を修正。
	Files: src/transfer/store.py, src/transfer/worker.py, tests/transfer/test_store.py, tests/transfer/test_worker.py
	Change: transition_state と save_review_correction_and_transition に explicit clear フラグを追加し、manual retry と review approve/correct で stale terminal data を消去するようにした。

5. review audit actor が system 固定だった問題を修正。
	Files: src/transfer/worker.py, tests/transfer/test_worker.py
	Change: review_transfer に keyword-only の actor: str = "system" を追加し、review correction 保存と redacted review event の両方で使用するようにした。

### Regression Tests Added

- received 429 / 503 を RETRYING に戻す worker regression
- MappingEngine.apply の reviewable failure を WAITING_REVIEW に戻す worker regression
- build_request / required header / credential resolution の送信前失敗を FAILED にする worker regression
- parse_response failure 後も次ジョブを処理し続ける start loop regression
- manual retry と review reactivation で stale result / error / completed_at を clear する regression
- custom review actor の保存と redaction を確認する regression

### Commands And Outputs

Initial focused regression command:

```bash
uv run pytest tests/transfer/test_worker.py tests/transfer/test_store.py -q
```

Initial focused regression output:

```text
10 failed, 33 passed, 2 warnings in 5.00s
```

Focused verification command:

```bash
uv run pytest tests/transfer/test_worker.py tests/transfer/test_store.py -q
```

Focused verification output:

```text
43 passed, 2 warnings in 3.46s
```

Full suite command:

```bash
uv run pytest -q
```

Full suite output:

```text
135 passed, 2 warnings in 4.21s
```

### Self-Review

- retry 対象は RetryPolicy の既定 status 群に一致する明示的な HTTP response のみで、unknown と post-send ambiguity には拡張していない。
- pre-send failure と post-send failure を worker 内で分離したため、unknown は送信結果不明時だけに限定された。
- stale state clearing は implicit ではなく explicit API にしてあり、既存遷移の意味論を変えずに retry/review reactivation だけを安全に直している。
- review actor は既存の positional caller を壊さない keyword-only 追加で、保存データと監査イベントの双方に同じ値が入る。

### Concerns

- run loop の broad catch はワーカー停止を防ぐための最小修正で、予期しない store-level 例外の詳細ログはまだ持っていない。
- 既存の Pydantic deprecation warnings は今回も継続しているが、この修正スコープの外と判断した。

## Fix Round 2026-09-18 Hot Loop Backoff

### Reviewer Finding Addressed

1. _run_loop() が run_once() の予期しない例外後に即 continue し、永続例外で CPU hot loop を起こす問題を修正。
	Files: src/transfer/worker.py, tests/transfer/test_worker.py
	Change: 例外経路でも poll interval ベースの awaitable wait を通す _wait_for_poll_interval() を追加し、blocking sleep を使わず stop() に即応できる backoff を入れた。成功ジョブ継続や idle poll の既存挙動は維持した。

### Regression Tests Added

- run_once() が 1 回例外を投げても start loop が wait を挟んで再試行し、次イテレーションで回復できる回帰テスト

### Commands And Outputs

Focused regression command:

```bash
uv run pytest tests/transfer/test_worker.py tests/transfer/test_store.py -q
```

Focused regression output:

```text
44 passed, 2 warnings in 4.22s
```

Full suite command:

```bash
uv run pytest -q
```

Full suite output:

```text
136 passed, 2 warnings in 4.34s
```

### Self-Review

- 例外 backoff は _stop_event.wait() を timeout 付きで await する形なので、sleep ベースの非応答は入っていない。
- wait の共通化により、idle poll と exception retry で同じ停止応答性を保っている。
- 既存の per-job failure persistence や retry/error classification、state clearing、actor まわりの挙動は変更していない。

### Concerns

- 予期しない例外の詳細ログは依然としてなく、今回は CPU hot loop 防止に限定した。
- 既存の Pydantic deprecation warnings は継続しているが、この修正スコープ外と判断した。