"""Project-scoped team task, review, and member workspace."""

from __future__ import annotations

import tkinter as tk

from . import theme as t
from .config_store import team_connection
from .team_collab_view import open_job_change_submit
from .widgets import FlatButton, ScrollArea, SelectField, separator
from .workspace_state import (
    can_cancel_team_job,
    can_dispatch_team_task,
    can_review_change,
    can_submit_change,
    project_allows_dispatch,
    project_status_label,
    response_is_current,
    selected_change_after_refresh,
    should_poll_team_jobs,
    team_workspace_available,
    team_jobs_for_project,
    team_projects,
)


_JOB_STATUS = {
    "queued": "排队中", "running": "进行中", "waiting_confirm": "等待确认",
    "completed": "已完成", "failed": "失败", "cancelled": "已取消",
}
_CHANGE_STATUS = {
    "draft": "草稿", "pending_review": "待审阅", "approved": "已通过",
    "rejected": "已拒绝", "merged": "已合并", "stale": "基线过期",
}


class TeamWorkspaceView(tk.Frame):
    """A two-column project workbench; team content never renders as chat."""

    NAV = (("tasks", "任务"), ("changes", "版本与审阅"), ("members", "成员"))

    def __init__(self, parent: tk.Misc, app, fonts, bridge) -> None:
        super().__init__(parent, bg=t.CANVAS)
        self.app = app
        self.fonts = fonts
        self.bridge = bridge
        self.page = "tasks"
        self._filter = "all"
        self._team_jobs: list[dict] = []
        self._selected_job: dict | None = None
        self._selected_change = ""
        self._changes: list[dict] = []
        self._change: dict = {}
        self._diff = ""
        self._members: list[dict] = []
        self._identity: dict = {}
        self._identity_request_pending = False
        self._project_detail: dict = {}
        self._versions: dict = {}
        self._request_generation = 0
        self._switch_pending = False
        self._service_reachable = False
        self._identity_verified = False
        self._model_ready = False
        self._project_options: dict[str, str] = {}
        self._setting_project_values = False
        self._dispatch_drafts: dict[str, str] = {}
        self._dispatch_project_id = ""
        self._dispatch_pending_project = ""
        self._confirm_lookup_job_id = ""
        self._jobs_refresh_job: str | None = None
        self._page_scroll_positions: dict[str, float] = {}
        self._rendered_page = ""
        self._queue_content: tk.Frame | None = None
        self._worker_windows: dict[tuple[str, str], tk.Toplevel] = {}
        self._build()

    @property
    def connection_record(self) -> dict | None:
        try:
            return team_connection(self.bridge.client.base)
        except ValueError:
            return None

    @property
    def team_connection(self) -> dict | None:
        row = self.connection_record
        return row if row and row.get("status") == "joined" else None

    @property
    def connected(self) -> bool:
        return self.team_connection is not None

    @property
    def api_available(self) -> bool:
        return team_workspace_available(
            connected=self.connected,
            service_reachable=self._service_reachable,
            identity_verified=self._identity_verified,
        )

    @property
    def project(self) -> dict | None:
        pid = str(self.bridge.active_project_id or "")
        return next((row for row in team_projects(self.bridge.projects)
                     if str(row.get("id") or "") == pid), None)

    @property
    def project_id(self) -> str:
        project = self.project
        return str(project.get("id") or "") if project else ""

    def _build(self) -> None:
        self.columnconfigure(0, weight=0, minsize=t.s(264))
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self.sidebar = tk.Frame(self, bg=t.SIDEBAR, width=t.s(264))
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        separator(self, vertical=True, color=t.LINE).grid(row=0, column=0, sticky="nse")

        tk.Label(self.sidebar, text="团队空间", bg=t.SIDEBAR, fg=t.INK,
                 font=self.fonts.display_md).pack(anchor="w", padx=t.s(20),
                                                  pady=(t.s(20), t.s(4)))
        self.team_label = tk.Label(self.sidebar, text="未连接 Hub", bg=t.SIDEBAR,
                                   fg=t.INK_MUTED, font=self.fonts.caption,
                                   anchor="w", justify="left", wraplength=t.s(222))
        self.team_label.pack(fill="x", padx=t.s(20), pady=(0, t.s(13)))
        tk.Label(self.sidebar, text="当前项目", bg=t.SIDEBAR, fg=t.INK_SOFT,
                 font=self.fonts.small_bold).pack(anchor="w", padx=t.s(20),
                                                   pady=(0, t.s(5)))
        self.project_picker = SelectField(self.sidebar, ["未选择项目"],
                                          font=self.fonts.small,
                                          value="未选择项目", bg=t.SURFACE)
        self.project_picker.pack(fill="x", padx=t.s(16))
        self.project_picker.variable.trace_add("write", self._project_choice_changed)
        self.project_meta = tk.Label(self.sidebar, text="", bg=t.SIDEBAR,
                                     fg=t.INK_MUTED, font=self.fonts.caption,
                                     anchor="w", justify="left", wraplength=t.s(224))
        self.project_meta.pack(fill="x", padx=t.s(20), pady=(t.s(7), t.s(15)))
        separator(self.sidebar, color=t.LINE_FAINT).pack(fill="x", padx=t.s(16),
                                                          pady=(0, t.s(9)))

        self.nav_buttons: dict[str, FlatButton] = {}
        for key, label in self.NAV:
            button = FlatButton(self.sidebar, label,
                                lambda page=key: self.show_page(page),
                                font=self.fonts.small_bold, variant="ghost",
                                height=38, radius=9, anchor="w", padx=14,
                                parent_bg=t.SIDEBAR)
            button.pack(fill="x", padx=t.s(12), pady=t.s(2))
            self.nav_buttons[key] = button

        tk.Frame(self.sidebar, bg=t.SIDEBAR).pack(fill="both", expand=True)
        separator(self.sidebar, color=t.LINE_FAINT).pack(fill="x", padx=t.s(16),
                                                          pady=(0, t.s(9)))
        FlatButton(self.sidebar, "项目管理", self._open_project_manager,
                   font=self.fonts.caption, variant="outline", height=34,
                   parent_bg=t.SIDEBAR).pack(fill="x", padx=t.s(16), pady=(0, t.s(6)))
        FlatButton(self.sidebar, "团队连接与成员审批",
                   lambda: self.app.show_settings_page("team"),
                   font=self.fonts.caption, variant="ghost", height=32,
                   parent_bg=t.SIDEBAR).pack(fill="x", padx=t.s(16), pady=(0, t.s(14)))

        self.main = tk.Frame(self, bg=t.CANVAS)
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.columnconfigure(0, weight=1)
        self.main.rowconfigure(1, weight=1)
        self.heading = tk.Frame(self.main, bg=t.CANVAS, height=t.s(90))
        self.heading.grid(row=0, column=0, sticky="ew", padx=(t.s(28), t.s(25)))
        self.heading.grid_propagate(False)
        self.page_title = tk.Label(self.heading, text="团队任务", bg=t.CANVAS,
                                   fg=t.INK, font=self.fonts.display_lg)
        self.page_title.pack(anchor="w", pady=(t.s(15), 0))
        self.page_subtitle = tk.Label(self.heading, text="项目任务与执行进度",
                                      bg=t.CANVAS, fg=t.INK_MUTED,
                                      font=self.fonts.small)
        self.page_subtitle.pack(anchor="w", pady=(t.s(3), 0))
        self.page_scroll = ScrollArea(self.main, bg=t.CANVAS, scrollbar=True)
        self.page_scroll.grid(row=1, column=0, sticky="nsew",
                              padx=(t.s(22), t.s(16)), pady=(0, t.s(12)))
        self.bind("<Configure>", self._resize_text, add="+")
        self._paint_nav()
        self._refresh_identity_label()
        self._render_page()

    def _resize_text(self, _event=None) -> None:
        try:
            width = max(t.s(280), self.winfo_width() - t.s(330))
            self._wrapwidth = width
        except tk.TclError:
            return

    def _paint_nav(self) -> None:
        for key, button in self.nav_buttons.items():
            button.set_active(key == self.page)

    def _refresh_identity_label(self) -> None:
        row = self.connection_record
        if not row:
            self.team_label.configure(text="尚未连接团队 Hub", fg=t.WARNING)
            return
        status = str(row.get("status") or "")
        if status == "pending":
            self.team_label.configure(text="团队入队申请待审批", fg=t.WARNING)
            return
        if status == "approved":
            self.team_label.configure(text="已批准 · 等待领取设备凭证", fg=t.SUCCESS)
            return
        if status in {"revoked", "invalidated", "rejected"}:
            self.team_label.configure(text="团队设备凭证不可用", fg=t.WARNING)
            return
        if status != "joined":
            self.team_label.configure(text="Hub 已保存 · 尚未加入团队", fg=t.WARNING)
            return
        name = str(row.get("team_name") or row.get("name") or "团队 Hub")
        if not self._identity_verified:
            state = "Hub 在线 · 正在核验身份" if self._service_reachable else "正在核验 Hub 与设备身份"
            self.team_label.configure(text=f"{name} · {state}", fg=t.WARNING)
        elif not self._service_reachable:
            self.team_label.configure(text=f"{name} · 身份已核验 · Hub 暂不可达",
                                      fg=t.WARNING)
        else:
            self.team_label.configure(text=f"{name} · 身份已核验 · Hub 在线",
                                      fg=t.SUCCESS)

    def stop_jobs_polling(self) -> None:
        job = self._jobs_refresh_job
        self._jobs_refresh_job = None
        if job is not None:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass

    def _tasks_page_visible(self) -> bool:
        return bool(self.page == "tasks"
                    and getattr(self.app, "_current_space", "") == "team"
                    and getattr(self.app, "_current_view", "") == "team")

    def _schedule_jobs_refresh(self) -> None:
        active = any(str(row.get("status") or "") in
                     {"queued", "running", "waiting_confirm"}
                     for row in self._visible_jobs())
        should_poll = should_poll_team_jobs(
            api_available=self.api_available,
            tasks_page_visible=self._tasks_page_visible(),
            has_active_jobs=active,
        )
        if not should_poll:
            self.stop_jobs_polling()
            return
        if self._jobs_refresh_job is not None:
            return
        try:
            self._jobs_refresh_job = self.after(5000, self._request_jobs_refresh)
        except tk.TclError:
            self._jobs_refresh_job = None

    def _request_jobs_refresh(self) -> None:
        self._jobs_refresh_job = None
        if not self.api_available or not self._tasks_page_visible():
            return
        self.bridge.submit("jobs", lambda: ("ok", self.bridge.client.get(
            "/api/v1/jobs?limit=200&scope=team", timeout=10)))

    def show_page(self, page: str) -> None:
        if page not in {key for key, _label in self.NAV}:
            return
        if page != "tasks":
            self.stop_jobs_polling()
        self.page = page
        self._paint_nav()
        titles = {
            "tasks": ("团队任务", "派发、跟踪并提交当前项目的任务变更"),
            "changes": ("版本与审阅", "核对提交 SHA、文件差异和审批票数"),
            "members": ("项目成员", "查看项目成员、角色与获准终端"),
        }
        self.page_title.configure(text=titles[page][0])
        self.page_subtitle.configure(text=titles[page][1])
        self._render_page()
        if self.connected and self.project_id:
            self._load_page_data()
        if page == "tasks":
            self._schedule_jobs_refresh()

    def activate(self) -> None:
        """Refresh after a backend switch or a visit from another workspace."""
        self.stop_jobs_polling()
        self._confirm_lookup_job_id = ""
        self._refresh_identity_label()
        self._switch_pending = False
        self._team_jobs = []
        self._selected_job = None
        self._changes = []
        self._change = {}
        self._diff = ""
        self._members = []
        self._identity = {}
        self._identity_request_pending = False
        self.bridge.projects = []
        self._service_reachable = False
        self._identity_verified = False
        self._model_ready = False
        if self.connected:
            self.request_identity()
            self._sync_projects()
        self._render_page()

    def reset_context(self) -> None:
        """Drop visible state immediately when the backend origin changes."""
        self.stop_jobs_polling()
        self._request_generation += 1
        self._team_jobs = []
        self._selected_job = None
        self._changes = []
        self._selected_change = ""
        self._change = {}
        self._diff = ""
        self._members = []
        self._identity = {}
        self._identity_request_pending = False
        self._versions = {}
        self._project_detail = {}
        self._switch_pending = False
        self._service_reachable = False
        self._identity_verified = False
        self._model_ready = False
        self._dispatch_drafts.clear()
        self._dispatch_project_id = ""
        self._dispatch_pending_project = ""
        self._confirm_lookup_job_id = ""
        self._refresh_identity_label()
        self._sync_projects()
        self._render_page()

    def begin_project_switch(self, project_id: str) -> None:
        self.stop_jobs_polling()
        self._switch_pending = True
        self._confirm_lookup_job_id = ""
        self._request_generation += 1
        self._team_jobs = []
        self._selected_job = None
        self._changes = []
        self._selected_change = ""
        self._change = {}
        self._diff = ""
        self._members = []
        self.project_meta.configure(text="Hub 正在确认当前项目…")
        self._render_page()

    def finish_project_switch(self, success: bool) -> None:
        self._switch_pending = False
        self._sync_projects()
        if success:
            self._load_page_data()
            if self.api_available:
                self.bridge.submit("jobs", lambda: ("ok", self.bridge.client.get(
                    "/api/v1/jobs?limit=200&scope=team", timeout=10)))
        else:
            self.project_meta.configure(text="项目切换失败，仍使用原项目。")
            self._load_page_data()
        self._render_page()

    def on_projects(self, code: int = 200, data: dict | None = None) -> None:
        if not self.connected or code != 200:
            self.stop_jobs_polling()
            self._service_reachable = False
            if self.connected:
                self.bridge.projects = []
            self._team_jobs = []
            self._changes = []
            self._members = []
        else:
            self._service_reachable = True
        self.app._refresh_space_navigation()
        self._sync_projects()
        self._render_page()
        if self.api_available and self.project_id:
            self._load_page_data()

    def _sync_projects(self) -> None:
        self._refresh_identity_label()
        projects = team_projects(self.bridge.projects) if self.api_available else []
        self._project_options = {}
        values: list[str] = []
        for row in projects:
            name = str(row.get("title") or row.get("name") or row.get("id") or "项目")
            status = project_status_label(row)
            option = f"{name} · {status}"
            if option in values:
                option = f"{option} · {row.get('id')}"
            values.append(option)
            self._project_options[option] = str(row.get("id") or "")
        current = self.project if self.api_available else None
        selected = next((label for label, pid in self._project_options.items()
                         if current and pid == str(current.get("id") or "")), "")
        if not selected:
            selected = "选择或加入项目" if self.connected else "未连接团队 Hub"
        self._setting_project_values = True
        try:
            self.project_picker.set_values(values)
            self.project_picker.variable.set(selected)
        finally:
            self._setting_project_values = False
        if self._switch_pending:
            self.project_meta.configure(text="正在等待 Hub 确认项目切换。")
        elif current:
            role = str(current.get("role") or "项目成员")
            self.project_meta.configure(
                text=f"{role} · {project_status_label(current)}")
        elif not self.connected:
            self.project_meta.configure(text="连接 Hub 并通过身份核验后，才能查看团队项目。")
        elif not self.api_available:
            self.project_meta.configure(text="正在核验 Hub 服务与团队身份…")
        elif projects:
            self.project_meta.configure(text="选择一个已授权项目，或打开项目管理加入项目。")
        else:
            self.project_meta.configure(text="已连接团队 Hub；当前没有可访问项目。")

    def _project_choice_changed(self, *_args) -> None:
        if self._setting_project_values or self._switch_pending:
            return
        project_id = self._project_options.get(self.project_picker.get())
        if project_id and project_id != self.project_id:
            selected = next((row for row in team_projects(self.bridge.projects)
                             if str(row.get("id") or "") == project_id), None)
            if selected and selected.get("status") == "pending_claim":
                self.app.toast("该项目待认领；请在项目管理中完成认领后再切换")
                self._sync_projects()
                return
            self.app.set_active_project(project_id)

    def _open_project_manager(self) -> None:
        if not self.connected:
            self.app.show_settings_page("team")
            return
        from .project_hub_view import open_project_hub
        open_project_hub(self, self.app, self.fonts, self.bridge.client)

    def _load_page_data(self) -> None:
        if not self.api_available or not self.project_id or self._switch_pending:
            return
        if self.page == "tasks":
            return
        self._request_generation += 1
        generation = self._request_generation
        pid = self.project_id
        if self.page == "changes":
            self._request("project", generation, pid, lambda: self.bridge.client.get(
                f"/api/v1/projects/{pid}", timeout=10))
            self._request("versions", generation, pid, lambda: self.bridge.client.get(
                f"/api/v1/projects/{pid}/versions", timeout=15))
            self._request("changes", generation, pid, lambda: self.bridge.client.get(
                f"/api/v1/projects/{pid}/changes?limit=100", timeout=15))
        elif self.page == "members":
            self._request("project", generation, pid, lambda: self.bridge.client.get(
                f"/api/v1/projects/{pid}", timeout=10))
            self._request("members", generation, pid, lambda: self.bridge.client.get(
                f"/api/v1/projects/{pid}/members", timeout=12))

    def _request(self, name: str, generation: int, project_id: str, fn) -> None:
        def run():
            try:
                result = fn()
                code, data = (result if isinstance(result, tuple) and len(result) == 2
                              else (0, {}))
            except Exception as exc:
                code, data = 0, {"detail": str(exc)}
            body = dict(data) if isinstance(data, dict) else {"detail": str(data)}
            body["_workspace_generation"] = generation
            body["_workspace_project_id"] = project_id
            return ("ok", (code, body))
        self.bridge.submit(f"team_workspace_{name}", run)

    def request_identity(self) -> None:
        if not self.connected or self._identity_request_pending:
            return
        self._identity_request_pending = True
        self._request("identity", 0, "",
                      lambda: self.bridge.client.get("/api/v1/team/me", timeout=8))

    def _refresh_team_lists(self) -> None:
        if not self.api_available:
            return
        self.bridge.submit("projects", lambda: ("ok", self.bridge.client.get(
            "/api/v1/projects", timeout=8)))
        self.bridge.submit("jobs", lambda: ("ok", self.bridge.client.get(
            "/api/v1/jobs?limit=200&scope=team", timeout=10)))

    def on_health(self, data: dict, *, reachable: bool = True) -> None:
        was_available = self.api_available
        if self.connected:
            self._service_reachable = bool(reachable)
        self._model_ready = bool(reachable and data.get("configured"))
        self._refresh_identity_label()
        self.app._refresh_space_navigation()
        available = self.api_available
        if self.connected and reachable and not self._identity_verified:
            # A failed first identity check must recover when the Hub comes
            # back, without queuing duplicate checks during normal startup.
            self.request_identity()
        if was_available != available:
            if not available:
                self.stop_jobs_polling()
                self._render_page()
            else:
                # A project/jobs request may have failed while the team identity
                # remained valid. Reload those lists when Hub reachability returns.
                self._refresh_team_lists()
        else:
            self._refresh_dispatch_controls()

    def on_jobs(self, jobs: list[dict]) -> None:
        previous_jobs = tuple(self._job_display_key(row) for row in self._team_jobs)
        previous_selected = self._job_display_key(self._selected_job)
        if not self.api_available:
            self._team_jobs = []
            self._selected_job = None
        else:
            rows = team_jobs_for_project(jobs or [], self.project_id)
            self._team_jobs = rows
            if self._selected_job:
                selected_id = str(self._selected_job.get("id") or "")
                self._selected_job = next((row for row in rows
                                          if str(row.get("id") or "") == selected_id), None)
        # Polling continues for active tasks, but an unchanged response must
        # not destroy and rebuild the queue (which flickers and steals focus).
        current_jobs = tuple(self._job_display_key(row) for row in self._team_jobs)
        if self.page == "tasks" and (current_jobs != previous_jobs
                                     or self._job_display_key(self._selected_job)
                                     != previous_selected):
            self._render_task_queue()
        self._schedule_jobs_refresh()

    @staticmethod
    def _job_display_key(job: dict | None) -> tuple | None:
        if not isinstance(job, dict):
            return None
        progress = job.get("progress") or {}
        if not isinstance(progress, dict):
            progress = {}
        # Only fields rendered in the queue affect repainting. A backend
        # heartbeat or non-visible progress metadata must leave widgets alone.
        return tuple(job.get(key) for key in (
            "id", "status", "title", "owner_name", "owner", "created_at",
            "created", "is_mine", "visibility", "project_id", "change_id",
            "result", "output", "result_summary", "error", "detail",
        )) + (progress.get("tool_calls"),)

    def on_dispatch(self, success: bool, detail: str = "",
                    project_id: str = "") -> None:
        target_project = str(self._dispatch_pending_project or project_id or self.project_id)
        is_current_project = target_project == self.project_id
        if self._dispatch_pending_project == target_project:
            self._dispatch_pending_project = ""
        if success:
            self._dispatch_drafts.pop(target_project, None)
            if is_current_project:
                entry = getattr(self, "dispatch_entry", None)
                status = getattr(self, "dispatch_status", None)
                try:
                    if entry is not None and entry.winfo_exists():
                        entry.delete("1.0", "end")
                    if status is not None and status.winfo_exists():
                        status.configure(text="任务已派发，正在刷新项目队列。",
                                         fg=t.SUCCESS)
                except tk.TclError:
                    pass
            if self.api_available and is_current_project:
                self.bridge.submit("jobs", lambda: ("ok", self.bridge.client.get(
                    "/api/v1/jobs?limit=200&scope=team", timeout=10)))
        else:
            if is_current_project:
                status = getattr(self, "dispatch_status", None)
                try:
                    if status is not None and status.winfo_exists():
                        status.configure(text=f"派发失败：{detail or '请重试'}",
                                         fg=t.DANGER)
                except tk.TclError:
                    pass
        self._refresh_dispatch_controls()

    def handle_backend(self, kind: str, payload) -> None:
        if kind == "worker_error":
            source, detail = payload
            self.handle_backend(str(source), ("error", str(detail)))
            return
        code, data = self.app._api_result(payload)
        generation = int(data.get("_workspace_generation") or 0)
        pid = str(data.get("_workspace_project_id") or "")
        change_id = str(data.get("_workspace_change_id") or "")
        if not response_is_current(
                project_id=pid, active_project_id=self.project_id,
                generation=generation, current_generation=self._request_generation,
                change_id=change_id, selected_change_id=self._selected_change):
            return
        if kind == "team_workspace_identity":
            self._identity_request_pending = False
            if code == 200:
                self._identity = dict(data)
                self._identity_verified = True
                self._service_reachable = True
            else:
                self.stop_jobs_polling()
                self._identity = {}
                self._identity_verified = False
                self._service_reachable = code != 0
                self.bridge.projects = []
                self.bridge.active_project_id = ""
                self._request_generation += 1
                self._team_jobs = []
                self._selected_job = None
                self._changes = []
                self._selected_change = ""
                self._change = {}
                self._diff = ""
                self._members = []
                self._versions = {}
                self._project_detail = {}
            self.app._refresh_space_navigation()
            if code == 200:
                self._refresh_team_lists()
            else:
                self._render_page()
        elif kind == "team_workspace_project":
            if code == 200:
                self._project_detail = data.get("project") or data
                if self.page == "changes":
                    self._refresh_changes_page()
                elif self.page == "members":
                    self._refresh_members_page()
            else:
                self._set_status_message(f"项目状态读取失败：{data.get('detail', code)}")
        elif kind == "team_workspace_versions":
            if code == 200:
                self._versions = dict(data)
            else:
                self._versions = {"error": str(data.get("detail") or code)}
            if self.page == "changes":
                self._refresh_changes_page()
        elif kind == "team_workspace_changes":
            if code == 200:
                self._changes = list(data.get("changes") or [])
                selected = selected_change_after_refresh(
                    self._changes, self._selected_change)
                if selected != self._selected_change:
                    self._change = {}
                    self._diff = ""
                self._selected_change = selected
                if self._selected_change:
                    # Reload details and diff so approval counts reflect other reviewers.
                    self.select_change(self._selected_change)
                else:
                    self._refresh_changes_page()
            else:
                self._changes = []
                self._set_status_message(f"变更读取失败：{data.get('detail', code)}")
                self._refresh_changes_page()
        elif kind == "team_workspace_change_detail":
            if code == 200:
                self._change = data.get("change") or {}
            else:
                self._change = {"error": str(data.get("detail") or code)}
            self._refresh_changes_page()
        elif kind == "team_workspace_diff":
            self._diff = (str(data.get("diff") or "（该变更没有文件差异）")
                          if code == 200 else
                          f"读取差异失败：{data.get('detail', code)}")
            if data.get("truncated"):
                self._diff += "\n\n（差异超过显示上限，当前内容已截断）"
            self._refresh_changes_page()
        elif kind == "team_workspace_members":
            self._members = list(data.get("members") or []) if code == 200 else []
            self._refresh_members_page(error="" if code == 200 else
                                       str(data.get("detail") or code))
        elif kind == "team_workspace_action":
            if code == 200:
                self.app.toast(str(data.get("message") or "协作操作已完成"))
                self._load_page_data()
            else:
                self.app.toast(str(data.get("detail") or f"HTTP {code}"), duration=4200)
                self._set_status_message(str(data.get("detail") or f"HTTP {code}"), danger=True)
        elif kind == "team_workspace_job_confirmation":
            job_id = str(data.get("_workspace_job_id") or "")
            if self._confirm_lookup_job_id == job_id:
                self._confirm_lookup_job_id = ""
            if (self.page != "tasks" or not self._selected_job
                    or str(self._selected_job.get("id") or "") != job_id):
                return
            ask = data.get("ask") or {}
            if code != 200 or not isinstance(ask, dict) or not ask.get("id"):
                self.app.toast(str(data.get("detail") or "确认已结束，请刷新任务状态"),
                               duration=4200)
                self._refresh_jobs()
                return
            from .confirm_dialog import show_confirm
            show_confirm(
                self.app.root, ask,
                lambda allowed, request_id, jid=job_id, project_id=pid,
                       request_generation=generation: self._answer_job_confirmation(
                           allowed, request_id, jid, project_id, request_generation),
                self.fonts,
            )
        elif kind == "team_workspace_confirm_vote":
            if code == 200:
                if data.get("pending"):
                    message = (f"确认票已记录：{data.get('yes_votes', 0)}/"
                               f"{data.get('required', 1)}，等待其他成员")
                elif data.get("choice") == "no":
                    message = "已拒绝这次操作，任务将继续处理拒绝结果"
                else:
                    message = "已允许这次操作，任务继续执行"
                self.app.toast(message)
            else:
                self.app.toast(str(data.get("detail") or "确认已超时或任务已结束"),
                               duration=4200)
            self._refresh_jobs()

    def _render_page(self) -> None:
        inner = self.page_scroll.inner
        previous_page = self._rendered_page or self.page
        try:
            self._page_scroll_positions[previous_page] = float(
                self.page_scroll.canvas.yview()[0])
        except (tk.TclError, ValueError, IndexError):
            pass
        old_entry = getattr(self, "dispatch_entry", None)
        if old_entry is not None and self._dispatch_project_id:
            try:
                if old_entry.winfo_exists():
                    self._dispatch_drafts[self._dispatch_project_id] = old_entry.get(
                        "1.0", "end-1c")
            except tk.TclError:
                pass
        for child in inner.winfo_children():
            child.destroy()
        self._queue_content = None
        self._rendered_page = self.page
        self._refresh_identity_label()
        self._sync_projects()
        if not self.connected:
            self._render_unconnected(inner)
            return
        if not self.api_available:
            self._render_unavailable(inner)
            return
        if self._switch_pending:
            self._message_card(inner, "正在切换项目",
                               "旧项目的任务、变更和成员信息已清空。等待 Hub 确认后再显示新项目。")
            return
        if not team_projects(self.bridge.projects):
            self._message_card(
                inner, "团队 Hub 已连接，当前没有可访问项目",
                "加入 Hub 和加入项目是两个步骤。打开项目管理创建项目、接受邀请，或认领待处理项目。",
                action=("打开项目管理", self._open_project_manager))
            return
        project = self.project
        if not project:
            self._message_card(
                inner, "选择一个团队项目",
                "团队任务、变更审阅和成员授权都按项目隔离。请从左侧选择一个已授权项目。",
                action=("打开项目管理", self._open_project_manager))
            return
        self._project_context_card(inner, project)
        if str(project.get("status") or "") == "pending_claim":
            self._message_card(inner, "项目等待负责人认领",
                               "该项目在认领完成前不能派发团队任务或初始化版本库。",
                               action=("前往项目管理认领", self._open_project_manager))
            return
        if self.page == "tasks":
            self._render_tasks(inner, project)
        elif self.page == "changes":
            self._render_changes(inner, project)
        else:
            self._render_members(inner, project)
        fraction = self._page_scroll_positions.get(self.page, 0.0)
        try:
            self.page_scroll.after_idle(
                lambda value=fraction: self.page_scroll.canvas.yview_moveto(value))
        except tk.TclError:
            pass

    def _render_unconnected(self, parent: tk.Misc) -> None:
        record = self.connection_record or {}
        state = str(record.get("status") or "")
        if state == "pending":
            title = "团队入队申请待审批"
            body = "管理员批准后，请在团队设置中领取此设备的凭证。此状态下不会显示团队任务或成员数据。"
            action = "检查申请状态"
        elif state == "approved":
            title = "申请已批准，等待领取设备凭证"
            body = "前往团队设置领取本设备凭证并完成身份核验。领取前不会显示团队任务或成员数据。"
            action = "领取设备凭证"
        elif state in {"revoked", "invalidated", "rejected"}:
            title = "团队设备凭证不可用"
            body = "当前设备不能访问 Hub。请在团队设置检查状态，或联系管理员重新邀请。"
            action = "检查团队连接"
        else:
            title = "团队 Hub 尚未连接"
            body = "连接并通过 Hub 身份核验后，团队任务、项目版本和成员信息会显示在这里。团队空间不包含私人对话。"
            action = "连接团队 Hub"
        card = self._card(parent, pady=(t.s(22), 0), padx=t.s(22))
        tk.Label(card, text=title, bg=t.SURFACE, fg=t.INK,
                 font=self.fonts.heading2).pack(anchor="w")
        tk.Label(card, text=body,
                 bg=t.SURFACE, fg=t.INK_MUTED, font=self.fonts.body,
                 justify="left", anchor="w", wraplength=t.s(690)).pack(
                     anchor="w", fill="x", pady=(t.s(9), t.s(16)))
        FlatButton(card, action, lambda: self.app.show_settings_page("team"),
                   font=self.fonts.small_bold, variant="primary", height=38,
                   parent_bg=t.SURFACE).pack(anchor="w")

    def _render_unavailable(self, parent: tk.Misc) -> None:
        card = self._card(parent, pady=(t.s(22), 0), padx=t.s(22))
        title = ("团队 Hub 暂不可达" if not self._service_reachable
                 else "团队 Hub 尚未通过身份核验")
        body = ("当前无法连接团队 Hub。检查网络或 Hub 状态后再刷新。"
                if not self._service_reachable else
                "项目任务、变更和成员数据需要通过团队设备身份核验后才能显示。请检查设备凭证并刷新状态。")
        tk.Label(card, text=title, bg=t.SURFACE,
                 fg=t.INK, font=self.fonts.heading2).pack(anchor="w")
        tk.Label(card,
                 text=body,
                 bg=t.SURFACE, fg=t.INK_MUTED, font=self.fonts.body,
                 anchor="w", justify="left", wraplength=t.s(690)).pack(
                     fill="x", pady=(t.s(8), t.s(14)))
        FlatButton(card, "检查团队连接", lambda: self.app.show_settings_page("team"),
                   font=self.fonts.small_bold, variant="primary", height=36,
                   parent_bg=t.SURFACE).pack(anchor="w")

    def _project_context_card(self, parent: tk.Misc, project: dict) -> None:
        row = self._card(parent, padx=t.s(15), pady=(0, t.s(10)))
        top = tk.Frame(row, bg=t.SURFACE)
        top.pack(fill="x")
        title = str(project.get("title") or project.get("name") or project.get("id"))
        tk.Label(top, text=title, bg=t.SURFACE, fg=t.INK,
                 font=self.fonts.heading3).pack(side="left")
        status = project_status_label(project)
        color = t.WARNING if project.get("status") in {"pending_claim", "archived"} else t.SUCCESS
        tk.Label(top, text=status, bg=t.SURFACE, fg=color,
                 font=self.fonts.caption).pack(side="right")
        role = str(project.get("role") or "项目成员")
        team = self.team_connection or {}
        hub = str(team.get("team_name") or team.get("name") or "团队 Hub")
        tk.Label(row, text=f"{hub} · {role} · 项目 ID：{project.get('id')}",
                 bg=t.SURFACE, fg=t.INK_MUTED,
                 font=self.fonts.caption).pack(anchor="w", pady=(t.s(5), 0))

    def _render_tasks(self, parent: tk.Misc, project: dict) -> None:
        compose = self._card(parent, padx=t.s(16), pady=(0, t.s(12)))
        tk.Label(compose, text="派发团队任务", bg=t.SURFACE, fg=t.INK,
                 font=self.fonts.heading3).pack(anchor="w")
        tk.Label(compose, text="项目成员可见 · 后台执行 · 归属当前项目",
                 bg=t.SURFACE, fg=t.INK_MUTED, font=self.fonts.caption).pack(
                     anchor="w", pady=(t.s(4), t.s(8)))
        self.dispatch_entry = tk.Text(
            compose, height=4, wrap="word", bg=t.SURFACE_ALT, fg=t.INK,
            insertbackground=t.INK, relief="flat", font=self.fonts.body,
            padx=t.s(10), pady=t.s(8), highlightthickness=1,
            highlightbackground=t.LINE, highlightcolor=t.TERRACOTTA)
        self.dispatch_entry.pack(fill="x")
        self.dispatch_entry.bind("<Control-Return>", self._dispatch_shortcut, add="+")
        self._dispatch_project_id = self.project_id
        draft = self._dispatch_drafts.get(self.project_id, "")
        if draft:
            self.dispatch_entry.insert("1.0", draft)
        self.dispatch_status = tk.Label(compose, text="", bg=t.SURFACE,
                                        fg=t.INK_MUTED, font=self.fonts.caption,
                                        anchor="w")
        self.dispatch_status.pack(fill="x", pady=(t.s(5), 0))
        actions = tk.Frame(compose, bg=t.SURFACE)
        actions.pack(fill="x", pady=(t.s(7), 0))
        self.dispatch_button = FlatButton(
            actions, "派发到此项目", self._dispatch_task,
            font=self.fonts.small_bold, variant="primary", height=36,
            parent_bg=t.SURFACE)
        self.dispatch_button.pack(side="left")
        self.dispatch_availability = tk.Label(
            actions, text="", bg=t.SURFACE, fg=t.WARNING,
            font=self.fonts.caption, anchor="w")
        self.dispatch_availability.pack(side="left", padx=t.s(10))
        self._refresh_dispatch_controls()

        queue_card = self._card(parent, padx=t.s(15), pady=(0, t.s(10)))
        head = tk.Frame(queue_card, bg=t.SURFACE)
        head.pack(fill="x", pady=(0, t.s(8)))
        tk.Label(head, text="当前项目任务", bg=t.SURFACE, fg=t.INK,
                 font=self.fonts.heading3).pack(side="left")
        FlatButton(head, "刷新", self._refresh_jobs, font=self.fonts.caption,
                   variant="outline", height=28, parent_bg=t.SURFACE).pack(side="right")
        self._queue_content = tk.Frame(queue_card, bg=t.SURFACE)
        self._queue_content.pack(fill="x")
        self._render_task_queue()

        worker_card = self._card(parent, padx=t.s(15), pady=(0, t.s(10)))
        tk.Label(worker_card, text="Worker 队列", bg=t.SURFACE, fg=t.INK,
                 font=self.fonts.heading3).pack(anchor="w")
        tk.Label(worker_card,
                 text="Worker 派发与审批使用独立队列，状态不代表上方的 Agent 任务正在运行。",
                 bg=t.SURFACE, fg=t.INK_MUTED, font=self.fonts.caption,
                 anchor="w", justify="left", wraplength=getattr(
                     self, "_wrapwidth", t.s(720))).pack(fill="x", pady=(t.s(4), t.s(8)))
        worker_actions = tk.Frame(worker_card, bg=t.SURFACE)
        worker_actions.pack(fill="x")
        worker_enabled = project_allows_dispatch(project)
        tasks_button = FlatButton(worker_actions, "Worker 任务与审批",
                                  self._open_worker_tasks,
                                  font=self.fonts.caption, variant="outline",
                                  height=30, parent_bg=t.SURFACE)
        tasks_button.pack(side="left", padx=(0, t.s(6)))
        tasks_button.set_enabled(worker_enabled)
        control_button = FlatButton(worker_actions, "本机 Worker 授权",
                                    self._open_worker_control,
                                    font=self.fonts.caption, variant="ghost",
                                    height=30, parent_bg=t.SURFACE)
        control_button.pack(side="left")
        control_button.set_enabled(worker_enabled)
        if not worker_enabled:
            tk.Label(worker_actions, text="项目只读或未就绪，Worker 派发已停用。",
                     bg=t.SURFACE, fg=t.WARNING, font=self.fonts.caption).pack(
                         side="left", padx=(t.s(8), 0))

    def _refresh_dispatch_controls(self) -> None:
        button = getattr(self, "dispatch_button", None)
        if button is None:
            return
        project = self.project
        pending = bool(self._dispatch_pending_project)
        allowed = can_dispatch_team_task(
            project=project,
            api_available=self.api_available,
            model_ready=self._model_ready,
            dispatch_in_flight=pending,
        )
        try:
            button.set_enabled(allowed)
            label = getattr(self, "dispatch_availability", None)
            if label is not None and label.winfo_exists():
                if pending:
                    hint = "正在派发团队任务…"
                elif not project_allows_dispatch(project):
                    hint = "项目只读或状态未就绪，当前不能派发。"
                elif not self._model_ready:
                    hint = "Hub 在线 · 模型未就绪，暂不能派发任务。"
                else:
                    hint = ""
                label.configure(text=hint)
        except (AttributeError, tk.TclError):
            pass

    def _render_task_queue(self) -> None:
        container = getattr(self, "_queue_content", None)
        if container is None:
            return
        try:
            if not container.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            top = float(self.page_scroll.canvas.yview()[0])
        except (tk.TclError, ValueError, IndexError):
            top = 0.0
        for child in container.winfo_children():
            child.destroy()
        filter_row = tk.Frame(container, bg=t.SURFACE)
        filter_row.pack(fill="x", pady=(0, t.s(8)))
        labels = (("all", "全部"), ("mine", "我发起的"), ("active", "进行中"))
        for key, label in labels:
            button = FlatButton(filter_row, label,
                                lambda item=key: self._set_filter(item),
                                font=self.fonts.caption,
                                variant="soft" if key == self._filter else "ghost",
                                height=27, padx=10, parent_bg=t.SURFACE)
            button.pack(side="left", padx=(0, t.s(4)))
        rows = self._visible_jobs()
        if not rows:
            text = ("暂无符合条件的团队任务。" if self._team_jobs else
                    "当前项目还没有团队任务。派发后，任务状态和结果会显示在这里。")
            tk.Label(container, text=text, bg=t.SURFACE, fg=t.INK_FAINT,
                     font=self.fonts.body, anchor="w", justify="left").pack(
                         fill="x", pady=(t.s(4), t.s(10)))
        else:
            waiting = [item for item in rows
                       if str(item.get("status") or "") == "waiting_confirm"]
            if waiting:
                notice = tk.Frame(container, bg=t.SURFACE_ALT, padx=t.s(10),
                                  pady=t.s(7))
                notice.pack(fill="x", pady=(0, t.s(7)))
                tk.Label(notice, text="有团队任务等待操作确认，请及时查看并决定。",
                         bg=t.SURFACE_ALT, fg=t.WARNING,
                         font=self.fonts.caption).pack(side="left", fill="x", expand=True)
                FlatButton(notice, "查看待确认任务",
                           lambda item=dict(waiting[0]): self._select_job(item),
                           font=self.fonts.caption, variant="soft", height=28,
                           parent_bg=t.SURFACE_ALT).pack(side="right")
            for item in rows:
                self._render_job_card(container, item)
        if self._selected_job:
            self._render_job_detail(container, self._selected_job,
                                    self.project or {})
        try:
            self.page_scroll.after_idle(
                lambda fraction=top: self.page_scroll.canvas.yview_moveto(fraction))
        except tk.TclError:
            pass

    def _open_worker_tasks(self) -> None:
        self._open_worker_window("tasks")

    def _open_worker_control(self) -> None:
        self._open_worker_window("control")

    def _open_worker_window(self, kind: str) -> None:
        project = self.project
        if not self.api_available or not project_allows_dispatch(project):
            self.app.toast("请先选择一个已连接且可派发的团队项目")
            return
        project_id = str(project.get("id") or "")
        key = (kind, project_id)
        existing = self._worker_windows.get(key)
        try:
            if existing is not None and existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                return
        except tk.TclError:
            pass
        if kind == "tasks":
            from .worker_task_view import open_worker_tasks
            window = open_worker_tasks(self, self.fonts, self.bridge.client, project)
        else:
            from .worker_control_view import open_worker_control
            window = open_worker_control(
                self, self.fonts, origin=self.bridge.client.base, project=project)
        self._worker_windows[key] = window

    def _visible_jobs(self) -> list[dict]:
        rows = team_jobs_for_project(self._team_jobs, self.project_id)
        if self._filter == "mine":
            rows = [row for row in rows if row.get("is_mine")]
        elif self._filter == "active":
            rows = [row for row in rows
                    if str(row.get("status") or "") in
                    {"queued", "running", "waiting_confirm"}]
        return rows

    def _set_filter(self, value: str) -> None:
        self._filter = value
        self._render_task_queue()

    def _render_job_card(self, parent: tk.Misc, job: dict) -> None:
        row = tk.Frame(parent, bg=t.SURFACE_ALT, highlightbackground=t.LINE_FAINT,
                       highlightthickness=1, padx=t.s(10), pady=t.s(8), cursor="hand2")
        row.pack(fill="x", pady=(0, t.s(6)))
        title = str(job.get("title") or "团队任务")
        status_key = str(job.get("status") or "queued")
        owner = str(job.get("owner_name") or job.get("owner") or "成员")
        created = str(job.get("created_at") or job.get("created") or "")
        project_name = str(self.project.get("title") or self.project.get("name") or "当前项目")
        first = tk.Frame(row, bg=t.SURFACE_ALT)
        first.pack(fill="x")
        tk.Label(first, text=title, bg=t.SURFACE_ALT, fg=t.INK,
                 font=self.fonts.small_bold, anchor="w").pack(side="left", fill="x", expand=True)
        color = t.SUCCESS if status_key == "completed" else (
            t.DANGER if status_key == "failed" else t.TERRACOTTA)
        tk.Label(first, text=_JOB_STATUS.get(status_key, status_key),
                 bg=t.SURFACE_ALT, fg=color, font=self.fonts.caption).pack(side="right")
        tk.Label(row, text=f"发起人：{owner} · 项目：{project_name}"
                 + (f" · {created}" if created else ""),
                 bg=t.SURFACE_ALT, fg=t.INK_MUTED,
                 font=self.fonts.caption, anchor="w").pack(fill="x", pady=(t.s(4), 0))
        for widget in (row, first, *row.winfo_children(), *first.winfo_children()):
            widget.bind("<Button-1>", lambda _event, item=dict(job): self._select_job(item), add="+")

    def _select_job(self, job: dict) -> None:
        if str((self._selected_job or {}).get("id") or "") != str(job.get("id") or ""):
            self._confirm_lookup_job_id = ""
        self._selected_job = job
        self._render_task_queue()

    def _render_job_detail(self, parent: tk.Misc, job: dict, project: dict) -> None:
        detail = self._card(parent, padx=t.s(12), pady=(t.s(9), 0), bg=t.SURFACE_ALT)
        tk.Label(detail, text="任务详情", bg=t.SURFACE_ALT, fg=t.INK,
                 font=self.fonts.small_bold).pack(anchor="w")
        status = str(job.get("status") or "queued")
        progress = job.get("progress") or {}
        content = str(job.get("result") or job.get("output")
                      or job.get("result_summary") or "").strip()
        error = str(job.get("error") or job.get("detail") or "").strip()
        lines = [f"状态：{_JOB_STATUS.get(status, status)}"]
        if progress:
            tools = progress.get("tool_calls")
            if tools is not None:
                lines.append(f"工具调用：{tools}")
        if content:
            lines.append(f"结果：{content}")
        if error:
            lines.append(f"错误：{error}")
        tk.Label(detail, text="\n".join(lines), bg=t.SURFACE_ALT, fg=t.INK_SOFT,
                 font=self.fonts.caption, anchor="w", justify="left",
                 wraplength=getattr(self, "_wrapwidth", t.s(720))).pack(fill="x", pady=(t.s(5), 0))
        actions = tk.Frame(detail, bg=t.SURFACE_ALT)
        actions.pack(fill="x", pady=(t.s(7), 0))
        if status == "waiting_confirm":
            FlatButton(actions, "处理待确认操作",
                       lambda jid=job.get("id"): self._open_job_confirmation(jid),
                       font=self.fonts.caption, variant="primary", height=28,
                       parent_bg=t.SURFACE_ALT).pack(side="left", padx=(0, t.s(6)))
        if status in {"queued", "running", "waiting_confirm"}:
            if can_cancel_team_job(job, str(project.get("role") or "")):
                FlatButton(actions, "取消任务", lambda jid=job.get("id"): self._cancel_job(jid),
                           font=self.fonts.caption, variant="outline", height=28,
                           parent_bg=t.SURFACE_ALT).pack(side="left")
            else:
                tk.Label(actions, text="仅任务发起人或项目 owner/admin 可以取消",
                         bg=t.SURFACE_ALT, fg=t.INK_FAINT,
                         font=self.fonts.caption).pack(side="left")
        if can_submit_change(job, project_id=self.project_id,
                             current_user_id=self._current_user_id()):
            FlatButton(actions, "提交变更", lambda item=dict(job): self._submit_change(item),
                       font=self.fonts.caption, variant="soft", height=28,
                       parent_bg=t.SURFACE_ALT).pack(side="left", padx=(t.s(6), 0))
        else:
            if status != "completed":
                reason = "任务完成后才能提交变更"
            elif not job.get("is_mine"):
                reason = "仅任务发起人可以提交自己的任务变更"
            elif not job.get("change_id"):
                reason = "任务没有生成可提交的文件变更"
            else:
                reason = "当前任务不符合变更提交条件"
            tk.Label(actions, text=reason, bg=t.SURFACE_ALT, fg=t.INK_FAINT,
                     font=self.fonts.caption).pack(side="left", padx=(t.s(8), 0))

    def _dispatch_shortcut(self, _event=None) -> str:
        self._dispatch_task()
        return "break"

    def _dispatch_task(self) -> None:
        project = self.project
        if not self.api_available:
            self.app.toast("当前项目不可派发；请确认 Hub 连接和项目状态")
            return
        if not self._model_ready:
            self.app.toast("Hub 在线但模型未就绪，暂不能派发团队任务")
            return
        if self._dispatch_pending_project:
            self.app.toast("上一条团队派发仍在提交，请等待结果后再试")
            return
        if not project_allows_dispatch(project):
            self.app.toast("当前项目只读或尚未就绪，不能派发团队任务")
            return
        text = self.dispatch_entry.get("1.0", "end-1c").strip()
        if not text:
            self.dispatch_status.configure(text="请填写团队任务内容。", fg=t.WARNING)
            self.dispatch_entry.focus_set()
            return
        self.dispatch_status.configure(text="正在派发到当前项目…", fg=t.INK_MUTED)
        self._dispatch_pending_project = self.project_id
        self._refresh_dispatch_controls()
        self.bridge.dispatch(text, None, agent_preference="")

    def _refresh_jobs(self) -> None:
        if self.api_available:
            self.bridge.submit("jobs", lambda: ("ok", self.bridge.client.get(
                "/api/v1/jobs?limit=200&scope=team", timeout=10)))

    def _open_job_confirmation(self, job_id) -> None:
        job_id = str(job_id or "")
        selected = self._selected_job or {}
        if (not self.api_available or not job_id
                or str(selected.get("id") or "") != job_id
                or str(selected.get("status") or "") != "waiting_confirm"):
            self.app.toast("此任务已不再等待确认，请刷新任务状态")
            return
        if self._confirm_lookup_job_id == job_id:
            return
        pid = self.project_id
        self._confirm_lookup_job_id = job_id

        def load_confirmation():
            def result(code, body):
                return code, {**(body if isinstance(body, dict) else
                                 {"detail": str(body)}), "_workspace_job_id": job_id}

            code, data = self.bridge.client.get("/api/v1/confirm/pending", timeout=8)
            if code != 200:
                return result(code, data)
            code, detail = self.bridge.client.get(f"/api/v1/jobs/{job_id}", timeout=8)
            if code != 200:
                return result(code, detail)
            job = detail.get("job") or {}
            ask = job.get("pending_ask") or {}
            request_id = str(job.get("confirm_request_id") or "")
            eligible = any(
                str(row.get("request_id") or "") == request_id
                and str(row.get("task_id") or "") == str(job.get("task_id") or "")
                for row in data.get("pending") or []
            )
            if (str(job.get("project_id") or "") != pid
                    or str(job.get("status") or "") != "waiting_confirm"
                    or not request_id or not eligible
                    or not isinstance(ask, dict)
                    or str(ask.get("id") or "") != request_id):
                return result(409, {"detail": "确认已超时、已投票，或任务不再等待确认"})
            return result(200, {"ask": ask})

        self._request("job_confirmation", self._request_generation, pid,
                      load_confirmation)

    def _answer_job_confirmation(self, allowed: bool, request_id: str,
                                 job_id: str, project_id: str,
                                 generation: int) -> None:
        if (not self.api_available or project_id != self.project_id
                or generation != self._request_generation
                or str((self._selected_job or {}).get("id") or "") != job_id):
            self.app.toast("项目或任务已切换，请重新打开待确认操作")
            return
        self._request("confirm_vote", generation, project_id,
                      lambda: self.bridge.client.post("/api/v1/agent/respond", {
                          "request_id": request_id,
                          "choice": "yes" if allowed else "no",
                      }, timeout=10))

    def _cancel_job(self, job_id) -> None:
        job = next((row for row in self._team_jobs
                    if str(row.get("id") or "") == str(job_id or "")), None)
        if not can_cancel_team_job(job, str((self.project or {}).get("role") or "")):
            self.app.toast("仅任务发起人或项目 owner/admin 可以取消此任务")
            return
        if job_id and self.api_available:
            self.bridge.submit("job_cancel", lambda: self.bridge.client.post(
                f"/api/v1/jobs/{job_id}/cancel", {}, timeout=12))

    def _submit_change(self, job: dict) -> None:
        if can_submit_change(job, project_id=self.project_id,
                             current_user_id=self._current_user_id()):
            open_job_change_submit(self, self.app, self.fonts, self.bridge.client, job)
        else:
            self.app.toast("只有当前用户已完成且包含文件变更的团队任务可以提交审阅")

    def _current_user_id(self) -> str:
        user = self._identity.get("user") or self._identity.get("member") or {}
        return str(user.get("id") or self._identity.get("user_id") or "")

    def _render_changes(self, parent: tk.Misc, project: dict) -> None:
        status = str(project.get("status") or "active")
        banner = self._card(parent, padx=t.s(13), pady=(0, t.s(10)), bg=t.SURFACE_ALT)
        if self._versions.get("error"):
            message = f"读取版本库失败：{self._versions['error']}"
            color = t.DANGER
        elif not self._versions:
            message = "正在读取共享版本库状态…"
            color = t.INK_MUTED
        elif not self._versions.get("initialized"):
            message = "版本库尚未初始化。先列出要纳入版本审阅的共享路径。"
            color = t.WARNING
        else:
            head = str(self._versions.get("head") or "—")
            paths = ", ".join(self._versions.get("shared_paths") or []) or "未设置"
            message = f"版本库已初始化 · 当前 SHA {head[:12]} · 共享范围：{paths}"
            color = t.SUCCESS
        if status == "archived":
            message += "\n项目已归档，版本历史只读。"
        tk.Label(banner, text=message, bg=t.SURFACE_ALT, fg=color,
                 font=self.fonts.caption, anchor="w", justify="left",
                 wraplength=getattr(self, "_wrapwidth", t.s(720))).pack(fill="x")
        role = str(project.get("role") or "")
        may_initialize = role in {"owner", "admin"}
        if (self._versions and not self._versions.get("initialized")
                and status == "active" and may_initialize):
            init = self._card(parent, padx=t.s(13), pady=(0, t.s(10)))
            tk.Label(init, text="初始化共享版本库", bg=t.SURFACE, fg=t.INK,
                     font=self.fonts.small_bold).pack(anchor="w")
            tk.Label(init, text="输入 Hub 工作区内的相对路径，多个路径用逗号分隔。",
                     bg=t.SURFACE, fg=t.INK_MUTED,
                     font=self.fonts.caption).pack(anchor="w", pady=(t.s(4), t.s(6)))
            self.init_paths = tk.Entry(init, bg=t.SURFACE_ALT, fg=t.INK,
                                       insertbackground=t.INK, relief="flat",
                                       font=self.fonts.body)
            self.init_paths.pack(fill="x", ipady=t.s(6))
            FlatButton(init, "初始化版本库", self._initialize_versions,
                       font=self.fonts.caption, variant="primary", height=30,
                       parent_bg=t.SURFACE).pack(anchor="e", pady=(t.s(7), 0))
        elif (self._versions and not self._versions.get("initialized")
              and status == "active" and not may_initialize):
            tk.Label(parent, text="版本库尚未初始化；请项目 owner 或 admin 完成初始化。",
                     bg=t.CANVAS, fg=t.WARNING, font=self.fonts.caption,
                     anchor="w").pack(fill="x", pady=(0, t.s(9)))
        list_card = self._card(parent, padx=t.s(13), pady=(0, t.s(10)))
        head = tk.Frame(list_card, bg=t.SURFACE)
        head.pack(fill="x")
        tk.Label(head, text="项目变更", bg=t.SURFACE, fg=t.INK,
                 font=self.fonts.heading3).pack(side="left")
        tk.Label(head, text=f"{len(self._changes)} 项", bg=t.SURFACE,
                 fg=t.INK_MUTED, font=self.fonts.caption).pack(side="left", padx=(t.s(8), 0))
        FlatButton(head, "刷新", self._load_page_data, font=self.fonts.caption,
                   variant="outline", height=28, parent_bg=t.SURFACE).pack(side="right")
        if not self._changes:
            text = ("暂无团队变更。完成团队任务后，在任务详情选择「提交变更」。"
                    if self._versions.get("initialized") else
                    "初始化版本库后，任务产生的变更会在这里等待审阅。")
            tk.Label(list_card, text=text, bg=t.SURFACE, fg=t.INK_FAINT,
                     font=self.fonts.body, anchor="w", justify="left").pack(
                         fill="x", pady=(t.s(8), t.s(5)))
        else:
            for change in self._changes:
                self._render_change_card(list_card, change)
        if self._selected_change or self._change:
            self._render_change_detail(parent, project)

    def _render_change_card(self, parent: tk.Misc, change: dict) -> None:
        selected = str(change.get("id") or "") == self._selected_change
        row = tk.Frame(parent, bg=t.ACTIVE if selected else t.SURFACE_ALT,
                       highlightbackground=t.TERRACOTTA if selected else t.LINE_FAINT,
                       highlightthickness=1, padx=t.s(10), pady=t.s(8), cursor="hand2")
        row.pack(fill="x", pady=(t.s(7), 0))
        status = str(change.get("status") or "draft")
        author = str(change.get("author_name") or change.get("author_id") or "成员")
        purpose = str(change.get("purpose") or "（未填写目的）")
        sha = str(change.get("current_sha") or change.get("commit_sha") or "—")
        tk.Label(row, text=f"{purpose}  ·  {_CHANGE_STATUS.get(status, status)}",
                 bg=row.cget("bg"), fg=t.INK, font=self.fonts.small_bold,
                 anchor="w", justify="left").pack(fill="x")
        tk.Label(row, text=f"作者：{author} · SHA：{sha[:12]} · ID：{change.get('id')}",
                 bg=row.cget("bg"), fg=t.INK_MUTED,
                 font=self.fonts.caption, anchor="w").pack(fill="x", pady=(t.s(4), 0))
        row.bind("<Button-1>", lambda _event, cid=str(change.get("id") or ""):
                 self.select_change(cid), add="+")
        for child in row.winfo_children():
            child.bind("<Button-1>", lambda _event, cid=str(change.get("id") or ""):
                       self.select_change(cid), add="+")

    def select_change(self, change_id: str) -> None:
        if not change_id or not self.project_id:
            return
        self._selected_change = str(change_id)
        self._change = {}
        self._diff = ""
        generation = self._request_generation
        pid = self.project_id
        self._request_change("change_detail", generation, pid, change_id,
                             lambda: self.bridge.client.get(
                                 f"/api/v1/projects/{pid}/changes/{change_id}", timeout=15))
        self._request_change("diff", generation, pid, change_id,
                             lambda: self.bridge.client.get(
                                 f"/api/v1/projects/{pid}/changes/{change_id}/diff", timeout=15))
        self._render_page()

    def _request_change(self, name: str, generation: int, project_id: str,
                        change_id: str, fn) -> None:
        def run():
            try:
                code, data = fn()
            except Exception as exc:
                code, data = 0, {"detail": str(exc)}
            body = dict(data) if isinstance(data, dict) else {"detail": str(data)}
            body["_workspace_generation"] = generation
            body["_workspace_project_id"] = project_id
            body["_workspace_change_id"] = change_id
            return ("ok", (code, body))
        self.bridge.submit(f"team_workspace_{name}", run)

    def _render_change_detail(self, parent: tk.Misc, project: dict) -> None:
        detail = self._card(parent, padx=t.s(14), pady=(0, t.s(12)))
        tk.Label(detail, text="变更详情与差异", bg=t.SURFACE, fg=t.INK,
                 font=self.fonts.heading3).pack(anchor="w")
        if self._change.get("error"):
            detail_text = f"详情读取失败：{self._change['error']}"
        elif not self._change:
            detail_text = "正在加载 SHA、审阅票数与变更详情…"
        else:
            c = self._change
            required = int(c.get("required_approvals") or 1)
            approval_count = int(c.get("approval_count") or 0)
            reviews = c.get("reviews") or []
            reviewer_text = "; ".join(
                f"{row.get('reviewer_name') or row.get('reviewer_id')}: "
                f"{'已通过' if row.get('decision') == 'approve' else '已拒绝'}"
                f"{f' ({str(row.get('reviewed_sha') or '')[:12]})' if row.get('reviewed_sha') else ''}"
                f"{' · 已失效' if row.get('invalidated') else ''}"
                for row in reviews) or "暂无审阅记录"
            detail_text = (
                f"目的：{c.get('purpose') or '—'}\n任务：{c.get('job_id') or '—'}  ·  "
                f"作者：{c.get('author_name') or c.get('author_id') or '—'}\n"
                f"状态：{_CHANGE_STATUS.get(str(c.get('status') or ''), c.get('status') or '')}  ·  "
                f"已通过：{approval_count}/{required}\n"
                f"基础 SHA：{c.get('base_sha') or '—'}\n当前 SHA：{c.get('current_sha') or '—'}\n"
                f"文件：{', '.join(c.get('modified_files') or []) or '—'}\n审阅：{reviewer_text}"
            )
            if c.get("error"):
                detail_text += f"\n服务端原因：{c['error']}"
        tk.Label(detail, text=detail_text, bg=t.SURFACE, fg=t.INK_SOFT,
                 font=self.fonts.caption, anchor="w", justify="left",
                 wraplength=getattr(self, "_wrapwidth", t.s(720))).pack(fill="x", pady=(t.s(6), t.s(8)))
        diff_frame = tk.Frame(detail, bg=t.CODE_SURFACE)
        diff_frame.pack(fill="both", expand=True)
        diff_frame.rowconfigure(0, weight=1)
        diff_frame.columnconfigure(0, weight=1)
        yscroll = tk.Scrollbar(diff_frame, orient="vertical")
        xscroll = tk.Scrollbar(diff_frame, orient="horizontal")
        diff_box = tk.Text(diff_frame, height=12, wrap="none", bg=t.CODE_SURFACE,
                           fg=t.INK, insertbackground=t.INK, relief="flat",
                           font=self.fonts.mono,
                           yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        yscroll.configure(command=diff_box.yview)
        xscroll.configure(command=diff_box.xview)
        diff_box.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        diff_box.insert("1.0", self._diff or "正在加载文件差异…")
        diff_box.configure(state="disabled")
        self._diff_box = diff_box
        action = tk.Frame(detail, bg=t.SURFACE)
        action.pack(fill="x", pady=(t.s(8), 0))
        current = self._change
        writable = str(project.get("status") or "active") == "active"
        author_id = str(current.get("author_id") or "")
        current_user = self._current_user_id()
        is_author = bool(author_id and current_user and author_id == current_user)
        may_review = can_review_change(current, current_user_id=current_user,
                                       project_status=str(project.get("status") or "active"))
        if is_author:
            tk.Label(action, text="你是此变更的作者，不能审阅自己的提交。",
                     bg=t.SURFACE, fg=t.WARNING, font=self.fonts.caption).pack(side="left")
        if current:
            if may_review:
                self._action_button(action, "批准", lambda: self._review("approve"), "primary")
                self._action_button(action, "拒绝", lambda: self._review("reject"), "danger")
            elif not is_author and current.get("status") == "pending_review":
                tk.Label(action, text="当前身份未能通过审阅资格检查。",
                         bg=t.SURFACE, fg=t.WARNING,
                         font=self.fonts.caption).pack(side="left")
            merge_ready = (str(current.get("status") or "") == "approved"
                           and int(current.get("approval_count") or 0)
                           >= int(current.get("required_approvals") or 1))
            merge_button = self._action_button(action, "合并", self._merge_change, "soft")
            merge_button.set_enabled(bool(writable and merge_ready))
            reason = ("项目已归档，变更只读。" if not writable else
                      "达到项目要求的审阅票数后才能合并。" if not merge_ready else "")
            if reason:
                tk.Label(action, text=reason, bg=t.SURFACE, fg=t.INK_FAINT,
                         font=self.fonts.caption).pack(side="left", padx=(t.s(5), 0))
            if writable and current.get("status") in {"pending_review", "stale"}:
                self._action_button(action, "刷新基线", self._rebase_change, "outline")
        if not writable:
            tk.Label(action, text="项目已归档：审阅和合并操作已停用。",
                     bg=t.SURFACE, fg=t.WARNING,
                     font=self.fonts.caption).pack(side="left")

    def _action_button(self, parent, text, command, variant) -> FlatButton:
        button = FlatButton(parent, text, command, font=self.fonts.caption,
                            variant=variant, height=30, parent_bg=t.SURFACE)
        button.pack(side="left", padx=(0, t.s(6)))
        return button

    def _review(self, decision: str) -> None:
        if not self._change or not self._selected_change:
            return
        if not can_review_change(self._change, current_user_id=self._current_user_id(),
                                 project_status=str((self.project or {}).get("status") or "active")):
            self.app.toast("当前身份或项目状态不允许审阅此变更")
            return
        self._submit_change_action(
            "review", {"decision": decision,
                       "reviewed_sha": str(self._change.get("current_sha") or "")})

    def _merge_change(self) -> None:
        required = int(self._change.get("required_approvals") or 1)
        count = int(self._change.get("approval_count") or 0)
        if count < required:
            self.app.toast(f"还需要 {required - count} 票才能合并")
            return
        self._submit_change_action("merge")

    def _rebase_change(self) -> None:
        self._submit_change_action("rebase")

    def _submit_change_action(self, action: str, body: dict | None = None) -> None:
        if not self.connected or not self.project_id or not self._selected_change:
            return
        pid, cid = self.project_id, self._selected_change
        self._request_generation += 1
        generation = self._request_generation
        self._request("action", generation, pid, lambda: self.bridge.client.post(
            f"/api/v1/projects/{pid}/changes/{cid}/{action}", body or {}, timeout=30))

    def _initialize_versions(self) -> None:
        raw = self.init_paths.get().strip()
        if not raw:
            self.app.toast("请填写要共享的相对路径")
            return
        paths = [part.strip() for part in raw.replace(";", ",").replace("\n", ",").split(",")
                 if part.strip()]
        pid = self.project_id
        self._request_generation += 1
        self._request("action", self._request_generation, pid, lambda: self.bridge.client.post(
            f"/api/v1/projects/{pid}/versions/init", {"shared_paths": paths}, timeout=30))

    def _render_members(self, parent: tk.Misc, project: dict) -> None:
        card = self._card(parent, padx=t.s(14), pady=(0, t.s(12)))
        top = tk.Frame(card, bg=t.SURFACE)
        top.pack(fill="x")
        tk.Label(top, text="项目成员与权限", bg=t.SURFACE, fg=t.INK,
                 font=self.fonts.heading3).pack(side="left")
        FlatButton(top, "刷新", self._load_page_data, font=self.fonts.caption,
                   variant="outline", height=28, parent_bg=t.SURFACE).pack(side="right")
        tk.Label(card, text=(f"本人角色：{project.get('role') or '项目成员'}  ·  "
                             "Hub 加入和项目授权是两个独立步骤。"),
                 bg=t.SURFACE, fg=t.INK_MUTED, font=self.fonts.caption,
                 anchor="w").pack(fill="x", pady=(t.s(6), t.s(8)))
        if not self._members:
            tk.Label(card, text="正在读取项目成员…" if not self._project_detail.get("error")
                     else str(self._project_detail["error"]), bg=t.SURFACE,
                     fg=t.INK_FAINT, font=self.fonts.body).pack(anchor="w", pady=t.s(8))
        else:
            for member in self._members:
                self._render_member_card(card, member)
        if project.get("role") in {"owner", "admin"}:
            FlatButton(card, "邀请或管理成员", self._open_project_manager,
                       font=self.fonts.caption, variant="soft", height=32,
                       parent_bg=t.SURFACE).pack(anchor="w", pady=(t.s(10), 0))

    def _render_member_card(self, parent: tk.Misc, member: dict) -> None:
        devices = list(member.get("devices") or [])
        active_devices = [row for row in devices if row.get("status") == "active"]
        name = str(member.get("display_name") or member.get("user_id") or "成员")
        row = tk.Frame(parent, bg=t.SURFACE_ALT, padx=t.s(10), pady=t.s(8))
        row.pack(fill="x", pady=(0, t.s(6)))
        top = tk.Frame(row, bg=t.SURFACE_ALT)
        top.pack(fill="x")
        tk.Label(top, text=name, bg=t.SURFACE_ALT, fg=t.INK,
                 font=self.fonts.small_bold).pack(side="left")
        tk.Label(top, text=str(member.get("role") or "member"), bg=t.SURFACE_ALT,
                 fg=t.TERRACOTTA, font=self.fonts.caption).pack(side="right")
        summary = f"{len(active_devices)} 个获准终端"
        if member.get("status"):
            summary += f" · {member.get('status')}"
        tk.Label(row, text=summary, bg=t.SURFACE_ALT, fg=t.INK_MUTED,
                 font=self.fonts.caption, anchor="w").pack(fill="x", pady=(t.s(4), 0))
        if devices:
            tk.Label(row, text="、".join(
                str(device.get("name") or device.get("display_code") or "终端")
                for device in active_devices) or "没有活动终端",
                bg=t.SURFACE_ALT, fg=t.INK_FAINT,
                font=self.fonts.caption, anchor="w").pack(fill="x", pady=(t.s(2), 0))

    def _render_changes_placeholder(self, error: str = "") -> None:
        self._set_status_message(error or "版本与审阅数据正在刷新。")

    def _refresh_changes_page(self) -> None:
        if self.page == "changes":
            self._render_page()

    def _refresh_members_page(self, error: str = "") -> None:
        if error:
            self._project_detail = {**self._project_detail, "error": error}
        if self.page == "members":
            self._render_page()

    def _set_status_message(self, message: str, *, danger: bool = False) -> None:
        self.app.toast(message, duration=4200 if danger else 3000)

    def _initialize_noop(self) -> None:
        return

    def _card(self, parent: tk.Misc, *, padx: int = 12, pady=(0, 8),
              bg: str = t.SURFACE) -> tk.Frame:
        frame = tk.Frame(parent, bg=bg, highlightbackground=t.LINE_FAINT,
                         highlightthickness=1, padx=padx, pady=t.s(11))
        frame.pack(fill="x", pady=pady)
        return frame

    def _message_card(self, parent: tk.Misc, title: str, body: str,
                      action: tuple[str, object] | None = None) -> None:
        card = self._card(parent, padx=t.s(20), pady=(t.s(22), 0))
        tk.Label(card, text=title, bg=t.SURFACE, fg=t.INK,
                 font=self.fonts.heading2).pack(anchor="w")
        tk.Label(card, text=body, bg=t.SURFACE, fg=t.INK_MUTED,
                 font=self.fonts.body, anchor="w", justify="left",
                 wraplength=getattr(self, "_wrapwidth", t.s(720))).pack(
                     fill="x", pady=(t.s(7), t.s(12)))
        if action:
            FlatButton(card, action[0], action[1], font=self.fonts.caption,
                       variant="primary", height=32,
                       parent_bg=t.SURFACE).pack(anchor="w")
