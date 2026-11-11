#!/usr/bin/env python3
"""A mock model server for the image tests. No real model and no credential.

It speaks the Anthropic Messages API, with and without a stream, and gives
fixed answers. It writes one JSON line for each request to standard output:
the tools that the agent offered, the tool calls so far, the tool results
that came back, and its answer. The tests read these lines.

The answer depends on the text of the user messages:

- With `TOOLPLAN=<name>`, the mock asks for the tool calls of that plan, one
  for each request, in order. A plan is a list in PLANS below.
- With `OUTMARK=<text>`, the final answer holds that text, so a test can look
  for the output of the model in the traces.
- Else it answers `MOCK-OK`.

A plan whose name starts with `claude-` is for Claude Code (the Claude CLI
runtime of OpenClaw). Claude Code can defer a tool behind its ToolSearch
tool, so the mock loads such a tool with ToolSearch first. It asks for each
step even when the tool is not offered, and records what came back. A request
with no tools (a title, a summary) gets a text answer.

Each line also records the route of the request (the path before `/v1/`),
which gateway key it carried (a label from MOCK_GATEWAY_KEYS, never the
value), and the client name from the User-Agent. A GET request is logged with
its path, so a test can see a fetch of a marker URL.

It started from the mock server of the spikes of effort 0003 in tnwks-coe.
"""

import hashlib
import itertools
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The probe that a code tool runs. It prints where it ran, as `key=value`
# lines, for the test to read. It prints the names of environment variables
# and a count of the secret values that it finds, never a value. It looks for
# the files of the agent pod by path, by name, and by a marker that only those
# files hold. The marker is in two parts in the command, so that a copy of
# the command in a file does not count as a find. The search crosses mounts:
# a workspace volume is a mount.
PROBE = r"""
echo PROBE-BEGIN
echo "host=$(hostname)"
echo "uid=$(id -u)"
echo "openshell_sandbox=${OPENSHELL_SANDBOX:-unset}"
echo "cap_eff=$(grep '^CapEff' /proc/self/status | awk '{print $2}')"
echo "sa_token=$(ls /var/run/secrets/kubernetes.io/serviceaccount 2>/dev/null | wc -l)"
echo "env_names=$(env | cut -d= -f1 | sort | tr '\n' ',')"
echo "env_secret_values=$(env | grep -c -F -e @@SECRET_VALUES@@)"
echo "config_file=$(ls -d /home/node/.openclaw/openclaw.json 2>/dev/null | wc -l)"
echo "state_dirs=$(ls -d /home/node/.openclaw/state /home/node/.openclaw/credentials 2>/dev/null | wc -l)"
echo "archive_dir=$(ls -d /home/node/.openclaw-archives 2>/dev/null | wc -l)"
echo "archive_files=$(find / \( -path /proc -o -path /sys -o -path /dev \) -prune -o -type f -name '@@ARCHIVE_NAME@@' -print 2>/dev/null | wc -l)"
m1='@@FILE_MARKER_1@@'; m2='@@FILE_MARKER_2@@'
echo "marker_files=$(timeout 120 grep -r -l -s -F -e "$m1$m2" / --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev --exclude-dir=node_modules 2>/dev/null | wc -l)"
echo PROBE-END
""".strip()

