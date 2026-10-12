# Kubernetes

- Kubernetes リソースは `infra/platform`（Terraform）で管理する。Job テンプレートだけは `deploy/jobs/`（Orchestrator が実行時に描画）
- 専用 namespace、別 ServiceAccount（orchestrator / worker / search-gateway / egress-proxy）
- Pod Security Standards `restricted`。read-only root filesystem、non-root、capability drop、seccomp
- default-deny の Ingress / Egress NetworkPolicy。許可経路は LLM エンドポイント・DNS・（Orchestrator のみ）GitHub / API サーバ・Search Gateway、および egress レベルに応じた proxy または 443（private CIDR を除く）
- egress レベルはデプロイ単位で 1 つ（既定 `none`）。task・Issue・計画から変えられない。レベルの違う repo は別デプロイ + 別 GitHub App で扱い、1 つの repo に複数の App を install しない
- Job に `activeDeadlineSeconds` と `ttlSecondsAfterFinished` を設定する
- イメージは digest 固定。ResourceQuota と LimitRange を設定する
- Worker Pod に GitHub の secret を mount しない。SA トークンも mount しない
- クラウド固有のコードは `infra/clusters/` に置き、`infra/platform` は kubeconfig 以外を前提にしない
