"""Local, opt-in Remote Worker control for one Hub project."""

from __future__ import annotations

import hashlib
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import remote_worker as worker

from . import theme as t
from .config_store import team_connection, team_token_for_connection
from .widgets import FlatButton


class WorkerControlWindow(tk.Toplevel):
    def __init__(self, parent, fonts, *, origin: str, project: dict) -> None:
        super().__init__(parent)
        self.fonts = fonts
        self.origin = origin
        self.project = dict(project)
        self.project_id = str(project.get("id") or "")
        self._closed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._updates: queue.Queue[str] = queue.Queue()
        self._prompts: queue.Queue[tuple[dict, dict, threading.Event, dict]] = queue.Queue()
        self._poll_id: str | None = None

        self.title("本机 Worker 授权")
        self.configure(bg=t.CANVAS)
        self.geometry("780x520")
        self.minsize(650, 450)
        self.transient(parent)
        body = tk.Frame(self, bg=t.CANVAS, padx=t.s(22), pady=t.s(20))
        body.pack(fill="both", expand=True)
        tk.Label(body, text="本机 Worker 授权", bg=t.CANVAS, fg=t.INK,
                 font=fonts.display_md).pack(anchor="w")
        name = str(project.get("name") or project.get("title") or self.project_id)
        tk.Label(body, text=f"Hub：{origin}\n项目：{name} · {self.project_id}",
                 bg=t.CANVAS, fg=t.INK_SOFT, font=fonts.body,
                 justify="left", wraplength=t.s(710)).pack(anchor="w", pady=(t.s(8), t.s(12)))
        tk.Label(body,
                 text="本机主动连接 Hub，仅开放所选目录和工具。命令、屏幕、键鼠控制不支持。"
                      "新建文件需要项目多票审批和本机逐次确认。关闭此窗口会停止轮询。",
                 bg=t.CANVAS, fg=t.INK_MUTED, font=fonts.caption,
                 justify="left", wraplength=t.s(710)).pack(anchor="w", pady=(0, t.s(12)))

        self.workspace_var = tk.StringVar(value="")
        self.expiry_var = tk.StringVar(value="8")
        self.tools = {
            "workspace.list": tk.BooleanVar(value=True),
            "workspace.read": tk.BooleanVar(value=True),
            "workspace.write": tk.BooleanVar(value=False),
        }
        self._field(body, "授权工作目录", self.workspace_var, choose_directory=True)
        self._field(body, "授权小时数（1–720）", self.expiry_var)
        tk.Label(body, text="允许的工具", bg=t.CANVAS, fg=t.INK_SOFT,
                 font=fonts.caption).pack(anchor="w", pady=(t.s(8), t.s(2)))
        names = {"workspace.list": "列举目录", "workspace.read": "读取 UTF-8 文本",
                 "workspace.write": "新建文件（每次本机确认）"}
        tool_row = tk.Frame(body, bg=t.CANVAS)
        tool_row.pack(fill="x")
        for tool, variable in self.tools.items():
            tk.Checkbutton(tool_row, text=names[tool], variable=variable,
                           bg=t.CANVAS, fg=t.INK, selectcolor=t.SURFACE,
                           activebackground=t.CANVAS, font=fonts.caption).pack(
                               side="left", padx=(0, t.s(14)))

        actions = tk.Frame(body, bg=t.CANVAS)
        actions.pack(fill="x", pady=(t.s(14), t.s(9)))
        for label, action, variant in (
            ("明确授权并启动", self.authorize_and_start, "primary"),
            ("紧急停止", self.stop, "outline"),
            ("撤销此项目本机授权", self.revoke, "outline"),
        ):
            FlatButton(actions, label, action, font=fonts.small,
                       variant=variant, height=34, parent_bg=t.CANVAS).pack(
                           side="left", padx=(0, t.s(7)))
        self.state_label = tk.Label(body, text="Worker 未运行", bg=t.CANVAS,
                                    fg=t.INK_MUTED, font=fonts.body,
                                    justify="left", wraplength=t.s(710))
        self.state_label.pack(anchor="w", pady=(t.s(8), 0))
        self._show_saved_status()
        self._poll_id = self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _field(self, parent, label: str, variable: tk.StringVar, *,
               choose_directory: bool = False) -> None:
        row = tk.Frame(parent, bg=t.CANVAS)
        row.pack(fill="x", pady=t.s(4))
        tk.Label(row, text=label, width=21, anchor="w", bg=t.CANVAS,
                 fg=t.INK_SOFT, font=self.fonts.caption).pack(side="left")
        tk.Entry(row, textvariable=variable, bg=t.SURFACE, fg=t.INK,
                 relief="flat", font=self.fonts.body).pack(side="left", fill="x", expand=True)
        if choose_directory:
            tk.Button(row, text="选择目录…", command=self._choose_workspace,
                      bg=t.SURFACE_ALT, fg=t.INK, relief="flat",
                      font=self.fonts.small, padx=t.s(9)).pack(
                          side="left", padx=(t.s(6), 0))

    def _choose_workspace(self) -> None:
        initial = self.workspace_var.get().strip()
        initial_path = Path(initial).expanduser() if initial else None
        selected = filedialog.askdirectory(
            parent=self, title="选择本机 Worker 授权工作目录",
            initialdir=(str(initial_path) if initial_path and initial_path.is_dir()
                        else None), mustexist=True)
        if selected:
            self.workspace_var.set(selected)

    def _show_saved_status(self) -> None:
        try:
            status = worker.local_worker_status()
            rows = [row for row in status["grants"]
                    if row.get("origin") == self.origin
                    and row.get("project_id") == self.project_id]
            if rows:
                row = rows[-1]
                state = "已停止" if status["stopped"] else "已保存授权，尚未运行"
                if not row.get("active"):
                    state = "本机授权已过期"
                self.state_label.configure(
                    text=f"{state} · 目录：{row.get('workspace')} · 工具："
                         f"{', '.join(row.get('tools') or [])}", fg=t.INK_SOFT)
        except worker.RemoteWorkerError as exc:
            self.state_label.configure(text=str(exc), fg=t.DANGER)

    def authorize_and_start(self) -> None:
        if self._thread and self._thread.is_alive():
            self.state_label.configure(text="Worker 已在本机运行", fg=t.SUCCESS)
            return
        connection = team_connection(self.origin)
        team_id = str(connection.get("team_id") or "") if connection else ""
        if not team_id or not self.project_id:
            self.state_label.configure(text="请先完成 Hub 连接并选择一个已加入的项目", fg=t.DANGER)
            return
        selected = [name for name, value in self.tools.items() if value.get()]
        try:
            hours = int(self.expiry_var.get().strip())
        except ValueError:
            hours = 0
        if not 1 <= hours <= 720 or not selected:
            self.state_label.configure(text="请选择工具，并设置 1–720 小时的授权期限", fg=t.DANGER)
            return
        workspace = self.workspace_var.get().strip()
        if not workspace:
            self.state_label.configure(text="请选择本机工作目录", fg=t.DANGER)
            return
        exact_scope = (f"Hub：{self.origin}\n项目：{self.project_id}\n目录：{workspace}\n"
                       f"工具：{', '.join(selected)}\n有效期：{hours} 小时\n\n"
                       "确定在本机授权并启动 Worker？")
        if not messagebox.askyesno("确认本机 Worker 授权", exact_scope, parent=self):
            return
        try:
            record = worker.authorize_local_grant(
                self.origin, team_id, self.project_id, workspace,
                selected, hours * 3600, consent=True)
        except (worker.RemoteWorkerError, OSError) as exc:
            self.state_label.configure(text=f"本机授权失败：{exc}", fg=t.DANGER)
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, args=(record,),
                                        daemon=True, name="venus-remote-worker-ui")
        self._thread.start()
        self.state_label.configure(text="Worker 已启动，正在主动连接 Hub…", fg=t.SUCCESS)

    def _run_loop(self, grant: dict) -> None:
        stop_reason = ""
        while not self._stop.is_set() and not worker.is_worker_stopped():
            try:
                token = team_token_for_connection(self.origin, str(grant["team_id"]))
                client = worker.HubClient(self.origin, token)
                worked = worker.process_once(
                    grant, client, confirm=self._confirm_write,
                    stop_check=lambda: self._stop.is_set() or worker.is_worker_stopped())
                if worked:
                    self._updates.put("一次 Worker 调用已完成；继续等待项目任务。")
            except worker.RemoteWorkerError as exc:
                message = str(exc)
                self._updates.put(message)
                if ("HTTP 401" in message or "HTTP 403" in message
                        or "授权已过期" in message or "已撤销" in message):
                    stop_reason = message
                    break
            if self._stop.wait(3):
                break
        suffix = f"（原因：{stop_reason}）" if stop_reason else ""
        self._updates.put(f"Worker 轮询已停止{suffix}。")

    def _confirm_write(self, call: dict, args: dict) -> bool:
        event = threading.Event()
        result: dict = {"approved": False}
        self._prompts.put((call, args, event, result))
        deadline = time.monotonic() + 90
        while not event.wait(0.2):
            if self._stop.is_set() or self._closed or time.monotonic() >= deadline:
                return False
        return bool(result["approved"])

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            while True:
                self.state_label.configure(text=self._updates.get_nowait(), fg=t.INK_SOFT)
        except queue.Empty:
            pass
        try:
            call, args, event, result = self._prompts.get_nowait()
        except queue.Empty:
            pass
        else:
            content = str(args.get("content") or "")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            message = (f"来源 Hub：{self.origin}\n项目：{call.get('project_id')}\n"
                       f"任务：{call.get('job_id')}\n调用：{call.get('call_id')}\n"
                       f"新建文件：{args.get('path')}\n内容字节数：{len(content.encode('utf-8'))}\n"
                       f"SHA-256：{digest}\n\n内容预览：\n{content[:1800]}"
                       + ("\n…（其余内容未显示）" if len(content) > 1800 else "")
                       + "\n\n允许本次新建文件？")
            try:
                result["approved"] = messagebox.askyesno(
                    "本机确认 Worker 写入", message, parent=self)
            finally:
                event.set()
        self._poll_id = self.after(100, self._poll)

    def stop(self) -> None:
        self._stop.set()
        try:
            worker.stop_worker()
        except OSError as exc:
            self.state_label.configure(text=f"本机轮询已请求停止，但无法写入紧急停止标记：{exc}",
                                       fg=t.DANGER)
            return
        self.state_label.configure(text="已紧急停止本机 Worker。", fg=t.WARNING)

    def revoke(self) -> None:
        if not messagebox.askyesno("撤销本机授权",
                                   f"撤销 {self.project_id} 的所有本机 Worker 授权？",
                                   parent=self):
            return
        self.stop()
        try:
            count = worker.revoke_local_grant(self.project_id, origin=self.origin)
        except (worker.RemoteWorkerError, OSError) as exc:
            self.state_label.configure(text=f"撤销本机授权失败：{exc}", fg=t.DANGER)
            return
        self.state_label.configure(text=f"已撤销 {count} 条本机授权。", fg=t.WARNING)

    def destroy(self) -> None:
        self._closed = True
        self._stop.set()
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
        super().destroy()


def open_worker_control(parent, fonts, *, origin: str,
                        project: dict) -> WorkerControlWindow:
    return WorkerControlWindow(parent, fonts, origin=origin, project=project)
