# PC Agent Daemon — 网页控制电脑的异步守护进程（骨架）

用浏览器控制你的 Windows 电脑：在网页上实时看到屏幕画面，点击画面即可在对应位置点击电脑，还能输入文字、发送按键、一键紧急止停。

## 快速开始

**推荐方式：一键启动（双击即用，无需命令行）**

```
一键启动控制台.bat    ← 双击它：首次自动建 .venv 装依赖，之后直接弹出 Chat 聊天前端
启动Daemon.bat        ← （可选）只后台跑 Daemon，配合 Web 控制台使用
停止Daemon.bat        ← 停止后台 Daemon（按 8000 端口查找进程）
```

启动后的使用流程：
1. **Chat 聊天前端**（`chat.py`，Codex 风格）自动弹出，并自动拉起后台 Daemon
2. 点击 Chat 右上角 **「Open Screen Backend」** 按钮 → 打开屏幕控制面板（`gui.py`，测试后端）
3. 屏幕控制面板会**复用** Chat 已拉起的 Daemon（不重复启动）

命令行方式：

```bash
# 1. 创建虚拟环境并安装依赖（Python 3.13）
python -m venv .venv
.venv\Scripts\activate            # Git Bash: source .venv/Scripts/activate
pip install -r requirements.txt

# 2. 启动 Chat 前端（自动拉起 Daemon）
python src/chat.py
#    或单独启动 Daemon：python src/app.py
#    浏览器控制台：http://127.0.0.1:8000  |  API 文档：http://127.0.0.1:8000/docs
```

## 项目结构

```
├── src/                 # 源代码
│   ├── app.py           # FastAPI 屏幕控制 daemon（任务队列/单线程池/Kill-Switch）
│   ├── llm_server.py    # LLM API 后端（OpenAI 兼容转发/工具循环/压缩/统计/配置热更新）
│   ├── cli.py           # CLI 客户端（Linux/WSL 终端，零依赖）
│   ├── chat.py          # Chat 聊天前端（Tkinter，Codex 风格）
│   ├── gui.py           # 屏幕控制面板（Tkinter）
│   └── mock_llm.py      # 本地 mock OpenAI 接口（无真实 Key 时验证全链路）
├── static/
│   └── index.html       # Web 控制台（单文件 HTML + CSS + JS，无构建步骤）
├── scripts/             # 启动脚本
│   ├── 一键启动控制台.bat
│   ├── 启动Daemon.bat
│   ├── 停止Daemon.bat
│   └── start_wsl.sh     # WSL 一键启动（隔离模式）
├── tests/
│   └── smoke_test.py    # 打桩冒烟测试（不产生真实鼠标/键盘动作）
├── chat_config.json     # API 配置（含 Key，已 gitignore，不提交）
├── requirements.txt
└── README.md
```

## 架构设计

### 1. 异步解耦（核心）

PyAutoGUI / pynput 的 GUI 操作是**同步阻塞**的，绝不能出现在 FastAPI 主事件循环里，否则一个慢动作（如输入长文本）会卡死所有 API 请求。

```
浏览器控制台
   │  POST /api/v1/execute {action:"click", x, y}
   ▼
FastAPI 异步事件循环
   │  submit() 入队 → worker 协程取队头 → run_in_executor(executor, ...)
   ▼
ThreadPoolExecutor(max_workers=1)           ← 所有 GUI 操作串行执行
   │
   ▼
PyAutoGUI（FAILSAFE = True）
```

- **任务队列 + worker 协程**：事件循环上有一个 worker 协程串行消费队列，把每个动作丢进单线程池执行，结果写回对应的 `asyncio.Future`。这带来两个精确性保证：
  - `is_busy` / `queued` 计数精确——入队即算 busy，止停可精确取消"排队中"的任务；
  - **客户端断连安全**——HTTP 请求断开只取消"等待结果"的协程，后台任务继续执行完，不会留下半完成的鼠标/键盘状态；
- `max_workers=1` 保证动作严格串行：GUI 操作天然不可并发，串行既安全又保序；
- 截图（`pyautogui.screenshot()`）同样是阻塞操作，同样经线程池执行；
- pyautogui 在 Windows 导入时已自动调用 `SetProcessDPIAware()`，坐标与截图像素统一为物理像素，无需额外处理。

