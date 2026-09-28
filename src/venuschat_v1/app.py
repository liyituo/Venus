"""VenusChat V1 — independent classical-minimal Windows frontend."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path

from . import theme as t
from .api_client import ApiClient
from .backend_bridge import BackendBridge
from .chat_view import ChatView
from .config_store import (active_project_for_origin, load_config,
                           personal_backend_for_team,
                           project_preference_key, save_active_project_for_origin,
                           save_local_config)
from .settings_view import SettingsView
from .widgets import Dot, FlatButton, MenuPopup, separator

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

    @property
    def client(self) -> ApiClient:
        bridge = self.__dict__.get("bridge")
        return bridge.client if bridge is not None else self.__dict__["_client"]

    @client.setter
    def client(self, value: ApiClient) -> None:
        self.__dict__["_client"] = value
        bridge = self.__dict__.get("bridge")
        if bridge is not None and bridge._default_client is not value:
            bridge.set_client(value)

    def __init__(self, root: tk.Tk, *, custom_chrome: bool = True) -> None:
        self.root = root
        self._backend_switch_lock = threading.RLock()
        self.custom_chrome = bool(custom_chrome)
        self.model_name = load_config().get("model") or "deepseek-v4-flash"
        self._toast_job: str | None = None
        self._drag_origin: tuple[int, int, int, int] | None = None
        self._resize_origin: tuple[int, int, int, int] | None = None
        self._maximized = False
        self._restore_geometry = ""
        self._restore_override_pending = False
        self._online = False
        self._current_view = "chat"
        self._model_change_pending = False
        self._active_project_change_serial = 0
        self._drag_moved = False
        self._initial_session_loaded = False
        self._backend_context_pending = False
        self._backend_session_preferences: dict[str, int | None] = {}
        self._backend_drafts: dict[str, dict[int, str]] = {}
        self._backend_unbound_drafts: dict[str, str] = {}
        active_project_map = load_config().get("active_project_by_hub") or {}
        if not isinstance(active_project_map, dict):
            active_project_map = {}
        self._backend_project_preferences: dict[str, str] = {
            str(k).rstrip("/"): str(v)
            for k, v in active_project_map.items()
        }
        self._pending_backend_switch: tuple[str, str] | None = None

        config = load_config()
        self.client = ApiClient()
        initial_base = self.client.base.rstrip("/")
        self._personal_backend_base = initial_base
        self._team_backend_origin = ""
        self._team_backend_name = "团队 Hub"
        joined = [row for row in (config.get("team_connections") or [])
                  if row.get("status") == "joined" and row.get("origin")]
        local_hub_base = str(config.get("team_hub_local_base") or "").rstrip("/")
        connection = (next((row for row in reversed(joined)
                            if str(row.get("origin") or "").rstrip("/") == initial_base), None)
                      or (joined[-1] if joined else None))
        if connection:
            origin = str(connection.get("origin") or "").rstrip("/")
            if local_hub_base and initial_base == local_hub_base:
                # Local llm_base may address the Hub service on its host;
                # use the enrolled HTTPS origin so its device token is scoped
                # correctly and the switch control starts in team context.
                self.client = ApiClient(base=origin)
                initial_base = origin
            self._team_backend_origin = origin
            self._team_backend_name = str(connection.get("team_name") or "团队 Hub")
            previous = str(connection.get("previous_llm_base") or "").rstrip("/")
            if origin == initial_base:
                self._personal_backend_base = personal_backend_for_team(
                    origin, previous, config)
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
        self.bridge.submit("agents", lambda: self.client.get("/api/v1/agents", timeout=8))
        self._poll_backend()
        self.root.after(30000, self._periodic_health)

    def _periodic_health(self) -> None:
        self.bridge.submit("health", lambda: ("ok", self.client.get("/api/v1/health", timeout=5)))
        self.root.after(30000, self._periodic_health)

    def configure_team_backend(self, origin: str, name: str,
                               personal_base: str = "") -> None:
        """Remember the joined Hub and activate its isolated conversation store."""
        self._team_backend_origin = str(origin or "").rstrip("/")
        self._team_backend_name = str(name or "团队 Hub")
        if personal_base:
            self._personal_backend_base = str(personal_base).rstrip("/")
        self._refresh_backend_switch_control()
        self.switch_backend_context(self._team_backend_origin,
                                    self._team_backend_name)

    def set_active_project(self, project_id: str) -> None:
        """Ask the Hub to set this user's current project, then update locally."""
        project_id = str(project_id or "")
        if project_id:
            selected = next((row for row in self.bridge.projects
                             if str(row.get("id") or "") == project_id), None)
            if not selected:
                self.toast("项目列表已变化，请刷新后重新选择")
                return
            if str(selected.get("status") or "") == "pending_claim":
                self.toast("请先认领项目，再设为当前派活项目")
                return
        self._active_project_change_serial += 1
        serial = self._active_project_change_serial
        self.toast("正在更新 Hub 当前项目…")
        self.bridge.submit(
            "active_project_update",
            lambda: self._post_active_project(project_id, serial))

    def _post_active_project(self, project_id: str, serial: int):
        code, data = self.client.post("/api/v1/projects/active", {
            "project_id": project_id,
        }, timeout=10)
        data = dict(data) if isinstance(data, dict) else {"detail": str(data)}
        data["_requested_active"] = project_id
        data["_request_serial"] = serial
        return ("ok", (code, data))

    def apply_active_project(self, project_id: str) -> None:
        """Apply a Hub-confirmed active project to this VenusChat client."""
        project_id = str(project_id or "")
        origin = self.client.base.rstrip("/")
        self.bridge.active_project_id = project_id
        self._backend_project_preferences[project_preference_key(origin)] = project_id
        save_active_project_for_origin(origin, project_id)
        self.chat_view.refresh_projects()

    def toggle_backend_context(self) -> None:
        current = self.client.base.rstrip("/")
        if self._team_backend_origin and current == self._team_backend_origin:
            target = self._personal_backend_base
            label = "个人对话"
        elif self._team_backend_origin:
            target = self._team_backend_origin
            label = self._team_backend_name
        else:
            self.toast("尚未连接团队 Hub；请先在设置的「团队与成员」中加入团队")
            return
        self.switch_backend_context(target, label)

    def switch_backend_context(self, base: str, label: str) -> None:
        """Switch between the personal server and a team Hub without mixing state."""
        target = str(base or "").rstrip("/")
        current = self.client.base.rstrip("/")
        if not target:
            self._pending_backend_switch = None
            self._refresh_backend_switch_control()
            return
        if target == current:
            # A quick reverse click cancels a queued switch instead of allowing
            # the old destination to win after the background queue drains.
            self._pending_backend_switch = None
            self._refresh_backend_switch_control()
            return
        if self.bridge._streaming or self.chat_view._creating_session:
            self.toast("当前对话操作尚未结束，请完成或停止后再切换空间")
            return
        self._pending_backend_switch = None
        unbound_draft = self.chat_view.prepare_backend_context_switch()
        self._backend_session_preferences[current] = self.bridge.current_sid
        self._backend_drafts[current] = dict(self.chat_view._drafts)
        self._backend_unbound_drafts[current] = unbound_draft
        self._backend_project_preferences[project_preference_key(current)] = self.bridge.active_project_id
        save_active_project_for_origin(current, self.bridge.active_project_id)

        # New work is bound to a fresh client; older queued work retains its
        # captured client and results are dropped by the bridge epoch check.
        with self._backend_switch_lock:
            save_local_config({"llm_base": target})
            self.client = ApiClient(base=target)
        settings = getattr(self, "settings_view", None)
        if settings is not None:
            settings.reset_backend_context_state()
        # Always refetch sessions from the selected origin. Session ids overlap
        # between local Venus and the Hub, so sharing cached SessionState objects
        # here can display one server's messages in the other's conversation.
        self.bridge.sessions = {}
        self.bridge.current_sid = None
        self.bridge.projects = []
        self.bridge.active_project_id = (
            self._backend_project_preferences.get(project_preference_key(target))
            or active_project_for_origin(target))
        self.chat_view.reset_backend_context(
            label, self._backend_drafts.get(target, {}),
            self._backend_unbound_drafts.get(target, ""))
        self._backend_context_pending = True
        self._model_change_pending = False
        self._active_project_change_serial += 1
        self.chat_view._deleting_sid = None
        self.chat_view._mode_menu_pending = False
        self.chat_view._mode_change_pending = False
        self._refresh_backend_switch_control()
        self.bridge.refresh_all()

    def _retry_backend_switch(self) -> None:
        request = self._pending_backend_switch
        if request is None:
            return
        if self.bridge._streaming or self.chat_view._creating_session:
            self._pending_backend_switch = None
            self.toast("当前对话操作尚未结束，空间切换已取消")
            return
        self._pending_backend_switch = None
        self.switch_backend_context(*request)

    def _refresh_backend_switch_control(self) -> None:
        view = getattr(self, "chat_view", None)
        if view is not None:
            view.refresh_backend_switch_button()

    def _poll_backend(self) -> None:
        try:
            self.bridge.poll()
        finally:
            try:
                self.root.after(80, self._poll_backend)
            except tk.TclError:
                pass

    def _on_backend_event(self, kind: str, payload) -> None:
        try:
            if kind == "health":
                code, data = self._api_result(payload)
                if code == 200:
                    configured = bool(data.get("configured"))
                    self._set_online(configured, data.get("model") or ("已连接" if configured else "未配置"))
                    self.model_name = data.get("model") or self.model_name
                    self.bridge._apply_health(code, data)
                    self.chat_view.on_health(data)
                else:
                    self._set_online(False, "")
                    self.chat_view.set_offline()
            elif kind == "sessions":
                code, data = self._api_result(payload)
                self.bridge._apply_sessions(code, data)
                self.chat_view.refresh_sidebar()
                if code == 200 and self._backend_context_pending:
                    self._backend_context_pending = False
                    preferred = self._backend_session_preferences.get(
                        self.client.base.rstrip("/"))
                    if preferred in self.bridge.sessions:
                        self.bridge.current_sid = preferred
                    sid = self.bridge.current_sid
                    if sid is not None:
                        self.bridge.load_session(sid)
                    else:
                        self.chat_view.show_empty_backend_context()
                elif code == 200 and not self._initial_session_loaded:
                    self._initial_session_loaded = True
                    sid = self.bridge.current_sid
                    if sid and not self.bridge.sessions[sid].loaded:
                        self.bridge.load_session(sid)
                elif code != 200:
                    self._backend_context_pending = False
            elif kind == "projects":
                code, data = self._api_result(payload)
                self.bridge._apply_projects(code, data)
                self.chat_view.refresh_projects()
            elif kind == "active_project_update":
                code, data = self._api_result(payload)
                serial = int(data.get("_request_serial") or 0)
                requested = str(data.get("_requested_active") or "")
                if serial != self._active_project_change_serial:
                    return
                active = str(data.get("active") or "")
                if code == 200 and active == requested:
                    self.apply_active_project(requested)
                    if requested:
                        project = next((row for row in self.bridge.projects
                                        if str(row.get("id") or "") == requested), {})
                        name = str(project.get("name") or project.get("title") or "项目")
                        self.toast(f"已设为当前派活项目：{name}")
                    else:
                        self.toast("已清除当前派活项目")
                else:
                    detail = str(data.get("detail") or f"HTTP {code}")
                    self.toast(f"切换当前项目失败：{detail}", duration=5000)
                    self.bridge.refresh_all()
            elif kind == "jobs":
                code, data = self._api_result(payload)
                if code == 200:
                    self.bridge.jobs_active = int((data.get("active_count") or 0))
                    self.chat_view.refresh_jobs(data.get("jobs") or [])
                elif self._backend_context_pending:
                    # Never leave the previous origin's task list visible when
                    # the newly selected backend is offline or rejects access.
                    self.chat_view.refresh_jobs([])
            elif kind == "session_new":
                code, data = self._api_result(payload)
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
                else:
                    self.chat_view.on_session_create_failed(str(data.get("detail") or "创建会话失败"))
            elif kind == "session_load":
                sid, result = payload
                code, data = result
                self.bridge._apply_session_load(code, data, sid)
                if code != 200:
                    self.chat_view.on_session_load_failed(sid)
            elif kind == "session_ready":
                self.chat_view.render_session(int(payload))
            elif kind == "dispatch":
                code, data = self._api_result(payload)
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
            elif kind == "model_change":
                self._model_change_pending = False
                code, data = self._api_result(payload)
                if code == 200:
                    cfg = data.get("config") or {}
                    self.model_name = str(cfg.get("model") or self.model_name)
                    self.toast(f"当前模型：{self.model_name}")
                    self.bridge.submit("health", lambda: ("ok", self.client.get("/api/v1/health")))
                else:
                    self.toast(f"模型切换失败：{data.get('detail', code)}", duration=4200)
            elif kind == "worker_error":
                source, detail = payload
                if str(source).startswith("settings_"):
                    self.settings_view.handle_backend(str(source), ("error", str(detail)))
                elif source == "model_change":
                    self._model_change_pending = False
                    self.toast(f"模型切换失败：{detail}", duration=4200)
                elif source == "session_new":
                    self.chat_view.on_session_create_failed(str(detail))
                elif source == "session_load":
                    self.chat_view.on_session_load_failed(self.bridge.current_sid or 0)
                    self.toast(f"加载会话失败：{detail}", duration=4200)
                elif source == "session_delete":
                    self.chat_view._deleting_sid = None
                    self.toast(f"删除会话失败：{detail}", duration=4200)
                elif source in {"mode_list", "mode_change"}:
                    if source == "mode_list":
                        self.chat_view.handle_backend(source, (0, {"detail": str(detail)}))
                    else:
                        self.chat_view.handle_backend(source, ("", (0, {"detail": str(detail)})))
                else:
                    self.toast(f"操作失败：{detail}", duration=4200)
            elif kind.startswith("settings_"):
                self.settings_view.handle_backend(kind, payload)
            else:
                self.chat_view.handle_backend(kind, payload)
        except tk.TclError:
            pass

    @staticmethod
    def _api_result(payload) -> tuple[int, dict]:
        """Normalize successful bridge results and worker failures."""
        if isinstance(payload, tuple) and len(payload) == 2 and payload[0] == "ok":
            result = payload[1]
            if isinstance(result, tuple) and len(result) == 2:
                code, data = result
                return int(code), data if isinstance(data, dict) else {"detail": str(data)}
        detail = payload[1] if isinstance(payload, tuple) and len(payload) == 2 else payload
        return 0, {"detail": str(detail)}

    def _on_confirm(self, allowed: bool, request_id: str) -> None:
        self.bridge.respond_confirm(allowed, request_id)

    def _set_online(self, online: bool, model: str) -> None:
        self._online = online
        if self._online_dot is not None:
            self._online_dot.set_color(t.SUCCESS if online else t.WARNING)
        if self._online_label is not None:
            self._online_label.configure(
                text=(model[:18] + "…") if online and len(model) > 18 else (model or "离线"),
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
        self._online_dot: Dot | None = None
        self._online_label: tk.Label | None = None
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

        brand_zone = tk.Frame(self.header, bg=t.HEADER, width=t.s(260), cursor="fleur")
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
        # Packing from the right keeps the visual order: 设置 / 模型.
        for key, label, callback, has_dot in reversed(
            (
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
            for widget in (brand_zone, self.brand_label):
                widget.bind("<ButtonRelease-1>", self._release_brand, add="+")

    def _ensure_taskbar_style(self) -> None:
        """Keep the taskbar/Alt-Tab entry (APPWINDOW, no TOOLWINDOW).

        Toggling overrideredirect recreates window styles and wipes the bits
        set at startup; without re-applying, the window vanishes from the
        taskbar after the first minimize-restore cycle and cannot be restored.
        """
        if not self.custom_chrome or sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.root.winfo_id())
            style = user32.GetWindowLongW(hwnd, -20)
            user32.SetWindowLongW(hwnd, -20, (style & ~0x00000080) | 0x00040000)
        except Exception:
            pass

    def _apply_windows_taskbar_style(self) -> None:
        if not self.custom_chrome or sys.platform != "win32":
            return
        try:
            self.root.update_idletasks()
            self._ensure_taskbar_style()
            self.root.withdraw()
            self.root.after(80, self._show_startup_window)
        except Exception:
            pass

    def _show_startup_window(self) -> None:
        """Make a freshly launched custom-chrome window visible and active."""
        try:
            if not self.root.winfo_exists():
                return
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    # Routing ----------------------------------------------------------------
    def show_chat(self) -> None:
        self.chat_view.tkraise()
        self._current_view = "chat"
        self.set_header_section("chat")

    def show_settings(self) -> None:
        self.settings_view.on_open()
        self.settings_view.tkraise()
        self._current_view = "settings"
        self.set_header_section("settings")

    def show_settings_page(self, key: str) -> None:
        """Jump straight into one settings page (workspace card, rail links)."""
        if key not in self.settings_view.page_keys:
            return
        self.settings_view.tkraise()
        self._current_view = "settings"
        self.settings_view.select_page(key)
        self.set_header_section("settings")

    def set_header_section(self, section: str) -> None:
        for key, link in self.header_links.items():
            link.set_active(section == key)

    def set_model(self, model: str) -> None:
        if self._model_change_pending:
            self.toast("正在切换模型，请稍候")
            return
        if not model or model == self.model_name:
            return
        self._model_change_pending = True
        self.toast("正在切换模型…")
        self.bridge.submit("model_change", lambda: ("ok", self.client.post("/api/v1/config", {"model": model})))

    def _open_model_menu(self, anchor=None) -> None:
        presets = ("deepseek-v4-flash", "deepseek-reasoner")
        names = ([self.model_name] if self.model_name else []) + \
                [m for m in presets if m != self.model_name]
        items = [{"label": n, "desc": "当前使用" if n == self.model_name else "切换模型",
                  "current": n == self.model_name} for n in names]
        items.append({"label": "配置其他模型…", "desc": "填写模型标识与连接信息"})

        def choose(index: int) -> None:
            if index == len(names):
                self.show_settings_page("model")
            else:
                self.set_model(names[index])
        anchor_widget = anchor if anchor is not None else self.header_links["model"]
        MenuPopup(anchor_widget, items, self.fonts, choose,
                  min_width=230, align_right=anchor is None)

    def _top_view(self):
        return self.settings_view if self._current_view == "settings" else self.chat_view

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
        self._drag_moved = False
        self._drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _drag_window(self, event: tk.Event) -> None:
        if self._drag_origin is None or self._maximized:
            return
        start_x, start_y, window_x, window_y = self._drag_origin
        if abs(event.x_root - start_x) + abs(event.y_root - start_y) > t.s(5):
            self._drag_moved = True
        self.root.geometry(f"+{window_x + event.x_root - start_x}+{window_y + event.y_root - start_y}")

    def _release_brand(self, _event=None) -> None:
        if not self._drag_moved:
            self.show_chat()
        self._drag_origin = None

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
            self.root.after(20, self._restore_custom_chrome)

    def _restore_custom_chrome(self) -> None:
        try:
            self.root.overrideredirect(True)
        except tk.TclError:
            return
        self._ensure_taskbar_style()
        try:
            self.root.lift()
        except tk.TclError:
            pass

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


def _claim_gui_instance():
    """Keep one GUI per checkout, including launches outside the batch file."""
    if sys.platform != "win32":
        return None, None, False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    suffix = hashlib.sha256(str(Path(__file__).resolve().parents[2]).casefold().encode()).hexdigest()[:16]
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, f"Local\\VenusChat-{suffix}")
    if not handle:
        return kernel32, None, False
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return kernel32, None, True
    return kernel32, handle, False


def _set_window_icon(root: tk.Tk) -> None:
    assets = Path(__file__).resolve().parents[2] / "assets"
    png = assets / "venuschat-icon.png"
    ico = assets / "venuschat.ico"
    try:
        if png.exists():
            root._venus_icon_photo = tk.PhotoImage(file=str(png))
            root.iconphoto(True, root._venus_icon_photo)
        if sys.platform == "win32" and ico.exists():
            root.iconbitmap(default=str(ico))
    except tk.TclError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    kernel32, mutex, duplicate = _claim_gui_instance()
    if duplicate:
        return 0
    marker = Path(__file__).resolve().parents[2] / ".venus" / "venuschat-gui.json"
    try:
        t.enable_dpi_awareness()
        root = tk.Tk()
        _set_window_icon(root)
        marker.parent.mkdir(parents=True, exist_ok=True)
        metadata = {"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}
        temporary = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(metadata), encoding="utf-8")
        temporary.replace(marker)
        app = VenusChatV1(root, custom_chrome=not args.native_frame)
        if args.geometry:
            root.geometry(args.geometry)
        if args.settings:
            app.show_settings()
        root.mainloop()
        return 0
    finally:
        try:
            if json.loads(marker.read_text(encoding="utf-8")).get("pid") == os.getpid():
                marker.unlink()
        except (OSError, ValueError, TypeError):
            pass
        if kernel32 is not None and mutex:
            kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
