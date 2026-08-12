import { spawn, spawnSync } from "node:child_process";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { dirname, extname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { BackendClient } from "../mcp-server/src/backend-client.mjs";
import { callTool } from "../mcp-server/src/tools.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const pluginRoot = resolve(here, "..");
const projectRoot = resolve(pluginRoot, "..", "..");
const uiRoot = resolve(pluginRoot, "ui", "dist");
const backendPort = Number(process.env.QUANT_AGENT_BACKEND_PORT ?? 8014);
const port = Number(process.env.HARNESS_PORT ?? 4173);
const backendUrl = process.env.QUANT_AGENT_BACKEND_URL ?? `http://127.0.0.1:${backendPort}`;
const defaultScenario = process.env.HARNESS_SCENARIO ?? "default";
const defaultTheme = process.env.HARNESS_THEME ?? "light";

if (!existsSync(resolve(uiRoot, "index.html"))) {
  console.error("UI build missing. Run `node scripts/build.mjs` before starting the harness.");
  process.exit(2);
}

const backend = new BackendClient(backendUrl);
let backendProcess = null;

function pythonEnvironment() {
  return { ...process.env, PYTHONPATH: "src" };
}

function seedDemo() {
  const result = spawnSync(process.env.PYTHON ?? "python", ["-m", "quant_agent", "seed-demo", "--reset"], {
    cwd: projectRoot,
    env: pythonEnvironment(),
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`seed-demo failed: ${result.stderr || result.stdout}`);
  }
}

async function waitForBackend() {
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline) {
    try {
      const health = await backend.request("/api/v1/health");
      if (health.status === "ok") return;
    } catch {
      await new Promise((resolvePromise) => setTimeout(resolvePromise, 200));
    }
  }
  throw new Error(`backend did not become healthy at ${backendUrl}`);
}

async function ensureBackend() {
  try {
    await waitForBackend();
    return;
  } catch {
    // The local harness owns a short-lived offline backend when one is not already running.
  }
  seedDemo();
  backendProcess = spawn(process.env.PYTHON ?? "python", [
    "-m", "uvicorn", "quant_agent.api.app:app", "--host", "127.0.0.1", "--port", String(backendPort),
  ], { cwd: projectRoot, env: pythonEnvironment(), stdio: ["ignore", "pipe", "pipe"] });
  backendProcess.stderr.on("data", (chunk) => process.stderr.write(`[backend] ${chunk}`));
  backendProcess.stdout.on("data", (chunk) => process.stdout.write(`[backend] ${chunk}`));
  await waitForBackend();
  await backend.generateDailyPlan({ request_id: "local-harness-plan" });
}

function jsonResponse(response, status = 200) {
  return JSON.stringify(response);
}

function clone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function scenarioResult(result, requestedTool, activeScenario = defaultScenario) {
  if (activeScenario === "disconnect") {
    return {
      content: [{ type: "text", text: JSON.stringify({ error: { code: "MCP_DISCONNECTED", message: "local harness simulated a disconnected MCP host" } }) }],
      structuredContent: { error: { code: "MCP_DISCONNECTED", message: "local harness simulated a disconnected MCP host" } },
      isError: true,
    };
  }
  if (activeScenario === "conflict" && ["quant_submit_approval", "quant_execute_paper_plan"].includes(requestedTool)) {
    return {
      content: [{ type: "text", text: JSON.stringify({ error: { code: "VERSION_CONFLICT", message: "报告版本已变化，请刷新后重新确认。" } }) }],
      structuredContent: { error: { code: "VERSION_CONFLICT", message: "报告版本已变化，请刷新后重新确认。" } },
      isError: true,
    };
  }
  if (activeScenario === "expired" && requestedTool === "quant_execute_paper_plan") {
    return {
      content: [{ type: "text", text: JSON.stringify({ error: { code: "APPROVAL_EXPIRED", message: "审批已过期，请生成新报告。" } }) }],
      structuredContent: { error: { code: "APPROVAL_EXPIRED", message: "审批已过期，请生成新报告。" } },
      isError: true,
    };
  }
  if (activeScenario === "error" && requestedTool === "quant_get_dashboard") {
    return {
      content: [{ type: "text", text: JSON.stringify({ error: { code: "BACKEND_UNAVAILABLE", message: "local harness simulated a backend disconnect" } }) }],
      structuredContent: { error: { code: "BACKEND_UNAVAILABLE", message: "local harness simulated a backend disconnect" } },
      isError: true,
    };
  }
  if (requestedTool !== "quant_get_dashboard" || !result?.structuredContent) return result;
  const dashboard = clone(result.structuredContent);
  if (activeScenario === "blocked" || activeScenario === "risk-blocked") {
    const report = dashboard.report;
    if (report) {
      report.status = "RISK_BLOCKED";
      report.plan.risk_decision.allowed = false;
      report.plan.risk_decision.allowed_order_ids = [];
      report.plan.risk_decision.blocked_order_ids = report.plan.orders.map((order) => order.order_id);
      const first = report.plan.risk_decision.checks[0];
      if (first) {
        first.passed = false;
        first.severity = "BLOCK";
        first.reason_code = "KILL_SWITCH_ON";
        first.message = "local harness simulated a risk block";
      }
    }
  }
  if (activeScenario === "expired") {
    if (dashboard.report) dashboard.report.status = "EXPIRED";
    if (dashboard.approval) dashboard.approval.expires_at = "2026-08-11T07:00:00Z";
  }
  if (activeScenario === "partial") {
    const report = dashboard.report;
    const firstOrder = report?.plan?.orders?.[0];
    if (report && firstOrder) {
      report.status = "PARTIALLY_FILLED";
      dashboard.execution = {
        execution_id: "exec_harness_partial",
        report_id: report.report_id,
        request_id: "harness-partial",
        mode: "paper",
        status: "PARTIALLY_FILLED",
        started_at: report.generated_at,
        completed_at: report.generated_at,
        broker_orders: [{
          order_id: firstOrder.order_id,
          client_order_id: firstOrder.client_order_id,
          symbol: firstOrder.symbol,
          side: firstOrder.side,
          quantity: firstOrder.quantity,
          status: "PARTIALLY_FILLED",
          submitted_at: report.generated_at,
          filled_quantity: String(Number(firstOrder.quantity) / 2),
          remaining_quantity: String(Number(firstOrder.quantity) / 2),
          fills: [],
        }],
        fills: [],
        reconciliation: { ok: true, messages: ["local harness simulated a partial fill"], remaining_order_ids: [firstOrder.order_id] },
      };
    }
  }
  if (activeScenario === "kill-switch") {
    dashboard.kill_switch = { enabled: true, reason: "local harness visual scenario", actor: "harness" };
  }
  return { ...result, structuredContent: dashboard, content: [{ type: "text", text: JSON.stringify(dashboard, null, 2) }] };
}

