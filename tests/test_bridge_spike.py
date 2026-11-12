"""The bridge spike: a Messages API bridge to the Claude Agent SDK.

It runs on the test cluster of the workflow bridge-spike.yaml only
(tests/bridge-spike/run.sh). The bridge (tests/bridge-spike/bridge.mjs) runs
each conversation through the unmodified Agent SDK; the upstream of the SDK
is the mock model (route /bridge-upstream) with a fake key. The agent pod has
the config tests/config/bridge.json: OpenClaw on its own runtime, its model
provider at the bridge, and the OpenShell sandbox backend for its tools.

It records evidence for these questions, in the job log and the job summary:

1. Does an OpenClaw turn on its own runtime complete through the bridge?
2. Do its tools (`write`, then `exec` on the file, then `exec` and `read` in
   one answer) run in the sandbox through OpenClaw's own path, with the
   results back at the model?
3. What reaches the upstream: system prompt, tools, model, headers.
4. Streaming, and the blocks that do or do not survive (thinking, images,
   cache_control).
5. State: sessions, a restart in the middle of a tool exchange, processes and
   memory, added latency.
6. A plain Messages API client (the `anthropic` Python SDK) with a tool.
7. LiteLLM in front of the bridge.

The findings are not assertions: the test fails only when the harness can
not give the evidence.
"""

import faulthandler
import json
import os
import re
import statistics
import subprocess
import threading
import time
import unittest
import uuid

from support import (
    AGENT_NS,
    CLUSTER,
    LITELLM_MASTER_KEY,
    SUPPORT_NS,
    agent_turn,
    kx,
    mock_records,
    parse_probes,
    sandbox_problems,
)

UPSTREAM = "/bridge-upstream"
SYSTEM_MARK = "CLIENT-SYSTEM-MARK-31d7"
WEATHER = {
    "name": "get_weather",
    "description": "Get the weather of a city.",
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "The city."},
            "unit": {"type": "string", "enum": ["c", "f"]},
        },
        "required": ["city"],
    },
}
# A PNG of one pixel.
PIXEL = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)
PROCESSES = r"""
const fs = require('fs'); const out = [];
for (const p of fs.readdirSync('/proc')) {
  if (!/^\d+$/.test(p)) continue;
  try {
    const cmd = fs.readFileSync(`/proc/${p}/cmdline`, 'utf8').split('\0').filter(Boolean);
    const st = fs.readFileSync(`/proc/${p}/status`, 'utf8');
    const rss = +((st.match(/VmRSS:\s+(\d+)/) || [0, 0])[1]);
    out.push({ pid: +p, cmd: cmd.join(' ').slice(0, 160), rss_kb: rss });
  } catch {}
}
console.log(JSON.stringify(out));
"""


def short(text, n=300):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:n]


def bridge_log():
    out = kx("-n", SUPPORT_NS, "logs", "deploy/bridge", "--tail=-1", check=False)
    lines = []
    for line in out.stdout.splitlines():
        try:
            lines.append(json.loads(line))
        except ValueError:
            continue
    return lines


def processes():
    """The processes of the bridge pod, and the Claude Code ones."""
    out = kx(
        "-n",
        SUPPORT_NS,
        "exec",
        "deploy/bridge",
        "--",
        "node",
        "-e",
        PROCESSES,
        check=False,
    )
    try:
        procs = json.loads(out.stdout)
    except ValueError:
        return {"error": short(out.stdout + out.stderr)}
    claude = [p for p in procs if "claude" in p["cmd"] and "bridge.mjs" not in p["cmd"]]
    bridge = [p for p in procs if "bridge.mjs" in p["cmd"]]
    return {
        "claude_processes": len(claude),
        "claude_rss_mib": round(sum(p["rss_kb"] for p in claude) / 1024, 1),
        "bridge_rss_mib": round(sum(p["rss_kb"] for p in bridge) / 1024, 1),
        "claude_cmd": sorted({p["cmd"][:100] for p in claude}),
    }


