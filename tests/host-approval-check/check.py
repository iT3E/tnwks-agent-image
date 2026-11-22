"""The update check of the host approval plugin (task 6.22, round 2c).

Starts the product image with the plugin and the live form of the product
config (sandbox mode all, the exact host tool names in alsoAllow, the config
read only, the four tools of A1 denied), next to a fake chat API, a second
chat channel (IRC, the case of a cutover), a fake model with scripted tool
calls, the gateway, the host server image and a fake host. Then it runs
each row as an assertion and prints a table. Exit code 0 only if each row
passes.

Each part runs in a container on one internal Docker network: no route out,
no secret (each key and token is made new for the run and removed after),
no kubeconfig, no gh. The runner needs Docker and Python 3.

  python3 -I check.py            (the images come from the env, see run.sh)
"""

import hashlib
import html
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.environ
RUN = "uc" + secrets.token_hex(3)
NET = RUN + "-net"
# Each start of the product gets a new state volume: the product counts
# quick restarts on one state as a crash loop and then does not start its
# channels.
VOLS = []

AGENT_IMAGE = ENV["AGENT_IMAGE"]
AGW_IMAGE = ENV["AGW_IMAGE"]
SSH_MCP_IMAGE = ENV["SSH_MCP_IMAGE"]
SSHD_IMAGE = ENV["SSHD_IMAGE"]
PY_IMAGE = ENV["PY_IMAGE"]
# The plugin as it ships, in the agent image; or a folder to mount (a
# change of the plugin tried before the image build).
PLUGIN_DIR = ENV.get("PLUGIN_DIR", "")
PLUGIN_PATH = "/lab/plugin" if PLUGIN_DIR else "/opt/openclaw/local-plugins/host-approval"
IRC_PLUGIN_DIR = ENV["IRC_PLUGIN_DIR"]
# The openshell plugin in the image (the agent image), or a folder to mount.
OPENSHELL_DIR = ENV.get("OPENSHELL_PLUGIN_DIR", "")
OPENSHELL_PATH = "/lab/openshell" if OPENSHELL_DIR else "/opt/openclaw/plugins/node_modules/@openclaw/openshell-sandbox"

APPROVER = "1000001"  # a lab ID, not a real one
MEMBER = "1000002"
GROUP = "-1001000000001"  # a lab group ID
HOST_TOOLS = ["read-command", "run-command", "sftp-list", "sftp-download", "read-session-output",
              "list-sessions", "list-connections"]
ALL_HOST_TOOLS = ["read-command", "run-command", "privileged-command", "signal-process", "sftp-list",
                  "sftp-download", "sftp-upload", "open-session", "read-session-output", "close-session",
                  "list-sessions", "list-connections"]

RESULTS = []


def sh(*args, check=True, capture=True, input=None):
    p = subprocess.run(list(args), capture_output=capture, text=True, input=input)
    if check and p.returncode != 0:
        raise RuntimeError(f"{args[:4]} failed: {p.stderr.strip()[:400]}")
    return p.stdout if capture else ""


def name(part):
    return f"{RUN}-{part}"


def record(row, ok, detail, untested=False):
    state = "could not be tested" if untested else ("pass" if ok else "FAIL")
    RESULTS.append((row, state, detail))
    print(f"[{state}] {row}: {detail}", flush=True)


# ---------------------------------------------------------------- set up


def make_secrets(work):
    keys = os.path.join(work, "keys")
    os.makedirs(keys, mode=0o755)
    for k in ("agent", "host"):
        sh("docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
           "-v", f"{keys}:/k", "--entrypoint", "ssh-keygen", SSHD_IMAGE,
           "-q", "-t", "ed25519", "-N", "", "-C", "lab", "-f", f"/k/{k}")
    # The host server runs as the user of the runner and reads its key and
    # config only at mode 0600. The fake host runs as root.
    for f in os.listdir(keys):
        os.chmod(os.path.join(keys, f), 0o644 if f.endswith(".pub") else 0o600)
    fp = sh("docker", "run", "--rm", "--network", "none", "-v", f"{keys}:/k:ro", "--entrypoint", "ssh-keygen",
            SSHD_IMAGE, "-l", "-E", "sha256", "-f", "/k/host.pub").split()[1]
    return {"MCP_KEY": secrets.token_hex(32), "SSH_MCP_TOKEN": secrets.token_hex(32),
            "GW_TOKEN": secrets.token_hex(32), "HOST_FP": fp, "keys": keys}


