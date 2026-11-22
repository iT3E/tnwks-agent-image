# tnwks-agent-image

The derived image of the tnwks agent platform: OpenClaw at one release, the
OpenShell CLI, the plugins of [`plugins.txt`](plugins.txt), and the npm
cache of the plugins of [`official-plugins.txt`](official-plugins.txt). One image
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
| Host approval plugin | `/opt/openclaw/local-plugins/host-approval`, owned by root and read only. The agent config in ops names it in `plugins.load.paths` and gives its config in `plugins.entries.host-approval`, whose `enabled` is the control of its load ("The host approval plugin") |
| Official plugins | `brave` (the Brave provider of `web_search`). The official plugins step installs it in the state directory. The config enables it in `plugins.entries` and does not name a path for it ("The official plugins") |
| Official plugins step | `install-official-plugins`, in an init container before doctor, with the env and the mounts of the gateway. It needs no secret and no network. It prints `official-plugins id=<id> version=<version> result=installed\|failed` for each plugin, or `official-plugins result=failed reason=<reason>`, and exits 1 on a failure. A failure stops the start of the pod |
| OpenShell client certificate | `ca.crt`, `tls.crt` and `tls.key` in `/home/node/.config/openshell/gateways/<endpoint>/mtls/`. `<endpoint>` is the `gatewayEndpoint` of the plugin config with each `:` and `/` as `_`: for `https://openshell.<ns>.svc.cluster.local:8080` it is `https___openshell.<ns>.svc.cluster.local_8080` |
| Env, set by the pod | `XDG_CACHE_HOME=/home/node/.openclaw/.cache` |
| Env, secret | `OPENCLAW_GATEWAY_TOKEN` (the gateway), `LITELLM_API_KEY` (the model gateway, named in the config as `${LITELLM_API_KEY}`), `TELEGRAM_BOT_TOKEN` (the Telegram channel), `BRAVE_API_KEY` (the Brave provider of `web_search`, read from the env by the plugin, not named in the config). Each other private value of the config is a `${NAME}` in the config and an env var from a Secret |

The product runs `openclaw doctor --fix` in its own entrypoint script, and
doctor writes to the config file. With a read-only config that fails, so
this image starts the gateway directly, as the Kubernetes manifests of the
product do. The pod runs doctor as an init container, on a copy of the
config, before the gateway:

```sh
mkdir -p /tmp/doctor && cp /home/node/.openclaw/openclaw.json /tmp/doctor/
OPENCLAW_CONFIG_PATH=/tmp/doctor/openclaw.json node /app/openclaw.mjs doctor --fix --non-interactive
```

## The official plugins

A sandboxed session of the product uses a `web_search` provider only when
its plugin is bundled with the product or is a verified official install
(`src/plugins/web-provider-resolution-shared.ts` of OpenClaw 2026.11.3). A
plugin from `plugins.load.paths` is neither, so in a sandboxed session the
tool fails with `web_search is unavailable. Enable or select a bundled or
verified official search provider for sandboxed agents.` and makes no
request. The Brave plugin is not bundled with the product, so it must be a
verified official install.

The product makes an install a verified official install when its own
installer (`openclaw plugins install`) installed the official npm package:
the install record in the state database names the npm source, the package
and its integrity, and the package is in the official catalog of the
product. The pod has no route to the npm registry. So:

1. The build runs the installer of the product once, with the network, and
   keeps its npm cache in the image (`/opt/openclaw/npm-cache`): each
   package of `official-plugins.txt` at the release of the product.
2. The official plugins step of the pod
   ([`image/install-official-plugins`](image/install-official-plugins))
   runs the same installer with npm offline, from a copy of that cache. The
   code goes to `<state directory>/npm/`, and the install record to the
   state database.

The step trusts nothing that it finds in the state directory. The gateway
runs as the same user and can write that directory, and the product
checks no hash of installed code at a start: trust comes from the install
record alone, and the gateway can write the record too. Changed code there
would run in the gateway, with its secrets, at each later start, and a
restored archive would bring back its old code. So at each start the step:

