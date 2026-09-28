"""Multi-party governance proposal UI for a single Hub project."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
import uuid
from tkinter import messagebox

from . import theme as t
from .project_api import ProjectHubApi
from .widgets import FlatButton


_ACTIONS = {
    "promote_admin": "提升为管理员",
    "lower_approval_threshold": "降低审批阈值",
    "owner_transfer": "转移项目所有权",
}
_STATUS = {
    "pending": "待审批", "open": "待审批", "awaiting_votes": "待审批",
    "approved": "已获批", "rejected": "已拒绝", "expired": "已过期",
    "applied": "已执行", "stale": "已失效", "accepted": "已完成",
    "completed": "已完成", "cancelled": "已取消",
}
_VOTER_ROLES = {"owner", "admin", "member"}


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def governance_voter_is_eligible(members: list[dict], user_id: str,
                                 proposer_user_id: str = "") -> bool:
    user_id = str(user_id or "")
    if not user_id or user_id == str(proposer_user_id or ""):
        return False
    for member in members:
        if not isinstance(member, dict):
            continue
        member_id = str(member.get("user_id") or member.get("id") or "")
        if (member_id != user_id or str(member.get("status") or "") != "active"
                or str(member.get("role") or "") not in _VOTER_ROLES):
            continue
        if any(str(device.get("status") or "") == "active"
               for device in member.get("devices") or [] if isinstance(device, dict)):
            return True
    return False


def governance_eligible_voter_count(members: list[dict],
                                    proposer_user_id: str = "") -> int:
    voter_ids = {
        str(member.get("user_id") or member.get("id") or "")
        for member in members if isinstance(member, dict)
    }
    return sum(governance_voter_is_eligible(members, user_id, proposer_user_id)
               for user_id in voter_ids)


def governance_can_propose(project: dict, *, read_only: bool = False) -> bool:
    return (not read_only and str(project.get("status") or "") == "active"
            and str(project.get("role") or "") == "owner")


def governance_can_vote(project: dict, proposal: dict, members: list[dict],
                        current_user_id: str, *, read_only: bool = False) -> bool:
    proposer_user_id = str(proposal.get("proposer_user_id") or "")
    return (not read_only and str(project.get("status") or "") == "active"
            and bool(proposer_user_id)
            and str(proposal.get("status") or "") in {"pending", "open", "awaiting_votes"}
            and governance_voter_is_eligible(
                members, current_user_id, proposer_user_id))


def governance_can_accept_transfer(project: dict, transfer: dict,
                                   current_user_id: str, *,
                                   read_only: bool = False) -> bool:
    return (not read_only and str(project.get("status") or "") == "active"
            and bool(current_user_id)
            and bool(transfer.get("transfer_id"))
            and str(transfer.get("to_user_id") or "") == str(current_user_id))


def governance_required_votes(project: dict) -> int:
    policy = project.get("policy") or {}
    threshold = _as_int(project.get("required_approvals"),
                        _as_int(policy.get("required_approvals"), 0))
    return max(2, threshold) if threshold > 0 else 0


def governance_capacity_message(project: dict, members: list[dict],
                                proposer_user_id: str, required_votes: int | None = None) -> str:
    declared = _as_int(required_votes, 0) if required_votes is not None else 0
    needed = max(2, declared or governance_required_votes(project))
    threshold_unknown = declared < 1 and governance_required_votes(project) < 1
    eligible = governance_eligible_voter_count(members, proposer_user_id)
    if eligible >= needed:
        return ("当前项目审批阈值尚未读取；提案至少需要 2 位非发起人审批人，"
                "实际票数要求以服务端提案为准。" if threshold_unknown else "")
    return (f"当前只有 {eligible} 位合格的非发起人投票成员；此提案至少需要 "
            f"{needed} 票，按当前成员构成无法达到。")


def build_governance_proposal_payload(action: str, request_id: str, *,
                                      target_user_id: str = "",
                                      required_approvals: int | None = None,
                                      current_threshold: int | None = None) -> dict:
    if action not in _ACTIONS:
        raise ValueError("请选择有效的治理提案类型")
    body = {"action": action, "request_id": str(request_id)}
    if not body["request_id"]:
        raise ValueError("提案请求 ID 缺失")
    if action in {"promote_admin", "owner_transfer"}:
        if not str(target_user_id or ""):
            raise ValueError("请选择目标项目成员")
        body["target_user_id"] = str(target_user_id)
    if action == "lower_approval_threshold":
        proposed = _as_int(required_approvals, 0)
        current = _as_int(current_threshold, 0)
        if current < 1:
            raise ValueError("无法读取当前审批阈值，暂时不能提交降低阈值提案")
        if proposed < 1 or proposed >= current:
            raise ValueError("新审批阈值必须为正数且低于当前阈值")
        body["required_approvals"] = proposed
    return body


def unwrap_governance_proposal(data: dict) -> dict:
    proposal = data.get("proposal") if isinstance(data, dict) else None
    return proposal if isinstance(proposal, dict) else (data if isinstance(data, dict) else {})


def unwrap_governance_proposals(data: dict) -> list[dict]:
    rows = data.get("proposals") if isinstance(data, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def governance_proposal_summary(proposal: dict, members: list[dict]) -> dict[str, str]:
    action = str(proposal.get("action") or "")
    params = proposal.get("parameters") or {}
    target_id = str(proposal.get("target_user_id") or params.get("target_user_id") or "")
    target_name = next((str(member.get("display_name") or member.get("name") or target_id)
                        for member in members if isinstance(member, dict)
                        and str(member.get("user_id") or member.get("id") or "") == target_id),
                       target_id or "未指定成员")
    if action == "promote_admin":
        description = f"将 {target_name} 提升为管理员"
    elif action == "lower_approval_threshold":
        value = (params.get("required_approvals") or params.get("target_required_approvals")
                 or params.get("value") or proposal.get("required_approvals"))
        description = f"将项目审批阈值降低至 {value} 票"
    elif action == "owner_transfer":
        description = f"将项目所有权转移给 {target_name}"
    else:
        description = str(proposal.get("summary") or action or "治理提案")
    status_key = str(proposal.get("status") or "unknown")
    approvals = max(0, _as_int(proposal.get("approvals"), 0))
    required = max(1, _as_int(proposal.get("required_approvals"), 1))
    return {
        "description": description,
        "status": _STATUS.get(status_key, status_key),
        "votes": f"赞成 {approvals}/{required}",
        "created_at": str(proposal.get("created_at") or "—"),
        "expires_at": str(proposal.get("expires_at") or "—"),
    }


class GovernanceRequestIdCache:
    """Reuse an idempotency key while the form represents the same request."""

    def __init__(self) -> None:
        self.signature: tuple[str, str, str] | None = None
        self.request_id = ""

    def invalidate_if_changed(self, signature: tuple[str, str, str]) -> None:
        if self.request_id and signature != self.signature:
            self.clear()

    def for_signature(self, signature: tuple[str, str, str]) -> str:
        if signature != self.signature or not self.request_id:
            self.signature = signature
            self.request_id = f"gui-{uuid.uuid4().hex}"
        return self.request_id

    def clear(self) -> None:
        self.signature = None
        self.request_id = ""


def _member_label(member: dict) -> str:
    return str(member.get("display_name") or member.get("name")
               or member.get("user_id") or member.get("id") or "项目成员")


class ProjectGovernanceWindow(tk.Toplevel):
    """Owner-created proposals, qualified votes, and target transfer acceptance."""

    def __init__(self, parent, app, fonts, api: ProjectHubApi, project: dict,
                 current_user_id: str, *, read_only: bool = False) -> None:
        super().__init__(parent)
        self.app = app
        self.fonts = fonts
        self.api = api
        self.project = dict(project)
        self.project_id = str(project.get("id") or "")
        self.current_user_id = str(current_user_id or "")
        self.read_only = bool(read_only)
        self._project_status_confirmed = False
        self.members: list[dict] = []
        self.proposals: list[dict] = []
        self.current_proposal: dict = {}
        self._proposal_detail_confirmed = False
        self.pending_transfer: dict = {}
        self._transfer_load_error = ""
        self._members_loaded = False
        self._busy = False
        self._destroying = False
        self._results: queue.Queue = queue.Queue()
        self._pending_create = GovernanceRequestIdCache()
        self._poll_id = self.after(80, self._poll_results)
        self._target_ids: dict[str, str] = {}

        self.title("项目治理提案")
        self.configure(bg=t.CANVAS)
        self.geometry("1080x760")
        self.minsize(900, 660)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        header = tk.Frame(self, bg=t.HEADER, padx=t.s(20), pady=t.s(14))
        header.pack(fill="x")
        tk.Label(header, text="项目治理提案", bg=t.HEADER, fg=t.INK,
                 font=fonts.display_md).pack(anchor="w")
        tk.Label(header,
                 text=(f"{project.get('name') or project.get('title') or self.project_id} · "
                       f"{self.project_id}    "
                       "管理员提权、降低审批阈值和所有权转移均需项目成员多票审批。"),
                 bg=t.HEADER, fg=t.INK_MUTED, font=fonts.caption,
                 anchor="w", justify="left", wraplength=t.s(1000)).pack(anchor="w", pady=(4, 0))

        self.status_label = tk.Label(self, text="正在读取治理状态…", bg=t.CANVAS,
                                     fg=t.INK_MUTED, font=fonts.caption,
                                     anchor="w", justify="left", wraplength=t.s(1000))
        self.status_label.pack(fill="x", padx=t.s(20), pady=(t.s(10), 0))

        transfer = tk.Frame(self, bg=t.SURFACE_ALT, padx=t.s(12), pady=t.s(9))
        transfer.pack(fill="x", padx=t.s(20), pady=(t.s(8), t.s(8)))
        self.transfer_label = tk.Label(transfer, text="没有待你接受的所有权转移。",
                                       bg=t.SURFACE_ALT, fg=t.INK_SOFT,
                                       font=fonts.caption, anchor="w", justify="left")
        self.transfer_label.pack(side="left", fill="x", expand=True)
        self.accept_transfer_button = FlatButton(
            transfer, "接受所有权转移", self.accept_owner_transfer,
            font=fonts.caption, variant="primary", height=30,
            parent_bg=t.SURFACE_ALT)
        self.accept_transfer_button.pack(side="right")
        self.accept_transfer_button.set_enabled(False)

        body = tk.Frame(self, bg=t.CANVAS, padx=t.s(20))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0, minsize=t.s(340))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        left = tk.Frame(body, bg=t.SURFACE_ALT, padx=t.s(12), pady=t.s(10))
        left.grid(row=0, column=0, sticky="nsew", padx=(0, t.s(10)))
        list_header = tk.Frame(left, bg=t.SURFACE_ALT)
        list_header.pack(fill="x", pady=(0, t.s(6)))
        tk.Label(list_header, text="提案与审批", bg=t.SURFACE_ALT, fg=t.INK,
                 font=fonts.small_bold).pack(side="left")
        FlatButton(list_header, "刷新", self.refresh, font=fonts.kicker,
                   variant="outline", height=26, radius=7,
                   parent_bg=t.SURFACE_ALT).pack(side="right")
        self.proposal_list = tk.Listbox(
            left, height=12, bg=t.SURFACE, fg=t.INK,
            selectbackground=t.TERRACOTTA_SOFT, selectforeground=t.INK,
            relief="flat", font=fonts.small, activestyle="none")
        self.proposal_list.pack(fill="both", expand=True)
        self.proposal_list.bind("<<ListboxSelect>>", self._on_proposal_selected)

        right = tk.Frame(body, bg=t.SURFACE, highlightbackground=t.LINE,
                         highlightthickness=1, padx=t.s(14), pady=t.s(12))
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)
        self.detail_label = tk.Label(
            right, text="选择一项提案查看目标、状态、票数和投票记录。",
            bg=t.SURFACE, fg=t.INK_SOFT, font=fonts.caption,
            anchor="nw", justify="left", wraplength=t.s(600))
        self.detail_label.grid(row=0, column=0, sticky="ew")
        self.vote_label = tk.Label(right, text="", bg=t.SURFACE, fg=t.INK_MUTED,
                                   font=fonts.caption, anchor="w", justify="left",
                                   wraplength=t.s(600))
        self.vote_label.grid(row=1, column=0, sticky="ew", pady=(t.s(8), t.s(5)))
        self.vote_history = tk.Listbox(
            right, height=7, bg=t.SURFACE_ALT, fg=t.INK_SOFT,
            relief="flat", font=fonts.small, activestyle="none")
        self.vote_history.grid(row=2, column=0, sticky="nsew")
        actions = tk.Frame(right, bg=t.SURFACE)
        actions.grid(row=3, column=0, sticky="ew", pady=(t.s(9), 0))
        self.approve_button = FlatButton(
            actions, "赞成", lambda: self.vote("approve"), font=fonts.caption,
            variant="primary", height=30, parent_bg=t.SURFACE)
        self.approve_button.pack(side="left", padx=(0, t.s(7)))
        self.reject_button = FlatButton(
            actions, "拒绝", lambda: self.vote("reject"), font=fonts.caption,
            variant="outline", height=30, parent_bg=t.SURFACE)
        self.reject_button.pack(side="left")

        self.composer = tk.Frame(self, bg=t.SURFACE_ALT, padx=t.s(14), pady=t.s(10))
        self.composer.pack(fill="x", padx=t.s(20), pady=(t.s(9), t.s(18)))
        tk.Label(self.composer, text="发起治理提案（仅活动项目负责人）",
                 bg=t.SURFACE_ALT, fg=t.INK, font=fonts.small_bold).pack(anchor="w")
        row = tk.Frame(self.composer, bg=t.SURFACE_ALT)
        row.pack(fill="x", pady=(t.s(7), 0))
        tk.Label(row, text="提案类型", width=15, anchor="w", bg=t.SURFACE_ALT,
                 fg=t.INK_SOFT, font=fonts.caption).pack(side="left")
        self._action_by_label = {label: key for key, label in _ACTIONS.items()}
        self.action_var = tk.StringVar(value=_ACTIONS["promote_admin"])
        self.action_menu = tk.OptionMenu(row, self.action_var, *tuple(_ACTIONS.values()))
        self.action_menu.configure(bg=t.SURFACE, fg=t.INK, relief="flat",
                                  font=fonts.caption, highlightthickness=0)
        self.action_menu.pack(side="left", fill="x", expand=True)

        self.target_row = tk.Frame(self.composer, bg=t.SURFACE_ALT)
        self.target_row.pack(fill="x", pady=(t.s(5), 0))
        self.target_label = tk.Label(self.target_row, text="目标项目成员", width=15,
                                     anchor="w", bg=t.SURFACE_ALT, fg=t.INK_SOFT,
                                     font=fonts.caption)
        self.target_label.pack(side="left")
        self.target_var = tk.StringVar(value="")
        self.target_menu = tk.OptionMenu(self.target_row, self.target_var, "")
        self.target_menu.configure(bg=t.SURFACE, fg=t.INK, relief="flat",
                                   font=fonts.caption, highlightthickness=0)
        self.target_menu.pack(side="left", fill="x", expand=True)

        self.threshold_row = tk.Frame(self.composer, bg=t.SURFACE_ALT)
        tk.Label(self.threshold_row, text="新的审批阈值", width=15,
                 anchor="w", bg=t.SURFACE_ALT, fg=t.INK_SOFT,
                 font=fonts.caption).pack(side="left")
        self.threshold_var = tk.StringVar(value="")
        self.threshold_entry = tk.Entry(
            self.threshold_row, textvariable=self.threshold_var,
            bg=t.SURFACE, fg=t.INK, relief="flat", font=fonts.caption)
        self.threshold_entry.pack(side="left", fill="x", expand=True, ipady=t.s(4))
        self.threshold_hint = tk.Label(self.composer, text="",
                                       bg=t.SURFACE_ALT, fg=t.INK_MUTED,
                                       font=fonts.kicker, anchor="w")
        self.threshold_hint.pack(fill="x", pady=(t.s(4), 0))
        self.capacity_label = tk.Label(self.composer, text="",
                                       bg=t.SURFACE_ALT, fg=t.WARNING,
                                       font=fonts.caption, anchor="w", justify="left",
                                       wraplength=t.s(900))
        self.capacity_label.pack(fill="x", pady=(t.s(4), 0))
        self.create_button = FlatButton(
            self.composer, "提交治理提案", self.create_proposal,
            font=fonts.caption, variant="primary", height=32,
            parent_bg=t.SURFACE_ALT)
        self.create_button.pack(anchor="e", pady=(t.s(6), 0))
        self.action_var.trace_add("write", lambda *_: self._on_composer_changed())
        self.threshold_var.trace_add("write", lambda *_: self._on_composer_changed())

        self.refresh()

    def _request(self, tag: str, work) -> None:
        def run() -> None:
            try:
                result = work()
            except Exception as exc:
                result = (0, {"detail": str(exc)})
            self._results.put((tag, result))
        threading.Thread(target=run, daemon=True,
                         name=f"venus-governance-{tag}").start()

    def _poll_results(self) -> None:
        if self._destroying:
            return
        for _ in range(20):
            try:
                tag, result = self._results.get_nowait()
            except queue.Empty:
                break
            self._on_result(tag, result)
        try:
            self._poll_id = self.after(80, self._poll_results)
        except tk.TclError:
            pass

    @staticmethod
    def _safe_call(work):
        try:
            return work()
        except Exception as exc:
            return 0, {"detail": str(exc)}

    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._project_status_confirmed = False
        self._update_composer()
        self._update_proposal_actions()

        def work():
            return {
                "project": self._safe_call(lambda: self.api.project_detail(self.project_id)),
                "members": self._safe_call(lambda: self.api.list_members(self.project_id)),
                "proposals": self._safe_call(
                    lambda: self.api.list_governance_proposals(self.project_id)),
                "transfer": self._safe_call(
                    lambda: self.api.pending_owner_transfer(self.project_id)),
            }
        self._request("refresh", work)

    def _on_proposal_selected(self, _event=None) -> None:
        selection = self.proposal_list.curselection()
        if not selection or selection[0] >= len(self.proposals):
            return
        proposal = self.proposals[selection[0]]
        proposal_id = str(proposal.get("id") or "")
        if not proposal_id:
            return
        self.current_proposal = proposal
        self._render_proposal(proposal)
        self._request("detail", lambda: self.api.governance_proposal(
            self.project_id, proposal_id))

    def _on_result(self, tag: str, result) -> None:
        if tag == "refresh":
            self._busy = False
            if not isinstance(result, dict):
                self.status_label.configure(text="治理状态返回格式异常。", fg=t.DANGER)
                return
            project_code, project_data = result.get("project", (0, {}))
            member_code, member_data = result.get("members", (0, {}))
            proposals_code, proposals_data = result.get("proposals", (0, {}))
            transfer_code, transfer_data = result.get("transfer", (0, {}))
            project_status_error = ""
            if project_code == 200 and isinstance(project_data, dict):
                project = project_data.get("project") or project_data
                self.project.update(project)
                status = str(project.get("status") or "")
                self._project_status_confirmed = bool(status)
                if status == "archived":
                    self.read_only = True
                if not status:
                    project_status_error = "Hub 项目详情缺少状态字段"
            else:
                self._project_status_confirmed = False
                detail = (project_data.get("detail") if isinstance(project_data, dict)
                          else None)
                project_status_error = f"读取项目状态失败：{detail or project_code}"
            if member_code == 200:
                self.members = list(member_data.get("members") or [])
                self._members_loaded = True
            else:
                self.members = []
                self._members_loaded = False
            if proposals_code == 200:
                self.proposals = unwrap_governance_proposals(proposals_data)
                self._render_proposals()
                self.status_label.configure(
                    text=f"已读取 {len(self.proposals)} 项治理提案。",
                    fg=t.SUCCESS)
            else:
                self.proposals = []
                self.current_proposal = {}
                self._proposal_detail_confirmed = False
                self._render_proposals()
                self.status_label.configure(
                    text=f"读取治理提案失败：{(proposals_data or {}).get('detail', proposals_code)}",
                    fg=t.DANGER)
            if transfer_code == 200:
                self.pending_transfer = self._unwrap_transfer(transfer_data)
                self._transfer_load_error = ""
                self._render_pending_transfer()
            elif transfer_code in (404, 204):
                self.pending_transfer = {}
                self._transfer_load_error = ""
                self._render_pending_transfer()
            else:
                self.pending_transfer = {}
                self._transfer_load_error = (
                    f"读取待接受所有权转移失败："
                    f"{(transfer_data or {}).get('detail', transfer_code)}")
                self._render_pending_transfer()
            self._update_composer()
            self._update_proposal_actions()
            if project_status_error:
                self.status_label.configure(
                    text=f"{project_status_error}；已禁用提案、投票和所有权接受操作。",
                    fg=t.WARNING)
            if not self._members_loaded and not project_status_error:
                self.status_label.configure(
                    text="成员资格读取失败；提案详情仍可查看，但投票资格和目标成员暂不可用。",
                    fg=t.WARNING)
            return

        code, data = result if isinstance(result, tuple) else (0, {})
        data = data if isinstance(data, dict) else {"detail": str(data)}
        if tag == "detail":
            proposal = unwrap_governance_proposal(data)
            if code == 200 and proposal:
                if str(proposal.get("id") or "") == str(self.current_proposal.get("id") or ""):
                    self.current_proposal = proposal
                    self._proposal_detail_confirmed = True
                    self._render_proposal(proposal)
                    self._update_proposal_actions()
            else:
                self._proposal_detail_confirmed = False
                self._update_proposal_actions()
                self.vote_label.configure(
                    text=f"读取提案详情失败：{data.get('detail', code)}", fg=t.DANGER)
            return

        self._busy = False
        if code != 200:
            message = str(data.get("detail") or f"HTTP {code}")
            self.status_label.configure(text=message, fg=t.DANGER)
            if self.app is not None:
                self.app.toast(message, duration=4500)
            self._update_composer()
            self._update_proposal_actions()
            return
        if tag == "create":
            proposal = unwrap_governance_proposal(data)
            self.current_proposal = proposal
            self._pending_create.clear()
            self.status_label.configure(text="治理提案已提交，等待合格成员多票审批。",
                                        fg=t.SUCCESS)
        elif tag == "vote":
            proposal = unwrap_governance_proposal(data)
            self.current_proposal = proposal
            self.status_label.configure(text="审批票已记录。", fg=t.SUCCESS)
        elif tag == "accept_transfer":
            transfer = data.get("transfer") or {}
            self.status_label.configure(
                text=("所有权转移已接受。" if not transfer.get("already_processed")
                      else "所有权转移已处理。"), fg=t.SUCCESS)
            if isinstance(data.get("project"), dict):
                self.project.update(data["project"])
        self._refresh_parent_project_hub()
        self.refresh()

    def _refresh_parent_project_hub(self) -> None:
        refresh = getattr(self.master, "refresh", None)
        if callable(refresh):
            refresh()

    @staticmethod
    def _unwrap_transfer(data: dict) -> dict:
        if not isinstance(data, dict):
            return {}
        transfer = data.get("transfer")
        return transfer if isinstance(transfer, dict) else {}

    def _render_proposals(self) -> None:
        selected_id = str(self.current_proposal.get("id") or "")
        self._proposal_detail_confirmed = False
        self.proposal_list.delete(0, "end")
        selected_index = None
        for index, proposal in enumerate(self.proposals):
            summary = governance_proposal_summary(proposal, self.members)
            self.proposal_list.insert(
                "end", f"{summary['status']} · {summary['votes']} · "
                      f"{summary['description']}")
            if str(proposal.get("id") or "") == selected_id:
                selected_index = index
        if self.proposals:
            index = selected_index if selected_index is not None else 0
            self.proposal_list.selection_set(index)
            self.proposal_list.activate(index)
            self.current_proposal = self.proposals[index]
            self._render_proposal(self.current_proposal)
            self._request("detail", lambda: self.api.governance_proposal(
                self.project_id, str(self.current_proposal.get("id") or "")))
        else:
            self.current_proposal = {}
            self._proposal_detail_confirmed = False
            self.detail_label.configure(text="目前没有治理提案。")
            self.vote_label.configure(text="")
            self.vote_history.delete(0, "end")
            self._update_proposal_actions()

    def _render_proposal(self, proposal: dict) -> None:
        summary = governance_proposal_summary(proposal, self.members)
        action_label = _ACTIONS.get(str(proposal.get("action") or ""), "治理提案")
        proposer_id = str(proposal.get("proposer_user_id") or "")
        proposer = next((_member_label(member) for member in self.members
                         if str(member.get("user_id") or member.get("id") or "") == proposer_id),
                        proposer_id or "未知")
        digest = str(proposal.get("parameters_sha256") or "")
        policy_version = proposal.get("policy_version")
        self.detail_label.configure(
            text=(f"{action_label}\n{summary['description']}\n"
                  f"状态：{summary['status']}    {summary['votes']}\n"
                  f"发起人：{proposer}\n"
                  f"创建：{summary['created_at']}    到期：{summary['expires_at']}\n"
                  f"策略版本：{policy_version if policy_version is not None else '—'}\n"
                  f"参数摘要：{digest or '—'}"))
        required = max(2, _as_int(proposal.get("required_approvals"), 2))
        eligible = governance_eligible_voter_count(self.members, proposer_id)
        capacity = (f"当前只有 {eligible} 位合格的非发起人投票成员；本提案至少需要 "
                    f"{required} 票，按当前成员构成无法达到。"
                    if eligible < required else "")
        votes = proposal.get("votes") or []
        vote_lines = []
        for vote in votes:
            if not isinstance(vote, dict):
                continue
            user_id = str(vote.get("user_id") or "")
            name = next((_member_label(member) for member in self.members
                         if str(member.get("user_id") or member.get("id") or "") == user_id),
                        user_id or "项目成员")
            decision = "赞成" if vote.get("decision") == "approve" else "拒绝"
            validity = "有效" if vote.get("valid", True) else "已失效"
            vote_lines.append(f"{name} · {decision} · {validity} · {vote.get('created_at') or '—'}")
        self.vote_history.delete(0, "end")
        for line in vote_lines:
            self.vote_history.insert("end", line)
        if not vote_lines:
            self.vote_history.insert("end", "尚无有效投票记录。")
        self.vote_label.configure(text=capacity or "投票按服务端成员资格和提案版本校验。",
                                  fg=t.WARNING if capacity else t.INK_MUTED)

    def _render_pending_transfer(self) -> None:
        if self._transfer_load_error:
            self.transfer_label.configure(text=self._transfer_load_error,
                                          fg=t.WARNING)
            self.accept_transfer_button.set_enabled(False)
            return
        transfer = self.pending_transfer
        target_id = str(transfer.get("to_user_id") or "")
        from_id = str(transfer.get("from_user_id") or "")
        if not transfer or not target_id or target_id != self.current_user_id:
            self.transfer_label.configure(text="没有待你接受的所有权转移。",
                                          fg=t.INK_MUTED)
            self.accept_transfer_button.set_enabled(False)
            return
        sender = next((_member_label(member) for member in self.members
                       if str(member.get("user_id") or member.get("id") or "") == from_id),
                      from_id or "现任负责人")
        self.transfer_label.configure(
            text=f"有一项已获批的所有权转移待你接受。发起人：{sender} · "
                 f"提案：{transfer.get('governance_proposal_id') or transfer.get('proposal_id') or '—'}",
            fg=t.WARNING)
        self.accept_transfer_button.set_enabled(
            self._project_status_confirmed and governance_can_accept_transfer(
                self.project, transfer, self.current_user_id, read_only=self.read_only))

    def _action_targets(self, action: str) -> list[dict]:
        allowed_roles = ({"member", "viewer"} if action == "promote_admin"
                         else {"admin", "member", "viewer"})
        seen = set()
        result = []
        for member in self.members:
            if not isinstance(member, dict):
                continue
            user_id = str(member.get("user_id") or member.get("id") or "")
            if (not user_id or user_id == self.current_user_id or user_id in seen
                    or member.get("status") != "active"
                    or member.get("role") not in allowed_roles):
                continue
            seen.add(user_id)
            result.append(member)
        return result

    def _rebuild_target_menu(self, action: str) -> None:
        candidates = self._action_targets(action)
        menu = self.target_menu["menu"]
        menu.delete(0, "end")
        self._target_ids = {}
        labels = []
        for member in candidates:
            user_id = str(member.get("user_id") or member.get("id") or "")
            label = f"{_member_label(member)} · {user_id}"
            self._target_ids[label] = user_id
            labels.append(label)
            menu.add_command(label=label,
                             command=lambda value=label: self._set_target(value))
        selected = self.target_var.get()
        self.target_var.set(selected if selected in self._target_ids else
                             (labels[0] if labels else ""))
        self.target_menu.configure(state="normal" if labels else "disabled")

    def _set_target(self, value: str) -> None:
        self.target_var.set(value)
        self._on_composer_changed()

    def _composer_signature(self) -> tuple[str, str, str]:
        action = self._action_by_label.get(self.action_var.get(), "promote_admin")
        target = (self._target_ids.get(self.target_var.get(), "")
                  if action in {"promote_admin", "owner_transfer"} else "")
        threshold = (self.threshold_var.get().strip()
                     if action == "lower_approval_threshold" else "")
        return action, target, threshold

    def _on_composer_changed(self) -> None:
        signature = self._composer_signature()
        self._pending_create.invalidate_if_changed(signature)
        self._update_composer()

    def _request_id_for_signature(self, signature: tuple[str, str, str]) -> str:
        return self._pending_create.for_signature(signature)

    def _update_composer(self) -> None:
        action = self._action_by_label.get(self.action_var.get(), "promote_admin")
        needs_target = action in {"promote_admin", "owner_transfer"}
        if needs_target:
            self.target_row.pack(fill="x", pady=(t.s(5), 0), before=self.threshold_hint)
            self._rebuild_target_menu(action)
        else:
            self.target_row.pack_forget()
        if action == "lower_approval_threshold":
            self.threshold_row.pack(fill="x", pady=(t.s(5), 0), before=self.threshold_hint)
            current = _as_int(self.project.get("required_approvals"),
                              _as_int((self.project.get("policy") or {}).get(
                                  "required_approvals"), 0))
            self.threshold_hint.configure(
                text=(f"当前阈值：{current} 票。新值必须为正数且更低。"
                      if current else "未读取到当前项目审批阈值，暂时不能提交降低阈值提案。"))
        else:
            self.threshold_row.pack_forget()
            self.threshold_hint.configure(text="")
        threshold = governance_required_votes(self.project)
        capacity = (governance_capacity_message(
            self.project, self.members,
            self.current_user_id or str(self.project.get("owner_id") or ""),
            threshold or None) if self._members_loaded else
            "正在确认成员资格，暂不能判断审批票数是否足够。")
        self.capacity_label.configure(text=capacity)
        can_create = (self._project_status_confirmed
                      and self._members_loaded
                      and governance_can_propose(self.project, read_only=self.read_only))
        can_create = can_create and not self._busy
        if needs_target and not self._target_ids:
            can_create = False
        if action == "lower_approval_threshold":
            current = _as_int(self.project.get("required_approvals"),
                              _as_int((self.project.get("policy") or {}).get(
                                  "required_approvals"), 0))
            can_create = can_create and current > 0
        self.create_button.set_enabled(can_create)

    def _update_proposal_actions(self) -> None:
        proposal = self.current_proposal
        can_vote = (self._project_status_confirmed and governance_can_vote(
            self.project, proposal, self.members, self.current_user_id,
            read_only=self.read_only) and not self._busy
                    and self._proposal_detail_confirmed)
        can_vote = can_vote and self._members_loaded
        self.approve_button.set_enabled(can_vote)
        self.reject_button.set_enabled(can_vote)
        self._render_pending_transfer()

    def create_proposal(self) -> None:
        if (not self._project_status_confirmed
                or not self._members_loaded
                or not governance_can_propose(self.project, read_only=self.read_only)):
            self.app.toast("需要确认活动项目状态、负责人身份和当前成员名单后才能发起提案")
            return
        action = self._action_by_label.get(self.action_var.get(), "")
        target_id = self._target_ids.get(self.target_var.get(), "")
        current = _as_int(self.project.get("required_approvals"),
                          _as_int((self.project.get("policy") or {}).get(
                              "required_approvals"), 0))
        try:
            required = (_as_int(self.threshold_var.get(), 0)
                        if action == "lower_approval_threshold" else None)
            signature = self._composer_signature()
            payload = build_governance_proposal_payload(
                action, self._request_id_for_signature(signature),
                target_user_id=target_id,
                required_approvals=required, current_threshold=current)
        except ValueError as exc:
            self.app.toast(str(exc))
            return
        self._busy = True
        self._update_composer()
        self._request("create", lambda: self.api.create_governance_proposal(
            self.project_id, payload["action"], payload["request_id"],
            target_user_id=payload.get("target_user_id", ""),
            required_approvals=payload.get("required_approvals")))

    def vote(self, decision: str) -> None:
        proposal_id = str(self.current_proposal.get("id") or "")
        if decision not in {"approve", "reject"} or not proposal_id:
            return
        if (not self._project_status_confirmed or not governance_can_vote(
                self.project, self.current_proposal, self.members,
                self.current_user_id, read_only=self.read_only)
                or not self._members_loaded or not self._proposal_detail_confirmed):
            self.app.toast("发起人不能投票；投票人还需满足项目成员和终端资格。")
            return
        self._busy = True
        self._update_proposal_actions()
        self._request("vote", lambda: self.api.vote_governance_proposal(
            self.project_id, proposal_id, decision))

    def accept_owner_transfer(self) -> None:
        transfer = self.pending_transfer
        if (not self._project_status_confirmed or not governance_can_accept_transfer(
                self.project, transfer, self.current_user_id,
                read_only=self.read_only)):
            self.app.toast("没有待当前用户接受的所有权转移")
            return
        if not messagebox.askyesno(
                "接受项目所有权转移",
                f"接受提案 {transfer.get('governance_proposal_id') or transfer.get('proposal_id') or '—'} 的所有权转移？\n\n"
                "接受后，你将成为该项目负责人。", parent=self):
            return
        self._busy = True
        self._update_proposal_actions()
        self.accept_transfer_button.set_enabled(False)
        self._request("accept_transfer", lambda: self.api.accept_owner_transfer(
            self.project_id))

    def destroy(self) -> None:
        self._destroying = True
        try:
            if self._poll_id:
                self.after_cancel(self._poll_id)
        except tk.TclError:
            pass
        super().destroy()


def open_project_governance(parent, app, fonts, api: ProjectHubApi,
                            project: dict, current_user_id: str, *,
                            read_only: bool = False) -> ProjectGovernanceWindow:
    return ProjectGovernanceWindow(parent, app, fonts, api, project, current_user_id,
                                   read_only=read_only)
