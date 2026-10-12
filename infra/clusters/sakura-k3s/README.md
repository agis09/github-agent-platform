# さくらのクラウド k3s クラスタ（構築例）

さくらのクラウド上に、グローバル IP を持たない k3s クラスタ（server 1 + agent 2）を **`terraform apply` 1 回** で作る例です。プラットフォーム本体のデプロイは [`infra/platform`](../../platform/) で行います。

```
VPC ルータ（グローバル IP、NAT）
  ├─ ポート転送 2222 → k3s server:22（踏み台、鍵認証のみ）
  ├─ ポート転送 8080 → k3s server:30080（GitHub webhook → Orchestrator NodePort）
  └─ スイッチ 192.168.10.0/24
       ├─ server   .11
       ├─ agent-1  .12
       └─ agent-2  .13
```

Terraform が行うこと:

1. スイッチ、SSH 鍵、ディスク、サーバを作成。SSH 鍵はディスクの修正機能でサーバ作成時に書き込みます（このアーカイブには cloud-init がないため）。
2. VPC ルータを作成。DHCP で各サーバの MAC アドレスに IP を固定し、ポート転送を設定します。
3. SSH で k3s をインストール。server にはルータのポート転送経由で、agent には server を踏み台にして接続します。
4. kubeconfig を手元に取得（`~/.config/agent-platform/kubeconfig-sakura-k3s.yaml`、0600）。
5. コンテナレジストリを作成（任意）。

k3s 同梱の kube-router が NetworkPolicy を強制し、local-path-provisioner が既定の StorageClass になります。

## 前提

- Terraform 1.9+
- このクラスタ専用の SSH 鍵ペア。Terraform が SSH で k3s をインストールするため、**パスフレーズなし** で作成します（パスフレーズ付きの鍵は remote-exec で使えません）。
  ```bash
  ssh-keygen -t ed25519 -N "" -C "agent-platform sakura-k3s" -f ~/.ssh/agent-platform
  ```
- さくらのクラウドの API キー（**対象プロジェクトに紐づくもの**）。リポジトリ直下の `.env` に書きます（gitignore 済み）。

```bash
# .env
SAKURACLOUD_ACCESS_TOKEN=...
SAKURACLOUD_ACCESS_TOKEN_SECRET=...
```

## 手順

### 1. 設定

```bash
cd infra/clusters/sakura-k3s
cat > terraform.tfvars <<EOT                       # gitignore 済み
admin_public_key       = "$(cat ~/.ssh/agent-platform.pub)"
admin_private_key_path = "~/.ssh/agent-platform"
registry_subdomain     = "<全 sakuracr.jp で一意なラベル>"   # レジストリ不要なら省略
EOT
```

ノード構成（台数・スペック）、ゾーン、ポートなどは `variables.tf` を参照してください。

### 2. 作成

```bash
set -a; . ../../../.env; set +a
terraform init
terraform apply
```

サーバの起動と k3s のインストールに 10〜20 分ほどかかります。完了すると kubeconfig が書き込まれます。

### 3. kubectl の接続

API サーバは公開していないため、SSH トンネルを張って使います。kubectl や `infra/platform` の Terraform を使う間は、トンネルを起動したままにしてください。

```bash
SSH_KEY=~/.ssh/agent-platform scripts/tunnel.sh &      # 127.0.0.1:16443 → API サーバ（terraform output tunnel_command と同じ）
export KUBECONFIG=$(terraform output -raw kubeconfig_path | sed "s#^~#$HOME#")
kubectl get nodes                                  # 3 台が Ready
```

### 4. イメージとプラットフォーム

```bash
docker login "$(terraform output -raw registry_fqdn)" -u push   # パスワード: terraform output -raw registry_password
REGISTRY=$(terraform output -raw registry_fqdn) ../../../scripts/build-push.sh
```

