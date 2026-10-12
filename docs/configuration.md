# 設定リファレンス

設定は 2 層です。

1. **Terraform 変数**（`infra/platform`）— クラウドにデプロイする利用者が触るのはこちらだけです。Terraform が下の環境変数を Orchestrator に設定します。
2. **環境変数**（`AGENT_PLATFORM_*`）— アプリケーションが読む値。ローカル実行や、Terraform 以外でデプロイする場合に直接設定します。

## 値の置き場所

| 種類 | クラウド（Kubernetes）にデプロイする場合 | ローカルで実行する場合 |
|---|---|---|
| secret（API キー・webhook secret・パスワード） | `.env` に `TF_VAR_<変数名>`（例: `TF_VAR_agent_api_key`） | `.env` に環境変数（例: `LLM_API_KEY`） |
| secret 以外 | `infra/platform/terraform.tfvars` | `.env` に `AGENT_PLATFORM_*` |

- `.env` はリポジトリ直下に [.env.example](../.env.example) をコピーして作ります（gitignore 済み）。`set -a; . ./.env; set +a` でシェルに読み込んでから `terraform` / `agentctl` を実行します。自動では読み込まれません。
- `terraform.tfvars` は [infra/platform/terraform.tfvars.example](../infra/platform/terraform.tfvars.example) をコピーして作ります（gitignore 済み）。
- 手順は [usage.md「設定の置き場所と .env」](./usage.md#21-設定の置き場所と-env) を参照してください。

## LLM の設定例

| LLM | `agent_runtime` | `agent_base_url` | `agent_model` | `agent_auth_scheme` | `agent_api_key` |
|---|---|---|---|---|---|
| さくらのクラウド AI Engine（OpenAI 互換） | `codex` | `https://api.ai.sakura.ad.jp/v1` | `gpt-oss-120b` | （不要） | アカウントトークン `<UUID>:<シークレット>` |
| さくらのクラウド AI Engine（Anthropic 互換） | `claude-code` | `https://api.ai.sakura.ad.jp` | `gpt-oss-120b` | `bearer` | 同上 |
| OpenAI API | `codex` | 空 | `gpt-5-codex` など | （不要） | OpenAI の API キー |
| Anthropic API | `claude-code` | 空 | `claude-sonnet-5-5` など | `api-key` | Anthropic の API キー |
| vLLM / LiteLLM など | `codex` | `https://<host>/v1` | エンドポイントのモデル名 | （不要） | エンドポイントのキー |

`agent_base_url` は、codex では `/v1` まで含め（CLI が `/responses` を付ける）、claude-code ではホストまでにします（CLI が `/v1/messages` を付ける）。

AI Engine の詳細は [usage.md「AI エージェントと LLM を選ぶ」](./usage.md#4-ai-エージェントと-llm-を選ぶ) を参照してください。

## Terraform 変数（infra/platform）

### クラスタ・イメージ

| 変数 | 既定 | 説明 |
|---|---|---|
| `kubeconfig_path` | `~/.kube/config` | Terraform が使う kubeconfig |
| `kubeconfig_context` | `null` | kubeconfig のコンテキスト（null = current） |
| `namespace` | `agent-platform` | 全リソースを置く namespace |
| `pod_security_version` | `latest` | PSS の enforce-version ラベル |
| `worker_image` | （必須） | Worker イメージ。`<repo>@sha256:<64 hex>` の digest 固定。`scripts/build-push.sh` が `images.auto.tfvars` に書き込む |
| `egress_proxy_image` | `""` | smokescreen イメージ（digest 固定）。`egress_level = allowlist` で必須 |
| `registry_server` / `registry_username` / `registry_password` | `""` | プライベートレジストリの pull 用 credential。`registry_server` はホスト名のみで、`worker_image` のホスト部分と一致させる（検証あり）。空なら imagePullSecret を作らない。`registry_password` は `.env` の `TF_VAR_registry_password` で渡す |
| `resource_quota` | pods 8 ほか | namespace の ResourceQuota |

### エージェント・LLM

| 変数 | 既定 | 説明 |
|---|---|---|
| `agent_runtime` | `codex` | `codex` / `claude-code`。イメージに該当 CLI が入っていること |
| `agent_model` | `""` | CLI に渡すモデル名（空 = CLI の既定） |
| `agent_base_url` | `""` | エンドポイント。codex: OpenAI Responses 互換の base URL（AI Engine: `https://api.ai.sakura.ad.jp/v1`）、claude-code: Anthropic 互換の base URL（AI Engine: `https://api.ai.sakura.ad.jp`）。空 = ベンダー API |
| `agent_auth_scheme` | `api-key` | claude-code のみ。`api-key`（`x-api-key` ヘッダ、Anthropic API）/ `bearer`（`Authorization: Bearer`、AI Engine などのゲートウェイ）。codex は常に Bearer |
| `agent_api_key` | （必須、sensitive） | LLM の API キー。`.env` の `TF_VAR_agent_api_key` で渡す。Secret `llm-api-key` に保存され、Job にだけ注入される |
| `agent_timeout_seconds` | `3600` | 1 回のエージェント実行の上限。Job の `activeDeadlineSeconds` はこれ + 300 |
| `agent_max_output_tokens` | `null` | codex のみ。`model_max_output_tokens` |
| `llm_egress_cidrs` | `[]` | エージェント Job が到達できる LLM エンドポイントの CIDR。空 = 公開 IPv4 全体 |
| `llm_egress_port` | `443` | LLM エンドポイントのポート |

### GitHub

| 変数 | 既定 | 説明 |
|---|---|---|
| `github_api_url` | `https://api.github.com` | GHES は `https://<host>/api/v3` |
| `github_ca_bundle_path` | `""` | GHES がプライベート CA の場合の CA バンドル（PEM のパス） |
| `reader_app_id` / `reader_installation_id` | （必須） | Reader App |
| `reader_private_key_path` | （必須） | Reader App の秘密鍵（Terraform 実行マシン上のパス） |
| `publisher_app_id` / `publisher_installation_id` / `publisher_private_key_path` | `""` | Publisher App。空なら Reader App を流用（1 App 構成） |

### Webhook

| 変数 | 既定 | 説明 |
|---|---|---|
| `webhook_secret` | （必須、sensitive） | GitHub App の webhook secret |
| `webhook_config` | 下表 | トリガー設定（ConfigMap `webhook-config` → `webhook_config.json`） |
| `webhook_service_type` | `ClusterIP` | `ClusterIP` / `NodePort` / `LoadBalancer` |
| `webhook_node_port` | `null` | NodePort の固定番号 |
| `webhook_ingress_cidrs` | `["0.0.0.0/0"]` | webhook ポートへの送信元 |

`webhook_config` のフィールド:

| フィールド | 既定 | 説明 |
|---|---|---|
| `trigger` | `comment` | `comment`（Issue コメントで起動）/ `none`（webhook を無視） |
| `command` | `/agent-platform run` | 起動コマンドの文字列（`answer` / `approve` / `redo` は同じ接頭辞で解釈） |
| `kicker_policy` | `collaborator` | 起動できる人の判定。書き込み権限者のみ（fail-closed） |
| `allowed_kickers` | `[]` | 起動できるユーザーをさらに限定（空 = 書き込み権限者全員） |
| `approvers` | `[]` | 承認できるユーザーをさらに限定（空 = 書き込み権限者全員） |
| `auto_pipeline` | `true` | 承認後に execute → publish → review を自動で進める |

### Orchestrator・Sandbox

| 変数 | 既定 | 説明 |
|---|---|---|
| `state_storage_class` | `null` | 状態 PVC の StorageClass（null = 既定） |
| `state_storage_size` | `5Gi` | 状態 PVC のサイズ |
| `orchestrator_egress_cidrs` | `[]` | Orchestrator が 443 で到達できる宛先（GitHub、S3）。空 = 公開 IPv4 全体。GHES / S3 が内部ネットワークにある場合に指定 |
| `kube_api_cidrs` / `kube_api_port` | `[]` / `6443` | API サーバのエンドポイント。空 = `default/kubernetes` の Endpoints から自動取得 |
| `snapshot_configmap` | `""` | Execute Job が展開するリポジトリスナップショット（`scripts/create-snapshot.sh`） |
| `review_enabled` | `true` | publish 後に Review Job を実行する |
| `s3` | `null`（sensitive） | S3 互換 artifact ストア `{endpoint, bucket, region, access_key_id, secret_access_key}`。null = PVC |

### ネットワーク egress・検索

| 変数 | 既定 | 説明 |
|---|---|---|
| `egress_level` | `none` | Execute Job の外向き通信: `none` / `allowlist` / `full` |
| `egress_allowed_hosts` | `[]` | `allowlist` で許可するホスト（小文字、先頭 `*.` のワイルドカード可）。`allowlist` のときだけ必須 |
| `search_provider` | `none` | `none` / `fake` / `brave` / `http-json` / `upstreams.py` に登録した名前 |
| `search_provider_url` | `""` | `http-json` のエンドポイント（https） |
| `search_api_key` | `""`（sensitive） | 検索 API のキー。Search Gateway の Pod にだけ渡る |

## 環境変数（アプリケーション）

### 共通

| 変数 | 既定 | 説明 |
|---|---|---|
| `AGENT_PLATFORM_HOME` | `.agent-platform` | 状態（SQLite・artifact）のディレクトリ |
| `AGENT_PLATFORM_ADAPTER` | `fake` | `fake`（オフライン）/ `local`（計画のみローカルの CLI で実行）/ `k8s`（Kubernetes Job） |
| `AGENT_PLATFORM_APPROVAL_TTL_HOURS` | `72` | 承認の有効期限 |
| `AGENT_PLATFORM_ARTIFACT_MAX_SIZE` | `16777216` | artifact 1 件の上限バイト数 |
| `AGENT_PLATFORM_GC_AUDIT_CONFIRM` | — | `yes` のとき `artifacts gc --retention-class audit` を許可 |

### エージェントランタイム

| 変数 | 既定 | 説明 |
|---|---|---|
| `AGENT_PLATFORM_AGENT_RUNTIME` | `codex` | `codex` / `claude-code` |
| `AGENT_PLATFORM_AGENT_BINARY` | `codex` / `claude` | CLI のパスまたはコマンド名 |
| `AGENT_PLATFORM_AGENT_MODEL` | — | モデル名 |
| `AGENT_PLATFORM_AGENT_BASE_URL` | — | エンドポイント（http(s) URL。例: `https://api.ai.sakura.ad.jp/v1`） |
| `AGENT_PLATFORM_AGENT_API_KEY_ENV` | `LLM_API_KEY` | API キーを保持する **環境変数の名前**（値ではない） |
| `AGENT_PLATFORM_AGENT_TIMEOUT_SECONDS` | `600` | 1 回の実行の上限秒数 |
| `AGENT_PLATFORM_AGENT_AUTH_SCHEME` | `api-key` | claude-code のみ。`api-key` / `bearer` |
| `AGENT_PLATFORM_AGENT_MAX_OUTPUT_TOKENS` | — | codex のみ |
| `AGENT_PLATFORM_CODEX_WIRE_API` | `responses` | codex のみ。現在は `responses` だけ |
| `LLM_API_KEY`（名前は上で変更可） | — | API キー本体（AI Engine はアカウントトークン `<UUID>:<シークレット>`） |

### GitHub

| 変数 | 説明 |
|---|---|
| `AGENT_PLATFORM_GITHUB_API_URL` / `_APP_ID` / `_INSTALLATION_ID` / `_PRIVATE_KEY_PATH` | Reader App |
| `AGENT_PLATFORM_GITHUB_CA_BUNDLE` | プライベート CA の PEM（API 呼び出しと Publisher の git 操作に使用） |
| `AGENT_PLATFORM_PUBLISHER_API_URL` / `_APP_ID` / `_INSTALLATION_ID` / `_PRIVATE_KEY_PATH` | Publisher App（未設定なら publish / Issue コメントは行わない） |
| `AGENT_PLATFORM_WEBHOOK_SECRET` | webhook の HMAC secret（`webhook serve` で必須） |

### Kubernetes（`AGENT_PLATFORM_ADAPTER=k8s`）

| 変数 | 既定 | 説明 |
|---|---|---|
| `AGENT_PLATFORM_K8S_PLAN_IMAGE` | （必須） | Job に使う Worker イメージ |
| `AGENT_PLATFORM_K8S_NAMESPACE` | `agent-platform` | Job を作る namespace |
| `AGENT_PLATFORM_KUBECONFIG` | — | kubeconfig（Pod 内では不要、SA トークンを使う） |
| `AGENT_PLATFORM_K8S_LLM_SECRET` | `llm-api-key` | API キーの Secret 名（キー `api-key`） |
| `AGENT_PLATFORM_K8S_DEADLINE_SECONDS` | `900` | Plan Job の期限 |
| `AGENT_PLATFORM_K8S_EXECUTE_DEADLINE_SECONDS` | `1800` | Execute Job の期限 |
| `AGENT_PLATFORM_K8S_REVIEW_DEADLINE_SECONDS` | `900` | Review Job の期限 |
| `AGENT_PLATFORM_K8S_TTL_SECONDS` | `600` | 完了 Job の TTL |
| `AGENT_PLATFORM_K8S_SNAPSHOT_CONFIGMAP` | — | リポジトリスナップショットの ConfigMap |
| `AGENT_PLATFORM_K8S_REVIEW_ENABLED` | — | `1` で Review Job を有効化 |
| `AGENT_PLATFORM_NETWORK_GUARD_SECONDS` | `30` | Job 内のワーカーが、NetworkPolicy の適用（Kubernetes API に接続できなくなること）を待つ上限秒数。超えるとエージェントを起動せずに失敗する。`0` で無効 |
| `AGENT_PLATFORM_EXECUTE_COMMAND_TIMEOUT` | `120` | テストコマンド 1 つの上限秒数 |
| `AGENT_PLATFORM_EXECUTE_MAX_OUTPUT` | `1048576` | patch の上限バイト数 |
| `AGENT_PLATFORM_EGRESS_LEVEL` / `_PROXY_URL` / `_ALLOWED_HOSTS` | `none` | egress レベル（Terraform と一致させる。不整合は起動時エラー） |
| `AGENT_PLATFORM_SEARCH_GATEWAY_URL` | — | 設定時のみ検索が有効 |

### S3 互換ストア

| 変数 | 説明 |
|---|---|
| `AGENT_PLATFORM_S3_ENDPOINT` / `_BUCKET` / `_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` | 4 つすべて設定すると S3 を使う（未設定ならローカル FS） |
| `AGENT_PLATFORM_S3_REGION` | 既定 `us-east-1` |

### Search Gateway（Terraform が設定）

| 変数 | 既定 | 説明 |
|---|---|---|
| `SEARCH_GATEWAY_UPSTREAM` | （必須） | プロバイダ名 |
| `SEARCH_GATEWAY_UPSTREAM_URL` | — | `http-json` などエンドポイントを取るプロバイダ用 |
| `SEARCH_GATEWAY_API_KEY` | — | 検索 API のキー |
| `SEARCH_GATEWAY_BIND` / `SEARCH_GATEWAY_PORT` | `127.0.0.1` / `8080` | 待ち受け |
| `SEARCH_GATEWAY_RATE_PER_MINUTE` / `SEARCH_GATEWAY_CLIENT_RATE_PER_MINUTE` | `30` / `10` | レート制限 |

### Job 内部（Orchestrator が設定、通常は触らない）

`PLAN_*`、`EXEC_*`、`REVIEW_*`、`SEARCH_PROVIDER`、`SEARCH_GATEWAY_URL` は Job テンプレート（`deploy/jobs/`）経由でワーカーに渡される入力です。各ワーカーのモジュール docstring（`src/agent_platform/worker/*.py`）に一覧があります。