def render(work, s, product_change=None):
    tools = ", ".join(f'"{t}"' for t in HOST_TOOLS)
    key_hash = hashlib.sha256(s["MCP_KEY"].encode()).hexdigest()
    agw = open(os.path.join(HERE, "config/agentgateway.tmpl.yaml")).read()
    agw = agw.replace("@MCP_KEY_HASH@", key_hash).replace("@SSH_MCP_TOKEN@", s["SSH_MCP_TOKEN"]).replace("@TOOLS@", tools)
    open(os.path.join(work, "agentgateway.yaml"), "w").write(agw)
    os.makedirs(os.path.join(work, "sshmcp"), mode=0o700, exist_ok=True)
    os.chmod(os.path.join(work, "sshmcp"), 0o700)
    t = open(os.path.join(HERE, "config/ssh-mcp.tmpl.toml")).read().replace("@HOST_KEY_FP@", s["HOST_FP"])
    open(os.path.join(work, "sshmcp/config.toml"), "w").write(t)
    os.chmod(os.path.join(work, "agentgateway.yaml"), 0o600)
    os.chmod(os.path.join(work, "sshmcp/config.toml"), 0o600)
    c = product_config(product_change)
    open(os.path.join(work, "openclaw.json"), "w").write(json.dumps(c, indent=1))
    os.chmod(os.path.join(work, "openclaw.json"), 0o644)


def product_config(change=None):
    t = open(os.path.join(HERE, "config/openclaw.tmpl.json")).read()
    t = (t.replace("@PLUGINS_ALLOW@", json.dumps(["telegram", "openshell", "host-approval", "irc"]))
          .replace("@LOAD_PATHS@", json.dumps([OPENSHELL_PATH, PLUGIN_PATH, "/lab/irc"]))
          .replace("@TIMEOUT_MS@", "600000"))
    c = json.loads(t)
    # The second channel of a cutover. The IRC sender "1000001" has the same
    # ID string as the approver on the approval channel (a bare nick needs
    # name matching on). The second IRC sender has a full prefix whose ID
    # ends in ":1000001".
    c["channels"]["irc"] = {"enabled": True, "host": "fakeirc", "port": 6667, "tls": False, "nick": "labbot",
                            "username": "labbot", "realname": "lab bot", "dmPolicy": "allowlist",
                            "allowFrom": [APPROVER, "x!u@2001:db8::1000001"], "groupPolicy": "disabled",
                            "dangerouslyAllowNameMatching": True}
    # A group on the approval channel, open to both lab users.
    c["channels"]["telegram"]["groupPolicy"] = "allowlist"
    c["channels"]["telegram"]["groupAllowFrom"] = [APPROVER, MEMBER]
    c["channels"]["telegram"]["groups"] = {GROUP: {"requireMention": False}}
    if change:
        change(c)
    return c


def up(work, s):
    sh("docker", "network", "create", "--internal", NET)
    os.makedirs(os.path.join(work, "model"), mode=0o777)
    os.chmod(os.path.join(work, "model"), 0o777)
    fakes = os.path.join(HERE, "fakes")
    run = ["docker", "run", "-d", "--network", NET]
    sh(*run, "--name", name("fakeirc"), "--network-alias", "fakeirc", "-v", f"{fakes}:/f:ro", PY_IMAGE,
       "python", "-I", "/f/fake_irc.py")
    sh(*run, "--name", name("fakechat"), "--network-alias", "fakechat", "-v", f"{fakes}:/f:ro", PY_IMAGE,
       "python", "-I", "/f/fake_chat.py")
    sh(*run, "--name", name("fakemodel"), "--network-alias", "fakemodel", "-v", f"{fakes}:/f:ro",
       "-v", f"{work}/model:/log", PY_IMAGE, "python", "-I", "/f/fake_model.py")
    sh(*run, "--name", name("sshd"), "--network-alias", "labhost",
       "-v", f"{s['keys']}/agent.pub:/k/agent.pub:ro", "-v", f"{s['keys']}/host:/k/host:ro", SSHD_IMAGE)
    uid, gid = os.getuid(), os.getgid()
    sh(*run, "--name", name("sshmcp"), "--network-alias", "sshmcp", "--user", f"{uid}:{gid}", "--read-only",
       "--tmpfs", f"/tmp:uid={uid},gid={gid}", "--tmpfs", f"/audit:uid={uid},gid={gid},mode=0700",
       "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "-e", "HOME=/tmp", "-e", "XDG_DATA_HOME=/audit",
       "-v", f"{work}/sshmcp:/cfg:ro", "-v", f"{s['keys']}/agent:/keys/agent_ed25519:ro",
       "-e", f"SSH_MCP_TOKEN={s['SSH_MCP_TOKEN']}", "--entrypoint", "sh", SSH_MCP_IMAGE, "-c",
       'exec node build/index.js --config=/cfg/config.toml --transport=http --httpHost=0.0.0.0 --httpPort=3000 '
       '--hostKeyMode=strict --allowedHosts=sshmcp,sshmcp:3000 --bearerToken="$SSH_MCP_TOKEN"')
    sh(*run, "--name", name("agw"), "--network-alias", "agw", "--user", f"{uid}:{gid}", "--read-only",
       "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
       "-v", f"{work}/agentgateway.yaml:/config.yaml:ro", AGW_IMAGE, "-f", "/config.yaml")
    start_agent(work, s)


