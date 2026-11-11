# tnwks-agent-image

The derived image of the tnwks agent platform: OpenClaw at one release, the
OpenShell CLI, and the plugins of [`plugins.txt`](plugins.txt). One image
serves the agent pod of each trust domain. Nothing of a trust domain is in
it: no config, no workspace, no state, no secret. Those come from tnwks-ops
and from the state volume at run time.

The image is public (`ghcr.io/it3e/tnwks-agent-image`). The design is effort
0003 in tnwks-coe (the Design Doc and the Execution Plan, workstream WS2).

## The image contract

tnwks-ops builds the agent pod of each trust domain against this contract.

| Item | Value |
| --- | --- |
| Image | `ghcr.io/it3e/tnwks-agent-image:<X.Y.Z>`, and `:sha-<short>` for the same build. Only a release tag pushes. There is no `:latest`. tnwks-ops pins `:<X.Y.Z>@sha256:<digest>` |
| User | uid 1000, gid 1000 (`node`). Set `fsGroup: 1000` |
| Root file system | Read only. Give the pod an `emptyDir` at `/home/node/.config`, and a `/tmp` with mode 0700 and owner uid 1000: an `emptyDir` with a `subPath` that an init container made. The sandbox plugin refuses a temp dir under a dir that others can write with no sticky bit, and an `emptyDir` has mode 2777 |
| Entrypoint, command | `tini -s --` and `node /app/openclaw.mjs gateway run` |
| Config | `/home/node/.openclaw/openclaw.json`, a read-only file (a `subPath` mount of a ConfigMap) |
| State directory | `/home/node/.openclaw`: the state volume, `subPath: state`. The workspace is `/home/node/.openclaw/workspace` |
| Archive directory | `/home/node/.openclaw-archives`: the same volume, `subPath: archives`. It is outside the state directory, so the sandbox plugin never copies it |
| Port | 18789, on loopback. `/healthz` and `/readyz` answer on it |
| Plugin paths | `/opt/openclaw/plugins/node_modules/@openclaw/openshell-sandbox` and `/opt/openclaw/plugins/node_modules/@openclaw/signal`. The config names them in `plugins.load.paths` |
| OpenShell client certificate | `ca.crt`, `tls.crt` and `tls.key` in `/home/node/.config/openshell/gateways/<endpoint>/mtls/`. `<endpoint>` is the `gatewayEndpoint` of the plugin config with each `:` and `/` as `_`: for `https://openshell.<ns>.svc.cluster.local:8080` it is `https___openshell.<ns>.svc.cluster.local_8080` |
| Env, set by the pod | `XDG_CACHE_HOME=/home/node/.openclaw/.cache` |
| Env, secret | `OPENCLAW_GATEWAY_TOKEN` (the gateway), `LITELLM_API_KEY` (the model gateway, named in the config as `${LITELLM_API_KEY}`), `TELEGRAM_BOT_TOKEN` (the Telegram channel). Each other private value of the config is a `${NAME}` in the config and an env var from a Secret |

The product runs `openclaw doctor --fix` in its own entrypoint script, and
doctor writes to the config file. With a read-only config that fails, so
this image starts the gateway directly, as the Kubernetes manifests of the
product do. The pod runs doctor as an init container, on a copy of the
config, before the gateway:

```sh
mkdir -p /tmp/doctor && cp /home/node/.openclaw/openclaw.json /tmp/doctor/
OPENCLAW_CONFIG_PATH=/tmp/doctor/openclaw.json node /app/openclaw.mjs doctor --fix --non-interactive
```

Kubernetes makes the directory of a `subPath` as root when it is not there.
Make `state` and `archives` on the volume, and the `/tmp` directory, as
uid 1000 first (an init container), or the gateway can not write to them. The pod of the tool test
does this ([`tests/kind/manifests.py`](tests/kind/manifests.py)).

## What is in it

- **The product:** `ghcr.io/openclaw/openclaw` at a release and its digest
  (the first `FROM` line of the [`Dockerfile`](Dockerfile)).
- **The OpenShell CLI:** the static build from the GitHub release of
  OpenShell. The `openshell-release` stage of the Dockerfile pins the
  release: the sandbox runtime image of OpenShell at a tag and its digest.
  The build reads the version from that image, downloads the CLI of the
  same version, and checks it against the checksum file of the release.
  `/opt/openclaw/openshell.release` records the version and the sha256.
- **The plugins:** each npm package of `plugins.txt`, at the release of the
  product. The build installs them in `/opt/openclaw/plugins`, read only.
  The agent can not install a plugin.
- **An empty archive directory** with mode 0700.

## The pins and their updates

Renovate uses the presets of the app template, with no rule of this
repository.

| Pin | Where | Update |
| --- | --- | --- |
| OpenClaw | `Dockerfile`, the first `FROM` | Renovate, tag and digest. A patch release (`2026.11.x`) and a new digest merge when CI is green. A new monthly line waits for a review. The plugins move with it |
| OpenShell | `Dockerfile`, the `openshell-release` stage | Renovate, tag and digest. The CLI in the image, and the chart and images of the test cluster, follow it |
| The images of the test cluster | [`.github/test-cluster/Dockerfile`](.github/test-cluster/Dockerfile) | Renovate, tag and digest. A change there makes no release |
| kind, helm, kubectl | [`tests/kind/up.sh`](tests/kind/up.sh), a version and a sha256 each | By hand, from the checksum file of each release. Renovate does not read files under `tests/` |
| Claude Code (the test layer of the Claude CLI test only) | [`.github/workflows/claude-cli.yaml`](.github/workflows/claude-cli.yaml), `CLAUDE_CODE_VERSION` | Renovate (npm). Not in the image that ships |

