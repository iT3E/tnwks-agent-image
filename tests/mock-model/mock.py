#!/usr/bin/env python3
"""A mock model server for the image tests. No real model and no credential.

It speaks the Anthropic Messages API, with and without a stream, and gives
fixed answers. It writes one JSON line for each request to standard output:
the tools that the agent offered, the tool calls so far, the tool results
that came back, and its answer. The tests read these lines.

The answer depends on the text of the user messages:

- With `TOOLPLAN=<name>`, the mock asks for the tool calls of that plan, one
  for each request, in order. A plan is a list in PLANS below. A step asks
  only for a tool that the agent offered, unless it has `"force": True`.
- With `OUTMARK=<text>`, the final answer holds that text, so a test can look
  for the output of the model in the traces.
- Else it answers `MOCK-OK`.

It started from the mock server of the spikes of effort 0003 in tnwks-coe.
"""

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

# One web search, with a query that holds the mark of the turn, so a test can
# find the query at the fake search endpoint and nowhere it must not be. The
# product offers web_search through the tool search, so the plan finds it
# there and calls it through dispatch_action.
PLANS["web-search"] = [
    {
        "tool": "tool_search",
        "input": {"queries": [{"query": "search the web", "limit": 5}]},
    },
    {
        "tool": "dispatch_action",
        "input": {
            "id": "web_search",
            "args": {"query": "tnwks fake query @@WRITE_MARK@@"},
        },
    },
]

# The tools that are on by default in the product and that the config of the
# agent denies: `code_execution` of the bundled xai plugin, and the file
# transfer tools of the bundled file-transfer plugin. The plan asks for each
# one by name, offered or not, then searches for them.
DENIED_TOOLS = ["code_execution", "file_fetch", "file_write", "dir_list", "dir_fetch"]
PLANS["denied-tools"] = [
    {"tool": "code_execution", "force": True, "input": {"task": "@@PROBE@@"}},
    {
        "tool": "file_fetch",
        "force": True,
        "input": {"node": "local", "path": "/home/node/.openclaw/openclaw.json"},
    },
    {
        "tool": "file_write",
        "force": True,
        "input": {
            "node": "local",
            "path": "/tmp/denied-@@WRITE_MARK@@.txt",
            "contentBase64": "REVOSUVELVdSSVRFCg==",
        },
    },
    {
        "tool": "dir_list",
        "force": True,
        "input": {"node": "local", "path": "/home/node"},
    },
    {
        "tool": "dir_fetch",
        "force": True,
        "input": {"node": "local", "path": "/home/node/.openclaw"},
    },
    # The product takes at most 50 results for one search.
    {
        "tool": "tool_search",
        "input": {
            "queries": [
                {"query": "code execution python analysis", "limit": 10},
                {"query": "fetch a file from a node", "limit": 10},
                {"query": "write a file on a node", "limit": 10},
                {"query": "list a directory on a node", "limit": 10},
                {"query": "fetch a directory archive", "limit": 10},
            ]
        },
    },
]


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

    def do_GET(self):
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

        block = None
        steps = PLANS.get(plan_name or "", [])
        if len(uses) < len(steps):
            # One step for each request: the step after the calls so far.
            step = steps[len(uses)]
            if step["tool"] in offered or step.get("force"):
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
            "model": body.get("model"),
            "stream": bool(body.get("stream")),
            "auth_header": "x-api-key"
            if self.headers.get("x-api-key")
            else ("authorization" if self.headers.get("authorization") else "none"),
            "plan": plan_name,
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
            "id": "msg_mock01",
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
