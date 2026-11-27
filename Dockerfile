# syntax=docker/dockerfile:1

# The derived image of the tnwks agent platform: OpenClaw at one release, the
# OpenShell CLI, the plugins of plugins.txt, and the npm cache of the plugins
# of official-plugins.txt. One image serves the agent
# pod of each trust domain. Nothing of a trust domain is in it: no config, no
# workspace, no state and no secret. Those come from the ops repository and
# from the state volume at run time. README.md has the image contract.

# The product, at one release and its digest. Renovate moves the tag and the
# digest together. A patch release merges when CI is green; a new monthly line
# waits for the owner.
FROM ghcr.io/openclaw/openclaw:2026.11.10@sha256:230afbdd8344310cac1e2d8e5dfad67315e82f3757692014310e77653639acb0 AS product

# The sandbox layer, at one OpenShell release and its digest. This image is
# the trusted runtime of a sandbox, and its binary reports the release. The
# CLI below comes from the same release, and the test cluster of CI runs the
# chart of the same release. So one Renovate update moves all three.
FROM ghcr.io/nvidia/openshell/sandbox:0.1.9@sha256:e57d07cd6cc1fc314b80368d6b51ec6fbf825fb62cd8f081312941180846866f AS openshell-release

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

# ---- the official plugins ----
# Each package of official-plugins.txt, at the release of the product, in an
# npm cache. The pod installs them from this cache with the installer of the
# product, with no network (image/install-official-plugins): that install
# makes each one a verified official install. This stage runs the same
# installer, with the network, so that the cache holds each request it makes.
# "npm:" names the npm registry: the product takes a bare package name from
# ClawHub.
FROM product AS official-plugins
USER root
COPY official-plugins.txt /opt/openclaw/official-plugins.txt
RUN set -eu; \
    version="$(node -p "require('/app/package.json').version")"; \
    mkdir -p /opt/openclaw/npm-cache /tmp/seed; \
    printf 'cache=/opt/openclaw/npm-cache\n' > /tmp/npmrc; \
    printf '{}\n' > /tmp/seed/openclaw.json; \
    sed -e 's/#.*//' /opt/openclaw/official-plugins.txt | while read -r id package; do \
      [ -n "${id}" ] || continue; \
      OPENCLAW_STATE_DIR=/tmp/seed OPENCLAW_CONFIG_PATH=/tmp/seed/openclaw.json \
      NPM_CONFIG_USERCONFIG=/tmp/npmrc OPENCLAW_NO_AUTO_UPDATE=1 DO_NOT_TRACK=1 \
        node /app/openclaw.mjs plugins install "npm:${package}@${version}" --pin --no-enable; \
    done; \
    rm -rf /tmp/seed /tmp/npmrc /opt/openclaw/npm-cache/_logs \
      /opt/openclaw/npm-cache/_update-notifier-last-checked; \
    chmod -R a+rX,go-w /opt/openclaw/npm-cache

# ---- the host approval plugin ----
# Our one own plugin (plugins/host-approval; Design Doc of effort 0003,
# section 4.2.6, "The prompt, our one own part"). Its unit tests run here
# with the node of the product. A red test, no test file, or no passed test
# stops the build. The image
# gets the plugin without its tests, owned by root and read only. The agent
# config in ops names it: plugins.load.paths holds
# /opt/openclaw/local-plugins/host-approval, and plugins.entries.host-approval
# (with enabled and its config) is the control of its load.
FROM product AS host-approval
USER root
COPY plugins/host-approval /tmp/host-approval
RUN set -eu; \
    cd /tmp/host-approval; \
    ls test/*.test.js >/dev/null; \
    node --test --test-reporter=tap test/*.test.js > /tmp/unit-tests.tap 2>&1 \
      || { cat /tmp/unit-tests.tap; exit 1; }; \
    cat /tmp/unit-tests.tap; \
    grep -Eq '^# pass [1-9]' /tmp/unit-tests.tap; \
    grep -Eq '^# fail 0$' /tmp/unit-tests.tap; \
    rm /tmp/unit-tests.tap; \
    mkdir -p /opt/openclaw/local-plugins/host-approval; \
    cp index.js lib.js openclaw.plugin.json package.json /opt/openclaw/local-plugins/host-approval/; \
    chown -R root:root /opt/openclaw/local-plugins; \
    chmod -R a+rX,go-w /opt/openclaw/local-plugins

# ---- the image ----
FROM product
USER root

COPY --from=openshell-cli /out/openshell /usr/local/bin/openshell
COPY --from=openshell-cli /out/openshell.release /opt/openclaw/openshell.release
COPY --from=plugins /opt/openclaw/plugins /opt/openclaw/plugins
COPY --from=host-approval /opt/openclaw/local-plugins /opt/openclaw/local-plugins
COPY --from=official-plugins /opt/openclaw/npm-cache /opt/openclaw/npm-cache
COPY official-plugins.txt /opt/openclaw/official-plugins.txt
COPY image/install-official-plugins /usr/local/bin/install-official-plugins

# The archive directory of the state volume is outside the state directory,
# so the backup command and the sandbox plugin never copy it. The pod mounts
# the volume over both paths. Here they only get the right owner and mode.
RUN install -d -m 0700 -o 1000 -g 1000 /home/node/.openclaw-archives \
 && openshell --version \
 && test -d /opt/openclaw/plugins/node_modules/@openclaw/openshell-sandbox \
 && chmod 0755 /usr/local/bin/install-official-plugins

# The official plugins step, with no network (npm offline), into a state
# directory that goes away: the cache holds each request of the installer.
RUN set -eu; \
    mkdir -p /tmp/check; \
    printf '{}\n' > /tmp/check/openclaw.json; \
    OPENCLAW_STATE_DIR=/tmp/check OPENCLAW_CONFIG_PATH=/tmp/check/openclaw.json \
    OPENCLAW_NO_AUTO_UPDATE=1 DO_NOT_TRACK=1 install-official-plugins; \
    rm -rf /tmp/check

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
