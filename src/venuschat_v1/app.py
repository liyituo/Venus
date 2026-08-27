"""VenusChat V1 — independent classical-minimal Windows frontend."""

from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import tkinter as tk

from . import theme as t
from .api_client import ApiClient
from .backend_bridge import BackendBridge
from .chat_view import ChatView
from .config_store import load_config
from .settings_view import SettingsView
from .widgets import Dot, FlatButton, MenuPopup, separator

try:   # 量化中心控制层可选：缺失时 V1 仍可运行，按钮降级为提示
    from quant_integration import (
        QuantIntegrationConfig, QuantLaunchError, QuantServiceController)
except ModuleNotFoundError:  # pragma: no cover
    QuantIntegrationConfig = QuantLaunchError = QuantServiceController = None


class HeaderLink(tk.Frame):
    """Text navigation item with a quiet active underline."""

    def __init__(self, parent: tk.Misc, text: str, command, *, fonts: t.Fonts,
                 with_dot: bool = False) -> None:
        width = max(t.s(76), fonts.small.measure(text) + t.s(42 if with_dot else 30))
        super().__init__(parent, bg=t.HEADER, cursor="hand2",
                         height=t.s(58), width=width)
        self.pack_propagate(False)
        self.command = command
        self.active = False
        self._base_text = text
        self._fonts = fonts
        textcol = tk.Frame(self, bg=t.HEADER)
        textcol.pack(side="left", fill="both", expand=True)
        self.label = tk.Label(
            textcol,
            text=text,
            bg=t.HEADER,
            fg=t.INK_SOFT,
            font=fonts.small,
            cursor="hand2",
            padx=t.s(14),
        )
        self.label.pack(side="left", fill="both", expand=True)
        self.dot = None
        if with_dot:
            self.dot = Dot(textcol, color=t.INK_FAINT, size=6, bg=t.HEADER)
            self.dot.place(relx=1, x=-t.s(12), rely=.5, anchor="e")
        self.line = tk.Frame(self, bg=t.HEADER, height=2)
        self.line.pack(fill="x", side="bottom", padx=t.s(10))
        for widget in (self, textcol, self.label, self.line):
            widget.bind("<Button-1>", lambda _event: self.command(), add="+")
            widget.bind("<Enter>", lambda _event: self._paint(True), add="+")
            widget.bind("<Leave>", lambda _event: self.after(16, self._settle), add="+")

    def set_status(self, color: str, suffix: str = "") -> None:
        if self.dot is None:
            return
        try:
            self.dot.set_color(color)
            text = self._base_text + suffix
            self.label.configure(text=text)
            self.configure(width=max(t.s(76), self._fonts.small.measure(text)
                                     + t.s(46)))
        except tk.TclError:
            pass

    def _inside(self) -> bool:
        x, y = self.winfo_pointerx(), self.winfo_pointery()
        left, top = self.winfo_rootx(), self.winfo_rooty()
        return left <= x <= left + self.winfo_width() and top <= y <= top + self.winfo_height()

    def _settle(self) -> None:
        try:
            if not self._inside():
                self._paint(False)
        except tk.TclError:
            pass

    def _paint(self, hover: bool) -> None:
        ink = t.TERRACOTTA if self.active else (t.INK if hover else t.INK_SOFT)
        self.label.configure(fg=ink)
        self.line.configure(bg=t.TERRACOTTA if self.active else t.HEADER)

    def set_active(self, active: bool) -> None:
        self.active = bool(active)
        self._paint(False)


class WindowControl(tk.Label):
    """Minimal custom title-bar control."""

    def __init__(self, parent: tk.Misc, text: str, command, *, danger: bool = False) -> None:
        super().__init__(
            parent,
            text=text,
            bg=t.HEADER,
            fg=t.INK_SOFT,
            font=("Segoe UI Variable Text", -t.s(15)),
            width=3,
            cursor="hand2",
        )
        self.command = command
        self.danger = danger
        self.bind("<Button-1>", lambda _event: self.command(), add="+")
        self.bind("<Enter>", self._enter, add="+")
        self.bind("<Leave>", self._leave, add="+")

    def _enter(self, _event=None) -> None:
        self.configure(
            bg=t.DANGER if self.danger else t.HOVER,
            fg=t.ON_ACCENT if self.danger else t.INK,
        )

    def _leave(self, _event=None) -> None:
        self.configure(bg=t.HEADER, fg=t.INK_SOFT)


