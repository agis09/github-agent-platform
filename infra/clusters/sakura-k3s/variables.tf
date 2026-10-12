variable "zone" {
  description = "Sakura Cloud zone"
  type        = string
  default     = "is1a"
}

variable "prefix" {
  description = "Name prefix for every resource of this stack"
  type        = string
  default     = "agent-platform"
}

variable "admin_public_key" {
  description = "SSH public key written to every node disk at creation (user ubuntu)"
  type        = string
}

variable "admin_private_key_path" {
  description = "Private key matching admin_public_key, used by terraform to install k3s over SSH (read locally, never stored in state)"
  type        = string
  default     = "~/.ssh/id_ed25519"
}

variable "kubeconfig_path" {
  description = "Where terraform writes the cluster kubeconfig (server rewritten to the SSH tunnel endpoint https://127.0.0.1:16443)"
  type        = string
  default     = "~/.config/agent-platform/kubeconfig-sakura-k3s.yaml"
}

variable "switch_cidr" {
  description = "Private switch network (/24)"
  type        = string
  default     = "192.168.10.0/24"
}

variable "nodes" {
  description = "k3s nodes: name -> {host (last octet), role, core, memory}. Exactly one node must have role = server"
  type = map(object({
    host   = number
    role   = string
    core   = optional(number, 2)
    memory = optional(number, 4)
  }))
  default = {
    server  = { host = 11, role = "server" }
    agent-1 = { host = 12, role = "agent" }
    agent-2 = { host = 13, role = "agent" }
  }
  validation {
    condition     = length([for n in var.nodes : n if n.role == "server"]) == 1
    error_message = "exactly one node must have role = server."
  }
}

variable "dns_servers" {
  description = "Resolvers handed out by the router DHCP (defaults: Sakura Internet public resolvers)"
  type        = list(string)
  default     = ["133.242.0.3", "133.242.0.4"]
}

variable "ssh_public_port" {
  description = "Router port forwarded to the k3s server's SSH (bastion)"
  type        = number
  default     = 2222
}

variable "webhook_public_port" {
  description = "Router port forwarded to the orchestrator NodePort (GitHub webhook URL: http://<router ip>:<port>/). 0 = no webhook forward"
  type        = number
  default     = 8080
}

variable "webhook_node_port" {
  description = "NodePort of the orchestrator Service (set webhook_service_type = NodePort and the same webhook_node_port in infra/platform)"
  type        = number
  default     = 30080
}

variable "registry_subdomain" {
  description = "Container registry label (<label>.sakuracr.jp). Globally unique across all sakuracr.jp users. Empty = no registry"
  type        = string
  default     = ""
}

variable "k3s_version" {
  description = "k3s version to install"
  type        = string
  default     = "v1.32.13+k3s1"
}
