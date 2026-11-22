"""A fake model endpoint in the form of the Messages API, for the lab.

No real model call. The reply is scripted by the last user message:

  CALL <tool name> <JSON arguments>   the reply is one tool call of that tool
  anything else                       the reply is the text "lab ok"

After a tool result, the reply is the text "RESULT <first 400 characters>".
Every request is logged to /log/model.jsonl (the tool names and the text of
the last message only).
"""

import json
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CALL_RE = re.compile(r"CALL\s+(\d{10,})\s+(\S+)\s+(\{[^\n]*?\})(?=\s*$|\\n|\")", re.M)
DONE = set()


def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for block in content or []:
        if block.get("type") == "text":
            out.append(block.get("text", ""))
        elif block.get("type") == "tool_result":
            c = block.get("content")
            out.append(text_of(c) if isinstance(c, list) else str(c))
    return "\n".join(out)


def plan(body):
    msgs = body.get("messages") or []
    last_asst = max((i for i, m in enumerate(msgs) if m.get("role") == "assistant"), default=-1)
    after = msgs[last_asst + 1:]
    results = [b for m in after if isinstance(m.get("content"), list)
               for b in m["content"] if b.get("type") == "tool_result"]
    if last_asst >= 0 and results:
        return {"kind": "text", "text": "RESULT " + text_of(results)[:400]}
    best = None
    for msg in msgs:
        for m2 in CALL_RE.finditer(text_of(msg.get("content"))):
            if best is None or int(m2.group(1)) > int(best.group(1)):
                best = m2
    if best and best.group(1) not in DONE:
        raw = best.group(3)
        args = None
        for cand in (raw, None):
            try:
                args = json.loads(raw) if cand else json.loads(json.loads('"' + raw + '"'))
                break
            except ValueError:
                continue
        if isinstance(args, dict):
            DONE.add(best.group(1))
            return {"kind": "tool", "name": best.group(2), "input": args, "nonce": best.group(1)}
    return {"kind": "text", "text": "lab ok", "after": [text_of(x.get("content"))[-400:] for x in after]}


def log(entry):
    try:
        with open("/log/model.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "lab-model", "type": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        p = plan(body)
        tools = [t.get("name") for t in body.get("tools") or []]
        if body.get("messages"):
            with open("/log/last-request.json", "w") as f:
                json.dump(body, f)
        log({"t": time.time(), "path": self.path, "tools": tools, "plan": p,
             "last": text_of((body.get("messages") or [{}])[-1].get("content"))[-300:]})
        mid = "msg_" + uuid.uuid4().hex[:16]
        model = body.get("model", "lab-model")
        if p["kind"] == "tool":
            block = {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:16], "name": p["name"], "input": p["input"]}
            stop = "tool_use"
        else:
            block = {"type": "text", "text": p["text"]}
            stop = "end_turn"
        usage = {"input_tokens": 10, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        if not body.get("stream"):
            out = json.dumps({"id": mid, "type": "message", "role": "assistant", "model": model,
                              "content": [block], "stop_reason": stop, "stop_sequence": None,
                              "usage": usage}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def ev(name, data):
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        ev("message_start", {"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": usage}})
        if block["type"] == "tool_use":
            ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}})
            ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(block["input"])}})
        else:
            ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": block["text"]}})
        ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                             "usage": {"output_tokens": 10}})
        ev("message_stop", {"type": "message_stop"})
        self.close_connection = True


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 4000), Handler).serve_forever()
