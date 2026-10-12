# Orchestrator: webhook receiver + control plane. It verifies webhook
# signatures, talks to GitHub with the Reader/Publisher Apps, launches the
# plan/execute/review Jobs with kubectl, and keeps state (SQLite +
# content-addressed artifacts) on a PVC. Single replica (SQLite).

locals {
  job_deadline_seconds = var.agent_timeout_seconds + 300

  orchestrator_env = merge(
    {
      AGENT_PLATFORM_ADAPTER                      = "k8s"
      AGENT_PLATFORM_HOME                         = "/home/worker/.agent-platform"
      AGENT_PLATFORM_WEBHOOK_SECRET               = var.webhook_secret
      AGENT_PLATFORM_K8S_NAMESPACE                = local.ns
      AGENT_PLATFORM_K8S_PLAN_IMAGE               = var.worker_image
      AGENT_PLATFORM_K8S_LLM_SECRET               = kubernetes_secret_v1.llm_api_key.metadata[0].name
      AGENT_PLATFORM_K8S_DEADLINE_SECONDS         = tostring(local.job_deadline_seconds)
      AGENT_PLATFORM_K8S_EXECUTE_DEADLINE_SECONDS = tostring(local.job_deadline_seconds)
      AGENT_PLATFORM_K8S_REVIEW_DEADLINE_SECONDS  = tostring(local.job_deadline_seconds)
      AGENT_PLATFORM_K8S_SNAPSHOT_CONFIGMAP       = var.snapshot_configmap
      AGENT_PLATFORM_K8S_REVIEW_ENABLED           = var.review_enabled ? "1" : "0"
      # Agent runtime settings forwarded into every Job (the key itself is a
      # secretKeyRef to llm-api-key, exposed in the Job as LLM_API_KEY).
      AGENT_PLATFORM_AGENT_RUNTIME         = var.agent_runtime
      AGENT_PLATFORM_AGENT_MODEL           = var.agent_model
      AGENT_PLATFORM_AGENT_BASE_URL        = var.agent_base_url
      AGENT_PLATFORM_AGENT_API_KEY_ENV     = "LLM_API_KEY"
      AGENT_PLATFORM_AGENT_AUTH_SCHEME     = var.agent_auth_scheme
      AGENT_PLATFORM_AGENT_TIMEOUT_SECONDS = tostring(var.agent_timeout_seconds)
      # Reader App (AGENT_PLATFORM_GITHUB_*) and Publisher App.
      AGENT_PLATFORM_GITHUB_API_URL             = var.github_api_url
      AGENT_PLATFORM_GITHUB_APP_ID              = var.reader_app_id
      AGENT_PLATFORM_GITHUB_INSTALLATION_ID     = var.reader_installation_id
      AGENT_PLATFORM_GITHUB_PRIVATE_KEY_PATH    = "${local.github_key_dir}/reader.pem"
      AGENT_PLATFORM_PUBLISHER_API_URL          = var.github_api_url
      AGENT_PLATFORM_PUBLISHER_APP_ID           = local.publisher_app_id
      AGENT_PLATFORM_PUBLISHER_INSTALLATION_ID  = local.publisher_installation_id
      AGENT_PLATFORM_PUBLISHER_PRIVATE_KEY_PATH = "${local.github_key_dir}/publisher.pem"
      # Execute-job egress level (egress.tf). EgressConfig rejects any
      # inconsistent combination at receiver startup.
      AGENT_PLATFORM_EGRESS_LEVEL         = var.egress_level
      AGENT_PLATFORM_EGRESS_PROXY_URL     = var.egress_level == "allowlist" ? local.egress_proxy_url : ""
      AGENT_PLATFORM_EGRESS_ALLOWED_HOSTS = var.egress_level == "allowlist" ? join(",", var.egress_allowed_hosts) : ""
    },
    # merge(): optional keys are left out rather than set to "".
    var.github_ca_bundle_path == "" ? {} : {
      AGENT_PLATFORM_GITHUB_CA_BUNDLE = "${local.github_key_dir}/github-ca.pem"
    },
    var.agent_max_output_tokens == null ? {} : {
      AGENT_PLATFORM_AGENT_MAX_OUTPUT_TOKENS = tostring(var.agent_max_output_tokens)
    },
    local.search_gateway == 1 ? {
      AGENT_PLATFORM_SEARCH_GATEWAY_URL = local.search_gateway_url
    } : {},
    var.s3 == null ? {} : {
      AGENT_PLATFORM_S3_ENDPOINT          = var.s3.endpoint
      AGENT_PLATFORM_S3_BUCKET            = var.s3.bucket
      AGENT_PLATFORM_S3_REGION            = var.s3.region
      AGENT_PLATFORM_S3_ACCESS_KEY_ID     = var.s3.access_key_id
      AGENT_PLATFORM_S3_SECRET_ACCESS_KEY = var.s3.secret_access_key
    },
  )
}

