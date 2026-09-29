"""Pure workspace and project scope rules for the VenusChat frontend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


PERSONAL = "personal"
TEAM = "team"


@dataclass(frozen=True, slots=True)
class WorkspaceScope:
    """A verified backend origin and the project currently shown within it."""

    kind: str = PERSONAL
    origin: str = ""
    project_id: str = ""
    connected: bool = True


def workspace_kind(origin: str, joined_team_origins: Iterable[str]) -> str:
    """Infer workspace from an enrolled origin, never from a UI toggle."""
    candidate = str(origin or "").rstrip("/").casefold()
    if candidate and candidate in {
        str(item or "").rstrip("/").casefold() for item in joined_team_origins
    }:
        return TEAM
    return PERSONAL


def team_projects(projects: Iterable[dict]) -> list[dict]:
    """Return only team projects that the active Hub already exposed to us."""
    return [row for row in projects or ()
            if isinstance(row, dict) and bool(row.get("is_team"))]


def team_jobs_for_project(jobs: Iterable[dict], project_id: str) -> list[dict]:
    """Keep a team task list scoped to one project and its shared visibility."""
    pid = str(project_id or "")
    if not pid:
        return []
    return [row for row in jobs or ()
            if isinstance(row, dict)
            and row.get("visibility") == "team"
            and str(row.get("project_id") or "") == pid]


def personal_jobs(jobs: Iterable[dict]) -> list[dict]:
    """Keep only the current user's private jobs for the personal workspace."""
    return [row for row in jobs or ()
            if isinstance(row, dict)
            and row.get("visibility") != "team"
            and bool(row.get("is_mine"))]


def project_status_label(project: dict | None) -> str:
    if not project:
        return "未选择项目"
    status = str(project.get("status") or "active")
    return {
        "active": "进行中",
        "pending_claim": "待认领",
        "archived": "已归档 · 只读",
        "planning": "规划中",
        "paused": "已暂停",
    }.get(status, status)


def project_allows_dispatch(project: dict | None) -> bool:
    return bool(project and project.get("is_team")
                and str(project.get("status") or "active") == "active")


def backend_switch_block_reason(*, streaming: bool,
                                creating_session: bool) -> str:
    """Explain why a workspace switch must wait for the current chat action."""
    if streaming or creating_session:
        return "当前对话操作尚未结束，请完成或停止后再切换空间"
    return ""


def team_workspace_available(*, connected: bool, service_reachable: bool,
                             identity_verified: bool) -> bool:
    """Project data needs both an answering Hub and a verified team identity."""
    return bool(connected and service_reachable and identity_verified)


def can_dispatch_team_task(*, project: dict | None, api_available: bool,
                           model_ready: bool, dispatch_in_flight: bool) -> bool:
    """Task dispatch requires a writable project and a ready model request path."""
    return bool(api_available and model_ready and not dispatch_in_flight
                and project_allows_dispatch(project))


def can_cancel_team_job(job: dict | None, project_role: str) -> bool:
    """Mirror the Hub rule: task author or project owner/admin may cancel."""
    if not job or str(job.get("visibility") or "") != "team":
        return False
    return bool(job.get("is_mine") or str(project_role or "") in {"owner", "admin"})


def should_poll_team_jobs(*, api_available: bool, tasks_page_visible: bool,
                          has_active_jobs: bool) -> bool:
    """Poll only while an active team queue is visible and authenticated."""
    return bool(api_available and tasks_page_visible and has_active_jobs)


def selected_change_after_refresh(changes: Iterable[dict],
                                  selected_change_id: str) -> str:
    """Keep the current review selected, or select the first available row."""
    rows = [row for row in changes or () if isinstance(row, dict)]
    selected = str(selected_change_id or "")
    if selected and any(str(row.get("id") or "") == selected for row in rows):
        return selected
    return str(rows[0].get("id") or "") if rows else ""


def response_is_current(*, project_id: str, active_project_id: str,
                        generation: int, current_generation: int,
                        change_id: str = "", selected_change_id: str = "") -> bool:
    """Reject a late page response after its project or selected change changed."""
    if generation and generation != current_generation:
        return False
    if project_id and project_id != active_project_id:
        return False
    if change_id and change_id != selected_change_id:
        return False
    return True


def can_submit_change(job: dict, *, project_id: str, current_user_id: str) -> bool:
    """Only the current user's completed team task can enter change submission."""
    return bool(job
                and str(job.get("project_id") or "") == str(project_id or "")
                and job.get("visibility") == "team"
                and job.get("is_mine")
                and job.get("change_id")
                and str(job.get("status") or "") == "completed"
                and (not current_user_id
                     or str(job.get("owner") or job.get("owner_id") or "")
                     == str(current_user_id)))


def can_review_change(change: dict, *, current_user_id: str,
                      project_status: str = "active") -> bool:
    """Hide review actions for authors and non-active projects."""
    if project_status != "active" or not change:
        return False
    author = str(change.get("author_id") or "")
    status = str(change.get("status") or "")
    return bool(current_user_id and author != current_user_id
                and status in {"pending_review", "approved"})
