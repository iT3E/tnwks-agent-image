"""The Claude CLI test: OpenClaw's Claude CLI runtime behind a model gateway.

It runs on the test cluster of the workflow claude-cli.yaml only
(tests/claude-cli/run.sh), with the test image that holds Claude Code and the
config tests/config/claude-cli.json. The released image holds no Claude Code.

It answers three questions with evidence, and writes what it finds to the job
log and the job summary:

1. The gateway path. Does the `claude` process send its model calls to the
   base URL and the gateway key that we set, and to no other address? The
   pod env has a dummy key (ANTHROPIC_AUTH_TOKEN), the config sets a base URL
   in `env.vars`, and the managed settings of Claude Code set another base URL
   and a key helper that prints LITELLM_API_KEY. The mock tells the three
   apart by the path and by a label of the key. A network policy lets the pod
   reach DNS, the mock and OpenShell only, and the cluster DNS logs each name
   that the pod looks up. A second phase turns on
   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC in the managed settings.
2. The sandbox. Where does a command of OpenClaw's `exec` tool (and each
   other tool that runs code or touches files) run when it comes through the
   MCP bridge? The probe of the tool test says where it ran.
3. The native tools. Can the model reach a Claude Code native tool that runs
   code, writes files or fetches URLs? The mock asks for each one, with
   `managedNativeTools.default: []` (agent main), with Agent and Workflow
   opted in (agent wide), and with the opt-in and an open exec policy (agent
   gwexec).

The agents of the config: main (the defaults), wide (Agent and Workflow
opted in), sbexec (`tools.exec.host: sandbox`) and gwexec
(`tools.exec.mode: full`, and Agent and Workflow). sbexec and gwexec change
the exec policy on purpose, to show where a command goes when the policy
lets it run. The findings are not assertions: the test fails only when the
harness can not give the evidence.
"""

import json
import os
import re
import subprocess
import time
import unittest
import uuid
from collections import Counter
from datetime import datetime

from support import (
    AGENT_NS,
    CLUSTER,
    FAKE_SECRETS,
    REPO,
    agent_turn,
    kx,
    load_mock,
    mock_records,
    parse_probes,
    probe_command,
    sandbox_problems,
)

CLAUDE_DIR = "/home/node/.openclaw/claude"
WORKSPACE = {
    "main": "/home/node/.openclaw/workspace",
    "wide": "/home/node/.openclaw/workspace-wide",
    "sbexec": "/home/node/.openclaw/workspace-sbexec",
    "gwexec": "/home/node/.openclaw/workspace-gwexec",
}
MANAGED = REPO / "tests" / "claude-cli" / "managed-settings.json"
QUIET = "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
# A host outside the cluster for the egress control. The runner talks to it
# anyway.
OUTSIDE = ("api.github.com", 443)
MOCK_HOST = ("mock-model.test-support.svc.cluster.local", 8000)
SEARCH = (".agent.svc.cluster.local", ".svc.cluster.local", ".cluster.local")
# Stop the watcher in the pod (the image has no pkill).
STOP_WATCH = """
import os, signal
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        if open(f"/proc/{pid}/cmdline", "rb").read() == b"python3\\0-\\0" + b"3600\\0":
            os.kill(int(pid), signal.SIGTERM)
    except OSError:
        pass
"""


def pod(*command, check=True, timeout=300):
    return kx(
        "-n",
        AGENT_NS,
        "exec",
        "agent",
        "-c",
        "agent",
        "--",
        *command,
        check=check,
        timeout=timeout,
    )


def short(text, n=240):
    return re.sub(r"\s+", " ", (text or "").split("<system-reminder>")[0]).strip()[:n]


def outside_name(name):
    """The name that the pod looked up, if it is outside the cluster."""
    name = name.rstrip(".").lower()
    if re.fullmatch(r"[^.]+\.[^.]+\.svc\.cluster\.local", name):
        return None
    for suffix in SEARCH:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if name.endswith("cluster.local") or "." not in name:
        return None
    return name