resource "kubernetes_secret_v1" "orchestrator_env" {
  metadata {
    name      = "orchestrator-env"
    namespace = local.ns
  }
  data = local.orchestrator_env
}

resource "kubernetes_config_map_v1" "webhook_config" {
  metadata {
    name      = "webhook-config"
    namespace = local.ns
  }
  data = {
    "webhook_config.json" = jsonencode(var.webhook_config)
  }
}

resource "kubernetes_persistent_volume_claim_v1" "orchestrator_state" {
  metadata {
    name      = "orchestrator-state"
    namespace = local.ns
  }
  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = var.state_storage_class
    resources {
      requests = {
        storage = var.state_storage_size
      }
    }
  }
  # Some provisioners bind on first consumer (WaitForFirstConsumer).
  wait_until_bound = false
}

# --- RBAC: launch/watch Jobs, read their pods' logs. Nothing else. ---

resource "kubernetes_service_account_v1" "orchestrator" {
  metadata {
    name      = "orchestrator"
    namespace = local.ns
  }
  # Off at the SA; the Deployment opts in at the pod level.
  automount_service_account_token = false
  dynamic "image_pull_secret" {
    for_each = local.image_pull_secret == 1 ? [1] : []
    content {
      name = kubernetes_secret_v1.regcred[0].metadata[0].name
    }
  }
}