次に [`infra/platform`](../../platform/) でプラットフォーム本体をデプロイします（[docs/usage.md §8](../../../docs/usage.md#8-プラットフォームをデプロイする)）。このクラスタを使うには、`infra/platform/terraform.tfvars` に **次の設定が必須** です（`terraform.tfvars.example` の既定値のままでは動きません）。

```hcl
kubeconfig_path      = "~/.config/agent-platform/kubeconfig-sakura-k3s.yaml"
registry_server      = "<label>.sakuracr.jp"      # terraform output -raw registry_fqdn
registry_username    = "push"
webhook_service_type = "NodePort"
webhook_node_port    = 30080
```

| 設定 | 理由 |
|---|---|
| `kubeconfig_path` | この Terraform が書き出した kubeconfig。既定の `~/.kube/config` のままだと接続先がなく、`dial tcp [::1]:80: connect: connection refused` で失敗します。トンネル（`scripts/tunnel.sh`）も起動しておきます |
| `registry_server` | ノードがイメージを pull するための Secret のキー。**ホスト名だけ**（`https://` やパスは付けない）を書き、`images.auto.tfvars` の `worker_image` のホスト部分と一致させます。食い違うと Terraform の検証エラーになります |
| `registry_username` / `TF_VAR_registry_password` | この Terraform が作ったレジストリなら `push` と `terraform output -raw registry_password`（パスワードは `.env` に書きます）。別に作ったレジストリを使う場合は、そのホスト・ユーザー・パスワードを指定します |
| `webhook_service_type` / `webhook_node_port` | VPC ルータはグローバル IP の 8080 番を server の 30080 番に転送します。Orchestrator の Service をこの番号の NodePort にします（`webhook_public_port` / `webhook_node_port` を変えた場合はその値） |

LLM には同じさくらのクラウドの AI Engine が使えます（`agent_base_url = "https://api.ai.sakura.ad.jp/v1"`、[docs/usage.md §4](../../../docs/usage.md#4-ai-エージェントと-llm-を選ぶ)）。

GitHub App の Webhook URL には `terraform output -raw webhook_url` を設定します（TLS が必要な場合は前段にリバースプロキシを置いてください）。

## 撤去

```bash
(cd ../../platform && terraform destroy)
terraform destroy
rm -f ~/.config/agent-platform/kubeconfig-sakura-k3s.yaml
```

`terraform destroy` の後、ディスクが残っていないかコントロールパネル（または `usacloud disk list`）で確認してください。コンテナレジストリのサブドメインは、削除後もしばらく予約されたままになります。

## トラブルシューティング

| 症状 | 確認すること |
|---|---|
| `remote-exec` が SSH 接続を待ち続ける | サーバの起動とルータの DHCP の完了を待っています（最大 20 分）。タイムアウトする場合は、コントロールパネルのコンソールで、ノードが `192.168.10.x` を取得しているか確認します（ユーザー `ubuntu`、パスワードは `terraform output -raw console_password`） |
| `waiting for network` が続く | ノードから外部に出られていません。ルータの NAT と DNS（`dns_servers`）を確認してください |
| k3s のインストールが途中で失敗した | `terraform apply` を再実行すると、失敗したステップからやり直します |
| `409 limit_count_in_zone` | ゾーンのリソース上限（スイッチ数など）に達しています。別のゾーンを使うか、既存リソースを解放してください |
| kubectl / `infra/platform` が `connection refused`（127.0.0.1:16443） | トンネル（`scripts/tunnel.sh`）が起動していません |
| 手動の `ssh` で `REMOTE HOST IDENTIFICATION HAS CHANGED` | クラスタの再構築や、以前使われたグローバル IP がルータに割り当てられた場合に、`~/.ssh/known_hosts` の古いホスト鍵と食い違います。このクラスタのホスト鍵は `~/.config/agent-platform/known_hosts-sakura-k3s` で管理しているので、`terraform output -raw ssh_command`（このファイルを使う）で接続してください |

## 制約

- この構成の VPC ルータはファイアウォールを無効にしています（このアーカイブとルータの組み合わせで、有効時にポート転送が機能しなかったため）。公開されるのは SSH（鍵認証のみ）と webhook ポートだけです。
- k3s の join token は、インストール時のコマンドとしてノード上で一時的に参照されます。Terraform の state には（`random_password` として）保存されるため、state は保護された場所に置いてください。
