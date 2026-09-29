"""Main workspace for the independent VenusChat V1 frontend.

Driven by ``backend_bridge.BackendBridge`` (sessions + SSE agent stream):
tool-call cards live inside the reply bubble, an execution dock shows live
todos and background jobs (with per-job SSE-style progress from /api/v1/jobs),
and the composer offers the sub-agent picker fed by /api/v1/agents.
"""

from __future__ import annotations

import math
import re
import time
import tkinter as tk
from tkinter import font as tkfont
from typing import Callable

from . import theme as t
from .api_client import ApiClient                      # noqa: F401  (contract)
from .backend_bridge import BackendBridge
from .config_store import team_connection
from .workspace_state import personal_jobs
from .widgets import (
    HAS_PIL,
    Dot,
    FlatButton,
    HoverSurface,
    MenuPopup,
    MessageDialog,
    ProgressBar,
    RoundButton,
    ScrollArea,
    SearchField,
    TodoIcon,
    ToolCard,
    kicker,
    rounded_rect,
    separator,
)
from .team_collab_view import open_job_change_submit

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


class MessageBody(tk.Text):
    """Selectable chat text with lightweight Markdown styling."""

    _INLINE = re.compile(
        r"(`[^`]+`|~~.+?~~|(?<!\w)__.+?__(?!\w)|\*\*.+?\*\*|"
        r"(?<!\w)_[^_]+_(?!\w)|\*[^*]+\*)"
    )
    _HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
    _LIST = re.compile(r"^\s*((?:[-*+])|(?:\d+[.)]))\s+(.*)$")
    _QUOTE = re.compile(r"^\s*>\s?(.*)$")

    def __init__(
        self,
        parent: tk.Misc,
        *,
        fonts: t.Fonts,
        bg: str,
        text: str = "",
        compact: bool = True,
        max_width: int = 680,
        on_layout: Callable[[], None] | None = None,
    ) -> None:
        self.fonts = fonts
        self.compact = compact
        self.raw_text = ""
        self._widest_line_px = 0
        self._rendered_lines = 1
        self.max_width = max_width
        self.on_layout = on_layout
        self._render_job: str | None = None
        self._measure_job: str | None = None
        self._columns = 0
        self._last_display_width = 0
        self._streaming = False
        self._stream_rendered = ""
        self._stream_caret = False
        super().__init__(
            parent,
            bg=bg,
            fg=t.INK,
            insertbackground=t.INK,
            selectbackground=t.TERRACOTTA_SOFT,
            selectforeground=t.INK,
            font=fonts.body,
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=0,
            pady=0,
            width=80,
            height=1,
            wrap="word",
            cursor="xterm",
            exportselection=False,
            takefocus=True,
        )
        for level, font in enumerate(
            (fonts.heading1, fonts.heading2, fonts.heading3), 1
        ):
            self.tag_configure(
                f"heading{level}", font=font, foreground=t.INK,
                spacing1=t.s(7 if level == 1 else 5),
                spacing3=t.s(3),
            )
        self.tag_configure("strong", font=fonts.body_bold)
        self.tag_configure("emphasis", font=fonts.body_italic)
        self.tag_configure("strike", overstrike=1, foreground=t.INK_MUTED)
        self.tag_configure("inline_code", font=fonts.mono,
                           foreground=t.INK, background=t.CODE_SURFACE)
        self.tag_configure("code", font=fonts.mono, foreground=t.INK_SOFT,
                           background=t.CODE_SURFACE, lmargin1=t.s(10),
                           lmargin2=t.s(10), rmargin=t.s(10))
        self.tag_configure("list", lmargin1=t.s(18), lmargin2=t.s(18))
        self.tag_configure("bullet", foreground=t.TERRACOTTA,
                           font=fonts.body_bold)
        self.tag_configure("quote", foreground=t.INK_MUTED,
                           lmargin1=t.s(12), lmargin2=t.s(12))
        self.tag_configure("quote_mark", foreground=t.TERRACOTTA,
                           font=fonts.body_bold)
        self.tag_configure("code_language", font=fonts.kicker,
                           foreground=t.TERRACOTTA, background=t.CODE_SURFACE)
        self.tag_configure("paragraph", spacing3=t.s(4))
        self.tag_configure("caret", foreground=t.TERRACOTTA)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Control-a>", self._select_all, add="+")
        self.bind("<Control-c>", self._copy_selection, add="+")
        self.set_wrap_width(max_width)
        self.set_content(text)

    def _select_all(self, _event=None):
        try:
            self.tag_add("sel", "1.0", "end-1c")
        except tk.TclError:
            pass
        return "break"

    def _copy_selection(self, _event=None):
        try:
            selection = self.tag_ranges("sel")
            if selection:
                self.clipboard_clear()
                self.clipboard_append(self.get(selection[0], selection[1]))
                return "break"
        except tk.TclError:
            return "break"
        return None

    def _on_destroy(self, event) -> None:
        if event.widget is self:
            for job in (self._render_job, self._measure_job):
                if job:
                    try:
                        self.after_cancel(job)
                    except tk.TclError:
                        pass
            self._render_job = None
            self._measure_job = None

    def _on_configure(self, event: tk.Event) -> None:
        if event.width != self._last_display_width:
            self._last_display_width = event.width
            self._schedule_measure()

    def _schedule_measure(self) -> None:
        if self._measure_job is None:
            try:
                self._measure_job = self.after_idle(self._measure_height)
            except tk.TclError:
                self._measure_job = None

    def _measure_height(self) -> None:
        self._measure_job = None
        try:
            lines = int(self.tk.call(
                self._w, "count", "-displaylines", "1.0", "end"))
            # Heading fonts and paragraph spacing need room beyond one body line.
            heading_count = (0 if self._streaming else sum(
                1 for line in self.raw_text.splitlines()
                if self._HEADING.match(line)
            ))
            text_height = max(1, lines, self._rendered_lines) + heading_count
            if int(self.cget("height")) != text_height:
                self.configure(height=text_height)
                if self.on_layout is not None:
                    self.on_layout()
        except (tk.TclError, TypeError, ValueError):
            pass

    def set_wrap_width(self, max_width: int) -> None:
        self.max_width = max(1, int(max_width))
        average = max(1, self.fonts.body.measure("0"))
        desired = self.max_width
        if self.compact and self.raw_text:
            desired = max(t.s(72), min(self.max_width, self._widest_line_px))
        columns = max(8, min(120, math.ceil(desired / average)))
        if columns == self._columns:
            return
        self._columns = columns
        try:
            self.configure(width=columns)
            self._schedule_measure()
        except tk.TclError:
            pass

    def _insert_inline(self, value: str, base_tags: tuple[str, ...] = ()) -> None:
        cursor = 0
        for match in self._INLINE.finditer(value):
            if match.start() > cursor:
                self._insert_tagged(value[cursor:match.start()], base_tags)
            token = match.group(0)
            if token.startswith(("**", "__")):
                self._insert_tagged(token[2:-2], base_tags + ("strong",))
            elif token.startswith("~~"):
                self._insert_tagged(token[2:-2], base_tags + ("strike",))
            elif token.startswith("`"):
                self._insert_tagged(token[1:-1], base_tags + ("inline_code",))
            else:
                self._insert_tagged(token[1:-1], base_tags + ("emphasis",))
            cursor = match.end()
        if cursor < len(value):
            self._insert_tagged(value[cursor:], base_tags)

    def _insert_tagged(self, value: str, tags: tuple[str, ...] = ()) -> None:
        if tags:
            self.insert("end", value, tags)
        else:
            self.insert("end", value)

    def set_content(self, value: str, *, streaming: bool = False) -> None:
        text = str(value or "")
        if streaming and self._streaming and text == self.raw_text:
            return
        self.raw_text = text
        if self.compact:
            self._widest_line_px = max(
                (self.fonts.body.measure(line) for line in self.raw_text.splitlines()),
                default=0,
            )
        self._streaming = bool(streaming)
        if streaming:
            if self._render_job is None:
                try:
                    delay = (70 if len(self.raw_text) < 4_000 else
                             120 if len(self.raw_text) < 20_000 else 200)
                    self._render_job = self.after(delay, self._flush_stream)
                except tk.TclError:
                    self._render_job = None
            return
        if self._render_job is not None:
            try:
                self.after_cancel(self._render_job)
            except tk.TclError:
                pass
            self._render_job = None
        self._render_content()

    def _flush_stream(self) -> None:
        self._render_job = None
        if self._streaming:
            self._render_stream_content()

    def _render_stream_content(self) -> None:
        """Append only the new stream text; style Markdown once on completion."""
        try:
            self.configure(state="normal")
            if self._stream_caret:
                caret = self.tag_ranges("caret")
                if len(caret) == 2:
                    self.delete(caret[0], caret[1])
                self._stream_caret = False
            if self.raw_text.startswith(self._stream_rendered):
                extra = self.raw_text[len(self._stream_rendered):]
                if extra:
                    self.insert("end-1c", extra)
            else:
                self.delete("1.0", "end")
                self.insert("1.0", self.raw_text)
            self._stream_rendered = self.raw_text
            self.insert("end-1c", " ▍", ("caret",))
            self._stream_caret = True
            self.configure(state="disabled")
            self._schedule_measure()
        except tk.TclError:
            pass

    def _render_content(self) -> None:
        try:
            self._stream_rendered = ""
            self._stream_caret = False
            selected = tuple(str(index) for index in self.tag_ranges("sel"))
            self.configure(state="normal")
            self.delete("1.0", "end")
            in_code = False
            normalized = self.raw_text.replace("\r\n", "\n").replace("\r", "\n")
            for raw_line in normalized.splitlines(keepends=True):
                has_newline = raw_line.endswith("\n")
                line = raw_line[:-1] if has_newline else raw_line
                if line.lstrip().startswith("```"):
                    in_code = not in_code
                    if in_code:
                        language = line.lstrip()[3:].strip().split(maxsplit=1)
                        if language:
                            self._insert_tagged(language[0].upper(), ("code_language",))
                            self.insert("end", "\n")
                    else:
                        self.insert("end", "\n")
                    continue
                if in_code:
                    self._insert_tagged(line + ("\n" if has_newline else ""), ("code",))
                    continue

                heading = self._HEADING.match(line)
                listing = self._LIST.match(line)
                quote = self._QUOTE.match(line)
                line_start = self.index("end-1c")
                if heading:
                    level = min(3, len(heading.group(1)))
                    self._insert_inline(heading.group(2), (f"heading{level}",))
                elif listing:
                    marker = listing.group(1)
                    self._insert_tagged(
                        ("• " if marker in {"-", "*", "+"} else f"{marker} "),
                        ("bullet",),
                    )
                    self._insert_inline(listing.group(2), ("list",))
                elif quote:
                    self._insert_tagged("│ ", ("quote_mark",))
                    self._insert_inline(quote.group(1), ("quote",))
                else:
                    self._insert_inline(line)
                if has_newline:
                    self.insert("end", "\n")
                line_end = self.index("end-1c")
                if not heading and not listing and not quote and line.strip():
                    self.tag_add("paragraph", line_start, line_end)

            if len(selected) == 2:
                self.tag_add("sel", selected[0], selected[1])
            self._rendered_lines = int(self.index("end-1c").split(".")[0])
            self.set_wrap_width(self.max_width)
            self.configure(state="disabled")
            self._schedule_measure()
        except tk.TclError:
            pass