# A plan is a list of steps. Each step names a tool and the input of the
# call. The placeholders @@...@@ get their values when the mock answers.
PLANS = {
    # Each tool that runs code, and each file tool, in the order of the
    # test. The test reads the result of each step by its index.
    "code-tools": [
        # 0: a command, the plain way.
        {"tool": "exec", "input": {"command": "@@PROBE@@", "yieldMs": 120000}},
        # 1: a command that asks for the host and for elevation.
        {
            "tool": "exec",
            "input": {
                "command": "@@PROBE@@",
                "host": "auto",
                "elevated": True,
                "yieldMs": 120000,
            },
        },
        # 2 to 4: a command in the background, then the process tool on it.
        {
            "tool": "exec",
            "input": {"command": "sleep 2; @@PROBE@@", "background": True},
        },
        {
            "tool": "process",
            "input": {
                "action": "poll",
                "sessionId": "@@SESSION_ID@@",
                "timeout": 30000,
            },
        },
        {"tool": "process", "input": {"action": "log", "sessionId": "@@SESSION_ID@@"}},
        # 5 to 8: the file tools.
        {
            "tool": "write",
            "input": {
                "path": "tool-test.txt",
                "content": "WRITE-MARK @@WRITE_MARK@@\n",
            },
        },
        {
            "tool": "edit",
            "input": {
                "path": "tool-test.txt",
                "edits": [{"oldText": "WRITE-MARK", "newText": "EDIT-MARK"}],
            },
        },
        {"tool": "read", "input": {"path": "tool-test.txt"}},
        {
            "tool": "apply_patch",
            "input": {
                "input": "*** Begin Patch\n*** Add File: patch-test.txt\n+PATCH-MARK @@WRITE_MARK@@\n*** End Patch\n"
            },
        },
        # 9: where the files of steps 5 to 8 are.
        {
            "tool": "exec",
            "input": {
                "command": "echo LS-BEGIN; echo host=$(hostname); cat tool-test.txt patch-test.txt; echo LS-END"
            },
        },
        # 10: a command through the dispatch of the tool search.
        {
            "tool": "dispatch_action",
            "input": {
                "id": "exec",
                "args": {"command": "@@PROBE@@", "yieldMs": 120000},
            },
        },
        # 11 and 12: a command and a file tool write to the path of the config.
        {
            "tool": "exec",
            "input": {
                "command": "for p in /home/node/.openclaw/openclaw.json ../openclaw.json ~/.openclaw/openclaw.json; do "
                'mkdir -p "$(dirname "$p")" 2>/dev/null; echo CONFIG-WRITE >> "$p" 2>/dev/null && echo "wrote $p"; done; echo CONFIG-TRY-END'
            },
        },
        {
            "tool": "write",
            "input": {
                "path": "../openclaw.json",
                "content": '{"CONFIG-WRITE": true}\n',
            },
        },
        # 13: the tools that the agent can find, beyond the ones it offers.
        {
            "tool": "tool_search",
            "input": {
                "queries": [
                    {"query": "run code", "limit": 12},
                    {"query": "execute a shell command", "limit": 12},
                    {"query": "python javascript interpreter", "limit": 12},
                    {"query": "write a file", "limit": 12},
                ]
            },
        },
    ],
    "trace": [
        {"tool": "exec", "input": {"command": "echo TOOLMARK-@@TRACE_MARK@@"}},
    ],
}

# The tools of OpenClaw reach Claude Code through a loopback MCP server, as
# `mcp__openclaw__<name>`. Each one that runs code or touches files.
CLAUDE_MCP_STEPS = [
    {
        "tool": "mcp__openclaw__exec",
        "input": {"command": "@@PROBE@@", "yieldMs": 120000},
    },
    {
        "tool": "mcp__openclaw__exec",
        "input": {"command": "sleep 2; @@PROBE@@", "background": True},
    },
    {
        "tool": "mcp__openclaw__process",
        "input": {"action": "poll", "sessionId": "@@SESSION_ID@@", "timeout": 30000},
    },
    {
        "tool": "mcp__openclaw__write",
        "input": {
            "path": "claude-tool-test.txt",
            "content": "WRITE-MARK @@WRITE_MARK@@\n",
        },
    },
    {
        "tool": "mcp__openclaw__edit",
        "input": {
            "path": "claude-tool-test.txt",
            "edits": [{"oldText": "WRITE-MARK", "newText": "EDIT-MARK"}],
        },
    },
    {"tool": "mcp__openclaw__read", "input": {"path": "claude-tool-test.txt"}},
    {
        "tool": "mcp__openclaw__apply_patch",
        "input": {
            "input": "*** Begin Patch\n*** Add File: claude-patch-test.txt\n"
            "+PATCH-MARK @@WRITE_MARK@@\n*** End Patch\n"
        },
    },
    {
        "tool": "mcp__openclaw__exec",
        "input": {
            "command": "echo LS-BEGIN; echo host=$(hostname); "
            "cat claude-tool-test.txt claude-patch-test.txt; echo LS-END"
        },
    },
]

