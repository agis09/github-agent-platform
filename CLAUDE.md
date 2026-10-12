# github-agent-platform

GitHub Issue 対応 Agent 基盤（GitHub App + Kubernetes 上の Sandbox Job）。概要は `docs/overview.md`、設計は `docs/design.md`。

## コマンド

```bash
uv sync                  # 依存導入
uv run pytest            # unit + contract（ネットワーク不要）
uv run ruff check src tests
uv run mypy              # strict
```

## 運用ルール

- 失敗を隠す fallback を追加しない（欠落・破損・不一致は例外として表面化させる）
- secret、Issue 本文、ソースコード、LLM の入出力をログ・成果物・外部へ出さない
- 明示的な依頼なしに commit、push、PR 作成、GitHub / Kubernetes の設定変更をしない
- secret をリポジトリに置かない。ローカルでは repo 直下の `.env`（gitignore 済み、`.env.example` 参照）、デプロイでは Terraform 変数（`TF_VAR_*`）で渡す
- Kubernetes の設定（Secret 含む）は `infra/platform` の Terraform に入れる。個別の運用スクリプトは増やさない
- 変更後は pytest / ruff / mypy を通し、関連ドキュメント（`docs/`）を更新する

CLI の状態は `--home`（既定 `.agent-platform/`）配下の SQLite と artifact に保存される。このディレクトリは commit しない。

詳細は `.claude/rules/` の architecture.md、security.md、testing.md、kubernetes.md を参照。
