# Execute-job egress level (docs/design.md "Network egress").
#
# One deployment = one level. A repo that needs another level is served by
# another deployment with its own GitHub App; one repo never has two Apps.
#
#   none      -> nothing here; the execute pod keeps the baseline policies
#                (default-deny + model endpoint + DNS in namespace.tf)
#   allowlist -> smokescreen egress proxy + NetworkPolicies: execute pod ->
#                proxy:4750 only; proxy -> DNS + 443 to public addresses
#   full      -> NetworkPolicy: execute pod -> 443 to public addresses
#
# The orchestrator labels the execute pod agent-platform.egress=<level> and
# (allowlist) sets HTTPS_PROXY; the level reaches it via orchestrator_env.

variable "egress_level" {
  description = "Execute-job egress level for this deployment: none | allowlist | full"
  type        = string
  default     = "none"
  validation {
    condition     = contains(["none", "allowlist", "full"], var.egress_level)
    error_message = "egress_level must be none, allowlist or full."
  }
}

variable "egress_allowed_hosts" {
  description = "Hosts the egress proxy may CONNECT to (allowlist only). Exact names or one leading *. wildcard, lowercase punycode"
  type        = list(string)
  default     = []
  validation {
    condition     = (var.egress_level == "allowlist") == (length(var.egress_allowed_hosts) > 0)
    error_message = "egress_allowed_hosts must be non-empty exactly when egress_level is allowlist."
  }
}

variable "egress_proxy_image" {
  description = "Digest-pinned smokescreen image (repo@sha256:...), written to images.auto.tfvars by scripts/build-push.sh. Required for allowlist"
  type        = string
  default     = ""
  validation {
    condition = (
      var.egress_level != "allowlist" || can(regex("@sha256:[0-9a-f]{64}$", var.egress_proxy_image))
    )
    error_message = "egress_level allowlist needs egress_proxy_image pinned by digest (<repo>@sha256:<64 hex>)."
  }
}

locals {
  egress_allowlist = var.egress_level == "allowlist" ? 1 : 0
  egress_full      = var.egress_level == "full" ? 1 : 0
  # Port of the smokescreen listener and the in-cluster URL the execute pod
  # gets as HTTPS_PROXY.
  egress_proxy_port = 4750
  egress_proxy_url  = "http://egress-proxy.${local.ns}.svc.cluster.local:${local.egress_proxy_port}"
  # Public addresses only (local.private_cidrs in namespace.tf):
  # smokescreen refuses internal addresses too; the NetworkPolicy is the
  # L3/L4 backstop.
}

# --- allowlist: smokescreen ---

