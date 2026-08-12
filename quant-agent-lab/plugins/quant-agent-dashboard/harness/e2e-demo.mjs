import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const pluginRoot = resolve(here, "..");
const port = Number(process.env.E2E_HARNESS_PORT ?? 4183);
const backendPort = Number(process.env.E2E_BACKEND_PORT ?? 8013);
const child = spawn(process.execPath, [resolve(here, "server.mjs")], {
  cwd: pluginRoot,
  env: {
    ...process.env,
    HARNESS_PORT: String(port),
    QUANT_AGENT_BACKEND_PORT: String(backendPort),
    QUANT_AGENT_BACKEND_URL: `http://127.0.0.1:${backendPort}`,
    HARNESS_SCENARIO: "default",
    HARNESS_THEME: "light",
  },
  stdio: ["ignore", "pipe", "pipe"],
});
child.stdout.on("data", (chunk) => process.stdout.write(`[harness] ${chunk}`));
child.stderr.on("data", (chunk) => process.stderr.write(`[harness] ${chunk}`));

async function waitForHarness() {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/healthz`);
      if (response.ok) return;
    } catch {
      await new Promise((resolvePromise) => setTimeout(resolvePromise, 250));
    }
  }
  throw new Error("harness did not start");
}

async function call(name, argumentsValue) {
  const response = await fetch(`http://127.0.0.1:${port}/bridge`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name, arguments: argumentsValue }),
  });
  return response.json();
}

try {
  await waitForHarness();
  const dashboard = await call("quant_get_dashboard", {});
  if (dashboard.isError || dashboard.structuredContent?.paper_only !== true) throw new Error("dashboard precondition failed");
  const reportId = dashboard.structuredContent.report_id;
  const approval = await call("quant_submit_approval", {
    report_id: reportId,
    scope: "ALL",
    approver: "e2e-demo-user",
    request_id: "e2e-demo-approval",
  });
  if (approval.isError) throw new Error(`approval failed: ${approval.structuredContent?.error?.message}`);
  const execution = await call("quant_execute_paper_plan", {
    report_id: reportId,
    request_id: "e2e-demo-execution",
  });
  if (execution.isError) throw new Error(`execution failed: ${execution.structuredContent?.error?.message}`);
  const finalDashboard = await call("quant_get_dashboard", { report_id: reportId });
  const summary = finalDashboard.structuredContent;
  if (summary.execution?.status !== "FILLED" || summary.execution?.mode !== "paper") {
    throw new Error(`unexpected final status: ${summary.execution?.status}`);
  }
  console.log(JSON.stringify({
    report_id: reportId,
    approval_id: summary.approval?.approval_id,
    execution_id: summary.execution?.execution_id,
    execution_status: summary.execution?.status,
    mode: summary.execution?.mode,
    paper_only: summary.paper_only,
    live_broker: summary.live_broker,
  }, null, 2));
} finally {
  child.kill();
}
