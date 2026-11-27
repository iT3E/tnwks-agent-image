// A browser device of the Control UI, for the rows of the Control UI path
// (task 6.52). It runs in a container of the agent image, with the gateway
// client of the product's plugin SDK, and connects through the fake ingress
// as the Control UI does: client id openclaw-control-ui, mode webchat, role
// operator, its own device key, the origin of the UI, and the scopes and
// capabilities that the Control UI of the release asks for (CONTROL_UI_
// OPERATOR_SCOPES and the caps in ui/src/api/gateway-connect-plan.ts at
// 2026.11.3). The admin scope among them: the auto approval of the trusted
// proxy auth caps the grant to its configured scopes.
//
// It prints one JSON line for each fact, to the log of its container:
//   {"hello": {...}}                     the scopes of the connection
//   {"chat": ...} or {"chat_error"}      the answer of each chat.send
//   {"list": [...]} or {"list_error"}    the ids of plugin.approval.list, each POLL_MS
//   {"event": name, "id": ...}           each approval event the device gets
//
// Env: URL, ORIGIN, SESSION (the session key of the web chat), MESSAGES (a
// JSON list, each sent once with chat.send, GAP_MS apart), POLL_MS.

import { randomUUID } from "node:crypto";
import { GatewayClient } from "openclaw/plugin-sdk/gateway-runtime";

const env = process.env;
const say = (obj) => console.log(JSON.stringify(obj));
const session = env.SESSION || "main";
const SCOPES = ["operator.admin", "operator.read", "operator.write", "operator.approvals", "operator.questions",
  "operator.pairing"];
const CAPS = ["agent-kind", "approvals", "task-suggestions", "terminal-offset-seq", "terminal-session-metadata",
  "terminal-upload-path-style", "tool-events", "chat-only-assistant-text", "session-scoped-events", "inline-widgets",
  "model-selection-policy", "ui-agent-github-settings", "ui-commands", "ultrafast", "usage-refreshing"];
let polling = false;

async function afterHello() {
  if (polling) return;
  polling = true;
  const poll = async () => {
    try {
      const list = await client.request("plugin.approval.list", {});
      say({ list: (Array.isArray(list) ? list : []).map((a) => a.id) });
    } catch (err) {
      say({ list_error: String(err?.message || err) });
    }
  };
  await poll();
  setInterval(poll, Number(env.POLL_MS || 2000));
  for (const message of JSON.parse(env.MESSAGES || "[]")) {
    try {
      const r = await client.request("chat.send", { sessionKey: session, message, idempotencyKey: randomUUID() });
      say({ chat: r?.status ?? r?.runId ?? "sent" });
    } catch (err) {
      say({ chat_error: String(err?.message || err) });
    }
    await new Promise((done) => setTimeout(done, Number(env.GAP_MS || 15000)));
  }
}

const client = new GatewayClient({
  url: env.URL,
  origin: env.ORIGIN,
  clientName: "openclaw-control-ui",
  clientVersion: "control-ui",
  platform: "web",
  mode: "webchat",
  role: "operator",
  scopes: SCOPES,
  caps: CAPS,
  onHelloOk: (hello) => {
    say({ hello: { scopes: hello?.auth?.scopes ?? null, role: hello?.auth?.role ?? null } });
    afterHello();
  },
  onEvent: (evt) => {
    if (/approval/.test(String(evt?.event))) {
      say({ event: evt.event, id: evt.payload?.id ?? evt.payload?.request?.id ?? null });
    }
  },
  onConnectError: (err) => say({ connect_error: String(err?.message || err) }),
  onClose: (code, reason) => say({ close: code, reason: String(reason || "") }),
});
client.start();
