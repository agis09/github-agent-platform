#!/bin/sh
# Create the repository snapshot ConfigMap the execute Job unpacks into
# /workspace/repo (git bundle of one branch). Prints the ConfigMap name to
# set as snapshot_configmap in infra/platform.
#
# Usage: scripts/create-snapshot.sh <local clone> [branch] [namespace]
#
# Limits: a ConfigMap holds ~1 MiB, so this suits small repositories; the
# bundle must contain the base SHA of every task (re-run after pushes).
set -eu

CLONE="${1:?usage: $0 <local clone> [branch] [namespace]}"
BRANCH="${2:-main}"
NS="${3:-agent-platform}"

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
