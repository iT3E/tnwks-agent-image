// Spike: a Messages API bridge. Test code only, not a product.
//
// It serves `POST /v1/messages` (JSON and SSE) and `GET /health`, and runs
// each conversation through the unmodified Claude Agent SDK, so the Claude
// Code engine makes the upstream model call. The upstream and its credential
// are whatever the SDK's environment gives (ANTHROPIC_BASE_URL and a key in
// CI, the SDK's own sign-in on a workstation). The bridge never forwards the
// key of its client.
//
// The mapping (README.md has the details):
// - The client's `system` is the SDK's custom system prompt (`systemPrompt`
//   as a string, or a string[] for system blocks).
// - The client's `tools` are offered through one in-process MCP server
//   ("client"), with the client's JSON Schema as written. The engine names
//   them `mcp__client__<name>`; the bridge gives the client its own names
//   back. The handler of a tool runs nothing: the bridge ends the HTTP
//   response with the tool_use blocks, keeps the SDK session open with the
//   handler pending, and resolves it when the next request brings the
//   tool_result with the same tool_use id.
// - Claude Code's built-in tools are off (`tools: []`), with no settings
//   sources, no skills, no hooks, and no MCP servers but this one.
// - One SDK session (one Claude Code process) for each conversation. A
//   request is matched to its session by the tool_use ids of the last
//   assistant message, or by a hash of the conversation as the client has
//   it (text, tool_use ids, tool_result ids). A request with history but no live session gets
//   a new session whose first prompt holds the history as text.
//
// It adds nothing to look like another client, and removes nothing from the
// client's prompt. It sets CLAUDE_AGENT_SDK_CLIENT_APP, the SDK's documented
// way to name the app that uses it.

import { createHash, randomUUID } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { query } from "@anthropic-ai/claude-agent-sdk";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const PORT = Number(process.env.PORT || 8080);
const IDLE_MS = Number(process.env.BRIDGE_IDLE_SECONDS || 900) * 1000;
// A model call that does not end in this time fails the request (504).
const CALL_MS = Number(process.env.BRIDGE_CALL_SECONDS || 300) * 1000;
// At most this many live sessions (Claude Code processes); a new one closes
// the idle session that was used least recently.
const MAX_SESSIONS = Number(process.env.BRIDGE_MAX_SESSIONS || 8);
const APP = "tnwks-messages-bridge/0.0.0-spike";
const SERVER = "client";
const PREFIX = `mcp__${SERVER}__`;
// Request fields that the engine sets itself; the bridge can not carry them.
const NOT_CARRIED = [
  "temperature",
  "top_p",
  "top_k",
  "stop_sequences",
  "tool_choice",
  "metadata",
  "service_tier",
  "container",
  "mcp_servers",
];

const sessions = new Set();
const byToolUse = new Map(); // tool_use id -> session
const byHistory = new Map(); // hash of the conversation as the client has it -> session

function log(event, fields = {}) {
  process.stdout.write(`${JSON.stringify({ ts: new Date().toISOString(), event, ...fields })}\n`);
}

function sha(value) {
  return createHash("sha256").update(typeof value === "string" ? value : JSON.stringify(value)).digest("hex").slice(0, 16);
}

// A tool_use id as a key. A client can rewrite ids: OpenClaw keeps only
// [A-Za-z0-9] of an id for a provider that is not the Anthropic API itself.
function idKey(id) {
  return String(id || "").replace(/[^A-Za-z0-9]/g, "");
}

const CODES = { text: "t", tool_use: "u", tool_result: "r", image: "i", thinking: "k", redacted_thinking: "k", document: "d" };

function blocksOf(message) {
  if (!message) return [];
  return typeof message.content === "string"
    ? [{ type: "text", text: message.content }]
    : message.content || [];
}

// A conversation in a form that survives a client's round trip: the text,
// the tool_use ids and the tool_result ids, in order. Thinking and other
// blocks are left out, since a client may drop them.
function shape(messages) {
  return messages
    .map((m) => [
      m.role,
      blocksOf(m)
        .map((b) =>
          b.type === "text"
            ? ["t", (b.text || "").trim()]
            : b.type === "tool_use"
              ? ["u", idKey(b.id)]
              : b.type === "tool_result"
                ? ["r", idKey(b.tool_use_id)]
                : null,
        )
        .filter(Boolean),
    ])
    .filter(([, bs]) => bs.length);
}

