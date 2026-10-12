# --- cluster access ---

variable "kubeconfig_path" {
  description = "kubeconfig used by terraform (not deployed anywhere)"
  type        = string
  default     = "~/.kube/config"
}

variable "kubeconfig_context" {
  description = "kubeconfig context; null = current context"
  type        = string
  default     = null
}

variable "namespace" {
  description = "Namespace for the whole platform (orchestrator, Jobs, gateways)"
  type        = string
  default     = "agent-platform"
}

variable "pod_security_version" {
  description = "Pod Security Standards version label for the namespace (e.g. v1.32 or latest)"
  type        = string
  default     = "latest"
}

# --- images ---

variable "worker_image" {
  description = "Worker image (orchestrator, Jobs and Search Gateway), pinned by digest: <repo>@sha256:<64 hex>. Written to images.auto.tfvars by scripts/build-push.sh"
  type        = string
  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.worker_image))
    error_message = "worker_image must be pinned by digest (<repo>@sha256:<64 hex>)."
  }
}

variable "registry_server" {
  description = "Private registry host for the image pull secret (e.g. ghcr.io). Empty = images are pullable without credentials"
  type        = string
  default     = ""
}

variable "registry_username" {
  description = "Registry pull user (used only when registry_server is set)"
  type        = string
  default     = ""
}

variable "registry_password" {
  description = "Registry pull password / token (used only when registry_server is set)"
  type        = string
  default     = ""
  sensitive   = true
}

# --- agent runtime / LLM ---

variable "agent_runtime" {
  description = "Coding-agent CLI the Jobs run: codex | claude-code (the CLI must be in worker_image)"
  type        = string
  default     = "codex"
  validation {
    condition     = contains(["codex", "claude-code"], var.agent_runtime)
    error_message = "agent_runtime must be codex or claude-code."
  }
}

variable "agent_model" {
  description = "Model name passed to the agent CLI (empty = the CLI's default)"
  type        = string
  default     = ""
}

variable "agent_base_url" {
  description = "Model endpoint. codex: OpenAI Responses-compatible base URL (Sakura AI Engine: https://api.ai.sakura.ad.jp/v1); claude-code: Anthropic-compatible base URL (Sakura AI Engine: https://api.ai.sakura.ad.jp). Empty = vendor API"
  type        = string
  default     = ""
  validation {
    condition     = var.agent_base_url == "" || can(regex("^https?://", var.agent_base_url))
    error_message = "agent_base_url must be an http(s) URL or empty."
  }
}

variable "agent_auth_scheme" {
  description = "claude-code only: how the key is sent. api-key = x-api-key header (Anthropic API); bearer = Authorization: Bearer (Sakura AI Engine and other gateways). codex always sends Bearer"
  type        = string
  default     = "api-key"
  validation {
    condition     = contains(["api-key", "bearer"], var.agent_auth_scheme)
    error_message = "agent_auth_scheme must be api-key or bearer."
  }
}

variable "agent_api_key" {
  description = "API key for the model endpoint. Stored in the llm-api-key Secret and injected into the Jobs only"
  type        = string
  sensitive   = true
}

variable "agent_timeout_seconds" {
  description = "Budget for one agent run. Job deadlines are this + 300s"
  type        = number
  default     = 3600
}

variable "agent_max_output_tokens" {
  description = "codex only: model_max_output_tokens (null = model default)"
  type        = number
  default     = null
}

variable "llm_egress_cidrs" {
  description = "CIDRs of the model endpoint the plan/execute/review Jobs may reach on llm_egress_port. Empty = any public IPv4 address (use for SaaS APIs whose IPs rotate)"
  type        = list(string)
  default     = []
}

variable "llm_egress_port" {
  description = "TCP port of the model endpoint"
  type        = number
  default     = 443
}

# --- GitHub ---

variable "github_api_url" {
  description = "GitHub REST API base URL: https://api.github.com or https://<ghes-host>/api/v3"
  type        = string
  default     = "https://api.github.com"
}

variable "github_ca_bundle_path" {
  description = "PEM CA bundle for a GitHub Enterprise Server with a private CA (path on the terraform machine). Empty = system CAs"
  type        = string
  default     = ""
}

variable "reader_app_id" {
  description = "Reader GitHub App ID (read-only: issues, contents, pull requests, metadata)"
  type        = string
}

