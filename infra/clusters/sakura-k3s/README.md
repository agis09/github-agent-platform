# さくらのクラウド k3s クラスタ（構築例）

さくらのクラウド上に、グローバル IP を持たない k3s クラスタ（server 1 + agent 2）を作る例です。プラットフォーム本体のデプロイは [`infra/platform`](../../platform/) で行います。

```
VPC ルータ（グローバル IP、NAT）
  ├─ ポート転送 2222 → k3s server:22（踏み台、鍵認証のみ）
  ├─ ポート転送 8080 → k3s server:30080（GitHub webhook → Orchestrator NodePort）
  └─ スイッチ 192.168.10.0/24
       ├─ server   .11
       ├─ agent-1  .12
       └─ agent-2  .13
```

k3s 同梱の kube-router が NetworkPolicy を強制し、local-path-provisioner が既定の StorageClass になります。

## 前提

- Terraform 1.9+、[usacloud](https://github.com/sacloud/usacloud)、SSH 鍵
- API キー（**対象プロジェクトに紐づくもの**）を環境変数で渡す。ファイルに書いてリポジトリに置かないこと

```bash
export SAKURACLOUD_ACCESS_TOKEN=...
export SAKURACLOUD_ACCESS_TOKEN_SECRET=...
```

## 手順

### 1. ネットワークとサーバ

```bash
cd infra/clusters/sakura-k3s
cat > terraform.tfvars <<EOT                 # gitignore 済み
admin_public_key   = "$(cat ~/.ssh/id_ed25519.pub)"
registry_subdomain = "<全 sakuracr.jp で一意なラベル>"   # レジストリ不要なら省略
EOT
terraform init
terraform apply
```

### 2. ノードの SSH 鍵（帯域外の手順）

この例が使う Ubuntu のパブリックアーカイブには cloud-init が無いため、SSH 鍵はディスク作成時の「ディスクの修正」で書き込みます。terraform の disk リソースはこの API を公開していないため、usacloud で作成して import します。

```bash
# 各ノードについて（terraform が作ったディスクを置き換える場合は先に削除）
usacloud disk create -y --zone is1a --name agent-platform-disk-server \
  --disk-plan ssd --connector virtio --size 20 --os-type ubuntu2404 \
  --edit-disk-ssh-key-ids <terraform が登録した SSH 鍵の ID>
terraform import 'sakuracloud_disk.nodes["server"]' <disk id>
```

> ホスト名を「ディスクの修正」で指定するとこのアーカイブでは起動しなくなります。ホスト名は k3s インストール時に設定してください。

### 3. ノードの IP 固定

```bash
terraform output node_macs          # 小文字の MAC
# terraform.tfvars に node_macs = { server = "9c:a3:...", agent-1 = "...", agent-2 = "..." } を追加
terraform apply
```

### 4. k3s のインストール

```bash
terraform output -json k3s_install   # 各ノードで実行するコマンド（join token を含む。扱いに注意）
ssh -p 2222 ubuntu@$(terraform output -raw router_global_ip)          # server
# agent へは server を踏み台にする: ssh -J ubuntu@<router>:2222 ubuntu@192.168.10.12
```

各ノードでホスト名を一意にしてから（`sudo hostnamectl set-hostname k3s-agent-1` など）、server → agent の順に出力されたコマンドを実行します。

### 5. kubectl の接続

```bash
scripts/tunnel.sh &                  # 127.0.0.1:16443 → API サーバ
scripts/fetch-kubeconfig.sh          # ~/.config/agent-platform/kubeconfig-sakura-k3s.yaml
export KUBECONFIG=~/.config/agent-platform/kubeconfig-sakura-k3s.yaml
kubectl get nodes
```

### 6. イメージとプラットフォーム

```bash
docker login $(terraform output -raw registry_fqdn) -u push    # パスワード: terraform output -raw registry_password
REGISTRY=$(terraform output -raw registry_fqdn) ../../../scripts/build-push.sh
```

`infra/platform/terraform.tfvars` の例:

```hcl
kubeconfig_path      = "~/.config/agent-platform/kubeconfig-sakura-k3s.yaml"
registry_server      = "<registry_fqdn>"
registry_username    = "push"
# registry_password は TF_VAR_registry_password で
webhook_service_type = "NodePort"
webhook_node_port    = 30080
```

LLM には同じさくらのクラウドの AI Engine が使えます（`agent_base_url = "https://api.ai.sakura.ad.jp/v1"`、[docs/usage.md §4](../../../docs/usage.md#4-ai-エージェントと-llm-を選ぶ)）。

GitHub App の Webhook URL には `terraform output -raw webhook_url` を設定します（TLS が必要な場合は前段にリバースプロキシを置いてください）。

## 撤去

```bash
(cd ../../platform && terraform destroy)
terraform destroy
```

`terraform destroy` 後にディスクが残ることがあります（usacloud で作成し import していないもの、サーバから外れた状態のもの）。確認して手動で削除してください。

```bash
usacloud disk list --zone is1a | jq '[.[] | select(.Name | startswith("agent-platform"))]'
```

コンテナレジストリのサブドメインは削除後もしばらく予約されたままになります。

ローカルに残るファイル（kubeconfig など）も削除してください: `~/.config/agent-platform/kubeconfig-sakura-k3s.yaml`

## 制約

- ゾーンのリソース上限（スイッチ数など）はアカウントによります。409 `limit_count_in_zone` が出たら別ゾーンを使うか既存リソースを解放してください。
- この構成の VPC ルータはファイアウォールを無効にしています（このアーカイブとルータの組み合わせで、有効時にポート転送が機能しなかったため）。公開されるのは SSH（鍵認証のみ）と webhook ポートだけです。
