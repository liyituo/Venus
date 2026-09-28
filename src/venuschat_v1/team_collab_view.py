"""Compact asynchronous team change review and submission windows."""

from __future__ import annotations

import queue
import threading
import uuid
import tkinter as tk
from tkinter import scrolledtext

from . import theme as t
from .api_client import ApiClient
from .widgets import FlatButton

_STATUS = {
    "draft": "草稿", "pending_review": "待审阅", "approved": "已批准",
    "rejected": "已拒绝", "merged": "已合并", "stale": "版本过期",
}


def is_archived_project(project: dict) -> bool:
    return str(project.get("status") or "") == "archived"


def project_writes_enabled(project: dict, *, read_only: bool = False) -> bool:
    return not read_only and str(project.get("status") or "") == "active"


class _AsyncWindow(tk.Toplevel):
    """Run HTTP requests on workers and deliver results from Tk's own queue poll."""

    def __init__(self, parent, app, fonts, client: ApiClient) -> None:
        super().__init__(parent)
        self.app = app
        self.fonts = fonts
        self.client = client
        self._results: queue.Queue = queue.Queue()
        self._poll_job = self.after(80, self._poll_results)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def request(self, tag: str, fn) -> None:
        def run() -> None:
            try:
                result = fn()
            except Exception as exc:
                result = (0, {"detail": str(exc)})
            self._results.put((tag, result))
        threading.Thread(target=run, daemon=True, name=f"venus-team-{tag}").start()

    def _poll_results(self) -> None:
        if not self.winfo_exists():
            return
        for _ in range(20):
            try:
                tag, result = self._results.get_nowait()
            except queue.Empty:
                break
            self.on_result(tag, result)
        self._poll_job = self.after(80, self._poll_results)

    def on_result(self, tag: str, result) -> None:
        raise NotImplementedError

    def destroy(self) -> None:
        try:
            if self._poll_job:
                self.after_cancel(self._poll_job)
        except tk.TclError:
            pass
        super().destroy()


