# PC Agent

把 LLM 接到你的电脑上，让它能看屏幕、点鼠标、敲键盘、读写文件、跑命令。

三个进程：

- `src/app.py` — 屏幕控制 daemon，所有鼠标键盘操作在单线程队列里排队执行
- `src/llm_server.py` — LLM 中转，接任意 OpenAI 兼容接口，把模型的工具调用转成实际动作
- 前端 — `src/chat.py`（聊天窗）、`src/cli.py`（终端）、`src/gui.py`（屏幕面板）、`src/telegram_bot.py`（手机遥控）

## 快速开始（Windows）

双击 `scripts/一键启动控制台.bat`：首次自动建 `.venv` 装依赖，之后直接弹聊天窗口。

手动起也一样：

```
.venv\Scripts\python src\chat.py
```

打开后在「设置」里填 API 地址和 Key，点「连接」验证。任意 OpenAI 兼容接口都能接，DeepSeek 填 `https://api.deepseek.com`。

也可以命令行方式：`cp chat_config.example.json chat_config.json` 后填 Key（样例文件无密钥，安全入库）。

## WSL / Linux

配好 Python 3.13 环境，然后：

```
bash scripts/start_wsl.sh
```

脚本用 `--isolated` 模式启动：删掉全部屏幕工具，只留文件类，保证不碰 Windows 桌面（前提是 WSL 配了 `automount=false`、`interop=false`）。

**WSL 里跑 Telegram 前端（手机遥控，纯标准库）：**

```
# 1. Windows 侧传代码（base64 管道，不依赖 /mnt）
base64 -w0 src/telegram_bot.py | wsl -d Debian -- bash -c "base64 -d > ~/telegram_bot.py"

# 2. WSL 里建配置 telegram_config.json（含 bot token，不入库）：
#    { "bot_token": "BotFather 获取", "proxy": "http://<宿主机IP>:7890",
#      "llm_url": "http://127.0.0.1:8001", "allowed_chat_ids": [] }

# 3. WSL 里后台运行
wsl -d Debian -- bash -c "cd ~ && nohup .venv/bin/python telegram_bot.py &"
```

白名单 `allowed_chat_ids` 留空时，第一个发 `/start` 的人自动成为管理员；敏感操作在手机上弹允许/拒绝按钮。

## 命令行

```
.venv\Scripts\python src\app.py --port 8000      # 屏幕 daemon
.venv\Scripts\python src\llm_server.py --port 8001
.venv\Scripts\python src\cli.py --token sk-xxx --api-url https://api.deepseek.com
```

CLI 里 `/help` 看全部命令；`/model` 换模型，`/confirm-mode` 切确认模式，`/reasoning` 切推理强度，`/stats` 看 token 用量。

**推理强度**（DeepSeek v4 系列）：三档可选，默认最高。
- 最高（`max`）→ `reasoning_effort: max`，思考最深，复杂任务效果最好
- 高（`high`）→ `reasoning_effort: high`，速度与质量平衡
- 关闭（`off`）→ 禁用思考（`thinking: disabled`），最快最省，适合简单问答

CLI 里 `/reasoning`（或 `/reasoning max|high|off`）切换；Chat 界面在 Settings 里选；配置存 `chat_config.json` 实时生效。上下文压缩和连接测试固定用「关闭」，不吃你的思考配额。

## Agent 能干的事

模型在对话里按需调工具，不用配置：

- **屏幕**：看分辨率、点击、输入文字、按按键
- **文件**：建文件夹、浏览目录、读写文件、`search_text` 代码检索、`list_symbols` 看函数结构
- **编辑**：`replace_text` 精确替换，改前弹 diff 给你确认；改错用 `undo` 一键回滚（自动备份，可逆）
- **Git**：`git_status` / `git_diff` / `git_log` / `git_commit`（提交需确认）
- **执行**：跑 Python（`run_code`）、shell 命令（`run_shell`）、后台进程（`start_process`）
- **规划**：`create_todo` 列任务清单，侧边栏实时显示，重启后保留
- **索引**：`repo_map` 生成项目结构摘要（目录树 + 符号表）

## 安全

