"""
PC Agent Daemon — 桌面 GUI 控制面板（Tkinter，零额外运行时依赖）

功能
----
- 实时屏幕预览（经 /api/v1/screenshot）：点击预览画面 = 在对应位置点击电脑；
- 状态徽章：Idle / Busy（含当前动作与排队数）/ Stopped / 离线；
- 紧急止停（STOP）与恢复（Reset）按钮；
- 命令面板：输入文字、发送按键、常用快捷键；
- 自动拉起 / 复用本地 Daemon（python app.py），失败给出明确提示；
- 屏幕权限自检：当前会话无法访问桌面时显式警示（SSH / 服务方式运行无效）。

GUI 与后端完全解耦：只通过 127.0.0.1 上的 HTTP API 通信，
所有网络请求在后台线程执行，Tk 主线程永不阻塞。

运行：python gui.py [--host 127.0.0.1] [--port 8000] [--no-spawn] [--smoke]
"""

from __future__ import annotations

import argparse
import io
import json
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageTk

BASE_DIR = Path(__file__).resolve().parent
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Windows 显示缩放下让窗口清晰（物理像素）
if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

# 状态徽章配色（与 Web 版一致）
MODE_COLOR = {"idle": "#22c55e", "busy": "#f59e0b", "stopped": "#ef4444", "offline": "#6b7280"}
MODE_TEXT = {
    "idle": "Idle（空闲）",
    "busy": "Busy（执行中）",
    "stopped": "Stopped（已止停）",
    "offline": "Offline（离线）",
}


def api_request(base_url: str, method: str, path: str, payload=None,
                timeout: float = 15, raw: bool = False):
    """同步 HTTP 请求（在后台线程调用）。raw=True 返回 (code, bytes, headers)。"""
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
                return resp.status, {"detail": "响应不是 JSON（端口可能被非 Daemon 程序占用）"}, None
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", "")
        except Exception:
            detail = f"HTTP {e.code}"
        return e.code, {"detail": detail}, None
    except Exception as e:
        return 0, {"detail": f"无法连接 Daemon（{e}）"}, None