# The native tools of Claude Code that run code, write files, read files or
# fetch URLs. Each one runs in the process of Claude Code, so in the agent pod.
CLAUDE_NATIVE_STEPS = [
    {"tool": "Bash", "input": {"command": "@@PROBE@@", "description": "probe"}},
    {
        "tool": "Write",
        "input": {
            "file_path": "/tmp/native-write-@@NATIVE_MARK@@.txt",
            "content": "NATIVE-WRITE @@NATIVE_MARK@@\n",
        },
    },
    {
        "tool": "Edit",
        "input": {
            "file_path": "/tmp/native-write-@@NATIVE_MARK@@.txt",
            "old_string": "NATIVE-WRITE",
            "new_string": "NATIVE-EDIT",
        },
    },
    {"tool": "Read", "input": {"file_path": "/home/node/.openclaw/openclaw.json"}},
    {
        "tool": "Glob",
        "input": {"pattern": "**/*", "path": "/home/node/.openclaw/credentials"},
    },
    {
        "tool": "Grep",
        "input": {"pattern": "marker", "path": "/home/node/.openclaw/credentials"},
    },
    {
        "tool": "WebFetch",
        "input": {
            "url": "@@MOCK_URL@@/native-webfetch-@@NATIVE_MARK@@",
            "prompt": "Return the text.",
        },
    },
    {"tool": "WebSearch", "input": {"query": "native-websearch-@@NATIVE_MARK@@"}},
    {
        "tool": "NotebookEdit",
        "input": {
            "notebook_path": "/tmp/native-@@NATIVE_MARK@@.ipynb",
            "new_source": "print(1)",
            "cell_type": "code",
            "edit_mode": "insert",
        },
    },
    {
        "tool": "Agent",
        "input": {
            "description": "probe",
            "prompt": "NATIVE-AGENT-@@NATIVE_MARK@@",
            "subagent_type": "general-purpose",
            "run_in_background": False,
        },
    },
    {
        "tool": "Task",
        "input": {
            "description": "probe",
            "prompt": "NATIVE-AGENT-@@NATIVE_MARK@@",
            "subagent_type": "general-purpose",
            "run_in_background": False,
        },
    },
    {
        "tool": "Workflow",
        "input": {
            "script": "export const meta = { name: 'probe', description: 'probe' }\n"
            "const r = await agent('NATIVE-AGENT-@@NATIVE_MARK@@', { label: 'probe' })\n"
            "return { r }\n"
        },
    },
]

