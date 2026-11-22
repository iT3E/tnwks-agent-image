import { test } from "node:test";
import assert from "node:assert/strict";
import { isIP } from "node:net";
import {
  DEFAULT_KNOWN_TOOLS,
  MAX_TIMEOUT_MS,
  asciiJson,
  canonicalJson,
  cardText,
  codeFence,
  checkProductConfig,
  checkServerRoutes,
  decide,
  findKeyForms,
  routeOf,
  parseConfig,
  requesterIsApprover,
  resolveConnection,
  safeDecide,
  senderIsApprover,
} from "../lib.js";
import defaultPlugin, { makePlugin } from "../index.js";

const APPROVER = "1000001"; // a lab value, not a real ID

const RAW = {
  serverPrefixes: ["host__"],
  readProfile: "agent",
  readList: ["uptime", "df -h /"],
  promptTools: ["browser__browser_click", "browser__browser_fill_form"],
  approverId: APPROVER,
  approvalChannel: "telegram",
  mcpUrl: "http://agw:3000/main-host/mcp",
  mcpKeyEnv: "MCP_KEY",
};
const KEY = "k".repeat(64); // a lab value

// A fake name service of the lab: the gateway has a short name, a full
// service name and its address; "other" is another service; "nowhere" does
// not resolve.
const DNS = {
  agw: ["10.96.0.10"],
  "agw.ai.svc.cluster.local": ["10.96.0.10"],
  other: ["10.96.0.20"],
  "agw-dual": ["fd00::10", "10.96.0.10"],
};
async function lookup(host) {
  // An address resolves to itself, as with dns.lookup.
  if (isIP(host)) return [{ address: host, family: isIP(host) }];
  const addresses = DNS[host];
  if (!addresses) throw Object.assign(new Error(`getaddrinfo ENOTFOUND ${host}`), { code: "ENOTFOUND" });
  return addresses.map((address) => ({ address, family: address.includes(":") ? 6 : 4 }));
}
const plugin = makePlugin({ lookup, retryMs: [5] });
// The hook context of a turn of the approver on his channel.
const OWNER = { requester: { channel: "telegram", senderId: APPROVER } };
const decideOwner = (st, event) => decide(st, event, OWNER);

const PRODUCT = {
  approvals: { plugin: { enabled: true, mode: "targets", targets: [{ channel: "telegram", to: APPROVER }] } },
  channels: { telegram: { execApprovals: { enabled: true, approvers: [APPROVER] } } },
  commands: { ownerAllowFrom: [`telegram:${APPROVER}`], allowFrom: { telegram: [APPROVER], "*": [] } },
};

function state(raw = RAW) {
  const parsed = parseConfig(raw);
  assert.ok(parsed.ok, JSON.stringify(parsed.errors));
  return { config: parsed.config };
}

const call = (tool, params) => ({ toolName: `host__${tool}`, params });

test("a listed read on the read profile passes with no prompt", () => {
  assert.equal(decideOwner(state(), call("read-command", { command: "uptime", profile: "agent" })), undefined);
  assert.equal(decideOwner(state(), call("read-command", { command: "df -h /", profile: "agent" })), undefined);
});

test("a read with one changed character prompts", () => {
  for (const command of ["uptime ", " uptime", "uptimE", "df -h /etc", "df -h  /", "uptime;id"]) {
    const r = decideOwner(state(), call("read-command", { command, profile: "agent" }));
    assert.ok(r?.requireApproval, command);
  }
});

test("a listed read with no profile, another profile or an extra argument prompts", () => {
  assert.ok(decideOwner(state(), call("read-command", { command: "uptime" }))?.requireApproval);
  assert.ok(decideOwner(state(), call("read-command", { command: "uptime", profile: "admin" }))?.requireApproval);
  assert.ok(decideOwner(state(), call("read-command", { command: "uptime", profile: "agent", x: 1 }))?.requireApproval);
  assert.ok(decideOwner(state(), call("read-command", { command: ["uptime"], profile: "agent" }))?.requireApproval);
});

test("each other known tool of the host server prompts", () => {
  for (const tool of ["run-command", "privileged-command", "signal-process", "sftp-list", "sftp-download",
    "sftp-upload", "open-session", "read-session-output", "close-session", "list-sessions", "list-connections"]) {
    const r = decideOwner(state(), call(tool, { profile: "agent" }));
    assert.ok(r?.requireApproval, tool);
    assert.equal(r.block, undefined, tool);
  }
});

