# Example cluster for the platform: k3s on Sakura Cloud.
#
#   VPC router (global IP, NAT egress)
#     ├─ port forward ssh_public_port -> k3s server :22 (bastion, key auth only)
#     ├─ port forward webhook_public_port -> k3s server :webhook_node_port
#     └─ private switch (switch_cidr)
#          ├─ k3s server   <prefix>.<host>
#          └─ k3s agents   ...
#
# Nodes have no global IP. kubectl reaches the API through an SSH tunnel
# (scripts/tunnel.sh). After the cluster is up, deploy the platform with
# infra/platform (kubeconfig from scripts/fetch-kubeconfig.sh).
#
# NOTE on provisioning: the Ubuntu public archive used here ships without
# cloud-init, so SSH keys are written at disk creation time with the
# create-with-edit API, which the terraform disk resource does not expose.
# README.md describes the out-of-band `usacloud disk create` + import step.

locals {
  network     = cidrhost(var.switch_cidr, 0)
  gateway     = cidrhost(var.switch_cidr, 1)
  node_ip     = { for name, n in var.nodes : name => cidrhost(var.switch_cidr, n.host) }
  server_name = one([for name, n in var.nodes : name if n.role == "server"])
  server_ip   = local.node_ip[local.server_name]
}

resource "sakuracloud_switch" "this" {
  name        = "${var.prefix}-switch"
  description = "agent-platform k3s private switch (${var.switch_cidr})"
}

resource "sakuracloud_vpc_router" "this" {
  name                = "${var.prefix}-router"
  plan                = "standard"
  internet_connection = true

  private_network_interface {
    index        = 1
    switch_id    = sakuracloud_switch.this.id
    ip_addresses = [local.gateway]
    netmask      = 24
  }

  port_forwarding {
    protocol     = "tcp"
    public_port  = var.ssh_public_port
    private_ip   = local.server_ip
    private_port = 22
  }

  # GitHub webhook -> orchestrator NodePort. The boundary is the HMAC
  # signature + the receiver's fail-closed kicker gate (GitHub IPs rotate).
  dynamic "port_forwarding" {
    for_each = var.webhook_public_port > 0 ? [1] : []
    content {
      protocol     = "tcp"
      public_port  = var.webhook_public_port
      private_ip   = local.server_ip
      private_port = var.webhook_node_port
    }
  }

  # Node IPs: DHCP with per-node static MAC mappings (filled via node_macs
  # after the first apply creates the NICs). Pool and mappings must not
  # overlap.
  dhcp_server {
    interface_index = 1
    range_start     = cidrhost(var.switch_cidr, 200)
    range_stop      = cidrhost(var.switch_cidr, 209)
    dns_servers     = var.dns_servers
  }

  dynamic "dhcp_static_mapping" {
    for_each = var.node_macs
    content {
      mac_address = dhcp_static_mapping.value
      ip_address  = local.node_ip[dhcp_static_mapping.key]
    }
  }
}

resource "sakuracloud_ssh_key" "admin" {
  name       = "${var.prefix}-admin-key"
  public_key = var.admin_public_key
}

data "sakuracloud_archive" "ubuntu" {
  os_type = "ubuntu2404"
}

resource "sakuracloud_disk" "nodes" {
  for_each          = var.nodes
  name              = "${var.prefix}-disk-${each.key}"
  plan              = "ssd"
  connector         = "virtio"
  size              = 20
  source_archive_id = data.sakuracloud_archive.ubuntu.id
}

resource "sakuracloud_server" "nodes" {
  for_each   = var.nodes
  name       = "${var.prefix}-${each.key}"
  core       = each.value.core
  memory     = each.value.memory
  commitment = "standard"
  disks      = [sakuracloud_disk.nodes[each.key].id]

  network_interface {
    upstream        = sakuracloud_switch.this.id
    user_ip_address = local.node_ip[each.key]
  }

  timeouts {
    create = "30m"
  }
}

# Private container registry (optional).
resource "sakuracloud_container_registry" "this" {
  count           = var.registry_subdomain != "" ? 1 : 0
  name            = "${var.prefix}-registry"
  subdomain_label = var.registry_subdomain

  user {
    name       = "push"
    password   = random_password.registry[0].result
    permission = "readwrite"
  }
}

resource "random_password" "registry" {
  count   = var.registry_subdomain != "" ? 1 : 0
  length  = 24
  special = false
}

resource "random_password" "k3s_token" {
  length  = 48
  special = false
}
