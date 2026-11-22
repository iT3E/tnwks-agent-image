// The host approval plugin: one hook, `before_tool_call`, that asks the
// approver before each call of the host server that is not a listed read.
// The logic is in lib.js. This file connects it to the product.
import { MCP_SERVER_NAME, PLUGIN_ID, checkProductConfig, parseConfig, resolveConnection, safeDecide } from "./lib.js";

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

export default {
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
      const productErrors = checkProductConfig(api.config, parsed.config);
      state = productErrors.length ? { errors: productErrors } : { config: parsed.config };
    }
    if (state.errors) {
      api.logger?.error?.(`${PLUGIN_ID}: refusing each tool call: ${state.errors.join("; ")}`);
    } else {
      api.logger?.info?.(`${PLUGIN_ID}: loaded, ${state.config.readList.size} read lines`);
    }
    // A high priority, so this hook runs before other hooks of the call.
    api.on("before_tool_call", (event) => safeDecide(state, event), { priority: 1000 });
    // The host server gets its address and key from this plugin only.
    api.registerMcpServerConnectionResolver({
      serverName: MCP_SERVER_NAME,
      resolve: async () => resolveConnection(state, process.env),
    });
  },
};
export const broken = ;