class GuiApp:
    def __init__(self, root: tk.Tk, base_url: str, spawn: bool, smoke: bool):
        self.root = root
        self.base_url = base_url
        self.spawn = spawn
        self.smoke = smoke
        self.smoke_ok = False
        self.quit_flag = False

        self._tasks: queue.Queue = queue.Queue()    # (kind, callable)
        self._results: queue.Queue = queue.Queue()  # (kind, payload)
        self._img: Image.Image | None = None        # 原始截图（坐标换算基准）
        self._photo: ImageTk.PhotoImage | None = None
        self._status: dict | None = None
        self._connecting = True
        self._daemon_err_fh = None

        # 后台 HTTP 线程：Tk 主线程永不执行网络调用
        threading.Thread(target=self._bg_loop, daemon=True).start()

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._start)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.root.title("PC Agent Daemon 控制台")
        self.root.geometry("1100x700")
        self.root.configure(bg="#0d1117")

        # 顶部状态栏
        bar = tk.Frame(self.root, bg="#161b22", bd=0, highlightthickness=1,
                       highlightbackground="#30363d")
        bar.pack(fill="x")
        tk.Label(bar, text="🖥️ PC Agent Daemon", bg="#161b22", fg="#e6edf3",
                 font=("Microsoft YaHei UI", 13, "bold")).pack(side="left", padx=12, pady=8)
        self.badge = tk.Label(bar, text="连接中…", bg="#161b22", fg="#8b949e",
                              font=("Microsoft YaHei UI", 11, "bold"), padx=10)
        self.badge.pack(side="left", padx=8)
        self.toast_lbl = tk.Label(bar, text="", bg="#161b22", fg="#8b949e",
                                  font=("Microsoft YaHei UI", 10))
        self.toast_lbl.pack(side="left", padx=8)
        self.topmost_var = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="窗口置顶", variable=self.topmost_var,
                       command=lambda: self.root.attributes("-topmost", self.topmost_var.get()),
                       bg="#161b22", fg="#8b949e", activebackground="#161b22",
                       activeforeground="#e6edf3", selectcolor="#161b22",
                       font=("Microsoft YaHei UI", 10)).pack(side="right", padx=10)
        self.btn_reset = tk.Button(bar, text="恢复 (Reset)", command=self._submit_reset,
                                   bg="#161b22", fg="#4ade80", activebackground="#1c2128",
                                   activeforeground="#4ade80", relief="flat", bd=0,
                                   padx=12, pady=4, cursor="hand2",
                                   font=("Microsoft YaHei UI", 10, "bold"))
        self.btn_reset.pack(side="right", padx=6, pady=6)
        self.btn_stop = tk.Button(bar, text="紧急止停 (STOP)", command=self._submit_stop,
                                  bg="#ef4444", fg="white", activebackground="#dc2626",
                                  relief="flat", bd=0, padx=16, pady=4, cursor="hand2",
                                  font=("Microsoft YaHei UI", 11, "bold"))
        self.btn_stop.pack(side="right", padx=10, pady=6)

        # 主体：左侧预览 + 右侧控制面板
        main = tk.Frame(self.root, bg="#0d1117")
        main.pack(fill="both", expand=True, padx=10, pady=10)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        preview_frame = tk.Frame(main, bg="#000000", highlightthickness=1,
                                 highlightbackground="#30363d")
        preview_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.canvas = tk.Canvas(preview_frame, bg="#000000", highlightthickness=0,
                                cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(400, 240, fill="#8b949e", font=("Microsoft YaHei UI", 12),
                                text="正在连接 Daemon…", tags="placeholder")
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        tk.Label(preview_frame, text="点击画面 = 在对应位置点击电脑",
                 bg="#000000", fg="#8b949e", font=("Microsoft YaHei UI", 9),
                 anchor="w").pack(fill="x", padx=8, pady=4)

        side = tk.Frame(main, bg="#0d1117", width=330)
        side.grid(row=0, column=1, sticky="ns")
        side.pack_propagate(False)

        # 状态信息
        info = tk.Frame(side, bg="#161b22", highlightthickness=1, highlightbackground="#30363d")
        info.pack(fill="x", pady=(0, 8))
        self._kv_rows = {}
        for label in ("屏幕权限", "屏幕分辨率", "排队任务", "最近动作", "FAILSAFE"):
            row = tk.Frame(info, bg="#161b22")
            row.pack(fill="x", padx=10, pady=2)
            tk.Label(row, text=label, bg="#161b22", fg="#8b949e", width=8, anchor="w",
                     font=("Microsoft YaHei UI", 10)).pack(side="left")
            val = tk.Label(row, text="—", bg="#161b22", fg="#e6edf3", anchor="w",
                           font=("Microsoft YaHei UI", 10))
            val.pack(side="left", fill="x", expand=True)
            self._kv_rows[label] = val

        # 命令面板
        cmd = tk.Frame(side, bg="#161b22", highlightthickness=1, highlightbackground="#30363d")
        cmd.pack(fill="x", pady=(0, 8))
        tk.Label(cmd, text="命令面板", bg="#161b22", fg="#8b949e",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=10, pady=(8, 4))

        text_row = tk.Frame(cmd, bg="#161b22")
        text_row.pack(fill="x", padx=10, pady=2)
        self.text_entry = tk.Entry(text_row, bg="#1c2128", fg="#e6edf3", insertbackground="#e6edf3",
                                   relief="flat", font=("Microsoft YaHei UI", 10))
        self.text_entry.pack(side="left", fill="x", expand=True, ipady=4)
        tk.Button(text_row, text="输入文字", command=self._submit_type, bg="#21262d", fg="#e6edf3",
                  activebackground="#30363d", relief="flat", cursor="hand2",
                  font=("Microsoft YaHei UI", 10)).pack(side="left", padx=(6, 0))

        key_row = tk.Frame(cmd, bg="#161b22")
        key_row.pack(fill="x", padx=10, pady=2)
        self.key_entry = tk.Entry(key_row, bg="#1c2128", fg="#e6edf3", insertbackground="#e6edf3",
                                  relief="flat", font=("Consolas", 10))
        self.key_entry.pack(side="left", fill="x", expand=True, ipady=4)
        tk.Button(key_row, text="按键", command=self._submit_key, bg="#21262d", fg="#e6edf3",
                  activebackground="#30363d", relief="flat", cursor="hand2",
                  font=("Microsoft YaHei UI", 10)).pack(side="left", padx=(6, 0))

        chips = tk.Frame(cmd, bg="#161b22")
        chips.pack(fill="x", padx=10, pady=6)
        for i, key in enumerate(["enter", "esc", "tab", "space", "ctrl+c", "ctrl+v", "ctrl+a", "alt+tab"]):
            tk.Button(chips, text=key, bg="#21262d", fg="#c9d1d9", activebackground="#30363d",
                      relief="flat", cursor="hand2", font=("Consolas", 9), padx=6,
                      command=lambda k=key: self._submit_execute({"action": "press_key", "key": k})
                      ).grid(row=i // 4, column=i % 4, padx=2, pady=2, sticky="ew")
        chips.columnconfigure((0, 1, 2, 3), weight=1)
        self.dbl_var = tk.BooleanVar(value=False)
        tk.Checkbutton(cmd, text="画面点击使用双击", variable=self.dbl_var,
                       bg="#161b22", fg="#8b949e", activebackground="#161b22",
                       activeforeground="#e6edf3", selectcolor="#161b22",
                       font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=10, pady=(0, 8))
        self.refresh_var = tk.BooleanVar(value=True)
        tk.Checkbutton(cmd, text="自动刷新画面", variable=self.refresh_var,
                       bg="#161b22", fg="#8b949e", activebackground="#161b22",
                       activeforeground="#e6edf3", selectcolor="#161b22",
                       font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=10, pady=(0, 4))
        tk.Button(cmd, text="刷新画面", command=self._submit_refresh, bg="#21262d", fg="#e6edf3",
                  activebackground="#30363d", relief="flat", cursor="hand2",
                  font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=10, pady=(0, 10))

        # 日志区
        log_frame = tk.Frame(side, bg="#161b22", highlightthickness=1, highlightbackground="#30363d")
        log_frame.pack(fill="both", expand=True)
        tk.Label(log_frame, text="日志", bg="#161b22", fg="#8b949e",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=10, pady=(8, 4))
        self.log_text = tk.Text(log_frame, bg="#0d1117", fg="#8b949e", relief="flat",
                                font=("Consolas", 9), height=8, state="disabled", wrap="none")
        self.log_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    # ----------------------------------------------------------- 后台线程
    def _bg_loop(self) -> None:
        while not self.quit_flag:
            try:
                kind, fn = self._tasks.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                result = fn()
            except Exception as exc:  # 任务内异常兜底
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
                "shot": self._on_shot,
                "exec": self._on_exec,
            }.get(kind)
            if handler:
                handler(payload)

    # ----------------------------------------------------------- 启动流程
    def _start(self) -> None:
        self._tasks.put(("start", self._ensure_daemon))

    def _ensure_daemon(self):
        """探测 Daemon；未运行且允许时自动拉起 python app.py。"""
        def probe():
            code, data, _ = api_request(self.base_url, "GET", "/api/v1/status", timeout=3)
            return code == 200 and data.get("daemon") == "running"
        if probe():
            return ("ok", "已连接运行中的 Daemon")
        if not self.spawn:
            return ("err", "未检测到 Daemon 且 --no-spawn 已禁用自动拉起，请先运行 python app.py")
        port = urllib.request.urlparse(self.base_url).port or 8000
        python = self._pick_daemon_python()
        self._ui_log(f"未检测到 Daemon，正在用 {Path(python).name} 自动启动 app.py --port {port} …", "info")
        self._daemon_err_fh = open(BASE_DIR / "daemon.err.log", "a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                [python, str(BASE_DIR / "app.py"), "--port", str(port)],
                cwd=str(BASE_DIR), creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=self._daemon_err_fh,
            )
        except Exception as exc:
            self._daemon_err_fh.close()
            return ("err", f"自动启动 Daemon 失败：{exc}")
        for _ in range(60):  # 最多等 30 秒
            time.sleep(0.5)
            if probe():
                return ("ok", "Daemon 已自动启动并就绪")
            if proc.poll() is not None:  # 子进程闪退：立即报出真实原因
                return ("err", f"Daemon 启动失败（进程已退出）：{self._read_daemon_err()}")
        return ("err", "等待 Daemon 启动超时（30s），请手动运行 python app.py")

    @staticmethod
    def _pick_daemon_python() -> str:
        """选择启动 Daemon 的解释器：优先能加载全部依赖的（一般是 .venv）。

        依赖（fastapi/pyautogui/pynput）装在 .venv 里，而用户可能用全局
        python 运行 gui.py——此时必须改用 venv 解释器拉起 Daemon。
        """
        candidates = [
            sys.executable,
            str(BASE_DIR / ".venv" / "Scripts" / "python.exe"),
            str(BASE_DIR / "venv" / "Scripts" / "python.exe"),
        ]
        probe_code = ("import importlib.util;"
                      "print(all(importlib.util.find_spec(m) for m in "
                      "('fastapi', 'pyautogui', 'uvicorn')))")  # pynput 可选，不参与判定
        for cand in dict.fromkeys(candidates):
            if not Path(cand).exists():
                continue
            try:
                out = subprocess.run([cand, "-c", probe_code],
                                     capture_output=True, text=True, timeout=10)
                if "True" in out.stdout:
                    return cand
            except Exception:
                continue
        return sys.executable  # 找不到就退回当前解释器（失败原因会显示在日志里）

    def _read_daemon_err(self) -> str:
        """读取 Daemon 子进程的 stderr 尾部，用于诊断启动失败。"""
        try:
            if self._daemon_err_fh is not None:
                self._daemon_err_fh.close()
            lines = BASE_DIR.joinpath("daemon.err.log").read_text(encoding="utf-8",
                                                                  errors="replace").strip().splitlines()
            return " | ".join(lines[-3:])[-400:] if lines else "无错误输出"
        except Exception:
            return "无法读取错误日志"

    def _on_start(self, payload) -> None:
        kind, msg = payload
        self._connecting = False
        self._ui_log(msg, kind)
        if kind == "ok":
            self.badge.config(text="已连接", fg="#22c55e")
            self._submit_poll()
            self._submit_refresh()
        else:
            self.badge.config(text="连接失败", fg="#ef4444")
            if self.smoke:
                self._finish_smoke(False)
        # 周期任务（轮询状态 / 自动刷新截图）
        self.root.after(1500, self._tick_status)
        self.root.after(2500, self._tick_refresh)

    def _tick_status(self) -> None:
        if not self.quit_flag:
            self._submit_poll()
            self.root.after(1500, self._tick_status)

    def _tick_refresh(self) -> None:
        if not self.quit_flag:
            if self.refresh_var.get() and self._status and self._status.get("daemon") == "running":
                self._submit_refresh()
            self.root.after(2500, self._tick_refresh)

    # ----------------------------------------------------------- 任务提交
    def _submit_poll(self) -> None:
        self._tasks.put(("status", lambda: api_request(self.base_url, "GET", "/api/v1/status")))

    def _submit_refresh(self) -> None:
        self._tasks.put(("shot", lambda: api_request(
            self.base_url, "GET", f"/api/v1/screenshot?t={int(time.time() * 1000)}",
            timeout=20, raw=True)))

    def _submit_execute(self, payload, silent: bool = False) -> None:
        self._tasks.put(("exec", lambda: (silent, payload,
            api_request(self.base_url, "POST", "/api/v1/execute", payload))))

    def _submit_stop(self) -> None:
        if not tk.messagebox.askyesno("紧急止停", "确认紧急止停？将拒绝所有后续指令，直到点击\"恢复\"。"):
            return
        self._tasks.put(("exec", lambda: (False, None,
            api_request(self.base_url, "POST", "/api/v1/stop"))))

    def _submit_reset(self) -> None:
        self._tasks.put(("exec", lambda: (False, None,
            api_request(self.base_url, "POST", "/api/v1/reset"))))

    def _submit_type(self) -> None:
        text = self.text_entry.get()
        if not text:
            return self._toast("请输入要发送的文字", "#ef4444")
        self._submit_execute({"action": "type_text", "text": text})

    def _submit_key(self) -> None:
        key = self.key_entry.get().strip()
        if not key:
            return self._toast("请输入按键，如 enter 或 ctrl+c", "#ef4444")
        self._submit_execute({"action": "press_key", "key": key})

    # ----------------------------------------------------------- 结果处理
    def _on_status(self, result) -> None:
        code, data, _ = result
        if code != 200:
            self._set_badge("offline")
            return
        self._status = data
        mode = data.get("mode", "offline")
        self._set_badge(mode)
        self.btn_stop.config(state="disabled" if mode == "stopped" else "normal")
        self.btn_reset.config(state="normal" if mode == "stopped" else "disabled")
        self._kv_rows["屏幕权限"].config(text="正常 ✓" if data.get("screen_access") else "不可用 ✗（需桌面会话）",
                                        fg="#22c55e" if data.get("screen_access") else "#ef4444")
        self._kv_rows["屏幕分辨率"].config(
            text=f"{data['screen_size']['width']} × {data['screen_size']['height']}")
        self._kv_rows["排队任务"].config(text=str(data.get("queued", 0)))
        self._kv_rows["最近动作"].config(
            text=(f"{data['last_action']} @ "
                  f"{time.strftime('%H:%M:%S', time.localtime(data['last_action_at']))}"
                  if data.get("last_action") else "—"))
        self._kv_rows["FAILSAFE"].config(text="已启用" if data.get("fail_safe") else "未启用")
        if self.smoke and data.get("daemon") == "running":
            if not data.get("screen_access"):
                self._ui_log("⚠ 当前会话无法访问屏幕（无桌面会话），控制功能不可用", "err")
                self._finish_smoke(False)
            # screen_access 正常：继续等待截图成功（_on_shot）才算全链路通过

    def _set_badge(self, mode: str) -> None:
        color = MODE_COLOR.get(mode, "#6b7280")
        if mode == "busy" and self._status:
            text = f"Busy · {self._status.get('current_action') or '执行中'}"
            if self._status.get("queued"):
                text += f" · 排队{self._status['queued']}"
        else:
            text = MODE_TEXT.get(mode, mode)
        self.badge.config(text=text, fg=color)

    def _on_shot(self, result) -> None:
        if result[0] == "err":
            self._ui_log(f"截图失败：{result[1]}", "err")
            return self._finish_smoke(False)
        code, body, _ = result
        if code != 200:
            self._ui_log(f"截图失败 HTTP {code}", "err")
            return self._finish_smoke(False)
        try:
            img = Image.open(io.BytesIO(body))
        except Exception as exc:
            self._ui_log(f"截图解码失败：{exc}", "err")
            return self._finish_smoke(False)
        self._render_screenshot(img)
        self._finish_smoke(True)  # smoke 模式：状态 + 截图均成功后通过

    def _render_screenshot(self, img: Image.Image) -> None:
        cw = max(self.canvas.winfo_width(), 400)
        ch = max(self.canvas.winfo_height(), 240)
        scale = min(cw / img.width, ch / img.height)
        dw, dh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
        self._photo = ImageTk.PhotoImage(img.resize((dw, dh), Image.LANCZOS))
        self._img = img
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

    def _on_exec(self, result) -> None:
        silent, payload, http = result
        code, data, _ = http
        if code == 200:
            self._ui_log(f"✓ {data.get('action')} 完成"
                         + (f" @ ({', '.join(map(str, data['position']))})"
                            if data.get("position") and data["position"][0] is not None else ""))
        else:
            self._ui_log(f"✗ 失败（HTTP {code}）：{data.get('detail', '')}", "err")
        self._submit_refresh()  # 动作后立即刷新画面
        self._submit_poll()

    def _on_canvas_click(self, event) -> None:
        if self._img is None or self._photo is None:
            return
        dw, dh = self._photo.width(), self._photo.height()
        if dw <= 0 or dh <= 0:
            return
        x = min(max(int(event.x * self._img.width / dw), 0), self._img.width - 1)
        y = min(max(int(event.y * self._img.height / dh), 0), self._img.height - 1)
        clicks = 2 if self.dbl_var.get() else 1
        self._toast(f"点击屏幕 ({x}, {y})" + (" 双击" if clicks == 2 else "") + "…", "#3b82f6")
        self._submit_execute({"action": "click", "x": x, "y": y, "clicks": clicks}, silent=True)

    # ----------------------------------------------------------- 通用
    def _toast(self, msg: str, color: str = "#8b949e") -> None:
        self.toast_lbl.config(text=msg, fg=color)
        self.root.after(3000, lambda: self.toast_lbl.config(text=""))

    def _ui_log(self, msg: str, kind: str = "info") -> None:
        colors = {"ok": "#4ade80", "err": "#f87171", "info": "#8b949e"}
        self.log_text.config(state="normal")
        stamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{stamp}] {msg}\n", kind)
        self.log_text.tag_configure("ok", foreground="#4ade80")
        self.log_text.tag_configure("err", foreground="#f87171")
        self.log_text.tag_configure("info", foreground="#8b949e")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _finish_smoke(self, ok: bool) -> None:
        """--smoke 模式：自检完成后自动关闭窗口。"""
        if not self.smoke:
            return
        self.smoke_ok = ok
        print(f"GUI SMOKE {'PASS' if ok else 'FAIL'}"
              + ("" if ok else "（原因见上方日志）"))
        self.quit_flag = True
        self.root.after(400, self.root.destroy)

    def on_close(self) -> None:
        self.quit_flag = True
        self.root.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(description="PC Agent Daemon 桌面控制面板")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-spawn", action="store_true",
                        help="不自动拉起 Daemon（要求已运行 python app.py）")
    parser.add_argument("--smoke", action="store_true",
                        help="自检模式：连接 + 截图验证后自动退出（用于测试）")
    args = parser.parse_args()
    base_url = f"http://{args.host}:{args.port}"
    root = tk.Tk()
    app = GuiApp(root, base_url, spawn=not args.no_spawn, smoke=args.smoke)
    root.mainloop()
    return 0 if not app.smoke or app.smoke_ok else 1


if __name__ == "__main__":
    sys.exit(main())