test("the card offers allow once and deny only, with the 10 minute limit", () => {
  const r = decideOwner(state(), call("run-command", { command: "touch /tmp/x", profile: "agent" }));
  assert.deepEqual(r.requireApproval.allowedDecisions, ["allow-once", "deny"]);
  assert.equal(r.requireApproval.timeoutMs, MAX_TIMEOUT_MS);
  assert.equal(r.requireApproval.severity, "critical");
});

test("the card shows the exact call, and the plugin returns the arguments it shows", () => {
  const params = { profile: "agent", command: "printf 'a\\tb' > /tmp/x" };
  const r = decideOwner(state(), call("run-command", params));
  const d = r.requireApproval.description;
  assert.ok(d.includes('tool: "host__run-command"'));
  assert.ok(d.includes('profile: "agent"'));
  assert.ok(d.includes(`command: ${JSON.stringify(params.command)}`));
  assert.ok(d.includes(`call: ${canonicalJson(params)}`));
  assert.deepEqual(r.params, params);
});

test("a space at the end of the command shows on the card", () => {
  const r = decideOwner(state(), call("read-command", { command: "uptime ", profile: "agent" }));
  assert.ok(r.requireApproval.description.includes('command: "uptime "'));
});

test("an unknown tool of the host server is refused with no prompt", () => {
  for (const tool of ["reload-config", "sftp-upload-file", "sftp-download-file", "made-up"]) {
    const r = decideOwner(state(), call(tool, {}));
    assert.equal(r?.block, true, tool);
    assert.equal(r.requireApproval, undefined, tool);
  }
});

test("a tool of another server is not the plugin's", () => {
  assert.equal(decideOwner(state(), { toolName: "vault__read_text_file", params: { path: "x" } }), undefined);
  assert.equal(decideOwner(state(), { toolName: "web_fetch", params: {} }), undefined);
  // The prefix is exact: a name that only looks like it is not the host server.
  assert.equal(decideOwner(state(), { toolName: "hostx__runcommand", params: {} }), undefined);
});

test("a host server tool under another server name is refused", () => {
  for (const toolName of ["hostx__run-command", "alias__read-command", "a__b__list-sessions"]) {
    const r = decideOwner(state(), { toolName, params: { command: "uptime", profile: "agent" } });
    assert.equal(r?.block, true, toolName);
  }
});

test("a browser tool named in promptTools prompts (D133)", () => {
  const r = decideOwner(state(), { toolName: "browser__browser_click", params: { ref: "e1" } });
  assert.ok(r?.requireApproval);
  assert.equal(decideOwner(state(), { toolName: "browser__browser_snapshot", params: {} }), undefined);
});

test("a bad plugin config refuses each tool call", () => {
  for (const raw of [
    undefined,
    {},
    { ...RAW, approverId: "" },
    { ...RAW, approverId: " 1" },
    { ...RAW, serverPrefixes: ["host"] },
    { ...RAW, timeoutMs: 600001 },
    { ...RAW, timeoutMs: 0 },
    { ...RAW, extra: true },
    { ...RAW, readList: ["uptime "] },
    { ...RAW, serverPrefixes: ["other__"] },
    { ...RAW, mcpUrl: "ftp://x" },
    { ...RAW, mcpKeyEnv: "lower" },
  ]) {
    const parsed = parseConfig(raw);
    assert.equal(parsed.ok, false, JSON.stringify(raw));
    const r = decideOwner({ errors: parsed.errors }, call("read-command", { command: "uptime", profile: "agent" }));
    assert.equal(r?.block, true);
    assert.equal(decideOwner({ errors: parsed.errors }, { toolName: "web_fetch", params: {} })?.block, true);
  }
});

test("the product config must send the card to the approver only", () => {
  const cfg = parseConfig(RAW).config;
  assert.deepEqual(checkProductConfig(PRODUCT, cfg), []);
  const bad = [
    {},
    { ...PRODUCT, approvals: { plugin: { ...PRODUCT.approvals.plugin, mode: "both" } } },
    { ...PRODUCT, approvals: { plugin: { ...PRODUCT.approvals.plugin, enabled: false } } },
    { ...PRODUCT, approvals: { plugin: { ...PRODUCT.approvals.plugin, targets: [] } } },
    { ...PRODUCT, approvals: { plugin: { ...PRODUCT.approvals.plugin, targets: [{ channel: "telegram", to: "-100" }] } } },
    { ...PRODUCT, approvals: { plugin: { ...PRODUCT.approvals.plugin, targets: [...PRODUCT.approvals.plugin.targets, { channel: "telegram", to: "2" }] } } },
    { ...PRODUCT, channels: { telegram: { execApprovals: { approvers: [APPROVER, "2"] } } } },
    { ...PRODUCT, channels: { telegram: { execApprovals: { enabled: false, approvers: [APPROVER] } } } },
    { ...PRODUCT, channels: { telegram: {} } },
  ];
  for (const p of bad) assert.ok(checkProductConfig(p, cfg).length > 0, JSON.stringify(p));
});

