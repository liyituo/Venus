import assert from "node:assert/strict";
import test from "node:test";
import { handleRequest } from "../src/protocol.mjs";

function fakeBackend() {
  const calls = [];
  return {
    calls,
    getDashboard: async (reportId) => { calls.push(["dashboard", reportId]); return { schema_version: "dashboard.v1", paper_only: true }; },
    generateDailyPlan: async (args) => { calls.push(["generate", args]); return { report_id: "rpt_test" }; },
    getReport: async (reportId) => { calls.push(["report", reportId]); return { report_id: reportId }; },
    submitApproval: async (args) => { calls.push(["approval", args]); return { approval_id: "appr_test", report_id: args.report_id }; },
    rejectPlan: async (args) => { calls.push(["reject", args]); return { report_id: args.report_id, status: "REJECTED" }; },
    executePaperPlan: async (args) => { calls.push(["execute", args]); return { report_id: args.report_id, mode: "paper", status: "FILLED" }; },
    setKillSwitch: async (args) => { calls.push(["kill", args]); return { enabled: args.enabled }; },
    getExecution: async (id) => { calls.push(["execution", id]); return { execution_id: id }; },
    getAuditEvents: async (args) => { calls.push(["audit", args]); return []; },
    getChartData: async (args) => { calls.push(["chart", args]); return { schema_version: "chart-data.v2" }; },
    listStrategies: async () => { calls.push(["strategies"]); return { strategies: [] }; },
    getStrategy: async (id, version) => { calls.push(["strategy", id, version]); return {}; },
    validateStrategy: async (args) => { calls.push(["validate", args]); return { validation: { valid: true } }; },
    saveStrategyDraft: async (args) => { calls.push(["save-draft", args]); return {}; },
    runStrategyDebug: async (args) => { calls.push(["debug", args]); return { run_id: "dbg_test" }; },
    getDebugTrace: async (id, start, limit) => { calls.push(["trace", id, start, limit]); return { run_id: id, trace: [] }; },
    runBacktest: async (args) => { calls.push(["backtest", args]); return { run_id: "bt_test" }; },
    getBacktestResult: async (id) => { calls.push(["backtest-result", id]); return { run_id: id }; },
    compareBacktests: async (ids) => { calls.push(["compare", ids]); return { runs: [] }; },
    promoteStrategyCandidate: async (args) => { calls.push(["promote", args]); return {}; },
    enablePaperStrategy: async (args) => { calls.push(["enable", args]); return {}; },
  };
}

test("MCP initialize advertises tools and resources", async () => {
  const backend = fakeBackend();
  const initialized = await handleRequest({ jsonrpc: "2.0", id: 1, method: "initialize" }, { backend, uiHtml: "<html>dashboard</html>" });
  assert.equal(initialized.result.serverInfo.name, "quant-agent-dashboard");
  assert.equal(initialized.result.capabilities.tools.listChanged, false);

  const tools = await handleRequest({ jsonrpc: "2.0", id: 2, method: "tools/list" }, { backend });
  assert.equal(tools.result.tools.length, 21);
  const execute = tools.result.tools.find((tool) => tool.name === "quant_execute_paper_plan");
  assert.deepEqual(execute.inputSchema.required, ["report_id", "request_id"]);
  assert.equal(execute._meta.ui.resourceUri, "ui://quant-agent-dashboard/dashboard.html");

  const resources = await handleRequest({ jsonrpc: "2.0", id: 3, method: "resources/list" }, { backend });
  assert.equal(resources.result.resources[0].mimeType, "text/html;profile=mcp-app");
  const resource = await handleRequest({ jsonrpc: "2.0", id: 4, method: "resources/read", params: { uri: resources.result.resources[0].uri } }, { backend, uiHtml: "<html>dashboard</html>" });
  assert.equal(resource.result.contents[0].text, "<html>dashboard</html>");
});

test("mutating MCP calls require request IDs and delegate to backend", async () => {
  const backend = fakeBackend();
  const missing = await handleRequest({ jsonrpc: "2.0", id: 10, method: "tools/call", params: { name: "quant_execute_paper_plan", arguments: { report_id: "rpt" } } }, { backend });
  assert.equal(missing.result.isError, true);
  assert.equal(missing.result.structuredContent.error.code, "INVALID_TOOL_INPUT");

  const approval = await handleRequest({ jsonrpc: "2.0", id: 11, method: "tools/call", params: { name: "quant_submit_approval", arguments: { report_id: "rpt", scope: "ALL", approver: "user", request_id: "req-1" } } }, { backend });
  assert.equal(approval.result.isError, false);
  assert.deepEqual(backend.calls[0], ["approval", { report_id: "rpt", scope: "ALL", approver: "user", request_id: "req-1", order_ids: [] }]);

  const execution = await handleRequest({ jsonrpc: "2.0", id: 12, method: "tools/call", params: { name: "quant_execute_paper_plan", arguments: { report_id: "rpt", request_id: "req-2" } } }, { backend });
  assert.equal(execution.result.structuredContent.mode, "paper");
  assert.equal(backend.calls[1][0], "execute");
});
