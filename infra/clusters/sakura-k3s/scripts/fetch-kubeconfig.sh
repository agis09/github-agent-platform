#!/bin/sh
# Fetch the k3s kubeconfig from the server and point it at the local tunnel
# (scripts/tunnel.sh). The file holds the admin client certificate: it is
# written outside the repository with mode 0600. terraform runs this after
# the k3s install (k3s.tf); it can also be run by hand.
#
# Usage: infra/clusters/sakura-k3s/scripts/fetch-kubeconfig.sh [dest]
# Env (optional): ROUTER_IP, SSH_PORT (2222), SSH_KEY (private key path)
set -eu

TFDIR="$(cd "$(dirname "$0")/.." && pwd)"
ROUTER="${ROUTER_IP:-$(terraform -chdir="$TFDIR" output -raw router_global_ip 2>/dev/null || true)}"
PORT="${SSH_PORT:-2222}"
DEST="${1:-$HOME/.config/agent-platform/kubeconfig-sakura-k3s.yaml}"
if [ -z "$ROUTER" ]; then
  echo "router IP not found: apply $TFDIR first or set ROUTER_IP" >&2
  exit 1
fi
set -- -o StrictHostKeyChecking=accept-new -o BatchMode=yes
if [ -n "${SSH_KEY:-}" ]; then
  set -- "$@" -i "$SSH_KEY"
fi
mkdir -p "$(dirname "$DEST")"
chmod 700 "$(dirname "$DEST")"

# k3s.yaml is 0600 root:root; stream it through sudo instead of copying it
# to a temporary file on the node.
umask 077
ssh "$@" -p "$PORT" "ubuntu@$ROUTER" "sudo cat /etc/rancher/k3s/k3s.yaml" > "$DEST.tmp"
# Rewrite whatever https://<addr>:6443 k3s wrote to the tunnel endpoint.
sed -E 's#server: https://[^:]+:6443#server: https://127.0.0.1:16443#' "$DEST.tmp" > "$DEST"
rm -f "$DEST.tmp"
chmod 600 "$DEST"
echo "written: $DEST (server https://127.0.0.1:16443)"