test("a fault in the hook refuses the call", () => {
  const params = {};
  Object.defineProperty(params, "command", { enumerable: true, get() { throw new Error("boom"); } });
  const r = safeDecide(state(), call("run-command", params));
  assert.equal(r?.block, true);
});

test("the card escapes each character outside printable ASCII, and its lines are still the JSON of the call", () => {
  // U+202E (right-to-left override), U+200B (zero width space), U+00A0
  // (no-break space), and one character above U+FFFF.
  const command = "cat /etc/motd\u202e\u200b\u00a0# x \u{1F600}";
  const params = { command, profile: "agent" };
  const text = cardText("host__run-command", params);
  for (const raw of ["\u202e", "\u200b", "\u00a0", "\u{1F600}"]) assert.equal(text.includes(raw), false, raw);
  assert.ok(text.includes(String.raw`\u202e\u200b\u00a0`));
  assert.ok(text.includes(String.raw`\ud83d\ude00`));
  // Each character of the card is printable ASCII or a line break.
  assert.ok(/^[\x20-\x7e\n]*$/.test(text));
  // The update check compares the card and the audit line of the server the
  // same way: JSON.parse of the `call:` line (and of the `command:` line)
  // gives the exact string of the call, so "the command on the card is
  // byte-equal to the command that ran" still compares the same bytes.
  const lines = text.split("\n");
  const callLine = lines.find((l) => l.startsWith("call: "));
  const commandLine = lines.find((l) => l.startsWith("command: "));
  assert.deepEqual(JSON.parse(callLine.slice("call: ".length)), params);
  assert.equal(JSON.parse(commandLine.slice("command: ".length)), command);
  assert.equal(Buffer.compare(Buffer.from(JSON.parse(commandLine.slice(9)), "utf8"), Buffer.from(command, "utf8")), 0);
  // Plain ASCII stays as JSON.stringify writes it, and DEL is escaped.
  assert.equal(asciiJson("ls -la /tmp"), JSON.stringify("ls -la /tmp"));
  assert.equal(asciiJson("a\x7fb"), String.raw`"a\u007fb"`);
  assert.equal(asciiJson("a\nb"), String.raw`"a\nb"`);
});

test("knownTools takes a subset of the built-in list only", () => {
  assert.equal(DEFAULT_KNOWN_TOOLS.length, 12);
  assert.ok(parseConfig({ ...RAW, knownTools: ["read-command", "run-command"] }).ok);
  for (const bad of [["run-command", "new-tool"], ["reload-config"], ["sftp-upload-file"], ["sftp-download-file"], [],
                     ["read-command "], "read-command"]) {
    assert.equal(parseConfig({ ...RAW, knownTools: bad }).ok, false, JSON.stringify(bad));
  }
  const st = state({ ...RAW, knownTools: ["read-command", "run-command"] });
  assert.equal(decideOwner(st, call("privileged-command", { profile: "agent" }))?.block, true);
});

test("the log lines name the tool and the reason, with no channel, sender, chat ID or session key", async () => {
  const hooks = [];
  const lines = [];
  const logger = { info: (m) => lines.push(m), error: (m) => lines.push(m), debug: (m) => lines.push(m) };
  const resolvers = [];
  plugin.register({ pluginConfig: RAW, config: PRODUCT, logger, on: (n, h) => hooks.push(h),
    registerMcpServerConnectionResolver: (r) => resolvers.push(r) });
  await plugin.ready;
  const ctx = { requester: { channel: "irc", senderId: "9000009" }, channelId: "irc:9000009", sessionKey: "session-7f3a" };
  const write = call("run-command", { command: "id", profile: "agent" });
  assert.equal(hooks[0](write, ctx)?.block, true);
  return resolvers[0].resolve({ requesterSenderId: "9000009", messageChannel: "irc" }).then(() => {
    const refused = lines.filter((l) => l.includes("refused"));
    assert.equal(refused.length, 1);
    assert.ok(refused[0].startsWith('host-approval: refused "host__run-command" ('));
    for (const line of lines) {
      for (const id of ["9000009", "session-7f3a", "irc:", APPROVER]) assert.equal(line.includes(id), false, line);
    }
  });
});

