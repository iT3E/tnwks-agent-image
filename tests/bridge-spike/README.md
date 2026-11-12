# The bridge spike

Test code only. It is not part of the image, and it is not a product.

`bridge.mjs` is a small HTTP service that serves the Anthropic Messages API
(`POST /v1/messages`, JSON and SSE) and `GET /health`. It runs each
conversation through the unmodified Claude Agent SDK
(`@anthropic-ai/claude-agent-sdk`, pinned in `package.json` and
`package-lock.json`), so the Claude Code engine makes the upstream model
call. The CI test is `tests/test_bridge_spike.py`, run by
`.github/workflows/bridge-spike.yaml`.

## Run it on its own

You need Node 22 or later. The SDK must be able to reach its upstream with its
own sign-in. The bridge sets no upstream and holds no credential.

```sh
cd tests/bridge-spike
npm ci --omit=dev
PORT=8080 node bridge.mjs
```

`PORT` is optional (default 8080). `HOST` is the address to listen on
(default `127.0.0.1`). The bridge has no auth, so keep it on loopback; the
CI pod sets `HOST=0.0.0.0`. Nothing else is required. A client then
uses `http://127.0.0.1:8080` as its Anthropic base URL, with any key: the
bridge does not check it and never forwards it. Optional limits:
`BRIDGE_IDLE_SECONDS` (default 900) closes a conversation idle that long,
`BRIDGE_MAX_SESSIONS` (default 8) is the most live conversations (each one
is a Claude Code process), and `BRIDGE_CALL_SECONDS` (default 300) fails a
model call that does not end in that time.

In CI, the SDK gets `ANTHROPIC_BASE_URL` (the mock model) and a fake
`ANTHROPIC_API_KEY` from the pod environment.

## The mapping

- **System prompt.** The client's `system` becomes the SDK's `systemPrompt`
  as one string: a string stays a string, and text blocks are joined with a
  blank line (their `cache_control` is not carried). This is the SDK's custom prompt, not the
  `claude_code` preset. The CI job records what the engine sends around it.
- **Cache marks.** The bridge removes `cache_control` from each content block
  of the client's messages before they go to the engine. The engine places
  its own marks (3 in CI), and the API takes at most 4 blocks with
  `cache_control`, so client marks on top of the engine's would make the
  API refuse the request.
- **Tools.** The client's `tools` are offered through one in-process MCP
  server named `client`. Its `tools/list` returns the client's JSON Schema
  as written, with `_meta["anthropic/alwaysLoad"]`, so that the engine does
  not defer a tool behind its tool search. The engine names a tool
  `mcp__client__<name>`. The bridge gives the client back its own name.
- **Tool calls.** The bridge runs no tool itself. When a model call ends with
  `stop_reason: "tool_use"`, the bridge ends the HTTP response with those
  blocks. The SDK session stays open, and the MCP handler of each call waits.
  The engine names the tool_use id of each MCP call in
  `_meta["claudecode/toolUseId"]` (seen in CI, not documented), and it can
  call a handler before the model call has ended. The client's next request
  brings `tool_result` blocks, and the bridge resolves each handler by that
  id. A result that comes before its handler call is kept until that call.
- **Off.** Claude Code's built-in tools are off (`tools: []`), with
  `settingSources: []`, `skills: []`, no hooks, `strictMcpConfig`,
  `permissionMode: "dontAsk"` with only the `client` server allowed,
  `persistSession: false`, and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`.
  `verbatimPrompts: true` turns off the engine's turn-start attachments
  (file mentions, slash commands, reminders) on a prompt.
- **Other fields.** `model` goes to the SDK's `model`, and `max_tokens` to
  `CLAUDE_CODE_MAX_OUTPUT_TOKENS` of the session. `thinking` is mapped to the
  SDK's `thinking` (disabled when the client does not ask). `temperature`,
  `top_p`, `top_k`, `stop_sequences`, `tool_choice` and `metadata` are not
  carried; the bridge logs them as dropped.
- **New input.** The new input of a request is every user message after the
  last assistant message. OpenClaw, for example, sends its runtime context as
  a user message of its own. Tool results go to their handlers. Any other
  block goes in as the next user message.
- **Sessions.** There is one SDK session (one Claude Code process) for each
  conversation. The bridge matches a request to its session by the tool_use
  ids of the last assistant message. If those do not match, it uses a hash of
  the conversation as the client has it (its text, tool_use ids and
  tool_result ids).
- **A request with history but no live session** (a restart, another
  replica, or tools that changed) starts a new session. Its first prompt
  holds the history as text, after one line that says so. The structured
  tool calls, thinking, images of earlier turns and the engine's own context
  are lost.
- **Identity.** The bridge sets `CLAUDE_AGENT_SDK_CLIENT_APP`, the SDK's
  documented way to name the app that uses it. It adds nothing to look like
  another client, and removes nothing from the client's prompt.