// A queue that the SDK reads as its streaming input.
class Inbox {
  constructor() {
    this.items = [];
    this.waiters = [];
    this.closed = false;
  }
  push(item) {
    const waiter = this.waiters.shift();
    if (waiter) waiter({ value: item, done: false });
    else this.items.push(item);
  }
  close() {
    this.closed = true;
    for (const waiter of this.waiters.splice(0)) waiter({ value: undefined, done: true });
  }
  [Symbol.asyncIterator]() {
    return {
      next: () => {
        if (this.items.length) return Promise.resolve({ value: this.items.shift(), done: false });
        if (this.closed) return Promise.resolve({ value: undefined, done: true });
        return new Promise((resolve) => this.waiters.push(resolve));
      },
    };
  }
}

// The index of the last assistant message, or -1.
function lastAssistant(messages) {
  for (let i = messages.length - 1; i >= 0; i--) if (messages[i].role === "assistant") return i;
  return -1;
}

// The blocks of the user messages after the last assistant message: the new
// input of a request. A client can send more than one (OpenClaw sends its
// runtime context as a user message of its own).
function newInput(messages) {
  return messages.slice(lastAssistant(messages) + 1).flatMap(blocksOf);
}

function userMessage(content) {
  return { type: "user", message: { role: "user", content }, parent_tool_use_id: null };
}

// A tool_result block of the client, as an MCP tool result.
function toMcpResult(block) {
  const content = [];
  const parts = typeof block.content === "string" ? [{ type: "text", text: block.content }] : block.content || [];
  for (const part of parts) {
    if (part.type === "text") content.push({ type: "text", text: part.text });
    else if (part.type === "image" && part.source?.type === "base64")
      content.push({ type: "image", data: part.source.data, mimeType: part.source.media_type });
    else content.push({ type: "text", text: JSON.stringify(part) });
  }
  if (!content.length) content.push({ type: "text", text: "" });
  return { content, isError: Boolean(block.is_error) };
}

// The history of a conversation as text, for a session that starts late.
function transcript(messages) {
  const lines = [];
  for (const m of messages) {
    for (const b of blocksOf(m)) {
      if (b.type === "text") lines.push(`[${m.role}] ${b.text}`);
      else if (b.type === "tool_use") lines.push(`[${m.role} tool_use ${b.name} id=${b.id}] ${JSON.stringify(b.input)}`);
      else if (b.type === "tool_result")
        lines.push(`[${m.role} tool_result id=${b.tool_use_id}${b.is_error ? " error" : ""}] ${toMcpResult(b).content.map((c) => c.text ?? `[${c.type}]`).join("\n")}`);
      else lines.push(`[${m.role} ${b.type} block not carried]`);
    }
  }
  return lines.join("\n");
}

// Where the events of one model call go: an SSE stream, or a JSON body.
class Sink {
  constructor(res, stream, session, mode) {
    this.res = res;
    this.timer = setTimeout(() => {
      log("call_timeout", { session: session.id, mode, current: Boolean(session.current), unanswered: session.unanswered() });
      this.fail(504, "bridge: the model call did not end in time");
      if (session.sink === this) session.sink = null;
    }, CALL_MS);
    this.stream = stream;
    this.message = null;
    this.json = new Map();
    this.done = false;
    const headers = { "x-bridge-session": session.id, "x-bridge-mode": mode };
    if (stream) {
      res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive", ...headers });
    } else {
      this.headers = headers;
    }
  }
  event(ev) {
    if (this.done) return;
    if (this.stream) this.res.write(`event: ${ev.type}\ndata: ${JSON.stringify(ev)}\n\n`);
  }
  finish(message) {
    if (this.done) return;
    this.done = true;
    clearTimeout(this.timer);
    if (this.stream) this.res.end();
    else {
      this.res.writeHead(200, { "content-type": "application/json", ...this.headers });
      this.res.end(JSON.stringify(message));
    }
  }
  fail(status, text) {
    if (this.done) return;
    this.done = true;
    clearTimeout(this.timer);
    const body = { type: "error", error: { type: "api_error", message: text } };
    if (this.stream && this.res.headersSent) {
      this.res.write(`event: error\ndata: ${JSON.stringify(body)}\n\n`);
      this.res.end();
    } else {
      this.res.writeHead(status, { "content-type": "application/json" });
      this.res.end(JSON.stringify(body));
    }
  }
}