test("the register step: a good config hooks with a high priority", async () => {
  const hooks = [];
  const resolvers = [];
  plugin.register({ pluginConfig: RAW, config: PRODUCT, logger: {}, on: (n, h, o) => hooks.push({ n, h, o }),
    registerMcpServerConnectionResolver: (r) => resolvers.push(r) });
  // Until the route check ends, each call is refused.
  assert.equal(((e) => hooks[0].h(e, OWNER))(call("read-command", { command: "uptime", profile: "agent" }))?.block, true);
  await plugin.ready;
  assert.equal(resolvers.length, 1);
  assert.equal(resolvers[0].serverName, "host");
  assert.equal(hooks.length, 1);
  assert.equal(hooks[0].n, "before_tool_call");
  assert.equal(hooks[0].o.priority, 1000);
  assert.equal(((e) => hooks[0].h(e, OWNER))(call("read-command", { command: "uptime", profile: "agent" })), undefined);
  assert.ok(((e) => hooks[0].h(e, OWNER))(call("run-command", { command: "id", profile: "agent" })).requireApproval);
});

test("the register step: a product config with a second approver refuses each call", () => {
  const hooks = [];
  const product = { ...PRODUCT, channels: { telegram: { execApprovals: { approvers: [APPROVER, "2"] } } } };
  plugin.register({ pluginConfig: RAW, config: product, logger: {}, on: (n, h) => hooks.push(h),
    registerMcpServerConnectionResolver: () => {} });
  assert.equal(((e) => hooks[0](e, OWNER))(call("read-command", { command: "uptime", profile: "agent" }))?.block, true);
});

test("the config schema of the entry refuses a bad config", () => {
  assert.equal(defaultPlugin.configSchema.safeParse(RAW).success, true);
  assert.equal(plugin.configSchema.safeParse({ ...RAW, timeoutMs: 900000 }).success, false);
});

