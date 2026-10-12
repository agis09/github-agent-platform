#!/bin/sh
# Fetch the k3s kubeconfig from the server and point it at the local tunnel
# (scripts/tunnel.sh). The file holds the admin client certificate: it is
# written outside the repository with mode 0600.
#
# Usage: infra/clusters/sakura-k3s/scripts/fetch-kubeconfig.sh [dest]
set -eu

TFDIR="$(cd "$(dirname "$0")/.." && pwd)"
ROUTER="${ROUTER_IP:-$(terraform -chdir="$TFDIR" output -raw router_global_ip 2>/dev/null || true)}"
PORT="${SSH_PORT:-2222}"
DEST="${1:-$HOME/.config/agent-platform/kubeconfig-sakura-k3s.yaml}"
if [ -z "$ROUTER" ]; then
  echo "router IP not found: apply $TFDIR first or set ROUTER_IP" >&2
  exit 1
fi
mkdir -p "$(dirname "$DEST")"

# k3s.yaml is 0600 root:root; stage a readable copy, copy it, remove it.
ssh -p "$PORT" "ubuntu@$ROUTER" "sudo install -m 0600 -o ubuntu /etc/rancher/k3s/k3s.yaml /tmp/k3s.yaml"
scp -q -P "$PORT" "ubuntu@$ROUTER:/tmp/k3s.yaml" "$DEST"
ssh -p "$PORT" "ubuntu@$ROUTER" "rm -f /tmp/k3s.yaml"
chmod 600 "$DEST"
# Rewrite whatever https://<addr>:6443 k3s wrote to the tunnel endpoint.
sed -i.bak -E 's#server: https://[^:]+:6443#server: https://127.0.0.1:16443#' "$DEST" && rm -f "$DEST.bak"
echo "written: $DEST (server https://127.0.0.1:16443)"
