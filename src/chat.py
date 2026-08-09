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

from PIL import Image, ImageTk

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR.parent / "chat_config.json"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# ===== 配色系统 =====
BG = "#04050c"
PANEL = "#0b1020"
PANEL_LIGHT = "#131b31"
BORDER = "#263657"
TEXT = "#edf5ff"
TEXT_DIM = "#8798b8"
ACCENT = "#6ee7ff"
ACCENT_HOVER = "#3aaed2"
USER_BUBBLE = "#153c69"
AGENT_BUBBLE = "#101a31"
STOP = "#ff667c"
OK = "#3ef0b5"
WARN = "#ffbf69"
CODE_BG = "#060a15"
CODE_FG = "#c9dcf7"
VIOLET = "#9d7bff"

# 会话首条 system 提示（本地结构发送用；后端只存 user/assistant 消息）
SYSTEM_FIRST = ("你是一个桌面 Agent 助手，可以控制用户的电脑"
                "（屏幕点击、输入、按键、截图）。回答尽量简洁、准确。")
# 发送前上下文压缩（与 cli/bot 对齐：超过窗口 60% 先压缩再发送，防 tokens 激增）
COMPRESS_THRESHOLD = 0.6
KEEP_RECENT = 8


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
def _summarize_result(result: str, ok: bool) -> str:
    """工具结果 UI 精简：成功显示 stdout 首行；失败只报简短原因，不刷原始 stderr。
    完整结果仍由后端回传模型，仅影响界面显示。"""
    try:
        d = json.loads(result)
    except Exception:
        return (result or "").strip()[:100]
    if isinstance(d, dict):
        if ok:
            out = (d.get("stdout") or "").strip()
            if out:
                lines = out.splitlines()
                return lines[0][:100] + (" …" if len(lines) > 1 else "")
            return "完成"
        if d.get("error"):
            return str(d["error"])[:100]
        rc = d.get("exit_code")
        return f"失败（exit {rc}）" if rc is not None else "失败"
    return (result or "").strip()[:100]


