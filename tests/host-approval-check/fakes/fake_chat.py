"""A fake of the chat API (the Bot API form) for the lab.

The product points its channel at this server with `apiRoot`. The server keeps
a queue of updates for `getUpdates`, records each message that the bot sends,
and gives the test driver a control path:

  POST /_lab/message   {"from": <id>, "chat": <id>, "text": "..."}
  POST /_lab/callback  {"from": <id>, "chat": <id>, "message_id": <n>, "data": "..."}
  GET  /_lab/sent      every call the bot made, in order
  POST /_lab/reset     empty the record

No real token and no real ID: the lab uses made-up numbers.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOCK = threading.Condition()
UPDATES = []  # pending updates
NEXT_UPDATE_ID = [1]
NEXT_MESSAGE_ID = [100]
SENT = []  # (time, method, params)


def bot_user():
    return {"id": 1000000, "is_bot": True, "first_name": "Lab bot", "username": "lab_bot",
            "can_join_groups": False, "can_read_all_group_messages": False,
            "supports_inline_queries": False}


def message_obj(chat_id, text, reply_markup=None):
    with LOCK:
        NEXT_MESSAGE_ID[0] += 1
        mid = NEXT_MESSAGE_ID[0]
    msg = {"message_id": mid, "date": int(time.time()),
           "chat": {"id": int(chat_id), "type": "private" if int(chat_id) > 0 else "group"},
           "from": bot_user(), "text": text or ""}
    if reply_markup:
        msg["reply_markup"] = reply_markup
    return msg


def push_update(update):
    with LOCK:
        update["update_id"] = NEXT_UPDATE_ID[0]
        NEXT_UPDATE_ID[0] += 1
        UPDATES.append(update)
        LOCK.notify_all()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _params(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")
        params = {}
        q = urlparse(self.path).query
        if q:
            params.update({k: v[0] for k, v in parse_qs(q).items()})
        if raw:
            if "json" in ctype:
                params.update(json.loads(raw))
            elif "x-www-form-urlencoded" in ctype:
                params.update({k: v[0] for k, v in parse_qs(raw.decode()).items()})
            elif "multipart" in ctype:
                params["_multipart"] = True
        for key in ("reply_markup",):
            if isinstance(params.get(key), str):
                try:
                    params[key] = json.loads(params[key])
                except ValueError:
                    pass
        return params

    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/_lab/"):
            return self._lab(path)
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2 or not parts[0].startswith("bot"):
            return self._send(404, {"ok": False, "error_code": 404, "description": "Not Found"})
        method = parts[-1]
        params = self._params()
        if method != "getUpdates":
            with LOCK:
                SENT.append({"t": time.time(), "method": method, "params": params})
        if method == "getMe":
            return self._send(200, {"ok": True, "result": bot_user()})
        if method == "getUpdates":
            offset = int(params.get("offset") or 0)
            timeout = min(int(params.get("timeout") or 0), 20)
            deadline = time.time() + timeout
            with LOCK:
                while True:
                    UPDATES[:] = [u for u in UPDATES if u["update_id"] >= offset]
                    if UPDATES or time.time() >= deadline:
                        break
                    LOCK.wait(max(0.0, deadline - time.time()))
                result = list(UPDATES)
            return self._send(200, {"ok": True, "result": result})
        if method in ("sendMessage", "sendPhoto", "sendDocument"):
            msg = message_obj(params.get("chat_id", 0), params.get("text") or params.get("caption"),
                              params.get("reply_markup"))
            with LOCK:
                SENT[-1]["message_id"] = msg["message_id"]
            return self._send(200, {"ok": True, "result": msg})
        if method in ("editMessageText", "editMessageReplyMarkup", "editMessageCaption"):
            msg = message_obj(params.get("chat_id", 0), params.get("text"), params.get("reply_markup"))
            msg["message_id"] = int(params.get("message_id") or msg["message_id"])
            return self._send(200, {"ok": True, "result": msg})
        if method == "getChat":
            cid = int(params.get("chat_id") or 0)
            return self._send(200, {"ok": True, "result": {"id": cid, "type": "private" if cid > 0 else "group"}})
        if method in ("getMyCommands",):
            return self._send(200, {"ok": True, "result": []})
        if method == "getWebhookInfo":
            return self._send(200, {"ok": True, "result": {"url": "", "has_custom_certificate": False, "pending_update_count": 0}})
        return self._send(200, {"ok": True, "result": True})

    def _lab(self, path):
        params = self._params()
        if path == "/_lab/message":
            uid = int(params["from"])
            chat = int(params.get("chat", uid))
            push_update({"message": {
                "message_id": int(time.time() * 1000) % 100000000,
                "date": int(time.time()),
                "chat": {"id": chat, "type": "private" if chat > 0 else "group", "first_name": "Lab"},
                "from": {"id": uid, "is_bot": False, "first_name": "Lab"},
                "text": params["text"]}})
            return self._send(200, {"ok": True})
        if path == "/_lab/callback":
            uid = int(params["from"])
            chat = int(params.get("chat", uid))
            push_update({"callback_query": {
                "id": f"cq{int(time.time() * 1000)}",
                "from": {"id": uid, "is_bot": False, "first_name": "Lab"},
                "chat_instance": "lab",
                "message": {"message_id": int(params["message_id"]), "date": int(time.time()),
                            "chat": {"id": chat, "type": "private" if chat > 0 else "group"},
                            "from": bot_user(), "text": "card"},
                "data": params["data"]}})
            return self._send(200, {"ok": True})
        if path == "/_lab/sent":
            with LOCK:
                return self._send(200, {"ok": True, "result": SENT})
        if path == "/_lab/reset":
            with LOCK:
                SENT.clear()
            return self._send(200, {"ok": True})
        return self._send(404, {"ok": False})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
