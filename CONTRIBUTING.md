# Contributing

## 開発環境

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run mypy
```

Terraform を変更した場合:

```bash
terraform fmt -recursive infra
(cd infra/platform && terraform init -backend=false && terraform validate)
```

## 方針

- テストはネットワークなしで通ること。外部サービスは fake で置換する（[.claude/rules/testing.md](./.claude/rules/testing.md)）
- 失敗を握りつぶす fallback を入れない
- secret を argv・ログ・prompt・成果物に出さない（canary テストで検証）
- 権限の境界（承認ゲート、credential 分離、NetworkPolicy）を弱める変更は、理由と代替の防御を PR に明記する
- 構造化出力のモデルを変えたら `schemas/` を再生成する（[docs/usage.md §12](./docs/usage.md#12-開発)）
- 新しいエージェント CLI・検索 API・クラウドの追加は [docs/extending.md](./docs/extending.md) に従う
- ドキュメント（`docs/`）も合わせて更新する

## Pull Request

- 1 つの PR は 1 つの目的に絞る
- 変更内容・テスト結果・残るリスクを記載する