class Session {
  constructor(body) {
    this.id = randomUUID();
    this.model = body.model;
    this.tools = body.tools || [];
    this.toolNames = new Set(this.tools.map((t) => t.name));
    this.inbox = new Inbox();
    // User messages given to the engine, and turns it ended. A result belongs
    // to the oldest open turn, so it can arrive after the next request.
    this.pushed = 0;
    this.ended = 0;
    this.sink = null;
    // Each tool call of the engine, by tool_use id: {name, resolve, result}.
    // The engine sends the id in the MCP call (`_meta["claudecode/toolUseId"]`),
    // and may call the handler before the model call has ended.
    this.calls = new Map();
    this.current = null; // the message that the engine streams now
    this.touched = Date.now();
    this.closed = false;
    this.cwd = mkdtempSync(join(tmpdir(), "bridge-"));
    sessions.add(this);

    const mcp = new McpServer({ name: SERVER, version: "0.0.0" }, { capabilities: { tools: { listChanged: false } } });
    mcp.server.setRequestHandler(ListToolsRequestSchema, async () => ({
      tools: this.tools.map((t) => ({
        name: t.name,
        description: t.description || "",
        inputSchema: t.input_schema || { type: "object" },
        // Offer each tool up front, not behind the engine's tool search.
        _meta: { "anthropic/alwaysLoad": true },
      })),
    }));
    mcp.server.setRequestHandler(CallToolRequestSchema, async (request) =>
      this.toolCall(request.params.name, request.params.arguments || {}, request.params._meta),
    );

    const system = Array.isArray(body.system)
      ? body.system.filter((b) => b.type === "text").map((b) => b.text)
      : body.system || "";
    const thinking =
      body.thinking?.type === "enabled"
        ? { type: "enabled", budgetTokens: body.thinking.budget_tokens }
        : body.thinking?.type === "adaptive"
          ? { type: "adaptive" }
          : { type: "disabled" };
    const env = {
      ...process.env,
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
      CLAUDE_AGENT_SDK_CLIENT_APP: APP,
    };
    if (body.max_tokens) env.CLAUDE_CODE_MAX_OUTPUT_TOKENS = String(body.max_tokens);
    this.options = {
      model: body.model,
      systemPrompt: system,
      tools: [],
      mcpServers: { [SERVER]: { type: "sdk", name: SERVER, instance: mcp } },
      strictMcpConfig: true,
      allowedTools: [`mcp__${SERVER}`],
      permissionMode: "dontAsk",
      settingSources: [],
      skills: [],
      persistSession: false,
      includePartialMessages: true,
      verbatimPrompts: true,
      thinking,
      cwd: this.cwd,
      env,
      stderr: (data) => log("sdk_stderr", { session: this.id, data: String(data).slice(0, 2000) }),
    };
    this.q = query({ prompt: this.inbox, options: this.options });
    log("session_start", {
      session: this.id,
      model: body.model,
      system_sha: sha(system),
      system_chars: JSON.stringify(system).length,
      tools: this.tools.map((t) => t.name),
      thinking,
      max_tokens: body.max_tokens,
    });
    this.pump();
  }

