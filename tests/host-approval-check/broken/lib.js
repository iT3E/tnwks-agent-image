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
  const knownTools = raw.knownTools ?? DEFAULT_KNOWN_TOOLS;
  if (!isStringArray(knownTools) || knownTools.length === 0) errors.push("knownTools: a list of tool names");
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
export function checkProductConfig(productConfig, config) {
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
  return errors;
}

// A stable text of the arguments: the keys in order, so the card shows the
// call in one form.
export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

function hostToolOf(toolName, config) {
  for (const prefix of config.serverPrefixes) {
    if (toolName.startsWith(prefix)) return toolName.slice(prefix.length);
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
// value is in JSON quotes, so a space or a control character shows.
export function cardText(toolName, params) {
  const lines = [`tool: ${JSON.stringify(toolName)}`];
  if (params && typeof params === "object") {
    if (typeof params.profile === "string") lines.push(`profile: ${JSON.stringify(params.profile)}`);
    if (typeof params.command === "string") lines.push(`command: ${JSON.stringify(params.command)}`);
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
export function decide(state, event) {
  const toolName = event?.toolName;
  if (typeof toolName !== "string") return undefined;
  const params = event.params;
  if (!state.config) {
    // A bad config: the plugin can not tell its tools from others, so it
    // refuses each tool call of the agent. The fault is loud, not silent.
    return block(`the plugin config is not valid (${state.errors.join("; ")})`);
  }
  const config = state.config;
  if (config.promptTools.has(toolName)) {
    return approval(config, "Browser action", toolName, params);
  }
  const tool = hostToolOf(toolName, config);
  if (tool === undefined) return undefined;
  if (!config.knownTools.has(tool)) return block(`unknown tool of the host server: ${tool}`);
  if (isListedRead(tool, params, config)) return undefined;
  return approval(config, "Host command", toolName, params);
}

// The connection of the host server for one turn: the gateway address and
// key, or null (the server is left out of the turn) on any fault.
export function resolveConnection(state, env) {
  try {
    if (!state.config) return null;
    const key = env[state.config.mcpKeyEnv];
    if (typeof key !== "string" || key.length < 32) return null;
    return { url: state.config.mcpUrl, headers: { Authorization: `Bearer ${key}` } };
  } catch {
    return null;
  }
}

// The hook body, with each fault turned into a refusal.
export function safeDecide(state, event) {
  try {
    return decide(state, event);
  } catch (err) {
    return block(`the hook failed (${String(err)})`);
  }
}
