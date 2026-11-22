"""A fake IRC server for the lab: the second channel of a cutover.

The product connects to port 6667 as a client. The test driver uses a
control port:

  POST /_lab/message  {"from": "<prefix>", "text": "..."}  a direct message to the bot
  GET  /_lab/sent     each PRIVMSG of the bot, in order
  POST /_lab/reset    empty the record

The prefix is sent as it is. A prefix with no "!" and no "@" makes the
sender ID of the product the bare nick, so a sender can carry the same ID
string as the approver on the other channel. No real server, no real user.
"""

import json
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
CLIENTS = []  # (handler, nick)
SENT = []


class Irc(socketserver.StreamRequestHandler):
    def setup(self):
        super().setup()
        self.nick = None
        self.lock = threading.Lock()

    def send(self, line):
        with self.lock:
            self.wfile.write((line + "\r\n").encode())
            self.wfile.flush()

    def handle(self):
        with LOCK:
            CLIENTS.append(self)
        try:
            for raw in self.rfile:
                line = raw.decode(errors="replace").rstrip("\r\n")
                if not line:
                    continue
                parts = line.split(" ")
                cmd = parts[0].upper()
                if cmd == "NICK":
                    self.nick = parts[1]
                elif cmd == "USER":
                    self.send(f":lab.irc 001 {self.nick} :Welcome to the lab")
                    self.send(f":lab.irc 376 {self.nick} :End of MOTD")
                elif cmd == "PING":
                    self.send(f":lab.irc PONG lab.irc {parts[1] if len(parts) > 1 else ''}")
                elif cmd == "CAP":
                    self.send(f":lab.irc CAP * LS :")
                elif cmd == "PRIVMSG":
                    target = parts[1]
                    text = line.split(" :", 1)[1] if " :" in line else ""
                    with LOCK:
                        SENT.append({"t": time.time(), "to": target, "text": text})
                elif cmd == "QUIT":
                    break
        finally:
            with LOCK:
                if self in CLIENTS:
                    CLIENTS.remove(self)


class Control(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def reply(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        with LOCK:
            self.reply({"sent": SENT, "clients": [c.nick for c in CLIENTS]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/_lab/message":
            with LOCK:
                clients = list(CLIENTS)
            for c in clients:
                if c.nick:
                    c.send(f":{data['from']} PRIVMSG {c.nick} :{data['text']}")
            return self.reply({"ok": True, "to": [c.nick for c in clients]})
        if self.path == "/_lab/reset":
            with LOCK:
                SENT.clear()
            return self.reply({"ok": True})
        self.reply({"ok": False})


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    threading.Thread(target=lambda: ThreadingHTTPServer(("0.0.0.0", 8082), Control).serve_forever(),
                     daemon=True).start()
    Server(("0.0.0.0", 6667), Irc).serve_forever()