  async pump() {
    try {
      for await (const msg of this.q) {
        this.touched = Date.now();
        if (msg.type !== "stream_event") log("sdk_message", { session: this.id, type: msg.type, subtype: msg.subtype });
        if (msg.type === "stream_event") {
          if (!msg.parent_tool_use_id) this.onEvent(msg.event);
        } else if (msg.type === "system" && msg.subtype === "init") {
          log("sdk_init", {
            session: this.id,
            claude_code_version: msg.claude_code_version,
            model: msg.model,
            tools: msg.tools,
            mcp_servers: msg.mcp_servers,
            permission_mode: msg.permissionMode,
            api_key_source: msg.apiKeySource,
            skills: msg.skills,
            plugins: msg.plugins,
            slash_commands: (msg.slash_commands || []).length,
          });
        } else if (msg.type === "result") {
          log("sdk_result", { session: this.id, subtype: msg.subtype, is_error: msg.is_error, num_turns: msg.num_turns, error: msg.is_error ? String(msg.result || msg.errors || "").slice(0, 500) : undefined });
          this.ended += 1;
          if (this.sink && !this.current && this.ended >= this.pushed && msg.is_error)
            this.sink.fail(502, `the engine ended the turn without a model call (${msg.subtype})`);
        } else if (msg.type === "assistant" && msg.error) {
          log("sdk_assistant_error", { session: this.id, error: msg.error });
        }
      }
    } catch (error) {
      log("sdk_error", { session: this.id, error: String(error).slice(0, 1000) });
      this.sink?.fail(502, `bridge: ${String(error).slice(0, 300)}`);
    } finally {
      this.close("the SDK stream ended");
    }
  }

  // One event of the engine's model call: to the client, with its own tool
  // names, and into the copy of the message.
  onEvent(ev) {
    const out = structuredClone(ev);
    if (ev.type === "message_start") {
      this.current = structuredClone(ev.message);
      this.current.content = [];
      this.jsonParts = new Map();
    } else if (ev.type === "content_block_start") {
      const block = out.content_block;
      if (block.type === "tool_use" && block.name.startsWith(PREFIX)) block.name = block.name.slice(PREFIX.length);
      this.current.content[ev.index] = structuredClone(block);
      if (block.type === "tool_use") {
        this.jsonParts.set(ev.index, "");
        this.call(block.id, block.name);
        byToolUse.set(idKey(block.id), this);
      }
    } else if (ev.type === "content_block_delta") {
      const block = this.current.content[ev.index];
      const d = ev.delta;
      if (d.type === "text_delta") block.text = (block.text || "") + d.text;
      else if (d.type === "thinking_delta") block.thinking = (block.thinking || "") + d.thinking;
      else if (d.type === "signature_delta") block.signature = (block.signature || "") + d.signature;
      else if (d.type === "input_json_delta") this.jsonParts.set(ev.index, this.jsonParts.get(ev.index) + d.partial_json);
      else if (d.type === "citations_delta") (block.citations ||= []).push(d.citation);
    } else if (ev.type === "content_block_stop") {
      const block = this.current.content[ev.index];
      if (block?.type === "tool_use") {
        const raw = this.jsonParts.get(ev.index);
        try {
          block.input = raw ? JSON.parse(raw) : block.input || {};
        } catch {
          block.input = { _unparsed: raw };
        }
      }
    } else if (ev.type === "message_delta") {
      Object.assign(this.current, ev.delta);
      this.current.usage = { ...this.current.usage, ...ev.usage };
    }
    this.sink?.event(out);
    if (ev.type === "message_stop") this.endCall();
  }

  // The model call is over: answer the client, and remember what it got.
  endCall() {
    const message = this.current;
    this.current = null;
    const uses = message.content.filter((b) => b?.type === "tool_use");
    byHistory.set(sha([...this.history, ...shape([{ role: "assistant", content: message.content }])]), this);
    log("model_call_end", {
      session: this.id,
      stop_reason: message.stop_reason,
      blocks: message.content.map((b) => b?.type),
      tool_uses: uses.map((u) => ({ id: u.id, name: u.name })),
      to_client: Boolean(this.sink),
    });
    if (!this.sink) log("orphan_model_call", { session: this.id, note: "the engine called the model with no client request waiting" });
    this.sink?.finish(message);
    this.sink = null;
  }

  call(id, name) {
    const key = idKey(id);
    if (!this.calls.has(key)) this.calls.set(key, { name, resolve: null, result: null, handled: false });
    return this.calls.get(key);
  }

  push(message) {
    this.pushed += 1;
    this.inbox.push(message);
  }