class Follow:
    """Print the log of a deployment to the job log as it comes, short."""

    def __init__(self, deploy, label):
        kubectl = os.environ.get("IMAGE_TEST_KUBECTL", "kubectl")
        self.proc = subprocess.Popen(
            [
                kubectl,
                "--kubeconfig",
                os.environ["IMAGE_TEST_KUBECONFIG"],
                "--context",
                f"kind-{CLUSTER}",
                "-n",
                SUPPORT_NS,
                "logs",
                "-f",
                "--tail=0",
                f"deploy/{deploy}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.label = label
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        for line in self.proc.stdout:
            print(f"{self.label}| {self.summary(line)}", flush=True)

    @staticmethod
    def summary(line):
        try:
            r = json.loads(line)
        except ValueError:
            return short(line, 300)
        if "bridge" in r:  # a record of the mock
            return short(
                {
                    k: r.get(k)
                    for k in (
                        "route",
                        "plan",
                        "turn",
                        "answer",
                        "gateway_key",
                        "stream",
                    )
                },
                300,
            )
        return short(r, 400)

    def close(self):
        self.proc.kill()


class PortForward:
    """`kubectl port-forward` to a service of the test support namespace."""

    def __init__(self, service, port):
        kubectl = os.environ.get("IMAGE_TEST_KUBECTL", "kubectl")
        self.proc = subprocess.Popen(
            [
                kubectl,
                "--kubeconfig",
                os.environ["IMAGE_TEST_KUBECONFIG"],
                "--context",
                f"kind-{CLUSTER}",
                "-n",
                SUPPORT_NS,
                "port-forward",
                f"svc/{service}",
                f":{port}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        line = self.proc.stdout.readline()
        m = re.search(r"127\.0\.0\.1:(\d+)", line)
        if not m:
            self.proc.kill()
            raise RuntimeError(f"port-forward {service}: {line!r}")
        self.url = f"http://127.0.0.1:{m.group(1)}"
        # Read the rest of its output, so that the pipe never fills.
        threading.Thread(target=self.proc.stdout.read, daemon=True).start()

    def close(self):
        self.proc.kill()
        self.proc.wait(timeout=10)


def dump(content):
    return [b.model_dump(exclude_none=True) for b in content]


def records_of(mark, since=None):
    return [
        r
        for r in mock_records(since)
        if (r.get("marks") or {}).get("write") == mark and r.get("route") == UPSTREAM
    ]


@unittest.skipUnless(
    os.environ.get("IMAGE_TEST_KUBECONFIG"),
    "no test cluster (tests/bridge-spike/run.sh)",
)
class BridgeSpike(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import anthropic

        cls.anthropic = anthropic
        # Where the test waits, every 5 minutes, in the job log.
        faulthandler.dump_traceback_later(300, repeat=True)
        cls.follow = [Follow("bridge", "bridge"), Follow("mock-model", "mock")]
        cls.findings = {}
        cls.errors = {}
        cls.started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cls.agent_host = kx(
            "-n", AGENT_NS, "exec", "agent", "-c", "agent", "--", "hostname"
        ).stdout.strip()
        cls.findings["q5_processes_idle"] = processes()
        cls.leg("openclaw", cls.openclaw_turn)
        cls.findings["q5_processes_after_openclaw"] = processes()
        cls.bridge = PortForward("bridge", 8080)
        cls.mock = PortForward("mock-model", 8000)
        try:
            cls.leg("client", cls.client_round_trip)
            cls.leg("stream", cls.client_stream)
            cls.leg("cache", cls.cache_marks)
            cls.leg("latency", cls.latency)
            cls.leg("live", cls.live_sessions)
            cls.log_before_restart = bridge_log()
            cls.leg("restart", cls.restart)
            cls.leg("litellm", cls.litellm)
        finally:
            cls.bridge.close()
            cls.mock.close()
        faulthandler.cancel_dump_traceback_later()
        for f in cls.follow:
            f.close()
        cls.collect()
        cls.report()

    @classmethod
    def leg(cls, name, fn):
        started = time.time()
        print(f"\n== leg {name}", flush=True)
        try:
            fn()
        except Exception as error:  # a leg that fails is a finding
            cls.errors[name] = f"{type(error).__name__}: {short(error, 600)}"
            print(f"leg {name}: {cls.errors[name]}", flush=True)
        print(f"== leg {name} done in {round(time.time() - started)} s", flush=True)

    @classmethod
    def client(cls, url, api_key="dummy-client-key", **kw):
        return cls.anthropic.Anthropic(
            base_url=url, api_key=api_key, max_retries=0, timeout=120, **kw
        )

    # -- 1 and 2: OpenClaw on its own runtime --------------------------------

    @classmethod
    def openclaw_turn(cls):
        mark = f"oc-{uuid.uuid4().hex[:8]}"
        started, result = agent_turn(
            f"bridge-{mark}", f"TOOLPLAN=bridge-tools WRITEMARK={mark}", timeout=420
        )
        time.sleep(2)
        recs = records_of(mark, started)
        last = max(recs, key=lambda r: len(r.get("tool_results", [])), default={})
        cls.openclaw = {
            "mark": mark,
            "exit": result.returncode,
            "stdout_has_final": "MOCK-FINAL" in result.stdout,
            "stderr": result.stderr[-1500:],
            "records": recs,
            "last": last,
        }
        print(f"\nOpenClaw turn: exit {result.returncode}, {len(recs)} upstream calls")
        for r in recs:
            print(
                f"  turn {r.get('turn')} answer={r.get('answer')} "
                f"results={[short(x.get('text'), 160) for x in r.get('tool_results', [])]}"
            )

    # -- 6: a plain Messages API client --------------------------------------

    @classmethod
    def client_round_trip(cls):
        mark = f"cl-{uuid.uuid4().hex[:8]}"
        c = cls.client(cls.bridge.url)
        system = f"You are a weather helper. {SYSTEM_MARK}"
        messages = [
            {"role": "user", "content": f"TOOLPLAN=bridge-client WRITEMARK={mark}"}
        ]
        raw = c.messages.with_raw_response.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=system,
            tools=[WEATHER],
            messages=messages,
        )
        first = raw.parse()
        steps = [
            {
                "mode": raw.headers.get("x-bridge-mode"),
                "session": raw.headers.get("x-bridge-session"),
                "stop_reason": first.stop_reason,
                "content": dump(first.content),
            }
        ]
        messages.append({"role": "assistant", "content": dump(first.content)})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": f"WEATHER {b.input.get('city')} sunny",
                    }
                    for b in first.content
                    if b.type == "tool_use"
                ],
            }
        )
        raw = c.messages.with_raw_response.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=system,
            tools=[WEATHER],
            messages=messages,
        )
        second = raw.parse()
        steps.append(
            {
                "mode": raw.headers.get("x-bridge-mode"),
                "session": raw.headers.get("x-bridge-session"),
                "stop_reason": second.stop_reason,
                "content": dump(second.content),
            }
        )
        # A second user turn in the same conversation.
        messages.append({"role": "assistant", "content": dump(second.content)})
        messages.append({"role": "user", "content": "And now?"})
        raw = c.messages.with_raw_response.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=system,
            tools=[WEATHER],
            messages=messages,
        )
        third = raw.parse()
        steps.append(
            {
                "mode": raw.headers.get("x-bridge-mode"),
                "session": raw.headers.get("x-bridge-session"),
                "stop_reason": third.stop_reason,
                "content": dump(third.content),
            }
        )
        cls.client_leg = {"mark": mark, "steps": steps}

    # -- 4: streaming --------------------------------------------------------

    @classmethod
    def client_stream(cls):
        c = cls.client(cls.bridge.url)
        out = {}
        for plan in ("bridge-blocks", "bridge-one"):
            mark = f"st-{uuid.uuid4().hex[:8]}"
            events = []
            with c.messages.stream(
                model="claude-opus-4-6",
                max_tokens=2048,
                system=[
                    {
                        "type": "text",
                        "text": f"You are a test client. {SYSTEM_MARK}",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[WEATHER],
                thinking={"type": "adaptive"},
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": PIXEL,
                                },
                            },
                            {
                                "type": "text",
                                "text": f"TOOLPLAN={plan} WRITEMARK={mark}",
                                "cache_control": {"type": "ephemeral"},
                            },
                        ],
                    }
                ],
            ) as stream:
                for event in stream:
                    events.append(event.type)
                final = stream.get_final_message()
            out[plan] = {
                "mark": mark,
                "event_types": sorted(set(events)),
                "raw_events": sum(
                    1
                    for e in events
                    if e
                    in (
                        "message_start",
                        "content_block_start",
                        "content_block_delta",
                        "content_block_stop",
                        "message_delta",
                        "message_stop",
                    )
                ),
                "stop_reason": final.stop_reason,
                "content": dump(final.content),
            }
        cls.stream_leg = out

    @classmethod
    def cache_marks(cls):
        """A client with 4 cache marks of its own, the most the API takes.

        The engine adds its own marks, and the mock refuses more than 4, as
        the real API does. The bridge must not carry the client's marks.
        """
        mark = f"cc-{uuid.uuid4().hex[:8]}"
        c = cls.client(cls.bridge.url)
        eph = {"type": "ephemeral"}
        system = [
            {"type": "text", "text": f"Cache test. {SYSTEM_MARK}", "cache_control": eph}
        ]
        tools = [dict(WEATHER, cache_control=eph)]
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"TOOLPLAN=bridge-one WRITEMARK={mark}",
                        "cache_control": eph,
                    },
                    {"type": "text", "text": "Second block.", "cache_control": eph},
                ],
            }
        ]
        first = c.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            system=system,
            tools=tools,
            messages=messages,
        )
        # The next request moves one mark to the tool result: still 4.
        messages[0]["content"][1].pop("cache_control")
        messages.append({"role": "assistant", "content": dump(first.content)})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": "WEATHER ok",
                        "cache_control": eph,
                    }
                    for b in first.content
                    if b.type == "tool_use"
                ],
            }
        )
        second = c.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            system=system,
            tools=tools,
            messages=messages,
        )
        time.sleep(2)
        recs = [
            r
            for r in mock_records(cls.started)
            if (r.get("marks") or {}).get("write") == mark
        ]
        cls.cache_leg = {
            "client_marks": [4, 4],
            "first": {"stop_reason": first.stop_reason, "content": dump(first.content)},
            "second": {
                "stop_reason": second.stop_reason,
                "content": dump(second.content),
            },
            "refused_at_upstream": [
                r.get("cache_marks") for r in recs if r.get("refused")
            ],
            "upstream_marks": [
                (r.get("bridge") or {}).get("cache_control_marks")
                for r in recs
                if not r.get("refused")
            ],
        }
        if cls.cache_leg["refused_at_upstream"]:
            raise AssertionError(f"the upstream refused: {cls.cache_leg}")

    # -- 5: latency, live sessions, a restart ---------------------------------

    @classmethod
    def latency(cls):
        direct = cls.client(cls.mock.url + "/direct")
        bridge = cls.client(cls.bridge.url)
        times = {"direct": [], "bridge_new": [], "bridge_warm": []}

        def call(c, messages):
            t = time.perf_counter()
            r = c.messages.create(
                model="claude-opus-4-6", max_tokens=256, messages=messages
            )
            times_ms = round((time.perf_counter() - t) * 1000)
            return r, times_ms

        for i in range(5):
            _, ms = call(direct, [{"role": "user", "content": f"hello {i}"}])
            times["direct"].append(ms)
            _, ms = call(bridge, [{"role": "user", "content": f"hello {i}"}])
            times["bridge_new"].append(ms)
        messages = [{"role": "user", "content": "warm 0"}]
        r, _ = call(bridge, messages)
        for i in range(1, 6):
            messages += [
                {"role": "assistant", "content": dump(r.content)},
                {"role": "user", "content": f"warm {i}"},
            ]
            r, ms = call(bridge, messages)
            times["bridge_warm"].append(ms)
        cls.latency_leg = {
            k: {"median_ms": statistics.median(v), "all_ms": v}
            for k, v in times.items()
        }

    @classmethod
    def live_sessions(cls):
        """Three conversations that wait for a tool result."""
        c = cls.client(cls.bridge.url)
        cls.open_conversations = []
        for _ in range(3):
            mark = f"lv-{uuid.uuid4().hex[:8]}"
            messages = [
                {"role": "user", "content": f"TOOLPLAN=bridge-one WRITEMARK={mark}"}
            ]
            r = c.messages.create(
                model="claude-opus-4-6",
                max_tokens=256,
                tools=[WEATHER],
                messages=messages,
            )
            messages.append({"role": "assistant", "content": dump(r.content)})
            cls.open_conversations.append(
                {"mark": mark, "messages": messages, "first": r}
            )
        time.sleep(3)
        health = json.loads(
            kx(
                "-n",
                SUPPORT_NS,
                "exec",
                "deploy/bridge",
                "--",
                "node",
                "-e",
                "fetch('http://127.0.0.1:8080/health').then(r=>r.text()).then(console.log)",
                check=False,
            ).stdout
            or "{}"
        )
        cls.live_leg = {"health": health, "processes": processes()}

    @classmethod
    def restart(cls):
        conv = cls.open_conversations[0]
        before = kx(
            "-n", SUPPORT_NS, "get", "pods", "-l", "app=bridge", "-o", "name"
        ).stdout.split()
        kx("-n", SUPPORT_NS, "rollout", "restart", "deploy/bridge")
        kx("-n", SUPPORT_NS, "rollout", "status", "deploy/bridge", "--timeout=300s")
        time.sleep(3)
        cls.follow.append(Follow("bridge", "bridge-2"))
        cls.bridge.close()
        cls.bridge = PortForward("bridge", 8080)
        c = cls.client(cls.bridge.url)
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        messages = conv["messages"] + [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": "WEATHER after the restart: rain",
                    }
                    for b in conv["first"].content
                    if b.type == "tool_use"
                ],
            }
        ]
        raw = c.messages.with_raw_response.create(
            model="claude-opus-4-6", max_tokens=256, tools=[WEATHER], messages=messages
        )
        r = raw.parse()
        time.sleep(2)
        recs = records_of(conv["mark"], since)
        cls.restart_leg = {
            "pods_before": before,
            "mode": raw.headers.get("x-bridge-mode"),
            "stop_reason": r.stop_reason,
            "content": dump(r.content),
            "upstream_after_restart": [
                {
                    "answer": x.get("answer"),
                    "messages": x["bridge"]["messages"],
                    "first_user_text": x["bridge"]["first_user_text"],
                }
                for x in recs
            ],
            "processes_after_restart": processes(),
        }

    # -- 7: LiteLLM in front --------------------------------------------------

    @classmethod
    def litellm(cls):
        kx("-n", SUPPORT_NS, "scale", "deploy/litellm", "--replicas=1")
        ready = kx(
            "-n",
            SUPPORT_NS,
            "rollout",
            "status",
            "deploy/litellm",
            "--timeout=240s",
            check=False,
        )
        if ready.returncode != 0:
            pod = kx(
                "-n", SUPPORT_NS, "logs", "deploy/litellm", "--tail=40", check=False
            )
            cls.litellm_leg = {
                "covered": False,
                "why": short(ready.stdout + ready.stderr, 400),
                "log": short(pod.stdout + pod.stderr, 1500),
            }
            return
        proxy = PortForward("litellm", 4000)
        try:
            mark = f"ll-{uuid.uuid4().hex[:8]}"
            c = cls.client(proxy.url, api_key=LITELLM_MASTER_KEY)
            messages = [
                {"role": "user", "content": f"TOOLPLAN=bridge-client WRITEMARK={mark}"}
            ]
            first = c.messages.create(
                model="claude-opus-4-6",
                max_tokens=512,
                tools=[WEATHER],
                messages=messages,
            )
            messages.append({"role": "assistant", "content": dump(first.content)})
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": b.id,
                            "content": "WEATHER ok",
                        }
                        for b in first.content
                        if b.type == "tool_use"
                    ],
                }
            )
            second = c.messages.create(
                model="claude-opus-4-6",
                max_tokens=512,
                tools=[WEATHER],
                messages=messages,
            )
            time.sleep(2)
            cls.litellm_leg = {
                "covered": True,
                "mark": mark,
                "first": {
                    "stop_reason": first.stop_reason,
                    "content": dump(first.content),
                },
                "second": {
                    "stop_reason": second.stop_reason,
                    "content": dump(second.content),
                },
                "upstream_calls": len(records_of(mark)),
            }
        finally:
            proxy.close()

    # -- the evidence -----------------------------------------------------------

    @classmethod
    def collect(cls):
        f = cls.findings
        f["leg_errors"] = cls.errors
        log = getattr(cls, "log_before_restart", []) + bridge_log()
        f["bridge_inits"] = [
            {k: v for k, v in e.items() if k not in ("ts",)}
            for e in log
            if e.get("event") == "sdk_init"
        ][:2]
        f["bridge_events"] = {
            name: sum(1 for e in log if e.get("event") == name)
            for name in sorted({e.get("event") for e in log})
        }
        f["bridge_stderr_sample"] = [
            short(e.get("data"), 300) for e in log if e.get("event") == "sdk_stderr"
        ][:8]

        oc = getattr(cls, "openclaw", None)
        if oc:
            last = oc["last"]
            results = last.get("tool_results", [])
            probes = [p for r in results for p in parse_probes(r.get("text"))]
            f["q1_openclaw"] = {
                "exit": oc["exit"],
                "final_in_output": oc["stdout_has_final"],
                "upstream_calls": len(oc["records"]),
                "turns_seen": [r.get("turn") for r in oc["records"]],
                "answers": [r.get("answer") for r in oc["records"]],
                "upstream_key": sorted({r.get("gateway_key") for r in oc["records"]}),
                "stderr_tail": short(oc["stderr"], 600) if oc["exit"] else "",
            }
            requests = [
                e
                for e in log
                if e.get("event") == "request"
                and e.get("user_agent", "").lower().find("openclaw") >= 0
            ] or [e for e in log if e.get("event") == "request"][:6]
            f["q1_openclaw_requests_at_bridge"] = [
                {
                    k: e.get(k)
                    for k in (
                        "mode",
                        "stream",
                        "model",
                        "messages",
                        "shape",
                        "new_input",
                        "dropped",
                        "cache_control_marks",
                        "user_agent",
                        "system_sha",
                    )
                }
                for e in requests[:8]
            ]
            f["q2_tool_results_at_upstream"] = [
                {"error": r.get("error"), "text": short(r.get("text"), 400)}
                for r in results
            ]
            f["q2_chain_saw_write"] = any(
                f"BRIDGE-WRITE {oc['mark']}" in (r.get("text") or "")
                and "CHAIN-BEGIN" in (r.get("text") or "")
                for r in results
            )
            f["q2_probe"] = [
                {
                    "host": p.get("host"),
                    "uid": p.get("uid"),
                    "openshell_sandbox": p.get("openshell_sandbox"),
                    "problems": sandbox_problems(p, cls.agent_host),
                }
                for p in probes
            ]
            f["q2_two_calls_in_one_answer"] = [
                r.get("answer") for r in oc["records"] if r.get("turn") == 2
            ]
            first = oc["records"][0]["bridge"] if oc["records"] else {}
            f["q3_upstream_openclaw"] = {
                "model": oc["records"][0].get("model") if oc["records"] else None,
                "headers": first.get("headers"),
                "system_blocks": first.get("system_blocks"),
                "tool_names": first.get("tool_names"),
                "tool_extras": first.get("tool_extras"),
                "tool_schema_exec": (first.get("tool_schemas") or {}).get(
                    "mcp__client__exec"
                ),
                "thinking": first.get("thinking"),
                "max_tokens": first.get("max_tokens"),
                "other_keys": first.get("other_keys"),
                "cache_control_marks": first.get("cache_control_marks"),
                "messages": first.get("messages"),
                "first_user_text": first.get("first_user_text"),
            }

        cl = getattr(cls, "client_leg", None)
        if cl:
            recs = records_of(cl["mark"], cls.started)
            first = recs[0]["bridge"] if recs else {}
            f["q6_client"] = cl["steps"]
            f["q6_upstream_calls"] = [
                {
                    "turn": r.get("turn"),
                    "answer": r.get("answer"),
                    "results": [
                        short(x.get("text"), 120) for x in r.get("tool_results", [])
                    ],
                }
                for r in recs
            ]
            f["q3_upstream_client"] = {
                "model": recs[0].get("model") if recs else None,
                "system_blocks": first.get("system_blocks"),
                "client_system_found": any(
                    SYSTEM_MARK in (b.get("text") or "")
                    for b in first.get("system_blocks") or []
                ),
                "tool_names": first.get("tool_names"),
                "tool_schema_get_weather": (first.get("tool_schemas") or {}).get(
                    "mcp__client__get_weather"
                ),
                "client_schema_get_weather": WEATHER["input_schema"],
                "headers": first.get("headers"),
                "upstream_key": sorted({r.get("gateway_key") for r in recs}),
            }
        st = getattr(cls, "stream_leg", None)
        if st:
            f["q4_stream_client"] = st
            for plan, leg in st.items():
                recs = records_of(leg["mark"], cls.started)
                b = recs[0]["bridge"] if recs else {}
                f[f"q4_upstream_{plan}"] = {
                    "stream": recs[0].get("stream") if recs else None,
                    "thinking": b.get("thinking"),
                    "max_tokens": b.get("max_tokens"),
                    "cache_control_marks": b.get("cache_control_marks"),
                    "system_cache_control": [
                        x.get("cache_control") for x in b.get("system_blocks") or []
                    ],
                    "messages": b.get("messages"),
                }
        f["q4_cache_marks"] = getattr(cls, "cache_leg", None)
        f["q5_latency"] = getattr(cls, "latency_leg", None)
        f["q5_live"] = getattr(cls, "live_leg", None)
        f["q5_restart"] = getattr(cls, "restart_leg", None)
        f["q7_litellm"] = getattr(cls, "litellm_leg", None)
        if f["q7_litellm"] and f["q7_litellm"].get("covered"):
            recs = records_of(f["q7_litellm"]["mark"], cls.started)
            f["q7_upstream_headers"] = recs[0]["bridge"]["headers"] if recs else None
            f["q7_requests_at_bridge"] = [
                {k: e.get(k) for k in ("mode", "stream", "user_agent", "dropped")}
                for e in bridge_log()
                if e.get("event") == "request"
                and "python" not in str(e.get("user_agent", "")).lower()
                and "openclaw" not in str(e.get("user_agent", "")).lower()
            ][:4]

    @classmethod
    def report(cls):
        text = json.dumps(cls.findings, indent=1, sort_keys=True, default=str)
        print("\n==== bridge spike findings\n" + text)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as out:
                out.write("## Bridge spike: findings\n\n```json\n" + text + "\n```\n")

    # -- the harness checks ----------------------------------------------------

    def test_the_openclaw_turn_reached_the_upstream(self):
        self.assertNotIn("openclaw", self.errors)
        self.assertTrue(
            self.openclaw["records"], "no upstream call of the OpenClaw turn"
        )

    def test_the_client_legs_ran(self):
        for leg in ("client", "stream", "cache", "latency", "live", "restart"):
            with self.subTest(leg=leg):
                self.assertNotIn(leg, self.errors)


if __name__ == "__main__":
    unittest.main()