resource "kubernetes_service_account_v1" "egress_proxy" {
  count = local.egress_allowlist
  metadata {
    name      = "egress-proxy"
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

resource "kubernetes_config_map_v1" "egress_proxy" {
  count = local.egress_allowlist
  metadata {
    name      = "egress-proxy"
    namespace = local.ns
  }
  data = {
    # Main config (smokescreen v0.1.0 YAML loader, strict). No mTLS: one
    # level per deployment, so every client gets the default rule. The ACL
    # is NOT referenced here (acl_file): v0.1.0 loads a config-file ACL
    # before --disable-acl-policy-action is applied, so an open ACL would
    # start (verified with v0.1.0). The ACL goes through --egress-acl-file.
    "config.yaml" = yamlencode({
      ip                             = "0.0.0.0"
      port                           = local.egress_proxy_port
      allow_missing_role             = true
      connect_timeout                = "10s"
      max_concurrent_connect_tunnels = 32
      max_request_rate               = 20
    })
    "acl.yaml" = yamlencode({
      version  = "v1"
      services = []
      default = {
        name            = "default"
        project         = "agent-platform"
        action          = "enforce"
        allowed_domains = var.egress_allowed_hosts
      }
    })
  }
}

resource "kubernetes_deployment_v1" "egress_proxy" {
  count = local.egress_allowlist
  metadata {
    name      = "egress-proxy"
    namespace = local.ns
    labels = {
      app = "egress-proxy"
    }
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        app = "egress-proxy"
      }
    }
    template {
      metadata {
        labels = {
          app = "egress-proxy"
        }
        annotations = {
          # Roll the pod when the ACL changes (smokescreen reads it at start).
          "agent-platform/config-sha256" = sha256(jsonencode(kubernetes_config_map_v1.egress_proxy[0].data))
        }
      }
      spec {
        service_account_name            = kubernetes_service_account_v1.egress_proxy[0].metadata[0].name
        automount_service_account_token = false
        security_context {
          run_as_non_root = true
          run_as_user     = 65532
          run_as_group    = 65532
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }
        container {
          name              = "smokescreen"
          image             = var.egress_proxy_image
          image_pull_policy = "IfNotPresent"
          args = [
            "--config-file", "/config/config.yaml",
            "--egress-acl-file", "/config/acl.yaml",
            # Enforce only: an ACL edited to open/report fails at startup
            # (fatal "utilizes a disabled policy", verified with v0.1.0).
            "--disable-acl-policy-action", "open",
            "--disable-acl-policy-action", "report",
          ]
          port {
            name           = "proxy"
            container_port = local.egress_proxy_port
          }
          volume_mount {
            name       = "config"
            mount_path = "/config"
            read_only  = true
          }
          resources {
            requests = {
              cpu                 = "50m"
              memory              = "64Mi"
              "ephemeral-storage" = "16Mi"
            }
            limits = {
              cpu                 = "500m"
              memory              = "256Mi"
              "ephemeral-storage" = "64Mi"
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
          readiness_probe {
            tcp_socket {
              port = local.egress_proxy_port
            }
            period_seconds = 10
          }
          liveness_probe {
            tcp_socket {
              port = local.egress_proxy_port
            }
            initial_delay_seconds = 5
            period_seconds        = 30
          }
        }
        volume {
          name = "config"
          config_map {
            name = kubernetes_config_map_v1.egress_proxy[0].metadata[0].name
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "egress_proxy" {
  count = local.egress_allowlist
  metadata {
    name      = "egress-proxy"
    namespace = local.ns
  }
  spec {
    selector = {
      app = "egress-proxy"
    }
    port {
      name        = "proxy"
      port        = local.egress_proxy_port
      target_port = local.egress_proxy_port
    }
  }
}

# Execute pods at allowlist reach the proxy and nothing new besides.
resource "kubernetes_network_policy_v1" "execute_to_egress_proxy" {
  count = local.egress_allowlist
  metadata {
    name      = "allow-execute-to-egress-proxy"
    namespace = local.ns
  }
  spec {
    pod_selector {
      match_labels = {
        "agent-platform.egress" = "allowlist"
      }
    }
    policy_types = ["Egress"]
    egress {
      to {
        pod_selector {
          match_labels = {
            app = "egress-proxy"
          }
        }
      }
      ports {
        protocol = "TCP"
        port     = tostring(local.egress_proxy_port)
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "egress_proxy" {
  count = local.egress_allowlist
  metadata {
    name      = "egress-proxy"
    namespace = local.ns
  }
  spec {
    pod_selector {
      match_labels = {
        app = "egress-proxy"
      }
    }
    policy_types = ["Ingress", "Egress"]
    ingress {
      from {
        pod_selector {
          match_labels = {
            "agent-platform.egress" = "allowlist"
          }
        }
      }
      ports {
        protocol = "TCP"
        port     = tostring(local.egress_proxy_port)
      }
    }
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
    # 443 only (smokescreen's ACL cannot restrict ports), public only.
    egress {
      to {
        ip_block {
          cidr   = "0.0.0.0/0"
          except = local.private_cidrs
        }
      }
      ports {
        protocol = "TCP"
        port     = "443"
      }
    }
  }
}

# --- full: 443 to public addresses straight from the execute pod ---

resource "kubernetes_network_policy_v1" "execute_full_egress" {
  count = local.egress_full
  metadata {
    name      = "allow-execute-full-egress"
    namespace = local.ns
  }
  spec {
    pod_selector {
      match_labels = {
        "agent-platform.egress" = "full"
      }
    }
    policy_types = ["Egress"]
    egress {
      to {
        ip_block {
          cidr   = "0.0.0.0/0"
          except = local.private_cidrs
        }
      }
      ports {
        protocol = "TCP"
        port     = "443"
      }
    }
  }
}