def start_agent(work, s):
    sh("docker", "rm", "-f", name("agent"), check=False)
    for old_vol in VOLS:
        sh("docker", "volume", "rm", old_vol, check=False)
    vol = f"{RUN}-state{len(VOLS)}"
    VOLS.append(vol)
    sh("docker", "volume", "create", vol)
    sh("docker", "run", "--rm", "--network", "none", "-v", f"{vol}:/d", PY_IMAGE,
       "sh", "-c", "mkdir -p /d/state && chown -R 1000:1000 /d && chmod 700 /d/state")
    mounts = ["-v", f"{IRC_PLUGIN_DIR}:/lab/irc:ro"]
    if PLUGIN_DIR:
        mounts += ["-v", f"{PLUGIN_DIR}:/lab/plugin:ro"]
    if OPENSHELL_DIR:
        mounts += ["-v", f"{OPENSHELL_DIR}:/lab/openshell:ro"]
    if os.path.isdir(os.path.join(HERE, "broken")):
        mounts += ["-v", f"{os.path.join(HERE, 'broken')}:/lab/broken:ro"]
    sh("docker", "run", "-d", "--name", name("agent"), "--network", NET, "--network-alias", "agent",
       "--read-only", "--tmpfs", "/tmp:uid=1000,gid=1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
       "-v", f"{vol}:/home/node/data", "-v", f"{work}/openclaw.json:/home/node/config/openclaw.json:ro", *mounts,
       "-e", "OPENCLAW_STATE_DIR=/home/node/data/state", "-e", "OPENCLAW_CONFIG_PATH=/home/node/config/openclaw.json",
       "-e", "OPENCLAW_CONFIG_READONLY=1", "-e", "OPENCLAW_NO_AUTO_UPDATE=1", "-e", "DO_NOT_TRACK=1",
       "-e", "OPENCLAW_OFFLINE=1", "-e", "XDG_CACHE_HOME=/home/node/data/state/.cache",
       "-e", f"OPENCLAW_GATEWAY_TOKEN={s['GW_TOKEN']}", "-e", "LITELLM_API_KEY=lab-not-a-key",
       "-e", "TELEGRAM_BOT_TOKEN=1000000:lab-not-a-real-token", "-e", f"MCP_KEY={s['MCP_KEY']}",
       AGENT_IMAGE)
    since = time.time()
    while time.time() - since < 120:
        log = agent_log()
        if "http server listening" in log and irc_clients():
            time.sleep(3)
            return log
        if "Config invalid" in log:
            raise RuntimeError("the product refused the config: " + log[-1500:])
        time.sleep(2)
    raise RuntimeError("the product did not start: " + agent_log()[-1500:])


def down(work):
    parts = ["agent", "agw", "sshmcp", "sshd", "fakechat", "fakemodel", "fakeirc"]
    sh("docker", "rm", "-f", *[name(p) for p in parts], check=False)
    for vol in VOLS:
        sh("docker", "volume", "rm", vol, check=False)
    sh("docker", "network", "rm", NET, check=False)
    shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- drivers


def py_in(container, code, *args):
    return sh("docker", "exec", container, "python", "-I", "-c", code, *args)


POST = ("import json,sys,urllib.request\n"
        "r=urllib.request.Request(sys.argv[1],data=sys.argv[2].encode(),method='POST',"
        "headers={'Content-Type':'application/json'})\n"
        "print(urllib.request.urlopen(r).read().decode())\n")
GET = "import sys,urllib.request\nprint(urllib.request.urlopen(sys.argv[1]).read().decode())\n"


def chat_msg(frm, text, chat=None):
    body = {"from": frm, "text": text}
    if chat is not None:
        body["chat"] = chat
    py_in(name("fakechat"), POST, "http://127.0.0.1:8081/_lab/message", json.dumps(body))