敏感操作会弹确认，超时默认拒绝：覆盖文件、修改代码（`replace_text` 会先展示 diff）、撤销修改（`undo` 同样展示 diff）、git 提交、非只读 shell、启动后台进程。`run_shell` 拦 `rm -rf /`、`mkfs`、`shutdown` 这类危险命令（Windows 上还拦 `format C:`、`rd /s /q`、`diskpart`）；鼠标甩到屏幕角落或按 Ctrl+Alt+Shift+X 立刻紧急止停。

**计划审批模式（plan）**：任务执行前 agent 先提交计划表格（步骤 + 每步所需工具 + 原因），你一次批准后按计划执行——计划内声明的操作免确认，计划外操作仍会确认，未规划直接调写操作会被拒绝。只读查询（list_folder/read_file 等）无需规划。`/confirm-mode plan` 切换。

## MCP 外部工具

通过 MCP（Model Context Protocol）接入生态工具，配置 `mcp_config.json`（含第三方 token，**已 gitignore 不入库**；示例见 `mcp_config.example.json`）：

```json
{
  "servers": {
    "github": {"command": "/path/to/github-mcp-server", "args": ["stdio"],
               "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "你的PAT"}},
    "chrome": {"command": "npx", "args": ["-y", "@playwright/mcp@latest", "--browser", "chrome"]}
  }
}
```

- 启动时连接各 server，工具以 `mcp_<server>_<tool>` 命名动态并入（如 `mcp_github_create_issue`），health 可见
- MCP 工具调用**默认弹确认**（trusted 模式放行 / query 模式拒绝）
- 官方 GitHub MCP 用 release 二进制（`github-mcp-server stdio`），不要用 npm 上的同名杂牌包

## 会话与数据

- 会话历史自动保存到项目根 `.pcagent/sessions.json`（含聊天记录，**已 gitignore，不会入库**）
- 重启程序自动恢复全部会话；chat / cli / 网页 / Telegram 共享同一份历史（后端权威存储）
- 文件修改自动备份到 `.pcagent/backups/`（`undo` 回滚用，50 条上限）
- 运行日志写入 `.pcagent/server.log` 与 `.pcagent/bot.log`（1MB 轮转保留 3 份，排查问题看这里）
- 任务清单存在工作区 `~/agent_workspace/.pcagent/todos.json`（跟随机器）
- 整个项目文件夹拷到 U 盘即可随身携带历史（`.venv` 需在每台机器重建，不进 U 盘）

## 目录结构

```
src/                源代码（app / llm_server / 五前端 / mock_llm 测试 API）
scripts/            一键启动 .bat、start_wsl.sh、start_telegram.sh
static/index.html   网页控制台
tests/              四套测试（daemon / 编程工具 / agent 循环 / 会话）
.github/workflows/  CI（push 自动跑四套测试）
chat_config.example.json   API 配置样例（复制为 chat_config.json 后填 Key）
mcp_config.example.json    MCP server 配置样例（复制为 mcp_config.json 后填 token）
chat_config.json    API 配置（含 Key，已 gitignore，别提交）
telegram_config.json  bot 配置（含 token，已 gitignore，别提交）
.pcagent/           会话历史 / 修改备份 / 运行日志（gitignore，不入库）
```

## 开发

```
.venv\Scripts\python tests\smoke_test.py       # daemon 冒烟测试（21 断言，不碰真实屏幕）
.venv\Scripts\python tests\llm_tools_test.py   # 编程工具测试（69 断言：检索/编辑/undo/git/进程/todo/repo/超时）
.venv\Scripts\python tests\agent_loop_test.py  # agent 循环端到端（19 断言：ask+diff/todo/上下文硬上界）
.venv\Scripts\python tests\session_test.py     # 会话持久化（28 断言：CRUD/重启恢复/上限）
.venv\Scripts\python src\mock_llm.py           # 无 Key 时本地假 API 验证全链路
```

推送后 GitHub Actions 自动跑四套测试（共 137 断言），回归在 Actions 页面一眼可见。

注意：`.bat` 要 ASCII + CRLF，`.sh` 要 LF；Windows 控制台是 GBK，CLI 中文乱码先 `chcp 65001`。
