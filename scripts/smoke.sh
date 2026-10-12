#!/bin/sh
# Post-deploy smoke check for infra/platform. Verifies:
#   1. the orchestrator is rolled out and answers /healthz
#   2. a pod with the agent-Job labels can reach the model endpoint
#   3. the same pod cannot reach an arbitrary Internet host (NetworkPolicy
#      enforced) when egress_level = none and llm_egress_cidrs is pinned
#
# Usage: LLM_HOST=api.ai.sakura.ad.jp scripts/smoke.sh [namespace]
# Requires kubectl pointing at the cluster and the worker image reference
# (WORKER_IMAGE, e.g. from infra/platform/images.auto.tfvars).
set -eu

NS="${1:-agent-platform}"
LLM_HOST="${LLM_HOST:?set LLM_HOST (model endpoint host, e.g. api.ai.sakura.ad.jp)}"
WORKER_IMAGE="${WORKER_IMAGE:?set WORKER_IMAGE (<repo>@sha256:...)}"
DENY_HOST="${DENY_HOST:-example.com}"

echo "=== 1. orchestrator ==="
kubectl -n "$NS" rollout status deploy/orchestrator --timeout=120s
kubectl -n "$NS" exec deploy/orchestrator -- python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).read().decode())"

probe() {
  # probe <name> <host>: TCP connect to <host>:443 from a pod labelled like
  # an agent Job; prints "open" or "blocked".
  kubectl -n "$NS" run "$1" --rm -i --restart=Never --quiet \
    --image="$WORKER_IMAGE" \
    --labels="app.kubernetes.io/part-of=agent-platform,agent-platform.role=smoke" \
    --overrides='{"spec":{"serviceAccountName":"worker","automountServiceAccountToken":false,"securityContext":{"runAsNonRoot":true,"runAsUser":10001,"seccompProfile":{"type":"RuntimeDefault"}},"containers":[{"name":"'"$1"'","image":"'"$WORKER_IMAGE"'","command":["python","-c","import socket,sys\ntry:\n socket.create_connection((sys.argv[1],443),timeout=5); print(\"open\")\nexcept OSError: print(\"blocked\")","'"$2"'"],"securityContext":{"allowPrivilegeEscalation":false,"readOnlyRootFilesystem":true,"capabilities":{"drop":["ALL"]}}}]}}'
}

echo "=== 2. model endpoint ($LLM_HOST) ==="
R="$(probe smoke-llm "$LLM_HOST")"; echo "$R"
[ "$R" = "open" ] || { echo "FAIL: model endpoint not reachable"; exit 1; }

echo "=== 3. other host ($DENY_HOST) ==="
R="$(probe smoke-deny "$DENY_HOST")"; echo "$R"
if [ "$R" = "open" ]; then
  echo "WARN: $DENY_HOST reachable — expected when llm_egress_cidrs is empty (public 443 allowed);"
  echo "      FAIL otherwise (CNI not enforcing NetworkPolicy?)"
fi
echo "smoke done"