async function invokeTool(name, args, activeScenario = defaultScenario) {
  const result = await callTool(name, args, backend);
  return scenarioResult(result, name, activeScenario);
}

async function readBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

function harnessHtml(activeScenario, activeTheme) {
  const safeScenario = JSON.stringify(activeScenario).replace(/</g, "\\u003c");
  const safeTheme = JSON.stringify(activeTheme).replace(/</g, "\\u003c");
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Local MCP Apps Harness</title>
<style>body{margin:0;background:#e8edf5;color:#17223b;font:13px system-ui,sans-serif}header{align-items:center;background:#172b55;color:white;display:flex;gap:14px;justify-content:space-between;padding:10px 16px}header strong{letter-spacing:.08em}header span{opacity:.75;font-size:12px}iframe{border:0;display:block;height:calc(100vh - 44px);width:100%}</style>
</head><body><header><strong>LOCAL MCP APPS HARNESS</strong><span id="scenario"></span></header><iframe id="app" title="Quant Agent Dashboard"></iframe>
<script>
const frame=document.getElementById('app');
const scenario=${safeScenario};
const theme=${safeTheme};
document.getElementById('scenario').textContent='scenario: '+scenario+' · theme: '+theme;
frame.src='/ui/index.html';
function send(message){frame.contentWindow.postMessage(message,'*');}
async function invoke(name,args,id){
  const response=await fetch('/bridge',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name,arguments:args,scenario})});
  const result=await response.json();
  send({jsonrpc:'2.0',id,result});
  send({jsonrpc:'2.0',method:'ui/notifications/tool-result',params:{name,result}});
}
window.addEventListener('message',async(event)=>{
  if(event.source!==frame.contentWindow)return;
  const message=event.data||{};
  if(message.method==='ui/initialize'){
    send({jsonrpc:'2.0',id:message.id,result:{protocolVersion:'2025-06-18',hostContext:{theme,displayMode:'inline',maxHeight:1200},capabilities:{toolCalls:true,messages:true}}});
    window.setTimeout(()=>{send({jsonrpc:'2.0',method:'ui/notifications/tool-input',params:{name:'quant_get_dashboard',arguments:{}}});invoke('quant_get_dashboard',{},'host-dashboard');},20);
  }else if(message.method==='tools/call'){
    await invoke(message.params?.name,message.params?.arguments||{},message.id);
  }else if(message.method==='ui/message'){
    document.title='Local MCP Apps Harness · message received';
  }
});
</script></body></html>`;
}

function contentType(path) {
  const ext = extname(path);
  return ({ ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json" })[ext] ?? "application/octet-stream";
}

const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url, `http://127.0.0.1:${port}`);
    const activeScenario = url.searchParams.get("scenario") || defaultScenario;
    const activeTheme = url.searchParams.get("theme") || defaultTheme;
    if (url.pathname === "/healthz") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ status: "ok", scenario: defaultScenario, theme: defaultTheme }));
      return;
    }
    if (url.pathname === "/bridge" && request.method === "POST") {
      const body = JSON.parse(await readBody(request));
      const result = await invokeTool(body.name, body.arguments ?? {}, body.scenario || defaultScenario);
      response.writeHead(200, { "content-type": "application/json" });
      response.end(jsonResponse(result));
      return;
    }
    if (url.pathname === "/" || url.pathname === "/harness") {
      response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      response.end(harnessHtml(activeScenario, activeTheme));
      return;
    }
    if (url.pathname.startsWith("/ui/")) {
      const relative = url.pathname.slice("/ui/".length) || "index.html";
      const path = resolve(uiRoot, relative);
      if (!path.startsWith(uiRoot)) throw new Error("invalid UI path");
      const body = await readFile(path);
      response.writeHead(200, { "content-type": contentType(path), "cache-control": "no-store" });
      response.end(body);
      return;
    }
    response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    response.end("not found");
  } catch (error) {
    response.writeHead(500, { "content-type": "application/json" });
    response.end(JSON.stringify({ error: error.message }));
  }
});

await ensureBackend();
server.listen(port, "127.0.0.1", () => {
  console.log(`Local MCP Apps harness: http://127.0.0.1:${port}/`);
  console.log(`Scenario: ${defaultScenario}; backend: ${backendUrl}`);
});

function shutdown() {
  server.close();
  if (backendProcess) backendProcess.kill();
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
process.on("exit", () => backendProcess?.kill());
