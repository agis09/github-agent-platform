output "namespace" {
  description = "Namespace of the platform"
  value       = local.ns
}

output "webhook_service" {
  description = "In-cluster address of the webhook receiver (expose it via Ingress/LoadBalancer/tunnel; any POST path is accepted, so the public URL is the GitHub App webhook URL)"
  value       = "http://orchestrator.${local.ns}.svc.cluster.local:8080"
}

output "search_gateway_url" {
  description = "In-cluster Search Gateway URL (empty when search_provider = none)"
  value       = local.search_gateway == 1 ? local.search_gateway_url : ""
}

output "egress_level" {
  description = "Execute-job egress level of this deployment"
  value       = var.egress_level
}
