"""The sandbox_exec spike: a sandboxed shell for the Claude CLI runtime.

It runs after tests/test_claude_cli.py, on the same test cluster, with the
test image of tests/claude-cli/Dockerfile. That image holds the spike plugin
tests/claude-cli/sandbox-exec, which registers the tool `sandbox_exec`: it
runs one command in the OpenShell sandbox of the session, through the public
plugin SDK. The image that ships does not hold it.

It answers six questions with evidence, and writes what it finds to the job
log and the job summary:

1. Does a Claude CLI turn get `sandbox_exec` through the loopback bridge,
   with the native tools still off and OpenClaw's `exec` still denied?
2. Does the command run in an OpenShell sandbox and not in the agent pod?
   The probe of the tool test says where it ran.
3. Which plugin APIs does it take? (The plugin and the job log: the result of
   each call names the transport that the backend gave.)
4. The workspace: does the sandbox see a file that the agent wrote, and does
   a file that the command writes come back to the agent?
5. The lifecycle: which sandbox does each call and each session get, who
   deletes it, and does a time limit stop a command?
6. The policy: does a per-agent `tools.deny` or `tools.allow` remove the
   tool?

The agents of the config: main (the defaults, exec denied), noshell
(`tools.deny: ["sandbox_exec"]`) and narrow (`tools.allow: ["read"]`). The
config adds `sandbox_exec` to the sandbox tool policy
(`tools.sandbox.tools.alsoAllow`), as the docs say a plugin tool needs. The
findings are not assertions: the test fails only when the harness can not
give the evidence.
"""

import json
import os
import re
import time
import unittest
import uuid

from support import (
    agent_turn,
    kx,
    load_mock,
    mock_records,
    parse_probes,
    probe_command,
    sandbox_problems,
)
from test_claude_cli import pod, short

WORKSPACE = {
    "main": "/home/node/.openclaw/workspace",
    "noshell": "/home/node/.openclaw/workspace-noshell",
    "narrow": "/home/node/.openclaw/workspace-narrow",
}
TOOL = "mcp__openclaw__sandbox_exec"
CLI = ["node", "/app/openclaw.mjs"]


def sandbox_crs():
    """The OpenShell Sandbox resources of the cluster: name, age, phase."""
    out = kx("get", "sandboxes.agents.x-k8s.io", "-A", "-o", "json", check=False)
    try:
        items = json.loads(out.stdout or "{}").get("items", [])
    except ValueError:
        return [f"unreadable: {short(out.stdout + out.stderr, 200)}"]
    return sorted(
        f"{i['metadata'].get('namespace')}/{i['metadata']['name']} "
        f"created={i['metadata'].get('creationTimestamp')}"
        for i in items
    )


def cli(*args, timeout=300):
    out = pod(*CLI, *args, check=False, timeout=timeout)
    return {"exit": out.returncode, "out": short(out.stdout + out.stderr, 1500)}


def host_of(text):
    m = re.search(r"host=(\S+)", text or "")
    return m.group(1) if m else None


