# Chat 嵌入与验收边界

## 已实现

插件目录：`quant-agent-lab/plugins/quant-agent-dashboard`（相对于仓库根目录）。

`.mcp.json` 暴露 `quant-agent-dashboard` stdio server，`.app.json` 注册插件 app，
`.codex-plugin/plugin.json` 仅描述本地插件，不写入个人 marketplace。MCP server
从 `ui/dist` 提供 UI resource，tool definitions 使用 `_meta.ui.resourceUri`。

后端启动（项目根目录）：

```powershell
cd quant-agent-lab
$env:PYTHONPATH = 'src'
python -m quant_agent seed-demo --reset
uvicorn quant_agent.api.app:app --host 127.0.0.1 --port 8014
```

插件构建与本地 harness（另一个 PowerShell）：

```powershell
cd quant-agent-lab\plugins\quant-agent-dashboard
node scripts\build.mjs
node harness\server.mjs
```

## 验证层次

| 层次 | 本轮状态 | 证明内容 |
|---|---|---|
| MCP 合同 | 已验证 | `initialize`、`tools/list`、`resources/list/read`、`tools/call`、固定 Paper mode、request_id 和错误结果；5 个 Node 测试通过 |
| 本地 embedded harness | 已验证 | 本地 iframe、postMessage JSON-RPC、tool input/result、主题、断开、过期、冲突、阻断、部分成交、Kill Switch 和浏览器视觉检查 |
| 主 Agent 本地接入 | 已验证 | 桌面 GUI 的「量化中心」按钮按需启动 8014/4173 服务、复用健康进程并打开真实 standalone Dashboard |
| 真实远程 Chat/MCP 宿主 | 未验证 | 当前环境没有远程 MCP 配置、认证或发布验收，因此不声称已完成远程宿主注册 |

## 真实 Chat 的下一步

在目标 Chat/MCP 宿主中注册 `quant-agent-lab/plugins/quant-agent-dashboard/.mcp.json` 等价的本地 stdio 配置，确保宿主能访问项目 API，并按宿主要求完成授权和 CSP/iframe 验收。先验证 `tools/list` 和 `resources/read` 能取得声明的工具与 `ui://quant-agent-dashboard/dashboard.html`，再验证宿主的 tool input/result notification 与 `tools/call` 代理。验收仍必须确认只显示 PAPER TRADING、不会传递凭据、不会出现 live 模式。

这一步需要真实宿主的连接权限和认证配置，超出本地离线任务范围；本轮没有请求或保存任何凭据。