def epoch(stamp):
    """An RFC 3339 time of `kubectl logs --timestamps`, as seconds."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z", stamp)
    frac = float("0" + (m.group(2) or ".0")[:7])
    return datetime.fromisoformat(m.group(1) + "+00:00").timestamp() + frac


def arg(argv, flag):
    """The value after a flag in an argument list ("" is a value too)."""
    if flag in argv and argv.index(flag) + 1 < len(argv):
        return argv[argv.index(flag) + 1]
    return None


@unittest.skipUnless(
    os.environ.get("IMAGE_TEST_KUBECONFIG"), "no test cluster (tests/claude-cli/run.sh)"
)
class ClaudeCliRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = load_mock()
        cls.agent_host = pod("hostname").stdout.strip()
        cls.pod_ip = kx(
            "-n", AGENT_NS, "get", "pod", "agent", "-o", "jsonpath={.status.podIP}"
        ).stdout.strip()
        cls.findings = {}
        cls.watch_out = os.path.join(
            os.environ.get("RUNNER_TEMP", "/tmp"),
            f"claude-watch-{uuid.uuid4().hex[:8]}.jsonl",
        )
        cls.start_watch()
        cls.phase = {"turns": time.time()}
        cls.telemetry = {"before": cls.telemetry_files()}
        cls.sandboxes = {"before": cls.sandbox_names()}
        try:
            cls.turns = {
                "main": cls.turn("main", "claude-main"),
                "wide": cls.turn("wide", "claude-native"),
                "sbexec": cls.turn("sbexec", "claude-mcp"),
                "gwexec-mcp": cls.turn("gwexec", "claude-mcp"),
                "gwexec-native": cls.turn("gwexec", "claude-native"),
            }
            cls.sandboxes["after"] = cls.sandbox_names()
            cls.telemetry["turns"] = cls.telemetry_files()
            cls.phase["quiet"] = time.time()
            cls.quiet_applied = cls.set_quiet()
            cls.phase["quiet_turn"] = time.time()
            cls.turns["main-quiet"] = cls.turn("main", "claude-native")
            cls.telemetry["quiet"] = cls.telemetry_files()
            cls.phase["end"] = time.time()
        finally:
            cls.stop_watch()
        cls.processes = cls.read_watch()
        cls.dns = cls.dns_lookups()
        cls.egress = {
            "outside": cls.connect(*OUTSIDE),
            "mock": cls.connect(*MOCK_HOST),
        }
        cls.collect()
        cls.report()

    # -- the harness ------------------------------------------------------

    @classmethod
    def start_watch(cls):
        kubectl = os.environ.get("IMAGE_TEST_KUBECTL", "kubectl")
        command = [
            kubectl,
            "--kubeconfig",
            os.environ["IMAGE_TEST_KUBECONFIG"],
            "--context",
            f"kind-{CLUSTER}",
            "-n",
            AGENT_NS,
            "exec",
            "-i",
            "agent",
            "-c",
            "agent",
            "--",
            "python3",
            "-",
            "3600",
        ]
        cls.watch_file = open(cls.watch_out, "w")
        cls.watcher = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=cls.watch_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        cls.watcher.stdin.write(
            (REPO / "tests" / "claude-cli" / "watch.py").read_text()
        )
        cls.watcher.stdin.close()
        time.sleep(3)

    @classmethod
    def stop_watch(cls):
        cls.watcher.terminate()
        try:
            cls.watcher.wait(timeout=30)
        except subprocess.TimeoutExpired:
            cls.watcher.kill()
        cls.watch_file.close()
        # The watcher in the pod stops by its own deadline, or now.
        pod("python3", "-c", STOP_WATCH, check=False)

    @classmethod
    def read_watch(cls):
        out = []
        with open(cls.watch_out) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    print(f"watcher: {line.rstrip()[:300]}")
        return out

    @classmethod
    def turn(cls, agent, plan):
        mark = f"{agent}-{uuid.uuid4().hex[:8]}"
        started, result = agent_turn(
            f"claude-{mark}",
            f"TOOLPLAN={plan} WRITEMARK={mark} NATIVEMARK={mark}",
            agent=agent,
        )
        time.sleep(2)
        records = mock_records(started)
        own = [r for r in records if (r.get("marks") or {}).get("write") == mark]
        main = [r for r in own if r.get("tools_offered") and not r.get("subagent")]
        last = max(main, key=lambda r: len(r.get("tool_uses", [])), default={})
        uses = [u for u in last.get("tool_uses", []) if u.get("name") != "ToolSearch"]
        results = {r.get("id"): r for r in last.get("tool_results", [])}
        steps = []
        for i, step in enumerate(cls.mock.PLANS[plan]):
            use = uses[i] if i < len(uses) else {}
            res = results.get(use.get("id"), {})
            steps.append(
                {
                    "tool": step["tool"],
                    "asked": bool(use),
                    "error": res.get("error"),
                    "text": res.get("text", ""),
                }
            )
        print(
            f"\nturn {agent}/{plan}: exit {result.returncode}, "
            f"answer {last.get('answer')!r}, {len(uses)} calls"
        )
        for i, s in enumerate(steps):
            print(
                f"  step {i:2d} {s['tool']:<28} error={s['error']} {short(s['text'])}"
            )
        return {
            "agent": agent,
            "plan": plan,
            "mark": mark,
            "started": started,
            "exit": result.returncode,
            "stderr": result.stderr[-2000:],
            "last": last,
            "steps": steps,
            "subagents": [r for r in records if r.get("subagent") == mark],
            "records": own,
        }

    @classmethod
    def telemetry_files(cls):
        out = pod(
            "sh", "-c", f"ls {CLAUDE_DIR}/telemetry 2>/dev/null | wc -l", check=False
        )
        return int(out.stdout.strip() or 0)

    @classmethod
    def sandbox_names(cls):
        out = kx("get", "sandboxes.agents.x-k8s.io", "-A", "-o", "name", check=False)
        return sorted(out.stdout.split())

    @classmethod
    def set_quiet(cls):
        """Add the switch to the managed settings, and wait for the file."""
        settings = json.loads(MANAGED.read_text())
        settings["env"][QUIET] = "1"
        cm = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "claude-managed-settings", "namespace": AGENT_NS},
            "data": {"managed-settings.json": json.dumps(settings, indent=2) + "\n"},
        }
        kx("apply", "-f", "-", input_text=json.dumps(cm))
        deadline = time.time() + 240
        while time.time() < deadline:
            text = pod(
                "cat", "/etc/claude-code/managed-settings.json", check=False
            ).stdout
            if QUIET in text:
                return True
            time.sleep(5)
        return False

    @classmethod
    def dns_lookups(cls):
        """Each lookup of the agent pod in the log of the cluster DNS."""
        out = kx(
            "-n",
            "kube-system",
            "logs",
            "-l",
            "k8s-app=kube-dns",
            "--timestamps",
            "--tail=-1",
            "--max-log-requests=10",
            check=False,
        ).stdout
        pattern = re.compile(
            r"^(\S+) \[INFO\] \[?([0-9a-f.:]+?)\]?:\d+ - \d+ \"(\w+) IN (\S+) "
        )
        lookups = []
        for line in out.splitlines():
            m = pattern.match(line)
            if m and m.group(2) == cls.pod_ip:
                lookups.append(
                    {"time": epoch(m.group(1)), "type": m.group(3), "name": m.group(4)}
                )
        return lookups

    @classmethod
    def connect(cls, host, port):
        script = (
            "const s=require('net').connect({host:process.argv[1],port:+process.argv[2]});"
            "s.setTimeout(8000);"
            "s.on('connect',()=>{console.log('connected');s.destroy()});"
            "s.on('timeout',()=>{console.log('timeout');s.destroy()});"
            "s.on('error',e=>console.log('error '+e.code));"
        )
        out = pod("node", "-e", script, host, str(port), check=False, timeout=60)
        return (out.stdout.strip() or out.stderr.strip())[:200]

    # -- the evidence -----------------------------------------------------

    @classmethod
    def phase_of(cls, t):
        if t < cls.phase["turns"]:
            return "before the turns"
        if t < cls.phase["quiet"]:
            return "turns"
        if t < cls.phase["quiet_turn"]:
            return "switch"
        if t <= cls.phase["end"] + 5:
            return "turn with the switch"
        return "after"

    @classmethod
    def collect(cls):
        f = cls.findings
        # 1. The gateway path.
        claude = [
            r
            for t in cls.turns.values()
            for r in t["records"] + t["subagents"]
            if str(r.get("user_agent", "")).startswith("claude-cli")
        ]
        f["model_calls"] = {
            f"{route} key={key} header={header}": n
            for (route, key, header), n in Counter(
                (r.get("route"), r.get("gateway_key"), r.get("auth_header"))
                for r in claude
            ).items()
        }
        all_records = mock_records()
        f["mock_routes_all"] = dict(
            Counter(
                f"{r.get('route') or r.get('path')} key={r.get('gateway_key')}"
                for r in all_records
            )
        )
        procs = [p for p in cls.processes if p.get("kind") == "process"]
        names = Counter()
        for p in procs:
            for n in p.get("env_names", []):
                if n.startswith(("ANTHROPIC", "CLAUDE")) or n in FAKE_SECRETS:
                    names[n] += 1
        f["claude_processes"] = len(procs)
        f["claude_env_names"] = dict(names)
        f["claude_env_base_url"] = sorted(
            {p["env"].get("ANTHROPIC_BASE_URL", "(unset)") for p in procs}
        )
        dns = {}
        for lookup in cls.dns:
            name = outside_name(lookup["name"])
            if name:
                dns.setdefault(cls.phase_of(lookup["time"]), Counter())[name] += 1
        f["dns_outside_names"] = {k: dict(v) for k, v in dns.items()}
        f["dns_lookups_of_pod"] = len(cls.dns)
        f["telemetry_files"] = cls.telemetry
        f["egress"] = cls.egress
        f["quiet_applied"] = cls.quiet_applied

        # 2. The sandbox: each MCP step of each turn.
        mcp = {}
        for key, t in cls.turns.items():
            for i, s in enumerate(t["steps"]):
                if not s["tool"].startswith("mcp__"):
                    continue
                probes = parse_probes(s["text"])
                where = []
                for probe in probes:
                    if probe.get("host") == cls.agent_host:
                        where.append("agent pod")
                    elif not sandbox_problems(probe, cls.agent_host):
                        where.append("clean sandbox")
                    else:
                        where.append(
                            f"other: {sandbox_problems(probe, cls.agent_host)}"
                        )
                host = None
                if s["error"] and "exec denied" in s["text"]:
                    where.append("denied: " + short(s["text"], 80))
                elif not probes:
                    host = re.search(r"host=(\S+)", s["text"])
                if host:
                    where.append(
                        "agent pod"
                        if host.group(1) == cls.agent_host
                        else f"host {host.group(1)}"
                    )
                mcp[f"{key} step {i} {s['tool']}"] = {
                    "error": s["error"],
                    "where": where,
                    "result": short(s["text"], 160),
                }
        f["mcp_steps"] = mcp
        files = {}
        for key, t in cls.turns.items():
            if t["plan"] not in ("claude-main", "claude-mcp"):
                continue
            ws = WORKSPACE[t["agent"]]
            out = pod(
                "sh",
                "-c",
                f"cd {ws} && for f in claude-tool-test.txt claude-patch-test.txt; do "
                f'if [ -f $f ]; then echo "$f: $(cat $f)"; else echo "$f: absent"; fi; done',
                check=False,
            ).stdout
            files[key] = out.strip().splitlines()
        f["files_in_pod_workspace"] = files
        f["sandboxes"] = cls.sandboxes

        # 3. The native tools.
        native = {}
        for key, t in cls.turns.items():
            for s in t["steps"]:
                if s["tool"].startswith("mcp__"):
                    continue
                native.setdefault(key, {})[s["tool"]] = (
                    f"error={s['error']} {short(s['text'], 140)}"
                )
        f["native_steps"] = native
        launches = {}
        for p in procs:
            argv = p.get("argv", [])
            if "--mcp-config" not in argv:
                continue
            agent = next(
                (a for a, ws in WORKSPACE.items() if p.get("cwd") == ws), p.get("cwd")
            )
            launches.setdefault(agent, set()).add(
                json.dumps(
                    {
                        "--tools": arg(argv, "--tools"),
                        "--disallowedTools": arg(argv, "--disallowedTools"),
                        "--setting-sources": arg(argv, "--setting-sources"),
                        "--permission-mode": arg(argv, "--permission-mode"),
                    }
                )
            )
        f["launch_args"] = {a: sorted(v) for a, v in launches.items()}
        side = {}
        get_paths = [r.get("path", "") for r in all_records if r.get("api") == "get"]
        for key, t in cls.turns.items():
            if t["plan"] not in ("claude-main", "claude-native"):
                continue
            mark = t["mark"]
            written = pod(
                "sh",
                "-c",
                f"ls /tmp/native-write-{mark}.txt /tmp/native-{mark}.ipynb 2>/dev/null",
                check=False,
            ).stdout.split()
            side[key] = {
                "native_files_in_pod": written,
                "webfetch_reached_mock": any(mark in p for p in get_paths),
                "subagent_requests": len(t["subagents"]),
                "subagent_tools": sorted(
                    {n for r in t["subagents"] for n in r.get("tools_offered", [])}
                ),
            }
        f["native_side_effects"] = side

    @classmethod
    def report(cls):
        text = json.dumps(cls.findings, indent=1, sort_keys=True, default=sorted)
        print("\n==== findings\n" + text)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as out:
                out.write(
                    "## Claude CLI runtime: findings\n\n```json\n" + text + "\n```\n"
                )

    # -- the harness checks -------------------------------------------------

    def test_each_turn_ran_its_plan(self):
        for key, t in self.turns.items():
            with self.subTest(turn=key):
                self.assertEqual(t["exit"], 0, t["stderr"])
                self.assertEqual(t["last"].get("answer"), "MOCK-FINAL")
                self.assertTrue(all(s["asked"] for s in t["steps"]), t["steps"])
                self.assertTrue(
                    all(s["error"] is not None for s in t["steps"]),
                    "a step has no result",
                )

    def test_the_watcher_saw_claude(self):
        self.assertGreater(self.findings["claude_processes"], 0)
        self.assertTrue(self.findings["launch_args"])

    def test_the_dns_log_has_the_pod(self):
        self.assertGreater(
            len(self.dns), 0, "no lookup of the agent pod in the DNS log"
        )

    def test_the_switch_reached_the_pod(self):
        self.assertTrue(self.quiet_applied)

    def test_the_mock_is_reachable(self):
        self.assertEqual(self.egress["mock"], "connected")

    def test_the_probe_finds_the_files_of_the_agent_pod(self):
        # The control of the tool test: the probe finds the things of the
        # agent pod, so a zero in a sandbox means a clean sandbox.
        out = pod("sh", "-c", probe_command(), timeout=300).stdout
        probes = parse_probes(out)
        self.assertEqual(len(probes), 1, out)
        probe = probes[0]
        self.assertEqual(probe["host"], self.agent_host)
        for key in ("env_secret_values", "config_file", "state_dirs", "marker_files"):
            self.assertGreater(int(probe[key]), 0, key)


if __name__ == "__main__":
    unittest.main()
