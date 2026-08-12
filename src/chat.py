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
import itertools
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import logging
import tkinter as tk

log = logging.getLogger("chat")
from collections import deque
from pathlib import Path
from tkinter import font, messagebox, ttk
import urllib.error
import urllib.request

from PIL import Image, ImageEnhance, ImageTk, ImageOps
# R3：Token 预算（动态压缩阈值）与历史检索（压缩后按需取回原文）
from token_budget import plan_budget
from history_index import HistoryIndex, find_keys
from quant_integration import (
    QuantIntegrationConfig,
    QuantLaunchError,
    QuantServiceController,
)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR.parent / "chat_config.json"

# Windows 显示缩放下让窗口清晰（物理像素）
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
# 统一日志目录（与 llm_server/gui 一致）：Daemon stderr 写入 .pcagent/daemon.err.log
from data_paths import data_file

DAEMON_ERR_LOG = data_file("daemon.err.log")
DAEMON_ERR_LOG_MAX = 1_000_000   # 轮转阈值（1MB → 重命名为 .log.1）


def _open_daemon_err_log():
    """打开统一 Daemon 错误日志（读写同一绝对路径）；超过 1MB 轮转为 .log.1。"""
    try:
        DAEMON_ERR_LOG.parent.mkdir(parents=True, exist_ok=True)
        if DAEMON_ERR_LOG.exists() and DAEMON_ERR_LOG.stat().st_size > DAEMON_ERR_LOG_MAX:
            DAEMON_ERR_LOG.replace(DAEMON_ERR_LOG.with_suffix(".log.1"))
    except OSError:
        pass
    return open(DAEMON_ERR_LOG, "a", encoding="utf-8")

# ===== 视觉系统：低饱和深空底色 + 克制的冰蓝/紫色状态色 =====
BG = "#03050a"
SURFACE = "#070a11"
SIDEBAR_BG = "#080b13"
PANEL = "#0a0f19"
PANEL_LIGHT = "#111827"
PANEL_HOVER = "#172033"
BORDER = "#1d2939"
BORDER_ACTIVE = "#45607d"
TEXT = "#f2f6fb"
TEXT_SOFT = "#c8d2df"
TEXT_DIM = "#8391a5"
TEXT_MUTED = "#586579"
ACCENT = "#8cecff"
ACCENT_HOVER = "#b9f4ff"
ACCENT_DIM = "#2c6f8f"     # 聚焦发光用的低亮冰蓝
USER_BUBBLE = "#12334a"
USER_BUBBLE_HI = "#153d58"
AGENT_BUBBLE = "#0d1420"
AGENT_BUBBLE_HI = "#111a2a"
STOP = "#ff7387"
OK = "#55e6b5"
WARN = "#ffc978"
CODE_BG = "#060a11"
CODE_FG = "#d0d9e6"
VIOLET = "#afa3ff"
VIOLET_DIM = "#4a3f7e"     # 徽章/图标用的低亮紫


def _pick_font_family(root: tk.Misc, *candidates: str) -> str:
    """选择本机存在的字体，避免把 Windows 11 字体硬编码成运行前提。"""
    try:
        available = {name.casefold(): name for name in font.families(root)}
        for candidate in candidates:
            if candidate.casefold() in available:
                return available[candidate.casefold()]
    except tk.TclError:
        pass
    return candidates[-1]

def _opt_value_str(v) -> str:
    """选项值字符串化：bool → "0"/"1"（兼容历史 bool 存储），其余转字符串。

    配置文件中 tool_router 以 bool 持久化，而设置下拉的选项值是字符串
    "0"/"1"，严格比较永远不匹配会导致显示回退（看起来像"没保存"）。
    """
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v).strip()


def _match_option(options: dict, current) -> str:
    """在 {显示文本: 值} 选项映射中找到与当前值匹配的显示文本（类型宽松）。"""
    cur = _opt_value_str(current)
    return next((k for k, v in options.items() if _opt_value_str(v) == cur),
                next(iter(options)))


