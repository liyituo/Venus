import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { callTool, TOOL_DEFINITIONS, UI_RESOURCE_URI } from "./tools.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const defaultUiPath = resolve(here, "../../ui/dist/index.html");

export async function loadUiHtml(uiPath = defaultUiPath) {
  return readFile(uiPath, "utf8");
}

function jsonRpcError(id, code, message) {
  return { jsonrpc: "2.0", id: id ?? null, error: { code, message } };
}

export async function handleRequest(request, { backend, uiHtml } = {}) {
  const id = request?.id ?? null;
  const method = request?.method;
  const params = request?.params ?? {};
  if (typeof method !== "string") return jsonRpcError(id, -32600, "invalid request");

  if (method === "notifications/initialized" || method === "notifications/cancelled") return null;
  if (method === "ping") return { jsonrpc: "2.0", id, result: {} };
  if (method === "initialize") {
    return {
      jsonrpc: "2.0",
      id,
      result: {
        protocolVersion: "2025-06-18",
        capabilities: { tools: { listChanged: false }, resources: { listChanged: false } },
        serverInfo: { name: "quant-agent-dashboard", version: "0.1.0" },
      },
    };
  }
  if (method === "tools/list") {
    return { jsonrpc: "2.0", id, result: { tools: TOOL_DEFINITIONS } };
  }
  if (method === "resources/list") {
    return {
      jsonrpc: "2.0",
      id,
      result: {
        resources: [
          {
            uri: UI_RESOURCE_URI,
            name: "Quant Agent Dashboard",
            description: "Paper Trading dashboard MCP App",
            mimeType: "text/html;profile=mcp-app",
          },
        ],
      },
    };
  }
  if (method === "resources/read") {
    if (params.uri !== UI_RESOURCE_URI) return jsonRpcError(id, -32002, "resource not found");
    const html = uiHtml ?? (await loadUiHtml());
    return {
      jsonrpc: "2.0",
      id,
      result: {
        contents: [
          {
            uri: UI_RESOURCE_URI,
            mimeType: "text/html;profile=mcp-app",
            text: html,
            _meta: { ui: { csp: { connectDomains: [] } } },
          },
        ],
      },
    };
  }
  if (method === "tools/call") {
    if (!backend) return jsonRpcError(id, -32001, "backend client is not configured");
    if (typeof params.name !== "string") return jsonRpcError(id, -32602, "tool name is required");
    return {
      jsonrpc: "2.0",
      id,
      result: await callTool(params.name, params.arguments, backend),
    };
  }
  return jsonRpcError(id, -32601, `method not found: ${method}`);
}