def chat_click(frm, mid, data):
    py_in(name("fakechat"), POST, "http://127.0.0.1:8081/_lab/callback",
          json.dumps({"from": frm, "message_id": mid, "data": data}))


def chat_sent():
    return json.loads(py_in(name("fakechat"), GET, "http://127.0.0.1:8081/_lab/sent"))["result"]


def irc_msg(prefix, text):
    py_in(name("fakeirc"), POST, "http://127.0.0.1:8082/_lab/message", json.dumps({"from": prefix, "text": text}))


def irc_sent():
    return json.loads(py_in(name("fakeirc"), GET, "http://127.0.0.1:8082/_lab/sent"))["sent"]


def irc_clients():
    try:
        return json.loads(py_in(name("fakeirc"), GET, "http://127.0.0.1:8082/_lab/sent"))["clients"]
    except Exception:
        return []


def audit():
    out = sh("docker", "exec", name("sshmcp"), "sh", "-c", "cat /audit/ssh-mcp/audit.log 2>/dev/null || true")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def agent_log():
    p = subprocess.run(["docker", "logs", name("agent")], capture_output=True, text=True)
    return p.stdout + p.stderr


def model_log(work):
    path = os.path.join(work, "model", "model.jsonl")
    if not os.path.exists(path):
        return []
    return [json.loads(line) for line in open(path) if line.strip()]


def nonce():
    time.sleep(0.01)
    return str(time.time_ns())[:13]


def call_text(n, tool, args):
    return f"CALL {n} dispatch_action " + json.dumps({"id": tool, "args": args}, separators=(",", ":"))


def wait(pred, timeout=60, step=1.0):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


def result_of(work, n):
    """The text the fake model wrote after the tool call of nonce n."""
    log = model_log(work)
    idx = next((i for i, e in enumerate(log) if (e.get("plan") or {}).get("nonce") == n), None)
    if idx is None:
        return None
    for e in log[idx + 1:]:
        p = e.get("plan") or {}
        if p.get("kind") == "text" and str(p.get("text", "")).startswith("RESULT"):
            return p["text"]
    return None


def cards(sent, chat=APPROVER):
    out = []
    for s in sent:
        p = s["params"]
        kb = (p.get("reply_markup") or {}).get("inline_keyboard") or []
        data = [b.get("callback_data") for row in kb for b in row]
        if s["method"] == "sendMessage" and str(p.get("chat_id")) == chat and any(
                str(d).startswith("tga1:p:") for d in data):
            out.append({"mid": s.get("message_id"), "text": p.get("text") or "", "data": data})
    return out


def card_call(text):
    """The call on the card, from its `call:` line."""
    for line in html.unescape(text).splitlines():
        if line.startswith("call: "):
            return json.loads(line[len("call: "):])
    return None



def audit_has(command, before):
    return [a for a in audit()[before:] if a.get("command") == command]


# ---------------------------------------------------------------- rows


def owner_card(work, command, tag):
    """The approver asks for a host write; returns (nonce, card) or (nonce, None)."""
    before = len(chat_sent())
    n = nonce()
    chat_msg(APPROVER, call_text(n, "host__run-command", {"profile": "agent", "command": command}))
    found = wait(lambda: cards(chat_sent()[before:]), 60)
    return n, (found[-1] if found else None)


def decide_card(card, frm, decision):
    data = [d for d in card["data"] if d.startswith("tga1:p:")]
    # The buttons of the product: o = allow once, d = deny (in the order shown).
    want = {"allow-once": ":o:", "deny": ":d:"}[decision]
    d = next((x for x in data if want in x), None)
    if d is None:
        raise RuntimeError(f"no {decision} button on the card: {data}")
    chat_click(frm, card["mid"], d)


