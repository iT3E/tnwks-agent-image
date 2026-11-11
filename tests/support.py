"""Helpers for the image tests: the pins, docker, kubectl and the probe.

The tests use the standard library only. They read the image under test from
the environment variable IMAGE_REF, which bin/image-test sets.
"""

import importlib.util
import json
import os
import re
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IMAGE_REF = os.environ.get("IMAGE_REF", "")

# The names of the test cluster. bin/image-test and tests/kind/up.sh use the
# same names.
CLUSTER = "tnwks-image-test"
AGENT_NS = "agent"
OPENSHELL_NS = "openshell"
SUPPORT_NS = "test-support"

# The fake values of the test. They are not secrets: they let a test find a
# value of a secret where it must not be.
FAKE_SECRETS = {
    "OPENCLAW_GATEWAY_TOKEN": "fake-gateway-token-7f3a91",
    "LITELLM_API_KEY": "fake-model-key-2c8d04",
    "TELEGRAM_BOT_TOKEN": "fake-bot-token-9b51e6",
}
# A dummy gateway key in the env of the pod (the Claude CLI test only). The
# mock names each key that it sees by a label, never by its value.
POD_ENV_KEY = "dummy-pod-env-key-4e1b77"
# The fake key of the bridge spike (tests/bridge-spike): the upstream key in
# the environment of the bridge.
BRIDGE_ENV_KEY = "dummy-bridge-env-key-7c2d90"
ARCHIVE_NAME = "openclaw-backup-test-archive.tar.gz"
FILE_MARKER = "TNWKS-STATE-FILE-MARKER-5e0c72"


def from_lines(path):
    """The `FROM <ref> AS <name>` lines of a Dockerfile, as {name: ref}."""
    refs = {}
    for line in (REPO / path).read_text().splitlines():
        m = re.match(r"^FROM\s+(\S+)\s+AS\s+(\S+)\s*$", line, re.IGNORECASE)
        if m:
            refs[m.group(2)] = m.group(1)
    return refs


def image_pins():
    """The pins of the image (Dockerfile) and of the test cluster."""
    pins = from_lines("Dockerfile")
    pins.update(from_lines(".github/test-cluster/Dockerfile"))
    return pins


def tag_of(ref):
    """The tag of `repo:tag@sha256:...`."""
    name = ref.split("@", 1)[0]
    return name.rsplit(":", 1)[1] if ":" in name.rsplit("/", 1)[-1] else ""


def digest_of(ref):
    return ref.split("@", 1)[1] if "@" in ref else ""


def openshell_version():
    """The OpenShell release of the image: the tag of the anchor stage."""
    return tag_of(image_pins()["openshell-release"])


def run(args, check=True, timeout=600, input_text=None):
    """Run a command and return its result, with text output."""
    return subprocess.run(
        args,
        check=check,
        timeout=timeout,
        capture_output=True,
        text=True,
        input=input_text,
    )


def docker(*args, check=True, timeout=600):
    return run(["docker", *args], check=check, timeout=timeout)


def in_image(*command, entrypoint="sh", user=None):
    """Run a command in a new container of the image under test."""
    as_user = ["--user", user] if user else []
    return docker(
        "run",
        "--rm",
        "--network",
        "none",
        *as_user,
        "--entrypoint",
        entrypoint,
        IMAGE_REF,
        *command,
    )


def kx(*args, check=True, timeout=600, input_text=None):
    """kubectl on the test cluster only. It never uses the default kubeconfig."""
    kubeconfig = os.environ.get("IMAGE_TEST_KUBECONFIG")
    if not kubeconfig:
        raise RuntimeError(
            "IMAGE_TEST_KUBECONFIG is not set: no kubectl call without the kubeconfig of the test cluster"
        )
    kubectl = os.environ.get("IMAGE_TEST_KUBECTL", "kubectl")
    return run(
        [kubectl, "--kubeconfig", kubeconfig, "--context", f"kind-{CLUSTER}", *args],
        check=check,
        timeout=timeout,
        input_text=input_text,
    )


def load_mock():
    """The mock model server module: its plans and its probe."""
    spec = importlib.util.spec_from_file_location(
        "mock_model", REPO / "tests" / "mock-model" / "mock.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mock_env():
    """The environment of the mock: what the probe looks for."""
    return {
        "MOCK_SECRET_VALUES": ",".join(FAKE_SECRETS.values()),
        "MOCK_ARCHIVE_NAME": ARCHIVE_NAME,
        "MOCK_FILE_MARKER": FILE_MARKER,
        "MOCK_GATEWAY_KEYS": f"pod-env={POD_ENV_KEY},secret-env={FAKE_SECRETS['LITELLM_API_KEY']},"
        f"bridge-env={BRIDGE_ENV_KEY}",
    }


def probe_command():
    """The probe of the mock, with the values of this test."""
    mock = load_mock()
    os.environ.update(mock_env())
    return mock.placeholders({"write": "none", "trace": "none"}, [])["@@PROBE@@"]


def parse_probes(text):
    """Each probe output in a text, as a dict of its `key=value` lines."""
    out = []
    # A result in JSON (dispatch_action) has each newline as `\n`.
    text = (text or "").replace("\\n", "\n")
    for block in re.findall(r"PROBE-BEGIN\n(.*?)PROBE-END", text, re.DOTALL):
        values = {}
        for line in block.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
        out.append(values)
    return out


# What a probe must show in a sandbox. Each count is of a thing of the agent
# pod: a secret value, the config, the state, an archive, a marker file.
SANDBOX_ZERO = (
    "sa_token",
    "env_secret_values",
    "config_file",
    "state_dirs",
    "archive_dir",
    "archive_files",
    "marker_files",
)


def sandbox_problems(probe, agent_host):
    """Each way in which a probe did not run in a clean sandbox."""
    problems = []
    if probe.get("openshell_sandbox") != "1":
        problems.append(f"OPENSHELL_SANDBOX={probe.get('openshell_sandbox')}")
    if probe.get("host") in ("", None, agent_host):
        problems.append(f"host={probe.get('host')} (the agent pod is {agent_host})")
    if probe.get("uid") in ("", None, "0", "1000"):
        problems.append(f"uid={probe.get('uid')}")
    if probe.get("cap_eff", "").strip("0"):
        problems.append(f"cap_eff={probe.get('cap_eff')}")
    for name in FAKE_SECRETS:
        if name in probe.get("env_names", "").split(","):
            problems.append(f"env has {name}")
    for key in SANDBOX_ZERO:
        if probe.get(key) != "0":
            problems.append(f"{key}={probe.get(key)}")
    return problems


def mock_records(since=None):
    """The JSON lines of the mock model server in the test cluster."""
    args = ["-n", SUPPORT_NS, "logs", "deploy/mock-model"]
    if since:
        args.append(f"--since-time={since}")
    records = []
    for line in kx(*args).stdout.splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def agent_turn(session, message, timeout=900, agent=None):
    """One turn of the agent in the test pod, through the CLI of the product."""
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    which = ["--agent", agent] if agent else []
    result = kx(
        "-n",
        AGENT_NS,
        "exec",
        "agent",
        "-c",
        "agent",
        "--",
        "node",
        "/app/openclaw.mjs",
        "agent",
        *which,
        "--session-id",
        session,
        "--message",
        message,
        "--json",
        check=False,
        timeout=timeout,
    )
    return started, result
