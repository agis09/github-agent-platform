# infra/platform

任意の Kubernetes クラスタに github-agent-platform をデプロイする Terraform です。手順は [docs/usage.md](../../docs/usage.md#8-プラットフォームをデプロイする)、変数は [docs/configuration.md](../../docs/configuration.md#terraform-変数infraplatform) を参照してください。

```bash
cp terraform.tfvars.example terraform.tfvars   # secret 以外（gitignore 済み）。例はさくらの AI Engine
set -a; . ../../.env; set +a                    # secret（TF_VAR_agent_api_key など、.env.example 参照）
terraform init
terraform apply -target=kubernetes_namespace_v1.this   # 初回のみ（スナップショット作成のため）
../../scripts/create-snapshot.sh <clone> main agent-platform
terraform apply
```

| ファイル | 内容 |
|---|---|
| `namespace.tf` | Namespace（PSS restricted）、worker SA、ベースライン NetworkPolicy、Quota |
| `secrets.tf` | LLM キー、imagePullSecret、GitHub App 鍵 |
| `orchestrator.tf` | Orchestrator（webhook 受信 + 制御面）一式 |
| `egress.tf` | `egress_level` に応じた egress proxy |
| `search_gateway.tf` | `search_provider` に応じた Search Gateway |