def rows_base(work):
    log = agent_log()
    ok = "host-approval: loaded" in log and "refusing each tool call" not in log
    record("start: the plugin loads with the live config", ok,
           "loaded" if ok else "no load line, or a config refusal")
    sandbox = json.load(open(os.path.join(work, "openclaw.json")))["agents"]["defaults"]["sandbox"]["mode"]
    record("start: sandbox mode", sandbox == "all", f"mode {sandbox}")

    # 1. A listed read: no card, it runs.
    a0, s0 = len(audit()), len(chat_sent())
    n = nonce()
    chat_msg(APPROVER, call_text(n, "host__read-command", {"profile": "agent", "command": "uptime"}))
    res = wait(lambda: result_of(work, n), 60)
    ran = audit_has("uptime", a0)
    record("a listed read runs with no card", bool(ran) and not cards(chat_sent()[s0:]),
           f"audit {len(ran)}, cards {len(cards(chat_sent()[s0:]))}, result {str(res)[:80]!r}")

    # 2. A write: a card, the approver says yes, the command on the card runs.
    tricky = "ls -d /etc/*_* /tmp/*a*b* `echo x` [y] ~z~ __u__ \\\\q > /dev/null; touch /tmp/uc-yes"
    a0 = len(audit())
    n, card = owner_card(work, tricky, "yes")
    if not card:
        record("a write makes a card", False, "no card in 60 s")
    else:
        none_before = not audit_has(tricky, a0)
        record("a write makes a card, and nothing runs before the answer", none_before, f"card {card['mid']}")
        decide_card(card, APPROVER, "allow-once")
        ran = wait(lambda: audit_has(tricky, a0), 60)
        shown = (card_call(card["text"]) or {}).get("command")
        record("the approver says yes: the call runs", bool(ran), f"audit {len(ran or [])}")
        record("the command on the card is byte-equal to the command that ran",
               bool(ran) and shown == ran[0]["command"] == tricky, f"shown {shown!r}")

    # 3. A no: the call does not run.
    a0 = len(audit())
    n, card = owner_card(work, "touch /tmp/uc-no", "no")
    if card:
        decide_card(card, APPROVER, "deny")
        res = wait(lambda: result_of(work, n), 60)
        record("the approver says no: nothing runs", not audit_has("touch /tmp/uc-no", a0) and res is not None,
               f"result {str(res)[:90]!r}")
    else:
        record("the approver says no: nothing runs", False, "no card")

    # 3b. The same write called by its own name, not through dispatch_action:
    # a card or an error, never a run with no card.
    s0, a0 = len(chat_sent()), len(audit())
    n = nonce()
    direct = json.dumps({"profile": "agent", "command": "touch /tmp/uc-direct"}, separators=(",", ":"))
    chat_msg(APPROVER, f"CALL {n} host__run-command {direct}")
    got = wait(lambda: cards(chat_sent()[s0:]) or result_of(work, n), 60)
    found = cards(chat_sent()[s0:])
    if found:
        decide_card(found[-1], APPROVER, "deny")
        wait(lambda: result_of(work, n), 30)
    res = result_of(work, n)
    record("a write called by its own tool name: a card or an error, never a run with no card",
           bool(got) and not audit_has("touch /tmp/uc-direct", a0),
           f"cards {len(found)}, result {str(res)[:90]!r}")

    # 4. A changed argument: a listed read with one more space is a card.
    s0, a0 = len(chat_sent()), len(audit())
    n = nonce()
    chat_msg(APPROVER, call_text(n, "host__read-command", {"profile": "agent", "command": "uptime "}))
    found = wait(lambda: cards(chat_sent()[s0:]), 60)
    record("a changed argument of a listed read makes a card", bool(found) and not audit_has("uptime ", a0),
           f"cards {len(found or [])}")
    if found:
        decide_card(found[-1], APPROVER, "deny")
        wait(lambda: result_of(work, n), 30)

    # 5. A second sender on the approval channel: no host tool; he can not
    # answer a card of the approver (button or command).
    a0 = len(audit())
    n = nonce()
    chat_msg(MEMBER, call_text(n, "host__read-command", {"profile": "agent", "command": "uptime"}))
    res = wait(lambda: result_of(work, n), 60)
    record("a second sender gets no host tool", bool(res) and "Unknown tool id: host__" in res and
           not audit_has("uptime", a0), f"result {str(res)[:90]!r}")
    a0 = len(audit())
    n, card = owner_card(work, "touch /tmp/uc-member", "member")
    if card:
        decide_card(card, MEMBER, "allow-once")
        approve_id = next(d for d in card["data"] if d.startswith("tga1:p:")).split(":", 3)[3]
        chat_msg(MEMBER, f"/approve {approve_id} allow-once")
        time.sleep(12)
        record("a second sender can not answer a card (button and /approve)",
               not audit_has("touch /tmp/uc-member", a0), "nothing ran")
        decide_card(card, APPROVER, "deny")
        wait(lambda: result_of(work, n), 30)
    else:
        record("a second sender can not answer a card", False, "no card")

    # 5b. The second sender keeps the ordinary chat commands, in his direct
    # chat and in a group of the approval channel; in the group he can not
    # answer a card either, and a turn of the approver in the group gets no
    # host call (the hook needs his direct chat at this release).
    for where, chat in (("direct chat", None), ("group", GROUP)):
        s0 = len(chat_sent())
        chat_msg(MEMBER, "/status", chat)
        want = str(chat if chat is not None else MEMBER)
        got = wait(lambda: [x for x in chat_sent()[s0:] if x["method"] == "sendMessage" and
                            str(x["params"].get("chat_id")) == want and "OpenClaw" in (x["params"].get("text") or "")], 30)
        record(f"a second sender runs an ordinary chat command (/status) in his {where}", bool(got),
               f"replies {len(got or [])}")
    a0 = len(audit())
    n, card = owner_card(work, "touch /tmp/uc-group", "group")
    if card:
        approve_id = next(d for d in card["data"] if d.startswith("tga1:p:")).split(":", 3)[3]
        chat_msg(MEMBER, f"/approve {approve_id} allow-once", GROUP)
        time.sleep(12)
        record("a second sender can not answer a card from the group", not audit_has("touch /tmp/uc-group", a0),
               "nothing ran")
        decide_card(card, APPROVER, "deny")
        wait(lambda: result_of(work, n), 30)
    else:
        record("a second sender can not answer a card from the group", False, "no card")
    a0 = len(audit())
    n = nonce()
    chat_msg(APPROVER, call_text(n, "host__read-command", {"profile": "agent", "command": "id"}), GROUP)
    res = wait(lambda: result_of(work, n), 60)
    record("a turn of the approver in a group gets no host call", bool(res) and not audit_has("id", a0),
           f"result {str(res)[:90]!r}")

    # 6. The approver ID on another channel (reviewer M2): no host tool, and
    # no answer to a card.
    for prefix in (APPROVER, "x!u@2001:db8::1000001"):
        a0 = len(audit())
        n = nonce()
        irc_msg(prefix, call_text(n, "host__read-command", {"profile": "agent", "command": "uptime"}))
        res = wait(lambda: result_of(work, n), 60)
        record(f"the ID {prefix!r} on the second channel gets no host tool",
               bool(res) and "Unknown tool id: host__" in res and not audit_has("uptime", a0),
               f"result {str(res)[:90]!r}")
    a0 = len(audit())
    n, card = owner_card(work, "touch /tmp/uc-irc", "irc")
    if card:
        approve_id = next(d for d in card["data"] if d.startswith("tga1:p:")).split(":", 3)[3]
        for prefix in (APPROVER, "x!u@2001:db8::1000001"):
            irc_msg(prefix, f"/approve {approve_id} allow-once")
        time.sleep(15)
        record("the second channel can not answer a card (/approve)", not audit_has("touch /tmp/uc-irc", a0),
               "nothing ran" if not audit_has("touch /tmp/uc-irc", a0) else "the call ran")
        decide_card(card, APPROVER, "deny")
        wait(lambda: result_of(work, n), 30)
    else:
        record("the second channel can not answer a card", False, "no card")

    # 7. A scheduled turn and a heartbeat: no host tool.
    a0 = len(audit())
    n = nonce()
    msg = call_text(n, "host__read-command", {"profile": "agent", "command": "uptime"})
    out = sh("docker", "exec", name("agent"), "openclaw", "cron", "add", "--name", "uc-sched", "--every", "1h",
             "--agent", "main", "--message", msg, "--channel", "telegram", "--to", APPROVER, "--announce",
             "--json", check=False)
    try:
        job = json.loads(out[out.index("{"):])
        job_id = job.get("id") or job.get("job", {}).get("id")
    except (ValueError, AttributeError):
        job_id = None
    if not job_id:
        jobs = json.loads(sh("docker", "exec", name("agent"), "openclaw", "cron", "list", "--json"))["jobs"]
        job_id = next((j["id"] for j in jobs if j.get("name") == "uc-sched"), None)
    if job_id:
        sh("docker", "exec", name("agent"), "openclaw", "cron", "run", job_id, check=False)
        res = wait(lambda: result_of(work, n), 90)
        record("a scheduled turn gets no host tool", bool(res) and "Unknown tool id: host__" in res and
               not audit_has("uptime", a0), f"result {str(res)[:90]!r}")
        sh("docker", "exec", name("agent"), "openclaw", "cron", "rm", job_id, check=False)
    else:
        record("a scheduled turn gets no host tool", False, "could not add the job", untested=True)

    a0 = len(audit())
    n = nonce()
    hb = call_text(n, "host__read-command", {"profile": "agent", "command": "uptime"})
    jobs = json.loads(sh("docker", "exec", name("agent"), "openclaw", "cron", "list", "--json"))["jobs"]
    hb_id = next((j["id"] for j in jobs if (j.get("payload") or {}).get("kind") == "heartbeat"), None)
    if hb_id:
        # The heartbeat reads its instructions from the scratch of its job
        # (the product no longer reads HEARTBEAT.md).
        sh("docker", "exec", name("agent"), "openclaw", "cron", "scratch", hb_id, "--set", f"Check this: {hb}",
           check=False)
        sh("docker", "exec", name("agent"), "openclaw", "cron", "run", hb_id, check=False)
        res = wait(lambda: result_of(work, n), 90)
        if res is None:
            record("a heartbeat gets no host tool", False, "the heartbeat did not call the tool", untested=True)
        else:
            record("a heartbeat gets no host tool", "Unknown tool id: host__" in res and not audit_has("uptime", a0),
                   f"result {str(res)[:90]!r}")
    else:
        # From 2026.11.6 the product has no heartbeat: Doctor turns it into
        # an ordinary scheduled job, and the scheduled turn row covers that.
        record("a heartbeat gets no host tool", False,
               "no heartbeat job in this release (a heartbeat is a scheduled job; see the row above)", untested=True)