### 2. Kill-Switch 三重保险

| 层级 | 机制 | 触发方式 |
|---|---|---|
| 物理层 #1 | `pyautogui.FAILSAFE = True` | 鼠标甩到屏幕**四角**，当前动作立即中断并自动进入止停状态 |
| 物理层 #2 | pynput 全局热键（可选） | 任意时候按 **Ctrl+Alt+Shift+X** |
| 逻辑层 #3 | `stop_requested` 全局止停标志 | `POST /api/v1/stop`，或前端红色 STOP 按钮 |

止停后：所有新指令返回 `423 Locked`，排队中尚未执行的任务被取消；`POST /api/v1/reset` 恢复。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/execute` | 执行动作：`click` / `type_text` / `press_key` / `screenshot`（止停时返回 423） |
| GET | `/api/v1/status` | Daemon 状态：`mode`（idle/busy/stopped）、`is_busy`、`queued`、`current_action`、`stop_requested`、屏幕尺寸 |
| POST | `/api/v1/stop` | 紧急止停：置标志 + 取消排队任务 |
| POST | `/api/v1/reset` | 恢复正常运行 |
| GET | `/api/v1/screenshot` | 当前屏幕 JPEG 字节流（`image/jpeg`，`Cache-Control: no-store`） |

`execute` 请求体示例：

```json
{ "action": "click",     "x": 960, "y": 540, "clicks": 1 }
{ "action": "click",     "x": 100, "y": 200, "clicks": 2 }      // 双击
{ "action": "type_text", "text": "你好，世界\n第二行" }           // 中文自动走剪贴板粘贴，\n 转 Enter
{ "action": "press_key", "key": "ctrl+c" }                       // 组合键用 +
{ "action": "press_key", "key": "enter" }
{ "action": "screenshot" }                                       // 返回 base64 JPEG
```

安全校验：`click` 的 x/y 必须成对且不越界（422）；`press_key` 校验按键合法性；`button` 仅允许 left/right/middle。
中文等非 ASCII 字符经剪贴板粘贴输入（完成后还原剪贴板），ASCII 直打。

## CLI 客户端（cli.py — Linux 虚拟机 / WSL / 任意终端）

零依赖（纯标准库，Python 3.8+），无需 Tkinter。**通过 HTTP 连接 Windows 主机上的 llm_server**，Agent 推理与工具执行都在主机端完成，虚拟机里只做交互。

### WSL 快速上手（已验证）

WSL2 默认开启 localhost 转发——**WSL 里用 `localhost` 就能访问 Windows 上的服务**，不需要 0.0.0.0：

```bash
# Windows 侧（两个终端）
python src/llm_server.py --port 8001        # LLM 后端（本机即可，WSL 通过 localhost 访问）
python src/app.py                           # 屏幕控制 daemon

# WSL 侧（Debian 13 + Python 3.13 已验证）
# 复制 cli.py（wsl.conf automount=false 时用 base64 管道传输）：
base64 -w0 cli.py | wsl -d Debian -- bash -c "base64 -d > ~/cli.py"
# 运行：
wsl -d Debian -- bash -c "cd ~ && python3 cli.py --host localhost --port 8001"
```

已在 WSL Debian 验证：对话、多轮上下文、工具调用循环（`⚙ get_screen_size → ✓ 2880×1620`）全部正常。

### 远程 Linux 虚拟机

```
Linux 虚拟机（只装 Python）
   │  python src/cli.py --host <主机IP> --port 8001 --token xxx
   ▼
Windows 主机 llm_server.py（:8001，--host 0.0.0.0，--token）
   │  ├─ 调 DeepSeek API（推理 + 工具调用循环）
   │  └─ 调本机 daemon app.py（:8000，屏幕操作）
   ▼
Windows 屏幕
```

**主机端启动（Windows，远程场景）：**
```bash
python src/llm_server.py --host 0.0.0.0 --port 8001 --token 你的随机token
python src/app.py                 # 屏幕控制 daemon（另开终端）
# Windows 防火墙放行 8001 端口（专用网络）
```

**虚拟机端：**
```bash
# 方式 1：命令行参数（把 cli.py 复制到 VM 即可，无任何依赖）
python src/cli.py --host 192.168.1.10 --port 8001 --token 你的随机token