# The spike plugin tool `sandbox_exec` (tests/claude-cli/sandbox-exec). The
# file tools write into the workspace of the agent; the commands show whether
# the sandbox sees that file, and whether its own file comes back. Then the
# exec tool of OpenClaw and the native Bash, which must stay off, and a
# command that runs past its time limit.
CLAUDE_SBX_STEPS = [
    {
        "tool": "mcp__openclaw__write",
        "input": {"path": "sbx-in.txt", "content": "MIRROR-IN @@WRITE_MARK@@\n"},
    },
    {"tool": "mcp__openclaw__sandbox_exec", "input": {"command": "@@PROBE@@"}},
    {
        "tool": "mcp__openclaw__sandbox_exec",
        "input": {
            "command": "echo SBX-BEGIN; echo host=$(hostname); echo pwd=$(pwd); "
            "cat sbx-in.txt; echo 'MIRROR-OUT @@WRITE_MARK@@' > sbx-out.txt; "
            "ls -la; echo SBX-END"
        },
    },
    {"tool": "mcp__openclaw__read", "input": {"path": "sbx-out.txt"}},
    {
        "tool": "mcp__openclaw__exec",
        "input": {"command": "echo EXEC-RAN host=$(hostname)"},
    },
    {"tool": "Bash", "input": {"command": "echo BASH-RAN", "description": "probe"}},
    {
        "tool": "mcp__openclaw__sandbox_exec",
        "input": {"command": "sleep 20; echo SLEPT", "timeoutSeconds": 3},
    },
    # Is the command that timed out still running in the sandbox? The pattern
    # is in two parts, so that this command does not count itself.
    {
        "tool": "mcp__openclaw__sandbox_exec",
        "input": {
            "command": "echo SBX-AGAIN host=$(hostname); "
            "echo left_running=$(for f in /proc/[0-9]*/cmdline; do "
            "tr '\\0' ' ' < $f 2>/dev/null; echo; done | grep -c '^sle''ep 20')"
        },
    },
]

# A second session of the same agent: which sandbox does it get?
CLAUDE_SBX_HOST_STEPS = [
    {
        "tool": "mcp__openclaw__sandbox_exec",
        "input": {"command": "echo SBX-AGAIN host=$(hostname); ls"},
    },
]

PLANS["claude-main"] = CLAUDE_MCP_STEPS + CLAUDE_NATIVE_STEPS
PLANS["claude-native"] = CLAUDE_NATIVE_STEPS
PLANS["claude-mcp"] = CLAUDE_MCP_STEPS
PLANS["claude-sbx"] = CLAUDE_SBX_STEPS
PLANS["claude-sbx-host"] = CLAUDE_SBX_HOST_STEPS

# The plans of the bridge spike (tests/test_bridge_spike.py). The bridge runs
# the conversation through the Claude Agent SDK, which offers a client tool
# `x` as `mcp__client__x`, so a step names the tool of the client and the
# mock asks for the offered tool whose name is `x` or ends in `__x`. A bridge
# plan is a list of turns: each turn is the tool calls of one answer (two
# calls in one answer are two tool_use blocks), and the turn index is the
# number of answers with a tool call in the history. After the last turn the
# answer is text.
BRIDGE_PLANS = {
    # An OpenClaw turn: a file tool, then a command that reads the file (a
    # chain of two steps), then two calls in one answer.
    "bridge-tools": [
        [
            {
                "tool": "write",
                "input": {
                    "path": "bridge-test.txt",
                    "content": "BRIDGE-WRITE @@WRITE_MARK@@\n",
                },
            }
        ],
        [
            {
                "tool": "exec",
                "input": {
                    "command": "echo CHAIN-BEGIN; echo host=$(hostname); "
                    "cat bridge-test.txt; echo CHAIN-END"
                },
            }
        ],
        [
            {"tool": "exec", "input": {"command": "@@PROBE@@", "yieldMs": 120000}},
            {"tool": "read", "input": {"path": "bridge-test.txt"}},
        ],
    ],
    # A plain Messages API client with one tool: two calls in one answer.
    "bridge-client": [
        [
            {"tool": "get_weather", "input": {"city": "Paris"}},
            {"tool": "get_weather", "input": {"city": "Oslo"}},
        ],
    ],
    # One call, for the restart and the latency checks.
    "bridge-one": [
        [{"tool": "get_weather", "input": {"city": "Rome"}}],
    ],
}
# A plan whose name is here answers with a thinking block before its text.
THINKING_PLANS = {"bridge-blocks"}
# The request headers that the mock records with their value. Others are
# recorded by name; a key is never recorded.
SHOWN_HEADERS = (
    "user-agent",
    "x-app",
    "anthropic-version",
    "anthropic-beta",
    "anthropic-dangerous-direct-browser-access",
    "x-stainless-lang",
    "x-stainless-package-version",
    "x-stainless-runtime",
    "x-stainless-runtime-version",
    "x-stainless-retry-count",
    "x-stainless-timeout",
    "x-claude-code-session-id",
    "x-client-request-id",
)

