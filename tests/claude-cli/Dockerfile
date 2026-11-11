# syntax=docker/dockerfile:1

# A test layer for the Claude CLI runtime test only. It adds Claude Code to the
# image under test. The released image does not hold Claude Code: that is the
# owner's call (a proprietary binary in a public image). Nothing pushes this
# image. The workflow .github/workflows/claude-cli.yaml passes the exact
# version, where Renovate sees it.
ARG IMAGE=tnwks-agent-image:ci
FROM ${IMAGE}
ARG CLAUDE_CODE_VERSION
USER root
# The package links the native build for this platform (an optional
# dependency of the same version) in its postinstall step. The npm of the
# product blocks install scripts unless they are allowed by name.
RUN set -eu; \
    test -n "${CLAUDE_CODE_VERSION}"; \
    npm install -g --no-audit --no-fund --cache /tmp/npm-cache \
      --allow-scripts=@anthropic-ai/claude-code \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"; \
    rm -rf /tmp/npm-cache; \
    test "$(claude --version | awk '{print $1}')" = "${CLAUDE_CODE_VERSION}"
USER 1000:1000
