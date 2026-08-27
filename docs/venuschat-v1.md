# VenusChat V1

VenusChat V1 是与已移除的 `src/chat.py` 完全隔离的原生 Windows 前端，通过 HTTP/SSE 对接 `llm_server`。

- 代码入口：`src/venuschat_v1/`
- 启动脚本：`scripts/启动VenusChat V1.bat` 或 `scripts/一键启动控制台.bat`
- 视觉方向：暖白石灰色、宋体标题、陶土红单一强调色、默认弱边框

## 已接入能力

| 区域 | 后端 API |
|------|----------|
| 会话列表 / 新建 / 删除 | `/api/v1/sessions` |
| 流式对话 + 工具卡 | `/api/v1/chat/stream` (SSE) |
| 工具确认 | `/api/v1/agent/respond` |
| 异步派活 | `!任务` 或 `/dispatch …` → `/api/v1/jobs` |
| 执行面板（待办 / 后台任务 / 本轮工具） | SSE `todo_update` + `/api/v1/jobs` |
| 健康 / 项目 / 记忆 / CodeGraph / MCP | 设置页各子面板 |
| 量化中心 | `quant_integration`（可选） |

## 启动

```powershell
# 先确保 llm_server 在 :8001 运行
.venv\Scripts\python -m venuschat_v1
```

打开设置页：

```powershell
.venv\Scripts\python -m venuschat_v1 --settings
```

配置读写 `chat_config.json`（本地，已在 `.gitignore`）。
