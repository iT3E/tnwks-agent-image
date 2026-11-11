#!/usr/bin/env python3
"""Print the manifests of the test cluster, as one JSON list for kubectl.

Usage: manifests.py support|agent|claude-agent <image ref>

- support: the namespaces, the mock model server and the trace collector.
- agent: the agent pod, as tnwks-ops runs it: a restricted pod, a read-only
  root, the config as a read-only file from a ConfigMap, the secrets as env
  vars from a Secret, the state and the archives on one volume, and the
  client certificate of the OpenShell gateway.
- claude-agent: the same pod with the config of the Claude CLI test
  (tests/claude-cli), the managed settings of Claude Code, a dummy gateway key
  in the pod env, and a network policy that lets the pod reach DNS, the test
  support namespace and OpenShell only. The image is the test image with
  Claude Code (tests/claude-cli/Dockerfile).

The image references come from the Dockerfiles (tests/support.py), so that
Renovate moves them. The values of the test are fake.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import (  # noqa: E402
    AGENT_NS,
    ARCHIVE_NAME,
    FAKE_SECRETS,
    FILE_MARKER,
    OPENSHELL_NS,
    POD_ENV_KEY,
    REPO,
    SUPPORT_NS,
    image_pins,
    mock_env,
)

# The endpoint of the OpenShell gateway, and the directory where the CLI
# looks for its client certificate: the endpoint with each `:` and `/` as `_`.
OPENSHELL_ENDPOINT = f"https://openshell.{OPENSHELL_NS}.svc.cluster.local:8080"
OPENSHELL_CERT_DIR = "/home/node/.config/openshell/gateways/https___openshell.openshell.svc.cluster.local_8080/mtls"

RESTRICTED = {
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "capabilities": {"drop": ["ALL"]},
}


def namespace(name, enforce=None):
    labels = {"pod-security.kubernetes.io/warn": "restricted"}
    if enforce:
        labels["pod-security.kubernetes.io/enforce"] = enforce
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": name, "labels": labels},
    }


def deployment(name, ns, container, volumes=()):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65534,
                        "runAsGroup": 65534,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [dict(container, securityContext=RESTRICTED)],
                    "volumes": list(volumes),
                },
            },
        },
    }


def service(name, ns, ports):
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "selector": {"app": name},
            "ports": [{"name": n, "port": p, "targetPort": p} for n, p in ports],
        },
    }


def config_map(name, ns, data):
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": ns},
        "data": data,
    }


def support(pins):
    collector_config = {
        "receivers": {
            "otlp": {
                "protocols": {
                    "http": {"endpoint": "0.0.0.0:4318"},
                    "grpc": {"endpoint": "0.0.0.0:4317"},
                }
            }
        },
        "exporters": {"debug": {"verbosity": "detailed"}},
        "service": {
            "pipelines": {
                "traces": {"receivers": ["otlp"], "exporters": ["debug"]},
                "metrics": {"receivers": ["otlp"], "exporters": ["debug"]},
                "logs": {"receivers": ["otlp"], "exporters": ["debug"]},
            }
        },
    }
    mock = {
        "name": "mock",
        "image": pins["python"],
        "command": ["python3", "-u", "/src/mock.py", "8000"],
        "env": [{"name": k, "value": v} for k, v in mock_env().items()],
        "ports": [{"containerPort": 8000}],
        "readinessProbe": {"tcpSocket": {"port": 8000}, "periodSeconds": 2},
        "volumeMounts": [{"name": "src", "mountPath": "/src"}],
        "resources": {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"memory": "256Mi"},
        },
    }
    collector = {
        "name": "otelcol",
        # The collector config is JSON, which is YAML too.
        "image": pins["collector"],
        "args": ["--config=/etc/otelcol/config.yaml"],
        "ports": [{"containerPort": 4318}, {"containerPort": 4317}],
        "readinessProbe": {"tcpSocket": {"port": 4318}, "periodSeconds": 2},
        "volumeMounts": [{"name": "config", "mountPath": "/etc/otelcol"}],
        "resources": {
            "requests": {"cpu": "10m", "memory": "64Mi"},
            "limits": {"memory": "512Mi"},
        },
    }
    return [
        namespace(SUPPORT_NS, enforce="restricted"),
        namespace(AGENT_NS, enforce="restricted"),
        config_map(
            "mock-src",
            SUPPORT_NS,
            {"mock.py": (REPO / "tests" / "mock-model" / "mock.py").read_text()},
        ),
        deployment(
            "mock-model",
            SUPPORT_NS,
            mock,
            [{"name": "src", "configMap": {"name": "mock-src"}}],
        ),
        service("mock-model", SUPPORT_NS, [("http", 8000)]),
        config_map(
            "otelcol",
            SUPPORT_NS,
            {"config.yaml": json.dumps(collector_config, indent=2)},
        ),
        deployment(
            "otelcol",
            SUPPORT_NS,
            collector,
            [{"name": "config", "configMap": {"name": "otelcol"}}],
        ),
        service("otelcol", SUPPORT_NS, [("http", 4318), ("grpc", 4317)]),
    ]


def agent(pins, image):
    env = [
        {"name": "XDG_CACHE_HOME", "value": "/home/node/.openclaw/.cache"},
        # A value of the config that names an env var (`${OPENSHELL_SANDBOX_FROM}`).
        {"name": "OPENSHELL_SANDBOX_FROM", "value": pins["python"]},
    ]
    secrets = [{"secretRef": {"name": "agent-secrets"}}]
    state = [
        {"name": "volume", "mountPath": "/home/node/.openclaw", "subPath": "state"},
        {
            "name": "volume",
            "mountPath": "/home/node/.openclaw-archives",
            "subPath": "archives",
        },
        {
            "name": "config",
            "mountPath": "/home/node/.openclaw/openclaw.json",
            "subPath": "openclaw.json",
            "readOnly": True,
        },
        # /tmp is a directory of the user with mode 0700 in an emptyDir. The
        # sandbox plugin refuses a temp dir under a dir that others can write
        # with no sticky bit, and an emptyDir has mode 2777.
        {"name": "tmp", "mountPath": "/tmp", "subPath": "tmp"},
        {"name": "user-config", "mountPath": "/home/node/.config"},
    ]
    # The first init container stands for what is on a state volume and
    # what the ops repository adds: the directories of the volume and the
    # temp dir with the owner of the image user, a credentials file and an
    # archive (each with a marker that the probe looks for), and the client
    # certificate of the OpenShell gateway in the directory of the CLI.
    prepare = "\n".join(
        [
            "set -eu",
            "mkdir -p /volume/state/credentials /volume/archives /scratch/tmp",
            "chmod 0700 /volume/state /volume/archives /scratch/tmp",
            f"printf '{{\"marker\": \"%s\"}}\\n' '{FILE_MARKER}' > /volume/state/credentials/test-marker.json",
            f"printf '%s\\n' '{FILE_MARKER}' > /scratch/tmp/marker.txt",
            f"tar -czf /volume/archives/{ARCHIVE_NAME} -C /scratch/tmp marker.txt",
            "rm /scratch/tmp/marker.txt",
            f"mkdir -p {OPENSHELL_CERT_DIR}",
            f"cp /openshell-tls/ca.crt /openshell-tls/tls.crt /openshell-tls/tls.key {OPENSHELL_CERT_DIR}/",
            f"chmod 0600 {OPENSHELL_CERT_DIR}/*",
        ]
    )
    # The second init container runs the doctor step of the product on a
    # copy of the config. The config is read only, and doctor writes to it.
    doctor = (
        "mkdir -p /tmp/doctor && cp /home/node/.openclaw/openclaw.json /tmp/doctor/openclaw.json "
        "&& exec node /app/openclaw.mjs doctor --fix --non-interactive"
    )
    ready = (
        "require('http').get('http://127.0.0.1:18789/readyz', r => process.exit(r.statusCode < 400 ? 0 : 1))"
        ".on('error', () => process.exit(1))"
    )
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "agent",
            "namespace": AGENT_NS,
            "labels": {"app": "agent"},
        },
        "spec": {
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "fsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "initContainers": [
                {
                    "name": "prepare",
                    "image": image,
                    "imagePullPolicy": "Never",
                    "command": ["sh", "-c", prepare],
                    "securityContext": RESTRICTED,
                    "volumeMounts": [
                        {"name": "volume", "mountPath": "/volume"},
                        {"name": "tmp", "mountPath": "/scratch"},
                        {"name": "user-config", "mountPath": "/home/node/.config"},
                        {
                            "name": "openshell-tls",
                            "mountPath": "/openshell-tls",
                            "readOnly": True,
                        },
                    ],
                },
                {
                    "name": "doctor",
                    "image": image,
                    "imagePullPolicy": "Never",
                    "command": ["sh", "-c", doctor],
                    "env": env
                    + [
                        {
                            "name": "OPENCLAW_CONFIG_PATH",
                            "value": "/tmp/doctor/openclaw.json",
                        }
                    ],
                    "envFrom": secrets,
                    "securityContext": RESTRICTED,
                    "volumeMounts": state,
                    "resources": {"limits": {"memory": "2Gi"}},
                },
            ],
            "containers": [
                {
                    "name": "agent",
                    "image": image,
                    "imagePullPolicy": "Never",
                    "env": env,
                    "envFrom": secrets,
                    "ports": [{"name": "gateway", "containerPort": 18789}],
                    "readinessProbe": {
                        "exec": {"command": ["node", "-e", ready]},
                        "initialDelaySeconds": 5,
                        "periodSeconds": 5,
                        "timeoutSeconds": 5,
                    },
                    "securityContext": RESTRICTED,
                    "volumeMounts": state,
                    # The tests run turns with the CLI in this container, which
                    # adds a process (spike notes: 2 GiB was not enough).
                    "resources": {
                        "requests": {"cpu": "250m", "memory": "1Gi"},
                        "limits": {"memory": "4Gi"},
                    },
                }
            ],
            "volumes": [
                # An emptyDir stands for the state volume of a trust domain.
                {"name": "volume", "emptyDir": {}},
                {"name": "config", "configMap": {"name": "agent-config"}},
                {"name": "tmp", "emptyDir": {}},
                {"name": "user-config", "emptyDir": {"medium": "Memory"}},
                {
                    "name": "openshell-tls",
                    "secret": {
                        "secretName": "openshell-client-tls",
                        "defaultMode": 0o440,
                    },
                },
            ],
        },
    }
    return [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "agent-secrets", "namespace": AGENT_NS},
            "type": "Opaque",
            "stringData": FAKE_SECRETS,
        },
        config_map(
            "agent-config",
            AGENT_NS,
            {
                "openclaw.json": (
                    REPO / "tests" / "config" / "openclaw.json"
                ).read_text()
            },
        ),
        pod,
    ]


def claude_agent(pins, image):
    """The agent pod of the Claude CLI test (tests/test_claude_cli.py)."""
    items = agent(pins, image)
    secret, config, pod = items
    config["data"]["openclaw.json"] = (
        REPO / "tests" / "config" / "claude-cli.json"
    ).read_text()
    container = pod["spec"]["containers"][0]
    # A gateway key in the pod env, as a person could set it for Claude Code.
    # The test looks for it at the mock.
    container["env"] = container["env"] + [
        {"name": "ANTHROPIC_AUTH_TOKEN", "value": POD_ENV_KEY}
    ]
    # The managed settings of Claude Code (its admin policy file). The
    # directory is a ConfigMap, so that the test can change the file.
    container["volumeMounts"] = container["volumeMounts"] + [
        {"name": "claude-managed", "mountPath": "/etc/claude-code", "readOnly": True}
    ]
    pod["spec"]["volumes"].append(
        {"name": "claude-managed", "configMap": {"name": "claude-managed-settings"}}
    )
    managed = config_map(
        "claude-managed-settings",
        AGENT_NS,
        {
            "managed-settings.json": (
                REPO / "tests" / "claude-cli" / "managed-settings.json"
            ).read_text()
        },
    )
    # Egress: DNS, the mock and the collector, and OpenShell. Nothing else,
    # so no call can reach a model API on the internet.
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "agent-egress", "namespace": AGENT_NS},
        "spec": {
            "podSelector": {"matchLabels": {"app": "agent"}},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "kube-system"
                                }
                            },
                            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                        }
                    ],
                    "ports": [
                        {"protocol": "UDP", "port": 53},
                        {"protocol": "TCP", "port": 53},
                    ],
                },
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": ns}
                            }
                        }
                        for ns in (SUPPORT_NS, OPENSHELL_NS)
                    ]
                },
            ],
        },
    }
    return [secret, config, managed, policy, pod]


MODES = {"support": None, "agent": agent, "claude-agent": claude_agent}


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in MODES:
        sys.exit(__doc__)
    pins = image_pins()
    if sys.argv[1] == "support":
        items = support(pins)
    else:
        items = MODES[sys.argv[1]](pins, sys.argv[2])
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=1))


if __name__ == "__main__":
    main()
