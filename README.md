# PC Agent

把 LLM 接到你的电脑上，让它能看屏幕、点鼠标、敲键盘、读写文件、跑命令、管 Git、查代码。

## 架构

三个进程 + 任意 OpenAI 兼容 LLM：

- `src/app.py` — 屏幕控制 daemon，鼠标键盘操作在单线程队列里排队执行
- `src/llm_server.py` — 中枢：接 LLM，把模型的工具调用转成实际动作，同时负责确认/计划审批/上下文压缩/会话存储/MCP
- 前端（任选其一）— `chat.py` 聊天窗 / `cli.py` 终端 / `gui.py` 屏幕面板 / `telegram_bot.py` 手机遥控 / `static/index.html` 网页

## 快速开始（Windows）

双击 `scripts/一键启动控制台.bat`：首次自动建 `.venv` 装依赖，之后直接弹聊天窗口。手动起也一样：

```
.venv\Scripts\python src\chat.py
```

打开后在「设置」里填 API 地址和 Key，点「连接」验证。任意 OpenAI 兼容接口都能接，DeepSeek 填 `https://api.deepseek.com`。也可以命令行方式：`cp chat_config.example.json chat_config.json` 后填 Key（样例文件无密钥，安全入库）。

## WSL / Linux（隔离测试环境）

配好 Python 3.13 环境后：

```
bash scripts/start_wsl.sh
```

脚本用 `--isolated` 模式启动：删掉全部屏幕工具，只留文件类，保证不碰 Windows 桌面（前提是 WSL 配了 `automount=false`、`interop=false`）。

**Telegram 手机遥控（WSL 里跑 bot，纯标准库）：**

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

bot 命令：`/status` `/stats` `/sessions` `/switch N` `/send <路径>`（把工作区文件发到你手机）`/schedule`（定时任务，见下）。

**定时任务（`/schedule`）**：`/schedule add 08:00 搜索今日科技新闻并总结` 添加，到点自动执行并把结果推送到手机；`/schedule` 查看，`/schedule del <id>` 删除，`/schedule off|on <id>` 暂停/恢复。任务存 `.pcagent/schedules.json`，重启不丢。

## 命令行

```
.venv\Scripts\python src\app.py --port 8000      # 屏幕 daemon
.venv\Scripts\python src\llm_server.py --port 8001   # 中枢（WSL 上加 --isolated）
.venv\Scripts\python src\cli.py --token sk-xxx --api-url https://api.deepseek.com
```

CLI 里 `/help` 看全部命令；`/model` 换模型，`/confirm-mode` 切确认模式，`/reasoning` 切推理强度，`/stats` 看 token 用量。

## 用法：对话就是操作

模型在对话里按需调用 29 个工具，不用配置：

- **屏幕**：看分辨率、点击、输入文字、按按键、截图
- **文件**：建文件夹、浏览目录、读写文件、`search_text` 代码检索、`list_symbols` 看函数结构
- **编辑**：`replace_text` 精确替换，改前弹 diff 确认；改错用 `undo` 一键回滚（自动备份，可逆）
- **Git**：`git_status` / `git_diff` / `git_log` / `git_commit`（提交需确认）
- **执行**：跑 Python（`run_code`）、shell 命令（`run_shell`）、后台进程（`start_process`）
- **规划**：`create_todo` 列任务清单，前端实时显示，重启后保留
- **索引**：`repo_map` 生成项目结构摘要（目录树 + 符号表）
- **系统**：`system_status` 查磁盘/内存/CPU 负载（只读）
- **技能**：`load_skill` 加载用户导入的技能包（见下节）

## 技能包（Skill）

把常用工作流写成 `skills/<名称>/SKILL.md`（frontmatter 写 `name`/`description`），丢进目录即导入，无需重启：

```markdown
---
name: daily-brief
description: 每日简报：搜科技新闻 + 查系统状态，汇总成简报
---
# 每日简报
1. 用 system_status 查资源
2. 用 tavily 搜索今日科技新闻
3. 汇总输出
```

启动时只把**技能清单**（名称 + 一句话）注入系统提示，模型判断任务匹配时用 `load_skill` 加载全文——惰性注入，技能再多也不撑上下文。技能要求的操作**不绕过确认模式**。

## 子 Agent 与视觉操作

`agents/<名称>.json` 定义专业子代理（system_prompt + 工具白名单 + 可选 model 覆盖），`delegate` 工具自动委派任务——子 agent 在**独立上下文 + 工具白名单**里执行，事件实时透传前端，结果摘要返回主循环：

