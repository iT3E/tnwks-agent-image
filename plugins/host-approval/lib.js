// The logic of the host approval plugin, with no import of the product, so
// the unit tests run it with node alone.
//
// One rule: a tool call of the host server runs with no prompt only when it
// is a `read-command` on the read profile whose command is an exact line of
// the read list, with no other argument. Each other call of the host server
// asks the approver (allow once or deny, 10 minutes at most). A tool of the
// host server that the config does not know is refused with no prompt. A
// tool that the config names in `promptTools` (a browser tool that can send
// a form) also asks. Any fault of config or of the hook refuses the call.

export const PLUGIN_ID = "host-approval";
export const MAX_TIMEOUT_MS = 600_000;
const DEFAULT_TIMEOUT_MS = 600_000;

// The tools of `ssh-mcp` v2.26.0 that the plugin knows. A name that is not
// here is refused before a prompt (a new tool of a later release needs a
// change of this list, through review).
export const DEFAULT_KNOWN_TOOLS = Object.freeze([
  "read-command",
  "run-command",
  "privileged-command",
  "signal-process",
  "sftp-list",
  "sftp-download",
  "sftp-upload",
  "open-session",
  "read-session-output",
  "close-session",
  "list-sessions",
  "list-connections",
]);

const CONFIG_KEYS = new Set([
  "serverPrefixes",
  "knownTools",
  "readProfile",
  "readList",
  "promptTools",
  "approverId",
  "approvalChannel",
  "timeoutMs",
  "mcpUrl",
  "mcpKeyEnv",
]);

// The MCP server of the host tools. The plugin manifest declares it, so it
// exists only while this plugin is on; and only the resolver of this plugin
// gives it its gateway address and key, so a plugin that is off, missing or
// broken leaves the agent with no host tool at all.
export const MCP_SERVER_NAME = "host";

function isNonEmptyString(v) {
  return typeof v === "string" && v.trim().length > 0 && v === v.trim();
}

function isStringArray(v) {
  return Array.isArray(v) && v.every((x) => typeof x === "string");
}

// Returns { ok: true, config } or { ok: false, errors: [...] }. The plugin
// config is checked fully: an unknown key, a wrong type or a missing value
// is an error, and an error makes the plugin refuse each call it owns.
export function parseConfig(raw) {
  const errors = [];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { ok: false, errors: ["the plugin config must be an object"] };
  }
  for (const key of Object.keys(raw)) {
    if (!CONFIG_KEYS.has(key)) errors.push(`unknown key: ${key}`);
  }
  const serverPrefixes = raw.serverPrefixes;
  if (!isStringArray(serverPrefixes) || serverPrefixes.length === 0 || !serverPrefixes.every((p) => isNonEmptyString(p) && p.endsWith("__"))) {
    errors.push("serverPrefixes: a list of one or more tool name prefixes, each ending in __");
  }
  // A subset of the built-in list only: a new tool of a later server
  // release needs a change of this code, through review.
  const knownTools = raw.knownTools ?? DEFAULT_KNOWN_TOOLS;
  if (!isStringArray(knownTools) || knownTools.length === 0 ||
      !knownTools.every((tool) => DEFAULT_KNOWN_TOOLS.includes(tool))) {
    errors.push("knownTools: a list of tool names of the built-in list");
  }
  if (!isNonEmptyString(raw.readProfile)) errors.push("readProfile: the name of the SSH profile of the reads");
  const readList = raw.readList ?? [];
  if (!isStringArray(readList) || !readList.every(isNonEmptyString)) {
    errors.push("readList: a list of exact command lines, each with no space at the start or end");
  }
  const promptTools = raw.promptTools ?? [];
  if (!isStringArray(promptTools) || !promptTools.every(isNonEmptyString)) errors.push("promptTools: a list of exact tool names");
  if (!isNonEmptyString(raw.approverId)) errors.push("approverId: the ID of the approver");
  if (!isNonEmptyString(raw.approvalChannel)) errors.push("approvalChannel: the channel of the approver");
  const timeoutMs = raw.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  if (!Number.isInteger(timeoutMs) || timeoutMs <= 0 || timeoutMs > MAX_TIMEOUT_MS) {
    errors.push(`timeoutMs: an integer from 1 to ${MAX_TIMEOUT_MS}`);
  }
  if (typeof raw.mcpUrl !== "string" || !/^https?:\/\/[^\s]+$/.test(raw.mcpUrl)) {
    errors.push("mcpUrl: the http or https address of the host route of the gateway");
  }
  if (typeof raw.mcpKeyEnv !== "string" || !/^[A-Z][A-Z0-9_]*$/.test(raw.mcpKeyEnv)) {
    errors.push("mcpKeyEnv: the name of the environment variable that holds the gateway key");
  }
  if (Array.isArray(serverPrefixes) && !(serverPrefixes.length === 1 && serverPrefixes[0] === `${MCP_SERVER_NAME}__`)) {
    errors.push(`serverPrefixes: must be ["${MCP_SERVER_NAME}__"], the server of the manifest`);
  }
  if (errors.length) return { ok: false, errors };
  return {
    ok: true,
    config: Object.freeze({
      serverPrefixes: Object.freeze([...serverPrefixes]),
      knownTools: new Set(knownTools),
      readProfile: raw.readProfile,
      readList: new Set(readList),
      promptTools: new Set(promptTools),
      approverId: raw.approverId,
      approvalChannel: raw.approvalChannel,
      timeoutMs,
      mcpUrl: raw.mcpUrl,
      mcpKeyEnv: raw.mcpKeyEnv,
    }),
  };
}

