# 本地 AI 额度桌面组件

Windows 透明桌面小部件：在同一块浮窗里**分开展示 Cursor 与 Codex 剩余额度**。无边框、可拖动、透明区域点击穿透，不会挡住桌面和其它窗口。

依赖主项目 `.venv`（需已安装 `Pillow`，见仓库根 `requirements.txt`）。

## 启动

双击 `start.bat`（`pythonw` 无控制台）。若已在运行，会自动忽略重复启动。

```powershell
cd local-quota-widget
..\.venv\Scripts\pythonw.exe app.py
```

## 操作

| 动作 | 说明 |
|------|------|
| 拖动卡片 | 移动组件（空白透明区不拦截点击） |
| 右上角 ··· | 菜单：刷新 / 置顶 / 开机自启 / 设置 Token / 关闭 |
| 右键 | 同一菜单 |
| Esc | 关闭 |
| F5 | 刷新 |

## 首次配置

1. **Cursor**：菜单 →「设置 Token」，粘贴浏览器 cookie `WorkosCursorSessionToken`
   - 打开 https://cursor.com/dashboard/usage 并登录
   - DevTools → Application → Cookies → `cursor.com` → 复制 `WorkosCursorSessionToken`
   - 凭据用 Windows DPAPI 加密，保存在 `.local/secrets.json`（已 gitignore）

2. **Codex**：在本机 Codex CLI/App 登录即可
   - 默认读取 `%USERPROFILE%\.codex\auth.json`
   - 若 token 在系统凭据库，请在 `%USERPROFILE%\.codex\config.toml` 加：
     ```toml
     cli_auth_credentials_store = "file"
     ```
     然后重新 `codex login`

## 展示内容

| 区块 | 数据来源 |
|------|----------|
| Cursor | Dashboard 非官方 API（本周期 / Auto / API 百分比 + **本周期 token 汇总**） |
| Codex | `wham/usage`（5 小时 / 7 天窗口、credits） |

- **自动刷新：默认每 5 分钟**（`.local/settings.json` 里 `refresh_seconds`，范围 60–3600 秒）
- Cursor 卡片左下角显示「更新 HH:MM · 每 5m」；F5 或菜单可立即刷新
- Cursor **token** 来自 Dashboard 的 `get-filtered-usage-events`，按当前计费周期汇总（输入+输出+缓存）；请求极多时会截断并显示 `+`
- 大数字与圆环表示**剩余**百分比；颜色：绿 > 40%，黄 ≤ 40%，红 ≤ 15%
- 界面：窗外全透明，上下两张圆角卡片（上 Cursor 海雾蓝，下 Codex 杏茶金）

## 文件说明

```
app.py              Tkinter 主程序 + 卡片绘制
layered.py          Windows 分层透明窗 + 透明区域点击穿透
storage.py          DPAPI 凭据与 settings 读写（.local/）
autostart.py        开机自启快捷方式
providers/cursor.py Cursor Dashboard API
providers/codex.py  Codex wham/usage API
start.bat           一键启动（复用上级 .venv）
.local/             本机 settings / secrets（不入库）
```

## 说明

- 使用 Cursor Dashboard 与 ChatGPT 后端的**非官方**接口，可能随版本变化失效
- Session / OAuth 过期后需重新登录或粘贴 token
- 本工具只读额度，不调用模型、不消耗额外 token
- Windows 任务栏不显示独立按钮（工具窗口）；关闭请用菜单或 Esc
- 典型内存占用约 **25–50 MB**（单实例）
- 开机自启：菜单 →「开机自启」，在 `%APPDATA%\...\Startup` 创建快捷方式
