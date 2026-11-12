# The bridge spike, for its CI test only (.github/workflows/bridge-spike.yaml).
# Not the image of this repository, and never pushed.

# LiteLLM, for the optional leg client -> LiteLLM -> bridge. The test reads
# this pin; the build does not use the stage.
FROM ghcr.io/berriai/litellm:v1.109.0@sha256:26aecfb02fb17ac6590f0549478561f9fbb3f69314317320b2e32a21beed4006 AS litellm

FROM docker.io/library/node:24-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54 AS bridge
WORKDIR /app
COPY package.json package-lock.json ./
# Exact versions from the lockfile. The SDK's native Claude Code binary comes
# from its optional package for this platform.
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force
COPY bridge.mjs ./
USER 65534:65534
ENV PORT=8080 HOME=/home/bridge
EXPOSE 8080
CMD ["node", "bridge.mjs"]