// The product must send each plugin approval to the approver only, and only
// the approver may answer it. The plugin can not choose who answers, so it
// checks the product config at load and refuses each call it owns if the
// config does not hold that rule.
export function checkProductConfig(productConfig, config, env = {}) {
  const errors = [];
  const plugin = productConfig?.approvals?.plugin;
  if (!plugin || plugin.enabled !== true) errors.push("approvals.plugin.enabled must be true");
  if (plugin?.mode !== "targets") errors.push('approvals.plugin.mode must be "targets"');
  const targets = Array.isArray(plugin?.targets) ? plugin.targets : [];
  if (
    targets.length !== 1 ||
    targets[0]?.channel !== config.approvalChannel ||
    String(targets[0]?.to) !== config.approverId ||
    targets[0]?.threadId !== undefined
  ) {
    errors.push("approvals.plugin.targets must be the approver only, in his direct chat");
  }
  const channel = productConfig?.channels?.[config.approvalChannel];
  const approvers = channel?.execApprovals?.approvers;
  if (!Array.isArray(approvers) || approvers.length !== 1 || String(approvers[0]) !== config.approverId) {
    errors.push(`channels.${config.approvalChannel}.execApprovals.approvers must be the approver only`);
  }
  if (channel?.execApprovals?.enabled === false) {
    errors.push(`channels.${config.approvalChannel}.execApprovals.enabled must not be false`);
  }
  // A card is answered by a button or by the /approve command. On the
  // approval channel the product holds both with its own approver rule
  // (channels.<channel>.execApprovals.approvers, checked above), whatever
  // the command rights of the sender. A channel with no approver rule of
  // its own (IRC, in the lab) lets each sender who may run commands answer
  // any plugin card by its ID. So the "*" list (each channel with no key of
  // its own) must be there and empty, and each other key but the approval
  // channel must be empty. The approval channel may keep its members, so
  // they keep the ordinary commands; no "*" wildcard there. With no key for
  // the approval channel, it takes the empty "*" list (no commands at all).
  // A channel with no key and no "*" list falls back to its own allowFrom,
  // so "*" is required.
  const commandsAllowFrom = productConfig?.commands?.allowFrom;
  if (!commandsAllowFrom || typeof commandsAllowFrom !== "object" || Array.isArray(commandsAllowFrom)) {
    errors.push("commands.allowFrom must be set");
  } else {
    const star = commandsAllowFrom["*"];
    if (!Array.isArray(star) || star.length !== 0) errors.push('commands.allowFrom."*" must be an empty list');
    for (const [key, list] of Object.entries(commandsAllowFrom)) {
      if (key === "*") continue;
      if (key === config.approvalChannel) {
        const ok = Array.isArray(list) && list.every((v) => (typeof v === "string" || typeof v === "number") &&
          String(v).trim() !== "*" && String(v).trim().length > 0);
        if (!ok) errors.push(`commands.allowFrom.${key} must be a list of senders, with no "*"`);
      } else if (!Array.isArray(list) || list.length !== 0) {
        errors.push(`commands.allowFrom.${key} must be empty`);
      }
    }
  }
  // The owner allow list names the approver alone, on the approval
  // channel. With it the product drops an answer from another channel
  // (D137), and no other sender is an owner of the product.
  const owners = productConfig?.commands?.ownerAllowFrom;
  if (!Array.isArray(owners) || owners.length !== 1 ||
      String(owners[0]) !== `${config.approvalChannel}:${config.approverId}`) {
    errors.push("commands.ownerAllowFrom must name the approver only, on the approval channel");
  }
  // The hook knows the host tools by name. A second server entry that
  // points at the route of the host server gives the same tools under
  // another prefix, and a tool that the plugin does not know (a new tool of
  // a later server release) would pass with no card. So no server entry of
  // the product config may point at that route or carry the key of the
  // plugin: the host server comes only through the resolver of the plugin.
  // The route check by address is async (checkServerRoutes); the plugin
  // entry runs it after this one.
  errors.push(...checkServerEntries(productConfig?.mcp?.servers, config, env));
  for (const path of findKeyForms(productConfig, config, env)) {
    if (!path.startsWith("mcp.servers.")) errors.push(`${path} carries the key of the host server`);
  }
  return errors;
}