# 方式 2：保存配置后直接运行
python src/cli.py /config host=192.168.1.10 token=你的随机token
python src/cli.py

# 方式 3：单次模式（脚本调用）
python src/cli.py --once "打开计算器" --host 192.168.1.10 --token xxx
```

**交互命令：** `/new` 新会话 · `/sessions` 列表 · `/switch N` 切换 · `/clear` 清空 · `/stats` Token 用量与缓存命中率 · `/config` 保存连接配置 · `/quit` 退出；流式输出期间 **Ctrl+C 中止**当前任务。

### Token 缓存命中率（/stats）

DeepSeek 上下文硬盘缓存：相同前缀（system 提示 + 历史）的 prompt token 按缓存计费（远低于未命中价）。llm_server 聚合每次上游调用的 `prompt_tokens_details.cached_tokens`：

```bash
>>> /stats
=== Token 用量统计 ===
  调用次数:        5
  Prompt tokens:   9,320
  缓存命中 tokens: 8,874
  缓存未命中:      446
  Completion:      1,245（推理 310）
  缓存命中率:      95.2%
```

命中率 >50% 绿色、20-50% 黄色、<20% 红色。实测：相同请求第二次命中率 75-99%（System 提示+历史全缓存，仅增量部分计费）。数据源：`GET /api/v1/stats`（llm_server 进程内聚合，重启清零）。

### 上下文容量与压缩（/status）

- **`/status`**：显示模型上下文窗口（`context_window`，可在 chat_config.json 配置，默认 64K）、当前会话估算用量（字符×0.8 保守系数）、占用率、剩余、压缩记录
- **自动压缩**：估算用量达到窗口 60% 时，发送前自动调用 `/api/v1/compress`：
  - 保留最近 8 条消息 + system
  - 早期对话交给模型生成**结构化中文摘要**（保留：需求/已完成任务/进行中状态/文件路径/决策约束）
  - 摘要以 system 消息注入，替换被压缩的历史
  - 实测：32 条 → 10 条、省 68,881 字符；压缩后模型基于摘要继续高质量回答

```
>>> /status
=== 上下文容量 ===
  模型窗口:      65,536 tokens
  当前用量(估):  19,047 tokens（33 条消息）
  占用率:        29%
  剩余:          46,489 tokens
  压缩阈值:      39,321（达到即自动压缩）
```

### Token 用量优化（内置）

| 优化项 | 说明 |
|---|---|
| 历史消息裁剪 | 发送上游前保留 system + 最近 19 条消息，早期对话丢弃并提示模型（agent 循环内部不裁剪，保证工具消息顺序） |
| 工具结果截断 | 工具结果回传模型限 1500 字符（read_file 大文件等不会撑爆上下文） |
| 前端上下文净化 | 会话 messages 只存模型纯回复（工具日志渲染文本仅用于界面展示，不再发给模型） |
| 输出上限收紧 | read_file 4000 字符 / list_folder 50 条 / run_code 2000 字符 |
| 缓存复用 | 固定 system+工具定义前缀被 DeepSeek 缓存命中（实测 75-99%），新增成本仅增量部分 |

⚠️ 远程访问安全：llm_server 必须带 `--token`（否则局域网内任何人都能控制你的电脑）；token 通过 `X-Api-Token` 请求头传递。

## Chat 聊天前端（chat.py）

Codex 风格的深色交互界面，作为整个 Agent 的主入口，正在向 ZCode 类编程助手演进：

- **消息渲染（Markdown 子集）**：
  - ```` ```python ```` 代码块 → 深色代码卡片（语言标签 + 等宽字体）
  - `**粗体**`、`` `行内代码` ``
  - **思考过程折叠**：模型的 `reasoning_content` 显示为可展开的「▶ 思考过程」区
- **流式输出**：回复 SSE 逐块渲染（打字机效果 + `▍` 光标）；v4-flash 等模型的推理过程实时显示为「◌ 思考中…」
- **多会话管理**：侧边栏会话列表（`会话 #1`…），**New Session** 新建，点击列表项切换（自动重建该会话历史），当前会话高亮
- **现代化消息气泡**：用户（蓝/右）/ Agent（深灰/左），头像、时间戳、自动滚动到底部
- **顶部工具栏**：
  - **Settings** — 打开 API 设置窗口（URL / API Key / Model / Test Connection）
  - **Open Screen Backend** — 打开 `gui.py` 屏幕控制面板（复用 Chat 已拉起的 Daemon）
  - **实时 Daemon 状态**：绿/红圆点 + 模式文本