  // The engine calls a client tool: wait for the client's result.
  toolCall(name, args, meta) {
    let id = meta?.["claudecode/toolUseId"];
    if (!id) {
      // No id in the call: the oldest call of that tool with no handler yet.
      id = [...this.calls].find(([, c]) => c.name === name && !c.handled)?.[0];
    }
    log("tool_call", { session: this.id, name, id: id || null, meta_keys: Object.keys(meta || {}) });
    if (!id) return Promise.resolve({ content: [{ type: "text", text: "bridge: no matching tool_use" }], isError: true });
    const call = this.call(id, name);
    call.handled = true;
    if (call.result) {
      this.calls.delete(idKey(id));
      byToolUse.delete(idKey(id));
      return Promise.resolve(call.result);
    }
    return new Promise((resolve) => {
      call.resolve = resolve;
    });
  }

  // A tool_result of the client: to its waiting handler, or kept for it.
  deliver(block) {
    const id = idKey(block.tool_use_id);
    const call = this.calls.get(id);
    const result = toMcpResult(block);
    if (!call) return log("unknown_tool_result", { session: this.id, id: block.tool_use_id });
    if (call.resolve) {
      this.calls.delete(id);
      byToolUse.delete(id);
      call.resolve(result);
    } else call.result = result;
  }

  // Tool calls that wait for a client result.
  unanswered() {
    return [...this.calls.values()].filter((c) => !c.result).length;
  }

  // The client's next request on this session.
  continueWith(body, res, stream) {
    this.history = shape(body.messages);
    const input = newInput(body.messages);
    const results = input.filter((b) => b.type === "tool_result");
    const other = input.filter((b) => b.type !== "tool_result");
    this.sink = new Sink(res, stream, this, results.length ? "tool-result" : "continue");
    this.touched = Date.now();
    if (results.length) {
      for (const block of results) this.deliver(block);
      // Text next to the results goes in as the next user message.
      if (other.length) {
        log("text_with_tool_results", { session: this.id, blocks: other.map((b) => b.type) });
        this.push(userMessage(other));
      }
    } else {
      this.push(userMessage(input));
    }
  }

  start(body, res, stream, rebuilt) {
    this.history = shape(body.messages);
    this.sink = new Sink(res, stream, this, rebuilt ? "rebuilt" : "new");
    const input = newInput(body.messages);
    if (!rebuilt) {
      this.push(userMessage(input));
      return;
    }
    const cut = lastAssistant(body.messages) + 1;
    const results = input.filter((b) => b.type === "tool_result");
    const carried = input.filter((b) => b.type === "text" || b.type === "image" || b.type === "document");
    const content = [
      {
        type: "text",
        text:
          "The conversation so far, as the client sent it (the bridge had no live session for it):\n" +
          transcript([...body.messages.slice(0, cut), ...(results.length ? [{ role: "user", content: results }] : [])]),
      },
      ...carried,
    ];
    this.push(userMessage(content));
  }

  close(reason) {
    if (this.closed) return;
    this.closed = true;
    sessions.delete(this);
    for (const [id, s] of byToolUse) if (s === this) byToolUse.delete(id);
    for (const [key, s] of byHistory) if (s === this) byHistory.delete(key);
    this.inbox.close();
    try {
      this.q.close?.();
    } catch {}
    rmSync(this.cwd, { recursive: true, force: true });
    for (const call of this.calls.values())
      call.resolve?.({ content: [{ type: "text", text: "bridge: the session closed" }], isError: true });
    this.sink?.fail(502, `bridge: the session closed (${reason})`);
    log("session_end", { session: this.id, reason });
  }
}

// The live session of a request, if any.
function liveSession(body) {
  const at = lastAssistant(body.messages);
  if (at < 0) return null;
  const ids = blocksOf(body.messages[at]).filter((b) => b.type === "tool_use").map((b) => b.id);
  const session = (ids.length && byToolUse.get(idKey(ids[0]))) || byHistory.get(sha(shape(body.messages.slice(0, at + 1))));
  if (!session || session.closed) return null;
  const answered = newInput(body.messages).some((b) => b.type === "tool_result");
  if (session.unanswered() && !answered) {
    // The engine waits for tool results that the client did not send.
    session.close("the client did not answer the tool calls");
    return null;
  }
  const names = new Set((body.tools || []).map((t) => t.name));
  if (names.size !== session.toolNames.size || [...names].some((n) => !session.toolNames.has(n))) {
    log("tools_changed", { session: session.id });
    session.close("the client changed its tools");
    return null;
  }
  return session;
}

