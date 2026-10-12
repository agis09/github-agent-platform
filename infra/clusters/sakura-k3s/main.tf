# Example cluster for the platform: k3s on Sakura Cloud, built by one
# `terraform apply` (network, servers, k3s install, kubeconfig).
#
#   VPC router (global IP, NAT egress)
#     ├─ port forward ssh_public_port -> k3s server :22 (bastion, key auth only)
#     ├─ port forward webhook_public_port -> k3s server :webhook_node_port
#     └─ private switch (switch_cidr)
#          ├─ k3s server   <switch_cidr>.<host>
#          └─ k3s agents   ...
#
# Nodes have no global IP. kubectl reaches the API through an SSH tunnel
# (scripts/tunnel.sh). After the cluster is up, deploy the platform with
# infra/platform (kubeconfig_path output).
#
# Order of creation:
#   switch -> servers (SSH key written by the disk edit at creation; the
#   NIC asks for DHCP) -> router (DHCP static mapping pinned to each
#   server's MAC, port forwards) -> k3s install over SSH (k3s.tf).

locals {
  gateway     = cidrhost(var.switch_cidr, 1)
  netmask     = tonumber(split("/", var.switch_cidr)[1])
  node_ip     = { for name, n in var.nodes : name => cidrhost(var.switch_cidr, n.host) }
  server_name = one([for name, n in var.nodes : name if n.role == "server"])
  server_ip   = local.node_ip[local.server_name]
  agent_names = [for name, n in var.nodes : name if n.role == "agent"]
}

resource "sakuracloud_switch" "this" {
  name        = "${var.prefix}-switch"
  description = "agent-platform k3s private switch (${var.switch_cidr})"
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

# Console login for rescue only (SSH password auth stays disabled).
resource "random_password" "console" {
  length  = 20
  special = false
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

  # The Ubuntu public archive has no cloud-init: the SSH key and the
  # console password are written by the disk edit at creation. Do not set
  # hostname here (it breaks booting on this archive); k3s.tf sets it.
  disk_edit_parameter {
    ssh_key_ids     = [sakuracloud_ssh_key.admin.id]
    password        = random_password.console.result
    disable_pw_auth = true
  }

  timeouts {
    create = "30m"
  }

  lifecycle {
    # The disk edit only runs at creation; a later change must not
    # recreate the node.
    ignore_changes = [disk_edit_parameter]
  }
}

resource "sakuracloud_vpc_router" "this" {
  name                = "${var.prefix}-router"
  plan                = "standard"
  internet_connection = true

  private_network_interface {
    index        = 1
    switch_id    = sakuracloud_switch.this.id
    ip_addresses = [local.gateway]
    netmask      = local.netmask
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

  # Node IPs: DHCP with each server's MAC pinned to its address. The pool
  # and the mappings must not overlap.
  dhcp_server {
    interface_index = 1
    range_start     = cidrhost(var.switch_cidr, 200)
    range_stop      = cidrhost(var.switch_cidr, 209)
    dns_servers     = var.dns_servers
  }

  dynamic "dhcp_static_mapping" {
    for_each = sakuracloud_server.nodes
    content {
      mac_address = lower(dhcp_static_mapping.value.network_interface[0].mac_address)
      ip_address  = local.node_ip[dhcp_static_mapping.key]
    }
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
