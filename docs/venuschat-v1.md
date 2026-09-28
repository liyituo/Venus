# VenusChat V1

VenusChat V1 是与已移除的 `src/chat.py` 完全隔离的原生 Windows 前端，通过 HTTP/SSE 对接 `llm_server`。

- 代码入口：`src/venuschat_v1/`
- 启动脚本：`scripts/启动VenusChat V1.bat` 或 `scripts/一键启动控制台.bat`（均会检查或启动后端）
- 视觉方向：暖白石灰色、统一的界面字体、陶土红单一强调色、圆角控件与低对比边框

## 已接入能力

| 区域 | 后端 API |
|------|----------|
| 会话列表 / 新建 / 删除 | `/api/v1/sessions` |
| 流式对话 + 工具卡 | `/api/v1/chat/stream` (SSE) |
| 工具确认 | `/api/v1/agent/respond` |
| 异步派活 | `!任务` 或 `/dispatch …` → `/api/v1/jobs` |
| 执行面板（待办 / 后台任务 / 本轮工具） | SSE `todo_update` + `/api/v1/jobs` |
| 团队成员与设备 | “设置 → 团队与成员”；独立 Hub 凭证、加入申请、管理员审核、设备撤销 |
| 健康 / 项目 / 记忆 / CodeGraph / MCP | 设置页各子面板 |

## 启动

Windows 上直接双击 `scripts/一键启动控制台.bat`。脚本负责创建虚拟环境、补齐依赖、检查 `chat_config.json` 指向的后端、等待本机后端就绪并打开 GUI；再次运行会复用已有进程。错误会留在控制台，后端和 GUI 日志在 `.venus/launcher/`。

双击 `scripts/创建VenusChat桌面快捷方式.bat` 可以在当前用户桌面安装带图标的快捷方式。窗口与任务栏使用 `assets/venuschat.ico`。配置远端 Agent URL 时，脚本只验证远端状态；团队 Hub 按下文专用流程启动。

```powershell
# 手动打开 GUI 时，先确保个人 llm_server 在 127.0.0.1:8001 运行
cd src
..\.venv\Scripts\python -m venuschat_v1
```

打开设置页：

```powershell
cd src
..\.venv\Scripts\python -m venuschat_v1 --settings
```

配置读写 `chat_config.json`（本地，已在 `.gitignore`）。

## 团队 Hub 与成员加入

团队 Hub 是独立于个人 API Key 的连接。远程地址必须是 Tailscale Serve 的 HTTPS 域名；HTTP 仅允许本机 `127.0.0.1` / `localhost`。设置页新增“团队与成员”，不把模型 API Key 当作团队凭证。

Hub 管理员在已登录 Tailscale 的 Hub 电脑运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_team_hub.ps1
```

脚本以 `--isolated --team-serve` 启动后端，只绑定 `127.0.0.1:8001`，并用 `tailscale serve --bg --https=443 http://127.0.0.1:8001` 对 tailnet 开放 HTTPS。要求启用 tailnet HTTPS 证书。脚本会检查 MagicDNS 主机名和已有 Serve 配置，显示 HTTPS 地址及 Serve 状态；已有其他 Serve 配置时会停止，避免静默覆盖。不要配置 Funnel，也不要把后端监听改成 `0.0.0.0`。Hub 初始化、成员迁移边界和错误码详见 [团队协作与入队指南](collaboration-plan.md#hub-安全启动与单团队边界)。

Hub 初始化后运行 `powershell -ExecutionPolicy Bypass -File scripts/check_team_hub.ps1`，脚本会验证 Serve 根路径、实际 HTTPS Hub 响应并显示团队名称与 `team_id`。Hub 电脑重启后再次运行 `start_team_hub.ps1` 启动后端；Serve 配置本身会后台恢复。

首次运行后，在 Hub 本机 VenusChat → 设置 → 团队与成员初始化一次团队。管理员创建邀请时填写受邀者的实际 Tailscale 登录名，通过可信渠道交付 HTTPS 地址、team_id 和 24 小时一次性邀请码。受邀者在自己的 VenusChat 核对 HTTPS Hub 和团队身份后提交申请；管理员核验申请上的实际 Serve 登录名并批准；申请人再领取设备凭证。申请与领取要求相同的、非 tagged Tailscale 用户身份。GUI 将每台设备凭证单独保存到 Windows DPAPI，成功后再验证“我的团队身份”。

```text
设置 → 团队与成员
  管理员：创建邀请 → 刷新待审申请 → 核对身份 → 批准 / 拒绝
  成员：核对 Hub → 提交加入申请 → 刷新状态 → 领取凭证并连接
  丢失设备：管理员撤销对应设备 → 为原登录名重新邀请
```

成员可以从“成员与设备”退出当前设备；管理员可以撤销单台设备或停用整名成员。撤销会让后续请求立即失效。丢失设备且无法在该设备上操作时，管理员应使用另一台仍有效的管理员设备撤销它。成员停用后如需恢复，管理员重新邀请并重新审核。不要把设备凭证、邀请码、个人记忆或私人文件放进项目仓库；团队项目只按项目明确共享范围建立 Git 仓库。

完整双机操作顺序、迁移现有用户、冲突与回退、API 列表及手工验收清单见 [docs/collaboration-plan.md](collaboration-plan.md)。
