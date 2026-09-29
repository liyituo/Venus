"""Unit checks for personal/team and project visibility boundaries."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from venuschat_v1.workspace_state import (  # noqa: E402
    backend_switch_block_reason,
    can_cancel_team_job,
    can_dispatch_team_task,
    can_review_change,
    can_submit_change,
    personal_jobs,
    project_allows_dispatch,
    response_is_current,
    selected_change_after_refresh,
    should_poll_team_jobs,
    team_workspace_available,
    team_jobs_for_project,
    team_projects,
    workspace_kind,
)


def main() -> None:
    assert workspace_kind("https://hub.example.ts.net/",
                          ["https://hub.example.ts.net"]) == "team"
    assert workspace_kind("http://127.0.0.1:8001", ["https://hub.example.ts.net"]) == "personal"

    projects = [
        {"id": "alpha", "is_team": True},
        {"id": "private", "is_team": False},
    ]
    assert [row["id"] for row in team_projects(projects)] == ["alpha"]

    jobs = [
        {"id": "mine", "visibility": "private", "is_mine": True},
        {"id": "other", "visibility": "private", "is_mine": False},
        {"id": "team-a", "visibility": "team", "project_id": "alpha"},
        {"id": "team-b", "visibility": "team", "project_id": "beta"},
    ]
    assert [row["id"] for row in personal_jobs(jobs)] == ["mine"]
    assert [row["id"] for row in team_jobs_for_project(jobs, "alpha")] == ["team-a"]
    assert team_jobs_for_project(jobs, "") == []
    assert response_is_current(project_id="alpha", active_project_id="alpha",
                               generation=3, current_generation=3)
    assert not response_is_current(project_id="alpha", active_project_id="beta",
                                  generation=3, current_generation=3)
    assert not response_is_current(project_id="alpha", active_project_id="alpha",
                                  generation=2, current_generation=3)
    assert not response_is_current(project_id="alpha", active_project_id="alpha",
                                  generation=3, current_generation=3,
                                  change_id="old", selected_change_id="new")

    base_job = {
        "project_id": "alpha", "visibility": "team", "is_mine": True,
        "owner": "alice", "status": "completed", "change_id": "change-1",
    }
    assert can_submit_change(base_job, project_id="alpha", current_user_id="alice")
    assert not can_submit_change(base_job, project_id="beta", current_user_id="alice")
    assert not can_submit_change({**base_job, "status": "failed"},
                                 project_id="alpha", current_user_id="alice")
    assert not can_submit_change({**base_job, "visibility": "private"},
                                 project_id="alpha", current_user_id="alice")

    project = {"id": "alpha", "is_team": True, "status": "active"}
    assert project_allows_dispatch(project)
    assert not project_allows_dispatch({**project, "status": "archived"})
    assert not project_allows_dispatch({"id": "alpha", "is_team": False})

    assert backend_switch_block_reason(streaming=True, creating_session=False)
    assert backend_switch_block_reason(streaming=False, creating_session=True)
    assert backend_switch_block_reason(streaming=False, creating_session=False) == ""

    assert team_workspace_available(connected=True, service_reachable=True,
                                   identity_verified=True)
    assert not team_workspace_available(connected=True, service_reachable=True,
                                        identity_verified=False)
    assert can_dispatch_team_task(project=project, api_available=True,
                                  model_ready=True, dispatch_in_flight=False)
    assert not can_dispatch_team_task(project=project, api_available=True,
                                     model_ready=False, dispatch_in_flight=False)
    assert not can_dispatch_team_task(project=project, api_available=True,
                                     model_ready=True, dispatch_in_flight=True)

    team_job = {"visibility": "team", "is_mine": False}
    assert not can_cancel_team_job(team_job, "member")
    assert can_cancel_team_job({**team_job, "is_mine": True}, "member")
    assert can_cancel_team_job(team_job, "admin")
    assert can_cancel_team_job(team_job, "owner")
    assert not can_cancel_team_job({"visibility": "private", "is_mine": True}, "owner")

    assert should_poll_team_jobs(api_available=True, tasks_page_visible=True,
                                 has_active_jobs=True)
    assert not should_poll_team_jobs(api_available=False, tasks_page_visible=True,
                                     has_active_jobs=True)
    assert not should_poll_team_jobs(api_available=True, tasks_page_visible=False,
                                     has_active_jobs=True)
    assert not should_poll_team_jobs(api_available=True, tasks_page_visible=True,
                                     has_active_jobs=False)

    changes = [{"id": "change-a"}, {"id": "change-b"}]
    assert selected_change_after_refresh(changes, "change-b") == "change-b"
    assert selected_change_after_refresh(changes, "removed") == "change-a"
    assert selected_change_after_refresh([], "change-b") == ""

    change = {"author_id": "alice", "status": "pending_review"}
    assert not can_review_change(change, current_user_id="alice")
    assert can_review_change(change, current_user_id="bob")
    assert not can_review_change(change, current_user_id="bob",
                                 project_status="archived")
    print("PASS workspace and project visibility boundaries")


if __name__ == "__main__":
    main()