## The tests

`bin/image-test <image ref>` is the test of the image job in CI. The job
builds the image, runs this test, and pushes only on a release tag. So a
failed test means no image.

1. **The image** ([`tests/test_image.py`](tests/test_image.py), Docker
   only): the user and the command; the CLI is the pinned OpenShell
   release; each plugin is at the release of the product; no file of a
   trust domain is in the image; the gateway starts with a read-only root,
   no capability and a read-only config, and loads the plugins; a `${NAME}`
   in the config takes the value of the env var; the config does not
   change; doctor works on a copy. `task test` runs this part on a
   workstation.
2. **The tool test** ([`tests/test_tools.py`](tests/test_tools.py), CI
   only): one turn of the agent calls each tool that runs code (`exec`;
   `exec` in the background, then `process`; `exec` with `host` and
   `elevated`; `exec` through `dispatch_action`) and each file tool. Each
   one must run in an OpenShell sandbox: another host, `OPENSHELL_SANDBOX=1`,
   not uid 1000, no capability, no service account token, no secret name
   or value in the environment, and no config, state, archive or marker
   file of the agent pod. The same probe in the agent pod must find each of
   these things. A write to the path of the config from a sandbox must not
   change the config. A new tool in a product update fails the test until
   a person adds it to the list of the test.
3. **The trace test** ([`tests/test_traces.py`](tests/test_traces.py), CI
   only): a marker in the prompt, in a tool call and in the answer of the
   model must not get to the trace collector, and the spans of the turn
   must get there.

A mock model server ([`tests/mock-model/mock.py`](tests/mock-model/mock.py))
answers as the model. It asks for the tool calls of a plan, and it logs what
it received. The tests use fake values only.

### The test cluster

[`tests/kind/up.sh`](tests/kind/up.sh) makes it in the CI job: a kind cluster
with one node, Agent Sandbox from its release manifest, the OpenShell chart
of the pinned release (with the sandbox runtime at its digest and the
telemetry of OpenShell off), the mock and an OpenTelemetry Collector in the
namespace `test-support`, and the agent pod in the namespace `agent` (pod
security `restricted`). [`tests/kind/manifests.py`](tests/kind/manifests.py)
writes the manifests. Each kubectl and helm call names the kubeconfig file
of the test cluster. When a step fails, [`tests/kind/dump.sh`](tests/kind/dump.sh)
prints the pods, the events and the logs to the job log.

Never run the kind part on a workstation that runs a live cluster.
`bin/image-test` makes the cluster only when `CI=true` or
`IMAGE_TEST_KIND=1`.

### The Claude CLI test

The workflow [`claude-cli.yaml`](.github/workflows/claude-cli.yaml) is a job
of its own, so its result hides no result of the image job. It tests
OpenClaw's Claude CLI runtime (`agentRuntime: { id: "claude-cli" }`) behind a
model gateway, on the test cluster with the agent pod of
`manifests.py claude-agent`. The image that ships holds no Claude Code: the
job adds it in a test layer ([`tests/claude-cli/Dockerfile`](tests/claude-cli/Dockerfile)),
at the version that the workflow pins for Renovate.
[`tests/test_claude_cli.py`](tests/test_claude_cli.py) records where the
model calls go (the mock, the network policy of the pod, the log of the
cluster DNS), where each OpenClaw tool runs when it comes through the MCP
bridge, and which Claude Code native tools the model can reach. It writes
what it finds to the job summary. It fails only when the harness can not
give the evidence: a finding is a result, not a failure.

The same job runs a spike on the same cluster:
[`tests/test_sandbox_exec.py`](tests/test_sandbox_exec.py). The test layer
also holds a small plugin ([`tests/claude-cli/sandbox-exec`](tests/claude-cli/sandbox-exec))
that registers the tool `sandbox_exec`. The tool runs one command in the
OpenShell sandbox of the session, through the public plugin SDK. The test
records whether a Claude CLI turn gets the tool, where the command runs,
what happens to the workspace, which sandbox each call gets and who deletes
it, and whether a per-agent tool policy removes the tool. The plugin is a
spike: the image that ships does not hold it.

## Workflow

This repo follows the app-repo standard in
[iT3E/app-repo-template](https://github.com/iT3E/app-repo-template), with
CI from [iT3E/github-workflows](https://github.com/iT3E/github-workflows);
`AGENTS.md` has the short version.

- Changes go through PRs, never straight to `main`. Merge only when every
  check is green (`gh-merge-safe.sh <pr> iT3E/tnwks-agent-image`).
- `task precommit:init` installs the hooks: betterleaks, the privacy
  denylist (staged files, git identity, commit message) and ruff format.
- Each PR adds a Changie fragment (`changie new`) or carries the
  `skip-changelog` label. Merging fragments cuts a release (`vX.Y.Z` tag,
  GitHub Release, image `:X.Y.Z`).
- Renovate keeps dependencies current. Patch, digest and non-0.x minor
  updates merge themselves once CI is green; majors wait for review on the
  Renovate Dashboard issue. Template releases arrive as their own
  `renovate/template` PRs.
