"""The checks of the image that need Docker only: bin/image-test runs them
first, on a workstation and in CI.

- The user, the entrypoint and the command of the image contract.
- The OpenShell CLI is the release of the anchor stage of the Dockerfile.
- Each plugin of plugins.txt is installed at the release of the product.
- The npm cache holds each package of official-plugins.txt at the release of
  the product.
- The image holds nothing of a trust domain: no config, no state, no archive.
- The official plugins step installs each plugin of official-plugins.txt
  with no network, as a verified official install, again at each run. It
  replaces changed code and a changed install record, and it stops the start
  for an install record of another plugin or a file in <state>/extensions.
- The gateway starts with a read-only root and a read-only config, and loads
  the plugins. The doctor step of the product works on a copy of the config.
- With plugins.allow in the config, a plugin that the state directory holds
  and the list does not name runs neither in doctor nor in the gateway: a
  plugin in <state>/extensions, one that names the configured web search
  provider, and one with an install record. A plugin in
  <state>/extensions with the id of a bundled plugin does not take its place.
- A web search goes to the search endpoint of the config with the key from
  the env, and its result comes back. The endpoint is a fake one in the
  container: the test calls no search vendor.
"""

import hashlib
import json
import re
import time
import unittest
import uuid

from support import FAKE_SECRETS, IMAGE_REF, REPO, docker, in_image, openshell_version

CONFIG = REPO / "tests" / "config" / "openclaw.json"
PLUGIN_DIR = "/opt/openclaw/plugins/node_modules"

# The env of a test container: fake values only.
TEST_ENV = dict(
    FAKE_SECRETS, OPENSHELL_SANDBOX_FROM="docker.io/library/python:3.13-slim"
)


