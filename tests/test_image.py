"""The checks of the image that need Docker only: bin/image-test runs them
first, on a workstation and in CI.

- The user, the entrypoint and the command of the image contract.
- The OpenShell CLI is the release of the anchor stage of the Dockerfile.
- Each plugin of plugins.txt is installed at the release of the product.
- The image holds nothing of a trust domain: no config, no state, no archive.
- The gateway starts with a read-only root and a read-only config, and loads
  the plugins. The doctor step of the product works on a copy of the config.
"""

import hashlib
import json
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
            "--tmpfs",
            "/tmp",
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
        for plugin_id in ("openshell", "signal", "diagnostics-otel"):
            self.assertIn(plugin_id, plugins)
            self.assertEqual(plugins[plugin_id]["status"], "loaded", plugin_id)
        self.assertEqual(
            plugins["openshell"]["version"], plugins["diagnostics-otel"]["version"]
        )

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

    def test_doctor_on_a_copy(self):
        result = self.doctor
        self.assertEqual(
            result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:]
        )


if __name__ == "__main__":
    unittest.main()
