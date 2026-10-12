terraform {
  required_version = ">= 1.9" # cross-variable validation

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.2"
    }
  }
}

# Any conformant cluster works (EKS / GKE / AKS / k3s / kind ...). The
# provider reads the same kubeconfig kubectl uses; exec credential plugins
# (aws, gke-gcloud-auth-plugin, kubelogin) are supported.
provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = var.kubeconfig_context
}
