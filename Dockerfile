# syntax=docker/dockerfile:1

# The derived image of the tnwks agent platform: OpenClaw at one release, the
# OpenShell CLI, and the plugins of plugins.txt. One image serves the agent
# pod of each trust domain. Nothing of a trust domain is in it: no config, no
# workspace, no state and no secret. Those come from the ops repository and
# from the state volume at run time. README.md has the image contract.

# The product, at one release and its digest. Renovate moves the tag and the
# digest together. A patch release merges when CI is green; a new monthly line
# waits for the owner.
FROM ghcr.io/openclaw/openclaw:2026.11.3@sha256:e73f689d663567f0fe3c5a85c54beb3e102dc21f0814bf375e9039ee2d2df51b AS product

# The sandbox layer, at one OpenShell release and its digest. This image is
# the trusted runtime of a sandbox, and its binary reports the release. The
# CLI below comes from the same release, and the test cluster of CI runs the
# chart of the same release. So one Renovate update moves all three.
FROM ghcr.io/nvidia/openshell/sandbox:0.1.7@sha256:89819c03f291d9b909c3fc2962360e24af4516627422d18f3e681829f003f347 AS openshell-release

# ---- the OpenShell CLI ----
# The OpenClaw plugin calls the CLI to make a sandbox and to reach it over SSH.
# It is the static build from the GitHub release of the same version, checked
# against the checksum file of that release.
FROM product AS openshell-cli
USER root
RUN --mount=type=bind,from=openshell-release,source=/openshell-sandbox,target=/tmp/openshell-sandbox \
    set -eu; \
    version="$(/tmp/openshell-sandbox --version | awk '{print $2}')"; \
    case "$(uname -m)" in \
      x86_64) arch=x86_64 ;; \
      aarch64) arch=aarch64 ;; \
      *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    base="https://github.com/NVIDIA/OpenShell/releases/download/v${version}"; \
    file="openshell-${arch}-unknown-linux-musl.tar.gz"; \
    mkdir -p /tmp/cli /out; \
    cd /tmp/cli; \
    curl -fsSL --retry 3 -o "${file}" "${base}/${file}"; \
    curl -fsSL --retry 3 -o checksums.txt "${base}/openshell-checksums-sha256.txt"; \
    grep -E "  ${file}\$" checksums.txt | sha256sum -c -; \
    tar -xzf "${file}" -C /out openshell; \
    test "$(/out/openshell --version | awk '{print $2}')" = "${version}"; \
    printf '%s %s\n' "${version}" "$(sha256sum "${file}" | awk '{print $1}')" > /out/openshell.release

# ---- the plugins ----
# Each package of plugins.txt, at the release of the product. The npm
# registry does not let a version change after it is published. The lock
# file of the install stays in the image as the record of each package.
FROM product AS plugins
USER root
COPY plugins.txt /tmp/plugins.txt
RUN set -eu; \
    version="$(node -p "require('/app/package.json').version")"; \
    specs="$(sed -e 's/#.*//' -e 's/[[:space:]]//g' /tmp/plugins.txt | grep -v '^$' | sed "s/\$/@${version}/")"; \
    mkdir -p /opt/openclaw/plugins; \
    cd /opt/openclaw/plugins; \
    printf '{"name":"tnwks-agent-plugins","private":true}\n' > package.json; \
    npm install --omit=dev --omit=peer --ignore-scripts --no-audit --no-fund \
      --save-exact --cache /tmp/npm-cache ${specs}; \
    rm -rf /tmp/npm-cache; \
    ln -s /app node_modules/openclaw; \
    chmod -R a+rX,go-w /opt/openclaw/plugins

# ---- the image ----
FROM product
USER root

COPY --from=openshell-cli /out/openshell /usr/local/bin/openshell
COPY --from=openshell-cli /out/openshell.release /opt/openclaw/openshell.release
COPY --from=plugins /opt/openclaw/plugins /opt/openclaw/plugins

# The archive directory of the state volume is outside the state directory,
# so the backup command and the sandbox plugin never copy it. The pod mounts
# the volume over both paths. Here they only get the right owner and mode.
RUN install -d -m 0700 -o 1000 -g 1000 /home/node/.openclaw-archives \
 && openshell --version \
 && test -d /opt/openclaw/plugins/node_modules/@openclaw/openshell-sandbox \
 && test -d /opt/openclaw/plugins/node_modules/@openclaw/brave-plugin

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="tnwks-agent-image" \
      org.opencontainers.image.description="OpenClaw with the OpenShell sandbox plugin, for the tnwks agent platform" \
      org.opencontainers.image.vendor="tnwks" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

# A numeric user, so that Kubernetes can check runAsNonRoot. It is the node
# user of the product image.
USER 1000:1000

# The gateway runs directly under tini. The entrypoint script of the product
# runs `openclaw doctor --fix` first, and doctor writes to the config file.
# The pod mounts the config read only, so it runs doctor as an init container
# on a copy instead (README.md). The Kubernetes manifests of the product also
# start the gateway directly.
ENTRYPOINT ["tini", "-s", "--"]
CMD ["node", "/app/openclaw.mjs", "gateway", "run"]
