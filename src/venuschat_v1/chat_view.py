"""Main workspace for the independent VenusChat V1 frontend.

Driven by ``backend_bridge.BackendBridge`` (sessions + SSE agent stream):
tool-call cards live inside the reply bubble, an execution dock shows live
todos and background jobs (with per-job SSE-style progress from /api/v1/jobs),
and the composer offers the sub-agent picker fed by /api/v1/agents.
"""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import font as tkfont
from typing import Callable

from . import theme as t
from .api_client import ApiClient                      # noqa: F401  (contract)
from .backend_bridge import BackendBridge, SessionState
from .widgets import (
    HAS_PIL,
    Dot,
    FlatButton,
    HoverSurface,
    MenuPopup,
    MessageDialog,
    PeopleBadge,
    ProgressBar,
    RoundButton,
    ScrollArea,
    SearchField,
    TodoIcon,
    ToolCard,
    kicker,
    separator,
)

if HAS_PIL:
    from PIL import Image, ImageDraw, ImageTk

PROJECT_STATUS_CN = {
    "active": "进行中", "planning": "规划中", "paused": "已暂停",
    "completed": "已完成", "blocked": "受阻",
}
PROJECT_STATUS_COLOR = {
    "active": t.TERRACOTTA, "planning": t.WARNING, "paused": t.INK_MUTED,
    "completed": t.SUCCESS, "blocked": t.DANGER,
}
JOB_STATUS_CN = {
    "queued": "排队中", "running": "执行中", "waiting_confirm": "等待确认",
    "completed": "已完成", "failed": "失败", "cancelled": "已取消",
}
JOB_STATUS_COLOR = {
    "queued": t.INK_MUTED, "running": t.TERRACOTTA, "waiting_confirm": t.WARNING,
    "completed": t.SUCCESS, "failed": t.DANGER, "cancelled": t.INK_FAINT,
}


def fit_text(font: tkfont.Font, text: str, max_px: int) -> str:
    """Ellipsize by measured width so sidebar rows never hard-clip."""
    text = str(text or "")
    if font.measure(text) <= max_px:
        return text
    tail = "…"
    while text and font.measure(text + tail) > max_px:
        text = text[:-1]
    return text + tail