resource "kubernetes_role_v1" "orchestrator" {
  metadata {
    name      = "orchestrator"
    namespace = local.ns
  }
  rule {
    api_groups = ["batch"]
    resources  = ["jobs"]
    verbs      = ["create", "get", "list", "delete", "patch"]
  }
  rule {
    api_groups = [""]
    resources  = ["pods", "pods/log"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = [""]
    resources  = ["configmaps"]
    verbs      = ["create", "get", "delete"]
  }
}

resource "kubernetes_role_binding_v1" "orchestrator" {
  metadata {
    name      = "orchestrator"
    namespace = local.ns
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.orchestrator.metadata[0].name
    namespace = local.ns
  }
  role_ref {
    kind      = "Role"
    name      = kubernetes_role_v1.orchestrator.metadata[0].name
    api_group = "rbac.authorization.k8s.io"
  }
}

resource "kubernetes_deployment_v1" "orchestrator" {
  metadata {
    name      = "orchestrator"
    namespace = local.ns
    labels = {
      app = "orchestrator"
    }
  }
  spec {
    replicas = 1
    strategy {
      type = "Recreate" # RWO PVC: the new pod must not start before the old one releases it
    }
    selector {
      match_labels = {
        app = "orchestrator"
      }
    }
    template {
      metadata {
        labels = {
          app = "orchestrator"
        }
        annotations = {
          # envFrom does not follow Secret updates: roll the pod on change.
          "agent-platform/env-sha256"    = sha256(jsonencode(local.orchestrator_env))
          "agent-platform/config-sha256" = sha256(kubernetes_config_map_v1.webhook_config.data["webhook_config.json"])
        }
      }
      spec {
        service_account_name            = kubernetes_service_account_v1.orchestrator.metadata[0].name
        automount_service_account_token = true # kubectl (SA token) drives the Jobs
        security_context {
          run_as_non_root = true
          run_as_user     = 10001
          run_as_group    = 10001
          fs_group        = 10001
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }
        container {
          name              = "webhook"
          image             = var.worker_image
          image_pull_policy = "IfNotPresent"
          command           = ["python", "-m", "agent_platform.cli", "webhook", "serve", "--bind", "0.0.0.0", "--port", "8080"]
          port {
            name           = "webhook"
            container_port = 8080
          }
          env_from {
            secret_ref {
              name = kubernetes_secret_v1.orchestrator_env.metadata[0].name
            }
          }
          volume_mount {
            name       = "state"
            mount_path = "/home/worker/.agent-platform"
          }
          volume_mount {
            name       = "scratch"
            mount_path = "/tmp"
          }
          volume_mount {
            name       = "github-keys"
            mount_path = local.github_key_dir
            read_only  = true
          }
          volume_mount {
            name       = "webhook-config"
            mount_path = "/home/worker/.agent-platform/webhook_config.json"
            sub_path   = "webhook_config.json"
            read_only  = true
          }
          resources {
            requests = {
              cpu    = "100m"
              memory = "128Mi"
            }
            limits = {
              cpu    = "500m"
              memory = "512Mi"
            }
          }
          security_context {
            run_as_non_root            = true
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities {
              drop = ["ALL"]
            }
          }
          # exec probe: works even when webhook_ingress_cidrs excludes the
          # node addresses kubelet probes come from.
          liveness_probe {
            initial_delay_seconds = 10
            period_seconds        = 30
            exec {
              command = ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]
            }
          }
        }
        volume {
          name = "state"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim_v1.orchestrator_state.metadata[0].name
          }
        }
        volume {
          name = "scratch"
          empty_dir {}
        }
        volume {
          name = "github-keys"
          secret {
            secret_name  = kubernetes_secret_v1.github_app_keys.metadata[0].name
            default_mode = "0600"
          }
        }
        volume {
          name = "webhook-config"
          config_map {
            name = kubernetes_config_map_v1.webhook_config.metadata[0].name
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "orchestrator" {
  metadata {
    name      = "orchestrator"
    namespace = local.ns
  }
  spec {
    type = var.webhook_service_type
    selector = {
      app = "orchestrator"
    }
    port {
      name        = "webhook"
      port        = 8080
      target_port = "webhook"
      node_port   = var.webhook_service_type == "NodePort" ? var.webhook_node_port : null
    }
  }
}

# --- NetworkPolicies ---

# The API server is reached at its endpoint addresses: NetworkPolicies are
# evaluated after the Service DNAT, so a rule for the ClusterIP never
# matches.
data "kubernetes_endpoints_v1" "apiserver" {
  count = length(var.kube_api_cidrs) == 0 ? 1 : 0
  metadata {
    name      = "kubernetes"
    namespace = "default"
  }
}

locals {
  kube_api_targets = length(var.kube_api_cidrs) > 0 ? [
    for cidr in var.kube_api_cidrs : { cidr = cidr, port = var.kube_api_port }
    ] : flatten([
      for subset in data.kubernetes_endpoints_v1.apiserver[0].subset : [
        for pair in setproduct(subset.address, subset.port) : {
          cidr = "${pair[0].ip}/32"
          port = pair[1].port
        }
      ]
  ])
}

resource "kubernetes_network_policy_v1" "orchestrator_egress" {
  metadata {
    name      = "allow-orchestrator-egress"
    namespace = local.ns
  }
  spec {
    pod_selector {
      match_labels = {
        app = "orchestrator"
      }
    }
    policy_types = ["Egress"]
    dynamic "egress" {
      for_each = local.kube_api_targets
      content {
        to {
          ip_block {
            cidr = egress.value.cidr
          }
        }
        ports {
          protocol = "TCP"
          port     = tostring(egress.value.port)
        }
      }
    }
    # GitHub API + git over HTTPS (+ S3). GitHub's addresses rotate, so the
    # default is any public address; the boundary is the pod selector.
    dynamic "egress" {
      for_each = length(var.orchestrator_egress_cidrs) > 0 ? var.orchestrator_egress_cidrs : ["0.0.0.0/0"]
      content {
        to {
          ip_block {
            cidr   = egress.value
            except = length(var.orchestrator_egress_cidrs) > 0 ? null : local.private_cidrs
          }
        }
        ports {
          protocol = "TCP"
          port     = "443"
        }
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "orchestrator_ingress" {
  metadata {
    name      = "allow-orchestrator-ingress"
    namespace = local.ns
  }
  spec {
    pod_selector {
      match_labels = {
        app = "orchestrator"
      }
    }
    policy_types = ["Ingress"]
    dynamic "ingress" {
      for_each = var.webhook_ingress_cidrs
      content {
        from {
          ip_block {
            cidr = ingress.value
          }
        }
        ports {
          protocol = "TCP"
          port     = "8080"
        }
      }
    }
  }
}