def plugin_names():
    names = []
    for line in (REPO / "plugins.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def official_plugins():
    """The lines of official-plugins.txt, as {plugin id: npm package}."""
    plugins = {}
    for line in (REPO / "official-plugins.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            plugin_id, package = line.split()
            plugins[plugin_id] = package
    return plugins


# The plugins that the test puts in the state directory after the official
# plugins step, as a process that can write the state could. Each one writes
# a line to <state>/plugin-ran.log when it is imported and registered.
# extra-search names the web search provider of the config (brave) in its
# contracts: with no allow list, the product enables such a plugin by itself
# (src/config/plugin-auto-enable.materialize.ts of OpenClaw 2026.11.3), and
# the gateway imports it.
STATE_PLUGINS = {
    "extensions/extra-file": ("extra-file", {}),
    "extensions/extra-search": ("extra-search", {"webSearchProviders": ["brave"]}),
    "extensions/memory-core": ("memory-core", {}),
}
RECORD_PLUGIN = "extra-record"


def plant_plugin(directory, plugin_id, contracts=None):
    """Shell lines that write a test plugin to a directory."""
    package = json.dumps(
        {
            "name": f"test-{plugin_id}",
            "version": "1.0.0",
            "type": "module",
            "openclaw": {"extensions": ["./index.js"]},
        }
    )
    manifest = json.dumps(
        {
            "id": plugin_id,
            "name": "Test",
            "description": "test",
            "activation": {"onStartup": True},
            "contracts": contracts or {},
            "configSchema": {"type": "object", "properties": {}},
        }
    )
    index = (
        'import fs from "node:fs";\n'
        "const mark = (what) => fs.appendFileSync("
        '(process.env.OPENCLAW_STATE_DIR ?? "/home/node/.openclaw") + "/plugin-ran.log", '
        f'"{plugin_id} " + what + "\\n");\n'
        'mark("imported");\n'
        f'export default {{ id: "{plugin_id}", name: "Test", description: "test", '
        'register() { mark("registered"); } };\n'
    )
    return (
        f"mkdir -p {directory}\n"
        f"cat > {directory}/package.json <<'JSON'\n{package}\nJSON\n"
        f"cat > {directory}/openclaw.plugin.json <<'JSON'\n{manifest}\nJSON\n"
        f"cat > {directory}/index.js <<'JS'\n{index}JS\n"
    )


def ready_check():
    return [
        "node",
        "-e",
        "require('http').get('http://127.0.0.1:18789/readyz', r => process.exit(r.statusCode < 400 ? 0 : 1))"
        ".on('error', () => process.exit(1))",
    ]


@unittest.skipUnless(IMAGE_REF, "IMAGE_REF is not set")
class ImageContract(unittest.TestCase):
    def test_user_entrypoint_command(self):
        config = json.loads(
            docker("image", "inspect", IMAGE_REF, "--format", "{{json .Config}}").stdout
        )
        self.assertEqual(config["User"], "1000:1000")
        self.assertEqual(config["Entrypoint"], ["tini", "-s", "--"])
        self.assertEqual(config["Cmd"], ["node", "/app/openclaw.mjs", "gateway", "run"])
        self.assertEqual(
            in_image("-c", "id -u; id -g").stdout.split(), ["1000", "1000"]
        )

    def test_openshell_cli_is_the_pinned_release(self):
        version = openshell_version()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        out = in_image(
            "-c", "openshell --version; cat /opt/openclaw/openshell.release"
        ).stdout.split()
        self.assertEqual(out[:2], ["openshell", version])
        self.assertEqual(out[2], version)
        self.assertRegex(out[3], r"^[0-9a-f]{64}$")

    def test_plugins_at_the_product_release(self):
        script = (
            "const fs = require('fs');"
            "const read = p => JSON.parse(fs.readFileSync(p, 'utf8'));"
            "const out = {product: read('/app/package.json').version, plugins: {}};"
            f"for (const n of {json.dumps(plugin_names())}) out.plugins[n] = read('{PLUGIN_DIR}/' + n + '/package.json').version;"
            "console.log(JSON.stringify(out));"
        )
        out = json.loads(in_image("-e", script, entrypoint="node").stdout)
        self.assertTrue(out["plugins"], "plugins.txt names no plugin")
        for name, version in out["plugins"].items():
            self.assertEqual(
                version, out["product"], f"{name} is not at the release of the product"
            )

    def test_official_plugins_in_the_npm_cache(self):
        version = in_image(
            "-e",
            "console.log(require('/app/package.json').version)",
            entrypoint="node",
        ).stdout.strip()
        keys = in_image(
            "-c", "cat /opt/openclaw/npm-cache/_cacache/index-v5/*/*/*"
        ).stdout
        self.assertTrue(official_plugins(), "official-plugins.txt names no plugin")
        for package in official_plugins().values():
            # The key of the tarball of the package in the cache of npm.
            tarball = f"{package}/-/{package.rsplit('/', 1)[-1]}-{version}.tgz"
            self.assertIn(tarball, keys, package)

    def test_nothing_of_a_trust_domain(self):
        # No file in the state, archive and config paths of the user, and no
        # config or env file anywhere outside the code of the product. As
        # root, so that each directory can be read.
        out = in_image(
            "-c",
            "find /home/node/.openclaw /home/node/.openclaw-archives /home/node/.config ! -type d; "
            "find / -xdev \\( -path /proc -o -path /sys -o -path /app -o -path /opt -o -path /usr \\) -prune "
            "-o -type f \\( -name openclaw.json -o -name '.env' -o -name '*.env' -o -name 'tls.key' \\) -print",
            user="0",
        ).stdout
        self.assertEqual(
            out.strip(), "", f"files of a trust domain in the image:\n{out}"
        )
        mode = in_image(
            "-c", "stat -c '%u:%g %a' /home/node/.openclaw-archives"
        ).stdout.strip()
        self.assertEqual(mode, "1000:1000 700")


@unittest.skipUnless(IMAGE_REF, "IMAGE_REF is not set")
class GatewayStarts(unittest.TestCase):
    """The gateway as the pod runs it: a read-only root, no capability, the
    config as a read-only file, and the state on a volume of its own."""

    @classmethod
    def setUpClass(cls):
        cls.name = f"tnwks-image-test-{uuid.uuid4().hex[:8]}"
        cls.volume = cls.name
        cls.config_hash = hashlib.sha256(CONFIG.read_bytes()).hexdigest()
        docker("volume", "create", cls.volume)
        base = [
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            # As in the pod: a /tmp of the user with mode 0700.
            "--tmpfs",
            "/tmp:uid=1000,gid=1000,mode=0700",
            "--tmpfs",
            "/home/node/.openclaw-archives:uid=1000,gid=1000,mode=0700",
            "--tmpfs",
            "/home/node/.config:uid=1000,gid=1000,mode=0755",
            # The state volume, and the config from git on top of it.
            "-v",
            f"{cls.volume}:/home/node/.openclaw",
            "-v",
            f"{CONFIG}:/home/node/.openclaw/openclaw.json:ro",
            "-e",
            "XDG_CACHE_HOME=/home/node/.openclaw/.cache",
        ]
        for key, value in TEST_ENV.items():
            base += ["-e", f"{key}={value}"]

        # The first init container of the product steps: the official plugins
        # step, with no network (the base has none). Twice: each run installs
        # each plugin again.
        cls.official = [
            docker(
                "run",
                "--rm",
                *base,
                IMAGE_REF,
                "install-official-plugins",
                check=False,
                timeout=300,
            )
            for _ in range(2)
        ]

        # Plugins that the image did not put in the state: two in
        # <state>/extensions, and one installed from a path, with an install
        # record. The official plugins step would stop a later start for
        # them; the allow list of the config must keep them out on its own.
        plant = "set -e\n"
        for directory, (plugin_id, contracts) in STATE_PLUGINS.items():
            plant += plant_plugin(
                f"/home/node/.openclaw/{directory}", plugin_id, contracts
            )
        plant += plant_plugin(f"/tmp/{RECORD_PLUGIN}", RECORD_PLUGIN)
        plant += (
            "cp /home/node/.openclaw/openclaw.json /tmp/config-copy.json\n"
            # With no OPENCLAW_STATE_DIR, the product would take the directory
            # of the config copy as the state directory.
            "OPENCLAW_STATE_DIR=/home/node/.openclaw "
            "OPENCLAW_CONFIG_PATH=/tmp/config-copy.json node /app/openclaw.mjs "
            f"plugins install /tmp/{RECORD_PLUGIN} --force --no-enable --accept-capabilities\n"
            "rm -f /home/node/.openclaw/plugin-ran.log\n"
        )
        cls.plant = docker(
            "run", "--rm", *base, IMAGE_REF, "sh", "-c", plant, check=False, timeout=300
        )

        # The init container of the pod: the doctor step of the product, on a
        # copy of the config, because the config is read only.
        cls.doctor = docker(
            "run",
            "--rm",
            *base,
            "-e",
            "OPENCLAW_CONFIG_PATH=/tmp/doctor/openclaw.json",
            IMAGE_REF,
            "sh",
            "-c",
            "mkdir -p /tmp/doctor && cp /home/node/.openclaw/openclaw.json /tmp/doctor/ "
            "&& node /app/openclaw.mjs doctor --fix --non-interactive",
            check=False,
            timeout=300,
        )

        # The gateway, with the command of the image.
        docker("run", "-d", "--name", cls.name, *base, IMAGE_REF)
        cls.ready = False
        deadline = time.time() + 180
        while time.time() < deadline:
            if docker("exec", cls.name, *ready_check(), check=False).returncode == 0:
                cls.ready = True
                break
            if (
                docker(
                    "inspect", "-f", "{{.State.Running}}", cls.name, check=False
                ).stdout.strip()
                != "true"
            ):
                break
            time.sleep(3)

    @classmethod
    def tearDownClass(cls):
        docker("rm", "-f", cls.name, check=False)
        docker("volume", "rm", "-f", cls.volume, check=False)

    def logs(self):
        result = docker("logs", "--tail", "40", self.name, check=False)
        return result.stdout + result.stderr

    def test_ready(self):
        self.assertTrue(self.ready, f"the gateway did not get ready:\n{self.logs()}")

    def test_plugins_loaded(self):
        self.assertTrue(self.ready, "the gateway did not get ready")
        out = docker(
            "exec", self.name, "node", "/app/openclaw.mjs", "plugins", "list", "--json"
        ).stdout
        plugins = {p["id"]: p for p in json.loads(out[out.index("{") :])["plugins"]}
        for plugin_id in ("openshell", "signal", "brave", "diagnostics-otel"):
            self.assertIn(plugin_id, plugins)
            self.assertEqual(plugins[plugin_id]["status"], "loaded", plugin_id)
        self.assertEqual(
            plugins["openshell"]["version"], plugins["diagnostics-otel"]["version"]
        )

    def test_only_the_allowed_plugins_load(self):
        self.assertTrue(self.ready, "the gateway did not get ready")
        allow = set(json.loads(CONFIG.read_text())["plugins"]["allow"])
        logs = docker("logs", self.name, check=False)
        logs = logs.stdout + logs.stderr
        line = re.search(r"http server listening \(\d+ plugins: ([^;)]*)", logs)
        self.assertIsNotNone(line, logs[-2000:])
        started = set(line.group(1).split(", "))
        # The gateway starts a channel plugin only with its channel in the
        # config, and the test config has none.
        self.assertLessEqual(started, allow)
        self.assertEqual(allow - started, {"signal"})

    def test_state_plugins_do_not_run(self):
        self.assertEqual(
            self.plant.returncode,
            0,
            self.plant.stdout[-2000:] + self.plant.stderr[-2000:],
        )
        self.assertTrue(self.ready, "the gateway did not get ready")
        ran = docker(
            "exec",
            self.name,
            "sh",
            "-c",
            "cat /home/node/.openclaw/plugin-ran.log 2>/dev/null",
            check=False,
        ).stdout
        self.assertEqual(ran, "", "a plugin of the state ran")
        out = docker(
            "exec", self.name, "node", "/app/openclaw.mjs", "plugins", "list", "--json"
        ).stdout
        plugins = {}
        for plugin in json.loads(out[out.index("{") :])["plugins"]:
            plugins.setdefault(plugin["id"], []).append(plugin)
        for plugin_id in ("extra-file", "extra-search", RECORD_PLUGIN):
            self.assertIn(plugin_id, plugins)
            for plugin in plugins[plugin_id]:
                self.assertEqual(plugin["status"], "disabled", plugin)
        # The bundled memory-core, not the one of the same id in the state.
        loaded = [p for p in plugins["memory-core"] if p["status"] == "loaded"]
        self.assertEqual(
            [p["origin"] for p in loaded], ["bundled"], plugins["memory-core"]
        )

    def test_official_plugins_step(self):
        first, second = self.official
        for result in self.official:
            self.assertEqual(
                result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:]
            )
        for plugin_id in official_plugins():
            self.assertRegex(first.stdout, rf"id={plugin_id} .*result=installed")
            self.assertRegex(second.stdout, rf"id={plugin_id} .*result=installed")

    def test_official_plugins_are_verified_official_installs(self):
        # A sandboxed session uses a web search provider only when it is
        # bundled or a verified official install. A plugin from
        # plugins.load.paths is neither.
        self.assertTrue(self.ready, "the gateway did not get ready")
        for plugin_id, package in official_plugins().items():
            out = docker(
                "exec",
                self.name,
                "node",
                "/app/openclaw.mjs",
                "plugins",
                "inspect",
                plugin_id,
                "--json",
            ).stdout
            info = json.loads(out[out.index("{") :])
            self.assertEqual(info["plugin"]["status"], "loaded", plugin_id)
            self.assertEqual(info["plugin"]["origin"], "global", plugin_id)
            self.assertIs(info["plugin"]["trustedOfficialInstall"], True, plugin_id)
            self.assertEqual(info["plugin"]["trust"]["reason"], "trusted-official")
            self.assertEqual(info["install"]["source"], "npm")
            self.assertEqual(info["install"]["resolvedName"], package)

    def test_config_env_reference(self):
        # A string of the config can name an environment variable. The ops
        # repository uses this form for each private value.
        self.assertTrue(self.ready, "the gateway did not get ready")
        out = docker(
            "exec",
            self.name,
            "node",
            "/app/openclaw.mjs",
            "config",
            "get",
            "plugins.entries.openshell.config.from",
        ).stdout
        self.assertEqual(
            out.strip().splitlines()[-1], TEST_ENV["OPENSHELL_SANDBOX_FROM"]
        )

    def test_config_not_changed(self):
        self.assertTrue(self.ready, "the gateway did not get ready")
        out = docker(
            "exec", self.name, "sha256sum", "/home/node/.openclaw/openclaw.json"
        ).stdout
        self.assertEqual(out.split()[0], self.config_hash)

    def test_web_search_at_the_configured_endpoint(self):
        # A fake Brave endpoint on loopback in the gateway container (the
        # baseUrl of the test config), then one search with the CLI of the
        # product. The fake logs the query and whether the key came.
        self.assertTrue(self.ready, "the gateway did not get ready")
        mark = uuid.uuid4().hex[:12]
        script = (
            "const http = require('http');"
            "http.createServer((req, res) => {"
            "const u = new URL(req.url, 'http://x');"
            "const key = req.headers['x-subscription-token'] === process.env.BRAVE_API_KEY;"
            "console.log(JSON.stringify({path: u.pathname, q: u.searchParams.get('q'), key}));"
            "res.setHeader('content-type', 'application/json');"
            "res.end(JSON.stringify({web: {results: [{title: 'FAKE-TITLE', "
            "url: 'https://example.com/', description: 'FAKE-SNIPPET-' + u.searchParams.get('q')}]}}));"
            "}).listen(18790, '127.0.0.1');"
        )
        out = docker(
            "exec",
            self.name,
            "sh",
            "-c",
            'node -e "$1" > /tmp/fake-search.log 2>&1 & pid=$!; sleep 1; '
            f"node /app/openclaw.mjs infer web search --query 'test query {mark}' --json; "
            "kill $pid; cat /tmp/fake-search.log",
            "sh",
            script,
            timeout=180,
        ).stdout
        lines = out.strip().splitlines()
        seen = json.loads(lines[-1])
        body = "\n".join(lines[:-1])
        result = json.loads(body[body.index("{") :])
        self.assertTrue(result["ok"], out)
        self.assertEqual(result["provider"], "brave")
        rows = result["outputs"][0]["result"]["results"]
        self.assertIn(f"FAKE-SNIPPET-test query {mark}", rows[0]["description"])
        self.assertEqual(
            seen, {"path": "/res/v1/web/search", "q": f"test query {mark}", "key": True}
        )

    def test_doctor_on_a_copy(self):
        result = self.doctor
        self.assertEqual(
            result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:]
        )