class SidebarItem(tk.Frame):
    """Compact project / conversation / job row used by the workspace rails."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        font: tkfont.Font,
        command: Callable[[], object],
        meta: str = "",
        meta_color: str = t.INK_MUTED,
        show_dot: bool = False,
        height: int = 42,
        bg: str = t.SIDEBAR,
        on_delete: Callable[[], object] | None = None,
    ) -> None:
        super().__init__(parent, bg=bg, height=t.s(height), cursor="hand2")
        self.pack_propagate(False)
        self.command = command
        self.active = False
        self.base_bg = bg
        self.marker = tk.Frame(self, bg=bg, width=2)
        self.marker.pack(side="left", fill="y")
        self.title_label = tk.Label(
            self, text=title, bg=bg, fg=t.INK_SOFT, font=font,
            anchor="w", cursor="hand2")
        self.title_label.pack(side="left", fill="x", expand=True,
                              padx=(t.s(12), t.s(4)))
        self.delete_label = None
        if on_delete is not None:
            self.delete_label = tk.Label(
                self, text="✕", bg=bg, fg=bg, font=font, width=2, cursor="hand2")
            self.delete_label.pack(side="right")
            self.delete_label.bind(
                "<Button-1>", lambda _e: on_delete(), add="+")
        if show_dot:
            self.dot = Dot(self, color=meta_color, size=6, bg=bg)
            self.dot.pack(side="right", padx=(t.s(4), t.s(6)))
        else:
            self.dot = None
        self.meta_label = tk.Label(
            self, text=meta, bg=bg, fg=meta_color, font=font, cursor="hand2")
        self.meta_label.pack(side="right", padx=(t.s(7), t.s(5)))
        widgets = [self, self.marker, self.title_label, self.meta_label]
        if self.dot is not None:
            widgets.append(self.dot)
        for widget in widgets:
            widget.bind("<Button-1>", lambda _event: self.command(), add="+")
            widget.bind("<Enter>", lambda _event: self._paint(True), add="+")
            widget.bind("<Leave>", lambda _event: self.after(18, self._settle), add="+")
        if self.delete_label is not None:
            # ✕ 只触发删除；hover/leave 仍归行管
            self.delete_label.bind("<Enter>", lambda _event: self._paint(True), add="+")
            self.delete_label.bind("<Leave>", lambda _event: self.after(18, self._settle), add="+")

    def _contains_pointer(self) -> bool:
        try:
            x, y = self.winfo_pointerx(), self.winfo_pointery()
            left, top = self.winfo_rootx(), self.winfo_rooty()
            return left <= x <= left + self.winfo_width() and top <= y <= top + self.winfo_height()
        except tk.TclError:
            return False

    def _settle(self) -> None:
        try:
            if not self._contains_pointer():
                self._paint(False)
        except tk.TclError:
            pass

    def _paint(self, hover: bool) -> None:
        try:
            bg = t.ACTIVE if self.active else (t.HOVER if hover else self.base_bg)
            for widget in (self, self.title_label, self.meta_label):
                widget.configure(bg=bg)
            if self.dot is not None:
                self.dot.configure(bg=bg)
            if self.delete_label is not None:
                self.delete_label.configure(
                    bg=bg, fg=t.DANGER if hover else bg)
            self.marker.configure(bg=t.TERRACOTTA if self.active else bg)
            self.title_label.configure(fg=t.INK if self.active else t.INK_SOFT)
        except tk.TclError:
            pass

    def set_active(self, active: bool) -> None:
        self.active = bool(active)
        self._paint(False)


class ChatView(tk.Frame):
    """Workspace bound to the local Venus backend through the bridge."""

    def __init__(self, parent: tk.Misc, app, fonts: t.Fonts,
                 bridge: BackendBridge) -> None:
        super().__init__(parent, bg=t.CANVAS)
        self.app = app
        self.fonts = fonts
        self.bridge = bridge

        self._turn: dict | None = None
        self.streaming = False
        self.agents: list[dict] = []
        self.agent_name = "通用智能体"
        self.search_open = False
        self.message_count = 0

        self.panel_pinned = False
        self._todos: list[dict] = []
        self._jobs_live: list[str] = []
        self._live_rows: dict[str, dict] = {}
        self._jobs_active = False
        self._jobs_refresh_job: str | None = None
        self._pending = ""
        self._deleting_sid: int | None = None

        self._build()
        bridge.submit("agents", lambda: ("ok", bridge.client.get("/api/v1/agents")))

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        self.columnconfigure(0, weight=0, minsize=t.s(310))
        self.columnconfigure(1, weight=1)
        self.columnconfigure(2, weight=0)
        self.rowconfigure(0, weight=1)

        self.sidebar = tk.Frame(self, bg=t.SIDEBAR, width=t.s(310))
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.pack_propagate(False)
        separator(self, vertical=True, color=t.LINE).grid(row=0, column=0, sticky="nse")
        self._build_sidebar()

        self.workspace = tk.Frame(self, bg=t.CANVAS)
        self.workspace.grid(row=0, column=1, sticky="nsew")
        self.workspace.columnconfigure(0, weight=1)
        self.workspace.rowconfigure(1, weight=1)
        self._build_toolbar()
        self._build_stage()
        self._build_composer()

        self._build_job_panel()

    def _build_toolbar(self) -> None:
        bar = tk.Frame(self.workspace, bg=t.CANVAS, height=t.s(38))
        bar.grid(row=0, column=0, sticky="ew")
        bar.pack_propagate(False)
        self.toolbar_title = tk.Label(
            bar, text="新对话", bg=t.CANVAS, fg=t.INK_SOFT,
            font=self.fonts.small_bold)
        self.toolbar_title.pack(side="left", padx=(t.s(34), 0), pady=t.s(8))
        right = tk.Frame(bar, bg=t.CANVAS)
        right.pack(side="right", padx=t.s(24))
        self.stop_button = FlatButton(
            right, "停止", self._stop, font=self.fonts.small, variant="ghost",
            height=28, min_width=58, parent_bg=t.CANVAS)
        self.stop_button.pack(side="right", padx=(t.s(6), 0))
        self.stop_button.set_enabled(False)
        self.panel_button = FlatButton(
            right, "任务面板", self._toggle_panel, font=self.fonts.small,
            variant="ghost", height=28, min_width=78, parent_bg=t.CANVAS)
        self.panel_button.pack(side="right")
        separator(self.workspace, color=t.LINE_FAINT).grid(row=0, column=0,
                                                           sticky="sew")

    def _build_sidebar(self) -> None:
        footer = tk.Frame(self.sidebar, bg=t.SIDEBAR, height=t.s(112))
        footer.pack(side="bottom", fill="x")
        footer.pack_propagate(False)
        separator(footer, color=t.LINE_FAINT).pack(fill="x")
        footer_title = tk.Frame(footer, bg=t.SIDEBAR)
        footer_title.pack(fill="x", padx=t.s(20), pady=(t.s(11), t.s(6)))
        tk.Label(footer_title, text="系统状态", bg=t.SIDEBAR, fg=t.INK_SOFT,
                 font=self.fonts.small_bold).pack(side="left")
        online_row = tk.Frame(footer_title, bg=t.SIDEBAR)
        online_row.pack(side="right")
        self._foot_dot = Dot(online_row, color=t.INK_FAINT, size=7, bg=t.SIDEBAR)
        self._foot_dot.pack(side="left", padx=(0, t.s(6)))
        self._foot_text = tk.Label(online_row, text="检测中…", bg=t.SIDEBAR,
                                   fg=t.INK_MUTED, font=self.fonts.caption)
        self._foot_text.pack(side="left")
        self._svc_line = self._status_line(footer, "模型服务")
        self._mem_line = self._status_line(footer, "记忆系统")

        # 固定区：标题、空间卡、新建、长期项目 —— 不参与滚动
        header_zone = tk.Frame(self.sidebar, bg=t.SIDEBAR)
        header_zone.pack(side="top", fill="x")

        tk.Label(header_zone, text="工作空间", bg=t.SIDEBAR, fg=t.INK,
                 font=self.fonts.display_md).pack(
            anchor="w", padx=t.s(20), pady=(t.s(18), t.s(11)))

        workspace_card = HoverSurface(
            header_zone, bg=t.SURFACE_ALT, resting_line=t.LINE,
            hover_line=t.LINE_STRONG, active_line=t.TERRACOTTA, height=t.s(74))
        workspace_card.pack(fill="x", padx=t.s(18))
        workspace_card.pack_propagate(False)
        tk.Frame(workspace_card, bg=t.TERRACOTTA, width=2).pack(side="left", fill="y")
        people = PeopleBadge(workspace_card, size=42, bg=t.SURFACE_ALT)
        people.pack(side="left", padx=(t.s(11), t.s(8)))
        copy = tk.Frame(workspace_card, bg=t.SURFACE_ALT, cursor="hand2")
        copy.pack(side="left", fill="both", expand=True, pady=t.s(10))
        title_row = tk.Frame(copy, bg=t.SURFACE_ALT, cursor="hand2")
        title_row.pack(fill="x")
        tk.Label(title_row, text="默认工作空间", bg=t.SURFACE_ALT, fg=t.INK,
                 font=self.fonts.small_bold, cursor="hand2").pack(side="left")
        tk.Label(title_row, text="本地", bg=t.TERRACOTTA_SOFT, fg=t.TERRACOTTA,
                 font=self.fonts.caption, padx=t.s(5), cursor="hand2").pack(
            side="left", padx=t.s(8))
        self._ws_path_label = tk.Label(copy, text="连接后显示", bg=t.SURFACE_ALT,
                                       fg=t.INK_MUTED, font=self.fonts.caption,
                                       cursor="hand2")
        self._ws_path_label.pack(anchor="w", pady=(t.s(4), 0))
        chevron = tk.Label(workspace_card, text="›", bg=t.SURFACE_ALT, fg=t.INK_SOFT,
                           font=self.fonts.display_md, cursor="hand2")
        chevron.pack(side="right", padx=t.s(12))
        for widget in (workspace_card, people, copy, title_row,
                       self._ws_path_label, chevron):
            widget.bind("<Button-1>",
                        lambda _event: self.app.show_settings_page("common"), add="+")
            workspace_card.watch(widget)

        new_chat = FlatButton(
            header_zone, "＋  新建对话", self.new_chat, font=self.fonts.body,
            variant="outline", height=42, radius=10, parent_bg=t.SIDEBAR)
        new_chat.pack(fill="x", padx=t.s(18), pady=(t.s(13), t.s(18)))

        self._section_heading(header_zone, "长期项目")
        self.project_box = tk.Frame(header_zone, bg=t.SIDEBAR)
        self.project_box.pack(fill="x", padx=t.s(10), pady=(t.s(3), t.s(10)))
        self.refresh_projects()

        # 最近对话：区头与搜索固定，仅列表自身滚动
        recent_zone = tk.Frame(self.sidebar, bg=t.SIDEBAR)
        recent_zone.pack(side="top", fill="both", expand=True)
        recent_head = tk.Frame(recent_zone, bg=t.SIDEBAR)
        recent_head.pack(fill="x", padx=t.s(20), pady=(t.s(7), t.s(5)))
        kicker(recent_head, "最近对话", font=self.fonts.kicker, bg=t.SIDEBAR,
               fg=t.INK_FAINT).pack(side="left")
        search_button = FlatButton(
            recent_head, "搜索", self._toggle_search, font=self.fonts.caption,
            variant="ghost", height=26, min_width=40, padx=9, parent_bg=t.SIDEBAR)
        search_button.pack(side="right")
        self._recent_zone = recent_zone
        self.search_field = SearchField(recent_zone, font=self.fonts.small,
                                        placeholder="搜索对话…")
        self.search_field.variable.trace_add("write", lambda *_a: self.refresh_sidebar())
        self.conversation_scroll = ScrollArea(recent_zone, bg=t.SIDEBAR,
                                              scrollbar=False)
        self.conversation_scroll.pack(fill="both", expand=True)
        self.conversation_box = tk.Frame(self.conversation_scroll.inner,
                                         bg=t.SIDEBAR)
        self.conversation_box.pack(fill="x", padx=t.s(10), pady=(0, t.s(14)))
        self.refresh_sidebar()

    def _status_line(self, parent: tk.Misc, title: str) -> tk.Label:
        row = tk.Frame(parent, bg=t.SIDEBAR)
        row.pack(fill="x", padx=t.s(20), pady=t.s(2))
        left = tk.Frame(row, bg=t.SIDEBAR)
        left.pack(side="left")
        Dot(left, color=t.INK_FAINT, size=6, bg=t.SIDEBAR).pack(side="left",
                                                                padx=(0, t.s(7)))
        tk.Label(left, text=title, bg=t.SIDEBAR, fg=t.INK_MUTED,
                 font=self.fonts.caption).pack(side="left")
        value_label = tk.Label(row, text="—", bg=t.SIDEBAR, fg=t.INK_FAINT,
                               font=self.fonts.caption)
        value_label.pack(side="right")
        return value_label

    def _section_heading(self, parent: tk.Misc, text: str) -> None:
        kicker(parent, text, font=self.fonts.kicker, bg=t.SIDEBAR,
               fg=t.INK_FAINT).pack(anchor="w", padx=t.s(20), pady=(0, t.s(5)))

    def _build_stage(self) -> None:
        self.stage = tk.Frame(self.workspace, bg=t.CANVAS)
        self.stage.grid(row=1, column=0, sticky="nsew")
        self.stage.rowconfigure(0, weight=1)
        self.stage.columnconfigure(0, weight=1)

        self.empty = tk.Canvas(self.stage, bg=t.CANVAS, bd=0, highlightthickness=0)
        self.empty.grid(row=0, column=0, sticky="nsew")
        self.empty.bind("<Configure>", self._draw_empty, add="+")
        self._empty_photo: "ImageTk.PhotoImage | None" = None
        self._empty_size = None

        # The greeting is typeset with real labels (ClearType) instead of
        # canvas text, which Tk renders without any anti-aliasing.
        card = tk.Frame(self.empty, bg=t.CANVAS)
        mark = tk.Frame(card, bg=t.TERRACOTTA, width=t.s(14), height=2)
        mark.pack(pady=(0, t.s(16)))
        tk.Label(card, text="VENUS 智能工作台", bg=t.CANVAS, fg=t.INK,
                 font=self.fonts.display_xl).pack()
        tk.Label(card, text="探索、思考与创造", bg=t.CANVAS, fg=t.INK_SOFT,
                 font=self.fonts.display_md).pack(pady=(t.s(7), 0))
        tk.Label(card, text="普通消息直接执行；以 ! 开头派发给后台任务队列",
                 bg=t.CANVAS, fg=t.INK_FAINT, font=self.fonts.caption).pack(
            pady=(t.s(18), 0))
        self.empty_card = card
        self.empty_card.place(relx=.5, rely=.47, anchor="center")

        self.messages = ScrollArea(self.stage, bg=t.CANVAS, scrollbar=True)
        self.messages.grid(row=0, column=0, sticky="nsew")
        self.message_column = tk.Frame(self.messages.inner, bg=t.CANVAS)
        self.message_column.pack(fill="x", expand=True,
                                 padx=t.s(32), pady=(t.s(14), t.s(20)))
        self.empty.tk.call("raise", self.empty._w)

    def _build_composer(self) -> None:
        self.composer = HoverSurface(
            self.workspace, bg=t.SURFACE, resting_line=t.LINE,
            hover_line=t.LINE_STRONG, active_line=t.TERRACOTTA)
        self.composer.grid(row=2, column=0, sticky="ew",
                           padx=t.s(28), pady=(t.s(6), t.s(18)))
        self.composer.columnconfigure(0, weight=1)

        meta = tk.Frame(self.composer, bg=t.SURFACE)
        meta.grid(row=0, column=0, sticky="ew", padx=t.s(12), pady=(t.s(8), 0))
        self.model_chip = FlatButton(
            meta, "模型 ▾", lambda: self.app._open_model_menu(anchor=self.model_chip),
            font=self.fonts.small_bold, variant="ghost", height=30,
            parent_bg=t.SURFACE)
        self.model_chip.pack(side="left")
        self.agent_chip = FlatButton(
            meta, "通用智能体 ▾", self._show_agent_menu,
            font=self.fonts.small, variant="ghost", height=30, parent_bg=t.SURFACE)
        self.agent_chip.pack(side="left", padx=t.s(5))
        self.mode_chip = FlatButton(
            meta, "确认模式 ▾", self._show_mode_menu,
            font=self.fonts.small, variant="ghost", height=30, parent_bg=t.SURFACE)
        self.mode_chip.pack(side="left", padx=t.s(5))

        editor = HoverSurface(
            self.composer, bg=t.SURFACE, resting_line=t.LINE_FAINT,
            hover_line=t.LINE, active_line=t.TERRACOTTA)
        editor.grid(row=1, column=0, sticky="ew", padx=t.s(10), pady=t.s(3))
        editor.columnconfigure(0, weight=1)
        self.input_box = tk.Text(
            editor, bg=t.SURFACE, fg=t.INK, insertbackground=t.INK,
            selectbackground=t.TERRACOTTA_SOFT, selectforeground=t.INK,
            font=self.fonts.body, relief="flat", bd=0, highlightthickness=0,
            height=2, wrap="word", undo=True, padx=t.s(12), pady=t.s(9))
        self.input_box.grid(row=0, column=0, sticky="ew")
        self.placeholder = tk.Label(
            editor, text="在这里输入你的问题、任务或想法…",
            bg=t.SURFACE, fg=t.INK_FAINT, font=self.fonts.body, cursor="xterm")
        self.placeholder.place(x=t.s(13), y=t.s(10))
        self.placeholder.bind("<Button-1>",
                              lambda _event: self.input_box.focus_set(), add="+")
        self.input_box.bind("<FocusIn>",
                            lambda _event: editor.set_active(True), add="+")
        self.input_box.bind("<FocusOut>",
                            lambda _event: editor.set_active(False), add="+")
        self.input_box.bind("<KeyRelease>", self._update_placeholder, add="+")
        self.input_box.bind("<Return>", self._on_return, add="+")
        self.composer.watch(meta, editor, self.input_box, self.placeholder)

        actions = tk.Frame(self.composer, bg=t.SURFACE)
        actions.grid(row=2, column=0, sticky="ew", padx=t.s(11),
                     pady=(t.s(5), t.s(10)))
        RoundButton(actions, "→", self.send_message, font=self.fonts.display_md,
                    size=48, parent_bg=t.SURFACE).pack(side="right")
        tk.Label(actions, text="Enter 发送     Shift + Enter 换行", bg=t.SURFACE,
                 fg=t.INK_FAINT, font=self.fonts.caption).pack(
            side="right", padx=(t.s(10), t.s(18)))

    # ------------------------------------------------------------- job panel
    def _build_job_panel(self) -> None:
        self.panel = tk.Frame(self, bg=t.SURFACE_ALT, width=t.s(302))
        self.panel.grid(row=0, column=2, sticky="nsew")
        self.panel.grid_propagate(False)
        tk.Frame(self.panel, bg=t.LINE, width=1).pack(side="left", fill="y")
        inner = tk.Frame(self.panel, bg=t.SURFACE_ALT)
        inner.pack(side="left", fill="both", expand=True)

        head = tk.Frame(inner, bg=t.SURFACE_ALT)
        head.pack(fill="x", padx=t.s(18), pady=(t.s(14), t.s(10)))
        kicker(head, "执行面板", font=self.fonts.kicker, bg=t.SURFACE_ALT,
               fg=t.INK_SOFT).pack(side="left")
        FlatButton(head, "收起", self._toggle_panel, font=self.fonts.caption,
                   variant="ghost", height=24, padx=8, parent_bg=t.SURFACE_ALT
                   ).pack(side="right")

        scroll = ScrollArea(inner, bg=t.SURFACE_ALT, scrollbar=False)
        scroll.pack(fill="both", expand=True)
        page = scroll.inner

        def section(title: str) -> tk.Frame:
            tk.Label(page, text=title, bg=t.SURFACE_ALT, fg=t.INK_FAINT,
                     font=self.fonts.caption).pack(anchor="w", padx=t.s(18),
                                                   pady=(t.s(6), t.s(5)))
            box = tk.Frame(page, bg=t.SURFACE_ALT)
            box.pack(fill="x", padx=t.s(12))
            return box

        self.todo_box = section("任务清单")
        self._render_todo_empty()
        tk.Frame(page, bg=t.LINE_FAINT, height=1).pack(fill="x",
                                                       padx=t.s(18), pady=t.s(12))
        self.jobs_box = section("后台任务")
        self._render_jobs_empty()
        tk.Frame(page, bg=t.LINE_FAINT, height=1).pack(fill="x",
                                                       padx=t.s(18), pady=t.s(12))
        self.job_box = section("本轮工具")
        self._render_job_empty()

        self.panel.grid_remove()

    def _render_todo_empty(self) -> None:
        tk.Label(self.todo_box, text="模型创建待办任务后，进度会在这里实时更新。",
                 bg=t.SURFACE_ALT, fg=t.INK_FAINT, font=self.fonts.caption,
                 justify="left", anchor="w", wraplength=t.s(250)).pack(
            anchor="w", pady=t.s(4))

    def _render_job_empty(self) -> None:
        tk.Label(self.job_box, text="每一次工具调用将在这里排队展示。",
                 bg=t.SURFACE_ALT, fg=t.INK_FAINT, font=self.fonts.caption,
                 justify="left", anchor="w", wraplength=t.s(250)).pack(
            anchor="w", pady=t.s(4))

    def _render_jobs_empty(self) -> None:
        tk.Label(self.jobs_box, text="用「! 任务」把长任务派发到异步队列。",
                 bg=t.SURFACE_ALT, fg=t.INK_FAINT, font=self.fonts.caption,
                 justify="left", anchor="w", wraplength=t.s(250)).pack(
            anchor="w", pady=t.s(4))

    def _toggle_panel(self) -> None:
        self.panel_pinned = not self.panel_pinned
        if self.panel_pinned:
            self.panel.grid()
        else:
            self.panel.grid_remove()

    def _auto_open_panel(self) -> None:
        if not self.panel_pinned:
            self.panel_pinned = True
            self.panel.grid()

    # ------------------------------------------------------------- todo view
    def render_todos(self, todos) -> None:
        todos = todos if isinstance(todos, list) else (todos or {}).get("todos", [])
        self._todos = todos or []
        for child in self.todo_box.winfo_children():
            child.destroy()
        if not self._todos:
            self._render_todo_empty()
            return
        self._auto_open_panel()
        done = sum(1 for x in self._todos if str(x.get("status")) == "done")
        header = tk.Frame(self.todo_box, bg=t.SURFACE_ALT)
        header.pack(fill="x", pady=(0, t.s(4)))
        tk.Label(header, text=f"{done} / {len(self._todos)}", bg=t.SURFACE_ALT,
                 fg=t.TERRACOTTA, font=self.fonts.kicker).pack(side="right")
        over = None
        if done:
            over = tkfont.Font(font=self.fonts.caption)
            over.configure(overstrike=1)
        for item in self._todos:
            status = str(item.get("status") or "pending")
            row = tk.Frame(self.todo_box, bg=t.SURFACE_ALT)
            row.pack(fill="x", pady=t.s(2))
            TodoIcon(row, status=status, bg=t.SURFACE_ALT).pack(
                side="left", padx=(t.s(4), t.s(8)))
            title = str(item.get("title") or "")
            if len(title) > 36:
                title = title[:36] + "…"
            is_done = status == "done"
            tk.Label(row, text=title, bg=t.SURFACE_ALT,
                     fg=t.INK_FAINT if is_done else t.INK_SOFT,
                     font=over if is_done else self.fonts.caption,
                     anchor="w", justify="left",
                     wraplength=t.s(218)).pack(side="left", fill="x", expand=True)

    # ------------------------------------------------- live tool rows (本轮)
    def _add_live_job(self, call_id: str, name: str, step: int,
                      max_steps: int) -> None:
        self._auto_open_panel()
        for child in list(self.job_box.winfo_children()):
            if isinstance(child, tk.Label):
                child.destroy()
        row = tk.Frame(self.job_box, bg=t.SURFACE_ALT)
        row.pack(fill="x", pady=t.s(3))
        dot = Dot(row, color=t.TERRACOTTA, size=6, bg=t.SURFACE_ALT)
        dot.pack(side="left", padx=(t.s(4), t.s(7)))
        textcol = tk.Frame(row, bg=t.SURFACE_ALT)
        textcol.pack(side="left", fill="x", expand=True)
        top = tk.Frame(textcol, bg=t.SURFACE_ALT)
        top.pack(fill="x")
        label = f"{name}  ·  {step}/{max_steps}" if step and max_steps else name
        tk.Label(top, text=label, bg=t.SURFACE_ALT, fg=t.INK_SOFT,
                 font=self.fonts.mono).pack(side="left")
        status = tk.Label(top, text="运行中", bg=t.SURFACE_ALT, fg=t.TERRACOTTA,
                          font=self.fonts.kicker)
        status.pack(side="right")
        bar = ProgressBar(textcol, width=226, height=4, bg=t.SURFACE_ALT,
                          trough=t.LINE_FAINT)
        bar.pack(anchor="w", pady=(t.s(3), 0))
        bar.start_indeterminate()
        self._live_rows[call_id] = {"dot": dot, "status": status, "bar": bar}
        self._jobs_live.append(call_id)
        while len(self._jobs_live) > 16:
            oldest = self._jobs_live.pop(0)
            self._live_rows.pop(oldest, None)

    def _finish_live_job(self, call_id: str, ok: bool) -> None:
        entry = self._live_rows.get(call_id)
        if not entry:
            return
        try:
            entry["bar"].set_value(1.0)
            entry["dot"].set_color(t.SUCCESS if ok else t.DANGER)
            entry["status"].configure(text="✓ 完成" if ok else "✗ 失败",
                                      fg=t.SUCCESS if ok else t.DANGER)
        except tk.TclError:
            self._live_rows.pop(call_id, None)

    def _reset_live_jobs(self) -> None:
        for child in self.job_box.winfo_children():
            child.destroy()
        self._live_rows.clear()
        self._jobs_live.clear()
        self._render_job_empty()

    # ------------------------------------------------- server jobs (后台任务)
    def refresh_jobs(self, jobs: list[dict]) -> None:
        for child in self.jobs_box.winfo_children():
            child.destroy()
        jobs = jobs or []
        if not jobs:
            self._render_jobs_empty()
            self._jobs_active = False
            return
        active = False
        for job in jobs[:12]:
            status = str(job.get("status") or "queued")
            if status in ("queued", "running", "waiting_confirm"):
                active = True
            prog = job.get("progress") or {}
            meta_txt = JOB_STATUS_CN.get(status, status)
            tools = int(prog.get("tool_calls") or 0)
            if tools:
                meta_txt = f"{tools} 次工具 · {meta_txt}"
            row = tk.Frame(self.jobs_box, bg=t.SURFACE_ALT)
            row.pack(fill="x", pady=t.s(4))
            dot = Dot(row, color=JOB_STATUS_COLOR.get(status, t.INK_MUTED),
                      size=6, bg=t.SURFACE_ALT)
            dot.pack(side="left", padx=(t.s(4), t.s(8)))
            col = tk.Frame(row, bg=t.SURFACE_ALT)
            col.pack(side="left", fill="x", expand=True)
            title = str(job.get("title") or "任务")
            if len(title) > 30:
                title = title[:30] + "…"
            tk.Label(col, text=title, bg=t.SURFACE_ALT, fg=t.INK_SOFT,
                     font=self.fonts.caption, anchor="w").pack(anchor="w")
            bar = ProgressBar(col, width=230, height=4, bg=t.SURFACE_ALT,
                              trough=t.LINE_FAINT,
                              fill=t.SUCCESS if status == "completed" else t.TERRACOTTA)
            bar.pack(anchor="w", pady=(t.s(3), 0))
            if active and status in ("queued", "running"):
                bar.start_indeterminate()
            else:
                bar.set_value(1.0 if status == "completed" else .35)
            right = tk.Frame(row, bg=t.SURFACE_ALT)
            right.pack(side="right")
            tk.Label(right, text=meta_txt, bg=t.SURFACE_ALT,
                     fg=JOB_STATUS_COLOR.get(status, t.INK_MUTED),
                     font=self.fonts.kicker).pack(anchor="e")
            if status in ("queued", "running", "waiting_confirm"):
                FlatButton(
                    right, "取消",
                    lambda jid=job.get("id"): self._cancel_job(jid),
                    font=self.fonts.caption, variant="ghost", height=22,
                    padx=6, parent_bg=t.SURFACE_ALT).pack(anchor="e",
                                                          pady=(t.s(2), 0))
        self._jobs_active = active
        if active and self._jobs_refresh_job is None:
            self._jobs_refresh_job = self.after(
                5000, self._request_jobs_refresh)

    def _request_jobs_refresh(self) -> None:
        self._jobs_refresh_job = None
        self.bridge.submit("jobs",
                           lambda: ("ok", self.bridge.client.get("/api/v1/jobs?limit=20")))

    def _cancel_job(self, job_id) -> None:
        if not job_id:
            return
        self.bridge.submit("job_cancel", lambda: self.bridge.client.post(
            f"/api/v1/jobs/{job_id}/cancel"))

    # ------------------------------------------------------- app event hooks
    def on_health(self, data: dict) -> None:
        try:
            self._foot_dot.set_color(t.SUCCESS)
            self._foot_text.configure(text="在线", fg=t.SUCCESS)
            self._svc_line.configure(text=str(data.get("model") or "—"),
                                     fg=t.SUCCESS)
            mem = data.get("memory_stats") or {}
            self._mem_line.configure(
                text=f"{mem.get('l1_memories', 0)} 条记忆" if mem.get("enabled")
                else "未启用", fg=t.INK_SOFT)
            ws = str(data.get("workspace") or "")
            self._ws_path_label.configure(
                text=ws.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or "—")
            if data.get("model"):
                self.model_chip.set_text(f"{data['model']} ▾")
        except tk.TclError:
            pass

    def set_offline(self) -> None:
        try:
            self._foot_dot.set_color(t.INK_FAINT)
            self._foot_text.configure(text="离线", fg=t.INK_MUTED)
            self._svc_line.configure(text="未连接", fg=t.INK_FAINT)
            self._mem_line.configure(text="—", fg=t.INK_FAINT)
        except tk.TclError:
            pass

    def refresh_sidebar(self) -> None:
        for child in self.conversation_box.winfo_children():
            child.destroy()
        sessions = sorted(self.bridge.sessions.values(),
                          key=lambda s: s.sid, reverse=True)
        query = (self.search_field.get().strip().casefold()
                 if hasattr(self, "search_field") else "")
        rows = [s for s in sessions
                if not query or query in (s.title or "").casefold()]
        if not rows:
            tk.Label(self.conversation_box, text="还没有对话，点上方「新建对话」。",
                     bg=t.SIDEBAR, fg=t.INK_FAINT, font=self.fonts.caption).pack(
                anchor="w", padx=t.s(12), pady=t.s(3))
            return
        for state in rows[:24]:
            meta = state.updated or (f"{state.message_count} 条"
                                     if state.message_count else "")
            row = SidebarItem(
                self.conversation_box,
                title=fit_text(self.fonts.small, state.title or f"会话 {state.sid}",
                               t.s(216)),
                font=self.fonts.small,
                command=lambda sid=state.sid: self.bridge.load_session(sid),
                meta=meta,
                on_delete=lambda st=state: self._confirm_delete_session(st))
            row.pack(fill="x", pady=1)
            row.set_active(state.sid == self.bridge.current_sid)

    def refresh_projects(self) -> None:
        for child in self.project_box.winfo_children():
            child.destroy()
        if not self.bridge.projects:
            tk.Label(self.project_box, text="暂无长期项目", bg=t.SIDEBAR,
                     fg=t.INK_FAINT, font=self.fonts.caption).pack(
                anchor="w", padx=t.s(12), pady=t.s(3))
            return
        for item in self.bridge.projects[-6:]:
            status = str(item.get("status") or "planning")
            row = SidebarItem(
                self.project_box,
                title=fit_text(self.fonts.small, str(item.get("title") or "项目"),
                               t.s(216)),
                font=self.fonts.small,
                command=lambda pid=item.get("id"): self._select_project(pid),
                meta=PROJECT_STATUS_CN.get(status, status),
                meta_color=PROJECT_STATUS_COLOR.get(status, t.INK_MUTED),
                show_dot=True)
            row.pack(fill="x", pady=1)
            row.set_active(str(item.get("id")) == self.bridge.active_project_id)

    def _select_project(self, project_id) -> None:
        if not project_id:
            return
        self.bridge.submit("project_set", lambda: self.bridge.client.post(
            "/api/v1/projects/active", {"project_id": str(project_id)}))

    def _confirm_delete_session(self, state) -> None:
        if self.bridge._streaming:
            self.app.toast("Venus 正在执行，请先停止", duration=2400)
            return

        def result(ok: bool) -> None:
            if not ok:
                return
            self._deleting_sid = state.sid
            self.bridge.delete_session(state.sid)
        title = state.title or f"会话 {state.sid}"
        if len(title) > 26:
            title = title[:26] + "…"
        MessageDialog(self.app.root, self.fonts,
                      title="删除对话",
                      message=f"确定删除「{title}」？该会话的全部消息将从本地存储中移除，操作不可恢复。",
                      confirm_text="删除", danger=True, on_choice=result)

    def on_new_session(self, sid: int) -> None:
        self._clear_thread()
        self._reset_live_jobs()
        self.toolbar_title.configure(text="新对话")
        self._show_empty()
        self.refresh_sidebar()
        self.input_box.focus_set()
        if self._pending:
            self._after_session_created_hook()

    def render_session(self, sid: int) -> None:
        state = self.bridge.sessions.get(sid)
        if state is None:
            return
        self.toolbar_title.configure(
            text=fit_text(self.fonts.small_bold, state.title or "新对话", t.s(430)))
        self._clear_thread()
        self._reset_live_jobs()
        if not state.messages:
            self._show_empty()
            self.refresh_sidebar()
            return
        self._show_messages()
        for m in state.messages:
            role = str(m.get("role") or "")
            if role in ("user", "assistant"):
                self._append_bubble(role, str(m.get("content") or ""))
        self.refresh_sidebar()
        self.input_box.focus_set()

    # -------------------------------------------------------------- chat flow
    def send_message(self) -> None:
        text = self.input_box.get("1.0", "end-1c").strip()
        if not text:
            self.app.toast("请先输入一条消息")
            self.input_box.focus_set()
            return
        if self.bridge._streaming:
            self.app.toast("Venus 正在执行，可点上方「停止」", duration=2600)
            return
        dispatch = text.startswith("!") or text.lower().startswith("/dispatch ")
        if self.bridge.current_sid is None:
            self.app.toast("正在创建会话…")
            self._pending = text
            self.bridge.create_session()
            return
        state = self.bridge.sessions.get(self.bridge.current_sid)
        if state is not None and not state.loaded:
            self.app.toast("会话加载中…")
            return
        self.input_box.delete("1.0", "end")
        self._update_placeholder()
        if not dispatch:
            self._show_messages()
            self._append_bubble("user", text)
            self.message_count += 1
        self.bridge.send_message(text, self._make_turn)

    def _make_turn(self, _initial: str = "") -> dict:
        """message_cb: create the live agent turn; handle routed back as stream_start."""
        self._show_messages()
        row = tk.Frame(self.message_column, bg=t.CANVAS)
        row.pack(fill="x", pady=t.s(7))
        stack = tk.Frame(row, bg=t.CANVAS)
        stack.pack(anchor="w", fill="x")
        bubble = tk.Frame(row, bg=t.AGENT_MESSAGE, highlightthickness=1,
                          highlightbackground=t.LINE_STRONG, bd=0)
        bubble.pack(anchor="w", fill="x", pady=(t.s(4), 0))
        head = tk.Frame(bubble, bg=t.AGENT_MESSAGE)
        head.pack(fill="x", padx=t.s(16), pady=(t.s(10), t.s(4)))
        dot = Dot(head, color=t.TERRACOTTA, size=6, bg=t.AGENT_MESSAGE)
        dot.pack(side="left", padx=(0, t.s(6)))
        state = tk.Label(head, text="思考中", bg=t.AGENT_MESSAGE, fg=t.INK_MUTED,
                         font=self.fonts.kicker)
        state.pack(side="left")
        body = tk.Label(bubble, text="", bg=t.AGENT_MESSAGE, fg=t.INK,
                        font=self.fonts.body, justify="left", anchor="w",
                        wraplength=t.s(820))
        body.pack(fill="x", padx=t.s(16), pady=(0, t.s(12)))
        turn = {"row": row, "stack": stack, "body": body, "dot": dot,
                "state": state, "cards": {}}
        self._turn = turn
        self._scroll_end()
        return turn

    def _stop(self) -> None:
        self.bridge.cancel_stream()
        if self._turn is not None:
            self._end_turn("已停止")
        self._set_streaming(False)

    def _set_streaming(self, on: bool) -> None:
        self.streaming = on
        try:
            self.stop_button.set_enabled(on)
        except tk.TclError:
            pass

    # ---------------------------------------------------- bridge ui delivery
    def handle_backend(self, kind: str, payload) -> None:
        try:
            if kind == "stream_start":
                if isinstance(payload, dict):
                    self._turn = payload
                self._set_streaming(True)
            elif kind == "stream_delta":
                turn = self._turn or self._make_turn()
                turn["state"].configure(text="输出中", fg=t.TERRACOTTA)
                turn["body"].configure(text=str(payload) + " ▍")
                self._scroll_end()
            elif kind == "stream_tool_call":
                self._on_tool_call(payload)
            elif kind == "stream_tool_result":
                self._on_tool_result(payload)
            elif kind == "stream_todo_update":
                data = payload if isinstance(payload, dict) else {}
                self.render_todos(data.get("todos", []) if isinstance(data.get("todos"), list) else [])
            elif kind == "stream_done":
                self._end_turn("完成")
                self._set_streaming(False)
                self.bridge.submit("sessions",
                                   lambda: ("ok", self.bridge.client.get("/api/v1/sessions")))
                self.bridge.submit("health",
                                   lambda: ("ok", self.bridge.client.get("/api/v1/health")))
            elif kind == "stream_error":
                if self._turn is not None:
                    self._end_turn("出错")
                self._set_streaming(False)
                self.app.toast(str(payload)[:150], duration=4200)
            elif kind == "agents":
                code, data = payload if isinstance(payload, tuple) else (200, {})
                if code == 200:
                    self.agents = list(data.get("agents") or [])
            elif kind == "job_cancel":
                code, data = payload if isinstance(payload, tuple) else (200, {})
                if code == 200:
                    self.app.toast("任务已取消")
                    self._request_jobs_refresh()
                else:
                    self.app.toast(f"取消失败：{(data or {}).get('detail', code)}")
            elif kind == "project_set":
                code, _data = payload if isinstance(payload, tuple) else (200, {})
                self.app.toast("已切换活跃项目" if code == 200 else "切换项目失败")
                self.bridge.submit("projects",
                                   lambda: ("ok", self.bridge.client.get("/api/v1/projects")))
            elif kind == "session_delete":
                code = payload[0] if isinstance(payload, tuple) else 200
                if code == 200:
                    self.app.toast("对话已删除")
                    if self._deleting_sid is not None \
                            and self._deleting_sid == self.bridge.current_sid:
                        self._clear_thread()
                        self._reset_live_jobs()
                        self.toolbar_title.configure(text="新对话")
                        self._show_empty()
                    elif self._deleting_sid is not None \
                            and self._deleting_sid not in self.bridge.sessions:
                        pass
                    self._deleting_sid = None
                    self.bridge.submit(
                        "sessions",
                        lambda: ("ok", self.bridge.client.get("/api/v1/sessions")))
                else:
                    detail = (payload[1] or {}).get("detail", code) \
                        if isinstance(payload, tuple) else code
                    self._deleting_sid = None
                    self.app.toast(f"删除失败：{detail}")
            elif kind == "sess_append":
                code = payload[0] if isinstance(payload, tuple) else 200
                if code not in (200, 0):
                    self.app.toast("会话持久化失败（内容仍保留在本次上下文中）")
            elif kind == "confirm":
                code = payload[0] if isinstance(payload, tuple) else 200
                if code not in (200,):
                    self.app.toast("确认回传失败：后端将按拒绝处理", duration=4200)
            elif kind == "stream_ask":
                pass  # shell 层弹出确认对话框
        except tk.TclError:
            pass

    def _on_tool_call(self, d) -> None:
        if not isinstance(d, dict):
            return
        turn = self._turn or self._make_turn()
        turn["state"].configure(text="调用工具", fg=t.TERRACOTTA)
        args_text = str(d.get("arguments") or "")
        if len(args_text) > 80:
            args_text = args_text[:80] + "…"
        card = ToolCard(turn["stack"], name=str(d.get("name") or "tool"),
                        args_text=args_text, step=int(d.get("step") or 0),
                        max_steps=int(d.get("max_steps") or 0),
                        fonts=self.fonts)
        card.pack(fill="x", pady=t.s(3))
        cid = str(d.get("id") or "")
        if cid:
            turn["cards"][cid] = card
        self._add_live_job(cid, str(d.get("name") or "tool"),
                           int(d.get("step") or 0), int(d.get("max_steps") or 0))
        self._scroll_end()

    def _on_tool_result(self, d) -> None:
        if not isinstance(d, dict) or self._turn is None:
            return
        cid = str(d.get("id") or "")
        card = self._turn["cards"].get(cid)
        if card:
            card.finish(bool(d.get("ok")), str(d.get("result") or ""))
        self._finish_live_job(cid, bool(d.get("ok")))

    def _end_turn(self, status: str) -> None:
        turn = self._turn
        self._turn = None
        if turn is None:
            return
        try:
            if not str(turn["body"].cget("text")).strip():
                turn["body"].configure(text=f"（{status}· 本轮没有文本回复）")
                turn["body"].configure(fg=t.INK_FAINT)
            else:
                turn["body"].configure(text=str(turn["body"].cget("text")).rstrip(" ▍"))
            turn["dot"].set_color(t.INK_FAINT)
            turn["state"].configure(text=time.strftime(f"{status} %H:%M"),
                                    fg=t.INK_FAINT)
        except tk.TclError:
            pass

    def _after_session_created_hook(self) -> None:
        pending = getattr(self, "_pending", "")
        self._pending = ""
        if pending:
            self.send_pending(pending)

    def send_pending(self, text: str) -> None:
        if self.bridge.current_sid is None:
            return
        dispatch = text.startswith("!") or text.lower().startswith("/dispatch ")
        if not dispatch:
            self._show_messages()
            self._append_bubble("user", text)
            self.message_count += 1
        self.bridge.send_message(text, self._make_turn)

    # ------------------------------------------------------------- rendering
    def _show_messages(self) -> None:
        self.messages.tkraise()

    def _show_empty(self) -> None:
        self.empty.tk.call("raise", self.empty._w)

    def _clear_thread(self) -> None:
        for child in self.message_column.winfo_children():
            child.destroy()
        self.message_count = 0
        self._turn = None

    def _append_bubble(self, role: str, text: str) -> tk.Label:
        user = role == "user"
        row = tk.Frame(self.message_column, bg=t.CANVAS)
        row.pack(fill="x", pady=t.s(7))
        bubble_bg = t.USER_MESSAGE if user else t.AGENT_MESSAGE
        bubble = tk.Frame(row, bg=bubble_bg, highlightthickness=1,
                          highlightbackground=t.LINE_FAINT, bd=0)
        bubble.pack(side="right" if user else "left",
                    padx=(t.s(80), 0) if user else (0, t.s(56)))
        head = tk.Frame(bubble, bg=bubble_bg)
        head.pack(fill="x", padx=t.s(16), pady=(t.s(10), t.s(4)))
        if not user:
            Dot(head, color=t.TERRACOTTA, size=6, bg=bubble_bg).pack(
                side="left", padx=(0, t.s(6)))
        tk.Label(head, text="你" if user else "VENUS", bg=bubble_bg,
                 fg=t.TERRACOTTA if user else t.INK_MUTED,
                 font=self.fonts.caption).pack(side="left")
        body = tk.Label(bubble, text=text, bg=bubble_bg, fg=t.INK,
                        font=self.fonts.body, justify="left", anchor="w",
                        wraplength=t.s(820))
        body.pack(fill="x", padx=t.s(16), pady=(0, t.s(12)))
        self._scroll_end()
        return body

    def _scroll_end(self) -> None:
        self.update_idletasks()
        try:
            self.messages.canvas.configure(
                scrollregion=self.messages.canvas.bbox("all"))
            self.messages.canvas.yview_moveto(1)
        except tk.TclError:
            pass

    # --------------------------------------------------------------- composer
    def _update_placeholder(self, _event=None) -> None:
        if self.input_box.get("1.0", "end-1c").strip():
            self.placeholder.place_forget()
        else:
            self.placeholder.place(x=t.s(13), y=t.s(10))

    def _on_return(self, event: tk.Event) -> str | None:
        if event.state & 0x0001:
            return None
        self.send_message()
        return "break"

    def new_chat(self) -> None:
        if self.bridge._streaming:
            self.app.toast("Venus 正在执行，请先停止", duration=2400)
            return
        self.bridge.create_session()

    def open_conversation(self, index: int) -> None:
        """Compat entry for preview / debug harnesses."""
        sids = sorted(self.bridge.sessions, reverse=True)
        if sids:
            self.bridge.load_session(sids[index % len(sids)])

    def _toggle_search(self) -> None:
        self.search_open = not self.search_open
        if self.search_open:
            self.search_field.pack(fill="x", padx=t.s(18), pady=(0, t.s(8)),
                                   before=self.conversation_scroll)
            self.search_field.entry.focus_set()
        else:
            self.search_field.pack_forget()
            self.search_field.set("")

    # ------------------------------------------------------------ agent modes
    def _show_agent_menu(self) -> None:
        items = [{"label": "通用智能体", "desc": "默认 · 全部本地工具",
                  "current": self.agent_name == "通用智能体"}]
        if not self.agents:
            items.append({"label": "暂无子 Agent", "desc": "后端未连接或 agents/ 目录为空",
                          "disabled": True})
        for a in self.agents:
            name = str(a.get("name") or "?")
            desc = str(a.get("description") or "")[:46]
            if a.get("model"):
                desc = f"{desc} · {a['model']}" if desc else str(a["model"])
            items.append({"label": name, "desc": desc,
                          "current": self.agent_name == name})

        def choose(index: int) -> None:
            if index == 0:
                self._choose_agent("通用智能体")
            else:
                picked = items[index]["label"]
                self._choose_agent(picked)
        MenuPopup(self.agent_chip, items, self.fonts, choose, min_width=230)

    def _choose_agent(self, name: str) -> None:
        self.agent_name = name
        self.agent_chip.set_text(f"{name} ▾")
        self.app.toast(f"委派倾向：{name}（是否委派仍由模型判断）")

    def _show_mode_menu(self) -> None:
        code, data = self.bridge.client.get("/api/v1/confirm-mode")
        if code != 200:
            self.app.toast("读取确认模式失败")
            return
        current = str(data.get("mode") or "auto")
        desc = data.get("descriptions") or {}
        cn = {"auto": "自动确认", "strict": "严格确认", "trusted": "信任模式",
              "query": "只读模式", "plan": "计划审批"}
        modes = list(data.get("modes") or [])
        items = [{"label": cn.get(str(m), str(m)), "desc": str(desc.get(m) or ""),
                  "current": str(m) == current} for m in modes]

        def choose(index: int) -> None:
            self._set_mode(modes[index])
        MenuPopup(self.mode_chip, items, self.fonts, choose, min_width=300)

    def _set_mode(self, mode: str) -> None:
        code, data = self.bridge.client.post("/api/v1/confirm-mode",
                                             {"mode": mode})
        if code == 200:
            cn = {"auto": "自动", "strict": "严格", "trusted": "信任",
                  "query": "只读", "plan": "计划"}.get(mode, mode)
            self.mode_chip.set_text(f"{cn}模式 ▾")
            self.app.toast(f"确认模式：{cn}")
        else:
            self.app.toast(f"切换失败：{(data or {}).get('detail', code)}")

    # ------------------------------------------------------------ empty view
    def _draw_empty(self, _event=None) -> None:
        canvas = self.empty
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        if self._empty_size == (width, height):
            return
        self._empty_size = (width, height)
        canvas.delete("decor")
        line = "#F0EBE5"
        profile = [
            (0, 0), (10, 18), (18, 40), (21, 63), (18, 86),
            (29, 108), (39, 125), (36, 145), (25, 152), (31, 163),
            (27, 175), (17, 181), (20, 193), (12, 208), (4, 217),
            (1, 244), (-10, 273), (-25, 300), (-43, 326),
        ]
        scale = max(.82, min(1.28, height / 670))
        origin_x, origin_y = width * .83, height * .12
        if HAS_PIL:
            ss = 2
            img = Image.new("RGBA", (width * ss, height * ss), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.arc((int(width * .63 * ss), int(-height * .38 * ss),
                   int(width * 1.02 * ss), int(height * .61 * ss)),
                  start=197, end=327, fill=line, width=ss)
            pts = [(int((origin_x + x * scale) * ss),
                    int((origin_y + y * scale) * ss)) for x, y in profile]
            d.line(pts, fill=line, width=ss, joint="curve")
            self._empty_photo = ImageTk.PhotoImage(
                img.resize((width, height), Image.LANCZOS))
            canvas.create_image(0, 0, image=self._empty_photo, anchor="nw",
                                tags="decor")
        else:
            canvas.create_arc(width * .63, -height * .38, width * 1.02,
                              height * .61, start=197, extent=130,
                              style="arc", outline=line, width=1, tags="decor")
            coords: list[float] = []
            for x, y in profile:
                coords.extend((origin_x + x * scale, origin_y + y * scale))
            canvas.create_line(*coords, fill=line, width=1, smooth=True,
                               splinesteps=24, tags="decor")
        self.empty_card.lift()