test("the whole call is in one code block that the call can not end", () => {
  for (const command of ["ls /etc/*_* `id` __u__", "echo ```x```", "echo ````", "plain"]) {
    const d = cardText("host__run-command", { command, profile: "agent" });
    const fence = codeFence(d.slice(d.indexOf("\n") + 1).split("\n").slice(1, -1).join("\n"));
    const lines = d.split("\n");
    assert.equal(lines[1], lines[lines.length - 1]);
    assert.ok(/^`{3,}$/.test(lines[1]));
    assert.ok(lines[1].length >= fence.length, command);
    const inner = lines.slice(2, -1).join("\n");
    assert.ok(!inner.includes(lines[1]), command);
    assert.ok(inner.includes(JSON.stringify(command)), command);
  }
});

test("the host server connects only through the plugin, with the key from env", () => {
  assert.deepEqual(resolveConnection(state(), { MCP_KEY: KEY }, { requesterSenderId: "1000001", messageChannel: "telegram" }), {
    url: "http://agw:3000/main-host/mcp",
    headers: { Authorization: `Bearer ${KEY}` },
  });
  assert.equal(resolveConnection(state(), {}), null);
  assert.equal(resolveConnection(state(), { MCP_KEY: "short" }), null);
  assert.equal(resolveConnection({ errors: ["x"] }, { MCP_KEY: KEY }), null);
});

test("the manifest declares the host server with an address that never connects", async () => {
  const { readFile } = await import("node:fs/promises");
  const m = JSON.parse(await readFile(new URL("../openclaw.plugin.json", import.meta.url), "utf8"));
  assert.deepEqual(Object.keys(m.mcpServers), ["host"]);
  assert.match(m.mcpServers.host.url, /\.invalid\//);
  assert.equal(m.mcpServers.host.headers, undefined);
});

test("canonicalJson orders the keys", () => {
  assert.equal(canonicalJson({ b: 1, a: [2, { d: 1, c: "x" }] }), '{"a":[2,{"c":"x","d":1}],"b":1}');
});

test("the host server resolves only for the approver", () => {
  const st = state();
  assert.notEqual(resolveConnection(st, { MCP_KEY: KEY }, { requesterSenderId: "telegram:1000001", messageChannel: "telegram" }), null);
  assert.equal(resolveConnection(st, { MCP_KEY: KEY }, { requesterSenderId: "1000002" }), null);
  assert.equal(resolveConnection(st, { MCP_KEY: KEY }, { requesterSenderId: "telegram:1000002" }), null);
  assert.equal(resolveConnection(st, { MCP_KEY: KEY }, {}), null);
  assert.equal(resolveConnection(st, { MCP_KEY: KEY }, undefined), null);
  assert.equal(resolveConnection(st, { MCP_KEY: KEY }, { requesterSenderId: "10000011" }), null);
});

// Reviewer M2: the approver is his ID on his configured channel only.

const OTHER_CHANNELS = ["irc", "signal", "discord", "qa-channel", "Telegram", "telegram ", ""];

test("the resolver needs the approver ID on the approval channel", () => {
  const st = state();
  const ok = (ctx) => resolveConnection(st, { MCP_KEY: KEY }, ctx) !== null;
  assert.equal(ok({ requesterSenderId: APPROVER, messageChannel: "telegram" }), true);
  assert.equal(ok({ requesterSenderId: `telegram:${APPROVER}`, messageChannel: "telegram" }), true);
  for (const channel of OTHER_CHANNELS) {
    assert.equal(ok({ requesterSenderId: APPROVER, messageChannel: channel }), false, channel);
    assert.equal(ok({ requesterSenderId: `telegram:${APPROVER}`, messageChannel: channel }), false, channel);
  }
  // No channel: a turn that the product can not tie to a message.
  assert.equal(ok({ requesterSenderId: APPROVER }), false);
  // A prefix of another channel, or an ID that ends with the approver ID.
  for (const id of [`irc:${APPROVER}`, `signal:${APPROVER}`, `x:telegram:${APPROVER}`, `@u:${APPROVER}`,
                    `nick!u@2001:db8::${APPROVER}`, `+${APPROVER}`, ` ${APPROVER}`, `${APPROVER} `, `0${APPROVER}`]) {
    assert.equal(ok({ requesterSenderId: id, messageChannel: "telegram" }), false, id);
  }
});

test("senderIsApprover and requesterIsApprover agree on the same inputs", () => {
  const st = state();
  for (const channel of ["telegram", ...OTHER_CHANNELS]) {
    for (const id of [APPROVER, `telegram:${APPROVER}`, `irc:${APPROVER}`, "1000002", ""]) {
      assert.equal(senderIsApprover(st, { messageChannel: channel, requesterSenderId: id }),
        requesterIsApprover(st.config, { requester: { channel, senderId: id } }), `${channel} ${id}`);
    }
  }
});

test("the hook refuses each host server call that is not of the approver on his channel", () => {
  const st = state();
  const read = call("read-command", { command: "uptime", profile: "agent" });
  const write = call("run-command", { command: "id", profile: "agent" });
  for (const ctx of [undefined, {}, { requester: {} }, { requester: { channel: "telegram" } },
                     { requester: { senderId: APPROVER } },
                     { requester: { channel: "irc", senderId: APPROVER } },
                     { requester: { channel: "telegram", senderId: "1000002" } },
                     { requester: { channel: "telegram", senderId: `irc:${APPROVER}` } }]) {
    assert.equal(decide(st, read, ctx)?.block, true, JSON.stringify(ctx));
    const w = decide(st, write, ctx);
    assert.equal(w?.block, true, JSON.stringify(ctx));
    assert.equal(w?.requireApproval, undefined, "no card for a turn that is not the approver's");
  }
  // The approver on his channel: the listed read passes, the write asks.
  assert.equal(decide(st, read, OWNER), undefined);
  assert.ok(decide(st, write, OWNER)?.requireApproval);
  assert.ok(decide(st, write, { requester: { channel: "telegram", senderId: `telegram:${APPROVER}` } })?.requireApproval);
});

test("the sender rule does not touch tools of other servers or the browser prompt", () => {
  const st = state();
  assert.equal(decide(st, { toolName: "web_fetch", params: {} }, undefined), undefined);
  assert.equal(decide(st, { toolName: "vault__read_text_file", params: {} }, { requester: { channel: "irc", senderId: "x" } }), undefined);
  assert.ok(decide(st, { toolName: "browser__browser_click", params: { ref: "e1" } }, undefined)?.requireApproval);
});

test("the register step passes the context of the hook", async () => {
  const hooks = [];
  plugin.register({ pluginConfig: RAW, config: PRODUCT, logger: {}, on: (n, h) => hooks.push(h),
    registerMcpServerConnectionResolver: () => {} });
  await plugin.ready;
  const write = call("run-command", { command: "id", profile: "agent" });
  assert.ok(hooks[0](write, OWNER)?.requireApproval);
  assert.equal(hooks[0](write, { requester: { channel: "irc", senderId: APPROVER } })?.block, true);
  assert.equal(hooks[0](write)?.block, true);
});

test("with no requester, the hook needs the direct chat of the approver on his channel", () => {
  const st = state();
  const write = call("run-command", { command: "id", profile: "agent" });
  const at = (ctx) => decide(st, write, ctx);
  // The form of 2026.11.3: no requester, and the conversation as channelId.
  assert.ok(at({ channelId: `telegram:${APPROVER}`, sessionKey: `agent:main:telegram:direct:${APPROVER}` })?.requireApproval);
  for (const channelId of [`irc:${APPROVER}`, "telegram:1000002", "telegram:-1001234", APPROVER, `telegram:${APPROVER}:x`,
                           `Telegram:${APPROVER}`, "", undefined]) {
    assert.equal(at({ channelId })?.block, true, String(channelId));
  }
  // A requester, when the product gives one, wins over the conversation.
  assert.equal(at({ channelId: `telegram:${APPROVER}`, requester: { channel: "irc", senderId: APPROVER } })?.block, true);
  assert.equal(at({ channelId: `telegram:${APPROVER}`, requester: { channel: "telegram", senderId: "1000002" } })?.block, true);
});

test("the product config: no chat commands on other channels; the approval channel may keep its members", () => {
  const cfg = state().config;
  const withCommands = (allowFrom) => ({ ...PRODUCT, commands: allowFrom === undefined
    ? { ownerAllowFrom: PRODUCT.commands.ownerAllowFrom }
    : { ownerAllowFrom: PRODUCT.commands.ownerAllowFrom, allowFrom } });
  for (const good of [{ telegram: [APPROVER], "*": [] }, { telegram: [APPROVER, "1000002"], "*": [] },
                      { telegram: [`telegram:${APPROVER}`, 1000002], "*": [], irc: [] }, { "*": [] },
                      { telegram: [], "*": [] }]) {
    assert.deepEqual(checkProductConfig(withCommands(good), cfg), [], JSON.stringify(good));
  }
  for (const bad of [undefined, [], { telegram: [APPROVER] }, { irc: [] }, { telegram: [APPROVER], "*": ["*"] },
                     { telegram: [APPROVER], "*": [APPROVER] }, { telegram: [APPROVER], "*": [], irc: [APPROVER] },
                     { telegram: ["*"], "*": [] }, { telegram: [" * "], "*": [] }, { telegram: [""], "*": [] },
                     { telegram: "1000001", "*": [] }, { telegram: [APPROVER], "*": [], signal: ["+1"] },
                     { telegram: [APPROVER], "*": {} }]) {
    assert.ok(checkProductConfig(withCommands(bad), cfg).length > 0, JSON.stringify(bad));
  }
});

test("the product config: the owner allow list names the approver alone, on the approval channel", () => {
  const cfg = state().config;
  const withOwners = (ownerAllowFrom) => ({ ...PRODUCT, commands: { ...PRODUCT.commands, ownerAllowFrom } });
  assert.deepEqual(checkProductConfig(withOwners([`telegram:${APPROVER}`]), cfg), []);
  for (const bad of [undefined, [], [APPROVER], [`telegram:${APPROVER}`, "telegram:1000002"], ["telegram:1000002"],
                     [`irc:${APPROVER}`], ["*"], [`telegram:${APPROVER} `], `telegram:${APPROVER}`]) {
    assert.ok(checkProductConfig(withOwners(bad), cfg).length > 0, JSON.stringify(bad));
  }
  const noOwners = { ...PRODUCT, commands: { allowFrom: PRODUCT.commands.allowFrom } };
  assert.ok(checkProductConfig(noOwners, cfg).length > 0);
});

test("no second server entry may reach the host server route or carry its key", () => {
  const cfg = state().config;
  const env = { MCP_KEY: KEY };
  const withServers = (servers) => ({ ...PRODUCT, mcp: { servers } });
  assert.deepEqual(checkProductConfig(PRODUCT, cfg, env), []);
  const url = new URL(cfg.mcpUrl);
  // Another server of the agent on its own route of the same gateway is fine.
  assert.deepEqual(checkProductConfig(withServers({ vault: { url: `${url.origin}/main-vault/mcp` } }), cfg, env), []);
  for (const entry of [
    { url: "http://other:3000/x", headers: { Authorization: "Bearer ${MCP_KEY}" } },
    { url: "http://other:3000/x", headers: { Authorization: "Bearer $MCP_KEY" } },
    { url: "http://other:3000/x", headers: { Authorization: "Bearer ${MCP_KEY:-x}" } },
    { url: "http://other:3000/x", headers: { Authorization: { source: "env", provider: "default", id: "MCP_KEY" } } },
    { url: "http://other:3000/x", headers: { Authorization: `Bearer ${KEY}` } },
    { url: `http://other:3000/x?key=${KEY}` },
    // Basic auth: base64 of "user:key", and of other prefixes.
    { url: "http://other:3000/x", headers: { Authorization: `Basic ${Buffer.from(`x:${KEY}`).toString("base64")}` } },
    { url: "http://other:3000/x", headers: { Authorization: `Basic ${Buffer.from(`ab:${KEY}`).toString("base64")}` } },
    { url: "http://other:3000/x", headers: { Authorization: `Basic ${Buffer.from(`abc:${KEY}`).toString("base64")}` } },
    { url: "http://other:3000/x", headers: { "X-Key": Buffer.from(KEY).toString("base64url") } },
  ]) {
    assert.ok(checkProductConfig(withServers({ hostb: entry }), cfg, env).length > 0, JSON.stringify(entry));
  }
  // A longer variable name that holds the name is another variable.
  assert.deepEqual(checkProductConfig(withServers({ vault: { url: "http://other:3000/x",
    headers: { Authorization: "Bearer ${MAIN_MCP_KEY_VAULT}" } } }), cfg, env), []);
});

