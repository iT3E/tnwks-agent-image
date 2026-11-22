"""The test driver of the lab, run inside the fake chat container.

  ctl.py msg <from> <text>                 a direct message from a user
  ctl.py click <from> <message_id> <data>  a button press on a card
  ctl.py sent [n]                          the last n calls of the bot
  ctl.py cards                             each card with buttons: id, chat, text, buttons
  ctl.py reset                             empty the record
"""

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8081"


def post(path, obj):
    req = urllib.request.Request(BASE + path, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req))


def sent():
    return json.load(urllib.request.urlopen(BASE + "/_lab/sent"))["result"]


def main(argv):
    cmd = argv[0]
    if cmd == "msg":
        print(post("/_lab/message", {"from": argv[1], "text": argv[2]}))
    elif cmd == "click":
        print(post("/_lab/callback", {"from": argv[1], "message_id": argv[2], "data": argv[3]}))
    elif cmd == "sent":
        n = int(argv[1]) if len(argv) > 1 else 10
        for s in sent()[-n:]:
            p = s["params"]
            print(json.dumps({"method": s["method"], "chat": p.get("chat_id"), "mid": s.get("message_id"),
                              "text": (p.get("text") or "")[:700],
                              "buttons": [[b.get("text") + "=" + str(b.get("callback_data")) for b in row]
                                          for row in (p.get("reply_markup") or {}).get("inline_keyboard", [])]}))
    elif cmd == "cards":
        for s in sent():
            p = s["params"]
            kb = (p.get("reply_markup") or {}).get("inline_keyboard")
            if s["method"] == "sendMessage" and kb:
                print(json.dumps({"mid": s.get("message_id"), "chat": p.get("chat_id"), "text": p.get("text"),
                                  "buttons": [[b.get("text") + "=" + str(b.get("callback_data")) for b in row] for row in kb]}))
    elif cmd == "reset":
        print(post("/_lab/reset", {}))


if __name__ == "__main__":
    main(sys.argv[1:])
