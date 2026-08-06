# PC Agent

一个跑在本机的 Agent：把 LLM 接到你的电脑上，让它能看屏幕、点鼠标、敲键盘、读写文件、跑命令。

三个进程，各干各的：

- `src/app.py` — 屏幕控制 daemon，PyAutoGUI 操作在单线程队列里排队执行，带紧急止停
- `src/llm_server.py` — LLM 中转：接任意 OpenAI 兼容接口，把模型的工具调用转成对 daemon 的请求
- 前端 — `src/chat.py`（Tkinter 聊天窗）、`src/cli.py`（终端）、`src/gui.py`（屏幕面板）、`static/index.html`（网页控制台）

## 快速开始

### Windows 桌面版

双击 `scripts/一键启动控制台.bat`：首次运行自动建 `.venv` 装依赖，之后直接弹聊天窗口。

也可以手动起：

```
.venv\Scripts\python src\chat.py
```

在聊天窗的「设置」里填 API 地址和 Key，点「连接」验证。地址兼容任意 OpenAI 格式接口，DeepSeek 直接填 `https://api.deepseek.com` 就行。

`llm_server.py` 默认监听 127.0.0.1:8001，由前端自动拉起；想单独跑也行：

```
.venv\Scripts\python src\llm_server.py --port 8001
```

### WSL / Linux

配好 Python 3.13 环境（venv 即可），然后：

```
bash scripts/start_wsl.sh
```

脚本用 `--isolated` 模式启动：代码层面删掉全部屏幕操作工具，只留文件类工具。前提是 WSL 配好隔离（`automount=false`、`interop=false`），这样 agent 在 WSL 里折腾，Windows 桌面完全不受影响。

### 手动起三个进程

```
.venv\Scripts\python src\app.py --port 8000      # 屏幕 daemon
.venv\Scripts\python src\llm_server.py --port 8001
.venv\Scripts\python src\chat.py                # 或 cli.py / gui.py
```

## 前端

| 前端 | 入口 | 用途 |
|---|---|---|
| 聊天窗 | `src/chat.py` | 主力前端：流式回复、工具调用过程、多会话、Markdown 渲染 |
| 终端 CLI | `src/cli.py` | 零依赖，SSH / WSL 里用 |
| 屏幕面板 | `src/gui.py` | 看截图、手动点按 |
| 网页控制台 | `static/index.html` | 早期版本：浏览器看屏幕 + 点击 |

### CLI 命令

| 命令 | 作用 |
|---|---|
| `/new` `/sessions` `/switch` | 会话管理 |
| `/status` | 连接状态、上下文容量 |
| `/stats` | token 统计（含缓存命中率） |
| `/model` | 换模型 |
| `/confirm-mode` | 切换确认模式（热生效） |
| `/config` `/apiconfig` | 查看 / 保存连接配置 |
| `/help` `/quit` | 帮助 / 退出 |

## Agent 工具

模型在对话里按需调用，不用配置：

- 屏幕：`get_screen_size` `click` `type_text` `press_key`
- 文件：`create_folder` `list_folder` `create_file` `read_file`
- 执行：`run_code`（子进程跑，30 秒超时强杀）、`run_shell`（Linux shell 常用命令）

## 安全

三层防护：

1. **确认制**。敏感操作先问人。`confirm_mode` 四档：
   - `auto` — 只读/低危自动放行，写操作问
   - `strict` — 凡是写操作都问
   - `trusted` — 基本不问
   - `query` — 只读也问（演示用）

   确认框长这样，超时不答默认拒绝：

   ```
   需要修改文件 /workspace/test.txt：
   1. 允许
   2. 允许并记住（本次会话）
   3. 拒绝
   ```

   遇到关键抉择（比如多条路选一条），模型会列出选项让你选。

2. **黑名单**。`run_shell` 拦截 `rm -rf /`、`mkfs`、`shutdown`、`dd`、fork 炸弹、`git push --force` 这类命令；重定向写文件（`echo hi > /etc/x`）同样拦。

3. **屏幕兜底**。PyAutoGUI 自带 FAILSAFE（鼠标甩到屏幕角落立刻中断），另加热键 Ctrl+Alt+Shift+X 紧急止停，前端 STOP 按钮同理。所有屏幕操作在单线程队列里排队，不会并发打架。

再加几个防呆：单轮最多 10 次工具调用、单次请求工具总数 30、连续失败 4 次熔断、单次请求最长 240 秒；同一时刻只允许一个 agent 任务在跑。

## 隔离（WSL 测试环境）

在 WSL 里测、不碰 Windows：

- `automount=false` — WSL 看不到 Windows 盘符
- `interop=false` — WSL 里起不了 Windows 程序
- `--isolated` — 代码里删掉屏幕工具，agent 想点鼠标都没工具可调

文件工具限制在 `workspace/` 内（`_safe_join` 拒绝绝对路径和 `..` 越界），`read_file` 有大小上限，防止爆 token。

## 上下文管理

对话历史超过上下文窗口的 60% 自动压缩：老消息丢给模型做摘要，保留最近 8 条原文。`context_window` 按模型在配置里调。`/stats` 能看到缓存命中率——DeepSeek 对重复前缀自动缓存，命中率高是正常的，不是 bug。

## 配置

`chat_config.json` 存 API 配置（`api_url` / `api_key` / `model` / `context_window` / `confirm_mode`），运行中改设置立即生效。**这个文件在 .gitignore 里，不要提交**。也可以用命令行传：

```
.venv\Scripts\python src\cli.py --token sk-xxx --api-url https://api.deepseek.com --model deepseek-v4-flash
```

## 目录结构

```
src/app.py          屏幕 daemon
src/llm_server.py   LLM 中转 + agent 循环
src/chat.py         Tkinter 聊天前端
src/cli.py          终端 CLI
src/gui.py          屏幕控制面板
src/mock_llm.py     假 API（测试用）
scripts/            一键启动 .bat、start_wsl.sh
static/index.html   网页控制台
tests/smoke_test.py 冒烟测试
```

## 开发

```
.venv\Scripts\python tests\smoke_test.py   # 21 个断言，pyautogui 已打桩，不碰真实屏幕
.venv\Scripts\python src\mock_llm.py       # 假 API：key 校验、工具模拟、loop-model 用来测熔断
```

改代码注意：

- `.bat` 必须 ASCII + CRLF，`.sh` 必须 LF（`.gitattributes` 已配好，别手动改行尾）
- Windows 控制台默认 GBK，CLI 中文乱码先 `chcp 65001`