def _as_bool_setting(value, default: bool = False) -> bool:
    """Read legacy JSON bool/string values without changing unrelated config."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "开启"}


# 会话首条 system 提示（本地结构发送用；后端只存 user/assistant 消息）
SYSTEM_FIRST = ("你是一个桌面 Agent 助手，可以控制用户的电脑"
                "（屏幕点击、输入、按键、截图）。回答尽量简洁、准确。")
# 发送前上下文压缩（与 cli/bot 对齐：超过窗口 60% 先压缩再发送，防 tokens 激增）
COMPRESS_THRESHOLD = 0.6
KEEP_RECENT = 8


def _token_for_base(base_url: str) -> str:
    """按结构化配置选择 token：base_url 与 llm_base/daemon_base 精确匹配。

    未配置自定义地址时回退默认端口（8001=llm / 8000=daemon）；
    配置为占位符（__secure__）时从安全存储读取。
    """
    cfg = load_config()
    llm_base = str(cfg.get("llm_base") or "").strip().rstrip("/")
    daemon_base = str(cfg.get("daemon_base") or "").strip().rstrip("/")
    base = base_url.rstrip("/")
    if llm_base and base == llm_base:
        key = "api_token"
    elif daemon_base and base == daemon_base:
        key = "daemon_token"
    else:
        key = "api_token" if (":8001" in base or base.endswith(":8001")) else "daemon_token"
    val = str(cfg.get(key) or "").strip()
    if val == "__secure__":
        try:
            from secure_store import load as ss_load
            val = ss_load(key)
        except Exception:
            val = ""
    return val


def api_request(base_url: str, method: str, path: str, payload=None,
                timeout: float = 15, raw: bool = False):
    headers = {"Content-Type": "application/json"}
    t = _token_for_base(base_url)
    if t:
        headers["X-Api-Token"] = t
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        base_url + path, data=data, method=method, headers=headers,
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


def _redact_args(arguments: str, limit: int = 120) -> str:
    """日志脱敏版参数：type_text 只显示字符数，其余长文本参数只显示长度。"""
    try:
        args = json.loads(arguments or "{}")
    except Exception:
        return (arguments or "")[:limit]
    parts = []
    for k, v in args.items():
        s = str(v)
        if k in ("text", "content", "code", "old", "new", "command", "prompt", "task", "message"):
            parts.append(f"{k}=<{len(s)}字>")
        else:
            parts.append(f"{k}={s[:60]}")
    return ", ".join(parts)[:limit]


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # 密钥安全存储：占位符 → 读取真实值（api_key / api_token / daemon_token）
            try:
                from secure_store import load as ss_load
                for k in ("api_key", "api_token", "daemon_token"):
                    if (cfg.get(k) or "") == "__secure__":
                        cfg[k] = ss_load(k)
            except Exception:
                pass
            return cfg
        except Exception:
            pass
    return {
        "api_url": "https://api.deepseek.com/v1/chat/completions",
        "api_key": "",
        "model": "deepseek-v4-flash",
        "reasoning_mode": "max",   # 推理强度：max（最高）/ high（高）/ off（关闭）
        "quant_enabled": True,
        "quant_auto_start": True,
        "quant_project_path": str(BASE_DIR.parent / "quant-agent-lab"),
        "quant_backend_url": "http://127.0.0.1:8014",
        "quant_gui_url": "http://127.0.0.1:4173",
        "quant_open_mode": "auto",
        "quant_stop_owned_processes_on_exit": True,
    }


def save_config(config: dict) -> None:
    """保存配置：api_key/api_token/daemon_token 进安全存储（DPAPI/受限文件），
    文件只留占位符；原子写。安全存储失败时抛错（不静默退回明文）。"""
    cfg = dict(config)
    try:
        from secure_store import store as ss_store
        for k in ("api_key", "api_token", "daemon_token"):
            val = (cfg.get(k) or "").strip()
            if val and val != "__secure__":
                ss_store(k, val)
                cfg[k] = "__secure__"
            elif not val:
                ss_store(k, "")     # 清空密钥：删除旧安全存储值
                cfg[k] = ""
    except Exception as exc:
        raise ValueError(f"密钥安全存储失败，未保存：{exc}")
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    if sys.platform != "win32":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    tmp.replace(CONFIG_PATH)
    if sys.platform != "win32":
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass


# ===== Settings 窗口（分组设置：常规 / 模型 / Agent 权限 / 工具路由 / MCP / 高级） =====
class SettingsWindow:
    """保持黑洞科幻风格：默认弱化边框、悬停显现；分组保存、保存前验证、原子写。

    视觉模型（view_image/vision_*）配置保留（用户指示不删除）。
    """

    def __init__(self, parent: tk.Tk, on_changed=None):
        self.parent = parent
        self.config = load_config()
        self.on_changed = on_changed   # 保存或连接成功后回调（主界面刷新 LLM 状态）
        self.win = tk.Toplevel(parent)
        self._ui_family = _pick_font_family(
            self.win, "Microsoft YaHei UI", "Segoe UI Variable Text", "Segoe UI")
        self._display_family = _pick_font_family(
            self.win, "Microsoft YaHei UI", "Segoe UI Variable Display", "Segoe UI")
        self._mono_family = _pick_font_family(
            self.win, "Cascadia Code", "Cascadia Mono", "Consolas", "Courier New")
        self.win.title("设置 - PC Agent")
        self.win.geometry("820x680")
        self.win.minsize(740, 610)
        self.win.configure(bg=BG)
        self.win.transient(parent)
        self.win.grab_set()
        self._build_ui()
        self.win.focus_force()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        header = tk.Frame(self.win, bg="#070b12", height=96,
                          highlightthickness=1, highlightbackground="#101827")
        header.pack(fill="x")
        header.pack_propagate(False)
        # 顶部霓虹渐变横幅（冰蓝：左暗右亮，静态绘制零开销）
        banner = tk.Canvas(header, height=3, bg="#070b12",
                           highlightthickness=0, bd=0)
        banner.pack(fill="x", side="top")

        def _draw_banner(_e=None):
            w = max(banner.winfo_width(), 1)
            banner.delete("all")
            steps = 48
            for i in range(steps):
                t = i / max(1, steps - 1)
                r = int(14 + (44 - 14) * t)
                g = int(43 + (111 - 43) * t)
                b = int(64 + (143 - 64) * t)
                x0 = w * i // steps
                x1 = w * (i + 1) // steps
                banner.create_rectangle(x0, 0, x1, 3, fill=f"#{r:02x}{g:02x}{b:02x}",
                                        outline="")

        banner.bind("<Configure>", _draw_banner)
        tk.Label(header, text="SYSTEM  /  CONFIGURATION", bg="#070b12", fg=ACCENT,
                 font=(self._mono_family, 8, "bold")).pack(anchor="w", padx=24, pady=(16, 0))
        tk.Label(header, text="设置", bg="#070b12", fg=TEXT,
                 font=(self._display_family, 18, "bold")).pack(anchor="w", padx=24)
        tk.Label(header, text="连接、权限与工具都在这里集中管理", bg="#070b12", fg=TEXT_DIM,
                 font=(self._ui_family, 8)).place(x=112, y=55)

        nb = ttk.Notebook(self.win)
        nb.pack(fill="both", expand=True, padx=18, pady=(15, 10))
        try:
            style = ttk.Style()
            style.theme_use("clam")
            style.configure("Cosmic.TNotebook", background=BG, borderwidth=0)
            style.configure("Cosmic.TNotebook.Tab", background="#0d131e", foreground=TEXT_DIM,
                            borderwidth=0, padding=(16, 8), font=(self._ui_family, 9, "bold"))
            style.map("Cosmic.TNotebook.Tab",
                      background=[("selected", "#142535"), ("active", PANEL_HOVER)],
                      foreground=[("selected", ACCENT), ("active", TEXT)])
            nb.configure(style="Cosmic.TNotebook")
        except Exception:
            pass

        self._tab_common = self._make_tab(nb, "常规")
        self._tab_model = self._make_tab(nb, "模型")
        self._tab_perm = self._make_tab(nb, "Agent 权限")
        self._tab_router = self._make_tab(nb, "工具路由")
        self._tab_mcp = self._make_tab(nb, "MCP")
        self._tab_adv = self._make_tab(nb, "高级")
        self._tab_quant = self._make_tab(nb, "量化")

        self._build_common()
        self._build_model()
        self._build_permissions()
        self._build_router()
        self._build_mcp()
        self._build_advanced()
        self._build_quant()

        # 底部：状态提示 + 按钮
        footer = tk.Frame(self.win, bg="#070b12", highlightthickness=1,
                          highlightbackground="#101827")
        footer.pack(fill="x", side="bottom")
        self.test_result = tk.Label(footer, text="", bg="#070b12", fg=TEXT_DIM,
                                    justify="left", anchor="w", wraplength=760,
                                    font=(self._ui_family, 9))
        self.test_result.pack(fill="x", padx=22, pady=(9, 0))
        btns = tk.Frame(footer, bg="#070b12")
        btns.pack(fill="x", padx=20, pady=(6, 13))
        self.test_btn = self._btn(btns, "测试连接", self._test_connection)
        self.test_btn.pack(side="left")
        self._btn(btns, "取消", self.win.destroy).pack(side="right", padx=(8, 0))
        self._btn(btns, "保存", self._save, accent=True).pack(side="right")

    def _make_tab(self, nb: ttk.Notebook, title: str) -> tk.Frame:
        tab = tk.Frame(nb, bg=BG, highlightthickness=1,
                       highlightbackground="#152030")
        nb.add(tab, text=title)
        tab.columnconfigure(1, weight=1)
        return tab

    def _row(self, parent, row: int, label: str, key: str, show: str = "", width: int = 44):
        tk.Label(parent, text=label.upper(), bg=parent["bg"], fg=VIOLET,
                 font=(self._mono_family, 8, "bold")).grid(row=row, column=0, sticky="nw",
                                                    padx=(18, 0), pady=(14, 4))
        ent = tk.Entry(parent, bg="#0e1622", fg=TEXT, relief="flat",
                       highlightthickness=1, highlightbackground="#263548",
                       highlightcolor=ACCENT, insertbackground=TEXT,
                       selectbackground="#274b61", font=(self._ui_family, 10),
                       width=width, show=show)
        ent.insert(0, str(self.config.get(key, "")))
        ent.grid(row=row, column=1, sticky="ew", pady=(14, 4), padx=(12, 18), ipady=5)
        setattr(self, f"entry_{key}", ent)
        return ent

    def _select(self, parent, row: int, label: str, options: dict, key: str):
        """下拉选择（options: 显示文本 -> 值）。"""
        display = _match_option(options, self.config.get(key))
        tk.Label(parent, text=label.upper(), bg=parent["bg"], fg=VIOLET,
                 font=(self._mono_family, 8, "bold")).grid(row=row, column=0, sticky="nw",
                                                    padx=(18, 0), pady=(14, 4))
        var = tk.StringVar(value=display)
        opt = tk.OptionMenu(parent, var, *options.keys())
        opt.config(bg="#0e1622", fg=TEXT, relief="flat", highlightthickness=0,
                   activebackground=PANEL_HOVER, activeforeground=TEXT,
                   font=(self._ui_family, 9), width=24, cursor="hand2")
        opt["menu"].config(bg=PANEL_LIGHT, fg=TEXT, font=(self._ui_family, 9))
        opt.grid(row=row, column=1, sticky="w", pady=(14, 4), padx=(12, 0))
        setattr(self, f"var_{key}", var)
        # 正映射 {显示文本: 值}：_collect 用显示文本取回配置值
        setattr(self, f"opts_{key}", dict(options))
        return var

    def _btn(self, parent, text: str, cmd, accent: bool = False) -> tk.Button:
        bgc = ACCENT if accent else "#101824"
        fgc = "#04111a" if accent else TEXT
        btn = tk.Button(parent, text=text, command=cmd, bg=bgc, fg=fgc,
                        activebackground=ACCENT_HOVER if accent else PANEL_HOVER,
                        activeforeground=fgc, relief="flat", bd=0,
                        padx=18, pady=7, cursor="hand2",
                        font=(self._ui_family, 9, "bold"))
        hover = ACCENT_HOVER if accent else PANEL_HOVER
        btn.bind("<Enter>", lambda _e: btn.configure(bg=hover), add="+")
        btn.bind("<Leave>", lambda _e: btn.configure(bg=bgc), add="+")
        return btn

    # ---- 常规：工作区 / 日志级别 / 开机行为 ----
    def _build_common(self) -> None:
        tab = self._tab_common
        tk.Label(tab, text="当前工作区（Agent 文件/Git/Todo/备份目录）", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=0, columnspan=2,
                                                              sticky="w", padx=18, pady=(16, 2))
        self.entry_workspace = tk.Entry(tab, bg=PANEL_LIGHT, fg=TEXT, relief="flat",
                                        highlightthickness=1, highlightbackground="#2a3d63",
                                        highlightcolor=ACCENT, insertbackground=TEXT,
                                        font=("Microsoft YaHei UI", 9), width=46)
        self.entry_workspace.insert(0, str(self.config.get("workspace", "")))
        self.entry_workspace.grid(row=1, column=0, sticky="ew", padx=(18, 6), pady=(6, 4), ipady=4)
        self._btn(tab, "选择文件夹…", self._pick_workspace).grid(row=1, column=1,
                                                                sticky="w", padx=(0, 18), pady=(6, 4))
        tk.Label(tab, text="最近工作区（切换后自动记录）", bg=BG, fg=TEXT_DIM,
                 font=("Microsoft YaHei UI", 9)).grid(row=2, column=0, columnspan=2,
                                                      sticky="w", padx=18, pady=(6, 2))
        recent = self.config.get("recent_workspaces") or []
        if recent:
            self.var_ws_recent = tk.StringVar(value="")
            opt = tk.OptionMenu(tab, self.var_ws_recent, *[str(r) for r in recent])
            opt.config(bg=PANEL_LIGHT, fg=TEXT, relief="flat", highlightthickness=0,
                       font=("Microsoft YaHei UI", 9), width=40, cursor="hand2")
            opt["menu"].config(bg=PANEL_LIGHT, fg=TEXT, font=("Microsoft YaHei UI", 9))
            opt.grid(row=3, column=0, columnspan=2, sticky="w", padx=18, pady=(2, 6))
            self._btn(tab, "切换到所选", self._switch_recent_ws).grid(
                row=4, column=0, sticky="w", padx=18, pady=(0, 8))
        else:
            tk.Label(tab, text="（暂无历史）", bg=BG, fg=TEXT_DIM,
                     font=("Microsoft YaHei UI", 9)).grid(row=3, column=0, sticky="w",
                                                          padx=18, pady=(2, 6))
        self._select(tab, 5, "日志级别",
                     {"信息": "info", "警告": "warning", "调试": "debug"}, "log_level")

    def _pick_workspace(self) -> None:
        from tkinter import filedialog
        p = filedialog.askdirectory(parent=self.win, title="选择工作区文件夹",
                                    initialdir=self.entry_workspace.get() or str(Path.home()))
        if p:
            self.entry_workspace.delete(0, "end")
            self.entry_workspace.insert(0, p)

    def _switch_recent_ws(self) -> None:
        v = self.var_ws_recent.get()
        if v:
            self.entry_workspace.delete(0, "end")
            self.entry_workspace.insert(0, v)

    # ---- 模型：URL / Key（安全存储）/ Model / 推理强度 / 上下文窗口 ----
    def _build_model(self) -> None:
        tab = self._tab_model
        self._row(tab, 0, "API URL", "api_url")
        self._row(tab, 1, "API Key（安全存储）", "api_key", show="*")
        self._row(tab, 2, "Model", "model")
        self._row(tab, 3, "Context Window", "context_window", width=14)
        self._select(tab, 4, "推理强度",
                     {"最高": "max", "高": "high", "关闭": "off"}, "reasoning_mode")
        # 视觉模型（保留：用户指示不删除视觉功能）
        tk.Label(tab, text="视觉模型（view_image 用）", bg=BG, fg=TEXT_DIM,
                 font=("Microsoft YaHei UI", 9, "bold")).grid(row=5, column=0, columnspan=2,
                                                              sticky="w", padx=18, pady=(16, 2))
        self._row(tab, 6, "Vision URL", "vision_api_url")
        self._row(tab, 7, "Vision Key", "vision_api_key", show="*")
        self._row(tab, 8, "Vision Model", "vision_model")

    # ---- Agent 权限：confirm mode / 执行策略 / 轮数与时长上限 ----
    def _build_permissions(self) -> None:
        tab = self._tab_perm
        self._select(tab, 0, "问询模式",
                     {"智能 (auto)": "auto", "严格 (strict)": "strict",
                      "信任 (trusted)": "trusted", "只读 (query)": "query",
                      "计划 (plan)": "plan"}, "confirm_mode")
        self._row(tab, 1, "最大工具轮数", "max_tool_steps", width=10)
        self._row(tab, 2, "最大任务时间(秒)", "max_agent_seconds", width=10)
        tk.Label(tab, text="危险命令黑名单、工作区路径限制与调用上限始终生效（不随模式关闭）。",
                 bg=BG, fg=TEXT_DIM, font=("Microsoft YaHei UI", 8)).grid(
            row=3, column=0, columnspan=2, sticky="w", padx=18, pady=(10, 0))

    # ---- 工具路由：开关 / Ollama URL / 模型 / 连接测试 ----
    def _build_router(self) -> None:
        tab = self._tab_router
        self._select(tab, 0, "工具路由",
                     {"关闭": "0", "开启": "1"}, "tool_router")
        self._row(tab, 1, "Ollama URL", "tool_router_url")
        self._row(tab, 2, "路由模型", "tool_router_model")
        self._btn(tab, "测试路由连接", self._test_router).grid(row=3, column=0,
                                                              sticky="w", padx=18, pady=(12, 0))
        self._router_result = tk.Label(tab, text="", bg=BG, fg=TEXT_DIM,
                                       font=("Microsoft YaHei UI", 9))
        self._router_result.grid(row=4, column=0, columnspan=2, sticky="w", padx=18, pady=(6, 0))

    def _test_router(self) -> None:
        url = self.entry_tool_router_url.get().strip() or "http://127.0.0.1:11434"
        self._router_result.config(text="正在测试…", fg=TEXT_DIM)

        def _do():
            try:
                import urllib.request
                req = urllib.request.Request(
                    url.rstrip("/") + "/api/tags",
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    import json as _j
                    data = _j.loads(resp.read().decode("utf-8"))
                    models = [m.get("name", "") for m in (data.get("models") or [])][:6]
                    return "ok", f"✓ Ollama 可达，模型：{'、'.join(models) or '（无模型）'}"
            except Exception as exc:
                return "err", f"✗ 无法连接 Ollama：{exc}"

        def _done(res):
            kind, text = res
            self._router_result.config(text=text, fg=OK if kind == "ok" else STOP)

        threading.Thread(target=lambda: self.win.after(0, _done, _do()), daemon=True).start()

    # ---- MCP：server 状态 / 工具数 / 只读声明 ----
    def _build_mcp(self) -> None:
        tab = self._tab_mcp
        tk.Label(tab, text="MCP 外部工具（mcp_config.json 配置）", bg=BG, fg=TEXT,
                 font=(self._ui_family, 9, "bold")).grid(
                     row=0, column=0, sticky="w", padx=18, pady=(16, 4))
        self._btn(tab, "刷新状态", self._refresh_mcp_status).grid(
            row=0, column=1, sticky="e", padx=18, pady=(12, 4))
        self._mcp_status = tk.Label(
            tab, text="◌ 正在读取连接状态…", bg=BG, fg=TEXT_DIM, justify="left",
            anchor="nw", font=(self._ui_family, 9))
        self._mcp_status.grid(row=1, column=0, columnspan=2, sticky="ew",
                              padx=18, pady=(6, 6))
        tk.Label(tab, text="只读/写权限在 mcp_config.json 中声明（read_only_tools/write_tools），\n"
                           "未声明的工具按写操作处理（调用需确认）。断线自动重连并隐藏不可用工具。",
                 bg=BG, fg=TEXT_DIM, font=(self._ui_family, 8)).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=18, pady=(8, 0))
        self.win.after(80, self._refresh_mcp_status)

    def _refresh_mcp_status(self) -> None:
        """后台刷新 MCP 状态，避免打开设置窗口时阻塞 Tk 主线程。"""
        if not hasattr(self, "_mcp_status"):
            return
        self._mcp_status.config(text="◌ 正在读取连接状态…", fg=TEXT_DIM)

        def _load():
            base = str(self.config.get("llm_base") or "http://127.0.0.1:8001").rstrip("/")
            status, data, _ = api_request(base, "GET", "/api/v1/health", timeout=5)
            if status != 200:
                return False, "未连接到 LLM 后端，MCP 状态暂不可用。"
            structured = (data or {}).get("mcp_servers") or []
            if structured:
                lines = []
                for server in structured:
                    name = server.get("name") or "未命名服务"
                    connected = bool(server.get("connected"))
                    count = int(server.get("tool_count") or 0)
                    state = "已连接" if connected else "正在重连"
                    lines.append(f"{'●' if connected else '○'}  {name}  ·  {state}  ·  {count} 个工具")
                return True, "\n".join(lines)
            tools = [t for t in ((data or {}).get("tools") or []) if t.startswith("mcp_")]
            if tools:
                return True, f"●  MCP 已连接  ·  {len(tools)} 个外部工具可用"
            return True, "○  暂无已连接的 MCP 服务"

        def _done(result):
            if not self.win.winfo_exists():
                return
            ok, text = result
            self._mcp_status.config(text=text, fg=OK if ok else WARN)

        def _worker():
            try:
                result = _load()
            except Exception as exc:
                result = (False, f"读取 MCP 状态失败：{exc}")
            try:
                self.win.after(0, _done, result)
            except tk.TclError:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    # ---- 高级：Daemon / LLM 地址 / token / 数据目录 ----
    _TOKEN_PLACEHOLDER = "（已安全存储，留空保持不变）"

    def _build_advanced(self) -> None:
        tab = self._tab_adv
        self._row(tab, 0, "Daemon 地址", "daemon_base")
        self._row(tab, 1, "LLM Backend 地址", "llm_base")
        # token 已安全存储时显示占位文本，不泄露真实值
        ent_api = self._row(tab, 2, "API Token（llm_server）", "api_token", show="*")
        if str(self.config.get("api_token") or "") == "__secure__":
            ent_api.delete(0, "end")
            ent_api.insert(0, self._TOKEN_PLACEHOLDER)
        ent_daemon = self._row(tab, 3, "Daemon Token", "daemon_token", show="*")
        if str(self.config.get("daemon_token") or "") == "__secure__":
            ent_daemon.delete(0, "end")
            ent_daemon.insert(0, self._TOKEN_PLACEHOLDER)
        tk.Label(tab, text="数据目录：.pcagent/（会话/备份/日志，自动轮转与损坏恢复）",
                 bg=BG, fg=TEXT_DIM, font=("Microsoft YaHei UI", 8)).grid(
            row=4, column=0, columnspan=2, sticky="w", padx=18, pady=(12, 0))

    # ---- 量化中心：仅配置 loopback 服务，不保存交易凭据 ----
    def _build_quant(self) -> None:
        tab = self._tab_quant
        self._quant_vars = {}
        fields = (
            ("启用量化中心", "quant_enabled", True),
            ("自动启动独立服务", "quant_auto_start", True),
            ("主 Agent 退出时停止自己启动的服务", "quant_stop_owned_processes_on_exit", True),
        )
        for row, (label, key, default) in enumerate(fields):
            var = tk.BooleanVar(value=_as_bool_setting(self.config.get(key), default))
            tk.Checkbutton(tab, text=label, variable=var, bg=BG, fg=TEXT,
                           activebackground=BG, activeforeground=TEXT,
                           selectcolor=PANEL_LIGHT, font=(self._ui_family, 9),
                           anchor="w").grid(row=row, column=0, columnspan=2,
                                             sticky="w", padx=18, pady=(14 if row == 0 else 6, 0))
            self._quant_vars[key] = var
        quant_defaults = {
            "quant_project_path": str(BASE_DIR.parent / "quant-agent-lab"),
            "quant_backend_url": "http://127.0.0.1:8014",
            "quant_gui_url": "http://127.0.0.1:4173",
        }
        for key, value in quant_defaults.items():
            if not self.config.get(key):
                self.config[key] = value
        self._row(tab, 3, "量化项目路径", "quant_project_path", width=54)
        self._row(tab, 4, "量化后端 URL", "quant_backend_url")
        self._row(tab, 5, "量化 GUI URL", "quant_gui_url")
        self._select(tab, 6, "打开方式", {"自动": "auto", "浏览器": "browser", "原生窗口（未启用）": "embedded"}, "quant_open_mode")
        tk.Label(tab, text="只允许 loopback；默认后端 8014、GUI 4173，不占用主 Agent 的 8000/8001。\n"
                           "按钮只负责健康检查、启动和导航，不生成报告、不审批、不执行交易。",
                 bg=BG, fg=TEXT_DIM, justify="left", font=(self._ui_family, 8)).grid(
            row=7, column=0, columnspan=2, sticky="w", padx=18, pady=(16, 0))
        self.quant_result = tk.Label(tab, text="", bg=BG, fg=TEXT_DIM, justify="left",
                                     anchor="w", wraplength=680, font=(self._ui_family, 9))
        self.quant_result.grid(row=8, column=0, columnspan=2, sticky="ew", padx=18, pady=(10, 0))
        self._btn(tab, "测试连接", self._test_quant).grid(row=9, column=0, sticky="w", padx=18, pady=(12, 0))
        self._btn(tab, "打开量化中心", self._open_quant_from_settings, accent=True).grid(row=9, column=1, sticky="e", padx=18, pady=(12, 0))

    def _collect_quant(self, cfg: dict) -> dict:
        for key, var in getattr(self, "_quant_vars", {}).items():
            cfg[key] = bool(var.get())
        for key in ("quant_project_path", "quant_backend_url", "quant_gui_url"):
            entry = getattr(self, f"entry_{key}", None)
            if entry is not None:
                cfg[key] = entry.get().strip()
        var = getattr(self, "var_quant_open_mode", None)
        opts = getattr(self, "opts_quant_open_mode", None)
        if var is not None and opts is not None:
            cfg["quant_open_mode"] = opts.get(var.get(), "auto")
        # Reuse the controller's strict loopback/project validation.
        QuantIntegrationConfig.from_mapping(cfg)
        return cfg

    def _test_quant(self) -> None:
        try:
            cfg = self._collect_quant(self._collect())
            controller = QuantServiceController.from_mapping(cfg)
        except (ValueError, QuantLaunchError) as exc:
            self.quant_result.config(text=f"✗ {exc}", fg=STOP)
            return
        self.quant_result.config(text="◌ 正在检查量化后端和 GUI…", fg=TEXT_DIM)
        threading.Thread(target=self._quant_probe_worker, args=(controller,), daemon=True).start()

    def _quant_probe_worker(self, controller: QuantServiceController) -> None:
        status = controller.probe()
        self.win.after(0, lambda: self.quant_result.config(
            text=(f"✓ {status.message} · backend={status.backend_url} · gui={status.gui_url}"
                  if status.ready else f"✗ {status.code}：{status.message}"),
            fg=OK if status.ready else WARN))

    def _open_quant_from_settings(self) -> None:
        try:
            cfg = self._collect_quant(self._collect())
            save_config(cfg)
            if self.on_changed:
                self.on_changed()
            self.win.destroy()
            self.parent.after(0, lambda: self.parent.event_generate("<<OpenQuantCenter>>", when="tail"))
        except (ValueError, QuantLaunchError) as exc:
            self.quant_result.config(text=f"✗ {exc}", fg=STOP)

    # ------------------------------------------------------------------ 保存 / 测试
    def _collect(self) -> dict:
        """收集全部字段（保存前验证）。"""
        cfg = dict(self.config)
        for key in ("api_url", "api_key", "model", "context_window", "vision_api_url",
                    "vision_api_key", "vision_model", "max_tool_steps", "max_agent_seconds",
                    "tool_router_url", "tool_router_model", "daemon_base", "llm_base",
                    "quant_project_path", "quant_backend_url", "quant_gui_url"):
            ent = getattr(self, f"entry_{key}", None)
            if ent is not None:
                cfg[key] = ent.get().strip()
        # token：占位文本 = 保持已安全存储状态；空 = 清空；其他 = 更新
        for key in ("api_token", "daemon_token"):
            ent = getattr(self, f"entry_{key}", None)
            if ent is not None:
                val = ent.get().strip()
                if val == self._TOKEN_PLACEHOLDER:
                    cfg[key] = "__secure__"
                else:
                    cfg[key] = val
        for key in ("reasoning_mode", "confirm_mode", "log_level"):
            var = getattr(self, f"var_{key}", None)
            opts = getattr(self, f"opts_{key}", None)
            if var is not None and opts is not None:
                cfg[key] = opts.get(var.get(), cfg.get(key, ""))
        # 工具路由开关：显示文本 → 选项值（"1"/"0"）→ bool
        var = getattr(self, "var_tool_router", None)
        opts = getattr(self, "opts_tool_router", None)
        if var is not None and opts is not None:
            cfg["tool_router"] = opts.get(var.get(), "0") == "1"
        # 工作区
        ws = self.entry_workspace.get().strip()
        if ws:
            cfg["workspace"] = ws
        for key, var in getattr(self, "_quant_vars", {}).items():
            cfg[key] = bool(var.get())
        var_quant_mode = getattr(self, "var_quant_open_mode", None)
        opts_quant_mode = getattr(self, "opts_quant_open_mode", None)
        if var_quant_mode is not None and opts_quant_mode is not None:
            cfg["quant_open_mode"] = opts_quant_mode.get(var_quant_mode.get(), "auto")
        # 数值验证
        for key in ("context_window", "max_tool_steps", "max_agent_seconds"):
            v = str(cfg.get(key) or "").strip()
            if v and not v.isdigit():
                raise ValueError(f"{key} 必须是正整数（当前：{v}）")
            if v:
                cfg[key] = int(v)
        # URL 验证（协议/主机含 IPv6/端口/userinfo）
        for key in ("api_url", "llm_base", "daemon_base", "vision_api_url", "tool_router_url"):
            if cfg.get(key):
                cfg[key] = self._validate_base_url(str(cfg[key]), key)
        QuantIntegrationConfig.from_mapping(cfg)
        return cfg

    @staticmethod
    def _validate_base_url(value: str, label: str) -> str:
        """验证 base URL：http/https、合法主机（含 IPv6）、合法端口；去尾斜杠。"""
        from urllib.parse import urlparse
        value = (value or "").strip().rstrip("/")
        if not value:
            return value
        u = urlparse(value)
        if u.scheme not in ("http", "https"):
            raise ValueError(f"{label} 必须是 http/https 地址（当前：{value}）")
        if not u.hostname:
            raise ValueError(f"{label} 缺少主机名（当前：{value}）")
        if u.port is not None and not (1 <= u.port <= 65535):
            raise ValueError(f"{label} 端口非法（当前：{value}）")
        if u.username is not None or u.password is not None:
            raise ValueError(f"{label} 不能包含用户名/密码（当前：{value}）")
        return value

    def _save(self) -> None:
        """保存设置：网络请求（工作区/问询模式/安全存储）全部放后台线程，不阻塞 UI。"""
        try:
            cfg = self._collect()
        except ValueError as exc:
            self.test_result.config(text=f"✗ {exc}", fg=STOP)
            return
        self.test_btn.config(state="disabled", text="保存中…")
        self.test_result.config(text="正在保存…", fg=TEXT_DIM)
        threading.Thread(target=self._do_save, args=(cfg,), daemon=True).start()

    def _do_save(self, cfg: dict) -> None:
        """后台线程：工作区切换 + 问询模式 + 本地保存（含安全存储）。"""
        err = None
        llm_base = str(cfg.get("llm_base") or "http://127.0.0.1:8001").rstrip("/")
        old_base = str(self.config.get("llm_base") or "http://127.0.0.1:8001").rstrip("/")
        ws = str(cfg.get("workspace") or "").strip()
        if ws:
            code, data, _ = api_request(llm_base, "POST", "/api/v1/workspace",
                                        {"path": ws}, timeout=10)
            if code != 200:
                err = f"✗ 工作区切换失败：{(data or {}).get('detail', f'HTTP {code}')}"
            else:
                recent = [str(r) for r in (self.config.get("recent_workspaces") or [])]
                if ws not in recent:
                    recent.insert(0, ws)
                    cfg["recent_workspaces"] = recent[:10]
        if err is None:
            mode = str(cfg.get("confirm_mode") or "auto")
            code, data, _ = api_request(llm_base, "POST", "/api/v1/confirm-mode",
                                        {"mode": mode}, timeout=10)
            if code != 200:
                err = f"✗ 确认模式更新失败：{(data or {}).get('detail', f'HTTP {code}')}"
        if err is None:
            try:
                save_config(cfg)
            except ValueError as exc:
                err = f"✗ {exc}"
        base_changed = llm_base != old_base
        self.win.after(0, lambda: self._save_done(err, base_changed))

    def _save_done(self, err: str | None, base_changed: bool = False) -> None:
        """主线程：保存结果（失败不关闭窗口、不假装成功、不谎称实时生效）。"""
        self.test_btn.config(state="normal", text="测试连接")
        if err:
            self.test_result.config(text=err, fg=STOP)
            return
        if self.on_changed:
            self.on_changed()
        if base_changed:
            # 服务地址变更：当前 client 不会自动切换，明确提示重启后生效
            self.test_result.config(text="✓ 已保存（服务地址变更，重启后生效）", fg=OK)
            self.win.after(1200, self.win.destroy)
            return
        self.test_result.config(text="✓ 已保存（实时生效）", fg=OK)
        self.win.after(600, self.win.destroy)

    # ---- 连接测试（后台线程，不阻塞 UI）----
    def _test_connection(self) -> None:
        try:
            cfg = self._collect()
        except ValueError as exc:
            self.test_result.config(text=f"✗ {exc}", fg=STOP)
            return
        save_config(cfg)
        self.test_btn.config(state="disabled", text="测试中…")
        self.test_result.config(text="正在测试连接…", fg=TEXT_DIM)
        threading.Thread(target=self._do_test, args=(cfg,), daemon=True).start()

    def _do_test(self, cfg: dict) -> None:
        # 使用候选配置的 llm_base（用户可能尚未保存），不硬编码端口
        llm_base = str(cfg.get("llm_base") or "http://127.0.0.1:8001").rstrip("/")
        code, data, _ = api_request(llm_base, "POST", "/api/v1/test", timeout=90)
        self.win.after(0, lambda: self._show_test_result(code, data))

    def _show_test_result(self, code: int, data: dict) -> None:
        self.test_btn.config(state="normal", text="测试连接")
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
        # 结构化地址：配置 llm_base / daemon_base 优先（设置界面保存后生效）
        cfg = load_config()
        llm_base = str(cfg.get("llm_base") or "").strip()
        if llm_base:
            self.llm_url = llm_base.rstrip("/")
            try:
                self.llm_port = urllib.request.urlparse(self.llm_url).port or 8001
            except Exception:
                self.llm_port = 8001
        daemon_base = str(cfg.get("daemon_base") or "").strip()
        if daemon_base:
            self.base_url = daemon_base.rstrip("/")
        self.quit_flag = False
        self._tasks: queue.Queue = queue.Queue()
        self._results: queue.Queue = queue.Queue()
        self._daemon_ok = False
        self._llm_ok = False
        self._llm_model = ""
        self._daemon_err_fh = None
        self._void_art: Image.Image | None = None
        self._void_photo: ImageTk.PhotoImage | None = None
        self._chat_backdrop_photo: ImageTk.PhotoImage | None = None
        self._chat_backdrop_item: int | None = None
        self._chat_backdrop_size = (0, 0)
        self._sidebar_visible = True
        self._follow_chat = True

        self._ui_family = _pick_font_family(
            root, "Microsoft YaHei UI", "Segoe UI Variable Text", "Segoe UI")
        self._display_family = _pick_font_family(
            root, "Microsoft YaHei UI", "Segoe UI Variable Display", "Segoe UI")
        self._mono_family = _pick_font_family(
            root, "Cascadia Code", "Cascadia Mono", "Consolas", "Courier New")
        self._bold_font = font.Font(root=root, family=self._ui_family, size=10, weight="bold")
        self._normal_font = font.Font(root=root, family=self._ui_family, size=10)
        self._mono_font = font.Font(root=root, family=self._mono_family, size=9)

        # 多会话：{sid: {"messages": [...], "history": [(role, text), ...]}}
        self._sessions: dict[int, dict] = {}
        self._current_sid = 1
        # ---- 流式状态（全部显式初始化，Stop/断开安全）----
        self._streaming = False          # 流式输出进行中
        self._stream_cancel = threading.Event()   # Stop 通知 SSE 读取线程退出
        self._stream_task_id = 0         # 当前流唯一 generation（旧流事件据此隔离）
        self._task_counter = itertools.count(1)
        self._stream_resp = None         # 当前流的 HTTP 响应（Stop 时关闭）
        self._stream_handle = None
        self._stream_content_acc = ""
        self._stream_reasoning_acc = ""
        self._agent_log: list[str] = []
        self._poll_running = False       # 事件轮询循环防重入
        self._log_buf: deque = deque(maxlen=200)   # 运行日志环形缓冲（有上限）
        # R3：压缩后历史索引 / 检索键 / 压缩计数（Token 可观测性）
        self._history_index = HistoryIndex()
        self._retrieval_keys: list[str] = []
        self._compress_count = 0
        self._quant_busy = False
        self._quant_controller = None
        self._quant_controllers = []
        self._quant_status = "未检查"
        # 首个会话在 _build_ui 完成后创建（需要 UI 组件已就绪）

        threading.Thread(target=self._bg_loop, daemon=True).start()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._start)
        self.root.after(40, self._poll_events)   # 固定周期事件轮询（实时渲染流事件）

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.root.title("PC Agent")
        self.root.geometry("1360x860")
        self.root.configure(bg=BG)
        self.root.minsize(1024, 680)
        self.root.option_add("*Font", f"{{{self._ui_family}}} 10")
        self._load_void_art()
        self.backdrop = tk.Canvas(self.root, bg=BG, highlightthickness=0, bd=0)
        self.backdrop.place(x=0, y=0, relwidth=1, relheight=1)
        # Canvas 内建 lower 子命令会覆盖窗口级 lower()：置底 items 用 tag_lower
        self.backdrop.tag_lower("all")
        self.backdrop.bind("<Configure>", self._draw_window_backdrop)

        toolbar = tk.Frame(self.root, bg="#050810", height=54, highlightthickness=0)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)

        # 品牌标志：发光核心 + 感应轨道环 + 导航卫星（静态绘制，零动画开销）
        brand = tk.Canvas(toolbar, width=34, height=34, bg="#050810",
                          highlightthickness=0, bd=0)
        brand.pack(side="left", padx=(16, 9))
        brand.create_oval(7, 7, 27, 27, outline="#1d3550", width=6)      # 光晕
        brand.create_oval(10, 10, 24, 24, outline="#0e2338", width=2)
        brand.create_arc(2, 2, 32, 32, start=206, extent=148, outline=ACCENT,
                         width=2, style="arc")                            # 主轨道
        brand.create_arc(2, 2, 32, 32, start=210, extent=18, outline=ACCENT_HOVER,
                         width=3, style="arc")                            # 轨道高光段
        brand.create_oval(24, 3, 27, 6, fill=ACCENT, outline="")          # 导航卫星
        brand.create_oval(11, 11, 23, 23, fill="#010307", outline="#6672b8", width=1)
        brand.create_arc(11, 11, 23, 23, start=250, extent=100, outline=ACCENT,
                         width=1, style="arc")                            # 核心吸积光

        title_stack = tk.Frame(toolbar, bg="#050810")
        title_stack.pack(side="left", pady=8)
        tk.Label(title_stack, text="PC AGENT", bg="#050810", fg=TEXT,
                 font=(self._display_family, 10, "bold")).pack(anchor="w")
        workspace = str(load_config().get("workspace") or "本地工作区")
        workspace_name = Path(workspace).name if workspace != "本地工作区" else workspace
        self.workspace_label = tk.Label(
            title_stack, text=workspace_name.upper(), bg="#050810", fg=TEXT_MUTED,
            font=(self._mono_family, 7))
        self.workspace_label.pack(anchor="w")

        status_pill = tk.Frame(toolbar, bg="#0a101b", highlightthickness=1,
                               highlightbackground="#152234")
        status_pill.pack(side="right", padx=(8, 16), pady=12)
        self.status_dot = tk.Label(status_pill, text="●", bg="#0a101b", fg=STOP,
                                   font=(self._ui_family, 8))
        self.status_dot.pack(side="left", padx=(10, 5), pady=4)
        self.status_text = tk.Label(status_pill, text="正在连接", bg="#0a101b",
                                    fg=TEXT_DIM, font=(self._ui_family, 8))
        self.status_text.pack(side="left", padx=(0, 4), pady=4)
        self.status_model = tk.Label(status_pill, text="", bg="#0a101b",
                                     fg=TEXT_MUTED, font=(self._mono_family, 7))
        self.status_model.pack(side="left", padx=(0, 10), pady=4)
        self.quant_center_btn = self._toolbar_btn(
            toolbar, "量化中心", self._open_quant_center)
        self.quant_center_btn.pack(side="right", padx=4, pady=11)
        self._toolbar_btn(toolbar, "设置", self._open_settings).pack(side="right", padx=4, pady=11)
        self.sidebar_toggle_btn = self._toolbar_btn(
            toolbar, "隐藏侧栏", self._shortcut_toggle_sidebar)
        self.sidebar_toggle_btn.pack(side="right", padx=4, pady=11)
        self.root.bind("<<OpenQuantCenter>>", lambda _event: self._open_quant_center(), add="+")

        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        main.columnconfigure(0, weight=0)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        # 会话栏：仅保留真正需要的会话操作，日志和工具清单不再污染主界面。
        side = tk.Frame(main, bg=SIDEBAR_BG, width=276, bd=0,
                        highlightthickness=1, highlightbackground="#0d1420")
        side.grid(row=0, column=0, rowspan=3, sticky="ns", padx=(0, 10))
        self.sidebar = side
        side.pack_propagate(False)
        self._reveal_outline(side, "#0d1420", BORDER_ACTIVE)
        nav_head = tk.Frame(side, bg=SIDEBAR_BG, height=154)
        nav_head.pack(fill="x")
        nav_head.pack_propagate(False)
        tk.Label(nav_head, text="工作区", bg=SIDEBAR_BG, fg=TEXT,
                 font=(self._display_family, 13, "bold")).pack(anchor="w", padx=16, pady=(16, 2))
        tk.Label(nav_head, text="对话会自动保存到当前项目", bg=SIDEBAR_BG, fg=TEXT_DIM,
                 font=(self._ui_family, 8)).pack(anchor="w", padx=16)
        self._toolbar_btn(nav_head, "＋  新建对话", self._new_session, accent=True).pack(
            fill="x", padx=15, pady=(12, 11))

        search_shell = tk.Frame(side, bg="#0b101a", highlightthickness=1,
                                highlightbackground="#172131")
        search_shell.pack(fill="x", padx=14, pady=(14, 8))
        tk.Label(search_shell, text="⌕", bg="#0b101a", fg=TEXT_MUTED,
                 font=(self._display_family, 13)).pack(side="left", padx=(9, 2))
        self._session_filter = tk.StringVar()
        self._session_filter.trace_add("write", lambda *_: self._update_session_sidebar())
        self.session_search = tk.Entry(search_shell, textvariable=self._session_filter,
                                       bg="#0b101a", fg=TEXT_SOFT, insertbackground=ACCENT,
                                       relief="flat", font=(self._ui_family, 9))
        self.session_search.pack(side="left", fill="x", expand=True, padx=(2, 9), pady=8)
        tk.Label(side, text="最近对话", bg=SIDEBAR_BG, fg=TEXT_MUTED,
                 font=(self._ui_family, 8, "bold")).pack(anchor="w", padx=16, pady=(5, 6))
        # 会话列表：滚动容器（会话多时不溢出）
        self._session_scroll = tk.Canvas(side, bg=SIDEBAR_BG, highlightthickness=0, bd=0, height=260)
        self.session_list = tk.Frame(self._session_scroll, bg=SIDEBAR_BG)
        self._session_win = self._session_scroll.create_window(
            (0, 0), window=self.session_list, anchor="nw")
        self.session_list.bind(
            "<Configure>",
            lambda _e: self._session_scroll.configure(
                scrollregion=self._session_scroll.bbox("all")))
        self._session_scroll.bind(
            "<MouseWheel>",
            lambda e: self._session_scroll.yview_scroll(
                -int(e.delta / 120) * 3, "units"))
        self._session_scroll.bind(
            "<Configure>",
            lambda e: self._session_scroll.itemconfigure(
                self._session_win, width=max(1, e.width)), add="+")
        self._session_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self._session_buttons: dict[int, tk.Widget] = {}

        side_footer = tk.Frame(side, bg="#090e17", height=124,
                               highlightthickness=1, highlightbackground="#101826")
        side_footer.pack(side="bottom", fill="x")
        side_footer.pack_propagate(False)
        self.llm_status = tk.Label(side_footer, text="正在连接模型…", bg="#090e17", fg=TEXT_DIM,
                                   justify="left", wraplength=225,
                                   font=(self._ui_family, 8))
        self.llm_status.pack(anchor="w", padx=15, pady=(13, 4))
        self.session_info = tk.Label(side_footer, text="", bg="#090e17", fg=TEXT_MUTED,
                                     justify="left", font=(self._ui_family, 8))
        self.session_info.pack(anchor="w", padx=15)
        # Token 用量统计（来自 /api/v1/usage；失败不影响聊天主流程）
        self.token_stats = tk.Label(side_footer, text="◈ 用量统计加载中…", bg="#090e17",
                                    fg=TEXT_MUTED, justify="left",
                                    font=(self._mono_family, 7))
        self.token_stats.pack(anchor="w", padx=15, pady=(6, 0))

        # 中央工作区：固定标题栏、居中消息列和持续可见的黑洞底图。
        chat_card = tk.Frame(main, bg=SURFACE, bd=0, highlightthickness=1,
                             highlightbackground="#0d1420")
        chat_card.grid(row=0, column=1, sticky="nsew")
        self.chat_card = chat_card
        self._reveal_outline(chat_card, "#0d1420", BORDER_ACTIVE)

        chat_head = tk.Frame(chat_card, bg="#070b12", height=57,
                             highlightthickness=1, highlightbackground="#101827")
        chat_head.pack(fill="x")
        chat_head.pack_propagate(False)
        head_text = tk.Frame(chat_head, bg="#070b12")
        head_text.pack(side="left", padx=17, pady=9)
        self.chat_title = tk.Label(head_text, text="新对话", bg="#070b12", fg=TEXT,
                                   font=(self._display_family, 11, "bold"))
        self.chat_title.pack(anchor="w")
        self.chat_meta = tk.Label(head_text, text="本地工作区  ·  Agent 模式", bg="#070b12",
                                  fg=TEXT_MUTED, font=(self._ui_family, 8))
        self.chat_meta.pack(anchor="w")
        self._toolbar_btn(chat_head, "屏幕控制", self._open_screen_backend).pack(
            side="right", padx=(4, 13), pady=12)

        self.canvas = tk.Canvas(chat_card, bg=SURFACE, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        scroll_style = ttk.Style()
        scroll_style.configure("Cosmic.Vertical.TScrollbar", background="#3b4d65",
                               troughcolor=SURFACE, bordercolor=SURFACE,
                               arrowcolor=ACCENT, lightcolor="#3b4d65", darkcolor="#1c2838",
                               width=7)
        vbar = ttk.Scrollbar(chat_card, orient="vertical", command=self.canvas.yview,
                             style="Cosmic.Vertical.TScrollbar")
        vbar.place(relx=1, x=-5, y=69, relheight=1, height=-82, anchor="ne")
        self._chat_scrollbar = vbar
        # 滚动条默认隐藏；鼠标进入聊天区或实际滚动时显示，离开后平滑隐藏
        vbar.place_forget()
        self._scrollbar_visible = False
        chat_card.bind("<Enter>", self._show_scrollbar, add="+")
        chat_card.bind("<Leave>", self._hide_scrollbar, add="+")
        self.canvas.configure(yscrollcommand=self._on_chat_yview)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

        self._content_bg = "#05080f"
        self._chat_backdrop_item = self.canvas.create_image(
            0, 0, anchor="nw", tags=("chat_backdrop",))
        self.scroll_frame = tk.Frame(self.canvas, bg=self._content_bg)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.scroll_frame,
                                                        anchor="n", width=760)
        self.canvas.tag_lower("chat_backdrop")
        self.scroll_frame.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.jump_btn = tk.Button(chat_card, text="↓ 回到底部", command=self._jump_to_bottom,
                                  bg="#172746", fg=TEXT, activebackground="#263d68",
                                  activeforeground=TEXT, relief="flat", bd=0,
                                  padx=12, pady=5, cursor="hand2",
                                  font=("Microsoft YaHei UI", 9, "bold"))

        self.empty_stage = tk.Canvas(chat_card, bg=BG, highlightthickness=0, bd=0)
        self.empty_stage.bind("<Configure>", self._draw_empty_stage)
        self._empty_visible = False

        # Codex 风格输入器：正文在上，操作与状态放到底部，不挤占输入区域。
        input_card = tk.Frame(main, bg="#0b1019", bd=0, highlightthickness=1,
                              highlightbackground="#162131")
        input_card.grid(row=2, column=1, sticky="ew", pady=(8, 0))
        self.input_card = input_card
        self._reveal_outline(input_card, "#162131", "#55738f")
        input_card.columnconfigure(0, weight=1)
        meta = tk.Frame(input_card, bg="#0b1019")
        meta.grid(row=0, column=0, sticky="ew", padx=14, pady=(9, 0))
        tk.Label(meta, text="▍", bg="#0b1019", fg=ACCENT,
                 font=(self._mono_family, 8, "bold")).pack(side="left")
        tk.Label(meta, text="AGENT", bg="#0b1019", fg=ACCENT,
                 font=(self._mono_family, 7, "bold")).pack(side="left", padx=(3, 0))
        tk.Label(meta, text="  当前工作区  ·  Enter 发送", bg="#0b1019", fg=TEXT_MUTED,
                 font=(self._ui_family, 8)).pack(side="left")

        composer_shell = tk.Frame(input_card, bg=CODE_BG, highlightthickness=1,
                                  highlightbackground="#1c2a3c", highlightcolor=ACCENT)
        composer_shell.grid(row=1, column=0, sticky="ew", padx=10, pady=(4, 0))
        composer_shell.columnconfigure(0, weight=1)
        self.input_box = tk.Text(composer_shell, bg=CODE_BG, fg=TEXT, relief="flat",
                                 highlightthickness=0, font=(self._ui_family, 11),
                                 height=3, wrap="word", undo=True, maxundo=80,
                                 insertbackground=ACCENT, selectbackground="#274b61",
                                 selectforeground=TEXT, padx=12, pady=10)
        self.input_box.grid(row=0, column=0, sticky="ew")
        # 聚焦反馈：外框冰蓝发光，失焦恢复低调深色
        def _focus_glow(_e=None):
            composer_shell.configure(highlightbackground=ACCENT_DIM)
        def _blur_glow(_e=None):
            composer_shell.configure(highlightbackground="#1c2a3c")
        self.input_box.bind("<FocusIn>", _focus_glow, add="+")
        self.input_box.bind("<FocusOut>", _blur_glow, add="+")
        self._composer_hint = tk.Label(
            composer_shell, text="描述任务，或粘贴代码、错误信息与上下文…",
            bg=CODE_BG, fg="#536174", cursor="xterm",
            font=(self._ui_family, 10))
        self._composer_hint.place(x=13, y=11)
        self._composer_hint.bind("<Button-1>", lambda _e: self.input_box.focus_set())
        self.input_box.bind("<Return>", self._on_return)
        self.input_box.bind("<Shift-Return>", lambda e: None)
        self.input_box.bind("<KeyRelease>", self._resize_composer, add="+")
        self.input_box.bind("<Configure>", self._resize_composer, add="+")
        self.input_box.bind("<FocusIn>", self._update_composer_hint, add="+")
        self.input_box.bind("<FocusOut>", self._update_composer_hint, add="+")

        composer_actions = tk.Frame(input_card, bg="#0b1019")
        composer_actions.grid(row=2, column=0, sticky="ew", padx=10, pady=(5, 9))
        self._toolbar_btn(composer_actions, "屏幕", self._open_screen_backend).pack(side="left")
        tk.Label(composer_actions, text="Enter 发送  ·  Shift + Enter 换行", bg="#0b1019",
                 fg=TEXT_MUTED, font=(self._ui_family, 8)).pack(side="left", padx=10)
        self.send_btn = self._toolbar_btn(
            composer_actions, "发送  ↑", self._send_message, accent=True)
        self.send_btn.pack(side="right")

        # 后台日志/任务仍保留给运行逻辑，但不再占据用户界面。
        # 任务面板（todo）：恢复为聊天区内的可见、可折叠区域（输入框上方）。
        self.todo_frame = tk.Frame(main, bg="#0d1526", bd=0, highlightthickness=1,
                                   highlightbackground="#16233f")
        self.todo_frame.grid(row=1, column=1, sticky="ew", pady=(10, 0))
        self.todo_frame.grid_remove()        # 无任务时隐藏
        self._todo_expanded = True
        # 顶部微光条：任务面板的层次标识
        tk.Frame(self.todo_frame, bg=ACCENT_DIM, height=2).pack(fill="x")
        self._todo_head = tk.Frame(self.todo_frame, bg="#0d1526")
        self._todo_head.pack(fill="x")
        self._todo_toggle = tk.Button(self._todo_head, text="▾ ☑ 任务清单（0）", command=self._toggle_todo,
                                      bg="#0d1526", fg=TEXT_DIM, activebackground="#16233f",
                                      activeforeground=ACCENT, relief="flat", bd=0, anchor="w",
                                      cursor="hand2", font=("Microsoft YaHei UI", 9, "bold"))
        self._todo_toggle.pack(fill="x", padx=13, pady=(6, 2))
        self._todo_body = tk.Frame(self.todo_frame, bg="#0d1526")
        self._todo_body.pack(fill="x", padx=13, pady=(0, 6))
        self._todo_items: list[dict] = []

        # 会话初始为空，由 _start 后异步从 LLM 后端加载恢复（失败降级本地创建）。
        self._sessions = {}
        self._current_sid = 0
        self._server_sessions_loaded = False
        self.root.bind("<Control-n>", self._shortcut_new_session)
        self.root.bind("<Control-l>", self._shortcut_focus_composer)
        self.root.bind("<Control-b>", self._shortcut_toggle_sidebar)
        self.root.bind("<Control-f>", self._shortcut_search_threads)
        # R3：Token 用量轮询（15s；统计失败不打扰聊天）
        self.root.after(15000, self._poll_token_stats)

    def _poll_token_stats(self) -> None:
        """周期拉取 /api/v1/usage（走后台任务队列，不阻塞 Tk 主线程）。"""
        if self.quit_flag:
            return
        self._tasks.put(("token_stats", self._fetch_token_stats))
        self.root.after(15000, self._poll_token_stats)

    def _fetch_token_stats(self):
        code, data, _ = api_request(self.llm_url, "GET", "/api/v1/usage", timeout=5)
        return (code, data)

    def _on_token_stats(self, payload) -> None:
        """主线程：刷新侧栏用量统计（失败静默，不影响聊天）。"""
        code, data = payload
        if code != 200 or not isinstance(data, dict):
            return
        try:
            prompt = int(data.get("prompt_tokens") or 0)
            comp = int(data.get("completion_tokens") or 0)
            total = prompt + comp
            hit = float(data.get("cache_hit_rate") or 0)
            calls = int(data.get("calls") or 0)
            unit = "tok"
            shown = f"{total:,}"
            if total >= 10000:
                shown = f"{total / 1000:.1f}k"
            self.token_stats.config(
                text=f"◈ 累计 {shown} {unit} · 缓存 {hit * 100:.0f}% · "
                     f"压缩 {self._compress_count} · 调用 {calls}")
        except (TypeError, ValueError):
            pass

    def _toggle_todo(self) -> None:
        """收起/展开任务面板。"""
        self._todo_expanded = not self._todo_expanded
        if self._todo_expanded:
            self._todo_body.pack(fill="x", padx=13, pady=(0, 6))
            self._todo_toggle.config(text=f"▾ ☑ 任务清单（{len(self._todo_items)}）")
        else:
            self._todo_body.pack_forget()
            self._todo_toggle.config(text=f"▸ ☑ 任务清单（{len(self._todo_items)}）")

    def _draw_empty_stage(self, _event=None) -> None:
        """空会话舞台：黑洞、欢迎语与四个可点击的第一步。"""
        c = self.empty_stage
        w, h = max(c.winfo_width(), 1), max(c.winfo_height(), 1)
        c.delete("all")
        if self._void_art is not None:
            art = ImageOps.fit(self._void_art, (w, h), Image.Resampling.LANCZOS)
            art = ImageEnhance.Color(art).enhance(.84)
            art = ImageEnhance.Brightness(art).enhance(.72)
            art = Image.blend(art, Image.new("RGB", (w, h), BG), .22)
            self._empty_photo = ImageTk.PhotoImage(art)
            c.create_image(0, 0, anchor="nw", image=self._empty_photo)
        else:
            c.create_rectangle(0, 0, w, h, fill=BG, outline="")
        cy = int(h * .40)
        c.create_text(w // 2, cy - 80, text="EVENT HORIZON  /  READY", fill="#668095",
                      font=(self._mono_family, 8, "bold"))
        # 标题两侧的微光装饰线
        line_w = 120
        c.create_line(w // 2 - line_w, cy - 56, w // 2 - 34, cy - 56,
                      fill="#1c3b52", width=1)
        c.create_line(w // 2 + 34, cy - 56, w // 2 + line_w, cy - 56,
                      fill="#1c3b52", width=1)
        c.create_text(w // 2, cy - 40, text="从这里开始", fill=TEXT,
                      font=(self._display_family, 23, "bold"))
        c.create_text(w // 2, cy - 4, text="把一个目标交给 Agent，它会探索、执行并验证结果。", fill=TEXT_SOFT,
                      font=(self._ui_family, 10))
        cards = [
            ("探索代码", "快速理解当前项目", "请分析这个项目的结构和关键入口。"),
            ("构建功能", "从需求开始实现", "请帮我规划并实现一个新功能。"),
            ("审查改动", "找出风险与改进点", "请审查当前代码改动并给出改进建议。"),
            ("修复问题", "定位并解决错误", "请帮我定位并修复这个问题。"),
        ]
        gap, card_h = 82, 88
        card_w = max(128, min(176, (w - 72) // 4))
        total = card_w * len(cards) + gap * (len(cards) - 1)
        start = max(24, (w - total) // 2)
        y = cy + 40
        for idx, (title, desc, prompt) in enumerate(cards):
            x = start + idx * (card_w + gap)
            tag = f"starter_{idx}"
            rect_tag = f"starter_card_{idx}"
            bar_tag = f"starter_bar_{idx}"
            c.create_rectangle(x, y, x + card_w, y + card_h, fill="#0a111d",
                               outline="#1c2b3d", width=1,
                               tags=(tag, "starter", rect_tag))
            # 卡片底部微光条（hover 时亮起）
            c.create_line(x + 5, y + card_h - 4, x + card_w - 5, y + card_h - 4,
                          fill="#0e2536", width=2, tags=(tag, "starter", bar_tag))
            c.create_text(x + 15, y + 25, anchor="w", text=f"{idx + 1:02d}",
                          fill=ACCENT_DIM, font=(self._mono_family, 8, "bold"),
                          tags=(tag, "starter"))
            c.create_text(x + 47, y + 25, anchor="w", text=title,
                          fill=ACCENT if idx in (0, 3) else VIOLET,
                          font=(self._ui_family, 9, "bold"), tags=(tag, "starter"))
            c.create_text(x + 15, y + 52, anchor="w", text=desc, fill=TEXT_DIM,
                          font=(self._ui_family, 8), tags=(tag, "starter"))
            c.tag_bind(tag, "<Button-1>", lambda _event, value=prompt: self._prefill_prompt(value))
            c.tag_bind(tag, "<Enter>", lambda _event, rt=rect_tag, bt=bar_tag: (
                c.configure(cursor="hand2"),
                c.itemconfigure(rt, fill="#101c2b", outline="#4e7895"),
                c.itemconfigure(bt, fill=ACCENT_DIM)))
            c.tag_bind(tag, "<Leave>", lambda _event, rt=rect_tag, bt=bar_tag: (
                c.configure(cursor=""),
                c.itemconfigure(rt, fill="#0a111d", outline="#1c2b3d"),
                c.itemconfigure(bt, fill="#0e2536")))
        # 快捷键提示行
        c.create_text(w // 2, y + card_h + 32,
                      text="Ctrl+L 聚焦输入   ·   Ctrl+N 新建对话   ·   Ctrl+B 侧栏",
                      fill=TEXT_MUTED, font=(self._mono_family, 8))

    def _show_empty_workspace(self) -> None:
        self.empty_stage.place(x=0, y=58, relwidth=1, relheight=1, height=-58)
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
        self._update_composer_hint()

    def _toolbar_btn(self, parent, text: str, cmd, accent: bool = False) -> tk.Button:
        bgc = ACCENT if accent else "#0e1521"
        fgc = "#04111a" if accent else TEXT
        hover_bg = ACCENT_HOVER if accent else PANEL_HOVER
        btn = tk.Button(parent, text=text, command=cmd, bg=bgc, fg=fgc,
                        activebackground=hover_bg, activeforeground=fgc,
                        relief="flat", bd=0, padx=13, pady=6, cursor="hand2",
                        takefocus=True, font=(self._ui_family, 9, "bold"))
        btn.bind("<Enter>", lambda e: btn.config(bg=hover_bg))
        btn.bind("<Leave>", lambda e: btn.config(bg=bgc))
        return btn

    def _reveal_outline(self, widget: tk.Widget, resting: str, active: str) -> None:
        """默认弱化容器边线，鼠标进入时才给出冷色轮廓反馈。"""
        widget.bind("<Enter>", lambda _event: widget.configure(highlightbackground=active), add="+")
        widget.bind("<Leave>", lambda _event: widget.configure(highlightbackground=resting), add="+")

    def _side_section(self, parent, title: str) -> None:
        tk.Label(parent, text=title, bg=PANEL, fg=TEXT,
                 font=(self._display_family, 11, "bold")).pack(anchor="w", padx=14, pady=(14, 8))

    def _draw_window_backdrop(self, _event=None) -> None:
        """让生成的深空图覆盖窗口底层，并用暗罩保证所有组件可读。

        防抖缓存：仅在窗口尺寸显著变化（≥4px）时重绘，拖动缩放期间
        不每个 Configure 事件都重新缩放完整图片。
        """
        if self._void_art is None:
            return
        canvas = self.backdrop
        w, h = max(canvas.winfo_width(), 1), max(canvas.winfo_height(), 1)
        last = getattr(self, "_backdrop_size", (0, 0))
        if abs(w - last[0]) < 4 and abs(h - last[1]) < 4:
            return   # 尺寸未显著变化：跳过（防抖）
        self._backdrop_size = (w, h)
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
        canvas.create_rectangle(0, 0, w, h, fill=BG, outline="", stipple="gray75")

    def _draw_chat_backdrop(self, width: int, height: int) -> None:
        """绘制聊天页的固定低对比度背景；消息列只覆盖中央阅读区域。"""
        if self._void_art is None or self._chat_backdrop_item is None:
            return
        width, height = max(width, 1), max(height, 1)
        last_w, last_h = self._chat_backdrop_size
        if abs(width - last_w) < 12 and abs(height - last_h) < 12:
            self._pin_chat_backdrop()
            return
        self._chat_backdrop_size = (width, height)
        art = ImageOps.fit(self._void_art, (width, height), Image.Resampling.LANCZOS)
        art = ImageEnhance.Color(art).enhance(.62)
        art = ImageEnhance.Brightness(art).enhance(.42)
        art = Image.blend(art, Image.new("RGB", (width, height), SURFACE), .38)
        self._chat_backdrop_photo = ImageTk.PhotoImage(art)
        self.canvas.itemconfigure(self._chat_backdrop_item, image=self._chat_backdrop_photo)
        self._pin_chat_backdrop()

    def _pin_chat_backdrop(self) -> None:
        """滚动消息时让宇宙背景停留在视口内，而不是随消息一起离场。"""
        if self._chat_backdrop_item is None:
            return
        try:
            self.canvas.coords(self._chat_backdrop_item, 0, self.canvas.canvasy(0))
            self.canvas.tag_lower(self._chat_backdrop_item)
        except tk.TclError:
            pass

    def _show_scrollbar(self, _event=None) -> None:
        """鼠标进入聊天区：显示滚动条（有内容可滚动时才需要）。"""
        if self.quit_flag:
            return
        try:
            if self.canvas.yview()[1] < 0.995:   # 有可滚动内容
                self._chat_scrollbar.place(relx=1, x=-5, y=69, relheight=1,
                                           height=-82, anchor="ne")
                self._scrollbar_visible = True
        except tk.TclError:
            pass

    def _hide_scrollbar(self, _event=None) -> None:
        """鼠标离开聊天区：隐藏滚动条（平滑淡出由 Tk 自动处理）。"""
        if self.quit_flag:
            return
        try:
            self._chat_scrollbar.place_forget()
            self._scrollbar_visible = False
        except tk.TclError:
            pass

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
        self._pin_chat_backdrop()
        self._refresh_jump_button()
        # 实际滚动时若滚动条未显示则显示
        if not self._scrollbar_visible:
            self._show_scrollbar()

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
            self.sidebar_toggle_btn.config(text="显示侧栏")
        else:
            self.sidebar.grid()
            self.sidebar_toggle_btn.config(text="隐藏侧栏")
        self._sidebar_visible = not self._sidebar_visible
        return "break"

    def _update_composer_hint(self, _event=None) -> None:
        """Text 没有原生 placeholder：仅在内容为空时展示轻量提示。"""
        try:
            empty = not self.input_box.get("1.0", "end-1c").strip()
            if empty:
                self._composer_hint.place(x=13, y=11)
            else:
                self._composer_hint.place_forget()
        except tk.TclError:
            pass

    def _resize_composer(self, _event=None) -> None:
        """编辑器随内容扩到 7 行，短消息保持紧凑。"""
        try:
            # 空文本时 end-1c 无效，count 返回 None（Tk 8.6.15 行为）
            cnt = self.input_box.count("1.0", "end-1c", "displaylines")
            lines = int(cnt[0]) if cnt else 1
        except (tk.TclError, ValueError, TypeError):
            lines = 1
        self.input_box.configure(height=max(3, min(7, lines + 1)))
        self._update_composer_hint()

    def _load_void_art(self) -> None:
        """加载居中黑洞主视觉；资源缺失时仍可正常使用聊天窗口。"""
        candidates = (
            BASE_DIR.parent / "assets" / "chat-black-hole-ambient-v2.png",
            BASE_DIR.parent / "assets" / "chat-black-hole-center.png",
        )
        self._void_art = None
        for path in candidates:
            try:
                with Image.open(path) as image:
                    self._void_art = image.convert("RGB")
                break
            except (OSError, ValueError):
                continue

    # ------------------------------------------------------------------ 布局事件
    def _on_frame_configure(self, event=None) -> None:
        should_follow = self._follow_chat or self.canvas.yview()[1] >= .985
        content_h = max(self.scroll_frame.winfo_reqheight(), self.canvas.winfo_height())
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), content_h))
        if should_follow:
            self.canvas.yview_moveto(1.0)
        self._pin_chat_backdrop()
        self._refresh_jump_button()

    def _on_canvas_configure(self, event) -> None:
        content_width = min(840, max(520, event.width - 92))
        self.canvas.coords(self.canvas_window, event.width // 2, 0)
        self.canvas.itemconfig(self.canvas_window, width=content_width)
        self._draw_chat_backdrop(event.width, event.height)
        self._on_frame_configure()
        self._refresh_jump_button()

    # ------------------------------------------------------------------ 后台线程
    def _bg_loop(self) -> None:
        """短任务执行器：只跑不阻塞的 API 请求（状态轮询/会话操作/压缩检查等）。
        SSE 流式读取绝不进入本队列（独立线程），否则确认回传会被流阻塞形成死锁。"""
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

    def _poll_events(self) -> None:
        """Tk 主线程固定周期事件轮询：实时渲染流事件，不等流结束。

        handler 异常不吞掉：记录 traceback、恢复流/UI 状态、显示非阻塞错误，
        并继续处理后续事件。
        """
        if self.quit_flag:
            return
        try:
            self._drain_results()
        except Exception:
            import traceback as _tb
            try:
                log.error("事件轮询 handler 异常：\n%s", _tb.format_exc())
            except Exception:
                pass
            try:
                self._on_stream_error("事件处理异常，已恢复（详见日志）")
            except Exception:
                pass
        try:
            self.root.after(40, self._poll_events)
        except (tk.TclError, RuntimeError):
            pass  # 窗口已销毁

    def _drain_results(self) -> None:
        """处理事件队列（仅在 Tk 主线程调用）。流事件按 task_id 隔离：旧流迟到事件丢弃。"""
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
                "sess_create": self._on_session_created,
                "sess_append_failed": self._on_sess_append_failed,
                "sess_loaded": self._on_session_messages_loaded,
                "stream_prep": self._on_stream_prep,
                "stream_delta": self._on_stream_delta,
                "stream_done": self._on_stream_done,
                "stream_error": self._on_stream_error,
                "stream_tool_call": self._on_stream_tool_call,
                "stream_tool_result": self._on_stream_tool_result,
                "stream_ask": self._on_stream_ask,
                "token_stats": self._on_token_stats,
                "stream_todo": self._on_stream_todo,
                "respond_failed": self._on_respond_failed,
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
        self._daemon_err_fh = _open_daemon_err_log()
        try:
            args = [python, str(BASE_DIR / "app.py"), "--port", str(port)]
            dt = _token_for_base(self.base_url)
            if dt:
                args += ["--token", dt]
            proc = subprocess.Popen(
                args, cwd=str(BASE_DIR), creationflags=CREATE_NO_WINDOW,
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
            lines = DAEMON_ERR_LOG.read_text(encoding="utf-8",
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
            args = [python, str(BASE_DIR / "llm_server.py"), "--port", str(self.llm_port)]
            at = _token_for_base(self.llm_url)
            if at:
                args += ["--token", at]
            proc = subprocess.Popen(
                args, cwd=str(BASE_DIR), creationflags=CREATE_NO_WINDOW,
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
        self.status_model.config(text=self._llm_model)
        if not self._server_sessions_loaded:
            # LLM 后端就绪：拉取持久化会话（只触发一次）
            self._server_sessions_loaded = True
            self._tasks.put(("sessions", self._load_sessions_from_server))
        cfg = load_config()
        if data.get("configured"):
            rm = data.get("reasoning_mode") or "max"
            rm_label = {"max": "最高", "high": "高", "off": "关闭"}.get(rm, rm)
            self.llm_status.config(
                text=f"●  {data.get('model') or '模型已连接'}\n"
                     f"推理 {rm_label}  ·  权限 {cfg.get('confirm_mode', 'auto')}",
                fg=OK)
        else:
            self.llm_status.config(
                text="未配置模型连接。\n打开顶部“设置”填写 API URL、Key 与模型名。",
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
        """后台线程：只拉会话摘要（不拉消息），切换会话时再懒加载完整内容。"""
        code, data, _ = api_request(self.llm_url, "GET", "/api/v1/sessions", timeout=8)
        if code != 200:
            return ("err", "无法连接 LLM 后端，本次会话仅保存在内存（重启丢失）")
        return ("ok", data.get("sessions") or [])

    def _load_session_messages(self, sid: int):
        """后台线程：懒加载单个会话的完整消息。"""
        code, data, _ = api_request(self.llm_url, "GET", f"/api/v1/sessions/{sid}", timeout=8)
        if code != 200:
            return ("err", sid, f"会话 #{sid} 加载失败：{(data or {}).get('detail', '?')}")
        return ("ok", sid, (data.get("session") or {}).get("messages") or [])

    def _on_sessions_loaded(self, payload) -> None:
        """主线程：用后端会话摘要重建本地状态（后端为权威源；消息懒加载）。"""
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
            self._sessions[sid] = {
                "messages": [{"role": "system", "content": SYSTEM_FIRST}],
                "history": [],          # 懒加载后填充
                "title": s.get("title") or "",
                "loaded": False,        # 消息未加载
                "count": s.get("message_count") or 0,
            }
        self._current_sid = 0
        self._switch_session(max(self._sessions))
        self._log(f"已恢复 {len(self._sessions)} 个持久化会话（消息按需加载）", "ok")

    def _append_to_server(self, msgs: list[dict]) -> None:
        """后台线程：把新增消息追加到后端会话（检查状态码，失败记录不静默）。"""
        sid = self._current_sid
        def _do():
            code, data, _ = api_request(
                self.llm_url, "POST", f"/api/v1/sessions/{sid}/messages",
                {"messages": msgs, "request_id": f"chat-{sid}-{int(time.time() * 1000)}"},
                timeout=5)
            if code not in (200, 404):
                detail = (data or {}).get("detail", f"HTTP {code}")
                self._results.put(("sess_append_failed", (sid, detail)))
            elif code == 404:
                self._results.put(("sess_append_failed", (sid, "会话已被删除")))
        self._tasks.put(("sess_append", _do))

    def _on_sess_append_failed(self, payload) -> None:
        """主线程：会话持久化失败提示（不静默）。"""
        sid, detail = payload
        self._log(f"会话 #{sid} 消息保存失败：{detail}", "err")

    def _on_stream_prep(self, payload) -> None:
        """主线程：压缩完成 → 用压缩结果替换本地消息 → 启动独立 SSE 读取线程。"""
        kind, msgs, task_id = payload
        if task_id != self._stream_task_id or not self._streaming:
            return   # 旧流（已 Stop/已替换）的迟到压缩结果：丢弃
        if kind == "compressed":
            self._sessions[self._current_sid]["messages"] = msgs
            self._log("上下文已压缩（发送前，省 tokens）", "ok")
        handle = self._stream_handle
        if handle is None:
            return
        threading.Thread(target=self._stream_reader,
                         args=(msgs, handle, task_id), daemon=True).start()

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
        self._update_composer_hint()
        self._resize_composer()

        sess = self._sessions[self._current_sid]
        sess["history"].append(("user", text))
        sess["messages"].append({"role": "user", "content": text})
        self._append_to_server([{"role": "user", "content": text}])   # 持久化 user 消息

        # 流式输出初始化：唯一 task_id（generation），旧流事件据此隔离
        self._stream_task_id = next(self._task_counter)
        self._stream_cancel = threading.Event()
        self._stream_resp = None
        self._streaming = True
        self._stream_content_acc = ""
        self._stream_reasoning_acc = ""
        self._agent_log = []                 # 工具调用日志行（渲染在回复前）
        self._stream_handle = self._add_message("agent", "◌ 思考中…")
        self._start_pulse()                  # 流式期间状态点呼吸
        # Send → Stop（流式期间可中止）
        self.send_btn.config(state="normal", text="停止  ■", bg=STOP,
                             command=self._stop_stream)
        # 压缩检查（短任务执行器，不阻塞流式线程）——每轮发送量有硬边界
        task_id = self._stream_task_id
        self._tasks.put(("stream_prep",
                         lambda: self._prepare_stream(sess, task_id)))

    def _prepare_stream(self, sess: dict, task_id: int):
        """短任务线程：发送前压缩检查 + 历史检索。返回 ("ready"|"compressed", 消息快照, task_id)。"""
        cfg = load_config()
        window = int(cfg.get("context_window") or 65536)
        # 动态阈值：按任务类型 + 输出预算计算（不再写死 60%；
        # 复杂编码任务保留更多上下文，简单任务更早压缩）
        last_user = next((m.get("content", "") for m in reversed(sess["messages"])
                          if m.get("role") == "user"), "")
        last_user = str(last_user)
        task_kind = ("coding" if (len(last_user) > 300 or any(
            k in last_user for k in ("代码", "重构", "实现", "调试", "bug", "fix", "优化")))
            else "default")
        report = plan_budget(context_window=window, messages=sess["messages"],
                             task=task_kind,
                             reasoning_mode=str(cfg.get("reasoning_mode") or "max"))
        if report.status not in ("compress", "over"):
            return ("ready", self._maybe_retrieve(sess, list(sess["messages"])), task_id)
        # 达到预算阈值：结构化压缩（后端一致性校验失败时保留原文，这里照常发送）
        code, data, _ = api_request(self.llm_url, "POST", "/api/v1/compress",
                                    {"messages": sess["messages"], "keep_recent": KEEP_RECENT},
                                    timeout=90)
        if code == 200 and data.get("compressed"):
            self._compress_count += 1
            self._index_compressed_history(sess["messages"], data)
            return ("compressed", data.get("messages") or sess["messages"], task_id)
        return ("ready", self._maybe_retrieve(sess, list(sess["messages"])), task_id)

    # ---- R3：压缩后历史索引 + 按需检索（本地确定性关键词，无外部依赖）----
    def _index_compressed_history(self, msgs: list, data: dict) -> None:
        """压缩后：索引被压缩的早期消息，保存 retrieval_keys（后续命中时检索原文）。"""
        others = [m for m in msgs if m.get("role") != "system"]
        early = others[:-KEEP_RECENT] if len(others) > KEEP_RECENT else others
        self._history_index.add_messages(early)
        keys = (data.get("stats") or {}).get("retrieval_keys") or []
        for k in keys:
            if k and k not in self._retrieval_keys:
                self._retrieval_keys.append(k)
        self._retrieval_keys = self._retrieval_keys[-60:]

    def _maybe_retrieve(self, sess: dict, messages: list) -> list:
        """当前用户消息命中检索键 → 从历史原文检索相关片段，附加到 system 提示。

        摘要与原文冲突时以原文为准：命中键即插入原文片段（非摘要表述）。
        """
        if not self._retrieval_keys:
            return messages
        last_user = next((m.get("content", "") for m in reversed(messages)
                          if m.get("role") == "user"), "")
        keys = find_keys(str(last_user), self._retrieval_keys)
        if not keys:
            return messages
        hits = self._history_index.search(" ".join(keys))
        if not hits:
            return messages
        snippets = "\n".join(
            f"[{h['role']} {time.strftime('%H:%M', time.localtime(h['ts']))}] {h['snippet']}"
            for h in hits[:4])
        note = (f"（历史原文检索：当前问题涉及之前提到的 {('、'.join(keys[:4]))}，"
                f"以下为相关原文片段，以原文为准）\n{snippets}")
        out = []
        inserted = False
        for m in messages:
            if m.get("role") == "system" and not inserted:
                out.append({"role": "system",
                            "content": (m.get("content") or "") + "\n\n" + note})
                inserted = True
            else:
                out.append(m)
        if not inserted:
            out.insert(0, {"role": "system", "content": note})
        self._log(f"历史检索命中 {len(keys)} 键，插入 {len(hits)} 条原文片段", "info")
        return out

    def _stop_stream(self) -> None:
        """前端中止：设置取消事件 + 关闭网络响应 + 使旧流事件失效，后端断开后停止循环。"""
        if not self._streaming:
            return
        self._stream_cancel.set()
        self._stream_task_id += 1          # 旧流迟到事件全部失效
        if self._stream_resp is not None:
            try:
                self._stream_resp.close()  # 后端收到断开 → 取消 agent 循环
            except Exception:
                pass
        if self._stream_handle is not None:
            self._update_message(self._stream_handle, "⏹ 已由用户中止")
        self._log("agent task stopped by user", "err")
        self._finish_streaming()

    # ------------------------------------------------------------------ 流式
    def _stream_reader(self, snapshot: list[dict], handle, task_id: int) -> None:
        """独立线程：SSE 流式读取（agent 模式），逐事件写入 _results 队列。
        只写队列不碰 Tk 控件；Stop/取消/断开时安全退出。"""
        url = f"{self.llm_url}/api/v1/chat/stream"
        headers = {"Content-Type": "application/json"}
        t = _token_for_base(self.llm_url)
        if t:
            headers["X-Api-Token"] = t
        req = urllib.request.Request(
            url, data=json.dumps({"messages": snapshot, "agent": True},
                                 ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST")
        # 块级 SSE 状态机：按空行分隔的完整事件块解析
        # （event 行 + 一条或多条 data 行 = 一个事件；事件消费后立即重置，
        # 避免 tool_result 后普通 delta 被误当成 tool_result 导致最终文本丢失）
        done_sent = False
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                if task_id != self._stream_task_id:
                    return   # 连接建立前已被 Stop：不报错
                self._stream_resp = resp
                current_event = ""
                buf_lines: list[str] = []
                byte_buf = b""
                for raw in resp:
                    if self._stream_cancel.is_set():
                        return   # 用户已点 Stop
                    # 字节级缓冲：任意网络分块边界（含 UTF-8 多字节被拆开）都安全
                    byte_buf += raw
                    while b"\n" in byte_buf:
                        line_b, byte_buf = byte_buf.split(b"\n", 1)
                        line = line_b.decode("utf-8", "replace").rstrip("\r")
                        if line == "":
                            # 空行 = 事件块结束（分块边界任意位置、CRLF/LF、多行 data 均支持）
                            if not buf_lines:
                                continue
                            kind, payload = self._parse_sse_block(current_event, buf_lines)
                            buf_lines = []
                            current_event = ""      # 事件消费后重置（状态机关键）
                            if kind is None:
                                continue
                            if kind == "delta" and not (payload[0] or payload[1]):
                                continue            # 空增量不产生事件
                            self._results.put((f"stream_{kind}", (task_id, handle, payload)))
                            if kind in ("done", "error"):
                                done_sent = True
                                return              # 每次请求只产生一次 done/error
                            continue
                        if line.startswith("event:"):
                            current_event = line[6:].strip()
                            continue
                        if line.startswith("data:"):
                            buf_lines.append(line[5:].strip())
                            continue
                        # 其他行（注释/未知）忽略
        except Exception as exc:
            if not self._stream_cancel.is_set() and task_id == self._stream_task_id:
                # 用户 Stop 导致的连接中断不报错
                self._results.put(("stream_error", (task_id, handle,
                                                    f"无法连接 LLM 后端：{exc}")))
                return
        # 正常结束（done 事件）已发送；流线程自然退出（EOF）时兜底补一次 done
        if (not done_sent and not self._stream_cancel.is_set()
                and task_id == self._stream_task_id):
            self._results.put(("stream_done", (task_id, handle, None)))

    def _parse_sse_block(self, event: str, data_lines: list[str]):
        """解析一个完整 SSE 事件块（event 行 + 一条或多条 data 行）→ (kind, payload)。

        kind: delta / tool_call / tool_result / ask / todo_update / done / error / None（忽略）。
        未指定 event 的 data 默认按普通 message/delta 处理。
        """
        payload = "\n".join(data_lines)
        if event in ("tool_call", "tool_result", "ask", "todo_update"):
            try:
                return event, json.loads(payload)
            except Exception:
                return event, payload
        if event == "error":
            try:
                d = json.loads(payload)
                return "error", d.get("detail", d) if isinstance(d, dict) else d
            except Exception:
                return "error", payload
        if payload == "[DONE]":
            return "done", None
        try:
            data = json.loads(payload)
            delta = (data.get("choices") or [{}])[0].get("delta") or {}
            return "delta", (delta.get("content") or "", delta.get("reasoning_content") or "")
        except Exception:
            return None, None

    def _parse_sse_chunk(self, line: str, sse_event: str):
        """解析一行 SSE：返回 (kind, payload)。
        kind: delta / tool_call / tool_result / ask / todo_update / done / error /
              event_marker（记录 event 行）/ None（忽略）
        """
        line = line.strip()
        if not line:
            return None, None
        if line.startswith("event:"):
            return "event_marker", line[6:].strip()
        if not line.startswith("data:"):
            return None, None
        payload = line[5:].strip()
        if sse_event in ("tool_call", "tool_result", "ask", "todo_update"):
            try:
                return sse_event, json.loads(payload)
            except Exception:
                return sse_event, payload
        if sse_event == "error":
            try:
                d = json.loads(payload)
                return "error", d.get("detail", d) if isinstance(d, dict) else d
            except Exception:
                return "error", payload
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

    def _is_current_stream(self, task_id: int, handle) -> bool:
        """旧流（已 Stop / 已替换）的迟到事件不得更新当前会话。"""
        return (task_id == self._stream_task_id
                and self._streaming
                and handle is self._stream_handle)

    def _on_stream_tool_call(self, payload) -> None:
        task_id, handle, data = payload
        if not self._is_current_stream(task_id, handle):
            return
        arg_str = _fmt_args(data.get("arguments") or "")
        step_info = f" · 轮次 {data.get('step')}/{data.get('max_steps')}" if data.get("step") else ""
        self._agent_log.append(f"`[⚙ {data.get('name')}]` {arg_str}{step_info}")
        # 日志脱敏：type_text 等长文本参数只记录长度
        self._log(f"tool call: {data.get('name')} {_redact_args(data.get('arguments') or '')}{step_info}",
                  "info")
        self._render_agent_log(handle)

    def _on_stream_tool_result(self, payload) -> None:
        task_id, handle, data = payload
        if not self._is_current_stream(task_id, handle):
            return
        ok = bool(data.get("ok"))
        summary = _summarize_result(data.get("result") or "", ok)
        mark = "✓" if ok else "✗"
        self._agent_log.append(f"  {mark} {summary}")
        self._log(f"tool result: {mark} {summary}", "ok" if ok else "err")
        self._render_agent_log(handle)

    def _on_stream_ask(self, payload) -> None:
        """模型请求确认：弹确认窗（含 diff 展示），用户选择后立即回传服务端。"""
        task_id, handle, data = payload
        if not self._is_current_stream(task_id, handle):
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
        """把确认选择回传 llm_server：立即独立线程执行，绝不排队等 SSE（防死锁）。"""
        def _do():
            code, data, _ = api_request(self.llm_url, "POST", "/api/v1/agent/respond",
                                        {"request_id": request_id, "choice": choice},
                                        timeout=10)
            if code != 200:
                detail = (data or {}).get("detail", f"HTTP {code}")
                self._results.put(("respond_failed", (request_id, choice, detail)))
        threading.Thread(target=_do, daemon=True).start()

    def _on_respond_failed(self, payload) -> None:
        """确认回传失败：界面明确显示（后端超时默认拒绝，不假装已批准）。"""
        request_id, choice, detail = payload
        self._log(f"确认回传失败（request {request_id} → {choice}）：{detail}", "err")
        try:
            messagebox.showerror("确认回传失败",
                                 f"无法将你的选择送达后端（{detail}）。\n"
                                 "安全起见该操作将按拒绝处理，可稍后重试。")
        except Exception:
            pass

    def _on_stream_todo(self, payload) -> None:
        """刷新可折叠任务面板（todo_update 事件）。"""
        task_id, handle, data = payload
        if not self._is_current_stream(task_id, handle):
            return
        todos = (data or {}).get("todos") or []
        self._todo_items = todos[-15:]
        # 标题计数 + 状态
        n_done = sum(1 for t in self._todo_items if t.get("status") == "completed")
        self._todo_toggle.config(
            text=f"{'▾' if self._todo_expanded else '▸'} ☑ 任务清单"
                 f"（{len(self._todo_items)} · 完成 {n_done}）")
        for w in self._todo_body.winfo_children():
            w.destroy()
        if not todos:
            self.todo_frame.grid_remove()
            return
        if not self.todo_frame.winfo_ismapped():
            self.todo_frame.grid()
            if not self._todo_expanded:
                self._todo_body.pack_forget()
        colors = {"pending": WARN, "in_progress": ACCENT, "completed": OK,
                  "failed": STOP, "cancelled": TEXT_DIM}
        for t in self._todo_items:
            row = tk.Frame(self._todo_body, bg="#0d1526")
            row.pack(fill="x", pady=1)
            st = t.get("status", "pending")
            tk.Label(row, text=f"[{st}]", bg="#0d1526", fg=colors.get(st, WARN),
                     font=("Consolas", 8, "bold")).pack(side="left")
            tk.Label(row, text=t.get("title", ""), bg="#0d1526", fg=TEXT,
                     font=("Microsoft YaHei UI", 9), wraplength=700,
                     justify="left").pack(side="left", padx=4)

    def _on_stream_delta(self, payload) -> None:
        task_id, handle, (content, reasoning) = payload
        if not self._is_current_stream(task_id, handle):
            return
        if reasoning:
            self._stream_reasoning_acc += reasoning
        if content:
            self._stream_content_acc += content
            self._render_agent_log(handle)

    def _on_stream_done(self, payload) -> None:
        task_id, handle, _ = payload
        if not self._is_current_stream(task_id, handle):
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
        task_id, handle, detail = payload
        if not self._is_current_stream(task_id, handle):
            return
        self._update_message(handle, f"⚠ {detail}\n\n请检查“设置”中的 API 配置。")
        self._log(f"stream error: {detail}", "err")
        self._finish_streaming()

    def _finish_streaming(self) -> None:
        self._streaming = False
        self._stream_handle = None
        self._stream_resp = None
        self._stream_cancel = threading.Event()
        self._stream_task_id += 1          # 后续迟到事件全部失效
        self._stop_pulse()                 # 恢复状态点常亮
        # Stop → Send
        self.send_btn.config(state="normal", text="发送  ↑", bg=ACCENT,
                             command=self._send_message)
        self._update_session_sidebar()

    def _add_message(self, role: str, text: str) -> None:
        self._hide_empty_workspace()
        is_user = role == "user"
        bubble_bg = USER_BUBBLE if is_user else AGENT_BUBBLE

        container = tk.Frame(self.scroll_frame, bg=self._content_bg)
        container.pack(fill="x", padx=22, pady=(12, 5))

        # 左侧/右侧对齐容器
        align = tk.Frame(container, bg=self._content_bg)
        align.pack(side="right" if is_user else "left", anchor="e" if is_user else "w")

        # 消息头：角色徽章 + 名称 + 时间
        meta = tk.Frame(align, bg=self._content_bg)
        meta.pack(anchor="w" if not is_user else "e", fill="x")
        label = "YOU" if is_user else "AGENT"
        tk.Label(meta, text="●", bg=self._content_bg,
                 fg=ACCENT if is_user else VIOLET,
                 font=(self._ui_family, 7)).pack(
                     side="left" if not is_user else "right")
        tk.Label(meta, text=label, bg=self._content_bg,
                 fg=ACCENT if is_user else VIOLET,
                 font=(self._mono_family, 7, "bold")).pack(
                     side="left" if not is_user else "right")
        tk.Label(meta, text=time.strftime("%H:%M"), bg=self._content_bg, fg=TEXT_MUTED,
                 font=(self._mono_family, 7)).pack(
                     side="left" if not is_user else "right", padx=7)

        # 气泡主体：顶部霓虹微光条 + 独立内容容器（流式更新只重建内容，不触碰发光条）
        bubble = tk.Frame(align, bg=bubble_bg, bd=0, highlightthickness=1,
                          highlightbackground="#28556b" if is_user else "#1a2737")
        bubble.pack(anchor="e" if is_user else "w", pady=(4, 0))
        self._reveal_outline(
            bubble, "#28556b" if is_user else "#1a2737",
            "#5d8da5" if is_user else "#495e79")
        glow = tk.Frame(bubble, bg=ACCENT_DIM if is_user else VIOLET_DIM, height=2)
        glow.pack(fill="x")
        body = tk.Frame(bubble, bg=bubble_bg)
        body.pack(fill="x", expand=True)
        bubble._body = body

        self._render_text(body, text, TEXT)
        self._append_message_actions(body, text, is_user)
        self._on_frame_configure()
        return bubble

    def _update_message(self, handle: tk.Frame, new_text: str) -> None:
        """原位更新已渲染的消息气泡（用于 thinking → 完整回复）。"""
        body = getattr(handle, "_body", None)
        if body is None or not body.winfo_exists():
            body = handle
        for child in body.winfo_children():
            child.destroy()
        self._render_text(body, new_text, TEXT)
        self._append_message_actions(body, new_text, False)
        self._on_frame_configure()

    def _append_message_actions(self, parent: tk.Frame, text: str, is_user: bool) -> None:
        """为每条消息提供轻量复制入口（悬停高亮）；流式更新后会携带最新正文。"""
        if not text.strip() or text.strip() == "◌ 思考中…":
            return
        action = tk.Button(
            parent, text="复制", command=lambda value=text: self._copy_text(value),
            bg=parent["bg"], fg=TEXT_MUTED, activebackground=parent["bg"],
            activeforeground=ACCENT, relief="flat", bd=0, cursor="hand2",
            font=(self._ui_family, 8), padx=3, pady=1)
        action.pack(anchor="e" if is_user else "w", padx=9, pady=(2, 5))
        action.bind("<Enter>", lambda _e: action.configure(fg=ACCENT), add="+")
        action.bind("<Leave>", lambda _e: action.configure(fg=TEXT_MUTED), add="+")

    def _copy_text(self, text: str) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            self._log("message copied", "ok")
        except tk.TclError as exc:
            self._log(f"copy failed: {exc}", "err")

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
        """深色代码卡片：语言徽章 + 等宽代码内容 + 悬停复制。"""
        box = tk.Frame(parent, bg=CODE_BG, highlightthickness=1,
                       highlightbackground=BORDER)
        box.pack(fill="x", padx=10, pady=(6, 2))
        # 头部：语言徽章 + 复制按钮
        head = tk.Frame(box, bg=CODE_BG)
        head.pack(fill="x", padx=10, pady=(5, 0))
        badge = tk.Label(head, text=lang.upper(), bg="#0e2233", fg=ACCENT,
                         padx=6, pady=1, font=(self._mono_family, 7, "bold"))
        badge.pack(side="left")
        copy_btn = tk.Button(
            head, text="复制", bg=CODE_BG, fg=TEXT_MUTED, activebackground=CODE_BG,
            activeforeground=ACCENT, relief="flat", bd=0, cursor="hand2",
            font=(self._ui_family, 8), padx=4, pady=0,
            command=lambda value=code: self._copy_text(value))
        copy_btn.pack(side="right")
        copy_btn.bind("<Enter>", lambda _e: copy_btn.configure(fg=ACCENT), add="+")
        copy_btn.bind("<Leave>", lambda _e: copy_btn.configure(fg=TEXT_MUTED), add="+")
        # 代码内容（超长行折行显示，防止撑爆气泡）
        code_lbl = tk.Label(box, text=code, bg=CODE_BG, fg=CODE_FG, justify="left",
                            anchor="w", wraplength=max(360, self.canvas.winfo_width() - 230),
                            font=(self._mono_family, 9))
        code_lbl.pack(fill="x", padx=10, pady=(2, 7))

    def _render_thinking(self, parent: tk.Frame, reasoning: str) -> None:
        """可折叠的思考过程区（reasoning_content）。"""
        state = {"open": False}
        body = getattr(parent, "_body", None)
        if body is None or not body.winfo_exists():
            body = parent
        inner = tk.Frame(body, bg=CODE_BG)
        btn = tk.Button(body, text="▸  思考过程", command=lambda: _toggle(),
                        bg=body["bg"], fg=TEXT_MUTED, activebackground=body["bg"],
                        activeforeground=VIOLET, relief="flat", bd=0,
                        cursor="hand2", anchor="w", font=(self._mono_family, 8))
        btn.pack(anchor="w", pady=(6, 0), padx=12)

        def _toggle():
            state["open"] = not state["open"]
            btn.config(text=("▾  思考过程" if state["open"] else "▸  思考过程"))
            if state["open"]:
                for child in inner.winfo_children():
                    child.destroy()
                tk.Label(inner, text=reasoning.strip(), bg=CODE_BG, fg=TEXT_DIM,
                         justify="left", wraplength=420, font=(self._ui_family, 9)).pack(
                    fill="x", padx=10, pady=6)
                inner.pack(fill="x", padx=10, pady=(0, 2))
            else:
                inner.pack_forget()
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

        # 用只读 Text + tag 渲染内联样式：短文本保持紧凑，中文长句和混合
        # bold/code 内容可以在任意宽度自然换行，不再用 200 字符阈值猜测。
        available_px = max(320, min(720, self.canvas.winfo_width() - 300))
        natural_px = 0
        for kind, txt in parts:
            fnt = self._mono_font if kind == "code" else (
                self._bold_font if kind == "bold" else self._normal_font)
            natural_px += fnt.measure(txt)
        zero_width = max(1, self._normal_font.measure("0"))
        requested_px = min(max(natural_px + 4, 24), available_px)
        width_chars = max(2, int(requested_px / zero_width) + 1)
        display_lines = max(1, int((natural_px + available_px - 1) / available_px))
        body = tk.Text(
            parent, width=width_chars, height=display_lines, bg=parent["bg"], fg=fg,
            relief="flat", bd=0, highlightthickness=0, wrap="char", cursor="arrow",
            takefocus=False, padx=12, pady=4, spacing1=1, spacing3=1,
            font=self._normal_font, selectbackground="#274b61", selectforeground=TEXT)
        body.tag_configure("normal", font=self._normal_font, foreground=fg)
        body.tag_configure("bold", font=self._bold_font, foreground=fg)
        body.tag_configure("code", font=self._mono_font, foreground=CODE_FG,
                           background="#182231")
        for kind, txt in parts:
            if txt:
                body.insert("end", txt, kind)
        body.configure(state="disabled")
        body.pack(anchor="w", padx=0, pady=(2, 0))

    # ------------------------------------------------------------------ 会话
    def _new_session(self) -> None:
        """创建新会话并切换过去（后台登记持久化，不阻塞 Tk 主线程；失败降级本地自增）。"""
        if self._streaming:
            return
        self._tasks.put(("sess_create", self._create_server_session))

    def _create_server_session(self):
        """短任务线程：后端创建会话。返回 ("ok", sid) 或 ("err", 本地 sid)。"""
        code, data, _ = api_request(self.llm_url, "POST", "/api/v1/sessions", timeout=5)
        if code == 200 and isinstance(data.get("id"), int):
            return ("ok", data["id"])
        # 后端不可用：降级本地自增（会话仅内存）
        sid = (max(self._sessions) + 1) if self._sessions else 1
        return ("err", sid)

    def _on_session_created(self, payload) -> None:
        """主线程：会话创建完成 → 建立本地会话并切换（后端失败时降级内存会话）。"""
        kind, sid = payload
        self._sessions[sid] = {
            "messages": [{"role": "system", "content": SYSTEM_FIRST}],
            "history": [],   # [(role, text), ...] 已展示的对话
            "title": "",
            "loaded": True,  # 新会话无历史，无需懒加载
            "count": 0,
        }
        if kind != "ok":
            self._log("后端不可用：新会话仅保存在内存（重启丢失）", "err")
        self._switch_session(sid)

    def _switch_session(self, sid: int) -> None:
        if self._streaming or sid not in self._sessions:
            return
        self._current_sid = sid
        sess = self._sessions[sid]
        # 懒加载：消息未加载且有历史时后台拉取（完成后刷新界面）
        if not sess.get("loaded") and sess.get("count"):
            def _load(sid=sid):
                return self._load_session_messages(sid)
            self._tasks.put(("sess_load", _load))
        # 重建消息区（已加载的部分）
        for child in self.scroll_frame.winfo_children():
            child.destroy()
        for role, text in sess["history"]:
            self._add_message(role, text)
        if not sess["history"]:
            if sess.get("loaded") or not sess.get("count"):
                self._show_empty_workspace()
        else:
            self._hide_empty_workspace()
        self._update_session_sidebar()

    def _on_session_messages_loaded(self, payload) -> None:
        """主线程：懒加载完成 → 填充会话并重建消息区（仅当仍是当前会话）。"""
        kind, sid, msgs = payload
        if sid not in self._sessions:
            return   # 会话已删除：丢弃
        sess = self._sessions[sid]
        sess["loaded"] = True
        if kind == "err":
            self._log(msgs, "err")
            return
        sess["messages"] = [{"role": "system", "content": SYSTEM_FIRST}] + [
            dict(m) for m in msgs]
        sess["history"] = [(m["role"], m["content"]) for m in msgs]
        if sid == self._current_sid:
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
            bgc = "#101c29" if is_current else SIDEBAR_BG
            row = tk.Frame(self.session_list, bg=bgc, height=58,
                           highlightthickness=1,
                           highlightbackground="#1d3548" if is_current else SIDEBAR_BG)
            title = self._sessions[sid].get("title") or f"会话 #{sid}"
            if query and query not in title.lower() and query not in str(sid):
                continue
            row.pack(fill="x", pady=2)
            row.pack_propagate(False)
            shown += 1
            marker = tk.Frame(row, bg=ACCENT if is_current else bgc, width=3)
            marker.pack(side="left", fill="y")
            marker.pack_propagate(False)
            body = tk.Frame(row, bg=bgc, cursor="hand2")
            body.pack(side="left", fill="both", expand=True, padx=(10, 2), pady=7)
            title_label = tk.Label(body, text=title[:22], bg=bgc,
                                   fg=TEXT if is_current else TEXT_SOFT,
                                   anchor="w", cursor="hand2",
                                   font=(self._ui_family, 9, "bold" if is_current else "normal"))
            title_label.pack(fill="x")
            count = len(self._sessions[sid].get("history") or [])
            detail_label = tk.Label(body, text=f"#{sid}  ·  {count} 条", bg=bgc,
                                    fg=TEXT_MUTED, anchor="w", cursor="hand2",
                                    font=(self._mono_family, 7))
            detail_label.pack(fill="x")
            del_btn = tk.Button(row, text="×", bg=bgc, fg=TEXT_MUTED,
                                relief="flat", bd=0, padx=9, pady=4, cursor="hand2",
                                activebackground=STOP, activeforeground="white",
                                font=(self._display_family, 11),
                                command=lambda s=sid: self._delete_session(s))
            del_btn.pack(side="right")
            for widget in (row, marker, body, title_label, detail_label):
                widget.bind("<Button-1>", lambda _e, s=sid: self._switch_session(s))
                widget.bind("<Enter>", lambda _e, r=row, b=body, t=title_label,
                            d=detail_label, x=del_btn, selected=is_current:
                            self._session_row_hover(r, b, t, d, x, selected, True), add="+")
                widget.bind("<Leave>", lambda _e, r=row, b=body, t=title_label,
                            d=detail_label, x=del_btn, selected=is_current:
                            self.root.after(35, self._session_row_hover,
                                            r, b, t, d, x, selected, False), add="+")
            self._session_buttons[sid] = title_label
        if not shown and self._sessions:
            tk.Label(self.session_list, text="没有匹配的对话", bg=SIDEBAR_BG, fg=TEXT_DIM,
                     font=(self._ui_family, 9)).pack(anchor="w", padx=8, pady=8)
        if self._current_sid not in self._sessions:
            self.session_info.config(text="正在加载会话…")
            return
        cfg = load_config()
        current = self._sessions[self._current_sid]
        current_title = current.get("title") or f"会话 #{self._current_sid}"
        self.chat_title.config(text=current_title)
        self.chat_meta.config(
            text=f"本地工作区  ·  {cfg.get('model') or 'Agent'}  ·  "
                 f"{len(current['history'])} 条消息")
        self.session_info.config(
            text=f"当前会话  #{self._current_sid}  ·  {len(current['history'])} 条消息")
        if not query:
            self._log(f"switched to session #{self._current_sid}", "info")

    def _session_row_hover(self, row, body, title, detail, delete, selected: bool,
                           entered: bool) -> None:
        """会话项默认安静，悬停时才显现边界和删除操作。"""
        try:
            if not entered:
                px, py = self.root.winfo_pointerx(), self.root.winfo_pointery()
                rx, ry = row.winfo_rootx(), row.winfo_rooty()
                if rx <= px <= rx + row.winfo_width() and ry <= py <= ry + row.winfo_height():
                    return
            bgc = "#101c29" if selected else (PANEL_HOVER if entered else SIDEBAR_BG)
            row.configure(bg=bgc,
                          highlightbackground="#41617b" if entered else (
                              "#1d3548" if selected else SIDEBAR_BG))
            body.configure(bg=bgc)
            title.configure(bg=bgc)
            detail.configure(bg=bgc)
            delete.configure(bg=bgc, fg=STOP if entered else TEXT_MUTED)
        except tk.TclError:
            pass

    def _delete_session(self, sid: int) -> None:
        """删除会话；删除当前会话时自动切换到剩余会话，删空则自动新建。"""
        if self._streaming or sid not in self._sessions:
            return
        if not messagebox.askyesno("删除会话", "确定删除该会话？此操作不可恢复。"):
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

    def _open_quant_center(self) -> None:
        """Start/check only the isolated quant services in a worker thread."""
        if self._quant_busy:
            return
        try:
            cfg = load_config()
            current_config = QuantIntegrationConfig.from_mapping(cfg)
            controller = self._quant_controller
            if controller is None or controller.config != current_config:
                controller = QuantServiceController(current_config)
                self._quant_controllers.append(controller)
        except (ValueError, QuantLaunchError) as exc:
            self._set_quant_button("量化中心 ⚠", STOP, enabled=True)
            self._log(f"量化中心配置错误：{str(exc)[:180]}", "err")
            messagebox.showerror("量化中心", str(exc), parent=self.root)
            return
        if not controller.config.enabled:
            self._set_quant_button("量化中心 ⚠", WARN, enabled=True)
            messagebox.showinfo("量化中心", "量化中心已禁用。请在设置 → 量化中启用。", parent=self.root)
            return
        self._quant_busy = True
        self._quant_controller = controller
        self._set_quant_button("检查量化服务…", WARN, enabled=False)
        threading.Thread(target=self._quant_open_worker, args=(controller,), daemon=True).start()

    def _quant_open_worker(self, controller: QuantServiceController) -> None:
        def progress(stage: str) -> None:
            labels = {
                "checking": "检查量化服务…",
                "starting-backend": "启动量化后端…",
                "starting-gui": "启动量化中心…",
                "opening": "打开量化中心…",
            }
            try:
                self.root.after(0, self._set_quant_button, labels.get(stage, "启动量化中心…"), WARN, False)
            except tk.TclError:
                pass
        try:
            status = controller.open_quant_center(progress=progress)
            self.root.after(0, self._quant_open_done, status, None)
        except QuantLaunchError as exc:
            self.root.after(0, self._quant_open_done, None, exc)
        except Exception as exc:
            error = QuantLaunchError("STARTUP_ERROR", "量化中心启动失败", stage="集成控制层", detail=str(exc))
            self.root.after(0, self._quant_open_done, None, error)

    def _quant_open_done(self, status, error) -> None:
        self._quant_busy = False
        if error is None:
            self._quant_status = "可用"
            self._set_quant_button("量化中心 · 可用", OK, enabled=True)
            self._log("量化中心已打开（Paper Trading only）", "ok")
            return
        self._quant_status = "启动失败"
        self._set_quant_button("量化中心 ⚠", STOP, enabled=True)
        self._log(f"量化中心失败：{error.code} · {error.user_message}", "err")
        detail = error.user_message
        if error.detail:
            detail += f"\n诊断：{error.detail[:240]}"
        if error.manual_command:
            detail += f"\n手动启动：{error.manual_command}"
        self._show_quant_error(error, detail)

    def _show_quant_error(self, error: QuantLaunchError, detail: str) -> None:
        """Offer retry and project-local log viewing without exposing secrets."""
        win = tk.Toplevel(self.root)
        win.title("量化中心启动失败")
        win.configure(bg=BG)
        win.transient(self.root)
        win.resizable(False, False)
        tk.Label(win, text="QUANT CENTER // STARTUP FAULT", bg=BG, fg=STOP,
                 font=(self._mono_family, 9, "bold")).pack(anchor="w", padx=22, pady=(18, 4))
        tk.Label(win, text=f"{error.code} · {detail}", bg=BG, fg=TEXT,
                 justify="left", anchor="w", wraplength=600,
                 font=(self._ui_family, 9)).pack(fill="x", padx=22, pady=(0, 12))
        log_dir = (self._quant_controller.config.project_path / "var" / "integration"
                   if self._quant_controller is not None else BASE_DIR.parent / "quant-agent-lab" / "var" / "integration")
        tk.Label(win, text=f"日志目录：{log_dir}", bg=BG, fg=TEXT_DIM,
                 justify="left", anchor="w", wraplength=600,
                 font=(self._mono_family, 8)).pack(fill="x", padx=22, pady=(0, 12))
        actions = tk.Frame(win, bg=BG)
        actions.pack(fill="x", padx=18, pady=(0, 18))

        def retry() -> None:
            win.destroy()
            self._open_quant_center()

        def view_logs() -> None:
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                if sys.platform == "win32":
                    os.startfile(str(log_dir))
                else:
                    subprocess.Popen(["xdg-open", str(log_dir)], start_new_session=True)
            except Exception as exc:
                messagebox.showinfo("量化中心日志", f"日志目录：\n{log_dir}\n\n无法自动打开：{exc}", parent=win)

        self._btn(actions, "查看日志", view_logs).pack(side="left", padx=4)
        self._btn(actions, "关闭", win.destroy).pack(side="right", padx=4)
        self._btn(actions, "重试", retry, accent=True).pack(side="right", padx=4)
        win.grab_set()

    def _set_quant_button(self, text: str, color: str, enabled: bool = True) -> None:
        try:
            self.quant_center_btn.config(text=f"● {text}", fg=color,
                                         state="normal" if enabled else "disabled")
        except tk.TclError:
            pass

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

    # ---- 流式期间状态点呼吸动画（低开销：仅切换 Label 颜色）----
    def _start_pulse(self) -> None:
        if getattr(self, "_pulse_after", None) is not None:
            return
        state = {"on": False}

        def _tick():
            if self.quit_flag:
                return
            state["on"] = not state["on"]
            try:
                self.status_dot.config(fg=OK if state["on"] else "#1f7a5c")
            except tk.TclError:
                return
            self._pulse_after = self.root.after(520, _tick)

        self._pulse_after = self.root.after(0, _tick)

    def _stop_pulse(self) -> None:
        after = getattr(self, "_pulse_after", None)
        if after is not None:
            try:
                self.root.after_cancel(after)
            except (tk.TclError, ValueError):
                pass
            self._pulse_after = None
        try:
            self.status_dot.config(fg=OK)
        except tk.TclError:
            pass

    def _log(self, msg: str, kind: str = "info") -> None:
        """运行日志：有上限环形缓冲（不显示不无限增长的 Tk Text，避免内存膨胀）。"""
        colors = {"ok": OK, "err": STOP, "info": TEXT_DIM}
        stamp = time.strftime("%H:%M:%S")
        self._log_buf.append((f"[{stamp}] {msg}", colors.get(kind, TEXT_DIM)))

    def on_close(self) -> None:
        """关闭窗口：停止轮询/动画、取消网络任务、关闭文件句柄。"""
        self.quit_flag = True
        self._stop_pulse()
        self._stream_cancel.set()
        if self._stream_resp is not None:
            try:
                self._stream_resp.close()   # 通知后端断开 → 取消 agent 循环
            except Exception:
                pass
        if self._daemon_err_fh is not None:
            try:
                self._daemon_err_fh.close()
            except Exception:
                pass
        cfg = load_config()
        controllers = list(dict.fromkeys(self._quant_controllers))
        if self._quant_controller is not None and self._quant_controller not in controllers:
            controllers.append(self._quant_controller)
        if controllers and _as_bool_setting(cfg.get("quant_stop_owned_processes_on_exit"), True):
            # Stop only exact Popen objects owned by this controller, but do it
            # off the Tk thread so a slow child cannot freeze window teardown.
            threading.Thread(
                target=lambda: [item.close_owned_processes() for item in controllers],
                name="quant-owned-process-cleanup",
                daemon=False,
            ).start()
        try:
            self.root.destroy()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="PC Agent Chat")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    root = tk.Tk()
    ChatApp(root, f"http://127.0.0.1:{args.port}")
    root.mainloop()


if __name__ == "__main__":
    main()