variable "reader_installation_id" {
  description = "Reader GitHub App installation ID"
  type        = string
}

variable "reader_private_key_path" {
  description = "Path to the Reader App private key PEM on the machine running terraform"
  type        = string
}

variable "publisher_app_id" {
  description = "Publisher GitHub App ID (contents/pull requests/issues/checks: write). Empty = reuse the Reader App (single-App setup)"
  type        = string
  default     = ""
}

variable "publisher_installation_id" {
  description = "Publisher GitHub App installation ID. Empty = reuse the Reader App"
  type        = string
  default     = ""
}

variable "publisher_private_key_path" {
  description = "Path to the Publisher App private key PEM. Empty = reuse the Reader App key"
  type        = string
  default     = ""
}

# --- webhook receiver ---

variable "webhook_secret" {
  description = "GitHub App webhook secret (HMAC-SHA256 signature check)"
  type        = string
  sensitive   = true
}

variable "webhook_config" {
  description = "Webhook trigger configuration (docs/configuration.md)"
  type = object({
    trigger         = optional(string, "comment")
    command         = optional(string, "/agent-platform run")
    kicker_policy   = optional(string, "collaborator")
    allowed_kickers = optional(list(string), [])
    approvers       = optional(list(string), [])
    auto_pipeline   = optional(bool, true)
  })
  default = {}
}

variable "webhook_service_type" {
  description = "Service type exposing the webhook receiver: ClusterIP (put your own Ingress/tunnel in front) | NodePort | LoadBalancer"
  type        = string
  default     = "ClusterIP"
  validation {
    condition     = contains(["ClusterIP", "NodePort", "LoadBalancer"], var.webhook_service_type)
    error_message = "webhook_service_type must be ClusterIP, NodePort or LoadBalancer."
  }
}

variable "webhook_node_port" {
  description = "Fixed nodePort when webhook_service_type is NodePort (null = assigned by Kubernetes)"
  type        = number
  default     = null
}

variable "webhook_ingress_cidrs" {
  description = "Sources allowed to reach the webhook port. GitHub delivery IPs rotate; the boundary is the HMAC signature + the kicker gate"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

# --- orchestrator ---

variable "state_storage_class" {
  description = "StorageClass for the orchestrator state PVC (SQLite + local artifacts). null = cluster default"
  type        = string
  default     = null
}

variable "state_storage_size" {
  description = "Size of the orchestrator state PVC"
  type        = string
  default     = "5Gi"
}

variable "orchestrator_egress_cidrs" {
  description = "Destinations the orchestrator may reach on 443 (GitHub API + git, S3). Empty = any public IPv4 address. Set this for GitHub Enterprise Server / S3 on a private network"
  type        = list(string)
  default     = []
}

variable "kube_api_cidrs" {
  description = "Kubernetes API server endpoint CIDRs the orchestrator may reach. Empty = read from the default/kubernetes Endpoints (NetworkPolicies match post-DNAT addresses, so the ClusterIP alone does not work)"
  type        = list(string)
  default     = []
}

variable "kube_api_port" {
  description = "API server endpoint port (used with kube_api_cidrs; otherwise read from the Endpoints)"
  type        = number
  default     = 6443
}

variable "snapshot_configmap" {
  description = "ConfigMap holding the repository git bundle the execute Job unpacks (scripts/create-snapshot.sh). Empty = execute runs without a snapshot and fails its base-SHA check"
  type        = string
  default     = ""
}

variable "review_enabled" {
  description = "Run the independent Review Job after publish"
  type        = bool
  default     = true
}

variable "s3" {
  description = "Optional S3-compatible artifact store (null = local PVC store)"
  type = object({
    endpoint          = string
    bucket            = string
    region            = optional(string, "us-east-1")
    access_key_id     = string
    secret_access_key = string
  })
  default   = null
  sensitive = true
}

# --- quota ---

variable "resource_quota" {
  description = "ResourceQuota hard limits for the namespace"
  type        = map(string)
  default = {
    "pods"                       = "8"
    "requests.cpu"               = "4"
    "requests.memory"            = "8Gi"
    "limits.cpu"                 = "8"
    "limits.memory"              = "16Gi"
    "requests.ephemeral-storage" = "8Gi"
    "limits.ephemeral-storage"   = "8Gi"
    "count/jobs.batch"           = "8"
  }
}