# The shell helpers of the tests of the official plugins step: a state
# directory in /tmp, the tree hash of a directory, and the install path and
# trust of the Brave plugin.
STEP_HELPERS = r"""
set -u
export OPENCLAW_STATE_DIR=/tmp/st XDG_CACHE_HOME=/tmp/st/.cache
mkdir -p /tmp/st
echo '{"plugins":{"entries":{"brave":{"enabled":true}}}}' > /tmp/st/openclaw.json
tree() { (cd "$1" && find . -type f | sort | xargs sha256sum | sha256sum | cut -c1-64); }
brave() {
  node /app/openclaw.mjs plugins inspect brave --json 2>/dev/null | node -e '
    let s = "";
    process.stdin.on("data", d => (s += d)).on("end", () => {
      const j = JSON.parse(s.slice(s.indexOf("{")));
      console.log(j.install.installPath + " " + j.plugin.trust.reason);
    });'
}
"""

# Points the install record of the Brave plugin to another directory, in the
# state database, as a process that can write the state could.
MOVE_RECORD = r"""
const { DatabaseSync } = require("node:sqlite");
const db = new DatabaseSync("/tmp/st/state/openclaw.sqlite");
const key = "plugins.installedIndex";
const row = db.prepare("select value_json from config_machine_state where state_key = ?").get(key);
const index = JSON.parse(row.value_json);
index.index.installRecords.brave.installPath = process.argv[2];
db.prepare("update config_machine_state set value_json = ? where state_key = ?").run(JSON.stringify(index), key);
"""