```json
{
  "name": "vision",
  "description": "视觉分析专家：查看图片/截图并描述内容",
  "system_prompt": "你是视觉分析专家。用 view_image 查看图片。",
  "tools": ["view_image", "read_file", "list_folder"]
}
```

- 安全护栏：子 agent **不能再委派**（深度 ≤ 2 层）、轮数上限 6、写操作仍走确认、plan 模式仅主循环生效
- `/agents`（cli/Telegram）查看列表；任务适合子 agent 时模型自动 `delegate`，也可显式说「委派给 vision」

**视觉操作（view_image）**：把图片（工作区路径，如 Telegram 上传的截图）发给配置的视觉模型分析。DeepSeek 无视觉能力，需在 `chat_config.json` 配一个 OpenAI 兼容视觉模型，如通义千问 qwen-vl：

```json
"vision_api_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
"vision_api_key": "sk-你的Key",
"vision_model": "qwen-vl-max"
```

## 安全

### 确认模式（5 种，`/confirm-mode` 切换，默认 auto）

| 模式 | 行为 |
| --- | --- |
| `auto`（默认） | 智能：敏感写操作弹确认，只读命令免确认 |
| `strict` | 严格：所有修改/执行类操作都需确认 |
| `trusted` | 信任：全部自动执行（危险命令黑名单仍拦截） |
| `query` | 只读：仅允许查询操作，一切修改直接拒绝 |
| `plan` | 计划：任务前先列计划表格（步骤 + 所需工具），一次批准后按计划执行，计划内免确认 |

- 确认超时默认**拒绝**（120 秒）；`run_shell` 拦 `rm -rf /`、`mkfs`、`shutdown` 等危险命令（Windows 上还拦 `format C:`、`rd /s /q`、`diskpart`）
- 紧急止停：鼠标甩到屏幕角落，或按 Ctrl+Alt+Shift+X

### 推理强度（DeepSeek v4 系列，默认最高）

| 档位 | 参数 | 适用 |
| --- | --- | --- |
| 最高 `max` | `reasoning_effort: max` | 复杂任务，思考最深 |
| 高 `high` | `reasoning_effort: high` | 速度与质量平衡 |
| 关闭 `off` | 禁用思考 | 简单问答，最快最省 |

CLI `/reasoning`（或 `/reasoning max|high|off`）、Chat 设置下拉、配置存 `chat_config.json` 实时生效。上下文压缩和连接测试固定用「关闭」，不吃思考配额。

### 密钥管理：什么文件不能提交

| 文件 | 内容 | 状态 |
| --- | --- | --- |
| `chat_config.json` | API Key | 已 gitignore |
| `telegram_config.json` | bot token | 已 gitignore |
| `mcp_config.json` | 第三方 PAT | 已 gitignore |
| `.pcagent/` | 聊天记录 / 修改备份 / 运行日志 | 已 gitignore |
| `tools/` | 本地工具目录 | 已 gitignore |

`*.example.json` 样例无密钥，可入库。

## MCP 外部工具

通过 MCP（Model Context Protocol）接入生态工具，配置 `mcp_config.json`（含第三方 token，**已 gitignore 不入库**；示例见 `mcp_config.example.json`）：

```json
{
  "servers": {
    "github": {"command": "/path/to/github-mcp-server", "args": ["stdio"],
               "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "你的PAT"}},
    "chrome": {"command": "npx", "args": ["-y", "@playwright/mcp@latest", "--browser", "chrome"]},
    "tavily": {"command": "npx", "args": ["-y", "tavily-mcp@latest"],
               "env": {"TAVILY_API_KEY": "tvly-你的Key"}},
    "spotify": {"command": "uvx", "args": ["mcp-spotify"],
                "env": {"SPOTIFY_CLIENT_ID": "你的ID", "SPOTIFY_CLIENT_SECRET": "你的Secret",
                        "SPOTIFY_REDIRECT_URI": "http://127.0.0.1:8888/callback"}}
  }
}
```

