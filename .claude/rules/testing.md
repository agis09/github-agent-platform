# Testing

```bash
uv run pytest            # unit + contract（ネットワークアクセスなしで全て通ること）
uv run ruff check src tests
uv run mypy
```

## 分類

- **unit** — 状態遷移と冪等性、plan_hash の canonicalization、承認の期限と範囲、artifact 破損検知、ログ redaction、設定
- **contract** — JSON Schema 互換性、adapter / runtime 契約（fake バイナリで codex / claude を模擬）、CLI 一巡、Job テンプレートの描画、webhook

## 規約

- ネットワークアクセスなしで全テストが通ること。外部依存は fixture または fake 実行可能スクリプトで置換する
- 失敗を隠すテストを書かない。assert は具体的に（hash 一致、行数、exit code）
- canary 方式で secret 非出現を検証する（植えた secret がログ全文に 1 つも現れないことを assert）
- invalid fixture は pydantic と jsonschema の**両方**で拒否されること
- テストは順序に依存しない（共有 fixture は `tests/conftest.py`）