class TeamCollabWindow(_AsyncWindow):
    def __init__(self, parent, app, fonts, client: ApiClient,
                 project_id: str, project_title: str = "团队项目", *,
                 read_only: bool = False) -> None:
        super().__init__(parent, app, fonts, client)
        self.project_id = project_id
        self.project_title = project_title
        self.read_only = bool(read_only)
        self._project_writes_enabled = False
        self._changes: list[dict] = []
        self._selected_change = ""
        self._current: dict = {}
        self._change_cards: list[tuple[tk.Frame, tk.Frame]] = []
        self.title("团队协作 · " + project_title)
        self.configure(bg=t.CANVAS)
        self.geometry("1080x740")
        self.minsize(860, 580)
        self.transient(parent)

        header = tk.Frame(self, bg=t.HEADER, padx=t.s(22), pady=t.s(16))
        header.pack(fill="x")
        title_row = tk.Frame(header, bg=t.HEADER)
        title_row.pack(fill="x")
        tk.Label(title_row, text=project_title, bg=t.HEADER, fg=t.INK,
                 font=fonts.display_md).pack(side="left", anchor="w")
        repo_pill = tk.Frame(title_row, bg=t.SURFACE_ALT, padx=t.s(10), pady=t.s(5))
        repo_pill.pack(side="right", anchor="e")
        self.repo_label = tk.Label(repo_pill, text="读取版本仓库…", bg=t.SURFACE_ALT,
                                   fg=t.INK_SOFT, font=fonts.caption)
        self.repo_label.pack()
        read_only_label = " · 归档只读" if self.read_only else ""
        tk.Label(header, text=f"团队版本 · {project_id}{read_only_label}", bg=t.HEADER,
                 fg=t.INK_MUTED, font=fonts.caption).pack(anchor="w", pady=(t.s(5), 0))

        self.init_frame = tk.Frame(self, bg=t.SURFACE_ALT, padx=t.s(18), pady=t.s(10))
        self.init_frame.pack(fill="x", padx=t.s(16), pady=(t.s(12), t.s(4)))
        tk.Label(self.init_frame, text="首次初始化只会导入你列出的共享文件/目录。",
                 bg=t.SURFACE_ALT, fg=t.INK_SOFT, font=fonts.caption).pack(anchor="w")
        tk.Label(self.init_frame,
                 text="填写 Hub 工作区内的相对路径，多个路径用逗号分隔（例如 src、docs/README.md）。",
                 bg=t.SURFACE_ALT, fg=t.INK_MUTED, font=fonts.caption).pack(anchor="w",
                                                                          pady=(t.s(3), 0))
        init_row = tk.Frame(self.init_frame, bg=t.SURFACE_ALT)
        init_row.pack(fill="x", pady=(t.s(6), 0))
        self.paths_var = tk.StringVar()
        self.paths_entry = tk.Entry(init_row, textvariable=self.paths_var,
                                    bg=t.SURFACE, fg=t.INK, insertbackground=t.INK,
                                    relief="flat", font=fonts.small)
        self.paths_entry.pack(side="left", fill="x", expand=True, ipady=t.s(6),
                              padx=(0, t.s(8)))
        self.init_button = FlatButton(
            init_row, "初始化版本库", self._initialize, font=fonts.caption,
            variant="primary", height=30, radius=8, parent_bg=t.SURFACE_ALT)
        self.init_button.pack(side="right")

        body = tk.Frame(self, bg=t.CANVAS, padx=t.s(16), pady=t.s(12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0, minsize=t.s(300))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        left = tk.Frame(body, bg=t.SURFACE_ALT, padx=t.s(12), pady=t.s(12))
        left.grid(row=0, column=0, sticky="nsew", padx=(0, t.s(10)))
        list_header = tk.Frame(left, bg=t.SURFACE_ALT)
        list_header.pack(fill="x", pady=(0, t.s(8)))
        tk.Label(list_header, text="项目变更", bg=t.SURFACE_ALT, fg=t.INK,
                 font=fonts.small_bold).pack(side="left")
        self.change_count = tk.Label(list_header, text="0", bg=t.SURFACE_ALT,
                                     fg=t.INK_MUTED, font=fonts.kicker)
        self.change_count.pack(side="right")
        list_surface = tk.Frame(left, bg=t.SURFACE, highlightbackground=t.LINE_FAINT,
                                highlightthickness=1)
        list_surface.pack(fill="both", expand=True)
        self.change_canvas = tk.Canvas(list_surface, bg=t.SURFACE, bd=0,
                                       highlightthickness=0)
        list_scroll = tk.Scrollbar(list_surface, orient="vertical",
                                   command=self.change_canvas.yview)
        self.change_canvas.configure(yscrollcommand=list_scroll.set)
        list_scroll.pack(side="right", fill="y")
        self.change_canvas.pack(side="left", fill="both", expand=True)
        self.change_rows_frame = tk.Frame(self.change_canvas, bg=t.SURFACE,
                                          padx=t.s(8), pady=t.s(7))
        self._change_rows_window = self.change_canvas.create_window(
            (0, 0), window=self.change_rows_frame, anchor="nw")
        self.change_rows_frame.bind("<Configure>", self._sync_change_scroll)
        self.change_canvas.bind("<Configure>", self._resize_change_rows)
        FlatButton(left, "刷新", self.refresh, font=fonts.caption,
                   variant="outline", height=28, radius=8,
                   parent_bg=t.SURFACE_ALT).pack(anchor="e", pady=(t.s(8), 0))

        right = tk.Frame(body, bg=t.SURFACE, highlightbackground=t.LINE,
                         highlightthickness=1, padx=t.s(14), pady=t.s(12))
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(3, weight=1)
        right.columnconfigure(0, weight=1)
        self.detail_label = tk.Label(right, text="选择一个变更以查看详情和差异。",
                                     bg=t.SURFACE, fg=t.INK_SOFT,
                                     justify="left", anchor="w", font=fonts.caption,
                                     wraplength=t.s(520))
        self.detail_label.grid(row=0, column=0, sticky="ew")
        right.bind("<Configure>", self._resize_detail_text, add="+")
        self.action_label = tk.Label(right, text="", bg=t.SURFACE, fg=t.INK_MUTED,
                                     font=fonts.caption, anchor="w")
        self.action_label.grid(row=1, column=0, sticky="ew", pady=(t.s(6), t.s(2)))
        diff_header = tk.Frame(right, bg=t.SURFACE)
        diff_header.grid(row=2, column=0, sticky="ew", pady=(t.s(4), t.s(5)))
        tk.Label(diff_header, text="文件差异", bg=t.SURFACE, fg=t.INK,
                 font=fonts.small_bold).pack(side="left")
        tk.Label(diff_header, text="提交 SHA 对应的审阅内容", bg=t.SURFACE,
                 fg=t.INK_FAINT, font=fonts.kicker).pack(side="right")
        diff_frame = tk.Frame(right, bg=t.CODE_SURFACE)
        diff_frame.grid(row=3, column=0, sticky="nsew")
        diff_frame.rowconfigure(0, weight=1)
        diff_frame.columnconfigure(0, weight=1)
        self.diff_yscroll = tk.Scrollbar(diff_frame, orient="vertical")
        self.diff_xscroll = tk.Scrollbar(diff_frame, orient="horizontal")
        self.diff_box = tk.Text(diff_frame, bg=t.CODE_SURFACE, fg=t.INK,
                                insertbackground=t.INK, relief="flat", wrap="none",
                                font=fonts.mono, height=20, padx=t.s(9), pady=t.s(8),
                                yscrollcommand=lambda *args: self.diff_yscroll.set(*args),
                                xscrollcommand=lambda *args: self.diff_xscroll.set(*args))
        self.diff_yscroll.configure(command=self.diff_box.yview)
        self.diff_xscroll.configure(command=self.diff_box.xview)
        self.diff_box.grid(row=0, column=0, sticky="nsew")
        self.diff_yscroll.grid(row=0, column=1, sticky="ns")
        self.diff_xscroll.grid(row=1, column=0, sticky="ew")
        self.diff_box.configure(state="disabled")
        buttons = tk.Frame(right, bg=t.SURFACE)
        buttons.grid(row=4, column=0, sticky="ew", pady=(t.s(10), 0))
        self.mutation_buttons = [
            self._button(buttons, "批准", lambda: self._review("approve"), "primary"),
            self._button(buttons, "拒绝", lambda: self._review("reject"), "danger"),
            self._button(buttons, "合并", self._merge, "soft"),
            self._button(buttons, "刷新基线", self._rebase, "outline"),
            self._button(buttons, "新建回退变更", self._revert, "outline"),
        ]
        if self.read_only:
            self._apply_read_only()
        else:
            self._apply_project_writability()
        self.refresh()

    def _sync_change_scroll(self, _event=None) -> None:
        self.change_canvas.configure(scrollregion=self.change_canvas.bbox("all"))

    def _resize_change_rows(self, event) -> None:
        self.change_canvas.itemconfigure(self._change_rows_window, width=event.width)
        for card, _accent in self._change_cards:
            children = card.winfo_children()
            if len(children) < 2:
                continue
            content = children[1]
            labels = [child for child in content.winfo_children()
                      if isinstance(child, tk.Label)]
            if labels:
                labels[0].configure(wraplength=max(t.s(150), event.width - t.s(42)))

    def _resize_detail_text(self, event) -> None:
        self.detail_label.configure(wraplength=max(t.s(220), event.width - t.s(48)))

    def _select_change(self, index: int) -> None:
        if index < 0 or index >= len(self._changes):
            return
        for row_index, (card, accent) in enumerate(self._change_cards):
            selected = row_index == index
            card.configure(bg=t.ACTIVE if selected else t.SURFACE,
                           highlightbackground=t.TERRACOTTA if selected else t.LINE_FAINT)
            for child in card.winfo_children():
                if child is accent:
                    continue
                if isinstance(child, tk.Frame):
                    child.configure(bg=t.ACTIVE if selected else t.SURFACE)
                    for label in child.winfo_children():
                        if isinstance(label, tk.Label):
                            label.configure(bg=t.ACTIVE if selected else t.SURFACE)
                elif isinstance(child, tk.Label):
                    child.configure(bg=t.ACTIVE if selected else t.SURFACE)
            accent.configure(bg=t.TERRACOTTA if selected else t.SURFACE)
        item = self._changes[index]
        self._selected_change = str(item.get("id") or "")
        change_id = self._selected_change
        self.request("detail", lambda cid=change_id: self.client.get(
            f"/api/v1/projects/{self.project_id}/changes/{cid}", timeout=15))
        self.request("diff", lambda cid=change_id: self.client.get(
            f"/api/v1/projects/{self.project_id}/changes/{cid}/diff", timeout=15))

    def _button(self, parent, text, command, variant) -> FlatButton:
        button = FlatButton(parent, text, command, font=self.fonts.caption,
                            variant=variant, height=30, radius=8,
                            parent_bg=t.SURFACE)
        button.pack(side="left", padx=(0, t.s(6)))
        return button

    def _apply_read_only(self) -> None:
        self.read_only = True
        self.init_frame.pack_forget()
        self._apply_project_writability()
        self.action_label.configure(text="项目已归档：审计与版本历史只读。",
                                    fg=t.INK_MUTED)

    def _apply_project_writability(self) -> None:
        enabled = self._project_writes_enabled and not self.read_only
        self.paths_entry.configure(state="normal" if enabled else "disabled")
        self.init_button.set_enabled(enabled)
        for button in self.mutation_buttons:
            button.set_enabled(enabled)

    def _initialize(self) -> None:
        if not self._project_writes_enabled or self.read_only:
            self.app.toast("项目状态尚未确认或已归档，当前不能初始化版本库")
            return
        raw = self.paths_var.get().strip()
        if not raw or raw.startswith("例如 "):
            self.app.toast("请填写共享文件或目录的相对路径")
            return
        paths = [part.strip() for part in raw.replace(";", ",").replace("\n", ",").split(",")
                 if part.strip()]
        self.action_label.configure(text="正在按指定路径初始化…")
        self.request("init", lambda: self.client.post(
            f"/api/v1/projects/{self.project_id}/versions/init", {"shared_paths": paths},
            timeout=30))

    def refresh(self) -> None:
        self._project_writes_enabled = False
        self._apply_project_writability()
        self.request("project", lambda: self.client.get(
            f"/api/v1/projects/{self.project_id}", timeout=10))
        self.request("status", lambda: self.client.get(
            f"/api/v1/projects/{self.project_id}/versions", timeout=15))
        self.request("changes", lambda: self.client.get(
            f"/api/v1/projects/{self.project_id}/changes?limit=100", timeout=15))

    def _review(self, decision: str) -> None:
        if not self._project_writes_enabled or self.read_only:
            self.app.toast("项目状态尚未确认或已归档，当前不能审阅变更")
            return
        if not self._selected_change:
            self.app.toast("请先选择变更")
            return
        change_id = self._selected_change
        reviewed_sha = str(self._current.get("current_sha") or "")
        self.request("action", lambda: self.client.post(
            f"/api/v1/projects/{self.project_id}/changes/{change_id}/review",
            {"decision": decision, "reviewed_sha": reviewed_sha}, timeout=20))

    def _merge(self) -> None:
        self._action("merge")

    def _rebase(self) -> None:
        self._action("rebase")

    def _revert(self) -> None:
        self._action("revert", {"request_id": f"gui-{uuid.uuid4().hex}"})

    def _action(self, action: str, body: dict | None = None) -> None:
        if not self._project_writes_enabled or self.read_only:
            self.app.toast("项目状态尚未确认或已归档，当前不能执行协作操作")
            return
        if not self._selected_change:
            self.app.toast("请先选择变更")
            return
        change_id = self._selected_change
        self.request("action", lambda: self.client.post(
            f"/api/v1/projects/{self.project_id}/changes/{change_id}/{action}",
            body, timeout=30))

    def on_result(self, tag: str, result) -> None:
        code, data = result if isinstance(result, tuple) else (0, {})
        data = data if isinstance(data, dict) else {"detail": str(data)}
        if tag == "project":
            if code == 200:
                project = data.get("project") or data
                if is_archived_project(project) and not self.read_only:
                    self._apply_read_only()
                    self.title("团队协作 · " + self.project_title + " · 只读归档")
                self._project_writes_enabled = project_writes_enabled(
                    project, read_only=self.read_only)
                self._apply_project_writability()
                if self.read_only:
                    self.action_label.configure(text="项目已归档：审计与版本历史只读。",
                                                fg=t.INK_MUTED)
                elif not self._project_writes_enabled:
                    self.action_label.configure(text="当前项目状态仅允许查看版本与历史。",
                                                fg=t.WARNING)
            else:
                self._project_writes_enabled = False
                self._apply_project_writability()
                self.action_label.configure(
                    text=f"读取项目状态失败：{data.get('detail', code)}",
                    fg=t.WARNING if self.read_only else t.DANGER)
        elif tag == "status":
            if code == 200:
                initialized = bool(data.get("initialized"))
                sha = str(data.get("head") or "")
                roots = ", ".join(data.get("shared_paths") or []) or "未设置"
                self.repo_label.configure(
                    text=(f"版本库已初始化 · {sha[:12]} · 共享范围：{roots}"
                          if initialized else "尚未初始化版本库"),
                    fg=t.SUCCESS if initialized else t.WARNING)
                if self.read_only:
                    self.init_frame.pack_forget()
                elif initialized:
                    self.init_frame.pack_forget()
                else:
                    self.init_frame.pack(fill="x", padx=t.s(16), pady=(t.s(12), t.s(4)))
            else:
                self.repo_label.configure(text=f"读取失败：{data.get('detail', code)}",
                                          fg=t.DANGER)
        elif tag == "changes":
            if code != 200:
                self.action_label.configure(text=f"读取变更失败：{data.get('detail', code)}",
                                            fg=t.DANGER)
                return
            self._changes = list(data.get("changes") or [])
            self.change_count.configure(text=str(len(self._changes)))
            for child in self.change_rows_frame.winfo_children():
                child.destroy()
            self._change_cards = []
            for index, item in enumerate(self._changes):
                status_key = str(item.get("status") or "")
                status = _STATUS.get(status_key, status_key or "未知")
                purpose = str(item.get("purpose") or "（未填写目的）")
                author = str(item.get("author_name") or item.get("author_id") or "成员")
                card = tk.Frame(self.change_rows_frame, bg=t.SURFACE,
                                highlightbackground=t.LINE_FAINT, highlightthickness=1,
                                cursor="hand2")
                card.pack(fill="x", pady=(0, t.s(7)))
                accent = tk.Frame(card, bg=t.SURFACE, width=t.s(3))
                accent.pack(side="left", fill="y")
                content = tk.Frame(card, bg=t.SURFACE, padx=t.s(9), pady=t.s(8))
                content.pack(side="left", fill="both", expand=True)
                topline = tk.Frame(content, bg=t.SURFACE)
                topline.pack(fill="x")
                status_color = (t.SUCCESS if status_key in ("approved", "merged")
                                else t.WARNING if status_key in ("pending_review", "stale")
                                else t.DANGER if status_key == "rejected" else t.INK_MUTED)
                tk.Label(topline, text=status, bg=t.SURFACE, fg=status_color,
                         font=self.fonts.kicker).pack(side="left")
                tk.Label(topline, text=author, bg=t.SURFACE, fg=t.INK_FAINT,
                         font=self.fonts.kicker).pack(side="right")
                tk.Label(content, text=purpose, bg=t.SURFACE, fg=t.INK_SOFT,
                         font=self.fonts.caption, anchor="w", justify="left",
                         wraplength=t.s(250)).pack(fill="x", anchor="w", pady=(t.s(4), 0))
                tk.Label(content, text=str(item.get("id") or ""), bg=t.SURFACE,
                         fg=t.INK_FAINT, font=self.fonts.kicker,
                         anchor="w").pack(fill="x", anchor="w", pady=(t.s(4), 0))
                self._change_cards.append((card, accent))
                for widget in (card, accent, content, topline, *content.winfo_children(),
                               *topline.winfo_children()):
                    widget.bind("<Button-1>", lambda _event, i=index: self._select_change(i))
            if self._changes:
                index = next((i for i, row in enumerate(self._changes)
                              if row.get("id") == self._selected_change), 0)
                self._select_change(index)
            else:
                self._selected_change = ""
                self._current = {}
                self.detail_label.configure(text="暂无团队变更。完成团队任务后，可从任务面板提交变更。")
                self._set_diff("")
        elif tag == "detail":
            if code == 200:
                self._current = data.get("change") or {}
                c = self._current
                reviews = c.get("reviews") or []
                reviewers = ", ".join(
                    f"{r.get('reviewer_name') or r.get('reviewer_id')}："
                    f"{'批准' if r.get('decision') == 'approve' else '拒绝'}"
                    f"（{str(r.get('reviewed_sha') or '')[:12]}"
                    f"{'，已失效' if r.get('invalidated') else ''}）"
                    for r in reviews)
                merge = c.get("merge") or {}
                merged_by = str(merge.get("actor_name") or merge.get("actor_id") or "")
                merge_line = (f"\n合并人：{merged_by}    合并 SHA：{merge.get('sha') or '—'}"
                              if merged_by else "")
                error_line = (f"\n冲突提示：{c.get('error')}" if c.get("error") else "")
                self.detail_label.configure(text=(
                    f"目的：{c.get('purpose') or '—'}\n"
                    f"任务：{c.get('job_id') or '—'}    作者：{c.get('author_name') or c.get('author_id')}\n"
                    f"状态：{_STATUS.get(str(c.get('status') or ''), c.get('status') or '')}"
                    f"    有效批准：{c.get('approval_count', 0)}/{c.get('required_approvals', 1)}"
                    f"    文件：{', '.join(c.get('modified_files') or []) or '—'}\n"
                    f"基础 SHA：{c.get('base_sha') or '—'}\n"
                    f"当前 SHA：{c.get('current_sha') or '—'}\n"
                    f"审阅：{reviewers or '尚无审阅记录'}{merge_line}{error_line}"))
            else:
                self.action_label.configure(text=f"详情读取失败：{data.get('detail', code)}",
                                            fg=t.DANGER)
        elif tag == "diff":
            if code == 200:
                diff = str(data.get("diff") or "（该变更没有文件差异）")
                if data.get("truncated"):
                    diff += "\n\n（差异超过显示上限，当前内容已截断）"
                self._set_diff(diff)
            else:
                self._set_diff(f"读取差异失败：{data.get('detail', code)}")
        elif tag == "init":
            if code == 200:
                self.app.toast("团队版本库已初始化")
                self.action_label.configure(text="")
                self.refresh()
            else:
                self.action_label.configure(text=f"初始化失败：{data.get('detail', code)}",
                                            fg=t.DANGER)
                self.app.toast(f"初始化失败：{data.get('detail', code)}")
        elif tag == "action":
            if code == 200:
                msg = str(data.get("message") or "协作操作已完成")
                self.app.toast(msg)
                self.action_label.configure(text=msg, fg=t.SUCCESS)
                self.refresh()
            else:
                msg = str(data.get("detail") or f"HTTP {code}")
                self.action_label.configure(text=msg, fg=t.DANGER)
                self.app.toast(msg, duration=4200)

    def _set_diff(self, value: str) -> None:
        self.diff_box.configure(state="normal")
        self.diff_box.delete("1.0", "end")
        self.diff_box.insert("1.0", value)
        self.diff_box.configure(state="disabled")


class JobChangeSubmitWindow(_AsyncWindow):
    def __init__(self, parent, app, fonts, client: ApiClient, job: dict) -> None:
        super().__init__(parent, app, fonts, client)
        self.job = job
        self.job_id = str(job.get("id") or "")
        self.project_id = str(job.get("project_id") or "")
        self._project_writes_enabled = False
        self.request_id = f"gui-{uuid.uuid4().hex}"
        self.title("提交团队变更")
        self.configure(bg=t.HEADER)
        self.geometry("660x520")
        self.minsize(560, 420)
        self.transient(parent)
        card = tk.Frame(self, bg=t.HEADER, padx=t.s(18), pady=t.s(16))
        card.pack(fill="both", expand=True)
        tk.Label(card, text="提交团队变更", bg=t.HEADER, fg=t.INK,
                 font=fonts.display_md).pack(anchor="w")
        tk.Label(card, text=f"任务：{job.get('title') or self.job_id}\n项目：{self.project_id}",
                 bg=t.HEADER, fg=t.INK_MUTED, justify="left",
                 font=fonts.caption).pack(anchor="w", pady=(t.s(5), t.s(12)))
        tk.Label(card, text="变更目的", bg=t.HEADER, fg=t.INK_SOFT,
                 font=fonts.caption).pack(anchor="w")
        self.purpose = tk.Entry(card, bg=t.SURFACE, fg=t.INK,
                                insertbackground=t.INK, relief="flat", font=fonts.body)
        self.purpose.pack(fill="x", ipady=t.s(6), pady=(t.s(4), t.s(10)))
        path_head = tk.Frame(card, bg=t.HEADER)
        path_head.pack(fill="x")
        tk.Label(path_head, text="提交路径（每行一个）", bg=t.HEADER, fg=t.INK_SOFT,
                 font=fonts.caption).pack(side="left")
        FlatButton(path_head, "读取任务改动", self._load_paths, font=fonts.caption,
                   variant="outline", height=28, radius=8,
                   parent_bg=t.HEADER).pack(side="right")
        self.paths = scrolledtext.ScrolledText(
            card, bg=t.SURFACE, fg=t.INK, insertbackground=t.INK,
            relief="flat", font=fonts.mono, height=10, wrap="none",
            padx=t.s(8), pady=t.s(7))
        self.paths.pack(fill="both", expand=True, pady=(t.s(5), t.s(10)))
        self.feedback = tk.Label(card, text="提交只会包含这里列出的路径。",
                                 bg=t.HEADER, fg=t.INK_MUTED,
                                 font=fonts.caption, anchor="w")
        self.feedback.pack(fill="x")
        buttons = tk.Frame(card, bg=t.HEADER)
        buttons.pack(fill="x", pady=(t.s(10), 0))
        FlatButton(buttons, "取消", self.destroy, font=fonts.small,
                   variant="ghost", height=32, parent_bg=t.HEADER).pack(side="right")
        self.submit_button = FlatButton(buttons, "提交审阅", self._submit,
                                        font=fonts.small_bold, variant="primary",
                                        height=32, parent_bg=t.HEADER)
        self.submit_button.pack(side="right", padx=(0, t.s(8)))
        self.submit_button.set_enabled(False)
        if self.project_id:
            self.request("project", lambda: self.client.get(
                f"/api/v1/projects/{self.project_id}", timeout=10))
        else:
            self.feedback.configure(text="任务没有关联项目，不能提交团队变更。",
                                    fg=t.WARNING)
        self._load_paths()

    def _load_paths(self) -> None:
        self.feedback.configure(text="正在读取任务改动…", fg=t.INK_MUTED)
        self.request("workspace", lambda: self.client.get(
            f"/api/v1/jobs/{self.job_id}/workspace", timeout=15))

    def _submit(self) -> None:
        if not self._project_writes_enabled:
            self.app.toast("项目状态尚未确认或已归档，当前不能提交变更")
            return
        purpose = self.purpose.get().strip()
        paths = [line.strip() for line in self.paths.get("1.0", "end").splitlines()
                 if line.strip()]
        if not purpose:
            self.app.toast("请填写变更目的")
            return
        if not paths:
            self.app.toast("请列出要提交的文件路径")
            return
        self.feedback.configure(text="正在提交指定路径并生成提交 SHA…", fg=t.INK_MUTED)
        self.request("submit", lambda: self.client.post(
            f"/api/v1/jobs/{self.job_id}/changes/commit",
            {"paths": paths, "purpose": purpose,
             "request_id": self.request_id}, timeout=30))

    def on_result(self, tag: str, result) -> None:
        code, data = result if isinstance(result, tuple) else (0, {})
        data = data if isinstance(data, dict) else {"detail": str(data)}
        if tag == "project":
            if code == 200:
                project = data.get("project") or data
                self._project_writes_enabled = project_writes_enabled(project)
                self.submit_button.set_enabled(self._project_writes_enabled)
                if self._project_writes_enabled:
                    self.feedback.configure(text="项目仍处于活动状态；提交只会包含这里列出的路径。",
                                            fg=t.INK_MUTED)
                else:
                    self.feedback.configure(text="项目状态仅允许查看；归档项目不能提交变更。",
                                            fg=t.WARNING)
            else:
                self._project_writes_enabled = False
                self.submit_button.set_enabled(False)
                self.feedback.configure(
                    text=f"无法确认项目状态，暂时不能提交：{data.get('detail', code)}",
                    fg=t.WARNING)
        elif tag == "workspace":
            if code == 200:
                files = list(data.get("files") or [])
                if files:
                    self.paths.delete("1.0", "end")
                    self.paths.insert("1.0", "\n".join(files))
                    self.feedback.configure(text=f"读取到 {len(files)} 个改动路径；提交前可编辑。",
                                            fg=t.SUCCESS)
                else:
                    self.feedback.configure(text="任务当前没有可提交的共享文件改动。",
                                            fg=t.WARNING)
            else:
                self.feedback.configure(text=f"读取失败：{data.get('detail', code)}",
                                        fg=t.DANGER)
        elif tag == "submit":
            if code == 200:
                change = data.get("change") or {}
                self.app.toast("变更已提交审阅 · " + str(change.get("current_sha") or "")[:12])
                self.destroy()
                TeamCollabWindow(self.master, self.app, self.fonts, self.client,
                                 self.project_id, self.project_id)
            else:
                msg = str(data.get("detail") or f"HTTP {code}")
                self.feedback.configure(text=msg, fg=t.DANGER)
                self.app.toast(f"提交失败：{msg}", duration=4200)


def open_team_collab(parent, app, fonts, client: ApiClient,
                     project_id: str, project_title: str = "团队项目", *,
                     read_only: bool = False) -> TeamCollabWindow:
    return TeamCollabWindow(parent, app, fonts, client, project_id, project_title,
                            read_only=read_only)


def open_job_change_submit(parent, app, fonts, client: ApiClient,
                           job: dict) -> JobChangeSubmitWindow:
    return JobChangeSubmitWindow(parent, app, fonts, client, job)