# The ids of the messages of the mock. Claude Code keys its transcript by
# message id, so each answer has its own.
MESSAGE_IDS = itertools.count(1)
TOOL_IDS = itertools.count(1)


def placeholders(marks, results):
    values = [v for v in os.environ.get("MOCK_SECRET_VALUES", "").split(",") if v]
    probe = PROBE.replace(
        "@@SECRET_VALUES@@",
        " -e ".join(f"'{v}'" for v in values) or "'no-secret-values-given'",
    )
    probe = probe.replace(
        "@@ARCHIVE_NAME@@", os.environ.get("MOCK_ARCHIVE_NAME", "no-archive-name-given")
    )
    marker = os.environ.get("MOCK_FILE_MARKER", "no-file-marker-given")
    half = len(marker) // 2
    probe = probe.replace("@@FILE_MARKER_1@@", marker[:half]).replace(
        "@@FILE_MARKER_2@@", marker[half:]
    )
    # The exec tool names a command in the background as `(session <id>,`,
    # and a JSON result as `"sessionId": "<id>"`.
    session = find(
        r'(?:\(session |"?sessionId"?\s*[:=]\s*"?)([A-Za-z0-9_.:-]+)',
        [r["text"] for r in reversed(results)],
    )
    return {
        "@@PROBE@@": probe,
        "@@WRITE_MARK@@": marks["write"],
        "@@TRACE_MARK@@": marks["trace"],
        "@@NATIVE_MARK@@": marks.get("native", "none"),
        "@@MOCK_URL@@": os.environ.get(
            "MOCK_SELF_URL", "http://mock-model.test-support.svc.cluster.local:8000"
        ),
        "@@SESSION_ID@@": session or "no-session-id",
    }


def fill(value, table):
    if isinstance(value, str):
        for key, text in table.items():
            value = value.replace(key, text)
        return value
    if isinstance(value, list):
        return [fill(v, table) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, table) for k, v in value.items()}
    return value


def text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "tool_result":
            parts.append(text_of(block.get("content")))
    return "\n".join(parts)


def blocks(body, role, kind):
    out = []
    for m in body.get("messages") or []:
        if m.get("role") != role or isinstance(m.get("content"), str):
            continue
        out.extend(
            b
            for b in m.get("content") or []
            if isinstance(b, dict) and b.get("type") == kind
        )
    return out


def user_texts(body):
    return [
        text_of(m.get("content"))
        for m in body.get("messages") or []
        if m.get("role") == "user"
    ]


def find(pattern, texts):
    for t in texts:
        m = re.search(pattern, t)
        if m:
            return m.group(1)
    return None


def short_text(text, n=1500):
    text = text or ""
    return text if len(text) <= n else text[:n] + f"...[{len(text)} chars]"


def digest(text):
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


