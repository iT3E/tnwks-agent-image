#!/usr/bin/env bash
# The update check of the host approval plugin (Design Doc of effort 0003,
# section 9, "The rows of the plugin at each product update"). bin/image-test
# runs it in CI on the image that the pull request built, under the required
# Build check, so a red row stops the merge of a move of the product pin,
# of the plugin, of the Dockerfile or of a base.
#
#   tests/host-approval-check/run.sh <agent image>
#
# It needs Docker and python3. The images are pinned by digest as stages of
# .github/test-cluster/Dockerfile, where Renovate moves them. The run has no
# route out (one internal Docker network) and no secret: each key and token
# is new for the run, and the run removes its containers, network, volumes
# and files. Before the run, it fetches the IRC plugin of the product (the
# second channel of the rows, with a fake server) at the release of the
# product, with npm. npm checks the package only against the integrity hash
# in the metadata of the registry; this repository holds no hash of it.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export AGENT_IMAGE="${1:?usage: run.sh <agent image>}"

pin() {
  awk -v n="$1" 'toupper($1) == "FROM" && toupper($3) == "AS" && $4 == n { print $2 }' \
    "$REPO/.github/test-cluster/Dockerfile"
}
export AGW_IMAGE="$(pin mcp-gateway)"
export PY_IMAGE="$(pin python)"
SSHD_BASE="$(pin sshd-base)"
export SSH_MCP_IMAGE="$(pin host-mcp)"
if [[ -z "$SSH_MCP_IMAGE" ]]; then
  echo "run.sh: no host-mcp stage in .github/test-cluster/Dockerfile (the released host server image)" >&2
  exit 3
fi
for v in "$AGW_IMAGE" "$PY_IMAGE" "$SSHD_BASE" "$SSH_MCP_IMAGE"; do
  [[ "$v" == *@sha256:* ]] || { echo "run.sh: a pin has no digest: '$v'" >&2; exit 1; }
done

export UC_WORK="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/host-approval-check.XXXXXX")"
# Containers leave files of root in the work folder: they go back to the
# caller first, so the clean up works for any uid.
cleanup() {
  docker run --rm --network none -v "$UC_WORK:/w" --entrypoint chown "$PY_IMAGE" -R "$(id -u):$(id -g)" /w \
    >/dev/null 2>&1 || true
  rm -rf "$UC_WORK"
}
trap cleanup EXIT
export SSHD_IMAGE="host-approval-check-sshd:$(basename "$UC_WORK" | tr 'A-Z.' 'a-z-')"
docker build -q --build-arg "BASE=$SSHD_BASE" -t "$SSHD_IMAGE" "$HERE/sshd" >/dev/null
for i in "$AGW_IMAGE" "$PY_IMAGE" "$SSH_MCP_IMAGE"; do
  docker image inspect "$i" >/dev/null 2>&1 || docker pull -q "$i" >/dev/null
done

# The IRC plugin at the release of the product, unpacked for a read-only mount.
# The product loads a plugin only from files of its own uid or of root, and
# the uid of a CI runner is neither, so the files go to root.
mkdir -p "$UC_WORK/irc"
docker run --rm --user 0:0 -e HOME=/tmp -v "$UC_WORK/irc:/out" --entrypoint sh "$AGENT_IMAGE" -c \
  'cd /out && npm pack --silent "@openclaw/irc@$(node -p "require(\"/app/package.json\").version")" >/dev/null && tar -xzf openclaw-irc-*.tgz && rm openclaw-irc-*.tgz && chown -R 0:0 package'
export IRC_PLUGIN_DIR="$UC_WORK/irc/package"
test -f "$IRC_PLUGIN_DIR/package.json"

status=0
python3 -I "$HERE/check.py" || status=$?
docker image rm -f "$SSHD_IMAGE" >/dev/null 2>&1 || true
exit "$status"
