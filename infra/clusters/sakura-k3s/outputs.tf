output "router_global_ip" {
  description = "Global IP of the VPC router (SSH bastion + webhook entry)"
  value       = sakuracloud_vpc_router.this.public_ip
}

output "ssh_command" {
  description = "SSH to the k3s server through the router port forward"
  value       = "ssh -i ${var.admin_private_key_path} -p ${var.ssh_public_port} ubuntu@${sakuracloud_vpc_router.this.public_ip}"
}

output "kubeconfig_path" {
  description = "kubeconfig for infra/platform (kubeconfig_path). Needs scripts/tunnel.sh running"
  value       = var.kubeconfig_path
}

output "tunnel_command" {
  description = "Start the SSH tunnel to the k3s API (keep it running while using kubectl / infra/platform)"
  value       = "SSH_KEY=${var.admin_private_key_path} ${path.module}/scripts/tunnel.sh"
}

output "webhook_url" {
  description = "GitHub App webhook URL"
  value       = var.webhook_public_port > 0 ? "http://${sakuracloud_vpc_router.this.public_ip}:${var.webhook_public_port}/" : ""
}

output "node_ips" {
  description = "Private IPs of the k3s nodes"
  value       = local.node_ip
}

output "console_password" {
  description = "Password of user ubuntu for the Sakura Cloud console (rescue only; SSH password auth is disabled)"
  value       = random_password.console.result
  sensitive   = true
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
