# github-agent-platform

GitHub の Issue を入力に、**AI コーディングエージェントが計画 → 人が承認 → 使い捨て Sandbox で実装・テスト → Draft PR 作成 → 独立レビュー** までを行う、GitHub App とその実行基盤です。

利用者はこのリポジトリをクローンし、設定（Terraform 変数 / 環境変数）で **AI エージェント・LLM・Web 検索 API・Kubernetes クラスタ** を選んで、自分の App として動かします。

```
Issue ──▶ Plan Job ──▶ 人の承認 ──▶ Execute Job ──▶ Draft PR ──▶ Review Job
        (計画のみ)     (Issue コメント)  (使い捨て Sandbox)  (LLM なしで作成)  (独立レビュー → check run)
```

## 特徴

- **承認ゲートが絶対**: エージェントが自律的に行えるのは計画の生成まで。コード変更は、人が `plan_hash` を承認した後だけ
- **エージェントに書き込み権限を渡さない**: Sandbox は GitHub credential を持たず、通信先は LLM エンドポイントと DNS のみ（既定）。PR は LLM を実行しない Publisher が作成
- **逸脱検知**: 承認範囲外のファイル変更・巨大 patch・バイナリ / symlink / submodule は公開しない
- **差し替え可能**

  | 項目 | 選択肢 |
  |---|---|
  | AI エージェント | Codex CLI / Claude Code |
  | LLM | さくらのクラウド AI Engine、OpenAI、Anthropic、互換エンドポイント（vLLM、LiteLLM など） |
  | Web 検索 | 無効（既定）/ Brave Search / 独自 HTTP API |
  | Sandbox の通信 | なし（既定）/ 許可リスト / HTTPS 全体 |
  | クラスタ | 任意の Kubernetes（構築例: kind、さくらのクラウド k3s） |
  | GitHub | github.com / GitHub Enterprise Server |
  | 成果物ストア | PVC / S3 互換 |

- **監査可能**: 計画・実行結果・patch・レビュー指摘を content-addressed artifact として保存し、全判断を監査ログから復元可能

## 使い方（Issue コメント）

| コメント | 動作 |
|---|---|
| `/agent-platform run` | 計画を作成して Issue にコメント |
| `/agent-platform answer <回答>` | 計画からの質問に回答し、計画を作り直す |
| `/agent-platform approve` | 最新の計画を承認 → 実装・Draft PR・レビューを自動実行 |
| `/agent-platform redo [task-id]` | やり直し（本文は追加指示になる） |

起動・承認できるのはリポジトリの書き込み権限者だけです。

## クイックスタート

```bash
git clone <this-repo> github-agent-platform && cd github-agent-platform
uv sync && uv run pytest                        # オフラインで全テストが通る

# 1. GitHub App を作成（docs/usage.md §3）
# 2. イメージをビルドして push
REGISTRY=ghcr.io/<you> scripts/build-push.sh
# 3. デプロイ（任意の Kubernetes クラスタ）
cd infra/platform && cp terraform.tfvars.example terraform.tfvars && $EDITOR terraform.tfvars
cp ../../.env.example ../../.env && $EDITOR ../../.env   # API キー等の secret（例: さくらの AI Engine のトークン）
set -a; . ../../.env; set +a
terraform init && terraform apply
```

クラスタなしで流れだけ試す場合は [docs/usage.md §1](./docs/usage.md#1-まずローカルで試すクラスタ不要) を参照してください。

## ドキュメント

| 文書 | 内容 |
|---|---|
| [docs/overview.md](./docs/overview.md) | 概要 — 目的・構成要素・人の操作 |
| [docs/design.md](./docs/design.md) | 詳細設計 — アーキテクチャ、状態機械、承認ゲート、エージェントランタイム、Sandbox、ネットワーク、検索、セキュリティモデル、IaC、既知の制約 |
| [docs/usage.md](./docs/usage.md) | 利用方法 — GitHub App 作成、エージェント選択、ビルド、デプロイ、運用、トラブルシューティング |
| [docs/configuration.md](./docs/configuration.md) | 設定リファレンス — Terraform 変数と環境変数 |
| [docs/extending.md](./docs/extending.md) | 拡張ガイド — エージェント CLI・検索 API・クラウドの追加 |
| [infra/](./infra/) | Terraform（本体 + クラスタ構築例） |

## リポジトリ構成

```
src/agent_platform/   # Orchestrator・ワーカー・アダプタ（Python 3.12、依存は pydantic / pyjwt / cryptography のみ）
deploy/               # Job テンプレートと Dockerfile
schemas/              # 構造化出力の JSON Schema（pydantic モデルから生成）
infra/platform/       # 本体のデプロイ（クラウド非依存）
infra/clusters/       # クラスタ構築例（kind、さくらのクラウド k3s）
scripts/              # build-push / create-snapshot / smoke
tests/                # unit + contract（ネットワーク不要）
```

## 開発

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run mypy
```

コントリビュートは [CONTRIBUTING.md](./CONTRIBUTING.md)、脆弱性の報告は [SECURITY.md](./SECURITY.md) を参照してください。

## ライセンス

TBD
