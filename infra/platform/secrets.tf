# Secrets. Values come from terraform variables (terraform.tfvars or
# TF_VAR_* environment, both kept out of git) and from private key files on
# the operator machine. Note: terraform state contains these values — keep
# the state in a protected backend.

# LLM API key: mounted only into the agent Jobs (secretKeyRef rendered by
# the orchestrator); the orchestrator itself never reads it.
resource "kubernetes_secret_v1" "llm_api_key" {
  metadata {
    name      = "llm-api-key"
    namespace = local.ns
  }
  data = {
    api-key = var.agent_api_key
  }
}

# Image pull secret for a private registry (optional).
resource "kubernetes_secret_v1" "regcred" {
  count = local.image_pull_secret
  metadata {
    name      = "regcred"
    namespace = local.ns
  }
  type = "kubernetes.io/dockerconfigjson"
  data = {
    ".dockerconfigjson" = jsonencode({
      auths = {
        (var.registry_server) = {
          auth = base64encode("${var.registry_username}:${var.registry_password}")
        }
      }
    })
  }
}

locals {
  publisher_app_id          = var.publisher_app_id != "" ? var.publisher_app_id : var.reader_app_id
  publisher_installation_id = var.publisher_installation_id != "" ? var.publisher_installation_id : var.reader_installation_id
  publisher_private_key     = var.publisher_private_key_path != "" ? var.publisher_private_key_path : var.reader_private_key_path
  github_key_dir            = "/home/worker/.config/agent-platform"
}

# GitHub App private keys, mounted read-only (0600) into the orchestrator.
resource "kubernetes_secret_v1" "github_app_keys" {
  metadata {
    name      = "github-app-keys"
    namespace = local.ns
  }
  data = merge(
    {
      "reader.pem"    = file(pathexpand(var.reader_private_key_path))
      "publisher.pem" = file(pathexpand(local.publisher_private_key))
    },
    var.github_ca_bundle_path == "" ? {} : {
      "github-ca.pem" = file(pathexpand(var.github_ca_bundle_path))
    },
  )
}
