// Spike: the tool `sandbox_exec` runs one shell command in the sandbox of the
// session (the OpenShell backend of OpenClaw), never on the gateway host.
// Test layer only: the image that ships does not hold it.
//
// It uses the public plugin SDK only:
// - `definePluginEntry` and `api.registerTool` (openclaw/plugin-sdk/plugin-entry),
// - `resolveSandboxContext` (openclaw/plugin-sdk/agent-harness-runtime), which
//   gives the SandboxContext of the session, with its backend handle. The
//   handle's `buildExecSpec` syncs the workspace into the sandbox (mirror
//   mode) and gives the argv of the transport; `finalizeExec` syncs it back.
//   The core exec tool uses the same two calls.
// If the session has no sandbox, the tool refuses: it never falls back to the
// host. It holds no credential and opens no connection of its own.

import { spawn } from "node:child_process";
import { resolveSandboxContext } from "openclaw/plugin-sdk/agent-harness-runtime";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const MAX_OUTPUT = 64 * 1024;
const DEFAULT_TIMEOUT_SECONDS = 120;
const MAX_TIMEOUT_SECONDS = 600;
const SANDBOX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";

function text(value, details) {
  return { content: [{ type: "text", text: value }], details };
}

// Run the transport argv of the backend; return its exit code and output.
function runSpec(spec, timeoutSeconds, signal) {
  return new Promise((resolve) => {
    const child = spawn(spec.argv[0], spec.argv.slice(1), {
      cwd: spec.cwd,
      env: spec.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    child.stdin.end();
    let output = "";
    const append = (chunk) => {
      if (output.length < MAX_OUTPUT) {
        output += chunk.toString("utf8").slice(0, MAX_OUTPUT - output.length);
      }
    };
    child.stdout.on("data", append);
    child.stderr.on("data", append);
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, timeoutSeconds * 1000);
    const abort = () => child.kill("SIGKILL");
    signal?.addEventListener("abort", abort, { once: true });
    const done = (exitCode, error) => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      resolve({ exitCode, timedOut, output: error ? `${output}\n${error}` : output });
    };
    child.on("error", (error) => done(null, String(error)));
    child.on("close", (code) => done(code, undefined));
  });
}

export default definePluginEntry({
  id: "tnwks-sandbox-exec",
  name: "Sandbox exec (spike)",
  description: "Run a shell command in the sandbox of the session, never on the gateway host.",
  register(api) {
    api.registerTool(
      (ctx) => ({
        name: "sandbox_exec",
        label: "Sandbox exec",
        description:
          "Run a shell command in the sandbox of this session, in its workspace. " +
          "It never runs on the gateway host; without a sandbox it refuses.",
        parameters: {
          type: "object",
          additionalProperties: false,
          properties: {
            command: { type: "string", description: "The shell command (sh -c)." },
            timeoutSeconds: {
              type: "number",
              description: `Kill the command after this many seconds (default ${DEFAULT_TIMEOUT_SECONDS}, at most ${MAX_TIMEOUT_SECONDS}).`,
            },
          },
          required: ["command"],
        },
        async execute(_toolCallId, params, signal) {
          const command = String(params?.command ?? "");
          const timeoutSeconds = Math.min(
            Math.max(Number(params?.timeoutSeconds) || DEFAULT_TIMEOUT_SECONDS, 1),
            MAX_TIMEOUT_SECONDS,
          );
          const config = ctx.getRuntimeConfig?.() ?? ctx.runtimeConfig ?? ctx.config;
          const sandbox = await resolveSandboxContext({
            config,
            agentId: ctx.agentId,
            sessionKey: ctx.sessionKey,
            workspaceDir: ctx.workspaceDir,
            assertCurrent: ctx.assertInvocationCurrent,
          });
          if (!sandbox?.enabled || !sandbox.backend?.buildExecSpec) {
            sandbox?.[Symbol.dispose]?.();
            return text("sandbox_exec refused: this session has no sandbox.", {
              refused: true,
            });
          }
          try {
            const backend = sandbox.backend;
            const spec = await backend.buildExecSpec({
              command,
              workdir: sandbox.containerWorkdir,
              env: { PATH: SANDBOX_PATH },
              usePty: false,
            });
            let result = { exitCode: null, timedOut: false, output: "" };
            try {
              spec.assertCurrent?.();
              result = await runSpec(spec, timeoutSeconds, signal);
            } finally {
              await backend.finalizeExec?.({
                status: result.exitCode === 0 ? "completed" : "failed",
                exitCode: result.exitCode,
                timedOut: result.timedOut,
                token: spec.finalizeToken,
              });
            }
            const head = result.timedOut
              ? `timed out after ${timeoutSeconds}s`
              : `exit ${result.exitCode}`;
            const via = String(spec.argv[0]).split("/").pop();
            return text(`${head} (sandbox ${backend.runtimeId}, via ${via})\n${result.output}`, {
              exitCode: result.exitCode,
              timedOut: result.timedOut,
              sandbox: backend.runtimeId,
            });
          } finally {
            sandbox[Symbol.dispose]?.();
          }
        },
      }),
      { name: "sandbox_exec" },
    );
  },
});