@unittest.skipUnless(
    os.environ.get("IMAGE_TEST_KUBECONFIG"), "no test cluster (tests/claude-cli/run.sh)"
)
class SandboxExecSpike(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = load_mock()
        cls.agent_host = pod("hostname").stdout.strip()
        cls.findings = {}
        cls.lifecycle = {
            "crs_before": sandbox_crs(),
            "cli_list_before": cli("sandbox", "list", "--json"),
        }
        session = uuid.uuid4().hex[:8]
        cls.turns = {
            "main-a": cls.turn("main", "claude-sbx", f"sbx-a-{session}"),
            "main-b": cls.turn("main", "claude-sbx-host", f"sbx-b-{session}"),
            "noshell": cls.turn("noshell", "claude-sbx-host", f"sbx-n-{session}"),
            "narrow": cls.turn("narrow", "claude-sbx-host", f"sbx-w-{session}"),
        }
        cls.lifecycle["crs_after_turns"] = sandbox_crs()
        cls.lifecycle["cli_list_after_turns"] = cli("sandbox", "list", "--json")
        cls.workspace = pod(
            "sh",
            "-c",
            f"cd {WORKSPACE['main']} && for f in sbx-in.txt sbx-out.txt; do "
            f'if [ -f $f ]; then echo "$f: $(cat $f)"; else echo "$f: absent"; fi; done',
            check=False,
        ).stdout.strip()
        cls.lifecycle["recreate"] = cli("sandbox", "recreate", "--all", "--force")
        deadline = time.time() + 180
        while time.time() < deadline:
            left = sandbox_crs()
            if not left:
                break
            time.sleep(10)
        cls.lifecycle["crs_after_recreate"] = sandbox_crs()
        cls.lifecycle["cli_list_after_recreate"] = cli("sandbox", "list", "--json")
        cls.collect()
        cls.report()

    @classmethod
    def turn(cls, agent, plan, session):
        mark = f"{agent}-{uuid.uuid4().hex[:8]}"
        started, result = agent_turn(
            session, f"TOOLPLAN={plan} WRITEMARK={mark} NATIVEMARK={mark}", agent=agent
        )
        time.sleep(2)
        own = [
            r
            for r in mock_records(started)
            if (r.get("marks") or {}).get("write") == mark
        ]
        main = [r for r in own if r.get("tools_offered") and not r.get("subagent")]
        last = max(main, key=lambda r: len(r.get("tool_uses", [])), default={})
        uses = [u for u in last.get("tool_uses", []) if u.get("name") != "ToolSearch"]
        results = {r.get("id"): r for r in last.get("tool_results", [])}
        offered = sorted({n for r in main for n in r.get("tools_offered", [])})
        loaded = [
            r.get("text", "")
            for r in last.get("tool_results", [])
            if any(
                u.get("id") == r.get("id") and u.get("name") == "ToolSearch"
                for u in last.get("tool_uses", [])
            )
        ]
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
        forced = [r.get("answer") for r in own if r.get("forced")]
        print(
            f"\nturn {agent}/{plan} ({session}): exit {result.returncode}, "
            f"answer {last.get('answer')!r}, {len(uses)} calls"
        )
        for i, s in enumerate(steps):
            print(
                f"  step {i:2d} {s['tool']:<30} error={s['error']} {short(s['text'], 400)}"
            )
        return {
            "agent": agent,
            "plan": plan,
            "session": session,
            "mark": mark,
            "exit": result.returncode,
            "stderr": result.stderr[-2000:],
            "last": last,
            "steps": steps,
            "offered": offered,
            "tool_search_results": [short(t, 300) for t in loaded],
            "forced": forced,
        }

    @classmethod
    def where(cls, text):
        """Where a probe ran, from the result text of a step."""
        probes = parse_probes(text)
        if not probes:
            host = host_of(text)
            if host is None:
                return "no probe"
            return "agent pod" if host == cls.agent_host else f"host {host}"
        out = []
        for probe in probes:
            problems = sandbox_problems(probe, cls.agent_host)
            if probe.get("host") == cls.agent_host:
                out.append("agent pod")
            elif not problems:
                out.append(f"clean sandbox (host {probe.get('host')})")
            else:
                out.append(f"other: {problems}")
        return "; ".join(out)

    @classmethod
    def collect(cls):
        f = cls.findings
        a = cls.turns["main-a"]
        steps = a["steps"]
        # 1. The tool reaches the turn; exec and the native tools stay off.
        f["q1_offered"] = {
            key: {
                "sandbox_exec": TOOL in t["offered"],
                "exec": "mcp__openclaw__exec" in t["offered"],
                "Bash": "Bash" in t["offered"],
                "ToolSearch": "ToolSearch" in t["offered"],
                "count": len(t["offered"]),
            }
            for key, t in cls.turns.items()
        }
        f["q1_offered_main"] = cls.turns["main-a"]["offered"]
        f["q1_steps_main"] = {
            f"{i} {s['tool']}": f"error={s['error']} {short(s['text'], 220)}"
            for i, s in enumerate(steps)
        }
        # 2. Where the commands ran.
        f["q2_where"] = {
            f"main-a step {i}": cls.where(s["text"])
            for i, s in enumerate(steps)
            if s["tool"] == TOOL
        }
        f["q2_where"]["main-b step 0"] = cls.where(
            cls.turns["main-b"]["steps"][0]["text"]
        )
        transports = sorted(
            set(
                re.findall(
                    r"\(sandbox ([^,]+), via ([^)]+)\)",
                    " ".join(s["text"] for t in cls.turns.values() for s in t["steps"]),
                )
            )
        )
        f["q3_sandbox_and_transport"] = [f"{r} via {v}" for r, v in transports]
        # 4. The workspace.
        mirror = steps[2]["text"]
        f["q4_sandbox_saw_agent_file"] = f"MIRROR-IN {a['mark']}" in mirror
        f["q4_sandbox_pwd"] = (re.search(r"pwd=(\S+)", mirror) or [None, None])[1]
        f["q4_read_tool_saw_sandbox_file"] = (
            f"MIRROR-OUT {a['mark']}" in steps[3]["text"]
        )
        f["q4_pod_workspace"] = cls.workspace.splitlines()
        f["q4_session_b_ls"] = short(cls.turns["main-b"]["steps"][0]["text"], 400)
        # 5. The lifecycle.
        hosts = {
            f"main-a step {i}": host_of(s["text"])
            for i, s in enumerate(steps)
            if s["tool"] == TOOL
        }
        hosts["main-b step 0"] = host_of(cls.turns["main-b"]["steps"][0]["text"])
        f["q5_hosts"] = hosts
        f["q5_timeout_step"] = short(steps[6]["text"], 300)
        f["q5_left_running"] = (
            re.search(r"left_running=(\d+)", steps[7]["text"]) or [None, None]
        )[1]
        f["q5_lifecycle"] = cls.lifecycle
        # 6. The policy.
        f["q6_policy"] = {
            key: {
                "offered": TOOL in t["offered"],
                "tool_search": t["tool_search_results"],
                "forced_call": t["forced"],
                "result": short(t["steps"][0]["text"], 240),
            }
            for key, t in cls.turns.items()
            if key in ("noshell", "narrow", "main-b")
        }

    @classmethod
    def report(cls):
        text = json.dumps(cls.findings, indent=1, sort_keys=True)
        print("\n==== sandbox_exec findings\n" + text)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as out:
                out.write(
                    "## sandbox_exec spike: findings\n\n```json\n" + text + "\n```\n"
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

    def test_the_probe_finds_the_files_of_the_agent_pod(self):
        # The control of the tool test: a zero in a sandbox means a clean one.
        out = pod("sh", "-c", probe_command(), timeout=300).stdout
        probes = parse_probes(out)
        self.assertEqual(len(probes), 1, out)
        self.assertEqual(probes[0]["host"], self.agent_host)
        for key in ("env_secret_values", "config_file", "state_dirs", "marker_files"):
            self.assertGreater(int(probes[0][key]), 0, key)


if __name__ == "__main__":
    unittest.main()