def bridge_facts(handler, body):
    """What reached the mock, in full, for the bridge spike."""
    system = body.get("system")
    if isinstance(system, str):
        system = [{"type": "text", "text": system}]
    system = system or []
    tools = body.get("tools") or []
    raw = json.dumps(body)
    return {
        "headers": {
            k.lower(): (v if k.lower() in SHOWN_HEADERS else "(set)")
            for k, v in handler.headers.items()
            if k.lower() not in ("x-api-key", "authorization", "content-length")
        },
        "system_blocks": [
            {
                "chars": len(b.get("text", "")),
                "sha": digest(b.get("text", "")),
                "cache_control": "cache_control" in b,
                "text": short_text(b.get("text", ""), 600),
            }
            for b in system
            if isinstance(b, dict)
        ],
        "tool_names": [t.get("name") or t.get("type") for t in tools],
        "tool_schemas": {
            t.get("name"): t.get("input_schema")
            for t in tools
            if str(t.get("name", "")).endswith(("get_weather", "__exec", "__read"))
        },
        "tool_extras": sorted(
            {
                k
                for t in tools
                for k in t
                if k not in ("name", "description", "input_schema")
            }
        ),
        "max_tokens": body.get("max_tokens"),
        "thinking": body.get("thinking"),
        "temperature": body.get("temperature"),
        "tool_choice": body.get("tool_choice"),
        "metadata_keys": sorted((body.get("metadata") or {}).keys()),
        "other_keys": sorted(
            k
            for k in body
            if k not in ("model", "messages", "system", "tools", "max_tokens", "stream")
        ),
        "cache_control_marks": raw.count('"cache_control"'),
        "messages": [
            {
                "role": m.get("role"),
                "blocks": [m["content"]]
                if isinstance(m.get("content"), str)
                else [
                    b.get("type") for b in m.get("content") or [] if isinstance(b, dict)
                ],
            }
            for m in body.get("messages") or []
        ],
        "first_user_text": short_text(
            text_of((body.get("messages") or [{}])[0].get("content")), 2500
        ),
    }