def rows_variant(work, s, label, change, expect_tools):
    """Start the product with one change of the config, and ask for a read
    and a write. With expect_tools False, no host tool may exist."""
    render(work, s, change)
    log = start_agent(work, s)
    a0 = len(audit())
    results = []
    for tool, command in (("host__read-command", "uptime"), ("host__run-command", f"touch /tmp/uc-{label}")):
        n = nonce()
        chat_msg(APPROVER, call_text(n, tool, {"profile": "agent", "command": command}))
        results.append(wait(lambda: result_of(work, n), 60))
    ran = [a for a in audit()[a0:]]
    return log, results, ran


def rows_variants(work, s):
    def disabled(c):
        c["plugins"]["entries"]["host-approval"]["enabled"] = False

    def not_allowed(c):
        c["plugins"]["allow"].remove("host-approval")

    def no_path(c):
        c["plugins"]["load"]["paths"].remove(PLUGIN_PATH)

    def broken(c):
        c["plugins"]["load"]["paths"] = [p if p != PLUGIN_PATH else "/lab/broken" for p in c["plugins"]["load"]["paths"]]

    for label, change in (("off", disabled), ("not-allowed", not_allowed), ("absent", no_path), ("broken", broken)):
        if label == "broken" and not os.path.isdir(os.path.join(HERE, "broken")):
            record("plugin broken: no host tool", False, "no broken copy", untested=True)
            continue
        log, results, ran = rows_variant(work, s, label, change, False)
        loaded = "host-approval: loaded" in log
        write_ran = any(a.get("command") == f"touch /tmp/uc-{label}" for a in ran)
        if loaded:
            # The product turned the plugin on by itself: the gate must hold.
            ok = not write_ran
            detail = f"the product loaded the plugin anyway; write ran {write_ran}"
        else:
            ok = not ran and all(r and "Unknown tool id: host__" in r for r in results)
            detail = f"audit {len(ran)}, results {[str(r)[:60] for r in results]}"
        record(f"plugin {label}: no host call runs with no card", ok, detail)

    # A second server entry at the host route, under another prefix, with
    # its names in alsoAllow: the plugin refuses each call at start.
    def second_entry(c):
        c.setdefault("mcp", {}).setdefault("servers", {})["hostb"] = {
            "url": "http://agw:3000/main-host/mcp", "transport": "streamable-http",
            "headers": {"Authorization": "Bearer ${MCP_KEY}"}}
        c["tools"]["sandbox"]["tools"]["alsoAllow"] += ["hostb__" + t for t in ALL_HOST_TOOLS]

    log, results, ran = rows_variant(work, s, "second", second_entry, False)
    refused = "mcp.servers.hostb" in log and "refusing each tool call" in log
    record("a second server entry at the host route: the plugin refuses each call", refused and not ran,
           f"start refusal {refused}, audit {len(ran)}")
    n = nonce()
    a0 = len(audit())
    chat_msg(APPROVER, call_text(n, "hostb__read-command", {"profile": "agent", "command": "uptime"}))
    res = wait(lambda: result_of(work, n), 60)
    record("a second server entry: its tool does not run", not audit()[a0:], f"result {str(res)[:90]!r}")

    # The same entry without its names in alsoAllow (the live list names the
    # host tools only): the sandbox hides the tools.
    def second_entry_hidden(c):
        c.setdefault("mcp", {}).setdefault("servers", {})["hostb"] = {
            "url": "http://agw:3000/main-host/mcp", "transport": "streamable-http",
            "headers": {"Authorization": "Bearer ${MCP_KEY}"}}
        c["plugins"]["entries"]["host-approval"]["enabled"] = False

    render(work, s, second_entry_hidden)
    start_agent(work, s)
    n = nonce()
    a0 = len(audit())
    chat_msg(APPROVER, call_text(n, "hostb__read-command", {"profile": "agent", "command": "uptime"}))
    res = wait(lambda: result_of(work, n), 60)
    record("a second server entry with the plugin off and no alsoAllow line: the sandbox hides its tools",
           bool(res) and "Unknown tool id" in res and not audit()[a0:], f"result {str(res)[:90]!r}")


