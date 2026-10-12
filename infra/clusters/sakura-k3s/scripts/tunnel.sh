#!/bin/sh
# SSH tunnel for kubectl / terraform access to the k3s API. The API is not
# exposed publicly; this forwards 127.0.0.1:16443 to the server's :6443
# through the router port forward. Keep it running while you use kubectl
# or apply infra/platform.
#
# Usage: infra/clusters/sakura-k3s/scripts/tunnel.sh
# Env (optional): ROUTER_IP, SSH_PORT (2222), SSH_KEY (private key path)
set -eu

TFDIR="$(cd "$(dirname "$0")/.." && pwd)"
ROUTER="${ROUTER_IP:-$(terraform -chdir="$TFDIR" output -raw router_global_ip 2>/dev/null || true)}"
PORT="${SSH_PORT:-2222}"
if [ -z "$ROUTER" ]; then
  echo "router IP not found: apply $TFDIR first or set ROUTER_IP" >&2
  exit 1
fi
set -- -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new
if [ -n "${SSH_KEY:-}" ]; then
  set -- "$@" -i "$SSH_KEY"
fi

echo "tunneling 127.0.0.1:16443 -> k3s server:6443 via $ROUTER:$PORT"
exec ssh -N "$@" -L 16443:127.0.0.1:6443 -p "$PORT" "ubuntu@$ROUTER"