def bridge_tool(name, offered):
    """The offered tool for a tool name of a bridge plan."""
    if name in offered:
        return name
    return next((o for o in offered if o.endswith("__" + name)), None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _event(self, event, data):
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()

    def request_facts(self):
        """Where the request came in, with which key, from which client."""
        auth = self.headers.get("x-api-key") or ""
        bearer = self.headers.get("authorization") or ""
        if bearer.lower().startswith("bearer "):
            auth = auth or bearer[7:]
        label = "none" if not auth else "unknown"
        for entry in os.environ.get("MOCK_GATEWAY_KEYS", "").split(","):
            name, _, value = entry.partition("=")
            if value and auth == value:
                label = name
        return {
            "path": self.path.split("?", 1)[0],
            "route": self.path.split("/v1/", 1)[0] if "/v1/" in self.path else None,
            "gateway_key": label,
            "user_agent": (self.headers.get("user-agent") or "").split(" ", 1)[0],
        }

    def do_GET(self):
        print(json.dumps(dict(self.request_facts(), api="get")), flush=True)
        if "models" in self.path:
            self._json(
                200,
                {
                    "data": [
                        {
                            "id": "claude-test",
                            "type": "model",
                            "display_name": "claude-test",
                        }
                    ]
                },
            )
        else:
            self._json(200, {"ok": True})

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(length) if length else b"{}")
        except ValueError:
            body = {}
        if "count_tokens" in self.path:
            return self._json(200, {"input_tokens": 1234})
        if "messages" not in self.path:
            print(json.dumps({"api": "unknown", "path": self.path}), flush=True)
            return self._json(
                404,
                {
                    "type": "error",
                    "error": {
                        "type": "not_found_error",
                        "message": "mock: unknown path",
                    },
                },
            )
        return self.messages(body)

    def claude_step(self, steps, offered, uses, marks, results):
        """The next step of a plan for Claude Code, and whether the tool was
        asked for although Claude Code did not offer it."""
        if not offered:
            return None, None
        done = [u for u in uses if u["name"] != "ToolSearch"]
        if len(done) >= len(steps):
            return None, None
        index = len(done)
        step = steps[index]
        tool = step["tool"]
        just_loaded = bool(uses) and uses[-1]["name"] == "ToolSearch"
        if tool not in offered and "ToolSearch" in offered and not just_loaded:
            block = {
                "type": "tool_use",
                "id": f"toolu_load{len(uses):02d}",
                "name": "ToolSearch",
                "input": {"query": f"select:{tool}", "max_results": 5},
            }
            return block, None
        block = {
            "type": "tool_use",
            "id": f"toolu_mock{index:02d}",
            "name": tool,
            "input": fill(step["input"], placeholders(marks, results)),
        }
        return block, tool not in offered

    def bridge_answer(self, body, plan_name, marks, outmark, offered, uses, results):
        """An answer for the bridge spike: a turn of a bridge plan, or text."""
        turns = BRIDGE_PLANS.get(plan_name or "", [])
        done = sum(
            1
            for m in body.get("messages") or []
            if m.get("role") == "assistant"
            and isinstance(m.get("content"), list)
            and any(
                isinstance(b, dict) and b.get("type") == "tool_use"
                for b in m["content"]
            )
        )
        content = []
        if offered and done < len(turns):
            table = placeholders(marks, results)
            for step in turns[done]:
                name = bridge_tool(step["tool"], offered)
                if not name:
                    content = [
                        {"type": "text", "text": f"MOCK-NOT-OFFERED {step['tool']}"}
                    ]
                    break
                content.append(
                    {
                        "type": "tool_use",
                        "id": f"toolu_bridge{next(TOOL_IDS):05d}",
                        "name": name,
                        "input": fill(step["input"], table),
                    }
                )
        if not content:
            final = "MOCK-FINAL" if plan_name else "MOCK-OK"
            if plan_name in THINKING_PLANS:
                content.append(
                    {
                        "type": "thinking",
                        "thinking": "MOCK-THINKING",
                        "signature": "mock-signature-0001",
                    }
                )
            content.append(
                {
                    "type": "text",
                    "text": f"{final} OUTMARK-{outmark}" if outmark else final,
                }
            )
        record = {
            "ts": time.strftime("%H:%M:%S"),
            **self.request_facts(),
            "model": body.get("model"),
            "stream": bool(body.get("stream")),
            "auth_header": "x-api-key"
            if self.headers.get("x-api-key")
            else ("authorization" if self.headers.get("authorization") else "none"),
            "plan": plan_name,
            "turn": done,
            "marks": {k: v for k, v in marks.items() if v != "none"},
            "tools_offered": offered,
            "tool_uses": uses,
            "tool_results": results,
            "answer": [
                b.get("name") or b.get("text") or b.get("type") for b in content
            ],
            "bridge": bridge_facts(self, body),
        }
        print(json.dumps(record), flush=True)
        self.reply(body, content)

    def reply(self, body, content):
        """Send an answer of one or more content blocks, as JSON or SSE."""
        stop = (
            "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        )
        usage = {
            "input_tokens": 11,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 22,
        }
        msg = {
            "id": f"msg_mock{next(MESSAGE_IDS):04d}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "claude-test"),
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": usage,
        }
        if not body.get("stream"):
            return self._json(200, msg)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        start = dict(
            msg, content=[], stop_reason=None, usage=dict(usage, output_tokens=1)
        )
        self._event("message_start", {"type": "message_start", "message": start})
        for i, block in enumerate(content):
            if block["type"] == "text":
                first = {"type": "text", "text": ""}
                deltas = [{"type": "text_delta", "text": block["text"]}]
            elif block["type"] == "thinking":
                first = {"type": "thinking", "thinking": "", "signature": ""}
                deltas = [
                    {"type": "thinking_delta", "thinking": block["thinking"]},
                    {"type": "signature_delta", "signature": block["signature"]},
                ]
            else:
                first = dict(block, input={})
                deltas = [
                    {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(block["input"]),
                    }
                ]
            self._event(
                "content_block_start",
                {"type": "content_block_start", "index": i, "content_block": first},
            )
            for delta in deltas:
                self._event(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": i, "delta": delta},
                )
            self._event(
                "content_block_stop", {"type": "content_block_stop", "index": i}
            )
        self._event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": usage,
            },
        )
        self._event("message_stop", {"type": "message_stop"})

    def messages(self, body):
        texts = user_texts(body)
        offered = [t.get("name") for t in body.get("tools") or []]
        uses = [
            {"id": b.get("id"), "name": b.get("name")}
            for b in blocks(body, "assistant", "tool_use")
        ]
        results = [
            {
                "id": b.get("tool_use_id"),
                "error": bool(b.get("is_error")),
                "text": text_of(b.get("content")),
            }
            for b in blocks(body, "user", "tool_result")
        ]
        plan_name = find(r"TOOLPLAN=([a-z0-9-]+)", texts)
        marks = {
            "write": find(r"WRITEMARK=([A-Za-z0-9-]+)", texts) or "none",
            "trace": find(r"TRACEMARK=([A-Za-z0-9-]+)", texts) or "none",
        }
        outmark = find(r"OUTMARK=([A-Za-z0-9-]+)", texts)
        marks["native"] = find(r"NATIVEMARK=([A-Za-z0-9-]+)", texts) or "none"
        route = self.request_facts()["route"] or ""
        if route.startswith(("/bridge", "/direct")) or (plan_name or "") in (
            set(BRIDGE_PLANS) | THINKING_PLANS
        ):
            return self.bridge_answer(
                body, plan_name, marks, outmark, offered, uses, results
            )

        block = None
        forced = None
        steps = PLANS.get(plan_name or "", [])
        if plan_name and plan_name.startswith("claude-"):
            block, forced = self.claude_step(steps, offered, uses, marks, results)
        elif len(uses) < len(steps):
            # One step for each request: the step after the calls so far.
            step = steps[len(uses)]
            if step["tool"] in offered:
                block = {
                    "type": "tool_use",
                    "id": f"toolu_mock{len(uses):02d}",
                    "name": step["tool"],
                    "input": fill(step["input"], placeholders(marks, results)),
                }
            else:
                block = {"type": "text", "text": f"MOCK-NOT-OFFERED {step['tool']}"}
        if block is None:
            final = "MOCK-FINAL" if plan_name else "MOCK-OK"
            block = {
                "type": "text",
                "text": f"{final} OUTMARK-{outmark}" if outmark else final,
            }

        record = {
            "ts": time.strftime("%H:%M:%S"),
            **self.request_facts(),
            # A subagent of Claude Code: the mark of its prompt.
            "subagent": find(r"NATIVE-AGENT-([A-Za-z0-9-]+)", texts),
            "forced": forced,
            "model": body.get("model"),
            "stream": bool(body.get("stream")),
            "auth_header": "x-api-key"
            if self.headers.get("x-api-key")
            else ("authorization" if self.headers.get("authorization") else "none"),
            "plan": plan_name,
            "marks": {k: v for k, v in marks.items() if v != "none"},
            "tools_offered": offered,
            "tool_uses": uses,
            "tool_results": results,
            "answer": block.get("name") or block.get("text"),
        }
        if os.environ.get("MOCK_DUMP_TOOLS") == "1":
            record["tool_schemas"] = body.get("tools")
        print(json.dumps(record), flush=True)

        stop = "tool_use" if block["type"] == "tool_use" else "end_turn"
        usage = {
            "input_tokens": 11,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 22,
        }
        msg = {
            "id": f"msg_mock{next(MESSAGE_IDS):04d}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "claude-test"),
            "content": [block],
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": usage,
        }
        if not body.get("stream"):
            return self._json(200, msg)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        start = dict(
            msg, content=[], stop_reason=None, usage=dict(usage, output_tokens=1)
        )
        self._event("message_start", {"type": "message_start", "message": start})
        if block["type"] == "text":
            first = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            first = {
                "type": "tool_use",
                "id": block["id"],
                "name": block["name"],
                "input": {},
            }
            delta = {
                "type": "input_json_delta",
                "partial_json": json.dumps(block["input"]),
            }
        self._event(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": first},
        )
        self._event(
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": delta},
        )
        self._event("content_block_stop", {"type": "content_block_stop", "index": 0})
        self._event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": usage,
            },
        )
        self._event("message_stop", {"type": "message_stop"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(json.dumps({"mock": "listening", "port": port}), flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
