#!/usr/bin/env bash
# The bridge spike: a Messages API bridge to the Claude Agent SDK, on the test
# cluster. The workflow bridge-spike.yaml runs it in CI only.
#
# Usage: tests/bridge-spike/run.sh <agent image ref> <bridge image ref>
#
# It makes the test cluster of tests/kind/up.sh with the bridge, LiteLLM and
# the agent pod of the spike, runs tests/test_bridge_spike.py with the Python
# of BRIDGE_PYTHON (it needs the `anthropic` package of requirements.txt), and
# deletes the cluster. Never run it on a workstation that runs a live cluster.
set -euo pipefail

image="${1:?usage: tests/bridge-spike/run.sh <agent image ref> <bridge image ref>}"
bridge="${2:?usage: tests/bridge-spike/run.sh <agent image ref> <bridge image ref>}"
python="${BRIDGE_PYTHON:-python3}"
repo="$(cd "$(dirname "$0")/../.." && pwd)"
export IMAGE_REF="$image" BRIDGE_IMAGE="$bridge"
export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

if [[ "${CI:-}" != "true" ]]; then
  echo "run.sh: the bridge spike runs in CI only" >&2
  exit 1
fi

work="$(mktemp -d "${RUNNER_TEMP:-/tmp}/bridge-spike.XXXXXX")"
finish() {
  local status=$?
  if ((status != 0)); then
    "$repo/tests/kind/dump.sh" "$work" || true
  fi
  echo "==== log: the bridge (the last 400 lines)"
  "$work/bin/kubectl" --kubeconfig "$work/kubeconfig" --context kind-tnwks-image-test \
    -n test-support logs deploy/bridge --tail=400 2>/dev/null | cut -c1-2000 || true
  "$repo/tests/kind/down.sh" "$work" || true
  rm -rf "$work"
  exit "$status"
}
trap finish EXIT

IMAGE_TEST_AGENT_MODE=bridge-agent "$repo/tests/kind/up.sh" "$image" "$work"

export IMAGE_TEST_KUBECONFIG="$work/kubeconfig"
export IMAGE_TEST_KUBECTL="$work/bin/kubectl"
echo "== the bridge spike"
(cd "$repo/tests" && "$python" -m unittest -v test_bridge_spike)