// The forms of the key that a config can carry: the name of its env
// variable as a word (`${NAME}`, `$NAME`, a secret reference with the id
// NAME, or a second variable set from it), the key, and its base64 at each
// of the three offsets (a basic auth header holds "user:key").
function keyForms(config, env) {
  const name = config.mcpKeyEnv;
  const nameWord = new RegExp(`(^|[^A-Za-z0-9_])${name}([^A-Za-z0-9_]|$)`);
  const key = env[config.mcpKeyEnv];
  const values = [];
  if (typeof key === "string" && key.length >= 32) {
    values.push(key, encodeURIComponent(key));
    for (let k = 0; k < 3; k += 1) {
      const b64 = Buffer.from("\0".repeat(k) + key, "utf8").toString("base64");
      // Skip the characters that hold bits of the prefix, and the last
      // four, which depend on what follows the key.
      const core = b64.slice(Math.ceil((k * 4) / 3), -4);
      values.push(core, core.replace(/\+/g, "-").replace(/\//g, "_"));
    }
  }
  return (text) => nameWord.test(text) || values.some((v) => text.includes(v));
}

// Each place of the product config, but the entry of this plugin, where a
// form of the key is. The key belongs to this plugin only.
export function findKeyForms(productConfig, config, env = {}) {
  const has = keyForms(config, env);
  const found = [];
  const walk = (value, path) => {
    if (found.length >= 5) return;
    if (path === `plugins.entries.${PLUGIN_ID}`) return;
    if (typeof value === "string") {
      if (has(value)) found.push(path);
    } else if (Array.isArray(value)) {
      value.forEach((v, i) => walk(v, `${path}[${i}]`));
    } else if (value && typeof value === "object") {
      for (const [k, v] of Object.entries(value)) {
        const p = path ? `${path}.${k}` : k;
        if (has(k)) found.push(p);
        walk(v, p);
      }
    }
  };
  walk(productConfig, "");
  return found;
}

export function checkServerEntries(servers, config, env = {}) {
  const errors = [];
  if (servers === undefined) return errors;
  if (!servers || typeof servers !== "object" || Array.isArray(servers)) return ["mcp.servers must be an object"];
  const has = keyForms(config, env);
  for (const [name, entry] of Object.entries(servers)) {
    if (has(JSON.stringify(entry ?? null))) errors.push(`mcp.servers.${name} carries the key of the host server`);
  }
  return errors;
}

const DEFAULT_PORTS = { "http:": 80, "https:": 443, "ws:": 80, "wss:": 443 };

// The route of an address as the gateway sees it: the host, the port, and
// the first segment of the path after percent decoding, with dot segments
// resolved, in lower case. Throws on an address that is not valid.
export function routeOf(url) {
  const u = new URL(url);
  const port = u.port ? Number(u.port) : DEFAULT_PORTS[u.protocol];
  if (!port) throw new Error(`no port for the scheme ${u.protocol}`);
  const segments = [];
  for (const raw of u.pathname.split("/")) {
    for (const part of decodeURIComponent(raw).split(/[/\\]/)) {
      if (part === "" || part === ".") continue;
      if (part === "..") segments.pop();
      else segments.push(part.toLowerCase());
    }
  }
  const host = u.hostname.replace(/^\[|\]$/g, "").replace(/\.$/, "").toLowerCase();
  return { host, port, route: segments[0] ?? "" };
}

// An IPv4 address mapped into IPv6 (::ffff:10.0.0.1 or ::ffff:a00:1) is
// the IPv4 address.
function plainAddress(address) {
  const a = String(address).toLowerCase().replace(/^\[|\]$/g, "");
  if (!a.startsWith("::ffff:")) return a;
  const rest = a.slice(7);
  if (rest.includes(".")) return rest;
  const m = /^([0-9a-f]{1,4}):([0-9a-f]{1,4})$/.exec(rest);
  if (!m) return a;
  const [hi, lo] = [parseInt(m[1], 16), parseInt(m[2], 16)];
  return [hi >> 8, hi & 255, lo >> 8, lo & 255].join(".");
}

// The route check: an entry is at the host route when its first path
// segment is the route of mcpUrl, on any host and any port; or, when
// mcpUrl has no route, when its port is that of mcpUrl and its host is the
// same name or shares an address with it. A name that does not resolve, or an address that is not valid, is an
// error: the plugin can not tell, so it fails closed. `lookup(host)` returns
// the addresses of a host (dns.promises.lookup with all: true).
export async function checkServerRoutes(servers, config, lookup) {
  const errors = [];
  let host;
  try {
    host = routeOf(config.mcpUrl);
  } catch {
    return ["mcpUrl is not a valid address"];
  }
  const addressesOf = async (name) => new Set((await lookup(name)).map((r) => plainAddress(r.address ?? r)));
  let hostAddresses;
  try {
    hostAddresses = await addressesOf(host.host);
  } catch {
    return ["the host of mcpUrl does not resolve"];
  }
  if (hostAddresses.size === 0) return ["the host of mcpUrl does not resolve"];
  if (servers === undefined) return errors;
  if (!servers || typeof servers !== "object" || Array.isArray(servers)) return ["mcp.servers must be an object"];
  for (const [name, entry] of Object.entries(servers)) {
    if (entry?.url === undefined) continue;
    let target;
    try {
      target = routeOf(String(entry.url));
    } catch {
      errors.push(`mcp.servers.${name} has an address that is not valid`);
      continue;
    }
    // The route names of the central gateway are unique, and the other
    // servers of the agent have their own routes. So an entry whose first
    // path segment is the host route is refused on any host and any port:
    // a headless service name, a pod address on the target port and
    // another port of the service all reach the same route.
    if (host.route !== "" && target.route === host.route) {
      errors.push(`mcp.servers.${name} points at the route of the host server`);
      continue;
    }
    if (target.port !== host.port || (host.route !== "" && target.route !== host.route)) continue;
    let same = target.host === host.host;
    if (!same) {
      try {
        const addresses = await addressesOf(target.host);
        if (addresses.size === 0) throw new Error("no address");
        same = [...addresses].some((a) => hostAddresses.has(a));
      } catch {
        errors.push(`mcp.servers.${name}: its host does not resolve`);
        continue;
      }
    }
    if (same) errors.push(`mcp.servers.${name} points at the route of the host server`);
  }
  return errors;
}

// JSON with each character outside printable ASCII (U+0020 to U+007E)
// escaped as \uXXXX (a surrogate pair as two escapes). So the card shows
// each character, also an invisible or a reordering one, and the text is
// still JSON: JSON.parse of it gives the same string, the bytes of the call.
export function asciiJson(value) {
  const text = JSON.stringify(value);
  if (text === undefined) return "null";
  return text.replace(/[^\x20-\x7e]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`);
}

// A stable text of the arguments: the keys in order, so the card shows the
// call in one form.
export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((k) => `${asciiJson(k)}:${canonicalJson(value[k])}`).join(",")}}`;
  }
  return asciiJson(value);
}

function hostToolOf(toolName, config) {
  for (const prefix of config.serverPrefixes) {
    if (toolName.startsWith(prefix)) return { tool: toolName.slice(prefix.length), known: true };
  }
  // A tool of the host server under another server name (a second entry of
  // the config that points at the host route) is refused, not passed.
  const sep = toolName.lastIndexOf("__");
  if (sep > 0 && config.knownTools.has(toolName.slice(sep + 2))) {
    return { tool: toolName.slice(sep + 2), known: false };
  }
  return undefined;
}

export function isListedRead(tool, params, config) {
  if (tool !== "read-command") return false;
  if (!params || typeof params !== "object" || Array.isArray(params)) return false;
  const keys = Object.keys(params).sort();
  if (keys.length !== 2 || keys[0] !== "command" || keys[1] !== "profile") return false;
  if (params.profile !== config.readProfile) return false;
  return typeof params.command === "string" && config.readList.has(params.command);
}

// A code fence longer than each run of backticks in the text, so the text
// can not end the fence early (CommonMark).
export function codeFence(text) {
  const longest = Math.max(0, ...[...text.matchAll(/`+/g)].map((m) => m[0].length));
  return "`".repeat(Math.max(3, longest + 1));
}

// The card text. The product renders the description as Markdown on the chat
// channel: in the lab, `*`, `_`, `__` and backticks of a command became
// italic, bold or code, and a backslash escape lost its backslash. So the
// whole call is in one code block, where nothing is rendered, and each
// value is in JSON quotes with each character outside printable ASCII
// escaped (asciiJson), so a space, a control character, an invisible
// character or a reordering mark shows.
export function cardText(toolName, params) {
  const lines = [`tool: ${asciiJson(toolName)}`];
  if (params && typeof params === "object") {
    if (typeof params.profile === "string") lines.push(`profile: ${asciiJson(params.profile)}`);
    if (typeof params.command === "string") lines.push(`command: ${asciiJson(params.command)}`);
  }
  lines.push(`call: ${canonicalJson(params ?? {})}`);
  const body = lines.join("\n");
  const fence = codeFence(body);
  return `Check each character of the call:\n${fence}\n${body}\n${fence}`;
}

function approval(config, title, toolName, params) {
  return {
    // The plugin returns the arguments it shows, so the call that runs is
    // the call on the card, even if an earlier hook changed them.
    params: params && typeof params === "object" ? params : {},
    requireApproval: {
      title,
      description: cardText(toolName, params),
      severity: "critical",
      timeoutMs: config.timeoutMs,
      allowedDecisions: ["allow-once", "deny"],
    },
  };
}

function block(reason) {
  return { block: true, blockReason: `${PLUGIN_ID}: ${reason}` };
}

// The decision for one tool call. `state` is { config } or { errors }.
// Returns undefined (not ours, or a listed read), a block, or an approval.
export function decide(state, event, ctx) {
  const toolName = event?.toolName;
  if (typeof toolName !== "string") return undefined;
  const params = event.params;
  if (state.pending) {
    // The route check by address has not ended yet (it runs at load).
    return block("the server entries are not checked yet");
  }
  if (!state.config) {
    // A bad config: the plugin can not tell its tools from others, so it
    // refuses each tool call of the agent. The fault is loud, not silent.
    return block(`the plugin config is not valid (${state.errors.join("; ")})`);
  }
  const config = state.config;
  if (config.promptTools.has(toolName)) {
    return approval(config, "Browser action", toolName, params);
  }
  const host = hostToolOf(toolName, config);
  if (host === undefined) return undefined;
  if (!host.known) return block(`a host server tool under another server name: ${toolName}`);
  const tool = host.tool;
  if (!config.knownTools.has(tool)) return block(`unknown tool of the host server: ${tool}`);
  if (!requesterIsApprover(config, ctx)) {
    return block("a host server call is for the approver on his channel only");
  }
  if (isListedRead(tool, params, config)) return undefined;
  return approval(config, "Host command", toolName, params);
}

// The approver is one ID on one channel: his ID on his configured channel,
// and never the same ID on another channel. During a cutover two channels
// run side by side, and an ID of one channel can be a valid ID of a sender
// on the other (two numeric IDs, or a name that a sender picks). So each
// check compares the channel first, and then the whole ID: the bare ID, or
// the ID with the prefix of that same channel. No other prefix and no part
// of an ID counts.
function isApproverId(config, channel, senderId) {
  if (typeof channel !== "string" || channel !== config.approvalChannel) return false;
  if (typeof senderId !== "string" || senderId.length === 0) return false;
  return senderId === config.approverId || senderId === `${config.approvalChannel}:${config.approverId}`;
}

// The connection of the host server for one turn: the gateway address and
// key, or null (the server is left out of the turn) on any fault.
// The host server exists only for a turn of the approver on his channel:
// the product gives the resolver the sender (requesterSenderId) and the
// channel of the message (messageChannel). Any other sender, the approver
// ID on another channel, and a turn with no sender or no channel (a
// scheduled job, a heartbeat, a sub-agent) get no host tools.
export function senderIsApprover(state, ctx) {
  return isApproverId(state.config, ctx?.messageChannel, ctx?.requesterSenderId);
}

// The same rule in the hook. The product gives the hook the requester of the
// turn (channel and sender ID) only where it can prove it; at 2026.11.3 the
// embedded runner gives none (lab of round 2c), and gives the conversation
// of the turn as channelId ("<channel>:<chat ID>"). So: with a requester,
// the requester must be the approver on his channel; with none, the
// conversation must be the direct chat of the approver on his channel (a
// direct chat has one sender). Anything else is refused before a card, so a
// card is only made in a turn of the approver.
export function requesterIsApprover(config, ctx) {
  const requester = ctx?.requester;
  if (requester && typeof requester === "object") {
    return isApproverId(config, requester.channel, requester.senderId);
  }
  return typeof ctx?.channelId === "string" && ctx.channelId === `${config.approvalChannel}:${config.approverId}`;
}

export function resolveConnection(state, env, ctx) {
  try {
    if (!state.config) return null;
    if (!senderIsApprover(state, ctx)) return null;
    const key = env[state.config.mcpKeyEnv];
    if (typeof key !== "string" || key.length < 32) return null;
    return { url: state.config.mcpUrl, headers: { Authorization: `Bearer ${key}` } };
  } catch {
    return null;
  }
}

// The hook body, with each fault turned into a refusal.
export function safeDecide(state, event, ctx) {
  try {
    return decide(state, event, ctx);
  } catch (err) {
    return block(`the hook failed (${String(err)})`);
  }
}
