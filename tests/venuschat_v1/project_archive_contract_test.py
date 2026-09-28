"""Display-free contracts for owner-only project archive controls."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from venuschat_v1.project_hub_view import (  # noqa: E402
    project_can_be_archived,
    project_worker_actions_enabled,
)
from venuschat_v1.team_collab_view import (  # noqa: E402
    is_archived_project,
    project_writes_enabled,
)
from venuschat_v1.worker_task_view import project_worker_writes_enabled  # noqa: E402


def main() -> None:
    assert project_can_be_archived({"role": "owner", "status": "active"})
    assert not project_can_be_archived({"role": "admin", "status": "active"})
    assert not project_can_be_archived({"role": "member", "status": "active"})
    assert not project_can_be_archived({"role": "owner", "status": "archived"})
    assert not project_can_be_archived({"role": "owner", "status": "pending_claim"})
    assert project_worker_actions_enabled({"status": "active", "is_team": True})
    assert not project_worker_actions_enabled({"status": "archived", "is_team": True})
    assert not project_worker_actions_enabled({"status": "active", "is_team": False})
    assert project_worker_writes_enabled({"status": "active"})
    assert not project_worker_writes_enabled({"status": "archived"})
    assert is_archived_project({"status": "archived"})
    assert not is_archived_project({"status": "active"})
    assert project_writes_enabled({"status": "active"})
    assert not project_writes_enabled({"status": "archived"})
    assert not project_writes_enabled({"status": "active"}, read_only=True)
    assert not project_writes_enabled({"status": "pending_claim"})
    print("PASS project archive owner and status contract")


if __name__ == "__main__":
    main()