- stops the start when the state holds an install record of a plugin that
  is not in `official-plugins.txt`, or any file in
  `<state directory>/extensions`. The image put neither there. An install
  record puts a plugin in front of a bundled plugin of the same id
  (`resolveDuplicatePrecedenceRank`, `src/plugins/manifest-registry-build.ts:168-200`
  of OpenClaw 2026.11.3), so it must not stay;
- removes `<state directory>/npm` and installs each official plugin again
  from the image (some 8 seconds). The install rewrites the record of the
  plugin, its source and its path too.

Run the step before doctor. Doctor tries to install a configured plugin that
is missing, from the npm registry: with no route there, it waits for the
timeouts and leaves an unfinished upgrade for the plugin.

```sh
install-official-plugins   # with OPENCLAW_STATE_DIR and OPENCLAW_CONFIG_PATH of the gateway
```

`openclaw plugins inspect brave --json` shows the result: `origin` is
`global`, `trustedOfficialInstall` is `true`, and `trust.reason` is
`trusted-official`. `openclaw sandbox explain` does not show it: it shows
the tool policy (the tool names that a sandbox allows), not the provider of
a tool.

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
- **The official plugins:** each npm package of `official-plugins.txt`, at
  the release of the product, in the npm cache `/opt/openclaw/npm-cache`,
  and the official plugins step that installs them ("The official
  plugins"). The code of each one is in the state directory at run time,
  and each start of the pod replaces it with the code of the image.
- **The host approval plugin:** our one own plugin, from
  [`plugins/host-approval`](plugins/host-approval), in
  `/opt/openclaw/local-plugins/host-approval`, read only ("The host approval
  plugin").
- **An empty archive directory** with mode 0700.

## The host approval plugin

The plugin asks the approver before each call of the host server that is
not a listed read, and refuses each host call of anyone else (Design Doc of
effort 0003 in tnwks-coe, section 4.2.6). The code and its unit tests are
in [`plugins/host-approval`](plugins/host-approval). The `host-approval`
stage of the Dockerfile runs the unit tests with the node of the product,
so a red test stops the build, and copies the plugin without its tests into
`/opt/openclaw/local-plugins/host-approval`.

The agent config in ops loads it with two keys:

- `plugins.load.paths` holds `/opt/openclaw/local-plugins/host-approval`.
- `plugins.entries.host-approval` holds `enabled` and the config of the
  plugin. The config schema is in
  [`openclaw.plugin.json`](plugins/host-approval/openclaw.plugin.json). The
  approver ID and the gateway key come from the env, never from a file of
  this repository.

At its load the plugin checks the product config. It refuses each tool
call when an `mcp.servers` entry reaches the host route by any name,
address or form of the path (an entry whose decoded first path segment is
the host route, on any host and port; it resolves the host names where an
address decides), or
when a place of the config other than its own entry holds a form of its
key. Until the check ends, and while a name does not resolve (DNS not ready
at the start of the pod), it refuses each tool call; it tries again after
2, 5, 10 and then each 30 seconds, and loads when the check passes.

Its log lines name no channel, sender, chat ID or session key: `loaded,
<n> read lines`, `refusing each tool call: <errors>`, and `refused <tool>
(<reason>)`.

The update check ([`tests/host-approval-check`](tests/host-approval-check))
starts the image with fakes of the chat API, the model and an IRC server,
the central MCP gateway, the host server and a fake Linux host, and runs
the rows of the plugin on one internal network with no route out. The
second channel is the IRC plugin of the product with the fake server.
`bin/image-test` runs it in CI under the Build check. It is switched off
(`host_approval_check=off`) until the host server has a release: to switch
it on, add the `host-mcp` stage to `.github/test-cluster/Dockerfile` and
set `host_approval_check=on`, in one pull request.

## The pins and their updates

Renovate uses the presets of the app template, with no rule of this
repository.

| Pin | Where | Update |
| --- | --- | --- |
| OpenClaw | `Dockerfile`, the first `FROM` | Renovate, tag and digest. A patch release (`2026.11.x`) and a new digest merge when CI is green. A new monthly line waits for a review. The plugins move with it |
| OpenShell | `Dockerfile`, the `openshell-release` stage | Renovate, tag and digest. The CLI in the image, and the chart and images of the test cluster, follow it |
| The images of the test cluster | [`.github/test-cluster/Dockerfile`](.github/test-cluster/Dockerfile) | Renovate, tag and digest. A change there makes no release |
| kind, helm, kubectl | [`tests/kind/up.sh`](tests/kind/up.sh), a version and a sha256 each | By hand, from the checksum file of each release. Renovate does not read files under `tests/` |
| The images of the update check of the host approval plugin | The `mcp-gateway`, `sshd-base` and `host-mcp` stages of [`.github/test-cluster/Dockerfile`](.github/test-cluster/Dockerfile) | Renovate, tag and digest. The `host-mcp` stage is not there yet ("The host approval plugin") |

## The image age check

Renovate proposes each OpenClaw update, but a pull request can wait or
fail, and Renovate itself can stop. So a scheduled check
([`bin/image-age`](bin/image-age)) fails when a newer stable OpenClaw
release came out more than 14 days ago and the `Dockerfile` still pins the
old one. The workflow `image-age.yaml` runs it each day and on demand.

- **The releases:** the tags of `ghcr.io/openclaw/openclaw`, the same list
  that Renovate reads. A stable release is a tag that Renovate can propose
  for the pin: numbers only, with as many parts as the pin. So a pre-release
  (`-beta.1`, `rc1`) or a variant (`-slim`) does not count.
- **The age:** the registry gives no publish date for a tag. So the date
  of a release is the `created` time in the config of its linux/amd64
  image. The registry gets the tag after the build, so this date is a
  little early and the check is a little strict.
- **The first newer release:** of the stable releases newer than the pin,
  the one that came out first. The check passes when there is none, or
  when it is 14 days old or less (`MAX_AGE_DAYS`, or `--max-days`).
- **Three results:** `OK` (exit 0), `TOO OLD` (exit 1), and `CHECK ERROR`
  (exit 2) when the registry fails twice (one retry after 30 seconds) or a
  pin or a date can not be read. A check error says nothing about the age
  of the image. Both failures fail the run.
- **The notice:** a failed run is a GitHub notification, not an alert. For
  a scheduled run, GitHub sends it to the person who last changed the cron
  line of the workflow. Keep that person the owner, and keep failed
  workflow notifications on in the GitHub notification settings.
- **No secrets:** the image is public, and the registry gives an anonymous
  pull token. The job has `contents: read` only.

Run it locally: `bin/image-age`. The unit tests use recorded registry
answers in [`tests/image-age/`](tests/image-age): `cd tests && python3 -m
unittest -v test_image_age`.

## The tests

`bin/image-test <image ref>` is the test of the image job in CI. The job
builds the image, runs this test, and pushes only on a release tag. So a
failed test means no image.

1. **The image** ([`tests/test_image.py`](tests/test_image.py), Docker
   only): the user and the command; the CLI is the pinned OpenShell
   release; each plugin is at the release of the product; no file of a
   trust domain is in the image; the npm cache holds each official plugin;
   the official plugins step installs each one with no network, as a
   verified official install, again at each run; it replaces changed code
   and a changed install record, and it stops the start for an install
   record of another plugin or a file in `<state>/extensions`; with
   `plugins.allow` in the config, a plugin in `<state>/extensions` or with
   an install record that the list does not name runs neither in doctor nor
   in the gateway, and one with the id of a bundled plugin in
   `<state>/extensions` does not take its place; the gateway
   starts with a read-only root,
   no capability and a read-only config, and loads the plugins; a `${NAME}`
   in the config takes the value of the env var; the config does not
   change; doctor works on a copy; a web search goes to the search endpoint
   of the config, a fake one in the container, with the key from the env.
   `task test` runs this part on a workstation.
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
   a person adds it to the list of the test. The product has tools that are
   on by default and that the config denies by name in `tools.deny`:
   `code_execution` (the bundled xai plugin) and the file transfer tools
   `file_fetch`, `file_write`, `dir_list` and `dir_fetch` (the bundled
   file-transfer plugin). A second turn asks for each one by name and
   searches for them: the product must not offer one, a call must fail and
   run nothing, and the tool search must not find one. A third turn runs a
   web search in the sandboxed session: it must get to the fake search
   endpoint of the pod, with the key, and its result must come back.
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