- 启动时连接各 server，工具以 `mcp_<server>_<tool>` 命名动态并入（如 `mcp_github_create_issue`、`mcp_tavily_tavily_search`、`mcp_spotify_search_tracks`），health 可见
- MCP 工具调用**默认弹确认**（trusted 模式放行 / query 模式拒绝）；**只读 MCP server（tavily 网络搜索类）免确认、免规划**；混合型 server（spotify）按工具区分——搜索/歌单查询免确认，播放/建歌单等写操作保持确认
- 官方 GitHub MCP 用 release 二进制（`github-mcp-server stdio`），不要用 npm 上的同名杂牌包
- 若 server 需要走代理/特殊环境（如 WSL 里的 npx、node、uvx），在 `env` 里显式补 `HTTP_PROXY`、`HOME` 等——MCP 的 `env` 会替换子进程完整环境，缺 PATH 时 npx 会启动失败
- Spotify 首次授权：dashboard.spotify.com 建 Developer App（Redirect URI 必须 `http://127.0.0.1:8888/callback`，不能用 localhost），配好 env 后首次调用走 OAuth，token 缓存 `~/.spotify_mcp_cache`；换区不影响凭据，仅 Premium 空窗期 API 不可用

## 会话与数据

- 会话历史自动保存到项目根 `.pcagent/sessions.json`（含聊天记录，**已 gitignore，不会入库**）；重启自动恢复，chat / cli / 网页 / Telegram 共享同一份历史（后端权威存储）
- 文件修改自动备份到 `.pcagent/backups/`（`undo` 回滚用，50 条上限）
- 运行日志写入 `.pcagent/server.log` 与 `.pcagent/bot.log`（1MB 轮转保留 3 份，排查问题看这里）
- 任务清单存在工作区 `.pcagent/todos.json`（跟随机器）；定时任务存 `.pcagent/schedules.json`
- 整个项目文件夹拷到 U 盘即可随身携带历史（`.venv` 需在每台机器重建，不进 U 盘）

## 进程守护（Linux）

WSL 里两个服务由 systemd 托管，崩溃自动拉起、开机自启（模板见 `scripts/systemd/`）：

```
cp scripts/systemd/pc-agent-*.service ~/.config/systemd/user/
systemctl --user enable --now pc-agent-llm pc-agent-bot
```

已内置代理环境变量；`journalctl --user -u pc-agent-llm -n 50` 看日志。

## 目录结构

```
src/                源代码（app / llm_server / 五个前端 / mock_llm 测试 API）
scripts/            一键启动 .bat、start_wsl.sh、start_telegram.sh、systemd 单元
static/index.html   网页控制台
skills/             技能包（用户自建：<名称>/SKILL.md，可入库分享）
tests/              九套测试（共 232 断言）
.github/workflows/  CI（push 自动跑九套测试）
chat_config.example.json   API 配置样例（无密钥，复制为 chat_config.json 后填 Key）
mcp_config.example.json    MCP server 配置样例（无密钥，复制为 mcp_config.json 后填 token）
chat_config.json    API 配置（含 Key，已 gitignore，别提交）
telegram_config.json  bot 配置（含 token，已 gitignore，别提交）
mcp_config.json     MCP server 配置（含 PAT，已 gitignore，别提交）
.pcagent/           会话历史 / 修改备份 / 运行日志（gitignore，不入库）
```

## 开发与测试

```
.venv\Scripts\python tests\smoke_test.py        # daemon 冒烟（21 断言，不碰真实屏幕）
.venv\Scripts\python tests\llm_tools_test.py    # 编程工具（69 断言：检索/编辑/undo/git/进程/todo/repo）
.venv\Scripts\python tests\agent_loop_test.py   # agent 循环端到端（33 断言：ask+diff/todo/上下文上界/plan）
.venv\Scripts\python tests\session_test.py      # 会话持久化（30 断言：CRUD/重启恢复/上限）
.venv\Scripts\python tests\mcp_test.py          # MCP 客户端（21 断言：echo server 全链路/命名解析/白名单）
.venv\Scripts\python tests\cli_session_test.py  # CLI 会话（13 断言：摘要/懒加载/降级）
.venv\Scripts\python tests\reasoning_test.py    # 推理强度（14 断言：档位映射/持久化/校验）
.venv\Scripts\python tests\skill_system_test.py # 技能包与系统监控（14 断言：扫描/加载/免确认/降级）
.venv\Scripts\python src\mock_llm.py            # 无 Key 时本地假 API 验证全链路
```

推送后 GitHub Actions 自动跑八套测试（共 215 断言），回归结果在 Actions 页面一眼可见。

注意：`.bat` 要 ASCII + CRLF，`.sh` 要 LF；Windows 控制台是 GBK，CLI 中文乱码先 `chcp 65001`。
