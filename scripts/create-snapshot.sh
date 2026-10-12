#!/bin/sh
# Create the repository snapshot ConfigMap the execute Job unpacks into
# /workspace/repo (git bundle of one branch). Prints the ConfigMap name to
# set as snapshot_configmap in infra/platform.
#
# Usage: scripts/create-snapshot.sh <local clone> [branch] [namespace]
#
# kubectl must point at the target cluster: set KUBECONFIG to the same
# kubeconfig as kubeconfig_path in infra/platform (for the Sakura Cloud k3s
# example, keep its SSH tunnel running). The namespace must already exist
# (terraform apply -target=kubernetes_namespace_v1.this).
#
# Limits: a ConfigMap holds ~1 MiB, so this suits small repositories; the
# bundle must contain the base SHA of every task (re-run after pushes).
set -eu

CLONE="${1:?usage: $0 <local clone> [branch] [namespace]}"
BRANCH="${2:-main}"
NS="${3:-agent-platform}"

# Fail early with the cause instead of kubectl's generic errors (an empty
# kubeconfig makes kubectl fall back to http://localhost:8080).
command -v kubectl >/dev/null 2>&1 || { echo "kubectl not found" >&2; exit 1; }
SERVER="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || true)"
if [ -z "$SERVER" ]; then
  echo "kubectl has no current cluster (KUBECONFIG=${KUBECONFIG:-~/.kube/config})." >&2
  echo "Set KUBECONFIG to the kubeconfig of the target cluster (kubeconfig_path in infra/platform)," >&2
  echo "e.g. export KUBECONFIG=~/.config/agent-platform/kubeconfig-sakura-k3s.yaml" >&2
  exit 1
fi
if ! ERR="$(kubectl get namespace "$NS" --request-timeout=15s -o name 2>&1 >/dev/null)"; then
  case "$ERR" in
    *NotFound*|*"not found"*)
      echo "namespace $NS does not exist on $SERVER." >&2
      echo "Create it first: (cd infra/platform && terraform apply -target=kubernetes_namespace_v1.this)" >&2 ;;
    *)
      echo "cannot reach the cluster at $SERVER: $(printf '%s\n' "$ERR" | tail -n 1)" >&2
      echo "Check KUBECONFIG and, for the Sakura Cloud k3s example, that scripts/tunnel.sh is running." >&2 ;;
  esac
  exit 1
fi

HEAD_SHA="$(git -C "$CLONE" rev-parse "$BRANCH")"
NAME="repo-snapshot-$(echo "$HEAD_SHA" | cut -c1-12)"
BUNDLE="$(mktemp "${TMPDIR:-/tmp}/snapshot-XXXXXX")"
trap 'rm -f "$BUNDLE"' EXIT

git -C "$CLONE" bundle create "$BUNDLE" "$BRANCH" >/dev/null 2>&1
SIZE="$(wc -c < "$BUNDLE" | tr -d ' ')"
if [ "$SIZE" -gt 900000 ]; then
  echo "bundle is $SIZE bytes: too large for a ConfigMap" >&2
  exit 1
fi
kubectl -n "$NS" create configmap "$NAME" \
  --from-file=snapshot.bundle="$BUNDLE" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "$NAME"
