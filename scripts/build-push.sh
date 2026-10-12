#!/bin/sh
# Build the worker image and the egress proxy image, push them to your
# container registry, and record the digest-pinned references for terraform.
#
# Prerequisites: docker buildx, and `docker login <registry>` done beforehand
# (credentials are never stored in this repository).
#
# Usage:
#   REGISTRY=registry.example.com/my-org scripts/build-push.sh
#
# Env:
#   REGISTRY             registry + optional namespace (required)
#   TAG                  image tag (default: timestamp; terraform pins the digest)
#   PLATFORM             buildx platform (default: linux/amd64)
#   INSTALL_CODEX        true|false (default: true)  -> codex CLI in the worker
#   INSTALL_CLAUDE_CODE  true|false (default: false) -> Claude Code CLI
#   CLAUDE_CODE_VERSION  exact npm version, required with INSTALL_CLAUDE_CODE=true
#   TFVARS               output file (default: infra/platform/images.auto.tfvars)
#
# Output: TFVARS (gitignored, auto-loaded by terraform) gets worker_image /
# egress_proxy_image as <repo>@sha256:<digest>. Other lines are kept.
set -eu

REGISTRY="${REGISTRY:-}"
if [ -z "$REGISTRY" ]; then
  echo "usage: REGISTRY=<registry>[/<namespace>] $0" >&2
  exit 1
fi
# Unique tag per push: with imagePullPolicy=IfNotPresent a reused tag can run
# a stale image. terraform pins the digest anyway; the tag is for humans.
TAG="${TAG:-$(date +%Y%m%d-%H%M%S)}"
PLATFORM="${PLATFORM:-linux/amd64}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TFVARS="${TFVARS:-$REPO_ROOT/infra/platform/images.auto.tfvars}"

# build_push <repo> <dockerfile> <context> [buildx args...]
#   -> prints <registry>/<repo>@sha256:<digest>
build_push() {
  repo="$1"
  image="$REGISTRY/$repo:$TAG"
  dockerfile="$2"
  context="$3"
  shift 3
  echo "building $image" >&2
  docker buildx build --platform "$PLATFORM" "$@" -t "$image" -f "$dockerfile" "$context" >&2
  docker push "$image" >&2
  # The registry's manifest digest (not the local image id).
  digest="$(docker buildx imagetools inspect "$image" | awk '$1 == "Digest:" { print $2; exit }')"
  case "$digest" in
    sha256:*) ;;
    *) echo "could not read the pushed digest of $image: '$digest'" >&2; exit 1 ;;
  esac
  echo "pushed: $image ($digest)" >&2
  echo "$REGISTRY/$repo@$digest"
}

# set_var <name> <value>: replace or append one line in $TFVARS.
set_var() {
  touch "$TFVARS"
  grep -v "^$1 *=" "$TFVARS" > "$TFVARS.tmp" || true
  printf '%s = "%s"\n' "$1" "$2" >> "$TFVARS.tmp"
  mv "$TFVARS.tmp" "$TFVARS"
  chmod 0600 "$TFVARS"
}

WORKER="$(build_push agent-platform-worker \
  "$REPO_ROOT/deploy/worker/Dockerfile" "$REPO_ROOT" \
  --build-arg "INSTALL_CODEX=${INSTALL_CODEX:-true}" \
  --build-arg "INSTALL_CLAUDE_CODE=${INSTALL_CLAUDE_CODE:-false}" \
  --build-arg "CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION:-}")"
set_var worker_image "$WORKER"

# Built without provenance so an unchanged proxy keeps its digest (a new
# digest would roll the proxy pod on apply).
PROXY="$(build_push agent-platform-egress-proxy \
  "$REPO_ROOT/deploy/egress-proxy/Dockerfile" "$REPO_ROOT/deploy/egress-proxy" \
  --provenance=false)"
set_var egress_proxy_image "$PROXY"

echo "wrote $TFVARS:"
cat "$TFVARS"