# A plugin with the id of a bundled plugin (memory-core), from a local path.
SAME_ID_PLUGIN = r"""
mkdir -p /tmp/same-id
cat > /tmp/same-id/package.json <<'JSON'
{"name": "test-same-id", "version": "1.0.0", "type": "module", "openclaw": {"extensions": ["./index.js"]}}
JSON
cat > /tmp/same-id/openclaw.plugin.json <<'JSON'
{"id": "memory-core", "name": "Test", "description": "test", "configSchema": {"type": "object", "properties": {}}}
JSON
echo 'export default { id: "memory-core", name: "Test", description: "test", register() {} };' > /tmp/same-id/index.js
cp /tmp/st/openclaw.json /tmp/config-copy.json
OPENCLAW_CONFIG_PATH=/tmp/config-copy.json node /app/openclaw.mjs plugins install /tmp/same-id \
  --force --no-enable --accept-capabilities > /dev/null 2>&1
"""


def in_step_container(script):
    """A script in a container of the image as the pod runs the step: no
    network, a read-only root, and the state in /tmp."""
    return docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--tmpfs",
        "/tmp:uid=1000,gid=1000,mode=0700",
        IMAGE_REF,
        "sh",
        "-c",
        STEP_HELPERS + script,
        check=False,
        timeout=600,
    )


