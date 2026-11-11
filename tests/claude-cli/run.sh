#!/usr/bin/env bash
# The Claude CLI test: OpenClaw's Claude CLI runtime behind a model gateway.
# The workflow claude-cli.yaml runs it in CI only, on the test image with
# Claude Code (tests/claude-cli/Dockerfile).
#
# Usage: tests/claude-cli/run.sh <test image ref>
#
# It makes the test cluster of tests/kind/up.sh with the agent pod of the
# Claude CLI test, runs tests/test_claude_cli.py and the sandbox_exec spike
# (tests/test_sandbox_exec.py), and deletes the cluster.
# Never run it on a workstation that runs a live cluster.
set -euo pipefail

image="${1:?usage: tests/claude-cli/run.sh <test image ref>}"
repo="$(cd "$(dirname "$0")/../.." && pwd)"
export IMAGE_REF="$image"
export PYTHONDONTWRITEBYTECODE=1

if [[ "${CI:-}" != "true" ]]; then
  echo "run.sh: the Claude CLI test runs in CI only" >&2
  exit 1
fi

work="$(mktemp -d "${RUNNER_TEMP:-/tmp}/claude-cli-test.XXXXXX")"
finish() {
  local status=$?
  if ((status != 0)); then
    "$repo/tests/kind/dump.sh" "$work" || true
  fi
  "$repo/tests/kind/down.sh" "$work" || true
  rm -rf "$work"
  exit "$status"
}
trap finish EXIT

IMAGE_TEST_AGENT_MODE=claude-agent "$repo/tests/kind/up.sh" "$image" "$work"

export IMAGE_TEST_KUBECONFIG="$work/kubeconfig"
export IMAGE_TEST_KUBECTL="$work/bin/kubectl"
echo "== the Claude CLI test"
(cd "$repo/tests" && python3 -m unittest -v test_claude_cli test_sandbox_exec)