test("the key belongs to the plugin entry only: another place of the config with a form of it refuses", () => {
  const cfg = state().config;
  const env = { MCP_KEY: KEY };
  const own = { ...PRODUCT, plugins: { entries: { "host-approval": { enabled: true, config: RAW } } } };
  assert.deepEqual(checkProductConfig(own, cfg, env), []);
  for (const extra of [
    { env: { vars: { OTHER_KEY: "${MCP_KEY}" } } },
    { agents: { defaults: { sandbox: { env: ["MCP_KEY"] } } } },
    { tools: { web: { headers: { Authorization: `Bearer ${KEY}` } } } },
  ]) {
    const errors = checkProductConfig({ ...own, ...extra }, cfg, env);
    assert.ok(errors.some((e) => e.includes("carries the key")), JSON.stringify(extra));
  }
  assert.deepEqual(findKeyForms(own, cfg, env), []);
});

test("routeOf: host, port and the decoded first path segment", () => {
  assert.deepEqual(routeOf("http://agw:3000/main-host/mcp"), { host: "agw", port: 3000, route: "main-host" });
  assert.deepEqual(routeOf("HTTP://AGW.:3000/Main-Host/mcp"), { host: "agw", port: 3000, route: "main-host" });
  assert.deepEqual(routeOf("http://agw:3000/main%2Dhost/mcp"), { host: "agw", port: 3000, route: "main-host" });
  assert.deepEqual(routeOf("http://agw:3000/%6d%61in-host"), { host: "agw", port: 3000, route: "main-host" });
  assert.deepEqual(routeOf("http://agw:3000//./x/../main-host/mcp"), { host: "agw", port: 3000, route: "main-host" });
  assert.deepEqual(routeOf("http://agw:3000/x%2F..%2Fmain-host/mcp"), { host: "agw", port: 3000, route: "main-host" });
  assert.deepEqual(routeOf("https://agw/main-host"), { host: "agw", port: 443, route: "main-host" });
  assert.deepEqual(routeOf("http://[fd00::10]:3000/main-host"), { host: "fd00::10", port: 3000, route: "main-host" });
  assert.throws(() => routeOf("http://agw:3000/%E0%A4%A"));
  assert.throws(() => routeOf("not a url"));
});

