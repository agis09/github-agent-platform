terraform {
  required_version = ">= 1.9"

  required_providers {
    sakuracloud = {
      source  = "sacloud/sakuracloud"
      version = "~> 2.36"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# Credentials: SAKURACLOUD_ACCESS_TOKEN / SAKURACLOUD_ACCESS_TOKEN_SECRET in
# the environment (never in a file under git).
provider "sakuracloud" {
  zone = var.zone
}
