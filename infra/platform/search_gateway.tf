# In-cluster Search Gateway (docs/design.md "Web search").
#
# Search is opt-in per deployment. search_provider = "none" (default) runs
# no gateway: execute jobs get SEARCH_PROVIDER=none, the prompt does not
# mention search and the worker search CLI refuses every call. Any other
# value names an upstream in src/agent_platform/search/upstreams.py and
# starts the gateway with it:
#
#   fake       offline fixed result (wiring checks; no Internet egress)
#   brave      Brave Search API (needs search_api_key)
#   http-json  your own search API speaking {results:[{title,url,snippet}]}
#              at search_provider_url (search_api_key optional, sent as
#              Authorization: Bearer)
#
# A new provider is added in upstreams.py only; this file needs no change
# (an unknown name makes the gateway exit with the list of known names).
#
# execute pod (agent-platform.search=gateway) -> search-gateway:8080 only.
# The gateway re-runs the sanitizer and calls the upstream. Non-fake
# upstreams get 443 to public addresses; each provider pins its own
# destination host. The worker egress proxy is not shared (the worker
# allowlist must not grow a search provider).

variable "search_provider" {
  description = "Search API for execute jobs: none (search disabled) | fake | brave | http-json | <name registered in search/upstreams.py>"
  type        = string
  default     = "none"
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,31}$", var.search_provider))
    error_message = "search_provider must be a provider name (lowercase letters, digits, hyphens) or none."
  }
}

variable "search_provider_url" {
  description = "Endpoint for providers that take one (http-json: https://...). Leave empty for brave / fake"
  type        = string
  default     = ""
  validation {
    condition     = var.search_provider_url == "" || startswith(var.search_provider_url, "https://")
    error_message = "search_provider_url must be an https:// URL."
  }
}

variable "search_api_key" {
  description = "API key for the selected search provider (brave: required). Stored in the search-api-key Secret; empty = no Secret"
  type        = string
  default     = ""
  sensitive   = true
}

locals {
  search_gateway      = var.search_provider != "none" ? 1 : 0
  search_upstream_net = var.search_provider != "none" && var.search_provider != "fake"
  search_api_key      = var.search_provider != "none" && var.search_api_key != "" ? 1 : 0
  search_gateway_port = 8080
  search_gateway_url  = "http://search-gateway.${local.ns}.svc.cluster.local:${local.search_gateway_port}"
}

resource "kubernetes_secret_v1" "search_api_key" {
  count = local.search_api_key
  metadata {
    name      = "search-api-key"
    namespace = local.ns
  }
  data = {
    api-key = var.search_api_key
  }
}

resource "kubernetes_service_account_v1" "search_gateway" {
  count = local.search_gateway
  metadata {
    name      = "search-gateway"
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

resource "kubernetes_deployment_v1" "search_gateway" {
  count = local.search_gateway
  metadata {
    name      = "search-gateway"
    namespace = local.ns
    labels = {
      app = "search-gateway"
    }
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        app = "search-gateway"
      }
    }
    template {
      metadata {
        labels = {
          app = "search-gateway"
        }
        annotations = {
          # Env from a Secret is read at start: a new key (Secret version)
          # rolls the pod. The version is a counter, not the key.
          "agent-platform/search-api-key-version" = local.search_api_key == 1 ? kubernetes_secret_v1.search_api_key[0].metadata[0].resource_version : ""
        }
      }
      spec {
        service_account_name            = kubernetes_service_account_v1.search_gateway[0].metadata[0].name
        automount_service_account_token = false
        security_context {
          run_as_non_root = true
          run_as_user     = 10001
          run_as_group    = 10001
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }
        container {
          name              = "gateway"
          image             = var.worker_image # same worker image; no new image
          image_pull_policy = "IfNotPresent"
          command           = ["python", "-m", "agent_platform.search_gateway"]
          env {
            name  = "SEARCH_GATEWAY_BIND"
            value = "0.0.0.0"
          }
          env {
            name  = "SEARCH_GATEWAY_PORT"
            value = tostring(local.search_gateway_port)
          }
          env {
            name  = "SEARCH_GATEWAY_UPSTREAM"
            value = var.search_provider
          }
          env {
            name  = "SEARCH_GATEWAY_UPSTREAM_URL"
            value = var.search_provider_url
          }
          dynamic "env" {
            for_each = local.search_api_key == 1 ? [1] : []
            content {
              name = "SEARCH_GATEWAY_API_KEY"
              value_from {
                secret_key_ref {
                  name = kubernetes_secret_v1.search_api_key[0].metadata[0].name
                  key  = "api-key"
                }
              }
            }
          }
          port {
            name           = "http"
            container_port = local.search_gateway_port
          }
          volume_mount {
            name       = "scratch"
            mount_path = "/tmp"
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
            http_get {
              path = "/healthz"
              port = local.search_gateway_port
            }
            period_seconds = 10
          }
          liveness_probe {
            http_get {
              path = "/healthz"
              port = local.search_gateway_port
            }
            initial_delay_seconds = 5
            period_seconds        = 30
          }
        }
        volume {
          name = "scratch"
          empty_dir {
            size_limit = "16Mi"
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "search_gateway" {
  count = local.search_gateway
  metadata {
    name      = "search-gateway"
    namespace = local.ns
  }
  spec {
    selector = {
      app = "search-gateway"
    }
    port {
      name        = "http"
      port        = local.search_gateway_port
      target_port = local.search_gateway_port
    }
  }
}

resource "kubernetes_network_policy_v1" "execute_to_search_gateway" {
  count = local.search_gateway
  metadata {
    name      = "allow-execute-to-search-gateway"
    namespace = local.ns
  }
  spec {
    pod_selector {
      match_labels = {
        "agent-platform.search" = "gateway"
      }
    }
    policy_types = ["Egress"]
    egress {
      to {
        pod_selector {
          match_labels = {
            app = "search-gateway"
          }
        }
      }
      ports {
        protocol = "TCP"
        port     = tostring(local.search_gateway_port)
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "search_gateway" {
  count = local.search_gateway
  metadata {
    name      = "search-gateway"
    namespace = local.ns
  }
  spec {
    pod_selector {
      match_labels = {
        app = "search-gateway"
      }
    }
    policy_types = ["Ingress", "Egress"]
    ingress {
      from {
        pod_selector {
          match_labels = {
            "agent-platform.search" = "gateway"
          }
        }
      }
      ports {
        protocol = "TCP"
        port     = tostring(local.search_gateway_port)
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
    # Upstream egress only for a real provider (fake needs none).
    dynamic "egress" {
      for_each = local.search_upstream_net ? [1] : []
      content {
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
}