test("the route check by address: another name, address or form of the host route is refused", async () => {
  const cfg = state().config;
  const at = (url) => checkServerRoutes({ hostb: { url } }, cfg, lookup);
  // Another server on its own route of the same gateway, and another
  // service, are fine.
  assert.deepEqual(await checkServerRoutes(undefined, cfg, lookup), []);
  assert.deepEqual(await at("http://agw:3000/main-vault/mcp"), []);
  assert.deepEqual(await at("http://agw.ai.svc.cluster.local:3000/main-vault/mcp"), []);
  assert.deepEqual(await checkServerRoutes({ local: { command: "x" } }, cfg, lookup), []);
  for (const url of [
    "http://agw:3000/main-host/mcp",
    "http://agw.ai.svc.cluster.local:3000/main-host/mcp",
    "http://10.96.0.10:3000/main-host/mcp",
    "http://[::ffff:10.96.0.10]:3000/main-host/mcp",
    "http://agw-dual:3000/main-host/mcp",
    "http://AGW:3000/MAIN-HOST/other",
    "http://agw:3000/main%2Dhost/mcp",
    "http://agw:3000/./main-host",
    "http://agw.ai.svc.cluster.local.:3000/x/../main-host/mcp",
    // Any host and any port with the host route as its path: the headless
    // service name with the pod's port, the pod address on the target port,
    // another port of the service, and a host that is not the gateway.
    "http://agw-headless.ai.svc.cluster.local:15000/main-host/mcp",
    "http://10.244.1.23:15000/main-host/mcp",
    "http://agw:3001/main-host/mcp",
    "http://other:3000/main-host/mcp",
    "https://example.invalid/Main%2DHost/mcp",
    // A name that does not resolve, with the host route as its path.
    "http://nowhere:3000/main-host/mcp",
  ]) {
    const errors = await at(url);
    assert.deepEqual(errors, ["mcp.servers.hostb points at the route of the host server"], url);
  }
  // Fail closed: a name that does not resolve, or an address that is not valid.
  assert.deepEqual(await at("http://nowhere:3000/main-vault/mcp"), []);

  assert.deepEqual(await at("http://agw:3000/%E0%A4%A"), ["mcp.servers.hostb has an address that is not valid"]);
  assert.deepEqual(await checkServerRoutes(undefined, state({ ...RAW, mcpUrl: "http://nowhere:3000/main-host/mcp" }).config, lookup),
    ["the host of mcpUrl does not resolve"]);
  // An mcpUrl with no route: each path of the same address and port is the host.
  const root = state({ ...RAW, mcpUrl: "http://agw:3000/" }).config;
  assert.equal((await checkServerRoutes({ v: { url: "http://10.96.0.10:3000/main-vault/mcp" } }, root, lookup)).length, 1);
  // There a name that does not resolve fails closed.
  assert.deepEqual(await checkServerRoutes({ hostb: { url: "http://nowhere:3000/main-vault/mcp" } }, root, lookup),
    ["mcp.servers.hostb: its host does not resolve"]);
});

