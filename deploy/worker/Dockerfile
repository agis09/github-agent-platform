# Worker image: agent-platform control code + the agent CLI(s) on python 3.12.
# The same image runs the orchestrator (webhook receiver, launches Jobs),
# the plan/execute/review Jobs and the Search Gateway.
#
# Runtime assumptions (enforced by the Job manifests / terraform module):
#   - runs as non-root user `worker` (uid/gid 10001)
#   - root filesystem is read-only; writable paths come from emptyDir volumes
#   - egress is limited by NetworkPolicy (model endpoint + DNS by default)
#
# Agent CLIs (select with build args; scripts/build-push.sh forwards them):
#   INSTALL_CODEX=true        codex CLI, tarball verified against npm SRI
#   INSTALL_CLAUDE_CODE=false Claude Code CLI via npm (needs Node.js);
#                             set CLAUDE_CODE_VERSION to an exact version
#
# Base image: python:3.12-slim pinned by digest. Refresh the digest (and the
# codex integrity values) when you upgrade; never use a floating tag.
#   linux/amd64 sha256:2fe5997d249a808b8eeea52c58a1dbffbba28754dc11699ef5c029f2d818ce79
#   linux/arm64 sha256:3949e4271b0a3ff82afac7306764c313dcc8edeeb89c0376a3c2ac6007c66b1d
# (the FROM line pins amd64; use the arm64 digest for arm64 nodes)
FROM python@sha256:2fe5997d249a808b8eeea52c58a1dbffbba28754dc11699ef5c029f2d818ce79 AS base

ARG INSTALL_CODEX=true
# npm SRI integrity of @openai/codex@<version>-linux-{x64,arm64} tarballs.
ARG CODEX_CLI_VERSION=0.154.0
ARG CODEX_X64_INTEGRITY=sha512-a4FI3A8sGtwGrOqltrPbrS2hajrHQG591EwmRfiRoLMb10VxdBtUGW4gu6IJVYENiYGA7k3P4jlRHEoCZU/s9Q==
ARG CODEX_ARM64_INTEGRITY=sha512-KmTCB6ST484zeYlPpKP/K5P/gRaYmt6TihVD+zotoe6O9q0JSBP+FYvCz4A/zZXR7xDOHURTSjHp0sD8wWS0YQ==

ARG INSTALL_CLAUDE_CODE=false
ARG CLAUDE_CODE_VERSION=

# kubectl: the orchestrator role launches Jobs with it.
ARG KUBECTL_VERSION=v1.32.2

# UID/GID are fixed so the manifests can reference runAsUser/fsGroup exactly.
ARG WORKER_UID=10001
ARG WORKER_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tar gzip git bubblewrap curl \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/$(dpkg --print-architecture)/kubectl" -o /usr/local/bin/kubectl \
    && chmod 0555 /usr/local/bin/kubectl \
    && rm -rf /var/lib/apt/lists/* \
    && kubectl version --client 2>/dev/null | head -1

# codex CLI: download the platform tarball directly and verify it against the
# npm SRI integrity hash — no postinstall scripts, no unpinned resolution.
RUN set -eux; \
    if [ "$INSTALL_CODEX" != "true" ]; then echo "skipping codex"; exit 0; fi; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) integrity="$CODEX_X64_INTEGRITY"; tarball="codex-${CODEX_CLI_VERSION}-linux-x64.tgz" ;; \
      arm64) integrity="$CODEX_ARM64_INTEGRITY"; tarball="codex-${CODEX_CLI_VERSION}-linux-arm64.tgz" ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    python -c "import base64,hashlib,sys,urllib.request; t,i=sys.argv[1],sys.argv[2]; d=urllib.request.urlopen('https://registry.npmjs.org/@openai/codex/-/'+t,timeout=120).read(); a,w=i.split('-',1); g=a+'-'+base64.b64encode(hashlib.new(a,d).digest()).decode(); sys.exit('SRI mismatch: '+g) if g!=i else open('/tmp/codex.tgz','wb').write(d); print('verified',t)" "$tarball" "$integrity"; \
    mkdir -p /tmp/codex-pkg; \
    tar -xzf /tmp/codex.tgz -C /tmp/codex-pkg; \
    install -m 0755 /tmp/codex-pkg/package/vendor/*/bin/codex /usr/local/bin/codex; \
    rm -rf /tmp/codex-pkg /tmp/codex.tgz; \
    codex --version

# Claude Code CLI (optional): pinned to an exact npm version.
RUN set -eux; \
    if [ "$INSTALL_CLAUDE_CODE" != "true" ]; then echo "skipping claude code"; exit 0; fi; \
    if [ -z "$CLAUDE_CODE_VERSION" ]; then echo "set CLAUDE_CODE_VERSION (exact version)" >&2; exit 1; fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs npm; \
    npm install -g --ignore-scripts "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"; \
    npm cache clean --force; \
    rm -rf /var/lib/apt/lists/*; \
    claude --version

RUN groupadd -g ${WORKER_GID} worker \
    && useradd -m -u ${WORKER_UID} -g ${WORKER_GID} -s /usr/sbin/nologin worker

# agent-platform: sources + runtime deps only (dev tools stay out).
COPY --chown=root:root pyproject.toml /opt/agent-platform/
COPY --chown=root:root src/ /opt/agent-platform/src/
COPY --chown=root:root schemas/ /opt/agent-platform/schemas/
# Job templates: the orchestrator pod renders deploy/jobs/*.yaml in-cluster.
COPY --chown=root:root deploy/jobs/ /opt/agent-platform/deploy/jobs/
RUN pip install --no-cache-dir "pydantic>=2.8,<3" "pyjwt>=2.14.0" "cryptography>=50.0.1"

ENV PYTHONPATH=/opt/agent-platform/src

# Home must be writable for agent CLI state even with a read-only rootfs;
# the manifests mount emptyDir volumes at /home/worker and /tmp.
USER worker
WORKDIR /home/worker
CMD ["python", "-m", "agent_platform.cli", "--help"]
