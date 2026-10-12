# 利用方法

このリポジトリをクローンし、自分の GitHub App・LLM・クラスタを設定して動かすまでの手順です。設計の背景は [design.md](./design.md)、全設定項目は [configuration.md](./configuration.md) を参照してください。

## 目次

1. [まずローカルで試す（クラスタ不要）](#1-まずローカルで試すクラスタ不要)
2. [必要なもの](#2-必要なもの)
   - [2.1 設定の置き場所と .env](#21-設定の置き場所と-env)
3. [GitHub App を作る](#3-github-app-を作る)
   - [3.1 控えた値の設定先](#31-控えた値の設定先)
4. [AI エージェントと LLM を選ぶ](#4-ai-エージェントと-llm-を選ぶ)
5. [Web 検索を設定する（任意）](#5-web-検索を設定する任意)
6. [イメージをビルドする](#6-イメージをビルドする)
7. [Kubernetes クラスタを用意する](#7-kubernetes-クラスタを用意する)
8. [プラットフォームをデプロイする](#8-プラットフォームをデプロイする)
9. [Issue から使う](#9-issue-から使う)
10. [運用](#10-運用)
11. [トラブルシューティング](#11-トラブルシューティング)
12. [開発](#12-開発)

---

## 1. まずローカルで試す（クラスタ不要）

fake アダプタで状態機械と承認ゲートの流れを確認できます（ネットワーク不要）。

```bash
git clone <this-repo> github-agent-platform && cd github-agent-platform
uv sync
uv run pytest                      # 全テストがオフラインで通る

export AGENT_PLATFORM_ADAPTER=fake AGENT_PLATFORM_HOME=/tmp/agent-platform-demo
uv run agentctl plan --repo octo/demo --issue 1 --base-sha $(printf 'a%.0s' {1..40})
# → task_id と plan_hash が表示される
uv run agentctl approve --task-id <task_id> --plan-hash <plan_hash> --approver alice
uv run agentctl execute --task-id <task_id> --plan-hash <plan_hash>
uv run agentctl status  --task-id <task_id>
```

本物のエージェントで **計画だけ** をローカルで試すこともできます。エージェント CLI（`codex` または `claude`）がローカルにインストール済みで、GitHub App（[3 章](#3-github-app-を作る)）を作成済みであることが前提です。設定は `.env` に書いて読み込みます（[2.1](#21-設定の置き場所と-env)）。以下はさくらのクラウド AI Engine を使う例です。

```bash
cp .env.example .env && chmod 600 .env
$EDITOR .env
#   AGENT_PLATFORM_ADAPTER=local
#   AGENT_PLATFORM_AGENT_RUNTIME=codex
#   AGENT_PLATFORM_AGENT_BASE_URL=https://api.ai.sakura.ad.jp/v1
#   AGENT_PLATFORM_AGENT_MODEL=gpt-oss-120b
#   LLM_API_KEY=<UUID>:<シークレット>            # AI Engine のアカウントトークン
#   AGENT_PLATFORM_GITHUB_API_URL / _APP_ID / _INSTALLATION_ID / _PRIVATE_KEY_PATH（3.1 の対応表）
set -a; . ./.env; set +a
uv run agentctl plan --repo <owner>/<repo> --issue <番号>
```

実装（execute）とレビューは、隔離のため Kubernetes の Sandbox でのみ動きます。

## 2. 必要なもの

| 項目 | 用途 |
|---|---|
| GitHub（github.com または GitHub Enterprise Server） | GitHub App を 1〜2 個作成できる権限 |
| Kubernetes クラスタ | NetworkPolicy を強制する CNI、Pod Security Admission、既定の StorageClass |
| LLM の API | さくらのクラウド AI Engine、OpenAI / Anthropic、または互換エンドポイント（vLLM、LiteLLM など） |
| コンテナレジストリ | Worker イメージの保管（ghcr.io、ECR、GAR、さくらのクラウド コンテナレジストリなど） |
| 公開 URL | GitHub から webhook を受けるための入口（Ingress、LoadBalancer、トンネルなど） |
| ツール | Python 3.12+ / [uv](https://docs.astral.sh/uv/) / docker（buildx）/ kubectl / Terraform 1.9+ |

### 2.1 設定の置き場所と .env

設定値は「どこで動かすか」によって置き場所が変わります。secret（API キー・webhook secret・パスワード）は、どちらの場合もリポジトリ直下の `.env` にだけ書きます。

| 動かす場所 | secret | secret 以外 | 読み込み方 |
|---|---|---|---|
| ローカル（`agentctl` を手元で実行） | `.env` の `LLM_API_KEY` など | `.env` の `AGENT_PLATFORM_*` | `set -a; . ./.env; set +a` |
| クラウド（Kubernetes にデプロイ） | `.env` の `TF_VAR_agent_api_key` など | `infra/platform/terraform.tfvars` | `.env` を読み込んだシェルで `terraform apply` |

`.env` の作り方:

```bash
cp .env.example .env      # .env は gitignore 済み。.env.example にはさくらの AI Engine を先頭に各 LLM の例がある
chmod 600 .env
$EDITOR .env
set -a; . ./.env; set +a  # 現在のシェルに export する
```

- `.env` を自動で読むツールはありません。`terraform` や `agentctl` を実行するシェルで毎回読み込んでください。
- Terraform は `TF_VAR_<変数名>` という環境変数を、同じ名前の Terraform 変数として扱います（例: `TF_VAR_agent_api_key` → `agent_api_key`）。
- クラウドにデプロイした場合、アプリケーションの環境変数（`AGENT_PLATFORM_*`）は Terraform が Kubernetes の Secret に書き込み、Orchestrator と Job に渡します。利用者が直接設定する必要はありません。
- 変数の一覧と、Terraform 変数と環境変数の対応は [configuration.md](./configuration.md) を参照してください。

## 3. GitHub App を作る

**Reader App**（読み取り専用）と **Publisher App**（書き込み）を分けて作ることを推奨します。1 つの App で済ませることもできます（その場合は Publisher の権限を付けた App を Reader としても使います）。

Settings → Developer settings → GitHub Apps → **New GitHub App**

| 権限 | Reader App | Publisher App |
|---|---|---|
| Repository: Contents | Read | Read and write |
| Repository: Issues | Read | Read and write |
| Repository: Pull requests | Read | Read and write |
| Repository: Checks | — | Read and write |
| Repository: Metadata | Read | Read |

**Webhook**（Issue コメントで起動するため、**Reader App 側**で設定）

- Active: ON
- Webhook URL: デプロイ後に決まる公開 URL（[8.3](#83-webhook-を公開する)）。パスは任意（全 POST を受け付けます）
- Webhook secret: ランダムな文字列（例: `openssl rand -hex 32`）。Terraform の `webhook_secret` に同じ値を設定
- Subscribe to events: **Issue comment**

作成後:

1. **App ID** を控える
2. **Generate a private key** で `.pem` をダウンロードし、リポジトリ外に置く
   ```bash
   mkdir -p ~/.config/agent-platform && chmod 700 ~/.config/agent-platform
   mv ~/Downloads/<reader-app>.*.pem    ~/.config/agent-platform/reader.pem
   mv ~/Downloads/<publisher-app>.*.pem ~/.config/agent-platform/publisher.pem
   chmod 600 ~/.config/agent-platform/*.pem
   ```
3. **Install App** で対象リポジトリにだけインストールし、**Installation ID** を控える。インストール後に開く設定ページの URL 末尾の数字です（個人: `https://github.com/settings/installations/<ID>`、Organization: `https://github.com/organizations/<org>/settings/installations/<ID>`）

### 3.1 控えた値の設定先

控えた値と秘密鍵のパスは、次のように設定します（[2.1](#21-設定の置き場所と-env) の置き場所の区分に従います）。

| 値 | クラウド（`infra/platform/terraform.tfvars`） | ローカル（`.env`） |
|---|---|---|
| GitHub の API URL | `github_api_url`（github.com なら既定のままでよい。GHES は `https://<host>/api/v3`） | `AGENT_PLATFORM_GITHUB_API_URL` |
| Reader App の App ID | `reader_app_id` | `AGENT_PLATFORM_GITHUB_APP_ID` |
| Reader App の Installation ID | `reader_installation_id` | `AGENT_PLATFORM_GITHUB_INSTALLATION_ID` |
| Reader App の秘密鍵 | `reader_private_key_path = "~/.config/agent-platform/reader.pem"` | `AGENT_PLATFORM_GITHUB_PRIVATE_KEY_PATH`（絶対パス） |
| Publisher App の App ID / Installation ID / 秘密鍵 | `publisher_app_id` / `publisher_installation_id` / `publisher_private_key_path`（1 App 構成なら空のまま） | `AGENT_PLATFORM_PUBLISHER_API_URL` / `_APP_ID` / `_INSTALLATION_ID` / `_PRIVATE_KEY_PATH`（publish もローカルで試す場合のみ） |
| Webhook secret | `.env` の `TF_VAR_webhook_secret`（secret なので tfvars には書かない） | `AGENT_PLATFORM_WEBHOOK_SECRET`（`agentctl webhook serve` をローカルで動かす場合のみ） |

```hcl
# infra/platform/terraform.tfvars（例）
github_api_url             = "https://api.github.com"
reader_app_id              = "123456"
reader_installation_id     = "12345678"
reader_private_key_path    = "~/.config/agent-platform/reader.pem"
publisher_app_id           = "234567"
publisher_installation_id  = "23456789"
publisher_private_key_path = "~/.config/agent-platform/publisher.pem"
```

- App ID と Installation ID は秘密情報ではありませんが、`terraform.tfvars` は gitignore 済みなのでリポジトリには入りません。
- 秘密鍵は `terraform apply` を実行するマシン上のパスを指定します。Terraform が鍵の内容を Secret `github-app-keys` に入れ、Orchestrator に読み取り専用でマウントします。Sandbox の Job には渡りません。
- Webhook URL は、デプロイ後に [8.3](#83-webhook-を公開する) で決まった URL を、GitHub App の設定画面（General → Webhook URL）に戻って入力します。

> 1 つのリポジトリに、このプラットフォームの App を複数インストールしないでください。同じコメントに複数のデプロイが反応します。

## 4. AI エージェントと LLM を選ぶ

「どの CLI（エージェント）で」「どの LLM エンドポイントを」使うかを選びます。

| 使う LLM | `agent_runtime` | `agent_base_url` | `agent_model` の例 | 備考 |
|---|---|---|---|---|
| **さくらのクラウド AI Engine**（OpenAI 互換） | `codex` | `https://api.ai.sakura.ad.jp/v1` | `gpt-oss-120b` | 推奨の最初の構成。Responses API を使用 |
| さくらのクラウド AI Engine（Anthropic 互換） | `claude-code` | `https://api.ai.sakura.ad.jp` | `gpt-oss-120b` | `agent_auth_scheme = "bearer"` が必要。イメージに Claude Code を入れる |
| OpenAI API | `codex` | 空 | `gpt-5-codex` | |
| Anthropic API | `claude-code` | 空 | `claude-sonnet-5-5` など | イメージに Claude Code を入れる |
| 自前の vLLM / LiteLLM（OpenAI Responses 互換） | `codex` | `https://llm.example.com/v1` | エンドポイントのモデル名 | `GET /v1/models` で確認 |
| Anthropic 互換ゲートウェイ | `claude-code` | `https://gateway.example.com` | ゲートウェイのモデル名 | 認証方式に合わせて `agent_auth_scheme` |


- Claude Code を使う場合は、イメージのビルド時に `INSTALL_CLAUDE_CODE=true CLAUDE_CODE_VERSION=<版>` が必要です（[6 章](#6-イメージをビルドする)）。
- エージェントの実行時間の上限は `agent_timeout_seconds`（既定 3600 秒）です。Job の期限はこれ + 300 秒になります。

### 4.1 設定する場所

**クラウド（Kubernetes）にデプロイする場合** — `infra/platform/terraform.tfvars`（secret 以外）と `.env`（API キー）に書きます。Terraform が API キーを Secret `llm-api-key` に保存し、Orchestrator がエージェントの Job を起動するときに `LLM_API_KEY` として注入します。Orchestrator 自身はキーを読みません。

```hcl
# infra/platform/terraform.tfvars — さくらの AI Engine + Codex CLI
agent_runtime  = "codex"
agent_base_url = "https://api.ai.sakura.ad.jp/v1"
agent_model    = "gpt-oss-120b"
# llm_egress_cidrs = []     # 空 = エージェント Job は公開アドレスの 443 に出られる
```

```bash
# .env
TF_VAR_agent_api_key=<UUID>:<シークレット>
```

Claude Code で使う場合は `agent_runtime = "claude-code"`、`agent_base_url = "https://api.ai.sakura.ad.jp"`、`agent_auth_scheme = "bearer"` にします。設定を変えたら `terraform apply` で反映します（次の Job から有効）。

**ローカルで実行する場合** — すべて `.env` に環境変数として書きます。

```bash
# .env
AGENT_PLATFORM_AGENT_RUNTIME=codex
AGENT_PLATFORM_AGENT_BASE_URL=https://api.ai.sakura.ad.jp/v1
AGENT_PLATFORM_AGENT_MODEL=gpt-oss-120b
LLM_API_KEY=<UUID>:<シークレット>
```

Terraform 変数とローカル環境変数の対応:

| Terraform 変数（クラウド） | 環境変数（ローカル） |
|---|---|
| `agent_runtime` | `AGENT_PLATFORM_AGENT_RUNTIME` |
| `agent_base_url` | `AGENT_PLATFORM_AGENT_BASE_URL` |
| `agent_model` | `AGENT_PLATFORM_AGENT_MODEL` |
| `agent_auth_scheme` | `AGENT_PLATFORM_AGENT_AUTH_SCHEME` |
| `agent_api_key`（`.env` の `TF_VAR_agent_api_key`） | `LLM_API_KEY`（変数名は `AGENT_PLATFORM_AGENT_API_KEY_ENV` で変更可） |
| `agent_timeout_seconds` | `AGENT_PLATFORM_AGENT_TIMEOUT_SECONDS` |

全項目は [configuration.md の「エージェント・LLM」](./configuration.md#エージェントllm) と [「エージェントランタイム」](./configuration.md#エージェントランタイム) を参照してください。

### 4.2 LLM への通信の許可

エージェントの Job は default-deny の NetworkPolicy の下で動き、LLM エンドポイントへの通信だけが許可されます。

- `llm_egress_cidrs` が空（既定）の場合、Job は公開アドレスの 443 全体に出られます。AI Engine や OpenAI / Anthropic などの SaaS はアドレスが変わることがあるため、通常はこれを使います。
- 自前の LLM エンドポイントのようにアドレスが固定されている場合は、`llm_egress_cidrs = ["203.0.113.10/32"]` のように絞ってください。

## 5. Web 検索を設定する（任意）

Execute 中のエージェントが使える Web 検索です。**既定は無効**（`search_provider = "none"`）で、デプロイ単位で検索 API を選んだときだけ有効になります。Issue や計画から有効にすることはできません。仕組みは [design.md §10](./design.md#10-web-検索) を参照してください。

> 有効にすると、**LLM が生成した検索クエリが外部の検索 API に送られます**。Issue 由来の識別子・secret の形をした文字列・コード行はサニタイザが送信前に拒否しますが、有効化は組織の外部送信ポリシーを確認してから行ってください。

### 選べるプロバイダ

| `search_provider` | 動作 | 必要な設定 |
|---|---|---|
| `none`（既定） | 検索無効。Search Gateway をデプロイせず、prompt にも検索の案内を出さない | なし |
| `fake` | 固定の 1 件を返す。外部に通信しない（配線の確認用） | なし |
| `brave` | [Brave Search API](https://brave.com/search/api/)（Web 検索、上位 5 件） | `search_api_key` 必須 |
| `http-json` | 独自の検索 API（下記の形式に従うもの） | `search_provider_url` 必須、`search_api_key` 任意 |

`http-json` の API が実装すべき形式:

```
GET <search_provider_url>?q=<query>
Authorization: Bearer <search_api_key>      # search_api_key を設定したときだけ
Accept: application/json
→ 200 {"results": [{"title": "...", "url": "...", "snippet": "..."}]}
```

https のみ、応答は 1MiB まで、`search_provider_url` のホスト以外には接続しません。これ以外の API を使いたい場合はプロバイダを追加します（[extending.md](./extending.md#web-検索-api-を追加する)）。

### 設定する場所

| 値 | 置き場所 |
|---|---|
| `search_provider` / `search_provider_url` | `infra/platform/terraform.tfvars` |
| `search_api_key` | `.env` の `TF_VAR_search_api_key`（secret なので tfvars には書かない） |

API キーは Secret `search-api-key` に保存され、**Search Gateway の Pod にだけ** 渡ります。Orchestrator や Execute Job は持ちません。

### 手順（Brave Search の例）

デプロイ前に設定しておけば、[デプロイ](#8-プラットフォームをデプロイする)の `terraform apply` で一緒に作成されます。デプロイ後に有効にする場合も同じ手順で、`terraform apply` を再実行します。

1. Brave Search API のダッシュボードで API キーを発行し、`.env` に追加します。
   ```bash
   # .env
   TF_VAR_search_api_key=<Brave の API キー>
   ```
2. `infra/platform/terraform.tfvars` でプロバイダを選びます。
   ```hcl
   search_provider = "brave"
   # http-json の場合:
   # search_provider     = "http-json"
   # search_provider_url = "https://search.example.com/api"
   ```
3. 反映します。
   ```bash
   set -a; . ../../.env; set +a
   terraform apply     # search-gateway 一式（Deployment / Service / SA / NetworkPolicy / Secret）が作成される
   ```
   `search_provider` を変えると Orchestrator が、キーを変えると Search Gateway が自動で再起動します。


### 動作確認（デプロイ後）

```bash
NS=agent-platform
# 1. Gateway が起動しているか
kubectl -n $NS logs deploy/search-gateway --tail=20
#   正常: "search-gateway listening on 0.0.0.0:8080"
#   設定誤り: "search-gateway: configuration error: ..."（CrashLoopBackOff になる）

# 2. Gateway の Pod から検索 API を 1 回呼ぶ（API キーと egress の確認。クエリは無害なものに）
kubectl -n $NS exec deploy/search-gateway -- python -c "
from agent_platform.search_gateway import gateway_from_environ
r = gateway_from_environ().handle_search(b'{\"query\": \"python asyncio timeout\"}', client='manual')
print(r.status, r.body.get('provider'), len(r.body.get('hits', [])))"
#   期待: 200 brave <件数>

# 3. Orchestrator に Gateway の URL が渡っているか（無効時は空）
kubectl -n $NS exec deploy/orchestrator -- sh -c 'echo "[$AGENT_PLATFORM_SEARCH_GATEWAY_URL]"'

# 4. 実際の task で検索されたか（1 回の検索につき 1 行）
kubectl -n $NS logs deploy/search-gateway | grep decision=
#   search-gateway: client=10.x.x.x decision=accepted query_sha256=... hits=5 provider=brave
```

Gateway のログに残るのはクエリの SHA-256・件数・プロバイダ名だけで、クエリ本文や結果は残りません。

### キーの更新・無効化

- **キーの更新**: `.env` の `TF_VAR_search_api_key` を書き換えて `terraform apply`。
- **無効化**: `search_provider = "none"` にして `terraform apply`。Search Gateway 一式と Secret `search-api-key` が削除され、Orchestrator が再起動して、以降の Execute から検索が消えます。

### トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| `configuration error: the brave upstream needs SEARCH_GATEWAY_API_KEY` | `TF_VAR_search_api_key` が空、または `.env` を読み込まずに apply した |
| `configuration error: unknown search upstream 'x' (known: ...)` | `search_provider` の綴り誤り、または未登録のプロバイダ（イメージが古い可能性も） |
| `configuration error: the brave upstream takes no SEARCH_GATEWAY_UPSTREAM_URL` | `brave` / `fake` では `search_provider_url` を空にする |
| 動作確認 2 が `502 ... upstream failed` | 検索 API 側のエラー（キー無効 401、上限超過 429 など）、または Gateway から 443 に出られない。Gateway のログは理由を伏せるので、キーと egress を確認 |
| 動作確認 2 が `429` | Gateway のレート制限（全体 30 回/分、送信元ごと 10 回/分）。1 分待つ |
| エージェントが検索しない | prompt に検索の案内が載るのは有効時のみ。動作確認 3 で URL が空なら Orchestrator が古い設定のまま（`terraform apply` を再実行） |
| 検索が拒否される（exit 1） | クエリに task 文脈の識別子・secret の形・コードが含まれている（意図された動作） |

## 6. イメージをビルドする

```bash
docker login <registry>                         # 認証情報はリポジトリに置かない
REGISTRY=<registry>/<namespace> scripts/build-push.sh
# Claude Code を使う場合:
REGISTRY=<registry>/<namespace> INSTALL_CLAUDE_CODE=true CLAUDE_CODE_VERSION=<exact version> \
  scripts/build-push.sh
```

- push には手元の `docker login` の認証情報を使います。クラスタが pull するときの認証情報は別に設定します（下記）。
- Worker イメージと egress proxy イメージをビルド・push し、digest 固定の参照を `infra/platform/images.auto.tfvars`（gitignore 済み）に書き込みます。
- 既定は `linux/amd64`。arm64 ノードの場合は `PLATFORM=linux/arm64` を指定し、`deploy/worker/Dockerfile` の `FROM` を arm64 の digest に変えてください。

**プライベートレジストリの場合**: クラスタのノードがイメージを pull するための認証情報を設定します。Terraform が pull 用の Secret `regcred` を作り、各 ServiceAccount に付けます。

| 値 | 置き場所 |
|---|---|
| `registry_server`（例: `ghcr.io`） | `infra/platform/terraform.tfvars` |
| `registry_username` | `infra/platform/terraform.tfvars` |
| `registry_password`（パスワード / トークン） | `.env` の `TF_VAR_registry_password` |

公開レジストリの場合や、ノード自身に pull 権限がある場合（EKS ノードの IAM ロールで ECR、GKE ノードのサービスアカウントで Artifact Registry など）は設定不要です（`registry_server` を空のまま）。pull 専用（読み取り専用）のユーザー / トークンを使うことを推奨します。

## 7. Kubernetes クラスタを用意する

既存のクラスタがあればそれを使います（EKS / GKE / AKS / 自前 k3s など）。kubeconfig で接続できれば十分です。

構築例:

- **ローカル（kind）**: [infra/clusters/kind/README.md](../infra/clusters/kind/README.md)
- **さくらのクラウド（k3s）**: [infra/clusters/sakura-k3s/README.md](../infra/clusters/sakura-k3s/README.md)
  - スイッチ・VPC ルータ・サーバ 3 台・k3s のインストール・kubeconfig の取得までを `terraform apply` 1 回で行います（コンテナレジストリも任意で作成）。
  - API サーバは公開しないため、kubectl と `infra/platform` は SSH トンネル（`scripts/tunnel.sh`）経由で使います。トンネルを起動したまま 8 章に進んでください。
  - `infra/platform` には `kubeconfig_path = "~/.config/agent-platform/kubeconfig-sakura-k3s.yaml"` を設定します。

> CNI が NetworkPolicy を強制することを必ず確認してください。強制しない CNI（kind 既定の kindnet の一部バージョン、Flannel 単体など）では、Sandbox の通信制限が効きません。デプロイ後に `scripts/smoke.sh` で確認できます。

## 8. プラットフォームをデプロイする

### 8.1 設定

```bash
cd infra/platform
cp terraform.tfvars.example terraform.tfvars    # gitignore 済み
$EDITOR terraform.tfvars
```

secret は `terraform.tfvars` に書かず、`.env` に `TF_VAR_*` として書いて読み込みます（[2.1](#21-設定の置き場所と-env)、[.env.example](../.env.example)）。

```bash
# .env（リポジトリ直下）
TF_VAR_agent_api_key=<UUID>:<シークレット>   # LLM の API キー（AI Engine のアカウントトークンなど）
TF_VAR_webhook_secret=...                   # GitHub App の webhook secret
# TF_VAR_registry_password=...              # プライベートレジストリ（任意）
# TF_VAR_search_api_key=...                 # 検索 API（任意）
```

```bash
# infra/platform で terraform を実行するシェルに読み込む
set -a; . ../../.env; set +a
```

最低限必要な変数: `worker_image`（build-push.sh が書き込み）、`agent_api_key`・`webhook_secret`（`.env`）、`reader_app_id`・`reader_installation_id`・`reader_private_key_path`（[3.1 の対応表](#31-控えた値の設定先)）、LLM の設定（[4.1](#41-設定する場所)）。

### 8.2 リポジトリスナップショットを作る

Execute Job は、対象リポジトリの git bundle を格納した ConfigMap を展開して作業します（[既知の制約](./design.md#15-既知の制約)）。namespace を先に作るため、初回は 2 段階で apply します。

```bash
terraform init
terraform apply -target=kubernetes_namespace_v1.this

git clone https://github.com/<owner>/<repo> /tmp/target-repo
scripts/create-snapshot.sh /tmp/target-repo main agent-platform   # → repo-snapshot-<sha12> を表示
# terraform.tfvars に snapshot_configmap = "repo-snapshot-<sha12>" を設定

terraform apply
```

対象リポジトリに新しいコミットが入ったら、スナップショットを作り直して `snapshot_configmap` を更新してください（Execute は承認時の base SHA を checkout するため、bundle にその SHA が含まれている必要があります）。

### 8.3 webhook を公開する

Orchestrator の Service（`orchestrator:8080`）を GitHub から到達できるようにし、その URL を GitHub App の Webhook URL に設定します。

| 方法 | 設定 |
|---|---|
| 既存の Ingress Controller | `webhook_service_type = "ClusterIP"` のまま、Service `orchestrator` を指す Ingress（TLS 推奨）を作成。`webhook_ingress_cidrs` に Ingress Controller の Pod CIDR を指定すると絞れます |
| LoadBalancer | `webhook_service_type = "LoadBalancer"` |
| NodePort + ルータのポート転送 | `webhook_service_type = "NodePort"`、`webhook_node_port = 30080`（さくら k3s 例の構成） |

境界は webhook の HMAC 署名と、起動者の権限チェック（fail-closed）です。GitHub の配信元 IP は変動するため、送信元 IP での制限は補助として扱ってください。

### 8.4 確認

```bash
kubectl -n agent-platform get pods
kubectl -n agent-platform logs deploy/orchestrator      # "webhook listening on 0.0.0.0:8080"
WORKER_IMAGE=$(sed -n 's/^worker_image = "\(.*\)"/\1/p' infra/platform/images.auto.tfvars) \
LLM_HOST=api.ai.sakura.ad.jp scripts/smoke.sh agent-platform
```

GitHub App の設定画面 → Advanced → Recent Deliveries で webhook の配信結果も確認できます。

## 9. Issue から使う

対象リポジトリの Issue に、書き込み権限を持つユーザーがコメントします。

1. **`/agent-platform run`** — 計画が作られ、Issue にコメントされます（要約・変更予定ファイル・テストコマンド・`execute network:` のレベル）。
2. 計画が質問を返した場合は **`/agent-platform answer <回答>`**。回答を加えて計画が作り直されます。
3. 計画を確認し、**`/agent-platform approve`**。承認後、実装 → Draft PR → レビューが自動で進みます。
   - 明示指定する場合: `/agent-platform approve <task-id> <plan-hash>`
4. Draft PR が作られ、計画の要約がコメントされ、レビュー結果が check run として表示されます。
5. マージ判断は人が行います。

途中で止まった場合（範囲外の変更、テスト失敗、レビューで計画に問題など）は Issue にその旨がコメントされます。**`/agent-platform redo`** で、コメント本文を追加指示としてやり直せます。

## 10. 運用

### Orchestrator 上での手動操作

状態（SQLite）はクラスタ内の PVC にあるため、`agentctl` は Orchestrator Pod 内で実行します。

```bash
OP="kubectl -n agent-platform exec deploy/orchestrator --"
$OP python -m agent_platform.cli status --task-id <task-id>
# 計画は同じで実装をやり直す（承認はそのまま有効）
$OP python -m agent_platform.cli reexecute --task-id <task-id> --plan-hash <hash> --requested-by <name>
# 計画からやり直す（再承認が必要）
$OP python -m agent_platform.cli replan --task-id <task-id> --plan-hash <hash> --approver <name>
# 承認待ちの計画を却下
$OP python -m agent_platform.cli reject --task-id <task-id> --plan-hash <hash> --approver <name> --reason "..."
# 期限切れの ephemeral artifact を削除
$OP python -m agent_platform.cli artifacts gc --retention-days 30
```

各コマンドの引数は `--help` で確認してください。

### 設定の変更

| 変更内容 | 手順 |
|---|---|
| コードの更新 | `scripts/build-push.sh` → `terraform apply`（digest が変わり Pod が更新される） |
| エージェント・モデルの変更 | `agent_runtime` / `agent_model` / `agent_base_url` を変えて `terraform apply`（runtime を変える場合はイメージに CLI が入っていること） |
| 検索の有効化・変更 | `search_provider`（+ `.env` の `TF_VAR_search_api_key`）を変えて `terraform apply`（[5 章](#5-web-検索を設定する任意)） |
| egress レベルの変更 | `egress_level` / `egress_allowed_hosts` を変えて `terraform apply` |
| API キーのローテーション | `TF_VAR_agent_api_key` を更新して `terraform apply`（次の Job から反映） |

### 撤去

```bash
cd infra/platform && terraform destroy
```

PVC（状態と artifact）も削除されます。必要なら事前に退避してください。

## 11. トラブルシューティング

| 症状 | 確認すること |
|---|---|
| コメントしても反応がない | GitHub App の Recent Deliveries（配信エラー / 署名不一致）、`kubectl logs deploy/orchestrator`（`rejected:` の理由）。コメントした人に書き込み権限があるか |
| 計画が失敗する（Job が FAILED） | `kubectl -n agent-platform logs job/plan-<task-id>`。モデル名の誤り、API キー、LLM への egress（`scripts/smoke.sh`） |
| LLM が 401 / 403 を返す | API キーと認証方式。AI Engine は `<UUID>:<シークレット>` を Bearer で送る（Claude Code では `agent_auth_scheme = "bearer"`） |
| LLM が 429 を返す | プランのリクエスト上限・レート制限（AI Engine の無償プランなど） |
| 計画が空で FAILED | モデルの出力のばらつき。`/agent-platform redo` で再試行するか、Issue をより具体的にする |
| Execute がタイムアウト | `agent_timeout_seconds` を延長 |
| `snapshot HEAD ... != base_sha` | スナップショットが古い。`scripts/create-snapshot.sh` で作り直し `snapshot_configmap` を更新 |
| パッケージのインストールに失敗 | 既定の `egress_level = none` ではインターネットに出られない。`allowlist` で必要なホストを許可 |
| 検索が拒否される | クエリに task 文脈の識別子・secret 形状・コードが含まれている（意図された動作） |
| Search Gateway が CrashLoopBackOff / 検索の不具合 | [5 章のトラブルシューティング](#5-web-検索を設定する任意) |

## 12. 開発

```bash
uv sync
uv run pytest                 # unit + contract（ネットワーク不要）
uv run ruff check src tests
uv run mypy                   # strict
```

- 外部依存は fake で置換しています（エージェント CLI は fake バイナリ、kubectl は fake スクリプト、GitHub / S3 は localhost の fake サーバ）。
- JSON Schema は pydantic モデルが正です。モデルを変えたら `schemas/` を再生成してください（契約テストが同期を検証します）:
  ```bash
  uv run python -c "import json; from agent_platform.schema_export import SCHEMAS, build; [open(f'schemas/{n}.schema.json','w').write(json.dumps(build(n), indent=2, sort_keys=True, ensure_ascii=False) + '\n') for n in SCHEMAS]"
  ```
- 拡張方法は [extending.md](./extending.md)、コントリビュートの方法は [CONTRIBUTING.md](../CONTRIBUTING.md) を参照してください。