class VenusChatV1:
    """Standalone V1 application shell and local frontend router."""

    MIN_WIDTH = 1120
    MIN_HEIGHT = 700

    def __init__(self, root: tk.Tk, *, custom_chrome: bool = True) -> None:
        self.root = root
        self.custom_chrome = bool(custom_chrome)
        self.model_name = load_config().get("model") or "deepseek-v4-flash"
        self._toast_job: str | None = None
        self._drag_origin: tuple[int, int, int, int] | None = None
        self._resize_origin: tuple[int, int, int, int] | None = None
        self._maximized = False
        self._restore_geometry = ""
        self._restore_override_pending = False
        self._online = False

        self.client = ApiClient()
        self.bridge = BackendBridge(self.client, self._on_backend_event)

        t.enable_dpi_awareness()
        t.init_scale(root)
        self.fonts = t.Fonts.create(root)
        t.configure_ttk(root, self.fonts)
        self._configure_window()
        self._build_shell()
        self._apply_windows_taskbar_style()
        self.show_chat()
        self.bridge.refresh_all()
        self._poll_backend()
        self.root.after(1600, self._quant_probe)

    def _poll_backend(self) -> None:
        self.bridge.poll()
        self.root.after(80, self._poll_backend)

    def _on_backend_event(self, kind: str, payload) -> None:
        try:
            if kind == "health":
                _, (code, data) = payload
                if code == 200:
                    self._set_online(True, data.get("model") or "")
                    self.model_name = data.get("model") or self.model_name
                    self.bridge._apply_health(code, data)
                    self.chat_view.on_health(data)
                else:
                    self._set_online(False, "")
            elif kind == "sessions":
                _, (code, data) = payload
                self.bridge._apply_sessions(code, data)
                self.chat_view.refresh_sidebar()
            elif kind == "projects":
                _, (code, data) = payload
                self.bridge._apply_projects(code, data)
                self.chat_view.refresh_projects()
            elif kind == "jobs":
                _, (code, data) = payload
                if code == 200:
                    self.bridge.jobs_active = int((data.get("active_count") or 0))
                    self.chat_view.refresh_jobs(data.get("jobs") or [])
            elif kind == "session_new":
                _, (code, data) = payload
                sid = 0
                if code == 200:
                    sid = int((data.get("session") or {}).get("id")
                              or data.get("id") or 0)
                if sid:
                    from .backend_bridge import SessionState
                    self.bridge.sessions[sid] = SessionState(
                        sid=sid, title="新对话", loaded=True, messages=[])
                    self.bridge.current_sid = sid
                    self.chat_view.on_new_session(sid)
            elif kind == "session_load":
                _, (code, data) = payload
                sid = self.bridge.current_sid or 0
                self.bridge._apply_session_load(code, data, sid)
            elif kind == "session_ready":
                self.chat_view.render_session(int(payload))
            elif kind == "dispatch":
                _, (code, data) = payload
                if code == 200 and data.get("job"):
                    jid = data["job"].get("id", "?")
                    self.toast(f"任务已派发 {jid}")
                    self.bridge.submit("jobs", lambda: ("ok", self.client.get("/api/v1/jobs?limit=20")))
                else:
                    self.toast(f"派活失败：{(data or {}).get('detail', code)}")
            elif kind == "stream_event":
                skind, spayload = payload
                self.bridge.handle_stream_event(skind, spayload)
            elif kind == "stream_ask":
                from .confirm_dialog import show_confirm
                show_confirm(self.root, payload, self._on_confirm, self.fonts)
            elif kind == "toast":
                self.toast(str(payload))
            elif kind == "error":
                self.toast(str(payload))
            else:
                self.chat_view.handle_backend(kind, payload)
        except tk.TclError:
            pass

    def _on_confirm(self, allowed: bool, request_id: str) -> None:
        self.bridge.respond_confirm(allowed, request_id)

    def _set_online(self, online: bool, model: str) -> None:
        self._online = online
        if self._online_label is not None:
            self._online_label.configure(
                text=(model[:18] + "…") if online and len(model) > 18 else (model if online else "离线"),
                fg=t.SUCCESS if online else t.WARNING,
            )

    # Window ----------------------------------------------------------------
    def _configure_window(self) -> None:
        self.root.title("VenusChat V1")
        self.root.configure(bg=t.LINE_STRONG)
        self.root.minsize(t.s(self.MIN_WIDTH), t.s(self.MIN_HEIGHT))
        # winfo values are physical pixels under DPI awareness; design in
        # logical units and convert on the way out.
        k = t.scale_factor()
        screen_w = self.root.winfo_screenwidth() / k
        screen_h = self.root.winfo_screenheight() / k
        width = max(self.MIN_WIDTH, min(1500, screen_w - 60))
        height = max(self.MIN_HEIGHT, min(900, screen_h - 60))
        x = max(18, (screen_w - width) // 2)
        y = max(18, (screen_h - height) // 2)
        self.root.geometry(f"{t.s(width)}x{t.s(height)}+{t.s(x)}+{t.s(y)}")
        if self.custom_chrome:
            self.root.overrideredirect(True)
        self.root.option_add("*tearOff", False)
        self.root.bind("<Control-comma>", lambda _event: self.show_settings(), add="+")
        self.root.bind("<Escape>", self._escape, add="+")
        self.root.bind("<F11>", lambda _event: self.toggle_maximize(), add="+")
        self.root.bind("<Map>", self._on_map, add="+")

    def _build_shell(self) -> None:
        self.shell = tk.Frame(
            self.root,
            bg=t.CANVAS,
            highlightthickness=1,
            highlightbackground=t.LINE_STRONG,
        )
        self.shell.pack(fill="both", expand=True)
        self.shell.rowconfigure(1, weight=1)
        self.shell.columnconfigure(0, weight=1)

        self._build_header()
        separator(self.shell, color=t.LINE).grid(row=0, column=0, sticky="sew")

        self.view_host = tk.Frame(self.shell, bg=t.CANVAS)
        self.view_host.grid(row=1, column=0, sticky="nsew")
        self.view_host.rowconfigure(0, weight=1)
        self.view_host.columnconfigure(0, weight=1)

        self.chat_view = ChatView(self.view_host, self, self.fonts, self.bridge)
        self.settings_view = SettingsView(self.view_host, self, self.fonts, self.client)
        self.chat_view.grid(row=0, column=0, sticky="nsew")
        self.settings_view.grid(row=0, column=0, sticky="nsew")

        self._online_dot: Dot | None = None
        self._online_label: tk.Label | None = None

        self.toast_frame = tk.Frame(
            self.shell,
            bg=t.INK,
            highlightthickness=1,
            highlightbackground=t.INK,
        )
        self.toast_label = tk.Label(
            self.toast_frame,
            text="",
            bg=t.INK,
            fg=t.ON_ACCENT,
            font=self.fonts.small,
            padx=t.s(16),
            pady=t.s(8),
        )
        self.toast_label.pack()

        if self.custom_chrome:
            self.resize_grip = tk.Frame(
                self.shell,
                bg=t.LINE_STRONG,
                width=t.s(8),
                height=t.s(8),
                cursor="size_nw_se",
            )
            self.resize_grip.place(relx=1, rely=1, anchor="se")
            self.resize_grip.bind("<ButtonPress-1>", self._start_resize, add="+")
            self.resize_grip.bind("<B1-Motion>", self._resize_window, add="+")

    def _build_header(self) -> None:
        self.header = tk.Frame(self.shell, bg=t.HEADER, height=t.s(60))
        self.header.grid(row=0, column=0, sticky="new")
        self.header.pack_propagate(False)

        brand_zone = tk.Frame(self.header, bg=t.HEADER, width=t.s(310), cursor="fleur")
        brand_zone.pack(side="left", fill="y")
        brand_zone.pack_propagate(False)
        brand_row = tk.Frame(brand_zone, bg=t.HEADER)
        brand_row.pack(side="left", padx=t.s(26))
        tk.Frame(brand_row, bg=t.TERRACOTTA, width=t.s(7), height=t.s(7)).pack(
            side="left", anchor="center", padx=(0, t.s(11)))
        self.brand_label = tk.Label(
            brand_row,
            text="V E N U S",
            bg=t.HEADER,
            fg=t.INK,
            font=self.fonts.brand,
            cursor="fleur",
        )
        self.brand_label.pack(side="left")

        if self.custom_chrome:
            controls = tk.Frame(self.header, bg=t.HEADER)
            controls.pack(side="right", fill="y")
            WindowControl(controls, "—", self.minimize).pack(side="left", fill="y")
            WindowControl(controls, "□", self.toggle_maximize).pack(side="left", fill="y")
            WindowControl(controls, "×", self.root.destroy, danger=True).pack(side="left", fill="y")

        online = tk.Frame(self.header, bg=t.HEADER)
        online.pack(side="right", padx=(t.s(15), t.s(18)), fill="y")
        self._online_dot = Dot(online, color=t.WARNING, size=7, bg=t.HEADER)
        self._online_dot.pack(side="left", pady=t.s(19))
        self._online_label = tk.Label(
            online,
            text="连接中",
            bg=t.HEADER,
            fg=t.INK_SOFT,
            font=self.fonts.small,
        )
        self._online_label.pack(side="left", pady=t.s(19), padx=(t.s(8), 0))

        separator(self.header, vertical=True, color=t.LINE).pack(
            side="right", fill="y", pady=t.s(16))
        self.pro_badge = FlatButton(
            self.header,
            "VENUS Pro",
            lambda: self.toast("VenusChat V1 · Preview"),
            font=self.fonts.caption,
            variant="outline",
            height=30,
            padx=12,
            parent_bg=t.HEADER,
        )
        self.pro_badge.pack(side="right", padx=t.s(16), pady=t.s(14))

        self.header_links: dict[str, HeaderLink] = {}
        # Packing from the right keeps the visual order: 量化中心 / 设置 / 模型.
        for key, label, callback, has_dot in reversed(
            (
                ("quant", "量化中心", self._open_quant_center, True),
                ("settings", "设置", self.show_settings, False),
                ("model", "模型", self._open_model_menu, False),
            )
        ):
            link = HeaderLink(self.header, label, callback, fonts=self.fonts,
                              with_dot=has_dot)
            link.pack(side="right", fill="y")
            self.header_links[key] = link

        if self.custom_chrome:
            for widget in (self.header, brand_zone, self.brand_label):
                widget.bind("<ButtonPress-1>", self._start_drag, add="+")
                widget.bind("<B1-Motion>", self._drag_window, add="+")
                widget.bind("<Double-Button-1>", lambda _event: self.toggle_maximize(), add="+")

    def _apply_windows_taskbar_style(self) -> None:
        if not self.custom_chrome or sys.platform != "win32":
            return
        try:
            self.root.update_idletasks()
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.root.winfo_id())
            style = user32.GetWindowLongW(hwnd, -20)
            style = (style & ~0x00000080) | 0x00040000  # no TOOLWINDOW, add APPWINDOW
            user32.SetWindowLongW(hwnd, -20, style)
            self.root.withdraw()
            self.root.after(12, self.root.deiconify)
        except Exception:
            pass

    # Routing ----------------------------------------------------------------
    def show_chat(self) -> None:
        self.chat_view.tkraise()
        self.set_header_section("chat")

    def show_settings(self) -> None:
        self.settings_view.tkraise()
        self.set_header_section("settings")

    def show_settings_page(self, key: str) -> None:
        """Jump straight into one settings page (workspace card, rail links)."""
        self.settings_view.tkraise()
        try:
            self.settings_view.select_page(key)
        except Exception:
            pass
        self.set_header_section("settings")

    def set_header_section(self, section: str) -> None:
        for key, link in self.header_links.items():
            link.set_active(section == key)

    def set_model(self, model: str) -> None:
        code, data = self.client.post("/api/v1/config", {"model": model})
        if code == 200:
            self.model_name = model
            self.toast(f"当前模型：{model}")
            self.bridge.submit("health",
                               lambda: ("ok", self.client.get("/api/v1/health")))
        else:
            self.toast(f"模型切换失败：{data.get('detail', code)}")

    def _open_model_menu(self, anchor=None) -> None:
        presets = ("deepseek-v4-flash", "deepseek-reasoner", "本地模型")
        names = ([self.model_name] if self.model_name else []) + \
                [m for m in presets if m != self.model_name]
        items = [{"label": n, "desc": "输入框模型芯片可快速识别当前档",
                  "current": n == self.model_name} for n in names]

        def choose(index: int) -> None:
            self.set_model(names[index])
        anchor_widget = anchor if anchor is not None else self.header_links["model"]
        MenuPopup(anchor_widget, items, self.fonts, choose,
                  min_width=230, align_right=anchor is None)

    def _top_view(self):
        try:
            name = self.view_host.tk.call("winfo", "containing", self.view_host.winfo_rootx() + 5,
                                         self.view_host.winfo_rooty() + 5)
            widget = self.root.nametowidget(name) if name else None
            while widget is not None and widget.master is not self.view_host:
                widget = widget.master
            return widget
        except (tk.TclError, KeyError):
            return self.chat_view

    # 量化中心 ---------------------------------------------------------------
    # 注意：后台动作一律经 bridge.submit（队列回主线程 poll），工作线程绝不
    # 直接触碰 Tk 接口。
    def _quant_link(self):
        return self.header_links.get("quant")

    def _quant_probe(self) -> None:
        """Read-only health check of the isolated quant services (loopback)."""
        if QuantServiceController is None:
            self._quant_probe_result(False, unavailable=True)
            return
        try:
            cfg = QuantIntegrationConfig.from_mapping(load_config())
        except Exception:
            self._quant_probe_result(False, misconfig=True)
            return
        if not cfg.enabled:
            self._quant_probe_result(False, disabled=True)
            return
        self.bridge.submit("quant_probe",
                           lambda: bool(QuantServiceController(cfg).probe().ready))
        self.root.after(15000, self._quant_probe)

    def _quant_probe_result(self, ready, *, unavailable=False, misconfig=False,
                            disabled=False) -> None:
        link = self._quant_link()
        if link is None:
            return
        if unavailable:
            link.set_status(t.INK_FAINT, suffix=" · 不可用")
        elif misconfig:
            link.set_status(t.WARNING, suffix=" · 配置错误")
        elif disabled:
            link.set_status(t.INK_FAINT, suffix=" · 已禁用")
        else:
            link.set_status(t.SUCCESS if ready else t.INK_FAINT,
                            suffix=" · 可用" if ready else "")

    def _open_quant_center(self) -> None:
        """Check, start if needed, and open the loopback quant dashboard."""
        if getattr(self, "_quant_busy", False):
            return
        if QuantServiceController is None:
            self.toast("量化控制层不可用（quant_integration 未安装）")
            return
        try:
            cfg = QuantIntegrationConfig.from_mapping(load_config())
        except Exception as exc:
            self.toast(f"量化中心配置错误：{str(exc)[:120]}")
            return
        if not cfg.enabled:
            self.toast("量化中心已禁用，可在设置 → 量化中启用")
            return
        self._quant_busy = True
        controller = QuantServiceController(cfg)

        def progress(stage: str) -> None:
            self.bridge.submit("quant_progress", lambda: stage)

        def job():
            try:
                controller.open_quant_center(progress=progress)
                return ("ok", None)
            except Exception as exc:      # QuantLaunchError 或意外错误
                return ("err", str(getattr(exc, "user_message", None) or exc)[:160])
        self.bridge.submit("quant_open", job)

    def _quant_progress(self, stage) -> None:
        link = self._quant_link()
        labels = {"checking": "检查中…", "starting-backend": "启动后端…",
                  "starting-gui": "启动 GUI…", "opening": "打开中…"}
        if link is not None:
            link.set_status(t.WARNING,
                            suffix=f" · {labels.get(str(stage), '启动中…')}")

    def _quant_open_done(self, result) -> None:
        self._quant_busy = False
        status, detail = result if isinstance(result, tuple) else ("ok", None)
        link = self._quant_link()
        if status == "ok":
            if link is not None:
                link.set_status(t.SUCCESS, suffix=" · 已打开")
            self.toast("量化中心已打开（Paper Trading only）")
        else:
            if link is not None:
                link.set_status(t.DANGER, suffix=" · 启动失败")
            self.toast(f"量化中心：{detail}", duration=4200)

    # Toast ------------------------------------------------------------------
    def toast(self, text: str, *, duration: int = 2200) -> None:
        if self._toast_job:
            try:
                self.root.after_cancel(self._toast_job)
            except tk.TclError:
                pass
        self.toast_label.configure(text=text)
        self.toast_frame.place(relx=.5, y=t.s(66), anchor="n")
        self.toast_frame.lift()
        self._toast_job = self.root.after(duration, self._hide_toast)

    def _hide_toast(self) -> None:
        self._toast_job = None
        try:
            self.toast_frame.place_forget()
        except tk.TclError:
            pass

    # Chrome interaction -----------------------------------------------------
    def _start_drag(self, event: tk.Event) -> None:
        if self._maximized:
            return
        self._drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _drag_window(self, event: tk.Event) -> None:
        if self._drag_origin is None or self._maximized:
            return
        start_x, start_y, window_x, window_y = self._drag_origin
        self.root.geometry(f"+{window_x + event.x_root - start_x}+{window_y + event.y_root - start_y}")

    def _start_resize(self, event: tk.Event) -> None:
        if self._maximized:
            return
        self._resize_origin = (
            event.x_root,
            event.y_root,
            self.root.winfo_width(),
            self.root.winfo_height(),
        )

    def _resize_window(self, event: tk.Event) -> None:
        if self._resize_origin is None or self._maximized:
            return
        start_x, start_y, width, height = self._resize_origin
        new_width = max(t.s(self.MIN_WIDTH), width + event.x_root - start_x)
        new_height = max(t.s(self.MIN_HEIGHT), height + event.y_root - start_y)
        self.root.geometry(f"{new_width}x{new_height}")

    def _work_area(self) -> tuple[int, int, int, int]:
        if sys.platform == "win32":
            try:
                class RECT(ctypes.Structure):
                    _fields_ = [
                        ("left", ctypes.c_long),
                        ("top", ctypes.c_long),
                        ("right", ctypes.c_long),
                        ("bottom", ctypes.c_long),
                    ]

                rect = RECT()
                ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
                return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
            except Exception:
                pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def toggle_maximize(self) -> None:
        if not self.custom_chrome:
            self.root.state("normal" if self.root.state() == "zoomed" else "zoomed")
            return
        if self._maximized:
            self.root.geometry(self._restore_geometry)
            self._maximized = False
            self.resize_grip.place(relx=1, rely=1, anchor="se")
        else:
            self._restore_geometry = self.root.geometry()
            x, y, width, height = self._work_area()
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            self._maximized = True
            self.resize_grip.place_forget()

    def minimize(self) -> None:
        if not self.custom_chrome:
            self.root.iconify()
            return
        self._restore_override_pending = True
        self.root.overrideredirect(False)
        self.root.iconify()

    def _on_map(self, _event=None) -> None:
        if self.custom_chrome and self._restore_override_pending:
            self._restore_override_pending = False
            self.root.after(20, lambda: self.root.overrideredirect(True))

    def _escape(self, _event=None) -> None:
        if self._top_view() is self.settings_view:
            self.show_chat()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the independent VenusChat V1 frontend")
    parser.add_argument(
        "--native-frame",
        action="store_true",
        help="Use the operating-system title bar instead of the V1 custom chrome",
    )
    parser.add_argument(
        "--settings",
        action="store_true",
        help="Open directly on the settings center",
    )
    parser.add_argument("--geometry", default="", help="Optional Tk geometry override")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    t.enable_dpi_awareness()
    root = tk.Tk()
    app = VenusChatV1(root, custom_chrome=not args.native_frame)
    if args.geometry:
        root.geometry(args.geometry)
    if args.settings:
        app.show_settings()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
