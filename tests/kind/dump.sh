#!/usr/bin/env bash
# Print the state of the test cluster to the job log, when a step failed.
# Usage: tests/kind/dump.sh <work dir>
set -uo pipefail

work="${1:?usage: dump.sh <work dir>}"
[[ -s "$work/kubeconfig" ]] || exit 0
kx() { "$work/bin/kubectl" --kubeconfig "$work/kubeconfig" --context kind-tnwks-image-test "$@"; }

section() { echo; echo "==== $*"; }

section "pods"
kx get pods -A -o wide
section "events"
kx get events -A --sort-by=.lastTimestamp | tail -60
section "sandboxes"
kx get sandboxes.agents.x-k8s.io -A -o wide 2>/dev/null
section "the agent pod"
kx -n agent describe pod agent | tail -40
for c in prepare doctor agent; do
  section "log: agent/$c"
  kx -n agent logs agent -c "$c" --tail=150
done
section "log: the OpenShell gateway"
kx -n openshell logs -l app.kubernetes.io/name=openshell --all-containers --tail=150 --prefix
section "log: the sandbox pods"
for pod in $(kx -n openshell get pods -o name | grep -v -e 'openshell-0' -e certgen); do
  kx -n openshell logs "$pod" --all-containers --tail=60 --prefix
done
section "log: the mock model server (the last 5 requests, cut)"
kx -n test-support logs deploy/mock-model --tail=5 | cut -c1-3000
section "log: the trace collector (the last 60 lines)"
kx -n test-support logs deploy/otelcol --tail=60