def _fmt_args(arguments: str, limit: int = 120) -> str:
    """工具调用参数显示：JSON 解析后截断（replace_text 的 old/new 等长文本不刷屏）。"""
    try:
        args = json.loads(arguments or "{}")
        text = ", ".join(f"{k}={v}" for k, v in args.items()) or "—"
    except Exception:
        text = str(arguments or "—")
    return text[:limit] + (" …" if len(text) > limit else "")


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
        "reasoning_mode": "max",   # 推理强度：max（最高）/ high（高）/ off（关闭）
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
        self.win.geometry("640x540")
        self.win.minsize(600, 500)
        self.win.configure(bg=BG)
        self.win.transient(parent)
        self.win.grab_set()
        self._build_ui()
        self.win.focus_force()

    def _build_ui(self) -> None:
        # 标题栏
        header = tk.Frame(self.win, bg="#090f20", height=84,
                          highlightthickness=1, highlightbackground=BORDER)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="CONNECTION CONFIGURATION", bg="#090f20", fg=ACCENT,
                 font=("Consolas", 9, "bold")).pack(anchor="w", padx=20, pady=(15, 0))
        tk.Label(header, text="模型与连接设置", bg="#090f20", fg=TEXT,
                 font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w", padx=20)

        body = tk.Frame(self.win, bg=BG, highlightthickness=1,
                        highlightbackground=BORDER)
        body.pack(fill="both", expand=True, padx=20, pady=16)
        body.columnconfigure(1, weight=1)

        # API URL
        self._row(body, 0, "API URL", "api_url")
        # API Key
        self._row(body, 1, "API Key", "api_key", show="*")
        # Model
        self._row(body, 2, "Model", "model")

        # 推理强度（DeepSeek v4 系列）
        _R_LABELS = {"max": "最高", "high": "高", "off": "关闭"}
        current_rm = self.config.get("reasoning_mode") or "max"
        tk.Label(body, text="推理强度", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI", 10, "bold")).grid(row=3, column=0, sticky="nw", pady=(14, 4))
        self.reasoning_var = tk.StringVar(value=_R_LABELS.get(current_rm, "最高"))
        opt = tk.OptionMenu(body, self.reasoning_var, *_R_LABELS.values())
        opt.config(bg=PANEL_LIGHT, fg=TEXT, relief="flat", highlightthickness=0,
                   font=("Segoe UI", 10), width=22, cursor="hand2")
        opt["menu"].config(bg=PANEL_LIGHT, fg=TEXT, font=("Segoe UI", 10))
        opt.grid(row=3, column=1, sticky="w", pady=(14, 4), padx=(12, 0))
        self._r_by_label = {v: k for k, v in _R_LABELS.items()}
        tk.Label(body, text="最高=reasoning_effort max · 高=high（默认平衡）\n"
                            "关闭=禁用思考（最快最省）",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 8), justify="left").grid(
            row=3, column=1, sticky="w", padx=(12, 0), pady=(40, 0))

        # 提示文本（支持各种 OpenAI 兼容 API）
        tk.Label(body, text="支持任意 OpenAI 兼容接口：可直接填域名或 base_url，\n"
                            "例如 https://api.deepseek.com / https://api.openai.com/v1 /\n"
                            "http://localhost:11434/v1 ，路径会自动补全为 /chat/completions。",
                 bg=BG, fg=TEXT_DIM, justify="left", font=("Segoe UI", 9)).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(14, 0))

        # 测试连接结果
        self.test_result = tk.Label(body, text="", bg=BG, fg=TEXT_DIM, justify="left",
                                    wraplength=480, font=("Segoe UI", 9))
        self.test_result.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # 按钮区
        btns = tk.Frame(self.win, bg=BG)
        btns.pack(fill="x", padx=20, pady=(0, 16))
        self.test_btn = self._btn(btns, "Test Connection", self._test_connection)
        self.test_btn.pack(side="left")
        self._btn(btns, "Cancel", self.win.destroy).pack(side="right", padx=(8, 0))
        self._btn(btns, "Save", self._save, accent=True).pack(side="right")

    def _row(self, parent, row: int, label: str, key: str, show: str = ""):
        tk.Label(parent, text=label.upper(), bg=PANEL, fg=VIOLET,
                 font=("Consolas", 9, "bold")).grid(row=row, column=0, sticky="nw", padx=(16, 0), pady=(16, 4))
        ent = tk.Entry(parent, bg=PANEL_LIGHT, fg=TEXT, relief="flat",
                       highlightthickness=1, highlightbackground="#2a3d63",
                       highlightcolor=ACCENT, insertbackground=TEXT,
                       font=("Microsoft YaHei UI", 10), width=44,
                       show=show)
        ent.insert(0, self.config.get(key, ""))
        ent.grid(row=row, column=1, sticky="ew", pady=(14, 4), padx=(12, 16), ipady=5)
        setattr(self, f"entry_{key}", ent)

    def _btn(self, parent, text: str, cmd, accent: bool = False) -> tk.Button:
        bgc = ACCENT if accent else PANEL_LIGHT
        fgc = "#04111a" if accent else TEXT
        btn = tk.Button(parent, text=text, command=cmd, bg=bgc, fg=fgc,
                        activebackground=ACCENT_HOVER if accent else BORDER,
                        activeforeground=fgc, relief="flat", bd=0,
                        padx=18, pady=7, cursor="hand2", font=("Microsoft YaHei UI", 9, "bold"))
        return btn

    def _save(self) -> None:
        self.config["api_url"] = self.entry_api_url.get().strip()
        self.config["api_key"] = self.entry_api_key.get().strip()
        self.config["model"] = self.entry_model.get().strip()
        self.config["reasoning_mode"] = self._r_by_label.get(self.reasoning_var.get(), "max")
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
        self.config["reasoning_mode"] = self._r_by_label.get(self.reasoning_var.get(), "max")
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
        self._void_art: Image.Image | None = None
        self._void_photo: ImageTk.PhotoImage | None = None
        self._sidebar_visible = True
        self._follow_chat = True

        self._bold_font = font.Font(family="Microsoft YaHei UI", size=10, weight="bold")
        self._normal_font = font.Font(family="Microsoft YaHei UI", size=10)
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
        self.root.title("PC Agent")
        self.root.geometry("1280x820")
        self.root.configure(bg=BG)
        self.root.minsize(960, 660)
        self.root.option_add("*Font", "{Microsoft YaHei UI} 10")
        self._load_void_art()
        self.backdrop = tk.Canvas(self.root, bg=BG, highlightthickness=0, bd=0)
        self.backdrop.place(x=0, y=0, relwidth=1, relheight=1)
        # Canvas 内建 lower 子命令会覆盖窗口级 lower()：置底 items 用 tag_lower
        self.backdrop.tag_lower("all")
        self.backdrop.bind("<Configure>", self._draw_window_backdrop)

        toolbar = tk.Frame(self.root, bg="#070b15", height=44,
                           highlightthickness=0)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)

        tk.Label(toolbar, text="◌", bg="#070b15", fg=ACCENT,
                 font=("Segoe UI", 17, "bold")).pack(side="left", padx=(16, 5))
        tk.Label(toolbar, text="PC Agent", bg="#070b15", fg=TEXT,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        tk.Label(toolbar, text="  /  WORKSPACE", bg="#070b15", fg=TEXT_DIM,
                 font=("Consolas", 8)).pack(side="left")
        self.status_dot = tk.Label(toolbar, text="●", bg="#070b15", fg=STOP,
                                   font=("Segoe UI", 10))
        self.status_dot.pack(side="right", padx=(0, 5))
        self.status_text = tk.Label(toolbar, text="connecting", bg="#070b15",
                                    fg=TEXT_DIM, font=("Microsoft YaHei UI", 9))
        self.status_text.pack(side="right", padx=(0, 18))
        self._toolbar_btn(toolbar, "设置", self._open_settings).pack(side="right", padx=5, pady=7)
        self._toolbar_btn(toolbar, "侧栏", self._shortcut_toggle_sidebar).pack(side="right", padx=5, pady=7)

        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        main.columnconfigure(0, weight=0)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        # 会话栏：仅保留真正需要的会话操作，日志和工具清单不再污染主界面。
        side = tk.Frame(main, bg="#09101e", width=264, bd=0,
                        highlightthickness=1, highlightbackground="#0c1525")
        side.grid(row=0, column=0, rowspan=2, sticky="ns", padx=(0, 12))
        self.sidebar = side
        side.pack_propagate(False)
        self._reveal_outline(side, "#0c1525", "#35577f")
        nav_head = tk.Frame(side, bg="#0c1527", height=126)
        nav_head.pack(fill="x")
        nav_head.pack_propagate(False)
        tk.Label(nav_head, text="你的工作区", bg="#0c1527", fg=TEXT,
                 font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=15, pady=(15, 3))
        tk.Label(nav_head, text="对话与项目会自动保存", bg="#0c1527", fg=TEXT_DIM,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=15)
        self._toolbar_btn(nav_head, "+ 新对话", self._new_session, accent=True).pack(
            fill="x", padx=15, pady=(10, 12))

        search_shell = tk.Frame(side, bg="#070d19", highlightthickness=1,
                                highlightbackground="#1d2a45")
        search_shell.pack(fill="x", padx=14, pady=(14, 8))
        self._session_filter = tk.StringVar()
        self._session_filter.trace_add("write", lambda *_: self._update_session_sidebar())
        self.session_search = tk.Entry(search_shell, textvariable=self._session_filter,
                                       bg="#070d19", fg=TEXT, insertbackground=TEXT,
                                       relief="flat", font=("Microsoft YaHei UI", 9))
        self.session_search.pack(fill="x", padx=9, pady=7)
        tk.Label(side, text="最近对话", bg="#09101e", fg=TEXT_DIM,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", padx=15, pady=(4, 5))
        self.session_list = tk.Frame(side, bg="#09101e")
        self.session_list.pack(fill="x", padx=10)
        self._session_buttons: dict[int, tk.Button] = {}

        side_footer = tk.Frame(side, bg="#0c1527", height=102)
        side_footer.pack(side="bottom", fill="x")
        side_footer.pack_propagate(False)
        self.llm_status = tk.Label(side_footer, text="正在连接模型…", bg="#0c1527", fg=TEXT_DIM,
                                   justify="left", wraplength=225,
                                   font=("Microsoft YaHei UI", 8))
        self.llm_status.pack(anchor="w", padx=15, pady=(13, 5))
        self.session_info = tk.Label(side_footer, text="", bg="#0c1527", fg="#667a9d",
                                     justify="left", font=("Microsoft YaHei UI", 8))
        self.session_info.pack(anchor="w", padx=15)

        # 中央工作区：聊天内容滚动层与空会话黑洞层彼此独立。
        chat_card = tk.Frame(main, bg="#060a14", bd=0, highlightthickness=1,
                             highlightbackground="#0c1525")
        chat_card.grid(row=0, column=1, sticky="nsew")
        self.chat_card = chat_card
        self._reveal_outline(chat_card, "#0c1525", "#35577f")
        self.canvas = tk.Canvas(chat_card, bg="#060a14", highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        scroll_style = ttk.Style()
        scroll_style.configure("Cosmic.Vertical.TScrollbar", background="#314d79",
                               troughcolor="#070c19", bordercolor="#070c19",
                               arrowcolor=ACCENT, lightcolor="#314d79", darkcolor="#17253e")
        vbar = ttk.Scrollbar(chat_card, orient="vertical", command=self.canvas.yview,
                             style="Cosmic.Vertical.TScrollbar")
        vbar.place(relx=1, x=-5, y=12, relheight=1, height=-24, anchor="ne")
        self._chat_scrollbar = vbar
        self.canvas.configure(yscrollcommand=self._on_chat_yview)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

        self.scroll_frame = tk.Frame(self.canvas, bg="#060a14")
        self.canvas_window = self.canvas.create_window((0, 0), window=self.scroll_frame,
                                                        anchor="nw", width=540)
        self.scroll_frame.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.jump_btn = tk.Button(chat_card, text="↓ 回到底部", command=self._jump_to_bottom,
                                  bg="#172746", fg=TEXT, activebackground="#263d68",
                                  activeforeground=TEXT, relief="flat", bd=0,
                                  padx=12, pady=5, cursor="hand2",
                                  font=("Microsoft YaHei UI", 9, "bold"))

        self.empty_stage = tk.Canvas(chat_card, bg="#040711", highlightthickness=0, bd=0)
        self.empty_stage.bind("<Configure>", self._draw_empty_stage)
        self._empty_visible = False

        # 悬浮输入器
        input_card = tk.Frame(main, bg="#0e1729", bd=0, highlightthickness=1,
                              highlightbackground="#111c31")
        input_card.grid(row=1, column=1, sticky="ew", pady=(12, 0))
        self._reveal_outline(input_card, "#111c31", "#4c78ad")
        input_card.columnconfigure(0, weight=1)
        meta = tk.Frame(input_card, bg="#0e1729")
        meta.grid(row=0, column=0, columnspan=2, sticky="ew", padx=13, pady=(9, 1))
        tk.Label(meta, text="◈  本地工作区", bg="#0e1729", fg=TEXT,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        tk.Label(meta, text="Agent 模式  ·  Enter 发送 / Shift + Enter 换行", bg="#0e1729",
                 fg=TEXT_DIM, font=("Microsoft YaHei UI", 8)).pack(side="right")
        self.input_box = tk.Text(input_card, bg=CODE_BG, fg=TEXT, relief="flat",
                                 highlightthickness=1, highlightbackground="#1a2b4a",
                                 highlightcolor=ACCENT,
                                 font=("Microsoft YaHei UI", 11), height=3, wrap="word",
                                 insertbackground=TEXT, padx=12, pady=10)
        self.input_box.grid(row=1, column=0, sticky="ew", padx=(10, 8), pady=(3, 9))
        self.input_box.bind("<Return>", self._on_return)
        self.input_box.bind("<Shift-Return>", lambda e: None)
        self.input_box.bind("<KeyRelease>", self._resize_composer, add="+")
        self.input_box.bind("<Configure>", self._resize_composer, add="+")

        self.send_btn = self._toolbar_btn(input_card, "发送  ↑", self._send_message,
                                          accent=True)
        self.send_btn.grid(row=1, column=1, padx=(0, 10), pady=(3, 9))

        # 后台日志/任务仍保留给运行逻辑，但不再占据用户界面。
        self.todo_frame = tk.Frame(self.root, bg=BG)
        self.log_text = tk.Text(self.root, bg=BG, fg=TEXT_DIM, relief="flat",
                                font=("Consolas", 8), state="disabled", wrap="word")

        # 会话初始为空，由 _start 后异步从 LLM 后端加载恢复（失败降级本地创建）
        self._sessions: dict[int, dict] = {}
        self._current_sid = 0
        self._server_sessions_loaded = False
        self.root.bind("<Control-n>", self._shortcut_new_session)
        self.root.bind("<Control-l>", self._shortcut_focus_composer)
        self.root.bind("<Control-b>", self._shortcut_toggle_sidebar)
        self.root.bind("<Control-f>", self._shortcut_search_threads)

    def _draw_empty_stage(self, _event=None) -> None:
        """空会话舞台：黑洞、欢迎语与四个可点击的第一步。"""
        c = self.empty_stage
        w, h = max(c.winfo_width(), 1), max(c.winfo_height(), 1)
        c.delete("all")
        if self._void_art is not None:
            ratio = max(w / self._void_art.width, h / self._void_art.height)
            size = (max(1, int(self._void_art.width * ratio)), max(1, int(self._void_art.height * ratio)))
            art = self._void_art.resize(size, Image.Resampling.LANCZOS)
            left, top = (size[0] - w) // 2, (size[1] - h) // 2
            art = art.crop((left, top, left + w, top + h))
            self._empty_photo = ImageTk.PhotoImage(art)
            c.create_image(0, 0, anchor="nw", image=self._empty_photo)
        else:
            c.create_rectangle(0, 0, w, h, fill="#040711", outline="")
        c.create_rectangle(0, 0, w, h, fill="#02040b", outline="", stipple="gray50")
        cy = int(h * .42)
        c.create_text(w // 2, cy - 62, text="◌", fill=ACCENT,
                      font=("Segoe UI", 30, "bold"))
        c.create_text(w // 2, cy - 18, text="从黑洞边缘，开始一次新对话", fill=TEXT,
                      font=("Microsoft YaHei UI", 20, "bold"))
        c.create_text(w // 2, cy + 18, text="让 Agent 探索代码、构建功能、审查改动或解决问题。", fill="#b5c6df",
                      font=("Microsoft YaHei UI", 10))
        cards = [
            ("探索代码", "快速理解当前项目", "请分析这个项目的结构和关键入口。"),
            ("构建功能", "从需求开始实现", "请帮我规划并实现一个新功能。"),
            ("审查改动", "找出风险与改进点", "请审查当前代码改动并给出改进建议。"),
            ("修复问题", "定位并解决错误", "请帮我定位并修复这个问题。"),
        ]
        gap, card_h = 12, 82
        card_w = max(124, min(158, (w - 72) // 4))
        total = card_w * len(cards) + gap * (len(cards) - 1)
        start = max(24, (w - total) // 2)
        y = cy + 58
        for idx, (title, desc, prompt) in enumerate(cards):
            x = start + idx * (card_w + gap)
            tag = f"starter_{idx}"
            c.create_rectangle(x, y, x + card_w, y + card_h, fill="#091324", outline="#2a3c60",
                               width=1, tags=(tag, "starter"))
            c.create_text(x + 15, y + 24, anchor="w", text=title, fill=ACCENT if idx in (0, 2) else "#c4b5fd",
                          font=("Microsoft YaHei UI", 10, "bold"), tags=(tag, "starter"))
            c.create_text(x + 15, y + 52, anchor="w", text=desc, fill="#a1b1cb",
                          font=("Microsoft YaHei UI", 8), tags=(tag, "starter"))
            c.tag_bind(tag, "<Button-1>", lambda _event, value=prompt: self._prefill_prompt(value))
            c.tag_bind(tag, "<Enter>", lambda _event: c.configure(cursor="hand2"))
            c.tag_bind(tag, "<Leave>", lambda _event: c.configure(cursor=""))

    def _show_empty_workspace(self) -> None:
        self.empty_stage.place(x=0, y=0, relwidth=1, relheight=1)
        # Canvas 内建 raise 子命令覆盖窗口级 lift()：窗口置顶用 Tcl raise 命令
        self.empty_stage.tk.call("raise", self.empty_stage._w)
        self._empty_visible = True
        self._draw_empty_stage()

    def _hide_empty_workspace(self) -> None:
        if getattr(self, "_empty_visible", False):
            self.empty_stage.place_forget()
            self._empty_visible = False

    def _prefill_prompt(self, prompt: str) -> None:
        self.input_box.delete("1.0", "end")
        self.input_box.insert("1.0", prompt)
        self._hide_empty_workspace()
        self.input_box.focus_set()
        self._resize_composer()

    def _toolbar_btn(self, parent, text: str, cmd, accent: bool = False) -> tk.Button:
        bgc = ACCENT if accent else PANEL_LIGHT
        fgc = "#04111a" if accent else TEXT
        hover_bg = ACCENT_HOVER if accent else "#1c2a49"
        btn = tk.Button(parent, text=text, command=cmd, bg=bgc, fg=fgc,
                        activebackground=hover_bg, activeforeground=fgc,
                        relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
                        font=("Microsoft YaHei UI", 9, "bold"))
        btn.bind("<Enter>", lambda e: btn.config(bg=hover_bg))
        btn.bind("<Leave>", lambda e: btn.config(bg=bgc))
        return btn

    def _reveal_outline(self, widget: tk.Widget, resting: str, active: str) -> None:
        """默认弱化容器边线，鼠标进入时才给出冷色轮廓反馈。"""
        widget.bind("<Enter>", lambda _event: widget.configure(highlightbackground=active), add="+")
        widget.bind("<Leave>", lambda _event: widget.configure(highlightbackground=resting), add="+")

    def _side_section(self, parent, title: str) -> None:
        tk.Label(parent, text=title, bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=14, pady=(14, 8))

    def _draw_window_backdrop(self, _event=None) -> None:
        """让生成的深空图覆盖窗口底层，并用暗罩保证所有组件可读。"""
        if self._void_art is None:
            return
        canvas = self.backdrop
        w, h = max(canvas.winfo_width(), 1), max(canvas.winfo_height(), 1)
        source_w, source_h = self._void_art.size
        target_ratio = w / h
        source_ratio = source_w / source_h
        if source_ratio > target_ratio:
            crop_w = int(source_h * target_ratio)
            left = max(0, (source_w - crop_w) // 2)
            crop = self._void_art.crop((left, 0, left + crop_w, source_h))
        else:
            crop_h = int(source_w / target_ratio)
            top = max(0, (source_h - crop_h) // 2)
            crop = self._void_art.crop((0, top, source_w, top + crop_h))
        self._backdrop_photo = ImageTk.PhotoImage(crop.resize((w, h), Image.Resampling.LANCZOS))
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=self._backdrop_photo)
        canvas.create_rectangle(0, 0, w, h, fill="#02040b", outline="", stipple="gray75")

    def _on_mousewheel(self, event) -> str | None:
        """只在指针位于聊天流上方时接管滚轮，避免输入框被误滚动。"""
        x, y = self.root.winfo_pointerx(), self.root.winfo_pointery()
        left, top = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
        if left <= x <= left + self.canvas.winfo_width() and top <= y <= top + self.canvas.winfo_height():
            steps = -int(event.delta / 120) if event.delta else 0
            if steps:
                self.canvas.yview_scroll(steps * 3, "units")
                self._follow_chat = self.canvas.yview()[1] >= .985
                self._refresh_jump_button()
            return "break"
        return None

    def _on_chat_yview(self, first: str, last: str) -> None:
        self._chat_scrollbar.set(first, last)
        self._follow_chat = float(last) >= .985
        self._refresh_jump_button()

    def _refresh_jump_button(self) -> None:
        if not hasattr(self, "jump_btn"):
            return
        if self.canvas.yview()[1] < .985:
            self.jump_btn.place(relx=.5, rely=1, anchor="s", y=-13)
        else:
            self.jump_btn.place_forget()

    def _jump_to_bottom(self) -> None:
        self._follow_chat = True
        self.canvas.yview_moveto(1.0)
        self._refresh_jump_button()

    def _shortcut_new_session(self, _event=None) -> str:
        self._new_session()
        return "break"

    def _shortcut_focus_composer(self, _event=None) -> str:
        self.input_box.focus_set()
        return "break"

    def _shortcut_search_threads(self, _event=None) -> str:
        if self._sidebar_visible:
            self.session_search.focus_set()
            self.session_search.selection_range(0, "end")
        return "break"

    def _shortcut_toggle_sidebar(self, _event=None) -> str:
        if self._sidebar_visible:
            self.sidebar.grid_remove()
        else:
            self.sidebar.grid()
        self._sidebar_visible = not self._sidebar_visible
        return "break"

    def _resize_composer(self, _event=None) -> None:
        """编辑器随内容扩到 7 行，短消息保持紧凑。"""
        try:
            # 空文本时 end-1c 无效，count 返回 None（Tk 8.6.15 行为）
            cnt = self.input_box.count("1.0", "end-1c", "displaylines")
            lines = int(cnt[0]) if cnt else 1
        except (tk.TclError, ValueError, TypeError):
            lines = 1
        self.input_box.configure(height=max(3, min(7, lines + 1)))

    def _draw_void(self, _event=None) -> None:
        """黑洞吸积盘：Canvas 仅作状态装饰，不占用任何交互。"""
        import math

        c = self.void_canvas
        w, h = max(c.winfo_width(), 200), max(c.winfo_height(), 80)
        cx, cy = int(w * .78), int(h * .48)
        pulse = int(3 + 2 * ((1 + math.sin(self._void_phase)) / 2))
        c.delete("all")
        c.create_rectangle(0, 0, w, h, fill="#060a16", outline="")
        if self._void_art is not None:
            # 保持横幅横向构图：从原图中裁取围绕事件视界的一条光带。
            source_w, source_h = self._void_art.size
            crop_h = max(1, int(source_w * h / w))
            center_y = int(source_h * .48)
            top = max(0, min(source_h - crop_h, center_y - crop_h // 2))
            strip = self._void_art.crop((0, top, source_w, top + crop_h))
            self._void_photo = ImageTk.PhotoImage(
                strip.resize((w, h), Image.Resampling.LANCZOS)
            )
            c.create_image(0, 0, anchor="nw", image=self._void_photo)
            # 半透明网格压低图片对文字的干扰，同时保留真实的星云细节。
            c.create_rectangle(0, 0, w, h, fill="#040711", outline="", stipple="gray50")
        for x in range(0, w, 36):
            c.create_line(x, h - 1, x + 28, h - 1, fill="#0a1730")
        c.create_text(16, 20, anchor="w", text="DIALOGUE STREAM", fill=ACCENT,
                      font=("Consolas", 9, "bold"))
        c.create_text(16, 42, anchor="w", text="对话、工具调用与计划进度会在这里实时汇集", fill=TEXT_DIM,
                      font=("Microsoft YaHei UI", 9))
        for i, color in enumerate(("#18265a", "#343081", VIOLET, ACCENT)):
            pad = 8 + i * 5 + pulse
            c.create_arc(cx - 42 - pad, cy - 16 - pad // 3, cx + 42 + pad, cy + 16 + pad // 3,
                         start=170 + i * 13, extent=180 - i * 10, style="arc", outline=color, width=1)
        c.create_oval(cx - 18, cy - 18, cx + 18, cy + 18, fill="#010107", outline="#1e2b59")
        c.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill="#000000", outline="")

    def _load_void_art(self) -> None:
        """加载居中黑洞主视觉；资源缺失时仍可正常使用聊天窗口。"""
        path = BASE_DIR.parent / "assets" / "chat-black-hole-center.png"
        try:
            with Image.open(path) as image:
                self._void_art = image.convert("RGB")
        except (OSError, ValueError):
            self._void_art = None

    def _animate_void(self) -> None:
        if self.quit_flag:
            return
        self._void_phase += .14
        self._draw_void()
        # 低帧率呼吸动画，避免装饰效果抢占聊天与流式输出的响应能力。
        self.root.after(120, self._animate_void)

    # ------------------------------------------------------------------ 布局事件
    def _on_frame_configure(self, event=None) -> None:
        should_follow = self._follow_chat or self.canvas.yview()[1] >= .985
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if should_follow:
            self.canvas.yview_moveto(1.0)
        self._refresh_jump_button()

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfig(self.canvas_window, width=event.width - 4)
        self._refresh_jump_button()

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
                "stream_prep": self._on_stream_prep,
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
            rm = data.get("reasoning_mode") or "max"
            rm_label = {"max": "最高", "high": "高", "off": "关闭"}.get(rm, rm)
            self.llm_status.config(
                text=f"ready · {data.get('model')}\n{data.get('api_url')}\n"
                     f"推理: {rm_label} · 确认: {cfg.get('confirm_mode', 'auto')}",
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
        code, data, _ = api_request(self.llm_url, "GET", "/api/v1/sessions?full=1", timeout=8)
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

    def _prepare_stream(self, sess: dict):
        """后台线程：发送前压缩检查。返回 ("ready"|"compressed", 消息快照)。"""
        cfg = load_config()
        window = int(cfg.get("context_window") or 65536)
        est = sum(len(m.get("content") or "") for m in sess["messages"]) * 0.8
        if est <= window * COMPRESS_THRESHOLD:
            return ("ready", list(sess["messages"]))
        code, data, _ = api_request(self.llm_url, "POST", "/api/v1/compress",
                                    {"messages": sess["messages"], "keep_recent": KEEP_RECENT},
                                    timeout=90)
        if code == 200 and data.get("compressed"):
            return ("compressed", data.get("messages") or sess["messages"])
        return ("ready", list(sess["messages"]))

    def _on_stream_prep(self, payload) -> None:
        """主线程：压缩完成 → 用压缩结果替换本地消息 → 发起流式。"""
        kind, msgs = payload
        if kind == "compressed":
            self._sessions[self._current_sid]["messages"] = msgs
            self._log("上下文已压缩（发送前，省 tokens）", "ok")
        self._tasks.put(("stream", lambda: self._do_chat_stream(msgs, self._stream_handle)))

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
        # 压缩检查（后台执行，压缩完再发起流式）——每轮发送量有硬边界
        self._tasks.put(("stream_prep", lambda: self._prepare_stream(sess)))

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
        arg_str = _fmt_args(data.get("arguments") or "")
        step_info = f" · 轮次 {data.get('step')}/{data.get('max_steps')}" if data.get("step") else ""
        self._agent_log.append(f"`[⚙ {data.get('name')}]` {arg_str}{step_info}")
        self._log(f"tool call: {data.get('name')} {arg_str}{step_info}", "info")
        self._render_agent_log(handle)

    def _on_stream_tool_result(self, payload) -> None:
        handle, data = payload
        if handle is not self._stream_handle:
            return
        ok = bool(data.get("ok"))
        summary = _summarize_result(data.get("result") or "", ok)
        mark = "✓" if ok else "✗"
        self._agent_log.append(f"  {mark} {summary}")
        self._log(f"tool result: {mark} {summary}", "ok" if ok else "err")
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

        plan = data.get("plan")
        if plan:
            lines = ["执行计划（批准后按计划执行，计划内操作免确认）:"]
            for i, s in enumerate(plan, 1):
                tools = ", ".join(s.get("tools") or []) or "—"
                lines.append(f"{i}. {s.get('step', '')}\n    需要: {tools}")
                if s.get("reason"):
                    lines.append(f"    原因: {s['reason']}")
            tk.Label(body, text="\n".join(lines), bg=BG, fg=TEXT, justify="left",
                     wraplength=600, font=("Segoe UI", 10)).pack(anchor="w", pady=(0, 10))

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
        self.send_btn.config(state="normal", text="发送  ↑", bg=ACCENT,
                             command=self._send_message)
        self._update_session_sidebar()

    def _add_message(self, role: str, text: str) -> None:
        self._hide_empty_workspace()
        is_user = role == "user"
        bubble_bg = USER_BUBBLE if is_user else AGENT_BUBBLE
        fg = "white" if is_user else TEXT

        container = tk.Frame(self.scroll_frame, bg=PANEL)
        container.pack(fill="x", padx=18, pady=8)

        # 左侧/右侧对齐容器
        align = tk.Frame(container, bg=PANEL)
        align.pack(side="right" if is_user else "left", anchor="e" if is_user else "w")

        # 头像 + 名称行
        meta = tk.Frame(align, bg=PANEL)
        meta.pack(anchor="w" if not is_user else "e", fill="x")
        label = "你" if is_user else "AGENT"
        tk.Label(meta, text=label, bg=PANEL, fg=ACCENT if is_user else VIOLET,
                 font=("Segoe UI", 9, "bold")).pack(side="left" if not is_user else "right")
        tk.Label(meta, text=time.strftime("%H:%M"), bg=PANEL, fg=TEXT_DIM,
                 font=("Segoe UI", 8)).pack(side="left" if not is_user else "right", padx=6)

        # 气泡主体
        bubble = tk.Frame(align, bg=bubble_bg, bd=0, highlightthickness=1,
                          highlightbackground="#287293" if is_user else "#2b3b68")
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
                            anchor="w", wraplength=max(360, self.canvas.winfo_width() - 230),
                            font=("Consolas", 9))
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

        # 普通长文本采用可换行标签，避免宽屏气泡被一行内容撑破。
        if len(parts) == 1 and parts[0][0] == "normal":
            tk.Label(parent, text=parts[0][1], bg=parent["bg"], fg=fg,
                     justify="left", anchor="w",
                     wraplength=max(360, self.canvas.winfo_width() - 210),
                     font=self._normal_font).pack(anchor="w", padx=12, pady=(4, 0))
            return

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
        if self._streaming:
            return
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
            self._show_empty_workspace()
        else:
            self._hide_empty_workspace()
        self._update_session_sidebar()

    def _update_session_sidebar(self) -> None:
        # 全量销毁重建：每行含切换按钮 + ✕ 删除按钮，必须全部销毁
        # （否则残留的旧 ✕ 按钮会引用已删除的会话，点击即 KeyError）
        for child in self.session_list.winfo_children():
            child.destroy()
        self._session_buttons.clear()
        query = self._session_filter.get().strip().lower()
        shown = 0
        for sid in sorted(self._sessions, reverse=True):
            is_current = sid == self._current_sid
            bgc = "#163452" if is_current else "#0d172a"
            row = tk.Frame(self.session_list, bg=bgc)
            title = self._sessions[sid].get("title") or f"会话 #{sid}"
            if query and query not in title.lower() and query not in str(sid):
                continue
            row.pack(fill="x", pady=2)
            shown += 1
            btn = tk.Button(row, text=title[:14], bg=bgc,
                            fg=ACCENT if is_current else TEXT, relief="flat", bd=0,
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
        if not shown and self._sessions:
            tk.Label(self.session_list, text="未找到匹配的对话", bg=PANEL, fg=TEXT_DIM,
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=5)
        if self._current_sid not in self._sessions:
            self.session_info.config(text="正在加载会话…")
            return
        cfg = load_config()
        self.session_info.config(
            text=f"当前: 会话 #{self._current_sid}\n模型: {cfg.get('model')}\n消息: "
                 f"{len(self._sessions[self._current_sid]['history'])} 条")
        if not query:
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
