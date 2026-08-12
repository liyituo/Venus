import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { extname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { BackendClient } from "../mcp-server/src/backend-client.mjs";
import { callTool } from "../mcp-server/src/tools.mjs";

const here = resolve(fileURLToPath(new URL(".", import.meta.url)));
const pluginRoot = resolve(here, "..");
const projectRoot = resolve(pluginRoot, "..", "..");
const uiRoot = resolve(pluginRoot, "ui", "dist");
const port = Number(process.env.QUANT_AGENT_STANDALONE_PORT ?? 4173);
const backendUrl = process.env.QUANT_AGENT_BACKEND_URL ?? "http://127.0.0.1:8014";
const backend = new BackendClient(backendUrl);

if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("invalid standalone port");
if (!existsSync(resolve(uiRoot, "index.html"))) throw new Error("UI build missing; run node scripts/build.mjs");

function json(res, status, payload) {
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
  res.end(JSON.stringify(payload));
}

function safeUiPath(urlPath) {
  const relative = urlPath.slice("/ui/".length) || "index.html";
  const path = resolve(uiRoot, relative);
  if (path !== uiRoot && !path.startsWith(uiRoot + "\\") && !path.startsWith(`${uiRoot}/`)) throw new Error("invalid UI path");
  return path;
}

function contentType(path) {
  return ({ ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8" })[extname(path)] ?? "application/octet-stream";
}

function hostHtml() {
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' ${backendUrl}; frame-ancestors 'self' http://127.0.0.1:*"><title>PC Agent · 量化中心</title><style>html,body{margin:0;height:100%;background:#050810;color:#e9f3ff;font:13px system-ui,sans-serif}header{height:42px;box-sizing:border-box;padding:7px 14px;background:#0a1424;border-bottom:1px solid #20344d;letter-spacing:.08em;font-weight:700}header span{float:right;color:#8cecff}header button{margin-left:12px;border:1px solid #31506a;border-radius:5px;background:#102338;color:#dff8ff;padding:4px 9px;cursor:pointer}iframe{display:block;border:0;width:100%;height:calc(100% - 42px)}</style></head><body><header>PC AGENT · QUANT CENTER <span>STANDALONE · PAPER ONLY <button id="return-main" type="button">返回主 Agent</button></span></header><iframe id="quant-app" title="量化中心"></iframe><script>const frame=document.getElementById('quant-app'); const route=()=>location.hash||'#/dashboard'; const load=()=>{frame.src='/ui/index.html'+route()}; load(); window.addEventListener('hashchange',load); document.getElementById('return-main').addEventListener('click',()=>{document.title='PC Agent · 请切换回主窗口'; document.getElementById('return-main').textContent='已返回，请切换主 Agent'; try{window.close()}catch(_){}}); async function invoke(name,args,id){try{const response=await fetch('/bridge',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name,arguments:args})}); const result=await response.json(); frame.contentWindow.postMessage({jsonrpc:'2.0',id,result},'*'); frame.contentWindow.postMessage({jsonrpc:'2.0',method:'ui/notifications/tool-result',params:{name,result}},'*')}catch(error){frame.contentWindow.postMessage({jsonrpc:'2.0',id,result:{isError:true,structuredContent:{error:{code:'STANDALONE_BRIDGE_ERROR',message:'本地量化服务暂时不可用'}}}},'*')}} window.addEventListener('message',async(event)=>{if(event.source!==frame.contentWindow)return; const message=event.data||{}; if(message.method==='ui/initialize'){frame.contentWindow.postMessage({jsonrpc:'2.0',id:message.id,result:{protocolVersion:'2025-06-18',hostContext:{theme:'dark',displayMode:'inline',maxHeight:2000},capabilities:{toolCalls:true,messages:true}}},'*')} else if(message.method==='tools/call'){await invoke(message.params?.name,message.params?.arguments||{},message.id)} else if(message.method==='ui/route'){const next=message.params?.hash;const allowed=['#/dashboard','#/chart','#/strategy-lab','#/backtests','#/risk','#/audit'];if(typeof next==='string'&&allowed.includes(next)){location.hash=next}} else if(message.method==='ui/message'){document.title='PC Agent · 量化中心 · message received'}});</script></body></html>`;
}

async function readRequestBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url, `http://127.0.0.1:${port}`);
    if (url.pathname === "/healthz") {
      json(response, 200, { status: "ok", service: "quant-agent-dashboard-standalone", backend_url: backendUrl, mode: "PAPER_TRADING", live_broker: "disabled" });
      return;
    }
    if (url.pathname === "/api/connection") {
      try { await backend.request("/api/v1/health"); json(response, 200, { status: "ok", backend_url: backendUrl, mode: "PAPER_TRADING", live_broker: "disabled" }); }
      catch (error) { json(response, 503, { status: "error", code: error.code ?? "BACKEND_UNAVAILABLE", message: "量化后端不可用" }); }
      return;
    }
    if (url.pathname === "/bridge" && request.method === "POST") {
      const body = JSON.parse(await readRequestBody(request));
      if (typeof body.name !== "string" || !body.name.startsWith("quant_")) {
        json(response, 400, { isError: true, structuredContent: { error: { code: "INVALID_TOOL", message: "只允许量化 MCP 工具" } } });
        return;
      }
      const result = await callTool(body.name, body.arguments ?? {}, backend);
      json(response, 200, result);
      return;
    }
    if (url.pathname === "/" || url.pathname === "/index.html") {
      response.writeHead(200, { "content-type": "text/html; charset=utf-8", "content-security-policy": `default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' ${backendUrl}; frame-ancestors 'self' http://127.0.0.1:*`, "cache-control": "no-store" });
      response.end(hostHtml());
      return;
    }
    if (url.pathname.startsWith("/ui/")) {
      const path = safeUiPath(url.pathname);
      const body = await readFile(path);
      response.writeHead(200, { "content-type": contentType(path), "cache-control": "no-store" });
      response.end(body);
      return;
    }
    response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    response.end("not found");
  } catch (error) {
    json(response, 500, { status: "error", code: "STANDALONE_HOST_ERROR", message: String(error.message ?? error).slice(0, 180) });
  }
});

server.listen(port, "127.0.0.1", () => console.log(`Quant Agent standalone host: http://127.0.0.1:${port}/#/dashboard`));

function shutdown() { server.close(); }
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
