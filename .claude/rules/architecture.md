# Architecture

## 分離境界

- `domain/` — 純粋なモデル・状態遷移・hash 計算。I/O を持たない
- `agent_runtime/` — エージェント CLI の差し替え点。`AgentRuntime` Protocol に従う（codex / claude-code）
- `worker/` — Job 内で動くワーカー。入力は環境変数、出力はログマーカー
- `adapters/agent/` — `AgentAdapter` Protocol（fake / local / remote）
- `adapters/artifacts/` — content-addressed 保存。DB には参照（digest）/ size / retention class のみ
- `adapters/kubernetes/` — `JobLauncher`。`TaskService` は Kubernetes を知らない
- `orchestrator/` — SQLite repository と TaskService（ユースケース層）
- `cli.py` — argparse のみ。フレームワークを持ち込まない

拡張点は Protocol（型）と JSON Schema（データ契約）で確保する。呼び出し元のないモジュール・未使用 Protocol を作らない。

## 冪等性の規約

- すべての状態遷移は `BEGIN IMMEDIATE` トランザクション内の CAS。同一状態への遷移は `ALREADY_APPLIED`
- `idempotency_keys` テーブルがコマンド → task の束縛を持つ
- 同一 `plan` 再実行は新規 task を作らず既存を返す（`duplicate: true`）
- 同一 `(task_id, plan_hash)` の二重承認は DB の UNIQUE 制約で拒否
- DB schema を変えたら `SCHEMA_VERSION` を上げる（旧 DB は明示的に拒否）

## plan_hash

- canonical JSON（sort_keys、区切り空白なし、UTF-8、allow_nan=False）の SHA-256
- `plan_hash` フィールド自身は除外。float は使わない
- artifact digest == plan_hash の hex という不変式を保つ
