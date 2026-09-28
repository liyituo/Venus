"""Temporary-data API contract coverage for team Git changes and job privacy."""

from __future__ import annotations

import os
import json
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="venus_team_versions_"))
_DATA = _ROOT / "hub-data"
_WORKSPACE = _ROOT / "hub-workspace"
_DATA.mkdir()
_WORKSPACE.mkdir()
os.environ["VENUS_DATA_DIR"] = str(_DATA)
os.environ.pop("PCAGENT_DATA_DIR", None)
os.environ["PCAGENT_DISABLE_MCP"] = "1"
os.environ["PCAGENT_ALLOW_TEST_HOST"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import agent_jobs  # noqa: E402
import llm_server as L  # noqa: E402
import team_collab as T  # noqa: E402
import team_versions as V  # noqa: E402
import project_store as P  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def run_contract() -> tuple[int, int]:
    passed = failed = 0

    def check(name: str, ok: bool, extra: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name} {extra}")

    try:
        shared = _WORKSPACE / "shared"
        shared.mkdir()
        (shared / "README.md").write_text("base readme\n", encoding="utf-8")
        (shared / "a.md").write_text("base a\n", encoding="utf-8")
        (shared / "b.md").write_text("base b\n", encoding="utf-8")
        skill = shared / "skills" / "team-helper" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: team-helper\ndescription: Team shared skill\n---\nUse this skill.\n",
                         encoding="utf-8")
        (_WORKSPACE / ".env").write_text("TOKEN=should-not-be-imported\n", encoding="utf-8")
        (_WORKSPACE / "private.md").write_text("not shared\n", encoding="utf-8")
        L._workspace_path = _WORKSPACE.resolve()

        token_a, token_b = "team-test-alpha", "team-test-beta"
        token_c = "team-test-charlie"
        T.save_users([
            {"id": "u_alpha", "name": "Alpha", "token_hash": T.hash_token(token_a),
             "role": "admin", "color": "#C9573D"},
            {"id": "u_beta", "name": "Beta", "token_hash": T.hash_token(token_b),
             "role": "member", "color": "#2F8A5B"},
            {"id": "u_charlie", "name": "Charlie", "token_hash": T.hash_token(token_c),
             "role": "member", "color": "#4978B8"},
        ])
        T.save_collab_config({"team_mode": True, "default_required": 1,
                              "tool_required": {"delete_file": 2}})
        agent_jobs.set_job_handler(lambda _job_id: None)
        client = TestClient(L.app)
        ha = {"X-User-Token": token_a}
        hb = {"X-User-Token": token_b}
        hc = {"X-User-Token": token_c}

        r = client.post("/api/v1/projects", headers=ha,
                        json={"title": "Version Contract", "is_team": True})
        check("team project created", r.status_code == 200, r.text)
        project_id = str((r.json().get("created") or {}).get("id") or "")

        # This legacy contract exercises Git/worktree behavior with three
        # explicit test actors. The production Hub now requires per-project
        # device grants; that boundary is tested through real Serve HTTP in
        # project_access_contract_test.py. Keep this old fixture's fake actors
        # scoped to its main project instead of restoring Hub-wide access.
        def version_fixture_access(pid: str, user: dict) -> bool:
            project = P.get_project(pid, include_checkpoints=0)
            if not project:
                return False
            uid = str(user.get("id") or "")
            if pid == project_id:
                return uid in {"u_alpha", "u_beta", "u_charlie"}
            return uid == str((project.get("meta") or {}).get("owner_id") or "")

        def version_fixture_role(pid: str, user: dict, allowed: set[str]) -> dict:
            project = L._require_team_project(pid, user)
            assigned = ("owner" if str(user.get("id") or "") == "u_alpha"
                        else "member")
            if assigned not in allowed:
                raise L.HTTPException(403, "test actor role denied")
            return project

        L._can_access_project = version_fixture_access
        L._require_project_role = version_fixture_role
        L._active_team_reviewers = lambda pid: (
            {"u_alpha", "u_beta", "u_charlie"} if pid == project_id else {"u_alpha"})

        T.save_collab_config({"team_mode": True, "default_required": "invalid",
                              "tool_required": "team_change"})
        check("malformed approval configuration safely falls back to one vote",
              T.required_votes("team_change", project_id=project_id) == 1)
        T.save_collab_config({"team_mode": True, "default_required": 1,
                              "tool_required": {"delete_file": 2}})

        bypass = client.post("/api/v1/chat/stream", headers=ha, json={
            "messages": [{"role": "user", "content": "team edit"}],
            "agent": True, "project_id": project_id,
        })
        check("team agent stream cannot bypass isolated job workspace",
              bypass.status_code == 409, bypass.text)

        r = client.get("/api/v1/projects", headers=hb)
        check("team project visible to second member",
              r.status_code == 200 and any(p.get("id") == project_id
                                            for p in r.json().get("projects", [])), r.text)
        private_project = client.post("/api/v1/projects", headers=ha,
                                      json={"title": "Alpha private", "is_team": False})
        private_id = str((private_project.json().get("created") or {}).get("id") or "")
        team_ctx = L._team_job_override.set(True)
        project_ctx = L._task_project_override.set(project_id)
        try:
            ok, listed = L._execute_tool("list_projects", "{}")
            listed_projects = (json.loads(listed).get("projects") or []) if ok else []
            check("team agent project listing excludes private projects",
                  [p.get("id") for p in listed_projects] == [project_id])
            ok, _ = L._execute_tool("get_project",
                                    json.dumps({"project_id": private_id}))
            check("team agent cannot read another private project", not ok)
            ok, _ = L._execute_tool("remember",
                                    json.dumps({"content": "team task private memory"}))
            check("team agent cannot write personal memory", not ok)
            ok, _ = L._execute_tool("recall_memory",
                                    json.dumps({"query": "private preference"}))
            check("team agent cannot read personal memory", not ok)
        finally:
            L._task_project_override.reset(project_ctx)
            L._team_job_override.reset(team_ctx)
        r = client.post(f"/api/v1/projects/{project_id}/versions/init", headers=ha,
                        json={"shared_paths": ["../private.md"]})
        check("path traversal rejected during initialization", r.status_code == 400, r.text)
        r = client.post(f"/api/v1/projects/{project_id}/versions/init", headers=ha,
                        json={"shared_paths": ["shared"]})
        check("explicit shared path initializes repository",
              r.status_code == 200 and r.json().get("initialized"), r.text)

        other = client.post("/api/v1/projects", headers=ha,
                            json={"title": "Existing path guard", "is_team": True})
        other_pid = str((other.json().get("created") or {}).get("id") or "")
        reserved_repo = V._repo(other_pid)
        reserved_repo.mkdir(parents=True)
        sentinel = reserved_repo / "keep.txt"
        sentinel.write_text("pre-existing user data", encoding="utf-8")
        blocked = client.post(f"/api/v1/projects/{other_pid}/versions/init", headers=ha,
                              json={"shared_paths": ["shared"]})
        check("initialization preserves an unmanaged existing directory",
              blocked.status_code == 409 and sentinel.read_text(encoding="utf-8")
              == "pre-existing user data", blocked.text)
        repo_files = V._git(V._repo(project_id), "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        check("private workspace files excluded from repository",
              set(repo_files) == {"shared/README.md", "shared/a.md", "shared/b.md",
                                  "shared/skills/team-helper/SKILL.md"}, repo_files)
        r = client.get(f"/api/v1/projects/{project_id}/versions", headers=hb)
        base_sha = str(r.json().get("head") or "")
        check("second member reads shared repository", r.status_code == 200 and bool(base_sha), r.text)
        spoof_repo = _ROOT / "spoof-git-config"
        spoof_repo.mkdir()
        V._git(spoof_repo, "init", "--initial-branch=main")
        V._git(spoof_repo, "-c", "user.name=Spoof", "-c", "user.email=spoof@venus.invalid",
               "commit", "--allow-empty", "-m", "wrong repository")
        old_git_dir = os.environ.get("GIT_DIR")
        os.environ["GIT_DIR"] = str(spoof_repo / ".git")
        try:
            isolated_head = V._head(V._repo(project_id))
        finally:
            if old_git_dir is None:
                os.environ.pop("GIT_DIR", None)
            else:
                os.environ["GIT_DIR"] = old_git_dir
        check("Git subprocess ignores inherited repository overrides",
              isolated_head == base_sha, isolated_head)
        (spoof_repo / "output.txt").write_bytes(b"x" * (2 * 1024 * 1024))
        V._git(spoof_repo, "add", "--", "output.txt")
        V._git(spoof_repo, "-c", "user.name=Output", "-c",
               "user.email=output@venus.invalid", "commit", "-m", "large output")
        bounded = V._git(spoof_repo, "show", "HEAD:output.txt", max_output=1024,
                          allow_truncate=True)
        check("Git output capture stays within configured bound", len(bounded) == 1024)

        routed = client.post("/api/v1/dispatch", headers=ha, json={
            "text": "short team task", "force_mode": "sync",
            "project_id": project_id, "request_id": "team-dispatch",
            "session_id": 987654,
        })
        routed_job = routed.json().get("job") or {}
        check("team dispatch forces an isolated asynchronous job",
              routed.status_code == 200 and routed.json().get("mode") == "async"
              and routed_job.get("visibility") == "team"
              and Path(routed_job.get("workspace") or "").parent.name == "worktrees"
              and routed_job.get("session_id") is None, routed.text)

        def create_job(token: str, title: str, request_id: str) -> dict:
            response = client.post("/api/v1/jobs", headers={"X-User-Token": token}, json={
                "messages": [{"role": "user", "content": title}],
                "title": title, "project_id": project_id, "request_id": request_id,
                "session_id": 987654,
            })
            if response.status_code != 200:
                check(f"create {title}", False, response.text)
                return {}
            job = response.json().get("job") or {}
            agent_jobs.update_job(job["id"], status="completed", finished_at=1)
            return agent_jobs.get_job(job["id"]) or job

        job_a = create_job(token_a, "Alpha task", "alpha-task")
        job_b = create_job(token_b, "Beta task", "beta-task")
        check("two tasks get separate worktrees and same base",
              bool(job_a and job_b and job_a["workspace"] != job_b["workspace"]
                   and V._head(Path(job_a["workspace"])) == base_sha
                   and V._head(Path(job_b["workspace"])) == base_sha),
              f"{job_a.get('workspace')} {job_b.get('workspace')}")
        check("jobs are explicitly team-visible and owner-bound",
              job_a.get("visibility") == "team" and job_a.get("owner") == "u_alpha"
              and job_b.get("owner") == "u_beta")
        check("team jobs do not attach to a member's private session",
              job_a.get("session_id") is None and job_b.get("session_id") is None)
        bulk_dir = Path(job_a["workspace"]) / "shared" / "bulk-limit"
        bulk_dir.mkdir()
        for index in range(101):
            (bulk_dir / f"file-{index:03d}.md").write_text("x", encoding="utf-8")
        too_many = client.post(f"/api/v1/jobs/{job_a['id']}/changes/commit", headers=ha,
                               json={"paths": ["shared/bulk-limit"],
                                     "purpose": "Path-count limit",
                                     "request_id": "too-many-paths"})
        check("directory expansion enforces file-count limit",
              too_many.status_code == 413, too_many.text)
        shutil.rmtree(bulk_dir)
        team_ctx = L._team_job_override.set(True)
        project_ctx = L._task_project_override.set(project_id)
        workspace_ctx = L._workspace_override.set(Path(job_a["workspace"]))
        try:
            ok, skill_text = L._execute_tool("load_skill",
                                             json.dumps({"name": "team-helper"}))
            check("team agent loads a skill only from declared project files",
                  ok and "Use this skill." in skill_text)
            ok, _ = L._execute_tool("load_skill", json.dumps({"name": "private-skill"}))
            check("team agent cannot load Hub-only skills", not ok)
            ok, _ = L._execute_tool("list_todos", "{}")
            check("team agent cannot read personal task lists", not ok)
        finally:
            L._workspace_override.reset(workspace_ctx)
            L._task_project_override.reset(project_ctx)
            L._team_job_override.reset(team_ctx)

        (Path(job_a["workspace"]) / "shared" / "a.md").write_text("alpha change\n", encoding="utf-8")
        (Path(job_a["workspace"]) / "private.md").write_text("private untracked\n", encoding="utf-8")
        r = client.get(f"/api/v1/jobs/{job_a['id']}/workspace", headers=ha)
        check("task workspace reports shared changes only",
              r.status_code == 200 and r.json().get("files") == ["shared/a.md"], r.text)
        r = client.post(f"/api/v1/jobs/{job_a['id']}/changes/commit", headers=ha,
                        json={"paths": ["shared/a.md", "private.md"],
                              "purpose": "Alpha docs", "request_id": "commit-alpha"})
        check("unshared path rejected from explicit commit", r.status_code == 403, r.text)
        r = client.post(f"/api/v1/jobs/{job_a['id']}/changes/commit", headers=ha,
                        json={"paths": ["shared/a.md", "../private.md"],
                              "purpose": "Alpha docs", "request_id": "commit-bad"})
        check("path traversal rejected from commit", r.status_code == 400, r.text)
        (Path(job_a["workspace"]) / "shared" / "settings.md").write_text(
            'api_key = "abcdefghijklmnopqrstuvwxyz012345"\n', encoding="utf-8")
        r = client.post(f"/api/v1/jobs/{job_a['id']}/changes/commit", headers=ha,
                        json={"paths": ["shared/settings.md"], "purpose": "secret test",
                              "request_id": "commit-secret"})
        check("secret-looking content rejected", r.status_code == 403, r.text)
        (Path(job_a["workspace"]) / "shared" / "settings.md").unlink()
        r = client.post(f"/api/v1/jobs/{job_a['id']}/changes/commit", headers=ha,
                        json={"paths": ["shared/a.md"], "purpose": "Alpha docs",
                              "request_id": "commit-alpha"})
        check("explicit path commit creates pending change",
              r.status_code == 200 and r.json()["change"].get("status") == "pending_review", r.text)
        change_a = r.json()["change"]
        before_events = len([e for e in (agent_jobs.get_job(job_a["id"]) or {}).get("events", [])
                             if e.get("kind") == "change_submitted"])
        repeated = client.post(f"/api/v1/jobs/{job_a['id']}/changes/commit", headers=ha,
                               json={"paths": ["shared/a.md"], "purpose": "Alpha docs",
                                     "request_id": "commit-alpha"})
        after_events = len([e for e in (agent_jobs.get_job(job_a["id"]) or {}).get("events", [])
                            if e.get("kind") == "change_submitted"])
        check("repeated commit request does not duplicate task events",
              repeated.status_code == 200 and repeated.json()["change"].get("already_processed")
              and before_events == after_events, repeated.text)
        committed_a = V._git(Path(job_a["workspace"]), "diff-tree", "--no-commit-id",
                             "--name-only", "-r", change_a["current_sha"]).splitlines()
        check("commit contains only the selected path", committed_a == ["shared/a.md"], committed_a)
        r = client.get(f"/api/v1/projects/{project_id}/changes/{change_a['id']}/diff", headers=hb)
        check("second member reads exact diff",
              r.status_code == 200 and "alpha change" in r.json().get("diff", ""), r.text)
        r = client.post(f"/api/v1/projects/{project_id}/changes/{change_a['id']}/review",
                        headers=ha, json={"decision": "approve",
                                         "reviewed_sha": change_a["current_sha"]})
        check("author cannot approve own change", r.status_code == 403, r.text)
        r = client.post(f"/api/v1/projects/{project_id}/changes/{change_a['id']}/review",
                        headers=hb, json={"decision": "approve", "reviewed_sha": "wrong-sha"})
        check("review must name displayed commit SHA", r.status_code == 409, r.text)
        r = client.post(f"/api/v1/projects/{project_id}/changes/{change_a['id']}/review",
                        headers=hb, json={"decision": "approve",
                                         "reviewed_sha": change_a["current_sha"]})
        check("other member approval records identity and SHA",
              r.status_code == 200 and r.json()["change"]["reviews"][-1]["reviewer_id"] == "u_beta"
              and r.json()["change"]["reviews"][-1]["reviewed_sha"] == change_a["current_sha"], r.text)

        (Path(job_b["workspace"]) / "shared" / "b.md").write_text("beta change\n", encoding="utf-8")
        V._git(Path(job_b["workspace"]), "add", "--", "shared/README.md")
        r = client.post(f"/api/v1/jobs/{job_b['id']}/changes/commit", headers=hb,
                        json={"paths": ["shared/b.md"], "purpose": "Beta docs",
                              "request_id": "commit-beta-1"})
        change_b = (r.json().get("change") or {}) if r.status_code == 200 else {}
        check("second task commits independently", r.status_code == 200
              and change_b.get("base_sha") == base_sha, r.text)
        committed_b = V._git(Path(job_b["workspace"]), "diff-tree", "--no-commit-id",
                             "--name-only", "-r", change_b["current_sha"]).splitlines()
        check("pre-staged unrelated path is excluded", committed_b == ["shared/b.md"], committed_b)
        r = client.post(f"/api/v1/projects/{project_id}/changes/{change_a['id']}/merge", headers=ha)
        check("approved change merges", r.status_code == 200
              and r.json()["change"].get("status") == "merged", r.text)
        (Path(job_a["workspace"]) / "shared" / "a.md").write_text(
            "post-merge edit\n", encoding="utf-8")
        r = client.post(f"/api/v1/jobs/{job_a['id']}/changes/commit", headers=ha,
                        json={"paths": ["shared/a.md"], "purpose": "late append",
                              "request_id": "commit-after-merge"})
        check("merged change cannot be reopened by a later task commit",
              r.status_code == 409
              and V.get_change(project_id, change_a["id"]).get("status") == "merged", r.text)

        r = client.post(f"/api/v1/projects/{project_id}/changes/{change_b['id']}/review",
                        headers=ha, json={"decision": "approve",
                                         "reviewed_sha": change_b.get("current_sha")})
        check("other member approves B change", r.status_code == 200, r.text)
        r = client.post(f"/api/v1/projects/{project_id}/changes/{change_b['id']}/merge", headers=hb)
        check("stale base returns understandable 409",
              r.status_code == 409 and "基线已过期" in r.json().get("detail", ""), r.text)
        stale = client.get(f"/api/v1/projects/{project_id}/changes/{change_b['id']}",
                           headers=hb).json().get("change") or {}
        check("stale merge immediately invalidates prior approval",
              stale.get("status") == "stale"
              and all(review.get("invalidated") for review in stale.get("reviews", [])))
        job_events = (agent_jobs.get_job(job_b["id"]) or {}).get("events", [])
        check("stale transition appears in linked task events",
              any(event.get("kind") == "change_stale" for event in job_events))
        r = client.post(f"/api/v1/projects/{project_id}/changes/{change_b['id']}/rebase", headers=hb)
        refreshed = (r.json().get("change") or {}) if r.status_code == 200 else {}
        check("explicit baseline refresh invalidates old approval",
              r.status_code == 200 and refreshed.get("status") == "pending_review"
              and all(review.get("invalidated") for review in refreshed.get("reviews", [])), r.text)
        r = client.post(f"/api/v1/projects/{project_id}/changes/{change_b['id']}/review",
                        headers=ha, json={"decision": "approve",
                                         "reviewed_sha": refreshed.get("current_sha")})
        check("rebased SHA requires new approval", r.status_code == 200, r.text)
        (Path(job_b["workspace"]) / "shared" / "b.md").write_text(
            "beta amended after review\n", encoding="utf-8")
        r = client.post(f"/api/v1/jobs/{job_b['id']}/changes/commit", headers=hb,
                        json={"paths": ["shared/b.md"], "purpose": "Beta follow-up",
                              "request_id": "commit-beta-2"})
        amended = (r.json().get("change") or {}) if r.status_code == 200 else {}
        check("author append invalidates previous approval",
              r.status_code == 200 and amended.get("status") == "pending_review"
              and any(rv.get("invalidated") for rv in amended.get("reviews", [])), r.text)
        r = client.post(f"/api/v1/projects/{project_id}/changes/{change_b['id']}/review",
                        headers=ha, json={"decision": "approve",
                                         "reviewed_sha": amended.get("current_sha")})
        check("appended commit can be reviewed at new SHA", r.status_code == 200, r.text)
        r = client.post(f"/api/v1/projects/{project_id}/changes/{change_b['id']}/merge", headers=ha)
        check("rebased and reviewed change merges", r.status_code == 200, r.text)

        T.save_collab_config({"team_mode": True, "default_required": 2,
                              "tool_required": {"delete_file": 2}})
        L._projects.set_active_project(private_id)
        policy_job = create_job(token_a, "Multi-review policy", "multi-review-policy")
        T.save_collab_config({"team_mode": True, "default_required": 1,
                              "tool_required": {"delete_file": 2}})
        (Path(policy_job["workspace"]) / "shared" / "README.md").write_text(
            "multi reviewer policy\n", encoding="utf-8")
        r = client.post(f"/api/v1/jobs/{policy_job['id']}/changes/commit", headers=ha,
                        json={"paths": ["shared/README.md"], "purpose": "Policy test",
                              "request_id": "commit-policy"})
        policy_change = (r.json().get("change") or {}) if r.status_code == 200 else {}
        check("change captures project-bound approval threshold at task creation",
              r.status_code == 200 and policy_change.get("required_approvals") == 2, r.text)
        r = client.post(f"/api/v1/projects/{project_id}/changes/{policy_change.get('id')}/review",
                        headers=hb, json={"decision": "approve",
                                          "reviewed_sha": policy_change.get("current_sha")})
        check("first approval remains pending when policy requires two reviewers",
              r.status_code == 200 and r.json()["change"].get("status") == "pending_review"
              and r.json()["change"].get("approval_count") == 1, r.text)
        repeated_vote = client.post(
            f"/api/v1/projects/{project_id}/changes/{policy_change.get('id')}/review",
            headers=hb, json={"decision": "approve",
                              "reviewed_sha": policy_change.get("current_sha")})
        check("duplicate review request is idempotent and does not add a vote",
              repeated_vote.status_code == 200
              and repeated_vote.json()["change"].get("already_processed")
              and repeated_vote.json()["change"].get("approval_count") == 1,
              repeated_vote.text)
        changed_vote = client.post(
            f"/api/v1/projects/{project_id}/changes/{policy_change.get('id')}/review",
            headers=hb, json={"decision": "reject",
                              "reviewed_sha": policy_change.get("current_sha")})
        check("reviewer cannot switch votes on the same SHA",
              changed_vote.status_code == 409, changed_vote.text)
        r = client.post(f"/api/v1/projects/{project_id}/changes/{policy_change.get('id')}/review",
                        headers=hc, json={"decision": "approve",
                                          "reviewed_sha": policy_change.get("current_sha")})
        check("distinct second reviewer satisfies captured approval threshold",
              r.status_code == 200 and r.json()["change"].get("status") == "approved"
              and r.json()["change"].get("approval_count") == 2, r.text)
        r = client.post(f"/api/v1/projects/{project_id}/changes/{policy_change.get('id')}/merge",
                        headers=hc)
        check("multi-approved change merges successfully", r.status_code == 200, r.text)

        job_c = create_job(token_a, "Later edit to B file", "later-b-file")
        (Path(job_c["workspace"]) / "shared" / "b.md").write_text(
            "later beta version\n", encoding="utf-8")
        r = client.post(f"/api/v1/jobs/{job_c['id']}/changes/commit", headers=ha,
                        json={"paths": ["shared/b.md"], "purpose": "Later beta update",
                              "request_id": "commit-later-b"})
        change_c = (r.json().get("change") or {}) if r.status_code == 200 else {}
        if change_c:
            client.post(f"/api/v1/projects/{project_id}/changes/{change_c['id']}/review",
                        headers=hb, json={"decision": "approve",
                                          "reviewed_sha": change_c.get("current_sha")})
            merged_c = client.post(
                f"/api/v1/projects/{project_id}/changes/{change_c['id']}/merge", headers=ha)
        else:
            merged_c = r
        check("later overlapping edit merges before testing revert conflict",
              merged_c.status_code == 200, merged_c.text)
        r = client.post(f"/api/v1/projects/{project_id}/changes/{change_b['id']}/revert",
                        headers=ha, json={"request_id": "revert-beta-conflict"})
        check("overlapping revert returns explicit 409",
              r.status_code == 409 and "冲突" in r.json().get("detail", ""), r.text)
        source_b = client.get(
            f"/api/v1/projects/{project_id}/changes/{change_b['id']}", headers=hb
        ).json().get("change") or {}
        conflict_id = str(source_b.get("revert_change_id") or "")
        conflict_change = client.get(
            f"/api/v1/projects/{project_id}/changes/{conflict_id}", headers=hb
        ).json().get("change") or {}
        check("revert conflict keeps one traceable stale change",
              conflict_change.get("status") == "stale"
              and bool(conflict_change.get("error"))
              and bool(agent_jobs.get_job(conflict_change.get("job_id") or "")))
        conflict_job = agent_jobs.get_job(conflict_change.get("job_id") or {}) or {}
        check("revert conflict is recorded on its task",
              any(event.get("kind") == "change_revert_conflict"
                  for event in conflict_job.get("events", [])))
        before_retry = len(V.list_changes(project_id))
        r2 = client.post(f"/api/v1/projects/{project_id}/changes/{change_b['id']}/revert",
                         headers=ha, json={"request_id": "revert-beta-conflict"})
        check("repeated conflicting revert does not create another result",
              r2.status_code == 409 and len(V.list_changes(project_id)) == before_retry
              and (client.get(f"/api/v1/projects/{project_id}/changes/{change_b['id']}",
                              headers=hb).json().get("change") or {}).get("revert_change_id")
              == conflict_id, r2.text)

        r = client.post(f"/api/v1/projects/{project_id}/changes/{change_a['id']}/revert",
                        headers=hb, json={"request_id": "revert-alpha"})
        revert = (r.json().get("change") or {}) if r.status_code == 200 else {}
        check("revert creates a new pending-review change",
              r.status_code == 200 and revert.get("status") == "pending_review"
              and revert.get("reverts_change_id") == change_a["id"], r.text)
        r2 = client.post(f"/api/v1/projects/{project_id}/changes/{change_a['id']}/revert",
                         headers=hb, json={"request_id": "revert-alpha"})
        check("repeated revert request reuses the same change",
              r2.status_code == 200 and r2.json()["change"].get("id") == revert.get("id")
              and r2.json().get("already_processed"), r2.text)
        r = client.post(f"/api/v1/projects/{project_id}/changes/{revert['id']}/review",
                        headers=ha, json={"decision": "approve",
                                         "reviewed_sha": revert.get("current_sha")})
        check("second member approves revert", r.status_code == 200, r.text)
        r = client.post(f"/api/v1/projects/{project_id}/changes/{revert['id']}/merge", headers=hb)
        check("approved revert merges as a new commit", r.status_code == 200, r.text)
        final_a = (V._repo(project_id) / "shared" / "a.md").read_text(encoding="utf-8")
        check("revert preserves history and restores file contents", final_a == "base a\n", final_a)

        private = client.post("/api/v1/jobs", headers=ha, json={
            "messages": [{"role": "user", "content": "private job"}],
            "title": "Private job", "request_id": "private-job",
        })
        private_job = private.json().get("job") or {}
        agent_jobs.update_job(private_job["id"], status="completed", finished_at=1)
        check("private job is absent from other member list",
              all(row.get("id") != private_job["id"]
                  for row in client.get("/api/v1/jobs?limit=200", headers=hb).json().get("jobs", [])))
        check("private job detail is hidden from other member",
              client.get(f"/api/v1/jobs/{private_job['id']}", headers=hb).status_code == 404)
        check("private task events are hidden from other member",
              client.get(f"/api/v1/jobs/{private_job['id']}/events", headers=hb).status_code == 404)
        check("private task cancellation is hidden from other member",
              client.post(f"/api/v1/jobs/{private_job['id']}/cancel", headers=hb).status_code == 404)
        check("bad token gets 401", client.get("/api/v1/jobs", headers={
            "X-User-Token": "not-a-member"}).status_code == 401)

        audit = client.get("/api/v1/audit", headers=ha).json().get("entries") or []
        change_audit = [row for row in audit
                        if row.get("action") in ("team.change.review", "team.change.merge",
                                                  "team.change.stale")]
        check("review and merge actions appear in audit", bool(change_audit)
              and all((row.get("detail") or {}).get("sha") for row in change_audit))
        check("stale transition is audited", any(row.get("action") == "team.change.stale"
                                                   for row in change_audit))
        check("revert conflict is audited with actor and SHA", any(
            row.get("action") == "team.change.revert_conflict"
            and row.get("user") == "u_alpha"
            and (row.get("detail") or {}).get("sha")
            for row in audit))

    except Exception as exc:
        failed += 1
        print(f"  FAIL  unexpected exception: {type(exc).__name__}: {exc}")
    finally:
        try:
            worker = getattr(agent_jobs, "_worker", None)
            if worker is not None:
                worker._stop.set()
        except Exception:
            pass
        try:
            L._memory_worker._stop.set()
        except Exception:
            pass
        shutil.rmtree(_ROOT, ignore_errors=True)
    return passed, failed


if __name__ == "__main__":
    passed, failed = run_contract()
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