- **侧边栏**：会话信息、LLM 连接状态（未配置时黄色引导）、可用工具列表、实时日志
- **输入框**：Enter 发送、Shift+Enter 换行
- 与后端解耦：HTTP 请求在后台线程，Tk 主线程永不阻塞

```bash
python src/chat.py                 # 默认 127.0.0.1:8000
python src/chat.py --port 9000     # 指定端口
```

### API 设置（前端本地保存 + 连接测试）

点击 Chat 顶部 **Settings** 按钮，可配置：
- **API URL** — 支持任意 OpenAI 兼容接口，可填域名 / base_url / 完整地址，路径自动归一化：
  - DeepSeek：`https://api.deepseek.com`
  - OpenAI：`https://api.openai.com/v1`
  - Ollama（本地）：`http://localhost:11434/v1`
- **API Key** — 输入框显示为密码
- **Model** — 例如 `deepseek-chat` / `gpt-4o`
- **Test Connection** — 发送最小请求（`ping`）验证配置，结果显示在窗口内：
  - 成功：`✓ 连接成功 · model: xxx · 回复: ...`
  - 失败：显示真实原因（Key 无效 / 模型不存在 / 路径错误 / 网络不通），并把上游原始错误信息（如 `Model Not Exist`、`Authentication Fails`）透出

配置保存在 `chat_config.json`，**LLM 后端（llm_server.py，端口 8001）实时读取**——保存或测试成功后主界面侧边栏 LLM 状态立即刷新为已连接。

> ⚠️ `chat_config.json` 包含你的 API Key，请勿提交到任何代码仓库（已在 .gitignore 中）。

## LLM 聊天链路

```
Chat 前端 (chat.py)
   │  POST /api/v1/chat/stream     ← 多轮消息历史 + agent 模式
   ▼
LLM 后端 (llm_server.py :8001)
   │  读取 chat_config.json (URL / Key / Model)
   │  网络调用在后台线程执行，不阻塞事件循环
   ▼
OpenAI 兼容接口（真实模型，如 OpenAI / DeepSeek / Ollama）
```

### Agent 工具调用循环

`agent: true` 时启用内置工具（OpenAI 格式 function calling）：

| 工具 | 说明 |
|---|---|
| `get_screen_size` | 获取屏幕分辨率（点击前获取坐标范围） |
| `click` | 屏幕坐标点击（x/y/clicks/button） |
| `type_text` | 聚焦输入框输入文字（中文自动处理） |
| `press_key` | 按键 / 组合键（enter、ctrl+c、alt+tab…） |
| `create_folder` | 在工作区 `~/agent_workspace` 内创建文件夹（支持嵌套路径） |
| `list_folder` | 浏览工作区目录内容（名称/类型/大小，单目录限 100 条） |
| `create_file` | 创建/覆盖写入文件（代码编写，限 100KB） |
| `read_file` | 读取文件内容（限 200KB / 返回 1 万字符，防爆 token） |
| `run_code` | 执行 Python 代码（file 或 code 参数；超时 30s 强制终止；输出限 3000 字符） |
| `stop` | 紧急止停（模型自主喊停） |

**隔离模式（`--isolated`）**：禁用全部屏幕工具，仅保留文件类工具（`create_folder`/`list_folder`/`create_file`/`read_file`/`run_code`）——WSL 安全测试用，代码层面保证 agent 无法操作屏幕（已实测：隔离模式下模型明确拒绝"点击屏幕"类指令）。

`create_folder` 安全约束：只允许工作区（主目录 `agent_workspace`）内的**相对路径**；拒绝绝对路径、盘符、`..` 穿越、空路径、符号链接越界。工作区跟随 llm_server 运行系统（Windows 上是 `C:\Users\xxx\agent_workspace`，WSL 上是 `/home/xxx/agent_workspace`）。

### WSL 隔离测试环境（已验证）

