"""Multi-project Hub window for VenusChat V1."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox

from . import theme as t
from .api_client import ApiClient
from .config_store import (
    active_project_for_origin,
    load_config,
    normalize_team_origin,
    project_preference_key,
    save_active_project_for_origin,
    team_connection,
    team_token_for_connection,
)
from .project_api import ProjectHubApi
from .project_governance_view import open_project_governance
from .terminal_identity import get_installation_code
from .team_collab_view import open_team_collab
from .worker_control_view import open_worker_control
from .worker_task_view import open_worker_tasks
from .widgets import FlatButton, MinimalField, ScrollArea, SelectField


_ROLE = {"owner": "负责人", "admin": "管理员", "member": "成员", "viewer": "只读"}
_INVITABLE_ROLES = ("member", "viewer")
_STATUS = {
    "active": "正常", "pending_claim": "等待认领", "removed": "已移除",
    "revoked": "已撤销", "pending": "待处理", "accepted": "已接受",
    "expired": "已过期", "archived": "已归档", "planning": "规划中",
}


def _project_name(project: dict) -> str:
    return str(project.get("name") or project.get("title") or "未命名项目")


def _pending_item_count(project: dict) -> int:
    try:
        return max(0, int(project.get("pending_approvals") or 0))
    except (TypeError, ValueError):
        return 0


def project_can_be_archived(project: dict) -> bool:
    return (str(project.get("role") or "") == "owner"
            and str(project.get("status") or "") == "active")


def invite_role_allowed(role: str) -> bool:
    return str(role or "") in _INVITABLE_ROLES


def project_worker_actions_enabled(project: dict | None) -> bool:
    return (bool(project)
            and str(project.get("status") or "") == "active"
            and bool(project.get("is_team")))


def project_access_from_results(detail_code: int, members_code: int,
                                invites_code: int) -> tuple[bool, bool, int]:
    """Separate project read access from invitation management access."""
    if detail_code != 200:
        return False, False, detail_code
    if members_code != 200:
        return False, False, members_code
    if invites_code == 200:
        return True, True, 0
    if invites_code == 403:
        return True, False, 0
    return False, False, invites_code


class ProjectHubWindow(tk.Toplevel):
    """Project controls use the same queue polling pattern as team review windows."""

    def __init__(self, parent, app, fonts, client: ApiClient, *,
                 initial_name: str = "", initial_description: str = "") -> None:
        super().__init__(parent)
        self.app = app
        self.fonts = fonts
        self.client = client
        self._results: queue.Queue = queue.Queue()
        self._poll_job = self.after(80, self._poll_results)
        self._busy = False
        self._rows: list[dict] = []
        self._member_rows: list[dict] = []
        self._invite_rows: list[dict] = []
        self._user_rows: list[dict] = []
        self._selected_project_id = ""
        self._selected_project_detail: dict = {}
        self._current_user_id = ""
        self._active_project_id: str | None = None
        self._pending_claim: dict = {}
        self._preview_invite: dict = {}
        self._preview_code = ""
        self._destroying = False
        self._terminal_display_code = ""
        try:
            self._installation_code = get_installation_code()
            self._installation_code_error = ""
        except Exception as exc:
            self._installation_code = ""
            self._installation_code_error = str(exc)
        self._action_buttons: list = []
        self._button_states_before: list[tuple[object, bool]] = []
        self._visible_rows: list[dict] = []
        self._can_manage_project = False
        self._can_archive_project = False
        self._worker_window = None
        self._worker_task_window = None

        config = load_config()
        connections = [row for row in (config.get("team_connections") or [])
                       if isinstance(row, dict) and row.get("origin")]
        current = str(client.base or "").rstrip("/")
        known = {str(row.get("origin") or "").rstrip("/") for row in connections}
        origin = current if current in known else str(
            config.get("team_last_origin") or
            (connections[-1].get("origin") if connections else current))
        self.title("项目中心")
        self.configure(bg=t.CANVAS)
        self.geometry("1040x800")
        self.minsize(840, 620)
        self.transient(parent)

        header = tk.Frame(self, bg=t.HEADER, padx=t.s(22), pady=t.s(14))
        header.pack(fill="x")
        tk.Label(header, text="项目中心", bg=t.HEADER, fg=t.INK,
                 font=fonts.display_md).pack(anchor="w")
        tk.Label(header,
                 text="加入项目只授予项目访问权限，不会开启本机 Worker。",
                 bg=t.HEADER, fg=t.INK_MUTED, font=fonts.caption).pack(anchor="w", pady=(3, 0))

        self.body = ScrollArea(self, bg=t.CANVAS, scrollbar=True)
        self.body.pack(fill="both", expand=True, padx=t.s(20), pady=(t.s(12), t.s(18)))
        content = self.body.inner

        section, body = self._section(content, "连接与终端身份")
        self.origin_field = self._field(body, "Hub HTTPS 地址", origin,
                                        "https://hub.example.ts.net")
        self.status = tk.Label(body, text="尚未刷新", bg=t.CANVAS,
                               fg=t.INK_MUTED, font=fonts.body, anchor="w")
        self.status.pack(fill="x", pady=(t.s(6), 2))
        installation_text = (self._installation_code or
                             f"本机码不可用：{self._installation_code_error or '无法写入本地标识'}")
        self.identity = tk.Label(
            body,
            text=(f"本机安装码（VI，公开标识）：{installation_text}\n"
                  "此 Hub 终端码（VN）：审批并领取后由该 Hub 分配\n"
                  "设备凭证：秘密，仅保存在安全存储中，不会显示"),
                                 bg=t.CANVAS, fg=t.INK_SOFT, font=fonts.small,
                                 anchor="w", justify="left", wraplength=t.s(880))
        self.identity.pack(fill="x", pady=(0, t.s(5)))
        tk.Label(body,
                 text="VI 只供核对申请来源；项目邀请必须使用此 Hub 批准设备后分配的 VN 码。",
                 bg=t.CANVAS, fg=t.INK_MUTED, font=fonts.caption,
                 anchor="w", justify="left", wraplength=t.s(880)).pack(fill="x")
        self._buttons(body, (("复制本机安装码 VI", self.copy_installation_code, "outline"),
                             ("复制此 Hub 终端码 VN", self.copy_terminal_display_code, "outline")))
        self._buttons(body, (("刷新项目和成员", self.refresh, "primary"),))
        self._finish_section(section)

        section, body = self._section(content, "我的项目", "按角色和待审批情况筛选；点击项目查看详情。")
        self.project_overview = tk.Label(body, text="尚未读取项目概况",
                                         bg=t.CANVAS, fg=t.INK_SOFT,
                                         font=fonts.body_medium, anchor="w")
        self.project_overview.pack(fill="x", pady=(0, t.s(5)))
        filter_row = tk.Frame(body, bg=t.CANVAS)
        filter_row.pack(fill="x", pady=(0, t.s(5)))
        tk.Label(filter_row, text="筛选", bg=t.CANVAS, fg=t.INK_MUTED,
                 font=fonts.caption).pack(side="left", padx=(0, t.s(8)))
        self.project_filter = SelectField(
            filter_row, ("全部项目", "我负责", "待处理"), font=fonts.small,
            value="全部项目", bg=t.CANVAS)
        self.project_filter.pack(side="left", padx=(0, t.s(5)))
        self.project_filter.variable.trace_add(
            "write", lambda *_args: self._render_projects(self._safe_origin()))
        self.project_list = self._listbox(body, 4)
        self.project_list.bind("<<ListboxSelect>>", self._on_project_selected)
        self.selected_label = tk.Label(body, text="请选择项目查看成员、邀请与审批状态。",
                                       bg=t.CANVAS, fg=t.INK_MUTED,
                                       font=fonts.caption, anchor="w", justify="left")
        self.selected_label.pack(fill="x", pady=(0, t.s(6)))
        self.set_current_button = self._buttons(
            body, (("设为当前派活项目", self.set_current_project, "primary"),))[0]
        self.archive_button = self._buttons(
            body, (("归档所选项目", self.archive_project, "outline"),))[0]
        self.archive_button.set_enabled(False)
        self._finish_section(section)

        section, body = self._section(
            content, "任务与审批入口",
            "Agent 后台任务显示在对话任务面板；Worker 派活与审批使用独立队列，队列状态不代表 Agent 正在运行。")
        self.task_overview = tk.Label(
            body, text="Agent 任务和 Worker 派活状态由对应面板读取。",
            bg=t.CANVAS, fg=t.INK_MUTED, font=fonts.caption,
            anchor="w", justify="left", wraplength=t.s(900))
        self.task_overview.pack(fill="x", pady=(0, t.s(4)))
        (self.agent_tasks_button, self.approvals_button,
         self.worker_tasks_button, self.local_worker_button,
         self.governance_button) = self._buttons(body, (
            ("打开 Agent 任务面板", self.open_agent_tasks, "primary"),
            ("查看所选项目审批", self.open_approvals, "outline"),
            ("项目 Worker 任务与审批", self.open_worker_tasks, "outline"),
            ("本机 Worker 授权", self.open_local_worker, "outline"),
            ("治理提案", self.open_governance, "outline"),
        ))
        self._set_project_action_states(None)
        self._finish_section(section)

        section, body = self._section(content, "创建并认领项目",
                                      "创建会生成一次性认领码。创建者需要在此手动输入认领码完成认领。")
        self.create_name = self._field(body, "项目名称", initial_name, "例如：Venus 客户端")
        self.create_description = self._field(body, "项目简介", initial_description,
                                              "项目用途或共享范围")
        self._buttons(body, (("创建项目并显示一次性认领码", self.create_project, "primary"),))
        self.claim_label = tk.Label(body, text="尚无等待认领的项目。",
                                    bg=t.CANVAS, fg=t.INK_MUTED,
                                    font=fonts.caption, anchor="w")
        self.claim_label.pack(fill="x", pady=(t.s(7), 3))
        self.claim_code = self._field(body, "手动输入认领码", "", "从一次性弹窗粘贴", secret=True)
        self._buttons(body, (("认领此项目", self.claim_project, "outline"),))
        self._finish_section(section)

        section, body = self._section(content, "接受项目邀请",
                                      "先预览 Hub、项目、负责人、角色和终端，再由服务端重新校验并接受。")
        self.invite_code = self._field(body, "项目邀请码", "", "由项目负责人通过可信渠道提供", secret=True)
        self._buttons(body, (("预览邀请", self.preview_invite, "primary"),
                             ("接受已预览的邀请", self.accept_invite, "outline")))
        self.preview_label = tk.Label(body, text="尚未预览邀请。", bg=t.CANVAS,
                                      fg=t.INK_MUTED, font=fonts.caption,
                                      anchor="w", justify="left", wraplength=t.s(900))
        self.preview_label.pack(fill="x", pady=(t.s(5), t.s(2)))
        self._finish_section(section)

        section, body = self._section(content, "项目成员与终端授权",
                                      "移除成员或撤销项目终端会立即影响该项目访问。负责人不能从成员列表移除。")
        self.terminal_code = self._field(body, "受邀终端显示码", "", "例如 VN-7C4M-2Q9P")
        tk.Label(body, text="Hub 用户目录（选择用户后自动填入用户 ID）",
                 bg=t.CANVAS, fg=t.INK_MUTED, font=fonts.caption,
                 anchor="w").pack(fill="x", pady=(t.s(7), 1))
        self.user_list = self._listbox(body, 4)
        self.user_list.bind("<<ListboxSelect>>", self._on_user_selected)
        self.target_user = self._field(body, "受邀用户 ID", "", "由 Hub 身份提供")
        self.role_field = self._select_field(body, "项目角色", _INVITABLE_ROLES, "member")
        self.invite_create_button = self._buttons(
            body, (("创建项目邀请并显示一次性邀请码", self.create_invite, "primary"),))[0]
        self.invite_list = self._listbox(body, 3)
        self.member_list = self._listbox(body, 7)
        self.member_list.bind("<<ListboxSelect>>", self._on_member_selected)
        self.member_actions = self._buttons(body, (
            ("移除所选成员", self.remove_member, "outline"),
            ("撤销所选终端", self.revoke_device, "outline"),
        ))
        self._set_management_enabled(False)
        self._finish_section(section, final=True)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.refresh()

    def _section(self, parent, title: str, subtitle: str = ""):
        outer = tk.Frame(parent, bg=t.CANVAS, padx=t.s(2), pady=t.s(4))
        outer.pack(fill="x", pady=(0, t.s(9)))
        tk.Label(outer, text=title, bg=t.CANVAS, fg=t.INK,
                 font=self.fonts.body_medium).pack(anchor="w", pady=(t.s(5), 2))
        if subtitle:
            tk.Label(outer, text=subtitle, bg=t.CANVAS, fg=t.INK_MUTED,
                     font=self.fonts.caption, anchor="w", justify="left").pack(anchor="w", pady=(0, t.s(5)))
        body = tk.Frame(outer, bg=t.CANVAS, highlightbackground=t.LINE,
                        highlightthickness=1, padx=t.s(12), pady=t.s(10))
        body.pack(fill="x", pady=(t.s(5), 0))
        return outer, body

    def _finish_section(self, section, *, final: bool = False) -> None:
        if final:
            section.pack_configure(pady=(0, t.s(18)))

    def _field(self, parent, label: str, value: str, placeholder: str,
               *, secret: bool = False) -> MinimalField:
        row = tk.Frame(parent, bg=t.CANVAS)
        row.pack(fill="x", pady=t.s(3))
        tk.Label(row, text=label, width=18, bg=t.CANVAS, fg=t.INK_SOFT,
                 font=self.fonts.caption, anchor="w").pack(side="left")
        field = MinimalField(row, font=self.fonts.body, value=value,
                             placeholder=placeholder, show="•" if secret else "",
                             bg=t.SURFACE)
        field.pack(side="left", fill="x", expand=True)
        return field

    def _select_field(self, parent, label: str, values: tuple[str, ...], value: str):
        row = tk.Frame(parent, bg=t.CANVAS)
        row.pack(fill="x", pady=t.s(3))
        tk.Label(row, text=label, width=18, bg=t.CANVAS, fg=t.INK_SOFT,
                 font=self.fonts.caption, anchor="w").pack(side="left")
        field = SelectField(row, values, font=self.fonts.body, value=value, bg=t.SURFACE)
        field.pack(side="left", fill="x", expand=True)
        return field

    def _buttons(self, parent, buttons):
        row = tk.Frame(parent, bg=t.CANVAS)
        row.pack(fill="x", pady=(t.s(8), t.s(3)))
        result = []
        for label, command, variant in buttons:
            button = FlatButton(row, label, command, font=self.fonts.small,
                                variant=variant, height=34, min_width=132,
                                parent_bg=t.CANVAS)
            button.pack(side="left", padx=(0, t.s(7)))
            result.append(button)
            self._action_buttons.append(button)
        return result

    def _set_action_buttons_busy(self, busy: bool) -> None:
        if busy:
            self._button_states_before = [
                (button, bool(button.enabled)) for button in self._action_buttons]
            for button, _enabled in self._button_states_before:
                button.set_enabled(False)
            return
        for button, enabled in self._button_states_before:
            try:
                if button.winfo_exists():
                    button.set_enabled(enabled)
            except tk.TclError:
                pass
        self._button_states_before = []

    def _listbox(self, parent, height: int) -> tk.Listbox:
        holder = tk.Frame(parent, bg=t.SURFACE, highlightthickness=1,
                          highlightbackground=t.LINE, height=t.s(height * 28))
        holder.pack(fill="x", pady=(t.s(4), t.s(7)))
        holder.pack_propagate(False)
        box = tk.Listbox(holder, height=height, font=self.fonts.small,
                         bg=t.SURFACE, fg=t.INK, selectbackground=t.TERRACOTTA_SOFT,
                         selectforeground=t.INK, relief="flat", bd=0,
                         activestyle="none", highlightthickness=0)
        box.pack(fill="both", expand=True, padx=t.s(7), pady=t.s(4))
        return box

    def _origin(self) -> str:
        try:
            return normalize_team_origin(str(self.origin_field.get()).strip())
        except ValueError as exc:
            raise ValueError(f"Hub 地址无效：{exc}") from exc

    def _hub_client(self, origin: str) -> ApiClient:
        record = team_connection(origin)
        token = team_token_for_connection(origin, str(record.get("team_id") or "")) if record else ""
        if token:
            return ApiClient(origin, token=token, token_header="X-Team-Device-Token",
                             use_default_token=False, deny_redirects=True)
        return ApiClient(origin, use_default_token=False, deny_redirects=True)

    def _request(self, action: str, work) -> None:
        if self._busy:
            self.app.toast("Hub 请求正在进行，请稍候")
            return
        self._busy = True
        self._set_action_buttons_busy(True)
        self.status.configure(text="正在安全连接 Hub…", fg=t.INK_MUTED)

        def run() -> None:
            try:
                result = work()
            except Exception as exc:
                result = {"code": 0, "data": {"detail": str(exc)}}
            self._results.put((action, result))

        threading.Thread(target=run, daemon=True,
                         name=f"venus-project-hub-{action}").start()

    def _poll_results(self) -> None:
        if self._destroying:
            return
        for _ in range(12):
            try:
                action, result = self._results.get_nowait()
            except queue.Empty:
                break
            self._on_result(action, result)
        try:
            self._poll_job = self.after(80, self._poll_results)
        except tk.TclError:
            pass

    def refresh(self) -> None:
        try:
            origin = self._origin()
        except ValueError as exc:
            self.status.configure(text=str(exc), fg=t.DANGER)
            return

        def work():
            client = self._hub_client(origin)
            api = ProjectHubApi(client)
            identity_code, identity = client.get("/api/v1/team/me", timeout=10)
            projects_code, projects = api.list_projects()
            directory = api.list_hub_users()
            rows = list(projects.get("projects") or []) if projects_code == 200 else []
            server_active = (str(projects.get("active") or "")
                             if "active" in projects else
                             active_project_for_origin(origin))
            preferred = self._selected_project_id or server_active
            if preferred not in {str(row.get("id") or "") for row in rows}:
                preferred = server_active
            if preferred not in {str(row.get("id") or "") for row in rows}:
                preferred = ""
            detail = members = invites = None
            selected_row = next((row for row in rows
                                 if str(row.get("id") or "") == preferred), None)
            if preferred and str((selected_row or {}).get("status") or "") != "pending_claim":
                detail = api.project_detail(preferred)
                members = api.list_members(preferred)
                invites = api.list_invites(preferred)
            return {"origin": origin, "client": client,
                    "identity": (identity_code, identity),
                    "directory": directory,
                    "projects": (projects_code, projects), "rows": rows,
                    "selected": preferred, "active": server_active,
                    "detail": detail,
                    "members": members, "invites": invites}

        self._request("refresh", work)

    def copy_terminal_display_code(self) -> None:
        code = self._terminal_display_code
        if not code:
            self.app.toast("此 Hub 的 VN 终端码尚未分配；审批并领取设备凭证后才可复制")
            return
        self.clipboard_clear()
        self.clipboard_append(code)
        self.update_idletasks()
        self.app.toast("此 Hub 的 VN 终端码已复制")

    def copy_installation_code(self) -> None:
        if not self._installation_code:
            self.app.toast(f"本机安装码不可用：{self._installation_code_error or '无法读取本地标识文件'}",
                           duration=5000)
            return
        self.clipboard_clear()
        self.clipboard_append(self._installation_code)
        self.update_idletasks()
        self.app.toast("本机安装码 VI 已复制；它是公开标识，不是设备凭证")

    def open_agent_tasks(self) -> None:
        self.withdraw()
        try:
            self.app.show_chat()
            chat_view = getattr(self.app, "chat_view", None)
            if chat_view is None:
                raise RuntimeError("对话任务面板暂不可用")
            if not getattr(chat_view, "panel_pinned", False):
                chat_view._toggle_panel()
            chat_view._request_jobs_refresh()
        except Exception as exc:
            self.deiconify()
            self.lift()
            self.app.toast(str(exc) or "对话任务面板暂不可用")
            return
        # Close this transient and its poll timer after the task panel is visible.
        self.destroy()

    def _selected_project(self) -> dict | None:
        if self.project_list is not None:
            selection = self.project_list.curselection()
            if selection and selection[0] < len(self._visible_rows):
                return self._visible_rows[selection[0]]
        return next((row for row in self._rows
                     if str(row.get("id") or "") == self._selected_project_id), None)

    def _on_project_selected(self, _event=None) -> None:
        project = self._selected_project()
        if not project:
            return
        self._set_project_action_states(None)
        self._set_archive_enabled(False)
        self._selected_project_detail = {}
        self._selected_project_id = str(project.get("id") or "")
        if str(project.get("status") or "") == "pending_claim":
            self._show_pending_claim(project)
            self.selected_label.configure(
                text=(f"{_project_name(project)} · 项目 ID：{project.get('id')}\n"
                      "状态：等待认领 · 请输入创建时显示且仍有效的认领码。"),
                fg=t.WARNING)
            self._render_projects(self._safe_origin())
            self._clear_project_lists()
            return
        self._pending_claim = {}
        self.claim_label.configure(text="请选择待认领项目，或创建新项目。",
                                   fg=t.INK_MUTED)
        self._load_selected_project()

    def _show_pending_claim(self, project: dict) -> None:
        self._pending_claim = dict(project)
        self.claim_label.configure(
            text=(f"待认领项目：{_project_name(project)} · {project.get('id')}。"
                  "输入仍有效的认领码；已关闭的认领码不会再次显示。"),
            fg=t.WARNING)

    def _clear_project_lists(self) -> None:
        self._member_rows = []
        self._invite_rows = []
        self.member_list.delete(0, "end")
        self.invite_list.delete(0, "end")
        self._set_management_enabled(False)
        self._set_archive_enabled(False)

    def _set_management_enabled(self, enabled: bool) -> None:
        self._can_manage_project = bool(enabled)
        self.invite_create_button.set_enabled(self._can_manage_project and not self._busy)
        for button in self.member_actions:
            button.set_enabled(self._can_manage_project and not self._busy)

    def _set_archive_enabled(self, enabled: bool) -> None:
        self._can_archive_project = bool(enabled)
        self.archive_button.set_enabled(self._can_archive_project and not self._busy)

    def _set_project_action_states(self, project: dict | None) -> None:
        status = str((project or {}).get("status") or "")
        is_team = bool((project or {}).get("is_team"))
        active = status == "active" and bool(project)
        self.set_current_button.set_enabled(active and not self._busy)
        self.approvals_button.set_enabled((active or status == "archived")
                                          and is_team and not self._busy)
        worker_actions = project_worker_actions_enabled(project)
        self.worker_tasks_button.set_enabled(worker_actions and not self._busy)
        self.local_worker_button.set_enabled(worker_actions and not self._busy)
        self.governance_button.set_enabled((active or status == "archived")
                                           and is_team and not self._busy)
        if hasattr(self, "archive_button"):
            self._set_archive_enabled(project_can_be_archived(project or {}))

    def _close_worker_windows_for(self, project_id: str) -> None:
        for attr in ("_worker_window", "_worker_task_window"):
            window = getattr(self, attr, None)
            if window is None:
                continue
            try:
                if not window.winfo_exists() or window.project_id != project_id:
                    continue
                window.destroy()
            except tk.TclError:
                pass
            setattr(self, attr, None)

    def _safe_origin(self) -> str:
        try:
            return self._origin()
        except ValueError:
            return ""

    def _load_selected_project(self) -> None:
        project = self._selected_project()
        if not project:
            return
        self._set_project_action_states(None)
        self._set_archive_enabled(False)
        project_id = str(project.get("id") or "")
        try:
            origin = self._origin()
        except ValueError as exc:
            self.status.configure(text=str(exc), fg=t.DANGER)
            return

        def work():
            api = ProjectHubApi(self._hub_client(origin))
            return {"origin": origin, "project_id": project_id,
                    "detail": api.project_detail(project_id),
                    "members": api.list_members(project_id),
                    "invites": api.list_invites(project_id)}

        self._request("project_detail", work)

    def set_current_project(self) -> None:
        project = self._selected_project()
        if not project:
            self.app.toast("请先选择一个项目")
            return
        if str(project.get("status") or "") == "pending_claim":
            self.app.toast("待认领项目不能设为当前派活项目")
            return
        if str(project.get("status") or "") == "archived":
            self.app.toast("已归档项目为只读，不能设为当前派活项目")
            return
        project_id = str(project.get("id") or "")
        try:
            origin = self._origin()
        except ValueError as exc:
            self.status.configure(text=str(exc), fg=t.DANGER)
            return
        def work():
            api = ProjectHubApi(self._hub_client(origin))
            code, data = api.set_active_project(project_id)
            return {"origin": origin, "code": code, "data": data,
                    "project_id": project_id, "project": project}

        self._request("activate", work)

    def archive_project(self) -> None:
        project = self._selected_project()
        if not project_can_be_archived(project or {}):
            self.app.toast("仅活动项目负责人可以归档项目")
            return
        project_id = str(project.get("id") or "")
        name = _project_name(project)
        confirm_text = (
            f"归档项目“{name}”？\n\n"
            "归档会停止该项目的新任务、邀请和 Worker 调用。项目归档后将变为只读。"
        )
        if not messagebox.askyesno("归档项目", confirm_text, parent=self):
            return
        try:
            origin = self._origin()
        except ValueError as exc:
            self.status.configure(text=str(exc), fg=t.DANGER)
            return

        def work():
            api = ProjectHubApi(self._hub_client(origin))
            code, data = api.archive_project(project_id)
            return {"origin": origin, "project_id": project_id,
                    "project": project, "code": code, "data": data}

        self._request("archive", work)

    def open_approvals(self) -> None:
        project = self._selected_project()
        if not project:
            self.app.toast("请先选择一个项目")
            return
        if not project.get("is_team", True):
            self.app.toast("审批面板仅适用于团队项目")
            return
        try:
            origin = self._origin()
        except ValueError as exc:
            self.status.configure(text=str(exc), fg=t.DANGER)
            return
        open_team_collab(self, self.app, self.fonts, self._hub_client(origin),
                         str(project.get("id") or ""), _project_name(project),
                         read_only=str(project.get("status") or "") == "archived")

    def open_governance(self) -> None:
        project = self._selected_project()
        if not project:
            self.app.toast("请先选择一个项目")
            return
        if not project.get("is_team", True):
            self.app.toast("治理提案仅适用于 Hub 团队项目")
            return
        project_id = str(project.get("id") or "")
        if str(self._selected_project_detail.get("id") or "") == project_id:
            project = {**project, **self._selected_project_detail}
        try:
            origin = self._origin()
        except ValueError as exc:
            self.status.configure(text=str(exc), fg=t.DANGER)
            return
        open_project_governance(
            self, self.app, self.fonts,
            ProjectHubApi(self._hub_client(origin)), project,
            self._current_user_id,
            read_only=str(project.get("status") or "") == "archived")

    def open_local_worker(self) -> None:
        project = self._selected_project()
        if not project or str(project.get("status") or "") != "active":
            self.app.toast("请先选择一个已认领且可访问的项目")
            return
        if not project.get("is_team"):
            self.app.toast("Worker 仅用于 Hub 团队项目")
            return
        try:
            origin = self._origin()
        except ValueError as exc:
            self.status.configure(text=str(exc), fg=t.DANGER)
            return
        if not team_connection(origin):
            self.app.toast("请先在此 Hub 注册本机 Venus 终端")
            return
        if self._worker_window is not None:
            try:
                if self._worker_window.winfo_exists():
                    if self._worker_window.project_id == str(project.get("id") or ""):
                        self._worker_window.deiconify()
                        self._worker_window.lift()
                        return
                    self._worker_window.destroy()
            except tk.TclError:
                pass
        self._worker_window = open_worker_control(
            self, self.fonts, origin=origin, project=project)

    def open_worker_tasks(self) -> None:
        project = self._selected_project()
        if not project or str(project.get("status") or "") != "active":
            self.app.toast("请先选择一个已认领且可访问的项目")
            return
        if not project.get("is_team"):
            self.app.toast("Worker 任务仅用于 Hub 团队项目")
            return
        try:
            origin = self._origin()
        except ValueError as exc:
            self.status.configure(text=str(exc), fg=t.DANGER)
            return
        if self._worker_task_window is not None:
            try:
                if self._worker_task_window.winfo_exists():
                    if self._worker_task_window.project_id == str(project.get("id") or ""):
                        self._worker_task_window.deiconify()
                        self._worker_task_window.lift()
                        return
                    self._worker_task_window.destroy()
            except tk.TclError:
                pass
        self._worker_task_window = open_worker_tasks(
            self, self.fonts, self._hub_client(origin), project)

    def create_project(self) -> None:
        name = str(self.create_name.get()).strip()
        description = str(self.create_description.get()).strip()
        if not name:
            self.app.toast("请填写项目名称")
            return
        try:
            origin = self._origin()
        except ValueError as exc:
            self.status.configure(text=str(exc), fg=t.DANGER)
            return

        def work():
            api = ProjectHubApi(self._hub_client(origin))
            code, data = api.create_project(name, description)
            return {"origin": origin, "code": code, "data": data}

        self._request("create", work)

    def claim_project(self) -> None:
        project_id = str(self._pending_claim.get("id") or "")
        claim_code = str(self.claim_code.get()).strip()
        if not project_id:
            self.app.toast("请先创建一个待认领项目")
            return
        if not claim_code:
            self.app.toast("请手动输入一次性认领码")
            return
        try:
            origin = self._origin()
        except ValueError as exc:
            self.status.configure(text=str(exc), fg=t.DANGER)
            return

        def work():
            api = ProjectHubApi(self._hub_client(origin))
            code, data = api.claim_project(project_id, claim_code)
            return {"origin": origin, "code": code, "data": data,
                    "project_id": project_id}

        self._request("claim", work)

    def preview_invite(self) -> None:
        code_text = str(self.invite_code.get()).strip()
        if not code_text:
            self.app.toast("请填写项目邀请码")
            return
        try:
            origin = self._origin()
        except ValueError as exc:
            self.status.configure(text=str(exc), fg=t.DANGER)
            return

        def work():
            api = ProjectHubApi(self._hub_client(origin))
            code, data = api.preview_invite(code_text)
            return {"origin": origin, "code": code, "data": data,
                    "invite_code": code_text}

        self._request("preview", work)

    def accept_invite(self) -> None:
        invite_id = str(self._preview_invite.get("id") or "")
        if not invite_id or not self._preview_code:
            self.app.toast("请先预览有效的邀请码")
            return
        try:
            origin = self._origin()
        except ValueError as exc:
            self.status.configure(text=str(exc), fg=t.DANGER)
            return
        invite_code = self._preview_code

        def work():
            api = ProjectHubApi(self._hub_client(origin))
            code, data = api.accept_invite(invite_id, invite_code)
            return {"origin": origin, "code": code, "data": data}

        self._request("accept", work)

    def create_invite(self) -> None:
        if not self._can_manage_project:
            self.app.toast("当前角色没有项目邀请管理权限")
            return
        project = self._selected_project()
        if not project:
            self.app.toast("请先选择一个项目")
            return
        terminal_code = str(self.terminal_code.get()).strip()
        user_id = str(self.target_user.get()).strip()
        role = str(self.role_field.get()).strip()
        if not invite_role_allowed(role):
            self.app.toast("管理员角色必须通过治理提案授予")
            return
        if not terminal_code or not user_id:
            self.app.toast("请填写受邀用户 ID 和终端显示码")
            return
        project_id = str(project.get("id") or "")
        try:
            origin = self._origin()
        except ValueError as exc:
            self.status.configure(text=str(exc), fg=t.DANGER)
            return

        def work():
            api = ProjectHubApi(self._hub_client(origin))
            code, data = api.create_invite(project_id, terminal_code, user_id, role)
            return {"origin": origin, "code": code, "data": data,
                    "project": project}

        self._request("create_invite", work)

    def _on_member_selected(self, _event=None) -> None:
        return

    def _on_user_selected(self, _event=None) -> None:
        selection = self.user_list.curselection()
        if not selection or selection[0] >= len(self._user_rows):
            return
        user = self._user_rows[selection[0]]
        self.target_user.set(str(user.get("id") or user.get("user_id") or ""))

    def _selected_member(self) -> dict | None:
        selection = self.member_list.curselection()
        if not selection or selection[0] >= len(self._member_rows):
            return None
        return self._member_rows[selection[0]]

    def remove_member(self) -> None:
        if not self._can_manage_project:
            self.app.toast("当前角色没有项目成员管理权限")
            return
        row = self._selected_member()
        project = self._selected_project()
        if not row or row.get("kind") != "member":
            self.app.toast("请选择一名成员")
            return
        if str(row.get("role") or "") == "owner":
            self.app.toast("项目负责人不能从成员列表移除")
            return
        if not project:
            return
        name = row.get("display_name") or row.get("name") or row.get("user_id")
        if not messagebox.askyesno("移除项目成员", f"移除 {name} 对此项目的访问？", parent=self):
            return
        self._member_action("remove_member", project, row)

    def revoke_device(self) -> None:
        if not self._can_manage_project:
            self.app.toast("当前角色没有项目终端管理权限")
            return
        row = self._selected_member()
        project = self._selected_project()
        if not row or row.get("kind") != "device":
            self.app.toast("请选择一台项目终端")
            return
        if not project:
            return
        name = row.get("name") or row.get("display_code") or row.get("device_id")
        if not messagebox.askyesno("撤销项目终端", f"立即撤销终端“{name}”的项目授权？", parent=self):
            return
        self._member_action("revoke_device", project, row)

    def _member_action(self, action: str, project: dict, row: dict) -> None:
        project_id = str(project.get("id") or "")
        try:
            origin = self._origin()
        except ValueError as exc:
            self.status.configure(text=str(exc), fg=t.DANGER)
            return

        def work():
            api = ProjectHubApi(self._hub_client(origin))
            if action == "remove_member":
                code, data = api.remove_member(project_id, str(row.get("user_id") or ""))
            else:
                code, data = api.revoke_device(
                    project_id, str(row.get("device_id") or row.get("id") or ""))
            return {"origin": origin, "code": code, "data": data}

        self._request(action, work)

    def _on_result(self, action: str, result) -> None:
        self._busy = False
        self._set_action_buttons_busy(False)
        if not isinstance(result, dict):
            self.status.configure(text="Hub 返回格式异常", fg=t.DANGER)
            return
        if action == "refresh":
            self._render_refresh(result)
            return
        if action == "project_detail":
            self._render_selected(result)
            return
        code = int(result.get("code") or 0)
        data = result.get("data") or {}
        if code != 200:
            msg = self._friendly_error(code, data)
            self.status.configure(text=msg, fg=t.DANGER)
            self.app.toast(msg, duration=5000)
            return
        if action == "activate":
            project = result.get("project") or {}
            project_id = str(result.get("project_id") or "")
            if str(data.get("active") or "") != project_id:
                msg = "Hub 未确认当前项目已切换，请刷新项目后重试。"
                self.status.configure(text=msg, fg=t.DANGER)
                self.app.toast(msg, duration=5000)
                return
            origin = str(result.get("origin") or "")
            self._active_project_id = project_id
            save_active_project_for_origin(origin, project_id)
            prefs = getattr(self.app, "_backend_project_preferences", None)
            if isinstance(prefs, dict):
                prefs[project_preference_key(origin)] = project_id
            if self.app.client.base.rstrip("/") == origin:
                apply_local = getattr(self.app, "apply_active_project", None)
                if callable(apply_local):
                    apply_local(project_id)
                else:
                    self.app.bridge.active_project_id = project_id
                    self.app.chat_view.refresh_projects()
            self._render_projects(origin)
            self.status.configure(text=f"当前 Hub 派活项目：{_project_name(project)}",
                                  fg=t.SUCCESS)
            self.app.toast(f"已设为当前派活项目：{_project_name(project)}")
            return
        if action == "archive":
            project = result.get("project") or {}
            project = {**project, "status": "archived"}
            project_id = str(result.get("project_id") or project.get("id") or "")
            self._selected_project_id = project_id
            self._close_worker_windows_for(project_id)
            if str(self._selected_project_detail.get("id") or "") == project_id:
                self._selected_project_detail["status"] = "archived"
            origin = str(result.get("origin") or "")
            was_active = self._active_project_id == project_id
            for row in self._rows:
                if str(row.get("id") or "") == project_id:
                    row["status"] = "archived"
            if was_active:
                self._active_project_id = ""
                if self.app.client.base.rstrip("/") == origin:
                    bridge = getattr(self.app, "bridge", None)
                    if getattr(bridge, "active_project_id", "") == project_id:
                        apply_local = getattr(self.app, "apply_active_project", None)
                        if callable(apply_local):
                            apply_local("")
            self._set_project_action_states(project)
            self._set_management_enabled(False)
            self._render_projects(origin)
            self.selected_label.configure(
                text=(f"{_project_name(project)} · 项目 ID：{project_id}\n"
                      "状态：已归档 · 归档项目为只读。"), fg=t.INK_SOFT)
            self.status.configure(
                text=f"项目“{_project_name(project)}”已归档为只读，正在刷新项目状态。",
                fg=t.SUCCESS)
            self.app.toast("项目已归档为只读；新任务、邀请和 Worker 调用已停止")
            self.refresh()
            return
        if action == "create":
            project = data.get("project") or data.get("created") or {}
            claim_code = str(data.get("claim_code") or data.get("claim_key") or "")
            if not project.get("id") or not claim_code:
                msg = "项目已创建，但 Hub 没有返回一次性认领码；请联系 Hub 管理员检查响应合同。"
                self.status.configure(text=msg, fg=t.DANGER)
                self.app.toast(msg, duration=6000)
                self.refresh()
                return
            self._pending_claim = dict(project)
            self._selected_project_id = str(project.get("id") or "")
            self.claim_label.configure(text=f"待认领项目：{_project_name(project)} · {project.get('id')}",
                                       fg=t.WARNING)
            self.claim_code.set("")
            self._show_secret_once("一次性项目认领码", project, claim_code,
                                   "认领码仅显示本次。请将它手动粘贴到‘手动输入认领码’后认领。")
            self.status.configure(text="项目已创建，等待当前用户手动输入认领码。", fg=t.SUCCESS)
            self.refresh()
            return
        if action == "claim":
            project = data.get("project") or self._pending_claim
            self._pending_claim = {}
            self.claim_code.set("")
            self.claim_label.configure(text="项目已认领。", fg=t.SUCCESS)
            self._selected_project_id = str(project.get("id") or result.get("project_id") or "")
            self.app.toast(f"已认领项目：{_project_name(project)}")
            self.refresh()
            return
        if action == "preview":
            invite = data.get("invite") or {}
            if not invite.get("id"):
                self.status.configure(text="预览响应缺少邀请 ID，无法继续接受。", fg=t.DANGER)
                return
            self._preview_invite = dict(invite)
            self._preview_code = str(result.get("invite_code") or "")
            project = invite.get("project") or {}
            owner = invite.get("owner") or {}
            device = invite.get("device") or {}
            terminal_name = (device.get("name") or invite.get("terminal_name") or "—")
            terminal_code = (device.get("display_code") or
                             invite.get("terminal_display_code") or "—")
            text = (f"Hub：{result.get('origin')}\n"
                    f"项目：{_project_name(project)} · {project.get('id') or invite.get('project_id')}\n"
                    f"负责人：{owner.get('display_name') or owner.get('name') or invite.get('owner_name') or invite.get('owner_id') or '—'}\n"
                    f"拟授予角色：{_ROLE.get(str(invite.get('role') or ''), invite.get('role') or '—')}\n"
                    f"用户：{invite.get('user_id') or '—'}\n"
                    f"终端：{terminal_name} · {terminal_code}\n"
                    f"状态：{_STATUS.get(str(invite.get('status') or ''), invite.get('status') or '—')}    到期：{invite.get('expires_at') or '—'}")
            self.preview_label.configure(text=text, fg=t.INK_SOFT)
            self.status.configure(text="请核对 Hub 与邀请详情；接受时 Hub 会再次验证邀请码和设备。",
                                  fg=t.SUCCESS)
            return
        if action == "accept":
            self._preview_invite = {}
            self._preview_code = ""
            self.invite_code.set("")
            self.preview_label.configure(text="邀请已接受；可从我的项目中选择项目。", fg=t.SUCCESS)
            self.app.toast("已加入项目。此操作不会开启本机 Worker。")
            self.refresh()
            return
        if action == "create_invite":
            invite = data.get("invite") or {}
            invite_code = str(data.get("invite_code") or "")
            if not invite_code:
                self.status.configure(text="邀请已创建，但 Hub 没有返回一次性邀请码。", fg=t.DANGER)
                self.refresh()
                return
            project = result.get("project") or {}
            popup_record = {**invite, "name": _project_name(project),
                            "project_id": project.get("id") or invite.get("project_id")}
            self._show_secret_once("一次性项目邀请码", popup_record, invite_code,
                                   "请通过可信渠道交给指定用户和终端。")
            self.status.configure(text="项目邀请已创建，邀请码只显示一次。", fg=t.SUCCESS)
            self.refresh()
            return
        if action in {"remove_member", "revoke_device"}:
            self.status.configure(text="项目授权已更新。", fg=t.SUCCESS)
            self.app.toast("项目授权已更新")
            self.refresh()

    def _render_refresh(self, result: dict) -> None:
        origin = str(result.get("origin") or "")
        identity_code, identity = result.get("identity") or (0, {})
        projects_code, projects = result.get("projects") or (0, {})
        directory_code, directory = result.get("directory") or (0, {})
        if identity_code == 200:
            user = identity.get("user") or {}
            self._current_user_id = str(user.get("id") or user.get("user_id") or "")
            device = identity.get("device") or {}
            display_code = (device.get("display_code") or identity.get("display_code")
                            or identity.get("terminal_display_code") or "未提供")
            self._terminal_display_code = ("" if display_code == "未提供"
                                           else str(display_code))
            user_name = user.get("display_name") or user.get("name") or user.get("user_id") or user.get("id") or "已认证用户"
            self.identity.configure(
                text=(f"Hub 用户：{user_name} · 用户 ID：{user.get('id') or user.get('user_id') or '—'}\n"
                      f"本机安装码（VI，公开标识）：{self._installation_code or '本机码不可用'}\n"
                      f"此 Hub 终端码（VN）：{display_code}\n"
                      "设备凭证：已在安全存储中使用；令牌不显示"), fg=t.INK_SOFT)
        else:
            self._current_user_id = ""
            self._terminal_display_code = ""
            self.identity.configure(
                text=(f"Hub 用户：未认证\n"
                      f"本机安装码（VI，公开标识）：{self._installation_code or '本机码不可用'}\n"
                      "此 Hub 终端码（VN）：审批并领取后由该 Hub 分配\n"
                      "设备凭证：不显示；请先在团队与成员中连接此 Hub"),
                fg=t.WARNING)
        self._user_rows = (list(directory.get("users") or [])
                           if directory_code == 200 else [])
        self.user_list.delete(0, "end")
        for user in self._user_rows:
            user_id = str(user.get("id") or user.get("user_id") or "")
            name = str(user.get("display_name") or user.get("name") or user_id or "Hub 用户")
            role = _ROLE.get(str(user.get("role") or ""), user.get("role") or "成员")
            status = _STATUS.get(str(user.get("status") or ""), user.get("status") or "")
            self.user_list.insert("end", f"{name} · {role} · {user_id} · {status}".rstrip(" ·"))
        if directory_code != 200:
            self.user_list.insert("end", "Hub 用户目录暂不可用；可手动输入用户 ID")
        if projects_code != 200:
            msg = self._friendly_error(projects_code, projects)
            self.status.configure(text=msg, fg=t.DANGER)
            self._rows = []
            self._set_project_action_states(None)
            self._clear_selected()
            self._render_projects(origin)
            return
        self._rows = list(result.get("rows") or [])
        pending_items = sum(_pending_item_count(row) for row in self._rows)
        self.project_overview.configure(
            text=f"{len(self._rows)} 个可访问项目 · {pending_items} 个项目待处理项")
        self._active_project_id = str(result.get("active") or "")
        self._selected_project_id = str(result.get("selected") or "")
        self._update_task_overview()
        self._render_projects(origin)
        selected_project = next((row for row in self._rows
                                 if str(row.get("id") or "") == self._selected_project_id), None)
        if selected_project and str(selected_project.get("status") or "") == "pending_claim":
            self._show_pending_claim(selected_project)
            self._set_project_action_states(None)
            self.selected_label.configure(
                text=(f"{_project_name(selected_project)} · 项目 ID：{selected_project.get('id')}\n"
                      "状态：等待认领 · 输入仍有效的认领码即可继续。"),
                fg=t.WARNING)
            self._clear_project_lists()
            if identity_code == 200:
                self.status.configure(text=f"已连接 Hub：{origin} · 待认领项目等待输入认领码",
                                      fg=t.WARNING)
            return
        if not selected_project:
            self._pending_claim = {}
            self.claim_label.configure(text="请选择待认领项目，或创建新项目。",
                                       fg=t.INK_MUTED)
        if selected_project:
            self._pending_claim = {}
        detail = result.get("detail")
        members = result.get("members")
        invites = result.get("invites")
        details_ok = True
        if self._selected_project_id and detail and members and invites:
            details_ok = self._render_selected({
                "origin": origin, "project_id": self._selected_project_id,
                "detail": detail, "members": members, "invites": invites})
        else:
            self._clear_selected()
        if identity_code == 200 and details_ok:
            self.status.configure(text=f"已连接 Hub：{origin} · {len(self._rows)} 个可访问项目",
                                  fg=t.SUCCESS)
        elif identity_code != 200 and details_ok:
            self.status.configure(text=self._friendly_error(identity_code, identity), fg=t.WARNING)

    def _render_projects(self, origin: str) -> None:
        if self.project_list is None:
            return
        self.project_list.delete(0, "end")
        active_id = (self._active_project_id if self._active_project_id is not None
                     else active_project_for_origin(origin))
        filter_value = (self.project_filter.get()
                        if getattr(self, "project_filter", None) is not None
                        else "全部项目")
        rows = self._rows
        if filter_value == "我负责":
            rows = [row for row in rows if str(row.get("role") or "") == "owner"]
        elif filter_value == "待处理":
            rows = [row for row in rows
                    if (_pending_item_count(row) > 0
                        or str(row.get("status") or "") in {"pending_claim", "planning"})]
        self._visible_rows = list(rows)
        for index, row in enumerate(self._visible_rows):
            pid = str(row.get("id") or "")
            status = _STATUS.get(str(row.get("status") or ""), row.get("status") or "未知状态")
            raw_role = row.get("role") or ("creator" if row.get("status") == "pending_claim" else "")
            role = _ROLE.get(str(raw_role), raw_role or "未知角色")
            if raw_role == "creator":
                role = "创建者"
            approvals = row.get("pending_approvals")
            approval_text = f" · 待处理 {approvals}" if approvals is not None else ""
            current = " · 当前派活" if active_id and active_id == pid else ""
            device_status = row.get("device_status")
            device_text = f" · 终端{_STATUS.get(str(device_status), device_status)}" if device_status else ""
            recent = row.get("recent_job") or {}
            recent_title = recent.get("title") or recent.get("name") or ""
            recent_text = f" · 最近任务：{recent_title}" if recent_title else ""
            self.project_list.insert(
                "end", f"{_project_name(row)} · {role} · {status}"
                f"{device_text}{approval_text}{recent_text}{current}")
            if pid == self._selected_project_id:
                self.project_list.selection_set(index)
                self.project_list.activate(index)
        if not self._visible_rows:
            self.project_list.insert(
                "end", "暂无可访问项目" if not self._rows else "当前筛选下暂无项目")

    def _update_task_overview(self, project: dict | None = None) -> None:
        if project is None:
            project = next((row for row in self._rows
                            if str(row.get("id") or "") == self._selected_project_id), None)
        recent = (project or {}).get("recent_job") or {}
        if not recent:
            self.task_overview.configure(
                text="Agent 任务的排队、运行、审批和结果见对话任务面板；Worker 派活与审批见项目 Worker 面板。")
            return
        status = str(recent.get("status") or "未知")
        status_text = {
            "queued": "排队中", "running": "运行中",
            "waiting_confirm": "等待确认", "completed": "已完成",
            "failed": "失败", "cancelled": "已取消",
        }.get(status, status)
        title = str(recent.get("title") or "Agent 任务")
        self.task_overview.configure(
            text=(f"最近 Agent 任务：{title} · {status_text}。完整任务列表见对话任务面板；"
                  "Worker 派活使用独立队列，状态不代表 Agent 正在运行。"))

    def _render_selected(self, result: dict) -> bool:
        origin = str(result.get("origin") or "")
        project_id = str(result.get("project_id") or "")
        detail_code, detail_data = result.get("detail") or (0, {})
        members_code, members_data = result.get("members") or (0, {})
        invites_code, invite_data = result.get("invites") or (0, {})
        can_view, can_manage, error_code = project_access_from_results(
            detail_code, members_code, invites_code)
        if not can_view:
            self._set_management_enabled(False)
            self._set_project_action_states(None)
            failed = next(((code, data) for code, data in
                           ((detail_code, detail_data), (members_code, members_data),
                            (invites_code, invite_data)) if code == error_code), (0, {}))
            message = self._friendly_error(*failed)
            self._member_rows = []
            self._invite_rows = []
            self.member_list.delete(0, "end")
            self.invite_list.delete(0, "end")
            self.status.configure(text=message, fg=t.DANGER)
            return False
        summary = next((row for row in self._rows
                        if str(row.get("id") or "") == project_id), {})
        project = dict(summary)
        project.update(detail_data.get("project") or detail_data)
        self._selected_project_detail = dict(project)
        if not project.get("role"):
            project["role"] = summary.get("role")
        archived = str(project.get("status") or "") == "archived"
        manager_access = can_manage
        if archived:
            can_manage = False
            self._close_worker_windows_for(project_id)
        self._set_management_enabled(can_manage)
        self._set_project_action_states(project)
        self._selected_project_id = project_id
        self._update_task_overview(project)
        approvals = project.get("pending_approvals")
        approval_text = f"待审批：{approvals}" if approvals is not None else "审批状态可在下方审批面板查看"
        access_text = (" · 已归档，只读" if archived else
                       ("" if manager_access else " · 当前角色仅可查看项目与成员"))
        self.selected_label.configure(
            text=(f"{_project_name(project)} · 项目 ID：{project_id}\n"
                  f"角色：{_ROLE.get(str(project.get('role') or ''), project.get('role') or '未知')} · "
                  f"状态：{_STATUS.get(str(project.get('status') or ''), project.get('status') or '未知')} · "
                  f"{approval_text}{access_text}"), fg=t.INK_SOFT)
        self._invite_rows = list(invite_data.get("invites") or []) if can_manage else []
        self.invite_list.delete(0, "end")
        for invite in self._invite_rows:
            target = invite.get("user_id") or invite.get("terminal_display_code") or "指定成员"
            status = _STATUS.get(str(invite.get("status") or ""), invite.get("status") or "未知")
            self.invite_list.insert("end", f"邀请 · {status} · {target} · {_ROLE.get(str(invite.get('role') or ''), invite.get('role') or '')}")
        self._member_rows = []
        self.member_list.delete(0, "end")
        for member in (members_data.get("members") or []):
            base = {"kind": "member", **member}
            self._member_rows.append(base)
            label = (f"成员 · {_ROLE.get(str(member.get('role') or ''), member.get('role') or '')} · "
                     f"{member.get('display_name') or member.get('name') or member.get('user_id')} · "
                     f"{_STATUS.get(str(member.get('status') or ''), member.get('status') or '')}")
            self.member_list.insert("end", label)
            for device in (member.get("devices") or []):
                device_row = {"kind": "device", **device,
                              "user_id": member.get("user_id")}
                self._member_rows.append(device_row)
                self.member_list.insert("end", f"    终端 · {device.get('display_code') or device.get('name') or device.get('device_id')} · {_STATUS.get(str(device.get('status') or ''), device.get('status') or '')}")
        self._render_projects(origin)
        return True

    def _clear_selected(self) -> None:
        self._selected_project_detail = {}
        self._set_management_enabled(False)
        self._set_project_action_states(None)
        self.selected_label.configure(text="请选择项目查看成员、邀请与审批状态。",
                                      fg=t.INK_MUTED)
        self._member_rows = []
        self._invite_rows = []
        self.member_list.delete(0, "end")
        self.invite_list.delete(0, "end")

    def _show_secret_once(self, title: str, record: dict, secret: str, help_text: str) -> None:
        window = tk.Toplevel(self)
        window.title(title)
        window.configure(bg=t.CANVAS)
        window.transient(self)
        window.resizable(False, False)
        body = tk.Frame(window, bg=t.CANVAS, padx=t.s(22), pady=t.s(18))
        body.pack(fill="both", expand=True)
        name = _project_name(record) if record.get("project_id") or record.get("name") or record.get("title") else "项目邀请"
        tk.Label(body, text=title, bg=t.CANVAS, fg=t.INK,
                 font=self.fonts.display_md).pack(anchor="w")
        context = (f"{name} · {record.get('project_id') or record.get('id') or ''}\n"
                   f"用户：{record.get('user_id') or '—'}    终端：{record.get('terminal_display_code') or '—'}\n"
                   f"角色：{_ROLE.get(str(record.get('role') or ''), record.get('role') or '—')}\n"
                   f"{help_text}")
        tk.Label(body, text=context,
                 bg=t.CANVAS, fg=t.INK_SOFT, font=self.fonts.caption,
                 justify="left", wraplength=t.s(570)).pack(anchor="w", pady=(t.s(7), t.s(12)))
        code = MinimalField(body, font=self.fonts.body, value=secret,
                            placeholder="一次性代码", bg=t.SURFACE)
        code.pack(fill="x", pady=(0, t.s(12)))
        row = tk.Frame(body, bg=t.CANVAS)
        row.pack(fill="x")

        def copy():
            window.clipboard_clear()
            window.clipboard_append(secret)
            window.update_idletasks()
            self.app.toast("代码已复制；请仅通过可信渠道传递")

        FlatButton(row, "复制", copy, font=self.fonts.small, variant="primary",
                   height=34, parent_bg=t.CANVAS).pack(side="left", padx=(0, t.s(7)))
        FlatButton(row, "完成", window.destroy, font=self.fonts.small,
                   variant="ghost", height=34, parent_bg=t.CANVAS).pack(side="left")

    @staticmethod
    def _friendly_error(code: int, data: dict) -> str:
        detail = str((data or {}).get("detail") or "") if isinstance(data, dict) else ""
        if code == 0:
            return f"无法连接 Hub：{detail or '检查 Tailscale 在线状态和 HTTPS 地址'}"
        if code == 401:
            return "Hub 设备凭证失效或本机尚未注册；请在“团队与成员”中连接并注册本机。"
        if code == 403:
            return detail or "当前用户或终端没有此项目操作权限。"
        if code == 404:
            return detail or "项目或邀请不存在，或当前终端无权查看。"
        if code == 409:
            return detail or "操作与项目当前状态冲突，请刷新后重试。"
        if code == 410:
            return detail or "代码已过期、撤销或使用；请负责人重新创建。"
        if code == 429:
            return detail or "认领尝试次数已达上限，请联系项目创建者。"
        if code in (301, 302, 303, 307, 308):
            return "Hub 地址发生跳转，已阻止凭证跨源发送；请使用正确的 HTTPS 地址。"
        return detail or f"Hub 请求失败（HTTP {code}）。"

    def destroy(self) -> None:
        self._destroying = True
        try:
            self.after_cancel(self._poll_job)
        except (tk.TclError, AttributeError):
            pass
        super().destroy()


def open_project_hub(parent, app, fonts, client: ApiClient, *,
                     initial_name: str = "", initial_description: str = "") -> ProjectHubWindow:
    return ProjectHubWindow(parent, app, fonts, client,
                            initial_name=initial_name,
                            initial_description=initial_description)
