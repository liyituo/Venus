"""
PC Agent Chat — Codex 风格的交互前端（Tkinter，零额外依赖）

改进点
------
- 更精致的美观：圆角容器、统一字体、现代化配色、hover 动效
- 消息气泡支持 Markdown 风格粗体/代码、自动滚动
- 顶部工具栏：Settings（API 设置页）、Open Screen Backend、状态指示
- Settings 窗口：独立的 API 配置界面（模型 URL / API Key / Model Name），
  保存到 chat_config.json（前端本地保存，后端暂不处理）
- 自动连接 / 拉起 FastAPI Daemon，状态实时显示

运行：python chat.py [--port 8000]
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import font, messagebox, ttk
import urllib.error
import urllib.request

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR.parent / "chat_config.json"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# ===== 配色系统 =====
BG = "#0d1117"
PANEL = "#161b22"
PANEL_LIGHT = "#1c2128"
BORDER = "#30363d"
TEXT = "#e6edf3"
TEXT_DIM = "#8b949e"
ACCENT = "#3b82f6"
ACCENT_HOVER = "#2563eb"
USER_BUBBLE = "#1f6feb"
AGENT_BUBBLE = "#21262d"
STOP = "#ef4444"
OK = "#22c55e"
WARN = "#f59e0b"
CODE_BG = "#0d1117"
CODE_FG = "#c9d1d9"

# 会话首条 system 提示（本地结构发送用；后端只存 user/assistant 消息）
SYSTEM_FIRST = ("你是一个桌面 Agent 助手，可以控制用户的电脑"
                "（屏幕点击、输入、按键、截图）。回答尽量简洁、准确。")


def api_request(base_url: str, method: str, path: str, payload=None,
                timeout: float = 15, raw: bool = False):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        base_url + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if raw:
                return resp.status, body, resp.headers
            try:
                return resp.status, json.loads(body.decode("utf-8")), None
            except ValueError:
                return resp.status, {"detail": "non-JSON response"}, None
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", "")
        except Exception:
            detail = f"HTTP {e.code}"
        return e.code, {"detail": detail}, None
    except Exception as e:
        return 0, {"detail": f"cannot connect to Daemon ({e})"}, None


# ===== 配置管理 =====
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "api_url": "https://api.deepseek.com/v1/chat/completions",
        "api_key": "",
        "model": "deepseek-v4-flash",
    }


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


# ===== Settings 窗口（API 设置） =====
class SettingsWindow:
    def __init__(self, parent: tk.Tk, on_changed=None):
        self.parent = parent
        self.config = load_config()
        self.on_changed = on_changed   # 保存或连接成功后回调（主界面刷新 LLM 状态）
        self.win = tk.Toplevel(parent)
        self.win.title("Settings - API")
        self.win.geometry("560x430")
        self.win.configure(bg=BG)
        self.win.transient(parent)
        self.win.grab_set()
        self._build_ui()
        self.win.focus_force()

    def _build_ui(self) -> None:
        # 标题栏
        header = tk.Frame(self.win, bg=PANEL, height=50)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="⚙ Settings", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 14, "bold")).pack(side="left", padx=16, pady=10)

        body = tk.Frame(self.win, bg=BG)
        body.pack(fill="both", expand=True, padx=20, pady=16)

        # API URL
        self._row(body, 0, "API URL", "api_url")
        # API Key
        self._row(body, 1, "API Key", "api_key", show="*")
        # Model
        self._row(body, 2, "Model", "model")

        # 提示文本（支持各种 OpenAI 兼容 API）
        tk.Label(body, text="支持任意 OpenAI 兼容接口：可直接填域名或 base_url，\n"
                            "例如 https://api.deepseek.com / https://api.openai.com/v1 /\n"
                            "http://localhost:11434/v1 ，路径会自动补全为 /chat/completions。",
                 bg=BG, fg=TEXT_DIM, justify="left", font=("Segoe UI", 9)).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(14, 0))

        # 测试连接结果
        self.test_result = tk.Label(body, text="", bg=BG, fg=TEXT_DIM, justify="left",
                                    wraplength=480, font=("Segoe UI", 9))
        self.test_result.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # 按钮区
        btns = tk.Frame(self.win, bg=BG)
        btns.pack(fill="x", padx=20, pady=(0, 16))
        self.test_btn = self._btn(btns, "Test Connection", self._test_connection)
        self.test_btn.pack(side="left")
        self._btn(btns, "Cancel", self.win.destroy).pack(side="right", padx=(8, 0))
        self._btn(btns, "Save", self._save, accent=True).pack(side="right")

    def _row(self, parent, row: int, label: str, key: str, show: str = ""):
        tk.Label(parent, text=label, bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI", 10, "bold")).grid(row=row, column=0, sticky="nw", pady=(14, 4))
        ent = tk.Entry(parent, bg=PANEL_LIGHT, fg=TEXT, relief="flat",
                       insertbackground=TEXT, font=("Segoe UI", 10), width=44,
                       show=show)
        ent.insert(0, self.config.get(key, ""))
        ent.grid(row=row, column=1, sticky="ew", pady=(14, 4), padx=(12, 0))
        setattr(self, f"entry_{key}", ent)

    def _btn(self, parent, text: str, cmd, accent: bool = False) -> tk.Button:
        bgc = ACCENT if accent else PANEL_LIGHT
        fgc = "white" if accent else TEXT
        btn = tk.Button(parent, text=text, command=cmd, bg=bgc, fg=fgc,
                        activebackground=ACCENT_HOVER if accent else BORDER,
                        activeforeground="white", relief="flat", bd=0,
                        padx=18, pady=6, cursor="hand2", font=("Segoe UI", 9, "bold"))
        return btn

    def _save(self) -> None:
        self.config["api_url"] = self.entry_api_url.get().strip()
        self.config["api_key"] = self.entry_api_key.get().strip()
        self.config["model"] = self.entry_model.get().strip()
        save_config(self.config)
        if self.on_changed:
            self.on_changed()
        self.win.destroy()

    # ---- 连接测试（后台线程，不阻塞 UI）----
    def _test_connection(self) -> None:
        # 先保存当前输入，测试用最新配置
        self.config["api_url"] = self.entry_api_url.get().strip()
        self.config["api_key"] = self.entry_api_key.get().strip()
        self.config["model"] = self.entry_model.get().strip()
        save_config(self.config)
        self.test_btn.config(state="disabled", text="Testing...")
        self.test_result.config(text="正在测试连接…", fg=TEXT_DIM)
        threading.Thread(target=self._do_test, daemon=True).start()

    def _do_test(self) -> None:
        code, data, _ = api_request(
            "http://127.0.0.1:8001", "POST", "/api/v1/test", timeout=90)
        self.win.after(0, lambda: self._show_test_result(code, data))

    def _show_test_result(self, code: int, data: dict) -> None:
        self.test_btn.config(state="normal", text="Test Connection")
        if code == 200:
            self.test_result.config(
                text=f"✓ 连接成功 · model: {data.get('model')} · 回复: {data.get('reply', '')}",
                fg=OK)
            if self.on_changed:
                self.on_changed()
        else:
            self.test_result.config(
                text=f"✗ 测试失败：{(data or {}).get('detail', f'HTTP {code}')}", fg=STOP)


# ===== 主 Chat 应用 =====
class ChatApp:
    def __init__(self, root: tk.Tk, base_url: str):
        self.root = root
        self.base_url = base_url
        self.llm_port = 8001
        self.llm_url = f"http://127.0.0.1:{self.llm_port}"
        self.quit_flag = False
        self._tasks: queue.Queue = queue.Queue()
        self._results: queue.Queue = queue.Queue()
        self._daemon_ok = False
        self._llm_ok = False
        self._llm_model = ""
        self._daemon_err_fh = None

        self._bold_font = font.Font(family="Segoe UI", size=10, weight="bold")
        self._normal_font = font.Font(family="Segoe UI", size=10)
        self._mono_font = font.Font(family="Consolas", size=9)

        # 多会话：{sid: {"messages": [...], "history": [(role, text), ...]}}
        self._sessions: dict[int, dict] = {}
        self._current_sid = 1
        self._streaming = False          # 流式输出进行中
        self._stream_content_acc = ""
        self._stream_reasoning_acc = ""
        self._stream_handle = None
        self._sse_event = ""
        # 首个会话在 _build_ui 完成后创建（需要 UI 组件已就绪）

        threading.Thread(target=self._bg_loop, daemon=True).start()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._start)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.root.title("PC Agent Chat")
        self.root.geometry("960x720")
        self.root.configure(bg=BG)
        self.root.minsize(700, 500)

        # 顶部工具栏
        toolbar = tk.Frame(self.root, bg=PANEL, height=56)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)

        tk.Label(toolbar, text="◆ PC Agent Chat", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 15, "bold")).pack(side="left", padx=18, pady=12)

        self.status_dot = tk.Label(toolbar, text="●", bg=PANEL, fg=STOP, font=("Segoe UI", 12))
        self.status_dot.pack(side="left", padx=(4, 4))
        self.status_text = tk.Label(toolbar, text="Daemon connecting...", bg=PANEL,
                                    fg=TEXT_DIM, font=("Segoe UI", 10))
        self.status_text.pack(side="left")

        # 右侧按钮组
        self._toolbar_btn(toolbar, "Settings", self._open_settings).pack(side="right", padx=(0, 14), pady=12)
        self._toolbar_btn(toolbar, "Open Screen Backend", self._open_screen_backend,
                          accent=True).pack(side="right", padx=(0, 8), pady=12)

        # 主体
        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=12, pady=12)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        # 聊天区
        chat_card = tk.Frame(main, bg=PANEL, bd=0)
        chat_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        chat_card.rowconfigure(0, weight=1)
        chat_card.columnconfigure(0, weight=1)

        # 自定义内嵌框架的 Canvas
        self.canvas = tk.Canvas(chat_card, bg=PANEL, highlightthickness=0, bd=0)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
        vbar = ttk.Scrollbar(chat_card, orient="vertical", command=self.canvas.yview)
        vbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=vbar.set)

        self.scroll_frame = tk.Frame(self.canvas, bg=PANEL)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.scroll_frame,
                                                        anchor="nw", width=540)
        self.scroll_frame.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        # 输入区
        input_card = tk.Frame(main, bg=PANEL, bd=0)
        input_card.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        input_card.columnconfigure(0, weight=1)

        self.input_box = tk.Text(input_card, bg=BG, fg=TEXT, relief="flat",
                                 font=("Segoe UI", 11), height=3, wrap="word",
                                 insertbackground=TEXT, padx=12, pady=10)
        self.input_box.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        self.input_box.bind("<Return>", self._on_return)
        self.input_box.bind("<Shift-Return>", lambda e: None)

        self.send_btn = self._toolbar_btn(input_card, "Send", self._send_message,
                                          accent=True)
        self.send_btn.grid(row=0, column=1, padx=(0, 8), pady=8)

        # 侧边栏
        side = tk.Frame(main, bg=PANEL, width=250, bd=0)
        side.grid(row=0, column=1, rowspan=2, sticky="ns")
        side.pack_propagate(False)

        self._side_section(side, "Session")
        btn_row = tk.Frame(side, bg=PANEL)
        btn_row.pack(fill="x", padx=14, pady=(0, 6))
        self._toolbar_btn(btn_row, "New Session", self._new_session,
                          accent=True).pack(side="left")
        # 会话列表（点击切换，当前会话高亮）
        self.session_list = tk.Frame(side, bg=PANEL)
        self.session_list.pack(fill="x", padx=14, pady=(0, 6))
        self._session_buttons: dict[int, tk.Button] = {}
        self.session_info = tk.Label(side, text="", bg=PANEL, fg=TEXT_DIM, justify="left",
                                     font=("Segoe UI", 9))
        self.session_info.pack(anchor="w", padx=14, pady=(0, 10))

        self._side_section(side, "LLM")
        self.llm_status = tk.Label(side, text="connecting...", bg=PANEL, fg=TEXT_DIM,
                                   justify="left", wraplength=210,
                                   font=("Segoe UI", 9))
        self.llm_status.pack(anchor="w", padx=14, pady=(0, 14))

        self._side_section(side, "Tools")
        for name, desc in [("repo_map", "project structure index"),
                           ("search_text", "code search / locate"),
                           ("replace_text", "precise edit + diff"),
                           ("git", "status / diff / commit"),
                           ("process", "background processes"),
                           ("todo", "task planning"),
                           ("screen", "capture / click / type"),
                           ("stop", "emergency kill-switch")]:
            row = tk.Frame(side, bg=PANEL)
            row.pack(fill="x", padx=14, pady=2)
            tk.Label(row, text=f"• {name}", bg=PANEL, fg=ACCENT,
                     font=("Consolas", 9, "bold"), width=12, anchor="w").pack(side="left")
            tk.Label(row, text=desc, bg=PANEL, fg=TEXT_DIM,
                     font=("Segoe UI", 8), wraplength=130, justify="left").pack(side="left")

        self._side_section(side, "Task")
        self.todo_frame = tk.Frame(side, bg=PANEL)
        self.todo_frame.pack(fill="x", padx=14, pady=(0, 8))
        tk.Label(self.todo_frame, text="(暂无任务)", bg=PANEL, fg=TEXT_DIM,
                 font=("Segoe UI", 9)).pack(anchor="w")

        self._side_section(side, "Log")
        self.log_text = tk.Text(side, bg=BG, fg=TEXT_DIM, relief="flat",
                                font=("Consolas", 8), height=10, state="disabled",
                                wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        # 会话初始为空，由 _start 后异步从 LLM 后端加载恢复（失败降级本地创建）
        self._sessions: dict[int, dict] = {}
        self._current_sid = 0
        self._server_sessions_loaded = False

    def _toolbar_btn(self, parent, text: str, cmd, accent: bool = False) -> tk.Button:
        bgc = ACCENT if accent else PANEL_LIGHT
        fgc = "white" if accent else TEXT
        hover_bg = ACCENT_HOVER if accent else "#262c36"
        btn = tk.Button(parent, text=text, command=cmd, bg=bgc, fg=fgc,
                        activebackground=hover_bg, activeforeground="white",
                        relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
                        font=("Segoe UI", 9, "bold"))
        btn.bind("<Enter>", lambda e: btn.config(bg=hover_bg))
        btn.bind("<Leave>", lambda e: btn.config(bg=bgc))
        return btn

    def _side_section(self, parent, title: str) -> None:
        tk.Label(parent, text=title, bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=14, pady=(14, 8))

    # ------------------------------------------------------------------ 布局事件
    def _on_frame_configure(self, event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(1.0)

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfig(self.canvas_window, width=event.width - 4)

    # ------------------------------------------------------------------ 后台线程
    def _bg_loop(self) -> None:
        while not self.quit_flag:
            try:
                kind, fn = self._tasks.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                result = fn()
            except Exception as exc:
                result = ("err", str(exc))
            self._results.put((kind, result))
            self.root.after(0, self._drain_results)

    def _drain_results(self) -> None:
        while True:
            try:
                kind, payload = self._results.get_nowait()
            except queue.Empty:
                return
            handler = {
                "start": self._on_start,
                "status": self._on_status,
                "llm": self._on_llm_status,
                "llm_status": self._on_llm_status_result,
                "sessions": self._on_sessions_loaded,
                "stream_delta": self._on_stream_delta,
                "stream_done": self._on_stream_done,
                "stream_error": self._on_stream_error,
                "stream_tool_call": self._on_stream_tool_call,
                "stream_tool_result": self._on_stream_tool_result,
                "stream_ask": self._on_stream_ask,
                "stream_todo": self._on_stream_todo,
            }.get(kind)
            if handler:
                handler(payload)

    # ------------------------------------------------------------------ 启动
    def _start(self) -> None:
        self._tasks.put(("start", self._ensure_daemon))
        self._tasks.put(("llm", self._ensure_llm_server))

    def _ensure_daemon(self):
        def probe():
            code, data, _ = api_request(self.base_url, "GET", "/api/v1/status", timeout=3)
            return code == 200 and data.get("daemon") == "running"
        if probe():
            return ("ok", "connected to running Daemon")
        port = urllib.request.urlparse(self.base_url).port or 8000
        python = self._pick_daemon_python()
        self._log(f"auto-starting Daemon with {Path(python).name} on port {port} ...", "info")
        self._daemon_err_fh = open(BASE_DIR.parent / "daemon.err.log", "a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                [python, str(BASE_DIR / "app.py"), "--port", str(port)],
                cwd=str(BASE_DIR), creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=self._daemon_err_fh,
            )
        except Exception as exc:
            self._daemon_err_fh.close()
            return ("err", f"failed to start Daemon: {exc}")
        for _ in range(60):
            time.sleep(0.5)
            if probe():
                return ("ok", "Daemon auto-started and ready")
            if proc.poll() is not None:
                return ("err", f"Daemon exited: {self._read_daemon_err()}")
        return ("err", "timeout waiting for Daemon (30s)")

    @staticmethod
    def _pick_daemon_python() -> str:
        candidates = [sys.executable, str(BASE_DIR.parent / ".venv" / "Scripts" / "python.exe")]
        probe_code = "import importlib.util;print(all(importlib.util.find_spec(m) for m in ('fastapi','pyautogui','uvicorn')))"
        for cand in dict.fromkeys(candidates):
            if not Path(cand).exists():
                continue
            try:
                out = subprocess.run([cand, "-c", probe_code], capture_output=True, text=True, timeout=10)
                if "True" in out.stdout:
                    return cand
            except Exception:
                continue
        return sys.executable

    def _read_daemon_err(self) -> str:
        try:
            if self._daemon_err_fh is not None:
                self._daemon_err_fh.close()
            lines = BASE_DIR.joinpath("daemon.err.log").read_text(encoding="utf-8",
                                                                  errors="replace").strip().splitlines()
            return " | ".join(lines[-3:])[-400:] if lines else "no stderr"
        except Exception:
            return "cannot read daemon.err.log"

    # ------------------------------------------------------------------ LLM 后端
    def _ensure_llm_server(self):
        """探测本地 LLM 后端（llm_server.py，端口 8001）；未运行则自动拉起。"""
        def probe():
            code, data, _ = api_request(self.llm_url, "GET", "/api/v1/health", timeout=3)
            return code == 200 and data.get("ok")
        if probe():
            return ("ok", "LLM backend ready")
        python = self._pick_daemon_python().replace("python.exe", "pythonw.exe")
        self._log(f"auto-starting LLM backend with {Path(python).name} ...", "info")
        try:
            proc = subprocess.Popen(
                [python, str(BASE_DIR / "llm_server.py"), "--port", str(self.llm_port)],
                cwd=str(BASE_DIR), creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            return ("err", f"failed to start LLM backend: {exc}")
        for _ in range(60):
            time.sleep(0.5)
            if probe():
                return ("ok", "LLM backend auto-started")
            if proc.poll() is not None:
                return ("err", "LLM backend exited unexpectedly")
        return ("err", "timeout waiting for LLM backend (30s)")

    def _on_llm_status(self, payload) -> None:
        kind, msg = payload
        self._log(f"LLM backend: {msg}", kind)
        if kind == "ok":
            self._tasks.put(("llm_status", self._refresh_llm_status))
        elif not self._server_sessions_loaded:
            # LLM 后端起不来：降级为纯内存会话
            self._server_sessions_loaded = True
            if not self._sessions:
                self._new_session()

    def _refresh_llm_status(self):
        code, data, _ = api_request(self.llm_url, "GET", "/api/v1/health")
        if code != 200:
            return ("err", {})
        return ("ok", data)

    def _on_llm_status_result(self, payload) -> None:
        kind, data = payload
        if kind == "ok" and isinstance(data, dict):
            self._apply_llm_status(data)

    def _apply_llm_status(self, data: dict) -> None:
        self._llm_ok = True
        self._llm_model = data.get("model", "")
        if not self._server_sessions_loaded:
            # LLM 后端就绪：拉取持久化会话（只触发一次）
            self._server_sessions_loaded = True
            self._tasks.put(("sessions", self._load_sessions_from_server))
        cfg = load_config()
        if data.get("configured"):
            self.llm_status.config(
                text=f"ready · {data.get('model')}\n{data.get('api_url')}",
                fg=OK)
        else:
            self.llm_status.config(
                text="未配置 API。\n点击顶部 Settings 填写 API URL / Key / Model。",
                fg=WARN)

    def _on_start(self, payload) -> None:
        kind, msg = payload
        self._log(msg, "ok" if kind == "ok" else "err")
        if kind == "ok":
            self._daemon_ok = True
            self._set_status(True)
            self._tasks.put(("status", lambda: api_request(self.base_url, "GET", "/api/v1/status")))
            self.root.after(2000, self._tick_status)
        else:
            self._set_status(False)

    # ------------------------------------------------------------------ 会话持久化
    def _load_sessions_from_server(self):
        """后台线程：从 LLM 后端拉取全部会话（含消息）用于恢复。"""
        code, data, _ = api_request(self.llm_url, "GET", "/api/v1/sessions", timeout=8)
        if code != 200:
            return ("err", "无法连接 LLM 后端，本次会话仅保存在内存（重启丢失）")
        return ("ok", data.get("sessions") or [])

    def _on_sessions_loaded(self, payload) -> None:
        """主线程：用后端会话重建本地状态（后端为权威源）。"""
        kind, msg = payload
        if kind == "err":
            self._log(msg, "err")
            if not self._sessions:
                self._new_session()
            return
        sessions = msg
        if not sessions:
            if not self._sessions:
                self._new_session()
            return
        self._sessions.clear()
        for s in sessions:
            sid = int(s["id"])
            msgs = [dict(m) for m in (s.get("messages") or [])]
            self._sessions[sid] = {
                "messages": [{"role": "system", "content": SYSTEM_FIRST}] + msgs,
                "history": [(m["role"], m["content"]) for m in msgs],
                "title": s.get("title") or "",
            }
        self._current_sid = 0
        self._switch_session(max(self._sessions))
        self._log(f"已恢复 {len(self._sessions)} 个持久化会话", "ok")

    def _append_to_server(self, msgs: list[dict]) -> None:
        """后台线程：把新增消息追加到后端会话（幂等失败，不阻塞 UI）。"""
        sid = self._current_sid
        def _do():
            api_request(self.llm_url, "POST", f"/api/v1/sessions/{sid}/messages",
                        {"messages": msgs}, timeout=5)
        self._tasks.put(("sess_append", _do))

    def _tick_status(self) -> None:
        if not self.quit_flag:
            self._tasks.put(("status", lambda: api_request(self.base_url, "GET", "/api/v1/status")))
            self.root.after(2000, self._tick_status)

    # ------------------------------------------------------------------ 消息
    def _on_return(self, event) -> str:
        if not event.state & 0x1:
            self._send_message()
            return "break"
        return None

    def _send_message(self) -> None:
        if self._streaming:
            return
        text = self.input_box.get("1.0", "end-1c").strip()
        if not text:
            return
        # 防御：会话尚未初始化（后端未就绪）时先建一个
        if not self._sessions:
            self._new_session()
        self._add_message("user", text)
        self.input_box.delete("1.0", "end")

        sess = self._sessions[self._current_sid]
        sess["history"].append(("user", text))
        sess["messages"].append({"role": "user", "content": text})
        snapshot = list(sess["messages"])   # 快照，后台线程不碰 UI 状态
        self._append_to_server([{"role": "user", "content": text}])   # 持久化 user 消息

        # 流式输出初始化
        self._streaming = True
        self._stream_content_acc = ""
        self._stream_reasoning_acc = ""
        self._agent_log: list[str] = []          # 工具调用日志行（渲染在回复前）
        self._stream_handle = self._add_message("agent", "◌ 思考中…")
        # Send → Stop（流式期间可中止）
        self.send_btn.config(state="normal", text="⏹ Stop", bg=STOP,
                             command=self._stop_stream)
        self._tasks.put(("stream", lambda: self._do_chat_stream(snapshot, self._stream_handle)))

    def _stop_stream(self) -> None:
        """前端中止：关闭 SSE 连接，后端收到断开后停止循环线程。"""
        if not self._streaming:
            return
        self._streaming = False
        if self._stream_resp is not None:
            try:
                self._stream_resp.close()
            except Exception:
                pass
        if self._stream_handle is not None:
            self._update_message(self._stream_handle, "⏹ 已由用户中止")
        self._log("agent task stopped by user", "err")
        self._finish_streaming()

    # ------------------------------------------------------------------ 流式
    def _do_chat_stream(self, snapshot: list[dict], handle) -> None:
        """后台线程：SSE 流式读取（agent 模式启用工具调用循环），逐块分发到主线程。"""
        url = f"{self.llm_url}/api/v1/chat/stream"
        req = urllib.request.Request(
            url, data=json.dumps({"messages": snapshot, "agent": True},
                                 ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        self._streaming = True
        self._stream_resp = None
        self._stop_called = False
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                self._stream_resp = resp
                for raw in resp:
                    if not self._streaming:   # 用户已点 Stop
                        return
                    kind, payload = self._parse_sse_chunk(raw.decode("utf-8", "replace"))
                    if kind == "delta":
                        self._results.put(("stream_delta", (handle, payload)))
                    elif kind == "tool_call":
                        self._results.put(("stream_tool_call", (handle, payload)))
                    elif kind == "tool_result":
                        self._results.put(("stream_tool_result", (handle, payload)))
                    elif kind == "ask":
                        self._results.put(("stream_ask", (handle, payload)))
                    elif kind == "todo_update":
                        self._results.put(("stream_todo", payload))
                    elif kind == "error":
                        self._results.put(("stream_error", (handle, payload)))
                        return
                    elif kind == "done":
                        break
            if self._streaming:
                self._results.put(("stream_done", (handle, None)))
        except Exception as exc:
            if self._streaming:   # 用户 Stop 导致的连接中断不报错
                self._results.put(("stream_error", (handle, f"无法连接 LLM 后端：{exc}")))

    def _parse_sse_chunk(self, line: str):
        """解析一行 SSE：返回 (kind, payload)。
        kind: delta / tool_call / tool_result / done / error / None（忽略）
        """
        line = line.strip()
        if not line:
            return None, None
        if line.startswith("event:"):
            self._sse_event = line[6:].strip()
            return None, None
        if not line.startswith("data:"):
            return None, None
        payload = line[5:].strip()
        if self._sse_event == "error":
            self._sse_event = ""
            try:
                return "error", json.loads(payload).get("detail", payload)
            except Exception:
                return "error", payload
        if self._sse_event == "tool_call":
            self._sse_event = ""
            try:
                return "tool_call", json.loads(payload)
            except Exception:
                return "tool_call", payload
        if self._sse_event == "tool_result":
            self._sse_event = ""
            try:
                return "tool_result", json.loads(payload)
            except Exception:
                return "tool_result", payload
        if self._sse_event == "ask":
            self._sse_event = ""
            try:
                return "ask", json.loads(payload)
            except Exception:
                return "ask", payload
        if self._sse_event == "todo_update":
            self._sse_event = ""
            try:
                return "todo_update", json.loads(payload)
            except Exception:
                return "todo_update", payload
        if payload == "[DONE]":
            return "done", None
        try:
            data = json.loads(payload)
            delta = (data.get("choices") or [{}])[0].get("delta") or {}
            return "delta", (delta.get("content") or "", delta.get("reasoning_content") or "")
        except Exception:
            return None, None

    def _render_agent_log(self, handle) -> None:
        """把工具调用日志 + 已累积内容渲染进气泡。"""
        text = ""
        if self._agent_log:
            text = "\n".join(self._agent_log) + "\n\n"
        text += self._stream_content_acc
        self._update_message(handle, (text + "▍") if self._stream_content_acc else "◌ 思考中…")

    def _on_stream_tool_call(self, payload) -> None:
        handle, data = payload
        if handle is not self._stream_handle:
            return
        try:
            args = json.loads(data.get("arguments") or "{}")
            arg_str = ", ".join(f"{k}={v}" for k, v in args.items()) or "—"
        except Exception:
            arg_str = data.get("arguments", "—")
        step_info = f" · 轮次 {data.get('step')}/{data.get('max_steps')}" if data.get("step") else ""
        self._agent_log.append(f"`[⚙ {data.get('name')}]` {arg_str}{step_info}")
        self._log(f"tool call: {data.get('name')} {arg_str}{step_info}", "info")
        self._render_agent_log(handle)

    def _on_stream_tool_result(self, payload) -> None:
        handle, data = payload
        if handle is not self._stream_handle:
            return
        result = (data.get("result") or "")[:150]
        mark = "✓" if data.get("ok") else "✗"
        self._agent_log.append(f"  {mark} {result}")
        self._log(f"tool result: {mark} {result}", "ok" if data.get("ok") else "err")
        self._render_agent_log(handle)

    def _on_stream_ask(self, payload) -> None:
        """模型请求确认：弹确认窗（含 diff 展示），用户选择后回传服务端。"""
        handle, data = payload
        if handle is not self._stream_handle:
            return
        # ask 串行发出，防御性关闭残留窗口
        if getattr(self, "_confirm_win", None) is not None:
            try:
                self._confirm_win.destroy()
            except Exception:
                pass
        self._show_confirm_window(data)

    def _show_confirm_window(self, data: dict) -> None:
        win = tk.Toplevel(self.root)
        win.title("需要确认")
        win.geometry("660x460")
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()
        self._confirm_win = win

        header = tk.Frame(win, bg=PANEL, height=44)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="⚠ 操作确认", bg=PANEL, fg=WARN,
                 font=("Segoe UI", 13, "bold")).pack(side="left", padx=14, pady=8)
        tool_name = data.get("name") or ""
        tk.Label(header, text=f"工具: {tool_name}", bg=PANEL, fg=TEXT_DIM,
                 font=("Consolas", 9)).pack(side="right", padx=14, pady=8)

        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=12)

        tk.Label(body, text=data.get("question") or "确认执行该操作吗？",
                 bg=BG, fg=TEXT, justify="left", wraplength=600,
                 font=("Segoe UI", 11)).pack(anchor="w", pady=(0, 8))

        diff = data.get("diff")
        if diff:
            tk.Label(body, text="改动预览（diff）:", bg=BG, fg=TEXT_DIM,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(6, 2))
            diff_frame = tk.Frame(body, bg=CODE_BG)
            diff_frame.pack(fill="both", expand=True)
            box = tk.Text(diff_frame, bg=CODE_BG, fg=CODE_FG, relief="flat",
                          font=("Consolas", 9), height=12, wrap="none")
            vbar = ttk.Scrollbar(diff_frame, orient="vertical", command=box.yview)
            hbar = ttk.Scrollbar(diff_frame, orient="horizontal", command=box.xview)
            box.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
            box.grid(row=0, column=0, sticky="nsew")
            vbar.grid(row=0, column=1, sticky="ns")
            hbar.grid(row=1, column=0, sticky="ew")
            diff_frame.rowconfigure(0, weight=1)
            diff_frame.columnconfigure(0, weight=1)
            box.insert("1.0", diff)
            box.config(state="disabled")

        btns = tk.Frame(win, bg=BG)
        btns.pack(fill="x", padx=16, pady=(0, 14))
        request_id = data.get("id", "")

        def choose(choice: str) -> None:
            self._send_respond(request_id, choice)
            win.destroy()

        tk.Button(btns, text="✓ 允许", command=lambda: choose("yes"),
                  bg=OK, fg="white", activebackground="#16a34a",
                  activeforeground="white", relief="flat", bd=0,
                  padx=24, pady=8, cursor="hand2",
                  font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Button(btns, text="✗ 拒绝", command=lambda: choose("no"),
                  bg=STOP, fg="white", activebackground="#b91c1c",
                  activeforeground="white", relief="flat", bd=0,
                  padx=24, pady=8, cursor="hand2",
                  font=("Segoe UI", 10, "bold")).pack(side="left", padx=10)
        tk.Button(btns, text="关闭（按拒绝处理）", command=lambda: choose("no"),
                  bg=PANEL_LIGHT, fg=TEXT_DIM, activebackground="#262c36",
                  activeforeground=TEXT_DIM, relief="flat", bd=0,
                  padx=14, pady=8, cursor="hand2",
                  font=("Segoe UI", 9)).pack(side="right")
        win.focus_force()

    def _send_respond(self, request_id: str, choice: str) -> None:
        """把确认选择回传 llm_server（后台线程执行，不阻塞 UI）。"""
        def _do():
            api_request(self.llm_url, "POST", "/api/v1/agent/respond",
                        {"request_id": request_id, "choice": choice}, timeout=10)
        self._tasks.put(("respond", _do))

    def _on_stream_todo(self, payload) -> None:
        """刷新侧边栏任务面板（todo_update 事件）。"""
        todos = (payload or {}).get("todos") or []
        for w in self.todo_frame.winfo_children():
            w.destroy()
        if not todos:
            tk.Label(self.todo_frame, text="(暂无任务)", bg=PANEL, fg=TEXT_DIM,
                     font=("Segoe UI", 9)).pack(anchor="w")
            return
        colors = {"pending": WARN, "in_progress": ACCENT, "completed": OK,
                  "failed": STOP, "cancelled": TEXT_DIM}
        for t in todos[-15:]:
            row = tk.Frame(self.todo_frame, bg=PANEL)
            row.pack(fill="x", pady=1)
            st = t.get("status", "pending")
            tk.Label(row, text=f"[{st}]", bg=PANEL, fg=colors.get(st, WARN),
                     font=("Consolas", 8, "bold")).pack(side="left")
            tk.Label(row, text=t.get("title", ""), bg=PANEL, fg=TEXT,
                     font=("Segoe UI", 9), wraplength=185, justify="left").pack(side="left", padx=4)

    def _on_stream_delta(self, payload) -> None:
        handle, (content, reasoning) = payload
        if handle is not self._stream_handle:
            return
        if reasoning:
            self._stream_reasoning_acc += reasoning
        if content:
            self._stream_content_acc += content
            self._render_agent_log(handle)

    def _on_stream_done(self, payload) -> None:
        handle, _ = payload
        if handle is not self._stream_handle:
            return
        content = self._stream_content_acc.strip()
        reasoning = self._stream_reasoning_acc.strip()
        if content or self._agent_log:
            full_text = ("\n".join(self._agent_log) + "\n\n" + content) if self._agent_log else content
            self._update_message(handle, full_text)
            if reasoning:
                self._render_thinking(handle, reasoning)
                self._on_frame_configure()
            sess = self._sessions[self._current_sid]
            sess["history"].append(("agent", full_text))          # 展示用（含工具日志）
            sess["messages"].append({"role": "assistant", "content": content})  # 模型上下文（纯回复，省 token）
            self._append_to_server([{"role": "assistant", "content": content}])  # 持久化回复
            self._log("agent reply complete", "ok")
        else:
            self._update_message(handle, "⚠ 模型未返回内容，请重试。")
            self._log("agent reply empty", "err")
        self._finish_streaming()

    def _on_stream_error(self, payload) -> None:
        handle, detail = payload
        if handle is not self._stream_handle:
            return
        self._update_message(handle, f"⚠ {detail}\n\n请检查 Settings 中的 API 配置。")
        self._log(f"stream error: {detail}", "err")
        self._finish_streaming()

    def _finish_streaming(self) -> None:
        self._streaming = False
        self._stream_handle = None
        self._stream_resp = None
        # Stop → Send
        self.send_btn.config(state="normal", text="Send", bg=ACCENT,
                             command=self._send_message)
        self._update_session_sidebar()

    def _add_message(self, role: str, text: str) -> None:
        is_user = role == "user"
        bubble_bg = USER_BUBBLE if is_user else AGENT_BUBBLE
        fg = "white" if is_user else TEXT

        container = tk.Frame(self.scroll_frame, bg=PANEL)
        container.pack(fill="x", padx=10, pady=6)

        # 左侧/右侧对齐容器
        align = tk.Frame(container, bg=PANEL)
        align.pack(side="right" if is_user else "left", anchor="e" if is_user else "w")

        # 头像 + 名称行
        meta = tk.Frame(align, bg=PANEL)
        meta.pack(anchor="w" if not is_user else "e", fill="x")
        label = "You" if is_user else "Agent"
        tk.Label(meta, text=label, bg=PANEL, fg=ACCENT if is_user else TEXT_DIM,
                 font=("Segoe UI", 9, "bold")).pack(side="left" if not is_user else "right")
        tk.Label(meta, text=time.strftime("%H:%M"), bg=PANEL, fg=TEXT_DIM,
                 font=("Segoe UI", 8)).pack(side="left" if not is_user else "right", padx=6)

        # 气泡主体
        bubble = tk.Frame(align, bg=bubble_bg, bd=0)
        bubble.pack(anchor="e" if is_user else "w", pady=(2, 0))

        self._render_text(bubble, text, fg)
        self._on_frame_configure()
        return bubble

    def _update_message(self, handle: tk.Frame, new_text: str) -> None:
        """原位更新已渲染的消息气泡（用于 thinking → 完整回复）。"""
        for child in handle.winfo_children():
            child.destroy()
        self._render_text(handle, new_text, TEXT)
        self._on_frame_configure()

    def _render_text(self, parent: tk.Frame, text: str, fg: str) -> None:
        """渲染简化 Markdown：```代码块```、**粗体**、`行内代码`、普通段落。"""
        # 按 ``` 切分：偶数段为普通文本，奇数段为 (lang, code)
        parts = re.split(r"```(\w*)\n?(.*?)```", text, flags=re.S)
        for i in range(0, len(parts), 3):
            self._render_paragraph(parent, parts[i], fg)
            if i + 2 < len(parts):
                lang = parts[i + 1] or "code"
                code = parts[i + 2].rstrip("\n")
                self._render_code_block(parent, code, lang)

    def _render_paragraph(self, parent: tk.Frame, text: str, fg: str) -> None:
        """按行渲染普通段落，支持 **bold** 与 `code` 内联样式。"""
        lines = text.splitlines()
        for li, line in enumerate(lines):
            if li > 0:
                tk.Label(parent, text="", bg=parent["bg"]).pack(anchor="w")
            self._render_line(parent, line, fg)

    def _render_code_block(self, parent: tk.Frame, code: str, lang: str) -> None:
        """深色代码卡片：语言标签 + 等宽代码内容。"""
        box = tk.Frame(parent, bg=CODE_BG, highlightthickness=1,
                       highlightbackground=BORDER)
        box.pack(fill="x", padx=10, pady=(6, 2))
        # 语言标签
        tk.Label(box, text=lang, bg=CODE_BG, fg=ACCENT,
                 font=("Consolas", 8, "bold")).pack(anchor="w", padx=10, pady=(4, 0))
        # 代码内容（超长行折行显示，防止撑爆气泡）
        code_lbl = tk.Label(box, text=code, bg=CODE_BG, fg=CODE_FG, justify="left",
                            anchor="w", wraplength=500, font=("Consolas", 9))
        code_lbl.pack(fill="x", padx=10, pady=(2, 6))

    def _render_thinking(self, parent: tk.Frame, reasoning: str) -> None:
        """可折叠的思考过程区（reasoning_content）。"""
        state = {"open": False}
        body = tk.Frame(parent, bg=CODE_BG)
        btn = tk.Button(parent, text="▶ 思考过程", command=lambda: _toggle(),
                        bg=parent["bg"], fg=TEXT_DIM, relief="flat", bd=0,
                        cursor="hand2", anchor="w", font=("Segoe UI", 8))
        btn.pack(anchor="w", pady=(4, 0), padx=2)

        def _toggle():
            state["open"] = not state["open"]
            btn.config(text=("▼ 思考过程" if state["open"] else "▶ 思考过程"))
            if state["open"]:
                for child in body.winfo_children():
                    child.destroy()
                tk.Label(body, text=reasoning.strip(), bg=CODE_BG, fg=TEXT_DIM,
                         justify="left", wraplength=420, font=("Segoe UI", 9)).pack(
                    fill="x", padx=10, pady=6)
                body.pack(fill="x", padx=10, pady=(0, 2))
            else:
                body.pack_forget()
            parent.update_idletasks()
            self._on_frame_configure()

    def _render_line(self, parent: tk.Frame, line: str, fg: str) -> None:
        """把一行按 **bold** 和 `code` 切分渲染。"""
        parts = []
        i = 0
        while i < len(line):
            if line.startswith("**", i):
                j = line.find("**", i + 2)
                if j != -1:
                    parts.append(("bold", line[i + 2:j]))
                    i = j + 2
                    continue
            if line.startswith("`", i):
                j = line.find("`", i + 1)
                if j != -1:
                    parts.append(("code", line[i + 1:j]))
                    i = j + 1
                    continue
            # 普通字符累积
            j = i
            while j < len(line) and not line.startswith("**", j) and line[j] != "`":
                j += 1
            parts.append(("normal", line[i:j]))
            i = j

        row = tk.Frame(parent, bg=parent["bg"])
        row.pack(anchor="w", padx=12, pady=(4, 0))
        for kind, txt in parts:
            if not txt:
                continue
            fnt = self._mono_font if kind == "code" else (
                self._bold_font if kind == "bold" else self._normal_font)
            bgc = "#0d1117" if kind == "code" else parent["bg"]
            tk.Label(row, text=txt, bg=bgc, fg=fg, font=fnt).pack(side="left")

    # ------------------------------------------------------------------ 会话
    def _new_session(self) -> None:
        """创建新会话并切换过去（后端登记持久化；失败降级本地自增）。"""
        sid = self._create_server_session()
        self._sessions[sid] = {
            "messages": [{"role": "system", "content": SYSTEM_FIRST}],
            "history": [],   # [(role, text), ...] 已展示的对话
            "title": "",
        }
        self._switch_session(sid)

    def _create_server_session(self) -> int:
        code, data, _ = api_request(self.llm_url, "POST", "/api/v1/sessions", timeout=5)
        if code == 200 and isinstance(data.get("id"), int):
            return data["id"]
        return (max(self._sessions) + 1) if self._sessions else 1

    def _switch_session(self, sid: int) -> None:
        if self._streaming or sid not in self._sessions:
            return
        self._current_sid = sid
        sess = self._sessions[sid]
        # 重建消息区
        for child in self.scroll_frame.winfo_children():
            child.destroy()
        for role, text in sess["history"]:
            self._add_message(role, text)
        if not sess["history"]:
            cfg = load_config()
            self._add_message("agent", f"会话 #{sid} 已开始。\n模型: `{cfg.get('model')}`\n\n有什么需要帮忙的？")
        self._update_session_sidebar()

    def _update_session_sidebar(self) -> None:
        # 全量销毁重建：每行含切换按钮 + ✕ 删除按钮，必须全部销毁
        # （否则残留的旧 ✕ 按钮会引用已删除的会话，点击即 KeyError）
        for child in self.session_list.winfo_children():
            child.destroy()
        self._session_buttons.clear()
        for sid in sorted(self._sessions, reverse=True):
            is_current = sid == self._current_sid
            bgc = ACCENT if is_current else PANEL_LIGHT
            row = tk.Frame(self.session_list, bg=bgc)
            row.pack(fill="x", pady=2)
            title = self._sessions[sid].get("title") or f"会话 #{sid}"
            btn = tk.Button(row, text=title[:14], bg=bgc,
                            fg="white" if is_current else TEXT, relief="flat", bd=0,
                            anchor="w", padx=10, pady=4, cursor="hand2",
                            font=("Segoe UI", 9),
                            command=lambda s=sid: self._switch_session(s))
            btn.pack(side="left", fill="x", expand=True)
            del_btn = tk.Button(row, text="✕", bg=bgc, fg="#f87171",
                                relief="flat", bd=0, padx=8, pady=4, cursor="hand2",
                                activebackground=STOP, activeforeground="white",
                                font=("Segoe UI", 9, "bold"),
                                command=lambda s=sid: self._delete_session(s))
            del_btn.pack(side="right")
            self._session_buttons[sid] = btn
        cfg = load_config()
        self.session_info.config(
            text=f"当前: 会话 #{self._current_sid}\n模型: {cfg.get('model')}\n消息: "
                 f"{len(self._sessions[self._current_sid]['history'])} 条")
        self._log(f"switched to session #{self._current_sid}", "info")

    def _delete_session(self, sid: int) -> None:
        """删除会话；删除当前会话时自动切换到剩余会话，删空则自动新建。"""
        if self._streaming or sid not in self._sessions:
            return
        if not messagebox.askyesno("删除会话", f"确定删除该会话？此操作不可恢复。"):
            return
        # 同步删除后端持久化（后台执行，失败静默——本地删除照常进行）
        def _do():
            api_request(self.llm_url, "DELETE", f"/api/v1/sessions/{sid}", timeout=5)
        self._tasks.put(("sess_delete", _do))
        del self._sessions[sid]
        self._log(f"deleted session #{sid}", "info")
        if self._current_sid == sid:
            remaining = sorted(self._sessions)
            if remaining:
                self._switch_session(remaining[-1])   # 切到最近创建的会话
            else:
                self._new_session()                   # 删空：自动新建
        else:
            self._update_session_sidebar()

    # ------------------------------------------------------------------ 工具按钮
    def _open_settings(self) -> None:
        SettingsWindow(self.root, on_changed=self._settings_changed)

    def _settings_changed(self) -> None:
        """Settings 保存 / 连接测试成功后：刷新侧边栏 LLM 状态。"""
        if not self.quit_flag:
            self._tasks.put(("llm_status", self._refresh_llm_status))

    def _open_screen_backend(self) -> None:
        python = self._pick_daemon_python().replace("python.exe", "pythonw.exe")
        self._log(f"opening Screen Backend with {Path(python).name} ...", "info")
        try:
            subprocess.Popen(
                [python, str(BASE_DIR / "gui.py")],
                cwd=str(BASE_DIR), creationflags=CREATE_NO_WINDOW,
            )
            self._log("Screen Backend launched", "ok")
        except Exception as exc:
            self._log(f"failed to launch Screen Backend: {exc}", "err")

    # ------------------------------------------------------------------ 状态 / 日志
    def _on_status(self, result) -> None:
        code, data, _ = result
        if code != 200:
            self._set_status(False)
            return
        self._set_status(True, data.get("mode", "online"))

    def _set_status(self, ok: bool, mode: str = "") -> None:
        self.status_dot.config(fg=OK if ok else STOP)
        text = f"Daemon {mode}" if mode else ("online" if ok else "offline")
        self.status_text.config(text=text, fg=OK if ok else STOP)

    def _log(self, msg: str, kind: str = "info") -> None:
        colors = {"ok": OK, "err": STOP, "info": TEXT_DIM}
        self.log_text.config(state="normal")
        stamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{stamp}] {msg}\n", kind)
        for tag, color in colors.items():
            self.log_text.tag_configure(tag, foreground=color)
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def on_close(self) -> None:
        self.quit_flag = True
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="PC Agent Chat")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    root = tk.Tk()
    ChatApp(root, f"http://127.0.0.1:{args.port}")
    root.mainloop()


if __name__ == "__main__":
    main()