async function handleMessages(req, res) {
  let body;
  try {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    res.writeHead(400, { "content-type": "application/json" });
    return res.end(JSON.stringify({ type: "error", error: { type: "invalid_request_error", message: "bad JSON" } }));
  }
  if (!Array.isArray(body.messages) || !body.messages.length || body.messages.at(-1).role !== "user") {
    res.writeHead(400, { "content-type": "application/json" });
    return res.end(JSON.stringify({ type: "error", error: { type: "invalid_request_error", message: "the last message must be a user message" } }));
  }
  const stream = Boolean(body.stream);
  const dropped = NOT_CARRIED.filter((k) => body[k] !== undefined);
  const cacheControl = JSON.stringify(body).split('"cache_control"').length - 1;
  let session = liveSession(body);
  let mode = "continue";
  if (session && session.sink) {
    res.writeHead(409, { "content-type": "application/json" });
    return res.end(JSON.stringify({ type: "error", error: { type: "invalid_request_error", message: "a request for this conversation is in progress" } }));
  }
  if (session) {
    session.continueWith(body, res, stream);
  } else {
    if (sessions.size >= MAX_SESSIONS) {
      const idle = [...sessions].filter((s) => !s.sink).sort((a, b) => a.touched - b.touched);
      const victim = idle.find((s) => !s.unanswered()) || idle[0];
      if (!victim) {
        res.writeHead(529, { "content-type": "application/json" });
        return res.end(JSON.stringify({ type: "error", error: { type: "overloaded_error", message: "bridge: too many live conversations" } }));
      }
      victim.close("evicted for a new conversation");
    }
    session = new Session(body);
    mode = lastAssistant(body.messages) >= 0 ? "rebuilt" : "new";
    session.start(body, res, stream, mode === "rebuilt");
  }
  log("request", {
    session: session.id,
    mode,
    stream,
    model: body.model,
    messages: body.messages.length,
    shape: body.messages.map((m) => `${m.role[0]}:${blocksOf(m).map((b) => CODES[b.type] || "?").join("")}`).join(" "),
    tool_ids: (() => {
      const at = lastAssistant(body.messages);
      return at < 0 ? [] : blocksOf(body.messages[at]).filter((b) => b.type === "tool_use").map((b) => b.id);
    })(),
    new_input: newInput(body.messages).map((b) => b.type),
    tools: (body.tools || []).map((t) => t.name),
    system_sha: sha(Array.isArray(body.system) ? body.system.filter((b) => b.type === "text").map((b) => b.text) : body.system || ""),
    dropped,
    cache_control_marks: cacheControl,
    user_agent: (req.headers["user-agent"] || "").split(" ")[0],
  });
  res.on("close", () => {
    if (session.sink && session.sink.res === res && !session.sink.done) {
      log("client_gone", { session: session.id });
      session.sink.done = true;
      session.sink = null;
    }
  });
}

const server = http.createServer((req, res) => {
  const path = (req.url || "/").split("?")[0];
  if (req.method === "GET" && path === "/health") {
    res.writeHead(200, { "content-type": "application/json" });
    return res.end(JSON.stringify({ ok: true, sessions: sessions.size, waiting_tool_calls: [...sessions].reduce((n, s) => n + s.unanswered(), 0), app: APP }));
  }
  if (req.method === "POST" && path.endsWith("/v1/messages")) {
    handleMessages(req, res).catch((error) => {
      log("handler_error", { error: String(error).slice(0, 1000) });
      if (!res.headersSent) res.writeHead(500, { "content-type": "application/json" });
      res.end(JSON.stringify({ type: "error", error: { type: "api_error", message: "bridge error" } }));
    });
    return;
  }
  log("not_found", { method: req.method, path });
  res.writeHead(404, { "content-type": "application/json" });
  res.end(JSON.stringify({ type: "error", error: { type: "not_found_error", message: `the bridge serves POST /v1/messages and GET /health only` } }));
});

setInterval(() => {
  const now = Date.now();
  for (const s of sessions) if (!s.sink && now - s.touched > IDLE_MS) s.close("idle");
}, 30_000).unref();

for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => {
    for (const s of [...sessions]) s.close(signal);
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 3000).unref();
  });
}

server.listen(PORT, () => log("listening", { port: PORT, app: APP }));
