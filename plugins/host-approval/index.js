// The host approval plugin: one hook, `before_tool_call`, that asks the
// approver before each call of the host server that is not a listed read.
// The logic is in lib.js. This file connects it to the product.
import { lookup as dnsLookup } from "node:dns/promises";
import {
  MCP_SERVER_NAME, PLUGIN_ID, checkProductConfig, checkServerRoutes, parseConfig, resolveConnection, safeDecide,
} from "./lib.js";

// The waits between two route checks while a name does not resolve.
const RETRY_MS = [2000, 5000, 10000, 30000];

const configSchema = {
  safeParse(value) {
    const parsed = parseConfig(value);
    if (parsed.ok) return { success: true, data: value };
    return {
      success: false,
      error: { issues: parsed.errors.map((message) => ({ path: [], message })) },
    };
  },
  jsonSchema: {
    type: "object",
    additionalProperties: false,
    required: ["serverPrefixes", "readProfile", "approverId", "approvalChannel", "mcpUrl", "mcpKeyEnv"],
    properties: {
      serverPrefixes: { type: "array", items: { type: "string" } },
      knownTools: { type: "array", items: { type: "string" } },
      readProfile: { type: "string" },
      readList: { type: "array", items: { type: "string" } },
      promptTools: { type: "array", items: { type: "string" } },
      approverId: { type: "string" },
      approvalChannel: { type: "string" },
      timeoutMs: { type: "integer", minimum: 1, maximum: 600000 },
      mcpUrl: { type: "string" },
      mcpKeyEnv: { type: "string" },
    },
  },
};

export function makePlugin({ lookup = (host) => dnsLookup(host, { all: true }), retryMs = RETRY_MS } = {}) {
  const plugin = {
    id: PLUGIN_ID,
    name: "Host approval",
    description: "Asks the approver before each host server call that is not a listed read.",
    configSchema,
    register(api) {
      let state;
      const parsed = parseConfig(api.pluginConfig);
      if (!parsed.ok) {
        state = { errors: parsed.errors };
      } else {
        const productErrors = checkProductConfig(api.config, parsed.config, process.env);
        state = productErrors.length ? { errors: productErrors } : { pending: true };
      }
      if (state.errors) api.logger?.error?.(`${PLUGIN_ID}: refusing each tool call: ${state.errors.join("; ")}`);
      // The route check by address. Until it ends, each tool call is
      // refused. A name that does not resolve (DNS not ready at the start
      // of the pod) refuses each call, and the check runs again after a
      // wait, until it passes; then the plugin loads.
      const checkRoutes = async (attempt) => {
        const errors = await checkServerRoutes(api.config?.mcp?.servers, parsed.config, lookup);
        if (errors.length === 0) {
          state = { config: parsed.config };
          api.logger?.info?.(`${PLUGIN_ID}: loaded, ${parsed.config.readList.size} read lines`);
          return;
        }
        state = { errors };
        const resolving = errors.every((e) => e.includes("does not resolve"));
        if (attempt === 0 || !resolving) api.logger?.error?.(`${PLUGIN_ID}: refusing each tool call: ${errors.join("; ")}`);
        if (!resolving) return;
        const wait = retryMs[Math.min(attempt, retryMs.length - 1)];
        await new Promise((r) => setTimeout(r, wait));
        return checkRoutes(attempt + 1);
      };
      plugin.ready = state.pending ? checkRoutes(0).catch((err) => {
        state = { errors: [`the route check failed (${String(err)})`] };
        api.logger?.error?.(`${PLUGIN_ID}: refusing each tool call: ${state.errors.join("; ")}`);
      }) : Promise.resolve();
      // A high priority, so this hook runs before other hooks of the call.
      api.on("before_tool_call", (event, ctx) => {
        const decision = safeDecide(state, event, ctx);
        // The tool and the reason only: no channel, sender, chat ID or
        // session key. The session store of the product records each
        // decision (Design Doc, section 4.2.6, "The record").
        if (decision?.block && typeof event?.toolName === "string") {
          api.logger?.info?.(`${PLUGIN_ID}: refused ${JSON.stringify(event.toolName)} (${decision.blockReason})`);
        }
        return decision;
      }, { priority: 1000 });
      // The host server gets its address and key from this plugin only.
      api.registerMcpServerConnectionResolver({
        serverName: MCP_SERVER_NAME,
        resolve: async (ctx) => resolveConnection(state, process.env, ctx),
      });
    },
  };
  return plugin;
}

export default makePlugin();