class RoundedMessageSurface(tk.Canvas):
    """Rounded chat surface that resizes with its native text controls."""

    def __init__(self, parent: tk.Misc, *, fill: str, outline: str) -> None:
        self.fill = fill
        self.outline = outline
        self.pad = t.s(6)
        self.radius = t.s(17)
        super().__init__(parent, bg=t.CANVAS, bd=0, highlightthickness=0,
                         width=t.s(96), height=t.s(48))
        self.content = tk.Frame(self, bg=fill, bd=0)
        rounded_rect(self, 1, 1, t.s(94), t.s(46), self.radius,
                     fill=fill, outline=outline, tags="surface")
        self.create_window(self.pad, self.pad, anchor="nw", window=self.content)
        self.content.bind("<Configure>", self._sync_size, add="+")
        self.bind("<Configure>", self._draw, add="+")
        self._draw_signature: tuple[int, int] | None = None

    def _sync_size(self, _event=None) -> None:
        width = self.content.winfo_reqwidth() + self.pad * 2
        height = self.content.winfo_reqheight() + self.pad * 2
        if width != int(self.cget("width")) or height != int(self.cget("height")):
            self.configure(width=width, height=height)

    def _draw(self, event: tk.Event) -> None:
        size = (event.width, event.height)
        if size == self._draw_signature or min(size) < 4:
            return
        self._draw_signature = size
        self.delete("surface")
        rounded_rect(self, 1, 1, size[0] - 2, size[1] - 2, self.radius,
                     fill=self.fill, outline=self.outline, tags="surface")
        self.tag_lower("surface")


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

    def _team_context(self) -> bool:
        try:
            return bool(team_connection(self.bridge.client.base))
        except ValueError:
            return False

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
        self._mode_menu_pending = False
        self._mode_change_pending = False
        self.search_open = False
        self.message_count = 0
        self.message_bodies: list[MessageBody] = []
        self._stream_scroll_job: str | None = None
        self._scroll_job: str | None = None
        self._scroll_follow = False
        self._follow_latest = True
        self._wrap_job: str | None = None
        self._history_job: str | None = None
        self._history_generation = 0
        self._rendering_sid: int | None = None
        self._sidebar_filter_job: str | None = None
        self._sidebar_signature: tuple | None = None
        self._creating_session = False
        self._displayed_sid: int | None = None
        self._draft_sid: int | None = None
        self._drafts: dict[int, str] = {}
        self._context_unbound_draft = ""

        self.panel_pinned = False
        self.sidebar_visible = True
        self._sidebar_auto_hidden = False
        self._narrow_mode = False
        self._todos: list[dict] = []
        self._todos_signature: tuple | None = None
        self._jobs_live: list[str] = []
        self._live_rows: dict[str, dict] = {}
        self._jobs_active = False
        self._jobs_signature: tuple | None = None
        self._jobs_refresh_job: str | None = None
        self._pending = ""
        self._pending_visible = False
        self._queued_send: tuple[int, str] | None = None
        self._deleting_sid: int | None = None
        self._personal_jobs: list[dict] = []
        self._selected_personal_job: dict | None = None
        self._showing_personal_tasks = False

        self._build()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        self.columnconfigure(0, weight=0, minsize=t.s(310))
        self.columnconfigure(1, weight=1)
        self.columnconfigure(2, weight=0)
        self.rowconfigure(0, weight=1)

        self.sidebar = tk.Frame(self, bg=t.SIDEBAR, width=t.s(310))
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.pack_propagate(False)
        self.sidebar_separator = separator(self, vertical=True, color=t.LINE)
        self.sidebar_separator.grid(row=0, column=0, sticky="nse")
        self._build_sidebar()

        self.workspace = tk.Frame(self, bg=t.CANVAS)
        self.workspace.grid(row=0, column=1, sticky="nsew")
        self.workspace.columnconfigure(0, weight=1)
        self.workspace.rowconfigure(1, weight=1)
        self._build_toolbar()
        self._build_stage()
        self._build_composer()

        self.personal_tasks_page = tk.Frame(self.workspace, bg=t.CANVAS)
        self.personal_tasks_page.grid(row=1, column=0, rowspan=2, sticky="nsew")
        self.personal_tasks_page.grid_remove()
        self._build_personal_tasks_page()

        self._build_job_panel()
        self.bind("<Configure>", self._adapt_layout, add="+")

    def _build_toolbar(self) -> None:
        bar = tk.Frame(self.workspace, bg=t.CANVAS, height=t.s(38))
        bar.grid(row=0, column=0, sticky="ew")
        bar.pack_propagate(False)
        self.sidebar_button = FlatButton(
            bar, "‹", self._toggle_sidebar, font=self.fonts.small,
            variant="ghost", height=28, min_width=36, parent_bg=t.CANVAS)
        self.sidebar_button.pack(side="left", padx=(t.s(12), 0), pady=t.s(5))
        self.toolbar_title = tk.Label(
            bar, text="新对话", bg=t.CANVAS, fg=t.INK_SOFT,
            font=self.fonts.small_bold)
        self.toolbar_title.pack(side="left", padx=(t.s(8), 0), pady=t.s(8))
        right = tk.Frame(bar, bg=t.CANVAS)
        right.pack(side="right", padx=t.s(24))
        self.stop_button = FlatButton(
            right, "停止", self._stop, font=self.fonts.small, variant="ghost",
            height=28, min_width=58, parent_bg=t.CANVAS)
        self.stop_button.pack(side="right", padx=(t.s(6), 0))
        self.stop_button.set_enabled(False)
        self.panel_button = FlatButton(
            right, "个人任务", self.show_personal_tasks, font=self.fonts.small,
            variant="ghost", height=28, min_width=78, parent_bg=t.CANVAS)
        self.panel_button.pack_forget()
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

        # 固定区：个人导航、对话入口和个人项目 —— 不参与滚动
        header_zone = tk.Frame(self.sidebar, bg=t.SIDEBAR)
        header_zone.pack(side="top", fill="x")

        tk.Label(header_zone, text="个人空间", bg=t.SIDEBAR, fg=t.INK,
                 font=self.fonts.display_md).pack(
            anchor="w", padx=t.s(20), pady=(t.s(18), t.s(11)))

        new_chat = FlatButton(
            header_zone, "＋  新建对话", self.new_chat, font=self.fonts.small_bold,
            variant="primary", height=42, radius=10, parent_bg=t.SIDEBAR)
        new_chat.pack(fill="x", padx=t.s(18), pady=(t.s(13), t.s(18)))

        FlatButton(
            header_zone, "个人任务", self.show_personal_tasks,
            font=self.fonts.small_bold, variant="outline", height=36,
            radius=9, parent_bg=t.SIDEBAR,
        ).pack(fill="x", padx=t.s(18), pady=(0, t.s(13)))

        self._section_heading(header_zone, "个人项目")
        proj_actions = tk.Frame(header_zone, bg=t.SIDEBAR)
        proj_actions.pack(fill="x", padx=t.s(18), pady=(0, t.s(4)))
        FlatButton(
            proj_actions, "＋  新建项目", self._new_project, font=self.fonts.caption,
            variant="outline", height=32, radius=8, parent_bg=t.SIDEBAR,
        ).pack(fill="x")
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
        self.search_field.variable.trace_add("write", self._schedule_sidebar_refresh)
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
        self._empty_cache: dict[float, "ImageTk.PhotoImage"] = {}
        self._empty_size = None
        self._empty_draw_job: str | None = None

        # The greeting is typeset with real labels (ClearType) instead of
        # canvas text, which Tk renders without any anti-aliasing.
        card = tk.Frame(self.empty, bg=t.CANVAS)
        mark = tk.Frame(card, bg=t.TERRACOTTA_SOFT, width=t.s(50), height=t.s(50))
        mark.pack(pady=(0, t.s(13)))
        mark.pack_propagate(False)
        tk.Label(mark, text="✦", bg=t.TERRACOTTA_SOFT, fg=t.TERRACOTTA,
                 font=self.fonts.display_md).place(relx=.5, rely=.5, anchor="center")
        kicker(card, "VENUS  /  PERSONAL WORKSPACE", font=self.fonts.kicker,
               bg=t.CANVAS, fg=t.INK_MUTED).pack(pady=(0, t.s(9)))
        tk.Label(card, text="今天，想完成什么？", bg=t.CANVAS, fg=t.INK,
                 font=self.fonts.display_lg).pack()
        tk.Label(card,
                 text="描述一个问题、一个目标，或一段想法。Venus 会陪你理清下一步。",
                 bg=t.CANVAS, fg=t.INK_MUTED, font=self.fonts.small).pack(
            pady=(t.s(9), 0))

        suggestions = tk.Frame(card, bg=t.CANVAS)
        suggestions.pack(pady=(t.s(22), 0))
        for title, prompt in (
            ("研究与调研", "请帮我把这个研究方向梳理成核心问题、关键假设和下一步计划：\n\n"),
            ("代码解释", "请解释这段代码的关键逻辑、输入输出和可能的边界问题：\n\n"),
            ("项目规划", "请把这个目标整理成阶段计划，列出里程碑、依赖、风险和第一步：\n\n"),
        ):
            FlatButton(
                suggestions, title,
                lambda text=prompt: self._use_suggestion(text),
                font=self.fonts.small, variant="outline", height=38,
                radius=19, padx=14, parent_bg=t.CANVAS,
            ).pack(side="left", padx=t.s(5))
        tk.Label(card, text="点击示例可填入输入框并继续编辑 · 输入「!」可派发后台任务",
                 bg=t.CANVAS, fg=t.INK_MUTED, font=self.fonts.caption).pack(
            pady=(t.s(12), 0))
        self.empty_card = card
        self.empty_card.place(relx=.5, rely=.45, anchor="center")

        self.messages = ScrollArea(self.stage, bg=t.CANVAS, scrollbar=True)
        self.messages.grid(row=0, column=0, sticky="nsew")
        self.message_column = tk.Frame(self.messages.inner, bg=t.CANVAS)
        self.message_column.pack(fill="x", expand=True,
                                 padx=t.s(24), pady=(t.s(14), t.s(20)))
        self.messages.canvas.configure(yscrollcommand=self._on_message_yview)
        self.messages.on_user_scroll = self._on_user_message_scroll
        self.jump_latest = FlatButton(
            self.stage, "↓  回到最新", self._scroll_to_latest,
            font=self.fonts.small, variant="outline", height=34,
            radius=17, min_width=116, parent_bg=t.SURFACE,
        )
        self.jump_latest.place(relx=.5, rely=1.0, y=-t.s(12), anchor="s")
        self.jump_latest.place_forget()
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
            bg=t.SURFACE, fg=t.INK_MUTED, font=self.fonts.body, cursor="xterm")
        self.placeholder.place(x=t.s(13), y=t.s(10))
        self.placeholder.bind("<Button-1>",
                              lambda _event: self.input_box.focus_set(), add="+")
        self.input_box.bind("<FocusIn>",
                            lambda _event: editor.set_active(True), add="+")
        self.input_box.bind("<FocusOut>",
                            lambda _event: editor.set_active(False), add="+")
        self.input_box.bind("<KeyRelease>", self._update_placeholder, add="+")
        self.input_box.bind("<<Modified>>", self._on_input_modified, add="+")
        self.input_box.edit_modified(False)
        self.input_box.bind("<Return>", self._on_return, add="+")
        self.composer.watch(meta, editor, self.input_box, self.placeholder)

        actions = tk.Frame(self.composer, bg=t.SURFACE)
        actions.grid(row=2, column=0, sticky="ew", padx=t.s(11),
                     pady=(t.s(5), t.s(10)))
        self.send_button = RoundButton(actions, "→", self.send_message, font=self.fonts.display_md,
                    size=42, parent_bg=t.SURFACE)
        self.send_button.pack(side="right")
        self._refresh_send_state()
        tk.Label(actions, text="Enter 发送 · Shift + Enter 换行 · ! 开头走后台任务", bg=t.SURFACE,
                 fg=t.INK_MUTED, font=self.fonts.caption).pack(
            side="right", padx=(t.s(10), t.s(18)))

    def _build_personal_tasks_page(self) -> None:
        self.personal_tasks_page.columnconfigure(0, weight=1)
        self.personal_tasks_page.rowconfigure(1, weight=1)
        heading = tk.Frame(self.personal_tasks_page, bg=t.CANVAS, height=t.s(82))
        heading.grid(row=0, column=0, sticky="ew", padx=t.s(28))
        heading.grid_propagate(False)
        title = tk.Frame(heading, bg=t.CANVAS)
        title.pack(side="left", fill="y")
        tk.Label(title, text="个人任务", bg=t.CANVAS, fg=t.INK,
                 font=self.fonts.display_md).pack(anchor="w", pady=(t.s(13), 0))
        tk.Label(title, text="仅显示当前个人后端中属于你的私人任务。",
                 bg=t.CANVAS, fg=t.INK_MUTED,
                 font=self.fonts.caption).pack(anchor="w", pady=(t.s(2), 0))
        actions = tk.Frame(heading, bg=t.CANVAS)
        actions.pack(side="right", pady=t.s(18))
        FlatButton(actions, "刷新", self._request_jobs_refresh,
                   font=self.fonts.caption, variant="outline", height=30,
                   parent_bg=t.CANVAS).pack(side="right", padx=(t.s(6), 0))
        FlatButton(actions, "返回对话", self.show_chat_view,
                   font=self.fonts.caption, variant="ghost", height=30,
                   parent_bg=t.CANVAS).pack(side="right")
        separator(self.personal_tasks_page, color=t.LINE_FAINT).grid(
            row=0, column=0, sticky="sew")
        self.personal_tasks_scroll = ScrollArea(self.personal_tasks_page, bg=t.CANVAS,
                                                 scrollbar=True)
        self.personal_tasks_scroll.grid(row=1, column=0, sticky="nsew",
                                        padx=(t.s(24), t.s(20)), pady=t.s(14))
        self._render_personal_tasks()

    def show_personal_tasks(self) -> None:
        if self._team_context():
            self.app.toast("团队任务请在团队空间的「任务」页面查看")
            return
        self._showing_personal_tasks = True
        self.personal_tasks_page.grid()
        self.personal_tasks_page.tkraise()
        self.toolbar_title.configure(text="个人任务")
        self._request_jobs_refresh()
        self._render_personal_tasks()

    def show_chat_view(self) -> None:
        self._showing_personal_tasks = False
        self.personal_tasks_page.grid_remove()
        self.toolbar_title.configure(text=(
            self.bridge.sessions[self.bridge.current_sid].title
            if self.bridge.current_sid in self.bridge.sessions else "新对话"))
        self._show_messages() if self.message_count else self._show_empty()

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
        FlatButton(head, "返回对话", self.show_chat_view, font=self.fonts.caption,
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
        tk.Label(self.jobs_box, text="个人任务 · 0\n\n用「! 任务」把个人长任务派发到异步队列。",
                 bg=t.SURFACE_ALT, fg=t.INK_FAINT, font=self.fonts.caption,
                 justify="left", anchor="w", wraplength=t.s(250)).pack(
            anchor="w", pady=t.s(4))

    def _toggle_panel(self) -> None:
        if self._showing_personal_tasks:
            self.show_chat_view()
        else:
            self.show_personal_tasks()

    def _toggle_sidebar(self, *, manual: bool = True) -> None:
        if manual:
            self._sidebar_auto_hidden = False
        self.sidebar_visible = not self.sidebar_visible
        if self.sidebar_visible:
            self.columnconfigure(0, minsize=t.s(310))
            self.sidebar.grid()
            self.sidebar_separator.grid()
            self.sidebar_button.set_text("‹")
        else:
            self.columnconfigure(0, minsize=0)
            self.sidebar.grid_remove()
            self.sidebar_separator.grid_remove()
            self.sidebar_button.set_text("☰")
        self.after_idle(self._adapt_layout)

    def _bubble_wrap(self) -> int:
        try:
            width = max(t.s(240), self.workspace.winfo_width() or 900)
        except tk.TclError:
            width = 900
        # 输入区边距 + 气泡侧边留白后取整，窄窗自动收窄
        return max(t.s(140), min(t.s(680), width - t.s(112)))

    def _adapt_layout(self, _event=None) -> None:
        try:
            width = self.winfo_width()
        except tk.TclError:
            return
        if width < 60:
            return
        if width < t.s(760) and self.sidebar_visible:
            self._toggle_sidebar(manual=False)
            self._sidebar_auto_hidden = True
        elif width >= t.s(900) and self._sidebar_auto_hidden:
            if not self.sidebar_visible:
                self._toggle_sidebar(manual=False)
            self._sidebar_auto_hidden = False
        self._narrow_mode = width < t.s(1050)
        # Wait for resize to settle before measuring every visible message.
        if self._wrap_job is not None:
            self.after_cancel(self._wrap_job)
        self._wrap_job = self.after(80, self._apply_bubble_wrap)

    def _apply_bubble_wrap(self) -> None:
        self._wrap_job = None
        wrap = self._bubble_wrap()
        try:
            for body in self.message_bodies:
                body.set_wrap_width(wrap)
        except tk.TclError:
            pass

    def _auto_open_panel(self) -> None:
        # Task status is available from the explicit personal-task page; tool
        # events must not resize the chat into a third permanent column.
        if self._showing_personal_tasks:
            self._render_personal_tasks()

    # ------------------------------------------------------------- todo view
    def render_todos(self, todos) -> None:
        todos = todos if isinstance(todos, list) else (todos or {}).get("todos", [])
        self._todos = todos or []
        signature = tuple((str(item.get("title") or ""),
                           str(item.get("status") or "pending"))
                          for item in self._todos)
        if signature == self._todos_signature:
            return
        self._todos_signature = signature
        for child in self.todo_box.winfo_children():
            child.destroy()
        if not self._todos:
            self._render_todo_empty()
            self._auto_open_panel()
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
        jobs = personal_jobs(jobs or [])
        self._personal_jobs = jobs
        if self._selected_personal_job:
            selected_id = str(self._selected_personal_job.get("id") or "")
            self._selected_personal_job = next(
                (row for row in jobs if str(row.get("id") or "") == selected_id), None)
        signature = tuple(
            (job.get("id"), job.get("status"), job.get("title"),
             job.get("visibility"), job.get("is_mine"),
             job.get("owner_name"), job.get("owner"), job.get("change_id"),
             (job.get("progress") or {}).get("tool_calls"))
            for job in jobs
        )
        if signature == self._jobs_signature:
            if self._jobs_active and self._jobs_refresh_job is None:
                self._jobs_refresh_job = self.after(5000, self._request_jobs_refresh)
            return
        self._jobs_signature = signature
        for child in self.jobs_box.winfo_children():
            child.destroy()
        if not jobs:
            self._render_jobs_empty()
            self._jobs_active = False
            return
        active = False
        mine = jobs

        def heading(label: str, count: int) -> None:
            head = tk.Frame(self.jobs_box, bg=t.SURFACE_ALT)
            head.pack(fill="x", padx=t.s(3), pady=(t.s(7), t.s(3)))
            tk.Label(head, text=label, bg=t.SURFACE_ALT, fg=t.INK_SOFT,
                     font=self.fonts.small_bold).pack(side="left")
            badge = tk.Frame(head, bg=t.SURFACE, padx=t.s(7), pady=t.s(2))
            badge.pack(side="right")
            tk.Label(badge, text=str(count), bg=t.SURFACE, fg=t.INK_MUTED,
                     font=self.fonts.kicker).pack()

        def render_group(rows: list[dict], label: str) -> None:
            nonlocal active
            heading(label, len(rows))
            if not rows:
                tk.Label(self.jobs_box, text="暂无任务", bg=t.SURFACE_ALT,
                         fg=t.INK_FAINT, font=self.fonts.caption).pack(
                    anchor="w", padx=t.s(3), pady=(0, t.s(4)))
                return
            for job in rows:
                status = str(job.get("status") or "queued")
                if status in ("queued", "running", "waiting_confirm"):
                    active = True
                prog = job.get("progress") or {}
                meta_txt = JOB_STATUS_CN.get(status, status)
                tools = int(prog.get("tool_calls") or 0)
                if tools:
                    meta_txt = f"{tools} 次工具 · {meta_txt}"
                row = tk.Frame(self.jobs_box, bg=t.SURFACE, padx=t.s(8), pady=t.s(7),
                               highlightbackground=t.LINE_FAINT, highlightthickness=1)
                row.pack(fill="x", pady=(0, t.s(6)))
                dot = Dot(row, color=JOB_STATUS_COLOR.get(status, t.INK_MUTED),
                          size=6, bg=t.SURFACE)
                dot.pack(side="left", padx=(t.s(2), t.s(7)))
                col = tk.Frame(row, bg=t.SURFACE)
                col.pack(side="left", fill="x", expand=True)
                title = str(job.get("title") or "任务")
                if len(title) > 30:
                    title = title[:30] + "…"
                tk.Label(col, text=title, bg=t.SURFACE, fg=t.INK_SOFT,
                         font=self.fonts.caption, anchor="w", justify="left",
                         wraplength=t.s(170)).pack(anchor="w", fill="x")
                tk.Label(col, text="私人任务", bg=t.SURFACE, fg=t.INK_FAINT,
                         font=self.fonts.kicker, anchor="w", justify="left",
                         wraplength=t.s(170)).pack(anchor="w", fill="x")
                bar = ProgressBar(col, width=160, height=4, bg=t.SURFACE,
                                  trough=t.LINE_FAINT,
                                  fill=t.SUCCESS if status == "completed" else t.TERRACOTTA)
                bar.pack(anchor="w", pady=(t.s(3), 0))
                if status in ("queued", "running"):
                    bar.start_indeterminate()
                else:
                    bar.set_value(1.0 if status == "completed" else .35)
                right = tk.Frame(row, bg=t.SURFACE)
                right.pack(side="right")
                tk.Label(right, text=meta_txt, bg=t.SURFACE,
                         fg=JOB_STATUS_COLOR.get(status, t.INK_MUTED),
                         font=self.fonts.kicker).pack(anchor="e")
                if status in ("queued", "running", "waiting_confirm"):
                    FlatButton(
                        right, "取消",
                        lambda jid=job.get("id"): self._cancel_job(jid),
                        font=self.fonts.caption, variant="ghost", height=22,
                        padx=6, parent_bg=t.SURFACE).pack(anchor="e",
                                                              pady=(t.s(2), 0))

        render_group(mine, "个人任务")
        self._jobs_active = active
        if active and self._jobs_refresh_job is None:
            self._jobs_refresh_job = self.after(
                5000, self._request_jobs_refresh)
        if self._showing_personal_tasks:
            self._render_personal_tasks()

    def _request_jobs_refresh(self) -> None:
        self._jobs_refresh_job = None
        path = ("/api/v1/jobs?limit=200&scope=team" if self._team_context()
                else "/api/v1/jobs?limit=20")
        self.bridge.submit("jobs",
                           lambda: ("ok", self.bridge.client.get(path)))

    def _render_personal_tasks(self) -> None:
        scroll = getattr(self, "personal_tasks_scroll", None)
        if scroll is None:
            return
        page = scroll.inner
        for child in page.winfo_children():
            child.destroy()
        if self._team_context():
            tk.Label(page, text="个人任务只在个人空间显示。",
                     bg=t.CANVAS, fg=t.WARNING,
                     font=self.fonts.body).pack(anchor="w", padx=t.s(12), pady=t.s(12))
            return
        todos = getattr(self, "_todos", [])
        if todos:
            activity = tk.Frame(page, bg=t.SURFACE_ALT, padx=t.s(12), pady=t.s(10))
            activity.pack(fill="x", pady=(0, t.s(10)))
            done = sum(1 for item in todos if str(item.get("status") or "") == "done")
            tk.Label(activity, text=f"当前会话计划 · {done}/{len(todos)} 已完成",
                     bg=t.SURFACE_ALT, fg=t.INK, font=self.fonts.small_bold).pack(
                         anchor="w", pady=(0, t.s(5)))
            for item in todos:
                row = tk.Frame(activity, bg=t.SURFACE_ALT)
                row.pack(fill="x", pady=t.s(2))
                status = str(item.get("status") or "pending")
                TodoIcon(row, status=status, bg=t.SURFACE_ALT).pack(
                    side="left", padx=(0, t.s(7)))
                tk.Label(row, text=str(item.get("title") or "计划事项"),
                         bg=t.SURFACE_ALT, fg=t.INK_SOFT, font=self.fonts.caption,
                         anchor="w", justify="left", wraplength=max(
                             t.s(340), self.winfo_width() - t.s(430))).pack(
                                 side="left", fill="x", expand=True)
        if not self._personal_jobs:
            tk.Label(page, text="还没有个人后台任务。\n在个人对话输入以「!」开头的任务，可将长任务放入个人队列。",
                     bg=t.CANVAS, fg=t.INK_FAINT, font=self.fonts.body,
                     anchor="w", justify="left").pack(fill="x", padx=t.s(10), pady=t.s(12))
            return
        for job in self._personal_jobs:
            row = tk.Frame(page, bg=t.SURFACE, highlightbackground=t.LINE_FAINT,
                           highlightthickness=1, padx=t.s(12), pady=t.s(9), cursor="hand2")
            row.pack(fill="x", pady=(0, t.s(8)))
            status = str(job.get("status") or "queued")
            heading = tk.Frame(row, bg=t.SURFACE)
            heading.pack(fill="x")
            tk.Label(heading, text=str(job.get("title") or "个人任务"),
                     bg=t.SURFACE, fg=t.INK, font=self.fonts.small_bold,
                     anchor="w").pack(side="left", fill="x", expand=True)
            tk.Label(heading, text=JOB_STATUS_CN.get(status, status),
                     bg=t.SURFACE, fg=JOB_STATUS_COLOR.get(status, t.INK_MUTED),
                     font=self.fonts.caption).pack(side="right")
            created = str(job.get("created_at") or job.get("created") or "")
            tk.Label(row, text=(f"私人任务 · {created}" if created else "私人任务"),
                     bg=t.SURFACE, fg=t.INK_MUTED,
                     font=self.fonts.caption, anchor="w").pack(fill="x", pady=(t.s(4), 0))
            for widget in (row, heading, *row.winfo_children(), *heading.winfo_children()):
                widget.bind("<Button-1>", lambda _event, item=dict(job):
                            self._select_personal_job(item), add="+")
        if self._selected_personal_job:
            selected_id = str(self._selected_personal_job.get("id") or "")
            selected = next((row for row in self._personal_jobs
                             if str(row.get("id") or "") == selected_id), None)
            if selected:
                detail = tk.Frame(page, bg=t.SURFACE_ALT, padx=t.s(12), pady=t.s(10))
                detail.pack(fill="x", pady=(t.s(2), t.s(10)))
                tk.Label(detail, text="任务详情", bg=t.SURFACE_ALT, fg=t.INK,
                         font=self.fonts.small_bold).pack(anchor="w")
                status = str(selected.get("status") or "queued")
                result = str(selected.get("result") or selected.get("output") or "").strip()
                error = str(selected.get("error") or selected.get("detail") or "").strip()
                text = (f"状态：{JOB_STATUS_CN.get(status, status)}\n" +
                        (f"错误：{error}" if error else
                         f"结果：{result}" if result else "任务仍在执行或尚无结果。"))
                tk.Label(detail, text=text, bg=t.SURFACE_ALT, fg=t.INK_SOFT,
                         font=self.fonts.caption, anchor="w", justify="left",
                         wraplength=max(t.s(320), self.winfo_width() - t.s(390))).pack(
                             fill="x", pady=(t.s(5), 0))
                if status in {"queued", "running", "waiting_confirm"}:
                    FlatButton(detail, "取消任务",
                               lambda jid=selected.get("id"): self._cancel_job(jid),
                               font=self.fonts.caption, variant="outline", height=28,
                               parent_bg=t.SURFACE_ALT).pack(anchor="w", pady=(t.s(7), 0))

    def _select_personal_job(self, job: dict) -> None:
        if job.get("visibility") == "team":
            return
        self._selected_personal_job = job
        self._render_personal_tasks()

    def _cancel_job(self, job_id) -> None:
        if not job_id:
            return
        self.bridge.submit("job_cancel", lambda: self.bridge.client.post(
            f"/api/v1/jobs/{job_id}/cancel"))

    def _submit_team_change(self, job: dict) -> None:
        open_job_change_submit(self, self.app, self.fonts, self.bridge.client, job)

    # ------------------------------------------------------- app event hooks
    def on_health(self, data: dict) -> None:
        try:
            ready = bool(data.get("configured"))
            self._foot_dot.set_color(t.SUCCESS if ready else t.WARNING)
            self._foot_text.configure(text="在线" if ready else "待配置",
                                      fg=t.SUCCESS if ready else t.WARNING)
            self._svc_line.configure(text=str(data.get("model") or "—"),
                                     fg=t.SUCCESS if ready else t.WARNING)
            mem = data.get("memory_stats") or {}
            self._mem_line.configure(
                text=f"{mem.get('l1_memories', 0)} 条记忆" if mem.get("enabled")
                else "未启用", fg=t.INK_SOFT)
            if data.get("model"):
                self.model_chip.set_text(
                    f"{fit_text(self.fonts.small_bold, str(data['model']), t.s(190))} ▾")
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
        if self._sidebar_filter_job is not None:
            try:
                self.after_cancel(self._sidebar_filter_job)
            except tk.TclError:
                pass
            self._sidebar_filter_job = None
        sessions = sorted(self.bridge.sessions.values(),
                          key=lambda s: s.sid, reverse=True)
        query = (self.search_field.get().strip().casefold()
                 if hasattr(self, "search_field") else "")
        rows = [s for s in sessions
                if not query or query in (s.title or "").casefold()]
        signature = (query, self.bridge.current_sid,
                     tuple((s.sid, s.title, s.updated, s.message_count)
                           for s in rows))
        if signature == self._sidebar_signature:
            return
        self._sidebar_signature = signature
        for child in self.conversation_box.winfo_children():
            child.destroy()
        if not rows:
            empty_text = ("没有匹配的对话。" if query else
                          "还没有对话，点上方「新建对话」。")
            tk.Label(self.conversation_box, text=empty_text,
                     bg=t.SIDEBAR, fg=t.INK_FAINT, font=self.fonts.caption).pack(
                anchor="w", padx=t.s(12), pady=t.s(3))
            return
        for state in rows:
            meta = state.updated or (f"{state.message_count} 条"
                                     if state.message_count else "")
            row = SidebarItem(
                self.conversation_box,
                title=fit_text(self.fonts.small, state.title or f"会话 {state.sid}",
                               t.s(216)),
                font=self.fonts.small,
                command=lambda sid=state.sid: self._select_session(sid),
                meta=meta,
                on_delete=lambda st=state: self._confirm_delete_session(st))
            row.pack(fill="x", pady=1)
            row.set_active(state.sid == self.bridge.current_sid)

    def _schedule_sidebar_refresh(self, *_args) -> None:
        if self._sidebar_filter_job is not None:
            try:
                self.after_cancel(self._sidebar_filter_job)
            except tk.TclError:
                pass
        self._sidebar_filter_job = self.after(90, self.refresh_sidebar)

    def refresh_projects(self) -> None:
        for child in self.project_box.winfo_children():
            child.destroy()
        projects = [p for p in self.bridge.projects if not p.get("is_team")]
        selected = next((p for p in projects
                         if str(p.get("id") or "") == self.bridge.active_project_id), None)
        target = (f"个人派活项目 · "
                  f"{selected.get('title') or '项目'}" if selected else "派活目标：未指定")
        target_row = tk.Frame(self.project_box, bg=t.SIDEBAR)
        target_row.pack(fill="x", padx=t.s(9), pady=(0, t.s(3)))
        tk.Label(target_row, text=fit_text(self.fonts.caption, target, t.s(205)),
                 bg=t.SIDEBAR, fg=(t.TERRACOTTA if selected and selected.get("is_team")
                                   else t.INK_MUTED),
                 font=self.fonts.caption).pack(side="left")
        if selected:
            clear = tk.Label(target_row, text="清除", bg=t.SIDEBAR, fg=t.INK_FAINT,
                             font=self.fonts.caption, cursor="hand2")
            clear.pack(side="right")
            clear.bind("<Button-1>", lambda _event: self._select_project(""), add="+")
        if not projects:
            tk.Label(self.project_box, text="暂无个人项目", bg=t.SIDEBAR,
                     fg=t.INK_FAINT, font=self.fonts.caption).pack(
                anchor="w", padx=t.s(12), pady=t.s(3))
            return
        for item in projects[-6:]:
            status = str(item.get("status") or "planning")
            row = SidebarItem(
                self.project_box,
                title=fit_text(self.fonts.small, str(item.get("title") or "项目"),
                               t.s(216)),
                font=self.fonts.small,
                command=lambda pid=item.get("id"): self._select_project(pid),
                meta=f"个人 · {PROJECT_STATUS_CN.get(status, status)}",
                meta_color=PROJECT_STATUS_COLOR.get(status, t.INK_MUTED),
                show_dot=True)
            row.pack(fill="x", pady=1)
            row.set_active(str(item.get("id")) == self.bridge.active_project_id)
        tk.Label(
            self.project_box,
            text="个人项目只用于你自己的后台任务。团队项目请到团队空间选择。",
            bg=t.SIDEBAR, fg=t.INK_FAINT, font=self.fonts.caption,
            wraplength=t.s(255), justify="left",
        ).pack(anchor="w", padx=t.s(9), pady=(t.s(4), 0))

    def _open_team_collab(self) -> None:
        self.app.show_team()
        team_view = getattr(self.app, "team_workspace_view", None)
        if team_view is not None:
            team_view.show_page("changes")

    def _select_project(self, project_id) -> None:
        setter = getattr(self.app, "set_active_project", None)
        if callable(setter):
            setter(str(project_id or ""))
            self.refresh_projects()
            return
        self.bridge.active_project_id = str(project_id or "")
        self.refresh_projects()
        if project_id:
            selected = next((p for p in self.bridge.projects
                             if str(p.get("id") or "") == str(project_id)), None)
            self.app.toast(
                f"已设为派活目标：{selected.get('title') if selected else '项目'}；"
                "普通对话仍保留在当前私有会话中")
        else:
            self.app.toast("已清除派活项目；后续派活默认为个人任务")

    def refresh_backend_switch_button(self) -> None:
        button = getattr(self, "backend_switch_button", None)
        if button is None:
            return
        button.pack_forget()

    def prepare_backend_context_switch(self) -> str:
        self._save_draft()
        try:
            if self._draft_sid is None:
                return self.input_box.get("1.0", "end-1c")
        except tk.TclError:
            pass
        return ""

    def reset_backend_context(self, label: str, drafts: dict[int, str],
                              unbound_draft: str = "") -> None:
        self._clear_thread()
        self._drafts = dict(drafts or {})
        self._context_unbound_draft = str(unbound_draft or "")
        self._displayed_sid = None
        self._draft_sid = None
        self._pending = ""
        self._pending_visible = False
        self._queued_send = None
        self.input_box.delete("1.0", "end")
        if unbound_draft:
            self.input_box.insert("1.0", unbound_draft)
        self._update_placeholder()
        self._reset_live_jobs()
        self._clear_server_jobs()
        self.toolbar_title.configure(text=f"新对话 · {label}")
        self._show_empty()
        self._sidebar_signature = None
        self.refresh_sidebar()
        self.refresh_projects()
        self.refresh_backend_switch_button()
        self._refresh_send_state()

    def _clear_server_jobs(self) -> None:
        """Remove origin-specific jobs and stop their polling timer."""
        if self._jobs_refresh_job is not None:
            try:
                self.after_cancel(self._jobs_refresh_job)
            except tk.TclError:
                pass
            self._jobs_refresh_job = None
        for child in self.jobs_box.winfo_children():
            child.destroy()
        self._jobs_active = False
        self._jobs_signature = None
        self._render_jobs_empty()

    def show_empty_backend_context(self) -> None:
        self._clear_thread()
        self._displayed_sid = None
        self._draft_sid = None
        self.toolbar_title.configure(text="新对话")
        self._show_empty()
        self.refresh_sidebar()
        self.refresh_projects()

    def _new_project(self) -> None:
        if self._team_context():
            self.app.toast("请切换到团队空间，在项目管理中创建团队项目")
            return
        dlg = tk.Toplevel(self.app.root)
        dlg.title("新建项目")
        dlg.configure(bg=t.HEADER)
        dlg.transient(self.app.root)
        dlg.grab_set()
        card = tk.Frame(dlg, bg=t.HEADER, padx=t.s(18), pady=t.s(16))
        card.pack(fill="both", expand=True)
        tk.Label(card, text="新建个人项目", bg=t.HEADER, fg=t.INK,
                 font=self.fonts.display_md).pack(anchor="w")
        tk.Label(card, text="个人项目只用于你的私人后台任务。团队项目请在团队空间的项目管理中创建。",
                 bg=t.HEADER, fg=t.INK_MUTED, font=self.fonts.caption,
                 wraplength=t.s(320), justify="left").pack(
            anchor="w", pady=(t.s(6), t.s(10)))
        tk.Label(card, text="项目名称", bg=t.HEADER, fg=t.INK_SOFT,
                 font=self.fonts.caption).pack(anchor="w")
        title_var = tk.StringVar()
        title_entry = tk.Entry(
            card, textvariable=title_var, bg=t.SURFACE, fg=t.INK,
            insertbackground=t.INK, relief="flat", font=self.fonts.body)
        title_entry.pack(fill="x", ipady=t.s(6), pady=(t.s(4), t.s(10)))
        tk.Label(card, text="目标（可选）", bg=t.HEADER, fg=t.INK_SOFT,
                 font=self.fonts.caption).pack(anchor="w")
        goal_var = tk.StringVar()
        goal_entry = tk.Entry(
            card, textvariable=goal_var, bg=t.SURFACE, fg=t.INK,
            insertbackground=t.INK, relief="flat", font=self.fonts.body)
        goal_entry.pack(fill="x", ipady=t.s(6), pady=(t.s(4), t.s(10)))
        actions = tk.Frame(card, bg=t.HEADER)
        actions.pack(fill="x")

        def submit() -> None:
            title = title_var.get().strip()
            if not title:
                self.app.toast("请输入项目名称")
                return
            goal = goal_var.get().strip()
            dlg.destroy()
            self.bridge.submit("project_create", lambda: self.bridge.client.post(
                "/api/v1/projects", {
                    "title": title,
                    "goal": goal,
                    "is_team": False,
                }))

        FlatButton(actions, "取消", dlg.destroy, font=self.fonts.small,
                   variant="ghost", height=34, min_width=80,
                   parent_bg=t.HEADER).pack(side="right", padx=(t.s(8), 0))
        FlatButton(actions, "创建", submit, font=self.fonts.small_bold,
                   variant="primary", height=34, min_width=80,
                   parent_bg=t.HEADER).pack(side="right")
        title_entry.focus_set()
        dlg.bind("<Return>", lambda _e: submit())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.update_idletasks()
        rx = self.app.root.winfo_rootx() + t.s(120)
        ry = self.app.root.winfo_rooty() + t.s(140)
        dlg.geometry(f"+{rx}+{ry}")

    def _confirm_delete_session(self, state) -> None:
        if self._deleting_sid is not None:
            self.app.toast("正在删除上一条对话，请稍候", duration=2200)
            return
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
        self._creating_session = False
        self._pending_visible = False
        self._displayed_sid = sid
        if self._pending:
            self._draft_sid = sid
        else:
            current_draft = self.input_box.get("1.0", "end-1c")
            if current_draft:
                self._drafts[sid] = current_draft
            self._restore_draft(sid)
        self._clear_thread()
        self._reset_live_jobs()
        self.toolbar_title.configure(text="新对话")
        self._show_empty()
        self.refresh_sidebar()
        self.input_box.focus_set()
        if self._pending:
            self._after_session_created_hook()

    def on_session_create_failed(self, detail: str = "") -> None:
        self._creating_session = False
        if self._pending_visible:
            self._show_messages()
            reason = str(detail or "请检查后端连接")[:240]
            self._append_bubble("assistant", f"会话创建失败：{reason}。输入内容已保留，请重试发送。")
            self.message_count += 1
            self._scroll_end(force=True)
        self._pending = ""
        if self._draft_sid is None and self._displayed_sid is not None:
            self._draft_sid = self._displayed_sid
            if not self.input_box.get("1.0", "end-1c"):
                self._restore_draft(self._displayed_sid)
        self.app.toast(detail or "新建对话失败，请重试", duration=4200)
        self._refresh_send_state()

    def on_session_load_failed(self, sid: int) -> None:
        """Keep the displayed conversation and its draft in sync after a failed load."""
        if self._queued_send is not None and self._queued_send[0] == sid:
            self._queued_send = None
            self.app.toast("对话加载失败，消息未发送，输入内容已保留", duration=4200)
        if sid != self.bridge.current_sid:
            return
        self._save_draft()
        visible_sid = self._displayed_sid
        if visible_sid is not None and visible_sid in self.bridge.sessions:
            self.bridge.current_sid = visible_sid
            self._restore_draft(visible_sid)
        else:
            self.bridge.current_sid = None
        self.refresh_sidebar()
        self._refresh_send_state()

    def render_session(self, sid: int) -> None:
        if sid != self.bridge.current_sid:
            return
        state = self.bridge.sessions.get(sid)
        if state is None:
            return
        if self._draft_sid != sid:
            self._save_draft()
            if self._context_unbound_draft:
                self._drafts[sid] = self._context_unbound_draft
                self._context_unbound_draft = ""
            self._restore_draft(sid)
        self._displayed_sid = sid
        self.toolbar_title.configure(
            text=fit_text(self.fonts.small_bold, state.title or "新对话", t.s(430)))
        self._clear_thread()
        self._reset_live_jobs()
        history = [m for m in state.messages
                   if str(m.get("role") or "") in ("user", "assistant")]
        if not history:
            self._show_empty()
            self.refresh_sidebar()
            self._flush_queued_send(sid)
            return
        self._show_messages()
        self._rendering_sid = sid
        self._refresh_send_state()
        generation = self._history_generation

        def append_batch(index: int = 0) -> None:
            self._history_job = None
            if generation != self._history_generation:
                return
            next_index = index
            deadline = time.perf_counter() + 0.012
            while next_index < len(history) and (
                next_index == index or time.perf_counter() < deadline
            ):
                m = history[next_index]
                self._append_bubble(str(m.get("role")), str(m.get("content") or ""))
                next_index += 1
            if next_index < len(history):
                self._history_job = self.after(4, append_batch, next_index)
            else:
                self._rendering_sid = None
                self._refresh_send_state()
                self._scroll_end(force=True)
                self._flush_queued_send(sid)

        append_batch()
        self.refresh_sidebar()
        self.input_box.focus_set()

    def _queue_send_for_load(self, sid: int, text: str) -> None:
        self._queued_send = (sid, text)
        self._drafts[sid] = text
        self.app.toast("对话加载中，加载完成后自动发送", duration=2600)
        self._refresh_send_state()

    def _flush_queued_send(self, sid: int) -> None:
        queued = self._queued_send
        if queued is None or queued[0] != sid:
            return
        self._queued_send = None
        self.input_box.delete("1.0", "end")
        self.input_box.insert("1.0", queued[1])
        self._update_placeholder()
        self.send_message()

    # -------------------------------------------------------------- chat flow
    def send_message(self) -> None:
        text = self.input_box.get("1.0", "end-1c").strip()
        if not text:
            self.app.toast("请先输入一条消息")
            self.input_box.focus_set()
            return
        if self._creating_session:
            if self._pending:
                self.app.toast("上一条消息正在等待新对话创建", duration=2200)
                return
            self._pending = text
            self._refresh_send_state()
            self.app.toast("正在创建对话，消息稍后发送", duration=2200)
            return
        if self.bridge._streaming:
            self.app.toast("Venus 正在执行，可点上方「停止」", duration=2600)
            return
        dispatch = text.startswith("!") or text.lower().startswith("/dispatch ")
        if self._team_context():
            if not dispatch:
                self.app.toast("团队空间请用「! 任务」派活；个人对话请切回个人空间")
                return
            if not self.bridge.active_project_id:
                self.app.toast("请先选择并认领一个团队项目作为派活目标")
                return
            task = text[1:].strip() if text.startswith("!") else text[len("/dispatch "):].strip()
            if not task:
                self.app.toast("请填写任务内容")
                return
            self.input_box.delete("1.0", "end")
            self._update_placeholder()
            self.bridge.dispatch(task, agent_preference=self.agent_name)
            return
        if self.bridge.current_sid is None:
            if self._pending_visible:
                self._clear_thread()
                self._pending_visible = False
            if self._displayed_sid is None and not dispatch:
                self._show_messages()
                self._append_bubble("user", text)
                self.message_count += 1
                self._scroll_end(force=True)
                self._pending_visible = True
            self.app.toast("正在创建会话…")
            self._pending = text
            self._creating_session = True
            self._refresh_send_state()
            self.bridge.create_session()
            return
        state = self.bridge.sessions.get(self.bridge.current_sid)
        if state is None:
            self.app.toast("当前对话不可用，请重新选择或新建")
            return
        if self._displayed_sid != self.bridge.current_sid:
            self._queue_send_for_load(self.bridge.current_sid, text)
            self.bridge.load_session(self.bridge.current_sid)
            return
        if self._rendering_sid == self.bridge.current_sid:
            self._queue_send_for_load(self.bridge.current_sid, text)
            return
        if not state.loaded:
            self._queue_send_for_load(self.bridge.current_sid, text)
            self.bridge.load_session(self.bridge.current_sid)
            return
        self.input_box.delete("1.0", "end")
        self._update_placeholder()
        if self._draft_sid is not None:
            self._drafts.pop(self._draft_sid, None)
        if not dispatch:
            self._show_messages()
            self._append_bubble("user", text)
            self.message_count += 1
            self._scroll_end(force=True)
        self.bridge.send_message(text, self._make_turn,
                                 agent_preference=self.agent_name)

    def _make_turn(self, _initial: str = "") -> dict:
        """message_cb: create the live agent turn; handle routed back as stream_start."""
        self._show_messages()
        row = tk.Frame(self.message_column, bg=t.CANVAS)
        row.pack(fill="x", pady=t.s(7))
        stack = tk.Frame(row, bg=t.CANVAS)
        stack.pack(anchor="w", fill="x")
        bubble = RoundedMessageSurface(
            row, fill=t.AGENT_MESSAGE, outline=t.LINE_STRONG)
        bubble.pack(anchor="w", pady=(t.s(4), 0))
        head = tk.Frame(bubble.content, bg=t.AGENT_MESSAGE)
        head.pack(fill="x", padx=t.s(16), pady=(t.s(10), t.s(4)))
        dot = Dot(head, color=t.TERRACOTTA, size=6, bg=t.AGENT_MESSAGE)
        dot.pack(side="left", padx=(0, t.s(6)))
        state = tk.Label(head, text="思考中", bg=t.AGENT_MESSAGE, fg=t.INK_MUTED,
                         font=self.fonts.kicker)
        state.pack(side="left")
        body = MessageBody(
            bubble.content, fonts=self.fonts, bg=t.AGENT_MESSAGE,
            compact=False, max_width=self._bubble_wrap(),
            on_layout=self._schedule_stream_scroll,
        )
        body.pack(fill="x", padx=t.s(16), pady=(0, t.s(12)))
        self.message_bodies.append(body)
        FlatButton(
            head, "复制", lambda target=body: self._copy_body(target),
            font=self.fonts.caption, variant="ghost", height=23,
            padx=7, parent_bg=t.AGENT_MESSAGE,
        ).pack(side="right")
        turn = {"row": row, "stack": stack, "body": body, "dot": dot,
                "state": state, "cards": {}}
        self._turn = turn
        self._scroll_end(force=True)
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
        self._refresh_send_state()

    # ---------------------------------------------------- bridge ui delivery
    def handle_backend(self, kind: str, payload) -> None:
        try:
            if kind == "stream_start":
                if isinstance(payload, dict):
                    self._turn = payload
                self._set_streaming(True)
            elif kind == "stream_delta":
                if not self.streaming:
                    return
                turn = self._turn or self._make_turn()
                turn["state"].configure(text="输出中", fg=t.TERRACOTTA)
                turn["body"].set_content(str(payload), streaming=True)
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
                self.bridge.refresh_health()
            elif kind == "stream_error":
                if self._turn is not None:
                    body = self._turn["body"]
                    partial = body.raw_text.strip()
                    error = f"请求失败：{str(payload)[:320]}"
                    body.set_content(f"{partial}\n\n{error}" if partial else error)
                    self._end_turn("发送失败")
                self._set_streaming(False)
                self.app.toast(str(payload)[:150], duration=4200)
            elif kind == "agents":
                code, data = payload if isinstance(payload, tuple) else (200, {})
                if code == 200:
                    self.agents = list(data.get("agents") or [])
            elif kind == "mode_list":
                self._mode_menu_pending = False
                code, data = payload if isinstance(payload, tuple) else (0, {})
                if code == 200 and isinstance(data, dict):
                    if self.app._top_view() is self and self.mode_chip.winfo_ismapped():
                        self._open_mode_menu(data)
                else:
                    self.app.toast(f"读取确认模式失败：{(data or {}).get('detail', code)}")
            elif kind == "mode_change":
                self._mode_change_pending = False
                mode, result = payload if isinstance(payload, tuple) and len(payload) == 2 else ("", (0, {}))
                code, data = result if isinstance(result, tuple) and len(result) == 2 else (0, {})
                if code == 200:
                    cn = {"auto": "自动", "strict": "严格", "trusted": "信任",
                          "query": "只读", "plan": "计划"}.get(mode, mode)
                    self.mode_chip.set_text(f"{cn}模式 ▾")
                    self.app.toast(f"确认模式：{cn}")
                else:
                    self.app.toast(f"切换失败：{(data or {}).get('detail', code)}")
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
            elif kind == "project_create":
                code, data = payload if isinstance(payload, tuple) else (200, {})
                if code == 200:
                    created = ((data or {}).get("project") or
                               (data or {}).get("created") or {})
                    kind_cn = "团队" if created.get("is_team") else "个人"
                    self.app.toast(f"已创建{kind_cn}项目：{created.get('name') or created.get('title', '')}")
                    pid = str(created.get("id") or "")
                    if pid:
                        if not any(str(row.get("id") or "") == pid
                                   for row in self.bridge.projects):
                            self.bridge.projects.append(dict(created))
                        setter = getattr(self.app, "set_active_project", None)
                        if callable(setter):
                            setter(pid)
                        else:
                            self.bridge.active_project_id = pid
                        self.refresh_projects()
                else:
                    self.app.toast(f"创建失败：{(data or {}).get('detail', code)}")
            elif kind == "session_delete":
                code = payload[0] if isinstance(payload, tuple) else 200
                if code == 200:
                    self.app.toast("对话已删除")
                    if self._deleting_sid is not None \
                            and self._deleting_sid == self._displayed_sid:
                        self._clear_thread()
                        self._displayed_sid = None
                        self._drafts.pop(self._deleting_sid, None)
                        self._draft_sid = None
                        self.input_box.delete("1.0", "end")
                        self._update_placeholder()
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
        if not isinstance(d, dict) or not self.streaming:
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
        if not isinstance(d, dict) or not self.streaming or self._turn is None:
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
        if self._stream_scroll_job is not None:
            try:
                self.after_cancel(self._stream_scroll_job)
            except tk.TclError:
                pass
            self._stream_scroll_job = None
        try:
            body = turn["body"]
            if not body.raw_text.strip():
                body.set_content(f"（{status}· 本轮没有文本回复）")
                turn["body"].configure(fg=t.INK_FAINT)
            else:
                body.set_content(body.raw_text)
            turn["dot"].set_color(t.INK_FAINT)
            turn["state"].configure(text=time.strftime(f"{status} %H:%M"),
                                    fg=t.INK_FAINT)
            self._scroll_end()
        except tk.TclError:
            pass

    def _after_session_created_hook(self) -> None:
        pending = getattr(self, "_pending", "")
        self._pending = ""
        if pending:
            if self.input_box.get("1.0", "end-1c").strip() == pending:
                self.input_box.delete("1.0", "end")
                self._update_placeholder()
            if self._draft_sid is not None:
                self._drafts.pop(self._draft_sid, None)
            self.send_pending(pending)

    def send_pending(self, text: str) -> None:
        if self.bridge.current_sid is None:
            return
        dispatch = text.startswith("!") or text.lower().startswith("/dispatch ")
        if not dispatch:
            self._show_messages()
            self._append_bubble("user", text)
            self.message_count += 1
            self._scroll_end(force=True)
        self.bridge.send_message(text, self._make_turn,
                                 agent_preference=self.agent_name)

    # ------------------------------------------------------------- rendering
    def _show_messages(self) -> None:
        self.messages.tkraise()
        if hasattr(self, "jump_latest"):
            # FlatButton 是 Canvas 子类：.lift()/.tkraise() 都被 Canvas
            # 重定向到 tag_raise，只能用 tk call raise 抬窗口层级。
            self.jump_latest.tk.call("raise", self.jump_latest._w)

    def _show_empty(self) -> None:
        self.empty.tk.call("raise", self.empty._w)
        if hasattr(self, "jump_latest"):
            self.jump_latest.place_forget()

    def _clear_thread(self) -> None:
        if self._wrap_job is not None:
            try:
                self.after_cancel(self._wrap_job)
            except tk.TclError:
                pass
            self._wrap_job = None
        self._history_generation += 1
        if self._history_job is not None:
            try:
                self.after_cancel(self._history_job)
            except tk.TclError:
                pass
            self._history_job = None
        self._rendering_sid = None
        if self._stream_scroll_job is not None:
            try:
                self.after_cancel(self._stream_scroll_job)
            except tk.TclError:
                pass
            self._stream_scroll_job = None
        if self._scroll_job is not None:
            try:
                self.after_cancel(self._scroll_job)
            except tk.TclError:
                pass
            self._scroll_job = None
        self._scroll_follow = False
        self._follow_latest = True
        for child in self.message_column.winfo_children():
            child.destroy()
        self.message_count = 0
        self.message_bodies.clear()
        self._turn = None
        if hasattr(self, "jump_latest"):
            self.jump_latest.place_forget()

    def _append_bubble(self, role: str, text: str) -> MessageBody:
        user = role == "user"
        row = tk.Frame(self.message_column, bg=t.CANVAS)
        row.pack(fill="x", pady=t.s(7))
        bubble_bg = t.USER_MESSAGE if user else t.AGENT_MESSAGE
        bubble = RoundedMessageSurface(
            row, fill=bubble_bg,
            outline=t.LINE_STRONG if user else t.LINE)
        bubble.pack(side="right" if user else "left",
                    padx=(t.s(48), 0) if user else (0, t.s(48)))
        head = tk.Frame(bubble.content, bg=bubble_bg)
        head.pack(fill="x", padx=t.s(16), pady=(t.s(10), t.s(4)))
        if not user:
            Dot(head, color=t.TERRACOTTA, size=6, bg=bubble_bg).pack(
                side="left", padx=(0, t.s(6)))
        tk.Label(head, text="你" if user else "VENUS", bg=bubble_bg,
                 fg=t.TERRACOTTA if user else t.INK_MUTED,
                 font=self.fonts.caption).pack(side="left")
        body = MessageBody(
            bubble.content, fonts=self.fonts, bg=bubble_bg, text=text,
            compact=True, max_width=self._bubble_wrap(),
            on_layout=self._schedule_stream_scroll,
        )
        body.pack(fill="x", padx=t.s(16), pady=(0, t.s(12)))
        self.message_bodies.append(body)
        FlatButton(
            head, "复制", lambda target=body: self._copy_body(target),
            font=self.fonts.caption, variant="ghost", height=23,
            padx=7, parent_bg=bubble_bg,
        ).pack(side="right")
        return body

    def _copy_body(self, body: MessageBody) -> None:
        try:
            text = body.raw_text
            if not text:
                self.app.toast("当前还没有可复制的内容")
                return
            self.clipboard_clear()
            self.clipboard_append(text)
        except tk.TclError:
            self.app.toast("复制失败，请稍后重试")
        else:
            self.app.toast("已复制到剪贴板")

    def _on_message_yview(self, first: str, last: str) -> None:
        self.messages._on_yview(first, last)
        button = getattr(self, "jump_latest", None)
        if button is None:
            return
        try:
            show = float(last) < .995 and bool(self.message_column.winfo_children())
            mapped = bool(button.winfo_manager())
            if show and not mapped:
                button.place(relx=.5, rely=1.0, y=-t.s(12), anchor="s")
                button.tk.call("raise", button._w)
            elif not show and mapped:
                button.place_forget()
        except (tk.TclError, TypeError, ValueError):
            pass

    def _scroll_to_latest(self) -> None:
        self._scroll_end(force=True)

    def _on_user_message_scroll(self) -> None:
        try:
            self._follow_latest = self.messages.canvas.yview()[1] >= .995
            if not self._follow_latest:
                self._scroll_follow = False
                if self._scroll_job is not None:
                    self.after_cancel(self._scroll_job)
                    self._scroll_job = None
        except tk.TclError:
            pass

    def _schedule_stream_scroll(self) -> None:
        if self._stream_scroll_job is not None:
            return
        try:
            self._stream_scroll_job = self.after(32, self._flush_stream_scroll)
        except tk.TclError:
            self._stream_scroll_job = None

    def _flush_stream_scroll(self) -> None:
        self._stream_scroll_job = None
        self._scroll_end()

    def _scroll_end(self, *, force: bool = False) -> None:
        try:
            canvas = self.messages.canvas
            _first, last = canvas.yview()
            if not (force or self._follow_latest or last >= .995):
                return
            self._follow_latest = True
            self._scroll_follow = True
            if self._scroll_job is None:
                self._scroll_job = self.after_idle(self._flush_scroll_end)
        except tk.TclError:
            pass

    def _flush_scroll_end(self) -> None:
        self._scroll_job = None
        if not self._scroll_follow:
            return
        self._scroll_follow = False
        try:
            canvas = self.messages.canvas
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.yview_moveto(1)
        except tk.TclError:
            pass

    # --------------------------------------------------------------- composer
    def _update_placeholder(self, _event=None) -> None:
        if self.input_box.get("1.0", "end-1c").strip():
            self.placeholder.place_forget()
        else:
            self.placeholder.place(x=t.s(13), y=t.s(10))
        self._auto_grow()
        self._refresh_send_state()

    def _on_input_modified(self, _event=None) -> None:
        try:
            if self.input_box.edit_modified():
                self.input_box.edit_modified(False)
                self._update_placeholder()
        except tk.TclError:
            pass

    def _auto_grow(self) -> None:
        try:
            lines = int(self.input_box.index("end-1c").split(".")[0])
        except (tk.TclError, ValueError):
            return
        height = max(2, min(7, lines))
        try:
            if int(self.input_box.cget("height")) != height:
                self.input_box.configure(height=height)
        except tk.TclError:
            pass

    def _refresh_send_state(self) -> None:
        button = getattr(self, "send_button", None)
        if button is None:
            return
        try:
            has_text = bool(self.input_box.get("1.0", "end-1c").strip())
            button.set_enabled(
                has_text and not self.streaming
                and self._queued_send is None
                and not (self._creating_session and bool(self._pending))
            )
        except tk.TclError:
            pass

    def _save_draft(self) -> None:
        if self._draft_sid is None:
            return
        try:
            draft = self.input_box.get("1.0", "end-1c")
        except tk.TclError:
            return
        if draft:
            self._drafts[self._draft_sid] = draft
        else:
            self._drafts.pop(self._draft_sid, None)

    def _restore_draft(self, sid: int) -> None:
        self._draft_sid = sid
        self.input_box.delete("1.0", "end")
        draft = self._drafts.get(sid, "")
        if draft:
            self.input_box.insert("1.0", draft)
        self._update_placeholder()

    def _on_return(self, event: tk.Event) -> str | None:
        if event.state & 0x0001:
            return None
        self.send_message()
        return "break"

    def new_chat(self) -> None:
        if self._team_context():
            self.app.toast("团队空间没有私有对话；请切回个人空间新建对话")
            return
        if self._creating_session:
            self.app.toast("正在创建对话…", duration=2200)
            return
        if self.bridge._streaming:
            self.app.toast("Venus 正在执行，请先停止", duration=2400)
            return
        if self._pending_visible:
            self._pending_visible = False
            self._clear_thread()
            self._show_empty()
        self._save_draft()
        self._queued_send = None
        self._draft_sid = None
        self.input_box.delete("1.0", "end")
        self._update_placeholder()
        self._creating_session = True
        self.bridge.create_session()

    def _select_session(self, sid: int) -> None:
        if sid == self._displayed_sid and sid == self.bridge.current_sid:
            return
        if sid not in self.bridge.sessions:
            self.app.toast("对话已不存在，请刷新列表")
            self.refresh_sidebar()
            return
        if self._creating_session:
            self.app.toast("正在创建对话，请稍候", duration=2200)
            return
        if self.bridge._streaming:
            self.app.toast("Venus 正在执行，请先停止", duration=2400)
            return
        if self._queued_send is not None and self._queued_send[0] != sid:
            self._queued_send = None
        self._save_draft()
        self._restore_draft(sid)
        self.bridge.load_session(sid)
        self._refresh_send_state()

    def open_conversation(self, index: int) -> None:
        """Compat entry for preview / debug harnesses."""
        sids = sorted(self.bridge.sessions, reverse=True)
        if sids:
            self._select_session(sids[index % len(sids)])

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
        self.agent_chip.set_text(f"{fit_text(self.fonts.small, name, t.s(170))} ▾")
        self.app.toast(f"已选择：{name}" if name == "通用智能体"
                       else f"本轮优先考虑委派给：{name}")

    def _show_mode_menu(self) -> None:
        if self._mode_menu_pending or self._mode_change_pending:
            return
        self._mode_menu_pending = True
        self.app.toast("正在读取确认模式…")
        self.bridge.submit("mode_list", lambda: self.bridge.client.get(
            "/api/v1/confirm-mode", timeout=6))

    def _open_mode_menu(self, data: dict) -> None:
        current = str(data.get("mode") or "auto")
        desc = data.get("descriptions") or {}
        cn = {"auto": "自动确认", "strict": "严格确认", "trusted": "信任模式",
              "query": "只读模式", "plan": "计划审批"}
        modes = list(data.get("modes") or [])
        if not modes:
            self.app.toast("后端未提供可用的确认模式")
            return
        items = [{"label": cn.get(str(m), str(m)), "desc": str(desc.get(m) or ""),
                  "current": str(m) == current} for m in modes]

        def choose(index: int) -> None:
            self._set_mode(modes[index])
        MenuPopup(self.mode_chip, items, self.fonts, choose, min_width=300)

    def _set_mode(self, mode: str) -> None:
        if self._mode_change_pending:
            return
        self._mode_change_pending = True
        self.app.toast("正在切换确认模式…")
        self.bridge.submit("mode_change", lambda: (mode, self.bridge.client.post(
            "/api/v1/confirm-mode", {"mode": mode}, timeout=8)))

    # ------------------------------------------------------------ empty view
    def _use_suggestion(self, prompt: str) -> None:
        draft = self.input_box.get("1.0", "end-1c").strip()
        self.input_box.delete("1.0", "end")
        self.input_box.insert("1.0", f"{draft}\n\n{prompt}" if draft else prompt)
        self._update_placeholder()
        self.input_box.focus_set()
        self.input_box.mark_set("insert", "end-1c")

    def _draw_empty(self, _event=None) -> None:
        canvas = self.empty
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        if self._empty_size == (width, height) and self._empty_draw_job is None:
            return
        if self._empty_draw_job is not None:
            canvas.after_cancel(self._empty_draw_job)
        self._empty_draw_job = canvas.after(80, self._render_empty, width, height)

    def _render_empty(self, width: int, height: int) -> None:
        self._empty_draw_job = None
        canvas = self.empty
        self._empty_size = (width, height)
        canvas.delete("decor")
        # A quiet orbital drawing gives the empty state depth without
        # competing with the welcome copy or the message composer.
        center_x, center_y = width * .84, height * .17
        scale = max(.78, min(1.24, height / 700))
        if HAS_PIL:
            scale = round(scale / .05) * .05
            photo = self._empty_cache.get(scale)
            extent = math.ceil(226 * scale) + 4
            if photo is None:
                ss = 2
                side = extent * 2
                img = Image.new("RGBA", (side * ss, side * ss), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
                center = extent * ss
                for radius, alpha in ((82, 46), (132, 34), (184, 22)):
                    r = radius * scale * ss
                    d.ellipse((center - r, center - r, center + r, center + r),
                              outline=(201, 87, 61, alpha), width=ss)
                d.arc((int(center - 220 * scale * ss),
                       int(center - 70 * scale * ss),
                       int(center + 220 * scale * ss),
                       int(center + 70 * scale * ss)),
                      start=198, end=342, fill=(133, 112, 91, 40), width=ss)
                dot_x = int(center + 116 * scale * ss)
                dot_y = int(center - 62 * scale * ss)
                d.ellipse((dot_x - 3 * ss, dot_y - 3 * ss,
                           dot_x + 3 * ss, dot_y + 3 * ss),
                          fill=(201, 87, 61, 120))
                photo = ImageTk.PhotoImage(img.resize((side, side), Image.LANCZOS))
                self._empty_cache[scale] = photo
            self._empty_photo = photo
            canvas.create_image(int(center_x) - extent, int(center_y) - extent,
                                image=photo, anchor="nw", tags="decor")
        else:
            for radius in (82, 132, 184):
                r = radius * scale
                canvas.create_oval(center_x - r, center_y - r,
                                   center_x + r, center_y + r,
                                   outline="#F1E6DD", width=1, tags="decor")
            canvas.create_arc(center_x - 220 * scale, center_y - 70 * scale,
                              center_x + 220 * scale, center_y + 70 * scale,
                              start=198, extent=144, style="arc",
                              outline="#E9DED4", width=1, tags="decor")
        self.empty_card.lift()