test("the register step: a name that does not resolve refuses each call, and the plugin loads when it resolves", async () => {
  const table = { ...DNS };
  delete table.agw;
  let calls = 0;
  const flaky = async (host) => {
    calls += 1;
    if (calls === 3) table.agw = DNS.agw; // DNS is ready at the third try
    if (!table[host]) throw new Error("ENOTFOUND");
    return table[host].map((address) => ({ address }));
  };
  const p = makePlugin({ lookup: flaky, retryMs: [1] });
  const hooks = [];
  const lines = [];
  p.register({ pluginConfig: RAW, config: PRODUCT, logger: { info: (m) => lines.push(m), error: (m) => lines.push(m) },
    on: (n, h) => hooks.push(h), registerMcpServerConnectionResolver: () => {} });
  const write = call("run-command", { command: "id", profile: "agent" });
  assert.equal(hooks[0](write, OWNER)?.block, true);
  await p.ready;
  assert.ok(hooks[0](write, OWNER)?.requireApproval);
  assert.equal(lines.filter((l) => l.includes("refusing each tool call")).length, 1);
  assert.equal(lines.filter((l) => l.includes("loaded")).length, 1);
  // A second entry at the host route under another name stays refused.
  const p2 = makePlugin({ lookup, retryMs: [1] });
  const hooks2 = [];
  p2.register({ pluginConfig: RAW, config: { ...PRODUCT, mcp: { servers: { hostb: { url: "http://10.96.0.10:3000/main-host/mcp" } } } },
    logger: {}, on: (n, h) => hooks2.push(h), registerMcpServerConnectionResolver: () => {} });
  await p2.ready;
  assert.equal(hooks2[0](write, OWNER)?.block, true);
  assert.equal(hooks2[0]({ toolName: "web_fetch", params: {} }, OWNER)?.block, true);
});

test("why the server entry check: a host tool that the plugin does not know passes under another prefix", () => {
  // The hook refuses a known host tool under another prefix ...
  assert.equal(decide(state(), { toolName: "hostb__run-command", params: {} }, OWNER)?.block, true);
  // ... but a tool name it does not know, under another prefix, is not its
  // call. Only the config check (no second entry) closes that path.
  assert.equal(decide(state(), { toolName: "hostb__new-exec-tool", params: {} }, OWNER), undefined);
});
