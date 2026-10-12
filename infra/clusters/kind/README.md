# kind（ローカル検証用クラスタ）

ローカルの Docker 上に、NetworkPolicy を強制するクラスタを作ります。

```bash
kind create cluster --name agent-platform --config infra/clusters/kind/kind-config.yaml
# 既定 CNI を無効化しているので Calico を入れる（NetworkPolicy の強制のため）
kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.29.1/manifests/calico.yaml
kubectl -n kube-system rollout status ds/calico-node --timeout=300s
```

イメージはレジストリを経由せず kind に直接読み込めます。

```bash
docker build -t agent-platform-worker:dev -f deploy/worker/Dockerfile .
kind load docker-image agent-platform-worker:dev --name agent-platform
# worker_image は digest 固定が必須なので、読み込んだイメージの ID ではなく
# レジストリに push した digest を使うか、ローカルレジストリ（kind の local registry 構成）を使ってください。
```

`infra/platform` の設定例:

```hcl
kubeconfig_context   = "kind-agent-platform"
webhook_service_type = "NodePort"
webhook_node_port    = 30080      # ホストの localhost:8080 に転送される
```

GitHub から webhook を受けるには、`localhost:8080` を公開するトンネル（リバースプロキシやトンネルサービス）が必要です。webhook なしでも、Orchestrator Pod 内で `agentctl` を直接実行して動作を確認できます（[usage.md の運用](../../../docs/usage.md#10-運用)）。