WSL 与 Windows 系统盘**完全隔离**（该环境 `wsl.conf`：`automount=false` + `interop=false`）：
- `/mnt/c`、`/mnt/d` 未挂载（空目录），WSL 内文件操作物理上无法触及 Windows 盘符
- 无法从 WSL 启动任何 Windows 程序
- 实测：WSL 里创建 `~/agent_workspace/demo/test1`，Windows 侧 `C:\Users\lyt\agent_workspace` 不存在 ✓

**在 WSL 里跑 Agent（无 sudo）：**
```bash
# 一次初始化（venv --without-pip + get-pip.py，无需 sudo，已验证）
python3 -m venv --without-pip ~/.venv
python3 /tmp/get-pip.py --quiet           # get-pip.py 需先传入 WSL
~/.venv/bin/pip install fastapi uvicorn

# 传输代码（base64 管道，不依赖 /mnt 挂载）
base64 -w0 llm_server.py | wsl -d Debian -- bash -c "base64 -d > ~/llm_server.py"
base64 -w0 cli.py         | wsl -d Debian -- bash -c "base64 -d > ~/cli.py"
base64 -w0 chat_config.json | wsl -d Debian -- bash -c "base64 -d > ~/chat_config.json"

# 启动 + 使用
wsl -d Debian -- bash -c "cd ~ && nohup .venv/bin/python src/llm_server.py --port 8001 &"
wsl -d Debian -- bash -c "cd ~ && .venv/bin/python src/cli.py --host localhost --port 8001"
```

已实测：Agent 对话「创建文件夹 demo/test1」→ 模型自主调 `create_folder` → `~/agent_workspace/demo/test1` 创建成功；尝试 `../escape` 越界路径被模型 + 工具双层拒绝。完整闭环「编写 fib.py → run_code 运行 → list_folder 浏览」一次成功。

### Agent 安全锁（防循环调用导致系统崩溃）

| 安全锁 | 阈值 | 触发行为 |
|---|---|---|
| 工具轮数上限 | 10 轮 | 中止任务 |
| 工具调用总数上限 | 30 次/请求 | 中止任务 |
| **连续失败熔断器** | 4 次 | 立即熔断中止（防死循环重试） |
| 任务总时长上限 | 240 秒 | 自动中止 |
| 参数校验 | — | type_text ≤5000 字符；click 坐标必须在屏幕范围内；press_key ≤50 字符 |
| **并发互斥** | 同一时刻仅 1 个 agent 循环 | 新请求返回"已有另一个 Agent 任务正在执行" |
| 客户端断开取消 | — | 前端 Stop / 断线 → 循环线程在下一检查点退出，锁自动释放 |
| 前端中止按钮 | — | Send 在流式期间变为红色 **⏹ Stop**，点击立即中断 |

模型侧还有 FAILSAFE（鼠标甩到屏幕四角）、`stop` 工具、前端紧急 STOP 按钮兜底。

流程：模型输出 `tool_calls` → llm_server 执行工具（调 daemon `/api/v1/execute`）→ 结果以 `tool` 消息回传 → 循环（上限 10 轮）→ 最终回复流式输出。

前端通过 SSE 事件实时显示：`event: tool_call`（⚙ 工具+参数）→ `event: tool_result`（✓/✗ 结果）→ 内容流式渲染，工具日志与会话历史合并保存（切换会话可回看）。

- 聊天时前端显示 `…` thinking 占位气泡，回复到达后**原位更新**为完整回复
- 多轮上下文：system 提示 + 完整 user/assistant 历史随请求发送
- 错误引导：Key 无效（401/403）、URL 路径错误（404）、限流（429）、未配置均返回可读中文提示

### 无真实 Key 时本地验证

```bash
python src/mock_llm.py                     # 终端 1：mock OpenAI 接口（:8999，Key 需含 good-key）
# 在 Chat 的 Settings 填入：
#   API URL: http://127.0.0.1:8999/v1/chat/completions
#   API Key: good-key
#   Model:   test-model
python src/chat.py                         # 终端 2：打开 Chat，直接对话
```

Tkinter 原生窗口，与后端完全解耦（仅通过 127.0.0.1 HTTP API 通信，后台线程执行网络请求，主线程永不阻塞）：