def rows_group_commands(work, s):
    """Task 6.22: the command list of the approval channel differs from the
    allow list of that channel and holds a group ID. The product still
    takes an answer to a plugin card from the approver list of the channel
    only, so the member can not answer the card, by button or by /approve,
    in his direct chat or in the group; the approver still can."""
    def group_commands(c):
        c["commands"]["allowFrom"]["telegram"] = [APPROVER, MEMBER, GROUP]

    row = "the command list of the approval channel differs from its allow list and holds a group ID: " \
          "only the approver answers a card"
    render(work, s, group_commands)
    log = start_agent(work, s)
    if "host-approval: loaded" not in log or "refusing each tool call" in log:
        record(row, False, "the plugin did not load with this command list")
        return
    # The row opens each sender's sessions before the card. A known limit
    # of the product at this release is recorded in the runbook; the
    # approval then fails closed.
    s_warm = len(chat_sent())
    chat_msg(MEMBER, "/status")
    chat_msg(MEMBER, "/status", GROUP)
    wait(lambda: len([x for x in chat_sent()[s_warm:] if "OpenClaw" in str(x["params"].get("text") or "")]) >= 2, 30)
    a0 = len(audit())
    n, card = owner_card(work, "touch /tmp/uc-group-commands", "group-commands")
    if not card:
        record(row, False, "no card")
        return
    s0 = len(chat_sent())
    decide_card(card, MEMBER, "allow-once")
    approve_id = next(d for d in card["data"] if d.startswith("tga1:p:")).split(":", 3)[3]
    chat_msg(MEMBER, f"/approve {approve_id} allow-once")
    chat_msg(MEMBER, f"/approve {approve_id} allow-once", GROUP)
    time.sleep(12)
    member_ran = bool(audit_has("touch /tmp/uc-group-commands", a0))
    s1 = len(chat_sent())
    decide_card(card, APPROVER, "allow-once")
    res = wait(lambda: result_of(work, n), 60)
    approver_ran = bool(wait(lambda: audit_has("touch /tmp/uc-group-commands", a0), 15))
    replies = [str(x["params"].get("text") or x["method"])[:60] for x in chat_sent()[s0:s1]]
    after = [str(x["params"].get("text") or x["method"])[:60] for x in chat_sent()[s1:]]
    record(row, not member_ran and approver_ran,
           f"member ran {member_ran}, approver ran {approver_ran}, replies to the member {replies}, "
           f"after the approver {after}, result {str(res)[:80]!r}")


