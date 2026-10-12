# Egress proxy: stripe/smokescreen v0.1.0 on distroless static (nonroot).
#
# Upstream publishes container images only from releases after v0.1.0
# (container releases enabled 2026-10-07), so this builds the same layout as
# upstream's Dockerfile from the v0.1.0 release tarball, verified against the
# release's SHA256SUMS (values below copied from
# https://github.com/stripe/smokescreen/releases/download/v0.1.0/SHA256SUMS,
# 2026-10-08). Switch to mirroring ghcr.io/stripe/smokescreen once a release
# with an image exists.
#
# Fetch stage reuses the worker's digest-pinned python base (no new base).
FROM python@sha256:2fe5997d249a808b8eeea52c58a1dbffbba28754dc11699ef5c029f2d818ce79 AS fetch

ARG SMOKESCREEN_VERSION=v0.1.0
ARG SMOKESCREEN_LINUX_AMD64_SHA256=4e1e447449e3cc653dae956208588722b5c8fe8ce894f633adc756a851d1ec46
ARG SMOKESCREEN_LINUX_ARM64_SHA256=f15ddd0dc2b2cbc6d7318f441928939eb9bedfab2a1881508af3096a1e24b7c9
ARG TARGETARCH

RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) sha="$SMOKESCREEN_LINUX_AMD64_SHA256" ;; \
      arm64) sha="$SMOKESCREEN_LINUX_ARM64_SHA256" ;; \
      *) echo "unsupported arch: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    name="smokescreen_${SMOKESCREEN_VERSION}_linux_${TARGETARCH}"; \
    python -c "import hashlib,sys,urllib.request; u,s=sys.argv[1],sys.argv[2]; d=urllib.request.urlopen(u,timeout=120).read(); g=hashlib.sha256(d).hexdigest(); sys.exit('sha256 mismatch: '+g) if g!=s else open('/tmp/s.tgz','wb').write(d); print('verified',u)" \
      "https://github.com/stripe/smokescreen/releases/download/${SMOKESCREEN_VERSION}/${name}.tar.gz" "$sha"; \
    tar -xzf /tmp/s.tgz -C /tmp; \
    install -m 0555 "/tmp/${name}/smokescreen" /smokescreen

# Same base digest as upstream's Dockerfile (master 99be9f5).
FROM gcr.io/distroless/static:nonroot@sha256:e2e927ec666bae08560abb3c55d0659eceabb657f56b6782ab500a9fc7f555e3
COPY --from=fetch /smokescreen /smokescreen
USER 65532:65532
EXPOSE 4750
ENTRYPOINT ["/smokescreen"]