@unittest.skipUnless(IMAGE_REF, "IMAGE_REF is not set")
class OfficialPluginsStepTrustsNothingInTheState(unittest.TestCase):
    """The gateway can write the state directory, and the product checks no
    hash of installed code. So the step installs the code again at each run
    and stops the start for any other plugin in the state."""

    def test_changed_code_and_record_are_replaced(self):
        out = in_step_container(
            "cat > /tmp/move-record.js <<'JS'\n"
            + MOVE_RECORD
            + "JS\n"
            + r"""
install-official-plugins > /dev/null || exit 1
set -- $(brave); first="$1"
echo "first $1 $2 $(tree "$1")"
echo '// CHANGED' >> "$first/dist/index.js"
mkdir -p /tmp/st/other && cp -R "$first" /tmp/st/other/brave-plugin
echo '// CHANGED' >> /tmp/st/other/brave-plugin/dist/index.js
node /tmp/move-record.js /tmp/st/other/brave-plugin 2> /dev/null
set -- $(brave); echo "changed $1 $2"
install-official-plugins | grep '^official-plugins'
set -- $(brave); echo "second $1 $2 $(tree "$1")"
echo "changed-lines $(grep -c CHANGED "$1/dist/index.js")"
"""
        )
        text = out.stdout
        self.assertEqual(out.returncode, 0, text[-2000:] + out.stderr[-2000:])
        lines = {
            line.split()[0]: line.split()[1:]
            for line in text.splitlines()
            if line.split()
        }
        first, changed, second = lines["first"], lines["changed"], lines["second"]
        self.assertEqual(changed[0], "/tmp/st/other/brave-plugin", text)
        self.assertRegex(text, r"id=brave .*result=installed")
        self.assertTrue(second[0].startswith("/tmp/st/npm/"), text)
        self.assertEqual(second[1], "trusted-official")
        self.assertEqual(second[2], first[2], "the code is not the code of the image")
        self.assertEqual(lines["changed-lines"], ["0"])

    def test_another_install_record_stops_the_start(self):
        out = in_step_container(SAME_ID_PLUGIN + "install-official-plugins; echo rc=$?")
        self.assertIn(
            "official-plugins result=failed reason=an install record of another plugin: memory-core",
            out.stdout,
        )
        self.assertIn("rc=1", out.stdout)

    def test_a_file_in_extensions_stops_the_start(self):
        out = in_step_container(
            "mkdir -p /tmp/st/extensions/extra && echo x > /tmp/st/extensions/extra/index.js; "
            "install-official-plugins; echo rc=$?"
        )
        self.assertIn(
            "official-plugins result=failed reason=files in /tmp/st/extensions",
            out.stdout,
        )
        self.assertIn("rc=1", out.stdout)


if __name__ == "__main__":
    unittest.main()