def main():
    base = ENV.get("UC_WORK") or tempfile.gettempdir()
    work = tempfile.mkdtemp(prefix=RUN + "-", dir=base)
    os.chmod(work, 0o755)
    t0 = time.time()
    s = None
    try:
        s = make_secrets(work)
        render(work, s)
        up(work, s)
        version = sh("docker", "exec", name("agent"), "openclaw", "--version", check=False).strip()
        print(f"product: {version}", flush=True)
        if ENV.get("UC_ONLY_VARIANTS") != "1":
            rows_base(work)
        if ENV.get("UC_SKIP_VARIANTS") != "1":
            rows_variants(work, s)
        if ENV.get("UC_SKIP_GROUP_COMMANDS") != "1":
            rows_group_commands(work, s)
    except Exception as err:  # a fault of the check itself is a failure
        record("the check ran to the end", False, repr(err)[:400])
    finally:
        if ENV.get("UC_KEEP") == "1":
            print(f"kept: {RUN}, {work}")
        else:
            down(work)
    took = int(time.time() - t0)
    print(f"\n| Row | Result | Detail |\n| --- | --- | --- |")
    for row, state, detail in RESULTS:
        print(f"| {row} | {state} | {detail.replace('|', '/')} |")
    fails = [r for r in RESULTS if r[1] == "FAIL"]
    print(f"\n{len(RESULTS)} rows, {len(fails)} failed, {took} s")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
