"""Project Worker task request and vote window."""

from __future__ import annotations

import json
import queue
import threading
import tkinter as tk

from . import theme as t
from .project_api import ProjectHubApi
from .widgets import FlatButton, ScrollArea


def project_worker_writes_enabled(project: dict) -> bool:
    return str(project.get("status") or "") == "active"


def render_call_detail(widget, call: dict | None = None) -> None:
    """Replace call details without leaving the text control editable."""
    widget.configure(state=tk.NORMAL)
    widget.delete("1.0", "end")
    if call:
        widget.insert("1.0", json.dumps(call, ensure_ascii=False, indent=2))
    widget.configure(state=tk.DISABLED)


class WorkerTaskWindow(tk.Toplevel):
    def __init__(self, parent, fonts, client, project: dict) -> None:
        super().__init__(parent)
        self.fonts = fonts
        self.api = ProjectHubApi(client)
        self.project = dict(project)
        self.project_id = str(project.get("id") or "")
        self._results: queue.Queue = queue.Queue()
        self._busy = False
        self._closed = False
        self._project_writes_enabled = False
        self._devices: list[dict] = []
        self._calls: list[dict] = []
        self._poll_id: str | None = None

        self.title("项目 Worker 任务与审批")
        self.configure(bg=t.CANVAS)
        self.geometry("860x760")
        self.minsize(700, 620)
        self.transient(parent)
        canvas = ScrollArea(self, bg=t.CANVAS, scrollbar=True)
        canvas.pack(fill="both", expand=True, padx=t.s(18), pady=t.s(16))
        body = canvas.inner
        tk.Label(body, text="项目 Worker 任务与审批", bg=t.CANVAS, fg=t.INK,
                 font=fonts.display_md).pack(anchor="w")
        tk.Label(body,
                 text=f"项目：{project.get('name') or project.get('title') or self.project_id}"
                      f" · {self.project_id}\n"
                      "仅向主动授权的终端派发文件操作；写入需两名非发起人批准，"
                      "终端所有者仍需逐次确认。",
                 bg=t.CANVAS, fg=t.INK_SOFT, font=fonts.caption,
                 justify="left", wraplength=t.s(790)).pack(anchor="w", pady=(t.s(7), t.s(10)))
        self.status = tk.Label(body, text="点击刷新以查看项目终端和调用。",
                               bg=t.CANVAS, fg=t.INK_MUTED, font=fonts.body,
                               justify="left", wraplength=t.s(790))
        self.status.pack(anchor="w", pady=(0, t.s(8)))

        tk.Label(body, text="目标终端", bg=t.CANVAS, fg=t.INK,
                 font=fonts.body_medium).pack(anchor="w")
        self.device_list = self._list(body, 4)
        self.title_var = tk.StringVar(value="")
        self.path_var = tk.StringVar(value="")
        self.tool_var = tk.StringVar(value="workspace.list")
        self._entry(body, "任务标题", self.title_var)
        self._entry(body, "工作区相对路径", self.path_var)
        tool_row = tk.Frame(body, bg=t.CANVAS)
        tool_row.pack(fill="x", pady=t.s(4))
        tk.Label(tool_row, text="文件工具", width=18, anchor="w", bg=t.CANVAS,
                 fg=t.INK_SOFT, font=fonts.caption).pack(side="left")
        tk.OptionMenu(tool_row, self.tool_var, "workspace.list", "workspace.read",
                      "workspace.write").pack(side="left", fill="x", expand=True)
        tk.Label(body, text="新建文件内容（仅 workspace.write 使用）",
                 bg=t.CANVAS, fg=t.INK_SOFT, font=fonts.caption).pack(anchor="w", pady=(t.s(6), 0))
        self.content = tk.Text(body, height=5, wrap="word", bg=t.SURFACE,
                               fg=t.INK, relief="flat", font=fonts.body)
        self.content.pack(fill="x", pady=(t.s(4), t.s(7)))
        self.create_button = FlatButton(body, "提交 Worker 任务", self.create_task,
                                        font=fonts.small, variant="primary", height=34,
                                        parent_bg=t.CANVAS)
        self.create_button.pack(anchor="w", pady=(0, t.s(12)))

        tk.Label(body, text="项目 Worker 调用与审批", bg=t.CANVAS, fg=t.INK,
                 font=fonts.body_medium).pack(anchor="w")
        self.call_list = self._list(body, 6)
        self.call_list.bind("<<ListboxSelect>>", self._selected_call_changed)
        self.detail = tk.Text(body, height=11, wrap="word", bg=t.SURFACE,
                              fg=t.INK, relief="flat", font=fonts.small)
        self.detail.pack(fill="x", pady=(t.s(5), t.s(8)))
        self.detail.configure(state=tk.DISABLED)
        actions = tk.Frame(body, bg=t.CANVAS)
        actions.pack(fill="x", pady=(0, t.s(16)))
        self.mutation_buttons = []
        for label, action, variant in (
            ("刷新", self.refresh, "outline"),
            ("批准所选调用", lambda: self.vote("approve"), "primary"),
            ("拒绝所选调用", lambda: self.vote("reject"), "outline"),
        ):
            button = FlatButton(actions, label, action, font=fonts.small,
                                variant=variant, height=34, parent_bg=t.CANVAS)
            button.pack(side="left", padx=(0, t.s(7)))
            if label != "刷新":
                self.mutation_buttons.append(button)
        self._apply_project_writability()
        self._poll_id = self.after(80, self._poll)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.refresh()

    def _list(self, parent, height: int) -> tk.Listbox:
        box = tk.Listbox(parent, height=height, bg=t.SURFACE, fg=t.INK,
                         selectbackground=t.TERRACOTTA_SOFT,
                         selectforeground=t.INK, relief="flat", font=self.fonts.small)
        box.pack(fill="x", pady=(t.s(4), t.s(9)))
        return box

    def _entry(self, parent, label: str, variable: tk.StringVar) -> None:
        row = tk.Frame(parent, bg=t.CANVAS)
        row.pack(fill="x", pady=t.s(4))
        tk.Label(row, text=label, width=18, anchor="w", bg=t.CANVAS,
                 fg=t.INK_SOFT, font=self.fonts.caption).pack(side="left")
        tk.Entry(row, textvariable=variable, bg=t.SURFACE, fg=t.INK,
                 relief="flat", font=self.fonts.body).pack(side="left", fill="x", expand=True)

    def _request(self, action: str, work) -> None:
        if self._busy:
            self.status.configure(text="正在等待上一次 Hub 请求完成…", fg=t.WARNING)
            return
        self._busy = True
        self.status.configure(text="正在连接 Hub…", fg=t.INK_MUTED)

        def run() -> None:
            try:
                result = work()
            except Exception as exc:
                result = (0, {"detail": str(exc)})
            self._results.put((action, result))

        threading.Thread(target=run, daemon=True, name=f"venus-worker-{action}").start()

    def _apply_project_writability(self) -> None:
        enabled = (self._project_writes_enabled
                   and str(self.project.get("role") or "") != "viewer")
        self.create_button.set_enabled(enabled)
        for button in self.mutation_buttons:
            button.set_enabled(enabled)

    def refresh(self) -> None:
        self._project_writes_enabled = False
        self._apply_project_writability()
        self.call_list.selection_clear(0, "end")
        render_call_detail(self.detail)

        def work():
            project = self.api.project_detail(self.project_id)
            members = self.api.list_members(self.project_id)
            calls = self.api.list_worker_calls(self.project_id)
            return project, members, calls
        self._request("refresh", work)

    def _selected_device(self) -> dict | None:
        selected = self.device_list.curselection()
        return self._devices[selected[0]] if selected and selected[0] < len(self._devices) else None

    def _selected_call(self) -> dict | None:
        selected = self.call_list.curselection()
        return self._calls[selected[0]] if selected and selected[0] < len(self._calls) else None

    def create_task(self) -> None:
        if not self._project_writes_enabled:
            self.status.configure(text="项目状态尚未确认或已归档，不能派发 Worker 任务。",
                                  fg=t.WARNING)
            return
        if str(self.project.get("role") or "") == "viewer":
            self.status.configure(text="只读成员不能派发 Worker 任务", fg=t.DANGER)
            return
        device = self._selected_device()
        if not device:
            self.status.configure(text="请先选择目标项目终端", fg=t.DANGER)
            return
        tool = self.tool_var.get()
        path = self.path_var.get().strip()
        if tool in {"workspace.read", "workspace.write"} and not path:
            self.status.configure(text="此工具需要工作区相对路径", fg=t.DANGER)
            return
        args = ({"path": path} if tool != "workspace.write" else
                {"path": path, "content": self.content.get("1.0", "end-1c")})
        title = self.title_var.get().strip() or f"Worker {tool} {path}".strip()
        self._request("create", lambda: self.api.create_worker_task(
            self.project_id, str(device["device_id"]), tool, args, title))

    def vote(self, decision: str) -> None:
        if not self._project_writes_enabled:
            self.status.configure(text="项目状态尚未确认或已归档，不能审批 Worker 调用。",
                                  fg=t.WARNING)
            return
        if str(self.project.get("role") or "") == "viewer":
            self.status.configure(text="只读成员不能参与 Worker 审批", fg=t.DANGER)
            return
        call = self._selected_call()
        if not call:
            self.status.configure(text="请先选择 Worker 调用", fg=t.DANGER)
            return
        if call.get("status") != "awaiting_approval":
            self.status.configure(text="此调用当前不接受投票", fg=t.WARNING)
            return
        self._request("vote", lambda: self.api.vote_worker_call(
            str(call["call_id"]), decision))

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            action, result = self._results.get_nowait()
        except queue.Empty:
            pass
        else:
            self._busy = False
            if action == "refresh":
                self.call_list.selection_clear(0, "end")
                render_call_detail(self.detail)
                if not (isinstance(result, tuple) and len(result) == 3
                        and all(isinstance(item, tuple) and len(item) == 2
                                for item in result)):
                    code, data = result if isinstance(result, tuple) and len(result) == 2 else (0, {})
                    self._project_writes_enabled = False
                    self._apply_project_writability()
                    self.status.configure(text=f"Hub HTTP {code}："
                                          f"{data.get('detail') or '无法刷新'}", fg=t.DANGER)
                    self._poll_id = self.after(80, self._poll)
                    return
                (project_code, project_data), (member_code, members), (call_code, calls) = result
                if project_code == 200:
                    project = project_data.get("project") or project_data
                    self.project.update(project)
                    self._project_writes_enabled = project_worker_writes_enabled(project)
                    self._apply_project_writability()
                else:
                    self._project_writes_enabled = False
                    self._apply_project_writability()
                if member_code == 200 and call_code == 200:
                    self._devices = [dict(device, user_id=member.get("user_id"))
                                     for member in members.get("members") or []
                                     for device in member.get("devices") or []
                                     if device.get("status") == "active"]
                    self.device_list.delete(0, "end")
                    for device in self._devices:
                        self.device_list.insert("end", f"{device.get('name') or '终端'} · "
                                                f"{device.get('display_code')} · "
                                                f"{device.get('device_id')}")
                    self._calls = list(calls.get("calls") or [])
                    self.call_list.delete(0, "end")
                    for call in self._calls:
                        self.call_list.insert(
                            "end", f"{call.get('tool')} · {call.get('status')} · "
                                   f"批准 {call.get('approvals')}/{call.get('required_approvals')}"
                                   f" · {call.get('call_id')}")
                    if (project_code == 200
                            and str(self.project.get("status") or "") == "archived"):
                        self.status.configure(text="项目已归档；Worker 派发与审批入口已禁用。",
                                              fg=t.WARNING)
                    elif project_code == 200 and not self._project_writes_enabled:
                        self.status.configure(text="当前项目状态仅允许查看。",
                                              fg=t.WARNING)
                    elif project_code == 200:
                        self.status.configure(text="项目终端与 Worker 调用已刷新",
                                              fg=t.SUCCESS)
                    else:
                        self.status.configure(text="项目状态无法确认；Worker 写入入口已禁用。",
                                              fg=t.WARNING)
                else:
                    failed = (member_code, members) if member_code != 200 else (call_code, calls)
                    detail = str(failed[1].get("detail") or "无法读取")
                    if (project_code == 200
                            and str(self.project.get("status") or "") == "archived"
                            and call_code == 403):
                        message = ("项目已归档；Worker 派发与审批已禁用。"
                                   "Hub 当前拒绝读取归档项目的 Worker 调用历史。")
                        self.status.configure(text=message, fg=t.WARNING)
                    else:
                        self.status.configure(text=f"Hub HTTP {failed[0]}：{detail}",
                                              fg=t.DANGER)
            else:
                code, data = result
                if code == 200:
                    if action == "create":
                        call = data.get("call") or {}
                        self.status.configure(
                            text=f"Worker 任务已提交：{call.get('call_id')}；等待项目审批。",
                            fg=t.SUCCESS)
                    else:
                        self.status.configure(text="Worker 审批票已记录", fg=t.SUCCESS)
                    self.refresh()
                else:
                    self.status.configure(text=f"Hub HTTP {code}："
                                          f"{data.get('detail') or '操作失败'}", fg=t.DANGER)
        self._poll_id = self.after(80, self._poll)

    def _selected_call_changed(self, _event=None) -> None:
        render_call_detail(self.detail, self._selected_call())

    def destroy(self) -> None:
        self._closed = True
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
        super().destroy()


def open_worker_tasks(parent, fonts, client, project: dict) -> WorkerTaskWindow:
    return WorkerTaskWindow(parent, fonts, client, project)
