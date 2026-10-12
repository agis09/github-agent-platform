# Namespace, worker ServiceAccount, baseline NetworkPolicies and quota.
#
# Baseline: default-deny ingress + egress for every pod in the namespace,
# then DNS for all pods and the model endpoint for the agent Jobs. Each
# long-running component (orchestrator, egress proxy, search gateway) adds
# its own narrow policies in its file.

resource "kubernetes_namespace_v1" "this" {
  metadata {
    name = var.namespace
    labels = {
      "pod-security.kubernetes.io/enforce"         = "restricted"
      "pod-security.kubernetes.io/enforce-version" = var.pod_security_version
      "pod-security.kubernetes.io/audit"           = "restricted"
      "pod-security.kubernetes.io/warn"            = "restricted"
    }
  }
}

locals {
  ns = kubernetes_namespace_v1.this.metadata[0].name

  # Public IPv4 only: RFC 1918, CGNAT and link-local cover the node network,
  # the pod/service CIDRs and cloud metadata endpoints.
  private_cidrs = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"]

  image_pull_secret = var.registry_server != "" ? 1 : 0
}

# The worker never talks to the Kubernetes API: no token is mounted, so a
# compromised agent cannot touch cluster state.
resource "kubernetes_service_account_v1" "worker" {
  metadata {
    name      = "worker"
    namespace = local.ns
  }
  automount_service_account_token = false
  dynamic "image_pull_secret" {
    for_each = local.image_pull_secret == 1 ? [1] : []
    content {
      name = kubernetes_secret_v1.regcred[0].metadata[0].name
    }
  }
}

resource "kubernetes_network_policy_v1" "default_deny" {
  metadata {
    name      = "default-deny"
    namespace = local.ns
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
}

resource "kubernetes_network_policy_v1" "allow_dns" {
  metadata {
    name      = "allow-dns"
    namespace = local.ns
  }
  spec {
    pod_selector {}
    policy_types = ["Egress"]
    egress {
      to {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "kube-system"
          }
        }
      }
      ports {
        protocol = "UDP"
        port     = "53"
      }
      ports {
        protocol = "TCP"
        port     = "53"
      }
    }
  }
}

# Agent Jobs (plan / execute / review) -> model endpoint. With
# llm_egress_cidrs empty this is "any public address on llm_egress_port",
# the only option for SaaS APIs whose addresses rotate; pin CIDRs when the
# endpoint has stable addresses (self-hosted vLLM, private gateway).
resource "kubernetes_network_policy_v1" "agent_to_llm" {
  metadata {
    name      = "allow-agent-to-llm"
    namespace = local.ns
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/part-of" = "agent-platform"
      }
    }
    policy_types = ["Egress"]
    dynamic "egress" {
      for_each = length(var.llm_egress_cidrs) > 0 ? var.llm_egress_cidrs : ["0.0.0.0/0"]
      content {
        to {
          ip_block {
            cidr   = egress.value
            except = length(var.llm_egress_cidrs) > 0 ? null : local.private_cidrs
          }
        }
        ports {
          protocol = "TCP"
          port     = tostring(var.llm_egress_port)
        }
      }
    }
  }
}

resource "kubernetes_resource_quota_v1" "this" {
  metadata {
    name      = "agent-platform-quota"
    namespace = local.ns
  }
  spec {
    hard = var.resource_quota
  }
}

# Keep every container within sane bounds even if a manifest forgets limits.
resource "kubernetes_limit_range_v1" "this" {
  metadata {
    name      = "agent-platform-limits"
    namespace = local.ns
  }
  spec {
    limit {
      type = "Container"
      max = {
        cpu    = "4"
        memory = "4Gi"
      }
      default = {
        cpu    = "1"
        memory = "1Gi"
      }
      default_request = {
        cpu    = "250m"
        memory = "256Mi"
      }
    }
  }
}
