"""Regression check that refreshing reviews re-reads selected detail and diff."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from venuschat_v1.team_workspace_view import TeamWorkspaceView  # noqa: E402


def main() -> None:
    submitted: list[str] = []
    view = TeamWorkspaceView.__new__(TeamWorkspaceView)
    view.app = SimpleNamespace(_api_result=lambda payload: payload[1])
    view.bridge = SimpleNamespace(
        active_project_id="project-a",
        projects=[{"id": "project-a", "is_team": True}],
        submit=lambda kind, _fn: submitted.append(kind),
        client=SimpleNamespace(),
    )
    view._request_generation = 7
    view._selected_change = "change-a"
    view._changes = [{"id": "change-a"}]
    view._change = {"approval_count": 1}
    view._diff = "old diff"
    view.page = "changes"
    view._refresh_changes_page = lambda: None
    view._render_page = lambda: None

    refreshed_list = {
        "changes": [{"id": "change-a", "approval_count": 2}],
        "_workspace_generation": 7,
        "_workspace_project_id": "project-a",
    }
    view.handle_backend("team_workspace_changes", ("ok", (200, refreshed_list)))

    assert view._selected_change == "change-a"
    assert view._change == {} and view._diff == ""
    assert submitted == ["team_workspace_change_detail", "team_workspace_diff"], submitted

    # A 200 Hub health response with no model must retain team page access,
    # while a transport failure marks the Hub unavailable.
    health_view = TeamWorkspaceView.__new__(TeamWorkspaceView)
    health_view.app = SimpleNamespace(_refresh_space_navigation=lambda: None)
    health_view.bridge = SimpleNamespace(
        client=SimpleNamespace(base="https://hub.example"),
        projects=[{"id": "project-a", "is_team": True}],
        active_project_id="project-a",
    )
    health_view._service_reachable = True
    health_view._identity_verified = True
    health_view._model_ready = True
    health_view._jobs_refresh_job = None
    health_view._refresh_identity_label = lambda: None
    health_view._refresh_dispatch_controls = lambda: None
    renders: list[str] = []
    health_view._render_page = lambda: renders.append("render")
    health_view.app = SimpleNamespace(_refresh_space_navigation=lambda: None)
    with patch("venuschat_v1.team_workspace_view.team_connection",
               return_value={"status": "joined"}):
        health_view.on_health({"configured": False}, reachable=True)
        assert health_view.api_available and not health_view._model_ready
        assert not renders, "model readiness must not hide accessible projects/reviews"
        health_view.on_health({"configured": False}, reachable=False)
        assert not health_view.api_available
        assert renders == ["render"]

        # If the first identity request failed, the next healthy Hub response
        # retries it. Request deduplication avoids duplicate startup checks.
        health_view._identity_verified = False
        retries: list[str] = []
        health_view.request_identity = lambda: retries.append("identity")
        health_view.on_health({"configured": True}, reachable=True)
        assert retries == ["identity"]

    identity_view = TeamWorkspaceView.__new__(TeamWorkspaceView)
    identity_view.bridge = SimpleNamespace(
        client=SimpleNamespace(base="https://hub.example"))
    identity_view._identity_request_pending = False
    requested: list[str] = []
    identity_view._request = lambda name, *_args: requested.append(name)
    with patch("venuschat_v1.team_workspace_view.team_connection",
               return_value={"status": "joined"}):
        identity_view.request_identity()
        identity_view.request_identity()
    assert requested == ["identity"]

    # A transient project-list error must keep the verified identity and project
    # selection so a successful health check can restore the lists automatically.
    recovery_view = TeamWorkspaceView.__new__(TeamWorkspaceView)
    recovery_view.app = SimpleNamespace(_refresh_space_navigation=lambda: None)
    recovery_view.bridge = SimpleNamespace(
        client=SimpleNamespace(base="https://hub.example"),
        projects=[{"id": "project-a", "is_team": True}],
        active_project_id="project-a",
    )
    recovery_view._service_reachable = True
    recovery_view._identity_verified = True
    recovery_view._model_ready = True
    recovery_view._jobs_refresh_job = None
    recovery_view._identity = {"user_id": "member-a"}
    recovery_view._team_jobs = [{"project_id": "project-a"}]
    recovery_view._changes = [{"id": "change-a"}]
    recovery_view._members = []
    recovery_view.stop_jobs_polling = lambda: None
    recovery_view._sync_projects = lambda: None
    recovery_view._render_page = lambda: None
    recovery_view._refresh_identity_label = lambda: None
    recovery_view._refresh_dispatch_controls = lambda: None
    list_refreshes: list[str] = []
    recovery_view._refresh_team_lists = lambda: list_refreshes.append("refresh")
    with patch("venuschat_v1.team_workspace_view.team_connection",
               return_value={"status": "joined"}):
        recovery_view.on_projects(503, {"detail": "temporary failure"})
        assert not recovery_view.api_available
        assert recovery_view._identity_verified
        assert recovery_view.bridge.active_project_id == "project-a"
        recovery_view.on_health({"configured": True}, reachable=True)
        assert recovery_view.api_available
    assert list_refreshes == ["refresh"]

    # While a dispatch response is pending, a second click cannot submit again.
    dispatches: list[str] = []
    notices: list[str] = []
    dispatch_view = TeamWorkspaceView.__new__(TeamWorkspaceView)
    dispatch_view.bridge = SimpleNamespace(
        client=SimpleNamespace(base="https://hub.example"),
        active_project_id="project-a",
        projects=[{"id": "project-a", "is_team": True, "status": "active"}],
        dispatch=lambda *_args, **_kwargs: dispatches.append("dispatch"),
    )
    dispatch_view._dispatch_pending_project = "project-a"
    dispatch_view._service_reachable = True
    dispatch_view._identity_verified = True
    dispatch_view._model_ready = True
    dispatch_view.app = SimpleNamespace(toast=lambda message: notices.append(message))
    with patch("venuschat_v1.team_workspace_view.team_connection",
               return_value={"status": "joined"}):
        dispatch_view._dispatch_task()
    assert not dispatches
    assert any("仍在提交" in notice for notice in notices)

    # The active queue gets one timer, which is removed as soon as the view hides.
    polling_view = TeamWorkspaceView.__new__(TeamWorkspaceView)
    polling_view.bridge = dispatch_view.bridge
    polling_view.app = SimpleNamespace(_current_space="team", _current_view="team")
    polling_view.page = "tasks"
    polling_view._service_reachable = True
    polling_view._identity_verified = True
    polling_view._filter = "all"
    polling_view._team_jobs = [{"visibility": "team", "project_id": "project-a",
                                "status": "running"}]
    polling_view._jobs_refresh_job = None
    scheduled: list[tuple[int, object]] = []
    cancelled: list[str] = []
    polling_view.after = lambda delay, callback: (
        scheduled.append((delay, callback)), f"timer-{len(scheduled)}")[1]
    polling_view.after_cancel = lambda token: cancelled.append(token)
    with patch("venuschat_v1.team_workspace_view.team_connection",
               return_value={"status": "joined"}):
        polling_view._schedule_jobs_refresh()
        polling_view._schedule_jobs_refresh()
        assert len(scheduled) == 1 and scheduled[0][0] == 5000
        polling_view.app._current_view = "settings"
        polling_view._schedule_jobs_refresh()
    assert cancelled == ["timer-1"] and polling_view._jobs_refresh_job is None

    # A five-second poll with identical summaries must not rebuild the page.
    polling_view._selected_job = None
    redraws: list[str] = []
    polling_view._render_task_queue = lambda: redraws.append("draw")
    polling_view._schedule_jobs_refresh = lambda: None
    with patch("venuschat_v1.team_workspace_view.team_connection",
               return_value={"status": "joined"}):
        polling_view.on_jobs([dict(polling_view._team_jobs[0])])
        assert redraws == [], "unchanged polling response should preserve GUI widgets"
        polling_view.on_jobs([{
            **polling_view._team_jobs[0],
            "progress": {"heartbeat": "new transport metadata"}}])
        assert redraws == [], "non-visible progress must not repaint the page"
        polling_view.on_jobs([{
            **polling_view._team_jobs[0], "status": "waiting_confirm"}])
    assert redraws == ["draw"], "a real status change must still be shown"

    # Confirmation is exposed only for an active request tied to this job and
    # project; submitting the modal's choice goes to the team Hub.
    confirmations: list[tuple[str, object]] = []
    refreshes: list[str] = []
    job = {"id": "job-a", "status": "waiting_confirm", "project_id": "project-a",
           "visibility": "team", "task_id": "task-a"}
    ask = {"id": "ask-a", "name": "replace_text", "question": "允许修改 readme.md？"}
    pending = [{"request_id": "ask-a", "task_id": "task-a"}]

    def get(path: str, **_kwargs):
        if path == "/api/v1/confirm/pending":
            return 200, {"pending": pending}
        assert path == "/api/v1/jobs/job-a"
        return 200, {"job": {**job, "confirm_request_id": "ask-a",
                             "pending_ask": ask}}

    def submit(kind: str, fn) -> None:
        confirmations.append((kind, fn()))

    confirm_view = TeamWorkspaceView.__new__(TeamWorkspaceView)
    confirm_view.app = SimpleNamespace(
        root=object(), _api_result=lambda payload: payload[1],
        toast=lambda message, **_kw: notices.append(message))
    confirm_view.fonts = object()
    confirm_view.bridge = SimpleNamespace(
        client=SimpleNamespace(base="https://hub.example", get=get,
                               post=lambda path, body, **_kw: (
                                   200, {"choice": body["choice"]})),
        active_project_id="project-a",
        projects=[{"id": "project-a", "is_team": True}])
    confirm_view.bridge.submit = submit
    confirm_view._service_reachable = True
    confirm_view._identity_verified = True
    confirm_view._selected_job = job
    confirm_view._selected_change = ""
    confirm_view._request_generation = 4
    confirm_view._confirm_lookup_job_id = ""
    confirm_view.page = "tasks"
    confirm_view._refresh_jobs = lambda: refreshes.append("refresh")
    dialogs: list[tuple[dict, object]] = []
    with patch("venuschat_v1.team_workspace_view.team_connection",
               return_value={"status": "joined"}), patch(
                   "venuschat_v1.confirm_dialog.show_confirm",
                   side_effect=lambda _root, data, callback, _fonts:
                   dialogs.append((data, callback))):
        confirm_view._open_job_confirmation("job-a")
        kind, payload = confirmations.pop()
        assert kind == "team_workspace_job_confirmation"
        confirm_view.handle_backend(kind, payload)
        assert dialogs[0][0] == ask
        dialogs[0][1](True, "ask-a")
        kind, payload = confirmations.pop()
        assert kind == "team_workspace_confirm_vote"
        confirm_view.handle_backend(kind, payload)
        assert refreshes == ["refresh"]

        pending.clear()
        confirm_view._open_job_confirmation("job-a")
        kind, payload = confirmations.pop()
        confirm_view.handle_backend(kind, payload)
    assert len(dialogs) == 1, "expired or already-voted confirmation must not open"
    assert any("确认已超时" in message for message in notices)
    print("PASS review refresh and team list recovery")


if __name__ == "__main__":
    main()
