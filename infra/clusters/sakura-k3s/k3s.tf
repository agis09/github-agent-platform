# k3s install over SSH, then the kubeconfig fetch.
#
# The server is reached through the router port forward; agents through
# the server as a bastion. Each step re-runs only when its node is
# replaced. The join token is passed in the provisioner script (not
# stored in any trigger); the private key is read locally and never
# written to the state.

locals {
  ssh_private_key = file(pathexpand(var.admin_private_key_path))
  router_ip       = sakuracloud_vpc_router.this.public_ip

  # Wait until the node has its DHCP lease and NAT egress works.
  wait_for_network = "until curl -sfI --max-time 10 https://get.k3s.io >/dev/null; do echo 'waiting for network'; sleep 5; done"
}

resource "terraform_data" "k3s_server" {
  triggers_replace = [sakuracloud_server.nodes[local.server_name].id]

  connection {
    type        = "ssh"
    host        = local.router_ip
    port        = var.ssh_public_port
    user        = "ubuntu"
    private_key = local.ssh_private_key
    timeout     = "20m"
  }

  provisioner "remote-exec" {
    inline = [
      "set -eu",
      local.wait_for_network,
      "sudo hostnamectl set-hostname k3s-${local.server_name}",
      "grep -q 'k3s-${local.server_name}' /etc/hosts || echo '127.0.1.1 k3s-${local.server_name}' | sudo tee -a /etc/hosts >/dev/null",
      "curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION='${var.k3s_version}' K3S_TOKEN='${random_password.k3s_token.result}' INSTALL_K3S_EXEC='server --node-ip ${local.server_ip} --advertise-address ${local.server_ip} --tls-san 127.0.0.1 --write-kubeconfig-mode 600 --disable traefik --disable servicelb' sh -",
      "until sudo k3s kubectl get --raw /readyz >/dev/null 2>&1; do echo 'waiting for k3s'; sleep 5; done",
    ]
  }

  depends_on = [sakuracloud_vpc_router.this]
}

resource "terraform_data" "k3s_agent" {
  for_each = toset(local.agent_names)

  triggers_replace = [
    sakuracloud_server.nodes[each.key].id,
    terraform_data.k3s_server.id,
  ]

  connection {
    type                = "ssh"
    host                = local.node_ip[each.key]
    user                = "ubuntu"
    private_key         = local.ssh_private_key
    bastion_host        = local.router_ip
    bastion_port        = var.ssh_public_port
    bastion_user        = "ubuntu"
    bastion_private_key = local.ssh_private_key
    timeout             = "20m"
  }

  provisioner "remote-exec" {
    inline = [
      "set -eu",
      local.wait_for_network,
      "sudo hostnamectl set-hostname k3s-${each.key}",
      "grep -q 'k3s-${each.key}' /etc/hosts || echo '127.0.1.1 k3s-${each.key}' | sudo tee -a /etc/hosts >/dev/null",
      "until curl -sk --max-time 5 https://${local.server_ip}:6443/ping >/dev/null; do echo 'waiting for k3s server'; sleep 5; done",
      "curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION='${var.k3s_version}' K3S_URL='https://${local.server_ip}:6443' K3S_TOKEN='${random_password.k3s_token.result}' INSTALL_K3S_EXEC='agent --node-ip ${local.node_ip[each.key]}' sh -",
    ]
  }
}

# Copy the admin kubeconfig to this machine (0600, outside the repository)
# with the server rewritten to the SSH tunnel endpoint (scripts/tunnel.sh).
resource "terraform_data" "kubeconfig" {
  triggers_replace = [terraform_data.k3s_server.id, var.kubeconfig_path]

  provisioner "local-exec" {
    command = "${path.module}/scripts/fetch-kubeconfig.sh '${pathexpand(var.kubeconfig_path)}'"
    environment = {
      ROUTER_IP = local.router_ip
      SSH_PORT  = tostring(var.ssh_public_port)
      SSH_KEY   = pathexpand(var.admin_private_key_path)
    }
  }
}
