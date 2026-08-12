# Quant Agent Dashboard

本插件是 `quant-agent-lab` 的本地 MCP Apps 适配器、Paper Trading UI 和离线 Strategy Lab。它只调用项目的版本化 FastAPI/ApplicationService，不复制策略、风险、审批或执行逻辑。

## 构建与测试

```powershell
node scripts\build.mjs
node --test mcp-server/tests/contract.test.mjs ui/tests/ui-contract.test.mjs
node harness\e2e-demo.mjs
```

正式本地界面（真实量化后端，Paper Trading）：

```powershell
node scripts\build.mjs
node standalone\server.mjs
# http://127.0.0.1:4173/#/dashboard
```

正式 standalone host 只监听 `127.0.0.1:4173`，提供 `/healthz`、`/api/connection`
和受限的量化 MCP bridge；它不使用 Harness 场景数据。`harness` 仍仅用于开发和
视觉状态测试：

```powershell
node harness\server.mjs
# http://127.0.0.1:4173/
```

场景：`?scenario=blocked`、`partial`、`kill-switch`、`error`、`expired`、`conflict`；主题：`?theme=dark`。场景 payload 仅用于 UI 视觉/状态测试。

默认界面包含极客终端风格 K 线图：OHLCV、成交量、均线、BUY/SELL、候选订单、Paper 成交和持仓成本线。策略实验室支持声明式 JSON AST 的编辑、参数校验、逐根 DebugTrace、确定性回测、版本比较和 Paper Candidate 提升。当前只有 `1d` 本地合成数据，不伪造分钟周期。

## 结构

- `.codex-plugin/plugin.json`：插件元数据；
- `.mcp.json`：本地 stdio server 配置；
- `.app.json`：插件 app 注册；
- `mcp-server/src`：MCP JSON-RPC、tool schema、资源和后端 HTTP adapter；
- `ui/src`：无依赖 Web Components 风格 UI；
- `harness`：iframe + postMessage 的本地宿主模拟；
- `docs/strategy-lab.md`、`docs/backtest-methodology.md`、`docs/custom-strategy-security.md`：研究合同、安全和回测假设；
- `artifacts/gui`：插件内临时构建/测试资产；项目交付截图在项目根 `artifacts/gui`。

## 安全边界

始终显示 PAPER TRADING。UI 不访问 broker、数据库、凭据或父页面 DOM；不自动执行；所有审批、Kill Switch、计划哈希和执行前 risk recheck 由 Python 后端决定。自定义 Python 策略因没有可信隔离沙箱而禁用，声明式策略不使用 `eval` 或 `exec`。真实 Chat 宿主配置和认证尚未验证，不要将本地 harness 视为真实 Chat 验收。
