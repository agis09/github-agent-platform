output "router_global_ip" {
  description = "Global IP of the VPC router (SSH bastion + webhook entry)"
  value       = sakuracloud_vpc_router.this.public_ip
}

output "ssh_command" {
  description = "SSH to the k3s server through the router port forward"
  value       = "ssh -p ${var.ssh_public_port} ubuntu@${sakuracloud_vpc_router.this.public_ip}"
}

output "webhook_url" {
  description = "GitHub App webhook URL"
  value       = var.webhook_public_port > 0 ? "http://${sakuracloud_vpc_router.this.public_ip}:${var.webhook_public_port}/" : ""
}

output "node_ips" {
  description = "Private IPs of the k3s nodes"
  value       = local.node_ip
}

output "node_macs" {
  description = "NIC MACs to copy into var.node_macs (lowercase)"
  value       = { for name, s in sakuracloud_server.nodes : name => lower(s.network_interface[0].mac_address) }
}

output "k3s_install" {
  description = "Commands to run on each node over SSH (server first). Contains the join token"
  sensitive   = true
  value = {
    for name, n in var.nodes : name => (
      n.role == "server"
      ? "curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=${var.k3s_version} K3S_TOKEN=${random_password.k3s_token.result} INSTALL_K3S_EXEC='server --node-ip ${local.node_ip[name]} --advertise-address ${local.node_ip[name]} --tls-san 127.0.0.1 --write-kubeconfig-mode 600 --disable traefik --disable servicelb' sh -"
      : "curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=${var.k3s_version} K3S_URL=https://${local.server_ip}:6443 K3S_TOKEN=${random_password.k3s_token.result} INSTALL_K3S_EXEC='agent --node-ip ${local.node_ip[name]}' sh -"
    )
  }
}

output "registry_fqdn" {
  description = "Container registry host (REGISTRY for scripts/build-push.sh, registry_server for infra/platform)"
  value       = var.registry_subdomain != "" ? sakuracloud_container_registry.this[0].fqdn : ""
}

output "registry_password" {
  description = "Registry user 'push' password (docker login / registry_password)"
  value       = var.registry_subdomain != "" ? random_password.registry[0].result : ""
  sensitive   = true
}