- **屏幕预览**：自动/手动刷新显示电脑屏幕，点击预览画面自动换算真实坐标并发送 click（支持双击）；
- **状态徽章**：Idle / Busy（含当前动作与排队数）/ Stopped / 离线；屏幕权限、分辨率、排队数、FAILSAFE 一屏可见；
- **紧急止停**：红色 STOP 按钮（带确认框）+ 恢复 (Reset)，与热键 Ctrl+Alt+Shift+X 等效；
- **命令面板**：输入文字（中文自动走剪贴板）、发送按键、常用快捷键；
- **自动拉起 Daemon**：探测失败时自动启动 `python src/app.py`（`--no-spawn` 可禁用），日志区实时显示操作结果。

```bash
python src/gui.py                     # 默认连接/拉起 127.0.0.1:8000
python src/gui.py --port 9000         # 指定端口
python src/gui.py --no-spawn          # 不自动拉起 Daemon（要求已运行 python src/app.py）
python src/gui.py --smoke             # 自检模式：连接+截图验证后自动退出（退出码 0/1）
```

> 依赖装在 `.venv` 里。即使你直接用全局 `python src/gui.py` 运行，GUI 也会自动选用 `.venv` 的解释器来拉起 Daemon；若 Daemon 启动失败（如端口被占用、依赖缺失），真实原因会直接显示在 GUI 日志区（stderr 写入 `daemon.err.log`），不再静默超时。

## 屏幕权限（Windows）

- Windows 应用截屏/输入**不需要权限弹窗**（不同于 macOS），但要求进程运行在**交互式桌面会话**中——通过 SSH、计划任务或服务方式运行时无法访问屏幕；
- 后端启动时会做屏幕权限自检，`GET /api/v1/status` 返回 `screen_access: true/false`，GUI 状态栏直接显示"正常 / 不可用 ✗"；
- 坐标基准：pyautogui 导入时自动设置进程 DPI 感知，截图与输入坐标统一为物理像素（建议显示缩放 100%，多显示器暂以主屏为准）；
- 部分杀软 / Windows 智能应用控制可能拦截输入注入（SendInput），如点击无响应请将 Python 加入白名单。

## Web 控制台功能

- **实时画面**：自动刷新（可关）或手动刷新显示电脑屏幕；
- **点击即控**：点击网页上的屏幕画面，按 `显示尺寸 / 自然尺寸` 比例换算成真实屏幕坐标，自动 POST `click` 指令；
- **状态徽章**：Idle（绿）/ Busy（黄，附带当前动作与排队数）/ Stopped（红）/ Offline（灰），1.5s 轮询；
- **紧急止停**：红色 STOP 大按钮 + 恢复 (Reset) 按钮，止停后指令按钮自动禁用；
- **命令面板**：输入文字、发送按键、常用快捷键 chips。

## 冒烟测试

打桩测试：把 PyAutoGUI 的副作用替换为无害延迟，验证队列/止停/校验逻辑，不产生真实鼠标键盘动作。

```bash
pip install httpx2        # TestClient 依赖（仅测试用）
python tests/smoke_test.py      # 21 项断言，含并发提交与止停竞态
```

## 安全注意

- ⚠️ **只监听 `127.0.0.1`**：本工程默认不对外暴露，否则局域网内任何人可通过 API 控制你的电脑；
- 若需远程访问，请通过 SSH 隧道 / VPN 等加密通道，而不是直接开放端口；
- 生产使用建议加 Token 鉴权（FastAPI `Depends` 校验 Header 即可）；
- Windows 显示缩放（DPI）非 100% 时，PyAutoGUI 坐标与截图像素可能不一致，建议设置缩放 100% 或在代码中统一 DPI 感知；
- 守护进程需在**交互式桌面会话**中运行（不能作为无桌面的服务）。

## 扩展点

- **新动作类型**：在 `HANDLERS` 字典中注册即可（如 `scroll`、`drag`、`key_hold`）；
- **实时画面推送**：截图接口已按需工作，进阶可改用 WebSocket 推送 JPEG 帧降低轮询开销；
- **任务队列**：`ThreadPoolExecutor` 天然带队列，可在 `submit()` 前加任务计数/优先级；
- **指令鉴权**：在 `execute` 路由上加 `Depends(verify_token)`。
