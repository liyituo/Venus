"""Isolated HTTP contract for multi-project Hub and Worker authorization."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="venus_project_access_"))
os.environ["VENUS_DATA_DIR"] = str(TMP / "hub-data")
os.environ["PCAGENT_DISABLE_MCP"] = "1"
os.environ["PCAGENT_ALLOW_TEST_HOST"] = "1"
sys.path.insert(0, str(ROOT / "src"))

import agent_jobs  # noqa: E402
import llm_server as hub  # noqa: E402
import team_enrollment  # noqa: E402
import worker_hub  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

logging.getLogger().setLevel(logging.ERROR)
HOST = "hub.example.ts.net"
client = TestClient(hub.app, client=("127.0.0.1", 51001))
hub.ISOLATED = True
hub.TEAM_SERVE_MODE = True
hub.TEAM_SERVE_HOST = HOST
hub.AUTH_TOKEN = ""


def headers(login: str, token: str = "") -> dict[str, str]:
    result = {"Host": HOST, "Tailscale-User-Login": login}
    if token:
        result["X-Team-Device-Token"] = token
    return result


def check(response, expected: int, label: str) -> dict:
    assert response.status_code == expected, f"{label}: {response.status_code} {response.text}"
    return response.json()


def enroll(login: str, name: str, admin_headers: dict) -> tuple[dict, str, dict]:
    invite = check(client.post("/api/v1/team/invites", headers=admin_headers,
                               json={"expected_login": login}), 200, "hub invite")
    secret = uuid.uuid4().hex + uuid.uuid4().hex
    request_id = uuid.uuid4().hex
    application = check(client.post("/api/v1/team/join-requests", headers=headers(login),
                                    json={"invite_code": invite["invite_code"],
                                          "display_name": name, "device_name": f"{name} laptop",
                                          "claim_secret": secret, "request_id": request_id}),
                        200, "hub application")["application"]
    check(client.post(f"/api/v1/team/applications/{application['id']}/review",
                      headers=admin_headers, json={"decision": "approve"}), 200, "hub approval")
    joined = check(client.post(f"/api/v1/team/join-requests/{application['id']}/claim",
                               headers=headers(login), json={"claim_secret": secret}),
                   200, "hub device claim")
    token = joined["device_token"]
    me = check(client.get("/api/v1/team/me", headers=headers(login, token)), 200, "hub me")
    return me["user"], token, me["device"]


def create_and_claim(actor_headers: dict, name: str) -> tuple[str, str]:
    made = check(client.post("/api/v1/projects", headers=actor_headers,
                             json={"name": name, "description": name + " scope"}),
                 200, "project create")
    project_id = made["created"]["id"]
    assert made["project"]["status"] == "pending_claim"
    assert "required_approvals" not in made["project"]
    assert "policy_version" not in made["project"]
    return project_id, made["claim_code"]


def invite_project(owner_headers: dict, project_id: str,
                   target_user: dict, target_device: dict, role: str = "member") -> dict:
    made = check(client.post(f"/api/v1/projects/{project_id}/invites",
                             headers=owner_headers,
                             json={"terminal_display_code": target_device["display_code"],
                                   "user_id": target_user["id"], "role": role}),
                 200, "project invite")
    return made


def accept_project(target_headers: dict, made: dict) -> None:
    code = made["invite_code"]
    preview = check(client.post("/api/v1/project-invites/preview",
                                headers=target_headers, json={"invite_code": code}),
                    200, "project invite preview")
    assert preview["invite"]["id"] == made["invite"]["id"]
    check(client.post(f"/api/v1/project-invites/{made['invite']['id']}/accept",
                      headers=target_headers, json={"invite_code": code}),
          200, "project invite accept")


def run() -> None:
    team_enrollment.ensure_bootstrap_credential()
    bootstrap = team_enrollment.bootstrap_credential_local()
    admin = check(client.post("/api/v1/team/bootstrap",
                              headers={"Host": "127.0.0.1:8001",
                                       "X-Team-Bootstrap-Token": bootstrap},
                              json={"team_name": "Project Contract",
                                    "admin_login": "alice@example.com",
                                    "admin_name": "Alice", "device_name": "Alice laptop"}),
                  200, "bootstrap")
    alice = admin["user"]
    alice_device = team_enrollment.get_device(admin["device_id"])
    ha = headers("alice@example.com", admin["device_token"])
    bob, bt, bob_device = enroll("bob@example.com", "Bob", ha)
    carol, ct, carol_device = enroll("carol@example.com", "Carol", ha)
    hb, hc = headers("bob@example.com", bt), headers("carol@example.com", ct)

    p1, code1 = create_and_claim(ha, "Project One")
    p2, code2 = create_and_claim(hc, "Project Two")
    assert client.post(f"/api/v1/projects/{p1}/claim", headers=hb,
                       json={"claim_code": code1}).status_code == 403
    check(client.post(f"/api/v1/projects/{p1}/claim", headers=ha,
                      json={"claim_code": code1}), 200, "claim p1")
    check(client.post(f"/api/v1/projects/{p2}/claim", headers=hc,
                      json={"claim_code": code2}), 200, "claim p2")
    assert client.post(f"/api/v1/projects/{p1}/claim", headers=ha,
                       json={"claim_code": code1}).status_code == 410
    assert client.get(f"/api/v1/projects/{p1}", headers=hc).status_code == 404
    assert client.get(f"/api/v1/projects/{p2}", headers=ha).status_code == 404
    assert [p["id"] for p in check(client.get("/api/v1/projects", headers=hb),
                                   200, "Bob project list")["projects"]] == []

    invite = invite_project(ha, p1, bob, bob_device)
    # Simulate an admin invitation issued by an older server version; an
    # already-issued code must not bypass the new governance path on acceptance.
    legacy_invite_id = "pinv_legacy_admin_" + uuid.uuid4().hex[:8]
    legacy_invite_code = "legacy-admin-code-" + uuid.uuid4().hex
    legacy_now = hub.project_access._now()
    with hub.project_access._connection() as conn:
        conn.execute("""INSERT INTO project_invites
            (invite_id,project_id,expected_user_id,device_id,role,secret_hash,
             status,created_by,created_at,expires_at)
            VALUES(?,?,?,?,?,?,'issued',?,?,?)""",
            (legacy_invite_id, p1, carol["id"], carol_device["id"], "admin",
             hub.project_access._hash_secret(legacy_invite_code), alice["id"],
             legacy_now, legacy_now + 3600))
    assert client.post(f"/api/v1/project-invites/{legacy_invite_id}/accept",
                       headers=hc, json={"invite_code": legacy_invite_code}).status_code == 409
    with hub.project_access._connection() as conn:
        legacy_state = conn.execute("SELECT status FROM project_invites WHERE invite_id=?",
                                    (legacy_invite_id,)).fetchone()
    assert legacy_state["status"] == "revoked"
    assert client.post(f"/api/v1/projects/{p1}/invites", headers=ha,
                       json={"terminal_display_code": carol_device["display_code"],
                             "user_id": carol["id"], "role": "admin"}).status_code == 409
    assert client.post("/api/v1/project-invites/preview", headers=hc,
                       json={"invite_code": invite["invite_code"]}).status_code == 403
    accept_project(hb, invite)
    assert client.post(f"/api/v1/project-invites/{invite['invite']['id']}/accept",
                       headers=hb, json={"invite_code": invite["invite_code"]}).status_code == 410
    assert client.get(f"/api/v1/projects/{p1}", headers=hb).status_code == 200
    assert client.get(f"/api/v1/projects/{p2}", headers=hb).status_code == 404

    # A member can read but cannot initialize or manage a repository.
    assert client.post(f"/api/v1/projects/{p1}/versions/init", headers=hb,
                       json={"shared_paths": []}).status_code == 403
    assert client.delete(f"/api/v1/projects/{p1}/members/{alice['id']}",
                         headers=ha).status_code == 409
    assert client.post(f"/api/v1/projects/{p1}/devices/{admin['device_id']}/revoke",
                       headers=ha, json={}).status_code == 409
    check(client.patch(f"/api/v1/projects/{p1}/members/{bob['id']}/role",
                       headers=ha, json={"role": "viewer"}), 200, "demote to viewer")
    assert client.post(f"/api/v1/projects/{p1}/invites", headers=hb,
                       json={"terminal_display_code": carol_device["display_code"],
                             "user_id": carol["id"], "role": "member"}).status_code == 403
    assert client.post(f"/api/v1/projects/{p1}/versions/init", headers=hb,
                       json={"shared_paths": []}).status_code == 403
    check(client.patch(f"/api/v1/projects/{p1}/members/{bob['id']}/role",
                       headers=ha, json={"role": "member"}), 200, "restore member")
    check(client.put(f"/api/v1/projects/{p1}/policy", headers=ha,
                     json={"required_approvals": 2}), 200, "project policy")
    assert client.put(f"/api/v1/projects/{p1}/policy", headers=ha,
                      json={"required_approvals": 1}).status_code == 409
    assert client.patch(f"/api/v1/projects/{p1}/members/{bob['id']}/role",
                        headers=ha, json={"role": "admin"}).status_code == 409
    check(client.post("/api/v1/projects/active", headers=hb,
                      json={"project_id": p1}), 200, "Bob active project")
    assert check(client.get("/api/v1/projects", headers=hb),
                 200, "Bob active read")["active"] == p1
    assert check(client.get("/api/v1/projects", headers=ha),
                 200, "Alice active read")["active"] == ""

    # Shared machine-wide APIs are closed on Serve, including model tools.
    assert client.get("/api/v1/memory", headers=hb).status_code == 403
    assert client.post("/api/v1/config", headers=hb, json={"model": "x"}).status_code == 403
    assert client.post("/api/v1/chat/stream", headers=hb,
                       json={"messages": [{"role": "user", "content": "x"}],
                             "agent": True}).status_code == 409
    assert client.post("/api/v1/jobs", headers=hb,
                       json={"messages": [{"role": "user", "content": "x"}]}).status_code == 422
    assert hub._execute_tool("run_shell", '{"command":"echo hi"}')[0] is False
    assert hub._execute_tool("get_project", '{"project_id":"' + p1 + '"}')[0] is False

    # Tool execution revalidates the persisted Job actor's device grant on
    # every call; a previously authorized context cannot survive revocation.
    project_token = hub._task_project_override.set(p1)
    actor_token = hub._task_user_override.set(
        {"id": bob["id"], "device_id": bob_device["id"], "name": "Bob"})
    team_token = hub._team_job_override.set(True)
    try:
        allowed, _ = hub._execute_tool("get_project", '{"project_id":"' + p1 + '"}')
        assert allowed, "active member should be able to read the bound project"
        stopped, _ = hub._execute_tool("stop", "{}")
        assert not stopped, "Team Serve must never expose the Hub-global stop tool"
        check(client.post(f"/api/v1/projects/{p1}/devices/{bob_device['id']}/revoke",
                          headers=ha, json={}), 200, "revoke active Job actor device")
        denied, reason = hub._execute_tool(
            "get_project", '{"project_id":"' + p1 + '"}')
        assert not denied and "授权已失效" in reason, (
            "an in-flight tool context must lose project access after device revocation")
    finally:
        hub._team_job_override.reset(team_token)
        hub._task_user_override.reset(actor_token)
        hub._task_project_override.reset(project_token)
    # Reauthorize Bob so the following role and Worker contracts exercise an
    # active project member, not the deliberately revoked device.
    rebinding = invite_project(ha, p1, bob, bob_device)
    accept_project(hb, rebinding)

    # Worker calls are scoped to a live team Job. A queued call can complete
    # after the Job finishes, but failed/cancelled Jobs cannot dispatch it.
    accept_project(hc, invite_project(ha, p1, carol, carol_device))

    # Sensitive project governance uses one-time proposals and two distinct
    # current, non-proposer votes. Role changes invalidate old votes permanently.
    promotion_request_id = "promote-bob-" + uuid.uuid4().hex[:12]
    promotion = check(client.post(f"/api/v1/projects/{p1}/governance-proposals",
                                  headers=ha, json={
                                      "action": "promote_admin",
                                      "target_user_id": bob["id"],
                                      "request_id": promotion_request_id}),
                      200, "admin promotion proposal")["proposal"]
    assert promotion["status"] == "pending"
    assert promotion["required_approvals"] == 2
    assert promotion["parameters_sha256"]
    retry_promotion = check(client.post(
        f"/api/v1/projects/{p1}/governance-proposals", headers=ha, json={
            "action": "promote_admin", "target_user_id": bob["id"],
            "request_id": promotion_request_id}), 200,
        "idempotent governance proposal retry")["proposal"]
    assert retry_promotion["id"] == promotion["id"]
    assert retry_promotion["already_processed"] is True
    assert client.post(f"/api/v1/projects/{p1}/governance-proposals/"
                       f"{promotion['id']}/votes", headers=ha,
                       json={"decision": "approve"}).status_code == 403
    bob_vote = check(client.post(f"/api/v1/projects/{p1}/governance-proposals/"
                                 f"{promotion['id']}/votes", headers=hb,
                                 json={"decision": "approve"}), 200,
                     "Bob promotion vote")["proposal"]
    assert bob_vote["approvals"] == 1
    repeated_bob_vote = check(client.post(
        f"/api/v1/projects/{p1}/governance-proposals/{promotion['id']}/votes",
        headers=hb, json={"decision": "approve"}), 200,
        "idempotent repeated governance vote")["proposal"]
    assert repeated_bob_vote["already_processed"] is True
    assert repeated_bob_vote["approvals"] == 1
    promoted = check(client.post(
        f"/api/v1/projects/{p1}/governance-proposals/{promotion['id']}/votes",
        headers=hc, json={"decision": "approve"}), 200,
        "second distinct vote applies admin promotion")["proposal"]
    assert promoted["status"] == "applied" and promoted["approvals"] == 2
    promotion_retry = check(client.post(
        f"/api/v1/projects/{p1}/governance-proposals/{promotion['id']}/votes",
        headers=hc, json={"decision": "approve"}), 200,
        "idempotent retry after promotion applied")["proposal"]
    assert promotion_retry["already_processed"] is True
    assert promotion_retry["approvals"] == 2
    assert check(client.get(f"/api/v1/projects/{p1}/members", headers=hb),
                 200, "verify promoted role")["members"]
    assert next(row for row in check(client.get(
        f"/api/v1/projects/{p1}/members", headers=hb), 200,
        "read project members after promotion")["members"]
                if row["user_id"] == bob["id"])["role"] == "admin"

    # A threshold reduction requires the old threshold, with a hard floor of
    # two independent approvals even when the requested new value is one.
    threshold = check(client.post(f"/api/v1/projects/{p1}/governance-proposals",
                                  headers=ha, json={
                                      "action": "lower_approval_threshold",
                                      "required_approvals": 1,
                                      "request_id": "lower-threshold-" + uuid.uuid4().hex[:12]}),
                      200, "lower threshold proposal")["proposal"]
    assert threshold["required_approvals"] == 2
    check(client.post(f"/api/v1/projects/{p1}/governance-proposals/"
                      f"{threshold['id']}/votes", headers=hb,
                      json={"decision": "approve"}), 200,
          "first threshold approval")
    check(client.patch(f"/api/v1/projects/{p1}/members/{bob['id']}/role",
                       headers=ha, json={"role": "viewer"}), 200,
          "invalidate governance voter role")
    check(client.patch(f"/api/v1/projects/{p1}/members/{bob['id']}/role",
                       headers=ha, json={"role": "member"}), 200,
          "restore governance voter role")
    after_restore = check(client.get(
        f"/api/v1/projects/{p1}/governance-proposals/{threshold['id']}",
        headers=hb), 200, "inspect invalidated governance vote")["proposal"]
    assert after_restore["approvals"] == 0
    assert [vote["valid"] for vote in after_restore["votes"]] == [False]
    fresh_bob_vote = check(client.post(
        f"/api/v1/projects/{p1}/governance-proposals/{threshold['id']}/votes",
        headers=hb, json={"decision": "approve"}), 200,
        "fresh vote after role restoration")["proposal"]
    assert fresh_bob_vote["approvals"] == 1
    assert [vote["valid"] for vote in fresh_bob_vote["votes"]] == [False, True]
    threshold_applied = check(client.post(
        f"/api/v1/projects/{p1}/governance-proposals/{threshold['id']}/votes",
        headers=hc, json={"decision": "approve"}), 200,
        "second threshold approval")["proposal"]
    assert threshold_applied["status"] == "applied"
    summary = check(client.get(f"/api/v1/projects/{p1}", headers=ha),
                    200, "project policy summary")["project"]
    assert summary["required_approvals"] == 1
    assert summary["policy_version"] > threshold["policy_version"]
    task = check(client.post("/api/v1/worker/tasks", headers=ha, json={
        "project_id": p1, "title": "Create approved file",
        "target_device_id": bob_device["id"], "tool_call_id": "write-one",
        "tool": "workspace.write", "args": {"path": "out.txt", "content": "hello"},
        "ttl_seconds": 300}), 200, "worker task create")
    job = task["job"]
    submitted = task["call"]
    call_id = submitted["call_id"]
    assert submitted["required_approvals"] >= 2
    project_role = hub.project_access.member_role(p1, bob["id"])
    assert project_role
    assert project_role in {"admin", "member"}, project_role
    assert hub.project_access.member_role(p1, carol["id"]) in {"admin", "member"}
    listed = check(client.get(f"/api/v1/worker/calls?project_id={p1}", headers=hb),
                   200, "worker calls inbox")["calls"]
    assert any(row["call_id"] == call_id for row in listed)
    assert client.get(f"/api/v1/worker/calls?project_id={p1}",
                      headers=headers("carol@example.com", ct)).status_code == 200
    assert client.get(f"/api/v1/worker/calls?project_id={p2}", headers=ha).status_code == 403
    assert client.post("/api/v1/worker/poll", headers=hb,
                       json={"project_id": p1}).json()["call"] is None
    check(client.post(f"/api/v1/worker/calls/{call_id}/votes", headers=hb,
                      json={"decision": "approve"}), 200, "Bob worker vote")
    assert client.post("/api/v1/worker/poll", headers=hb,
                       json={"project_id": p1}).json()["call"] is None
    check(client.post(f"/api/v1/worker/calls/{call_id}/votes", headers=hc,
                      json={"decision": "approve"}), 200, "Carol worker vote")
    offer = check(client.post("/api/v1/worker/poll", headers=hb,
                              json={"project_id": p1}), 200, "worker poll")["call"]
    assert offer and offer["call_id"] == call_id
    preflight = check(client.post(f"/api/v1/worker/calls/{call_id}/preflight",
                                  headers=hb,
                                  json={"lease_token": offer["lease_token"]}),
                      200, "worker preflight")
    assert preflight["args"] == {"path": "out.txt", "content": "hello"}
    check(client.post(f"/api/v1/worker/calls/{call_id}/complete", headers=hb,
                      json={"lease_token": offer["lease_token"],
                            "args_sha256": offer["args_sha256"],
                            "result": {"created": True}}), 200, "worker complete")
    assert agent_jobs.get_job(job["id"])["status"] == "completed"
    assert client.post(f"/api/v1/worker/calls/{call_id}/preflight", headers=hb,
                       json={"lease_token": offer["lease_token"]}).status_code == 409

    rejected = check(client.post("/api/v1/worker/tasks", headers=ha, json={
        "project_id": p1, "target_device_id": bob_device["id"],
        "tool": "workspace.read", "args": {"path": "rejected.txt"},
        "ttl_seconds": 300}), 200, "rejectable Worker task")
    rejected_id = rejected["call"]["call_id"]
    check(client.post(f"/api/v1/worker/calls/{rejected_id}/votes", headers=hb,
                      json={"decision": "reject"}), 200, "reject Worker task")
    assert agent_jobs.get_job(rejected["job"]["id"])["status"] == "failed"

    expiring = check(client.post("/api/v1/worker/tasks", headers=ha, json={
        "project_id": p1, "target_device_id": bob_device["id"],
        "tool": "workspace.read", "args": {"path": "expired.txt"},
        "ttl_seconds": 1}), 200, "expiring Worker task")
    clock_before_expiry = worker_hub._now
    worker_hub._now = lambda: clock_before_expiry() + 2
    try:
        expired = check(client.get(
            f"/api/v1/worker/calls/{expiring['call']['call_id']}", headers=ha),
            200, "expired Worker call")
    finally:
        worker_hub._now = clock_before_expiry
    assert expired["status"] == "expired"
    assert agent_jobs.get_job(expiring["job"]["id"])["status"] == "failed"

    role_call = check(client.post("/api/v1/worker/tasks", headers=ha, json={
        "project_id": p1, "target_device_id": bob_device["id"],
        "tool": "workspace.write", "args": {"path": "role.txt", "content": "ok"},
        "ttl_seconds": 300}), 200, "role-sensitive Worker task")
    role_call_id = role_call["call"]["call_id"]
    for voter in (hb, hc):
        check(client.post(f"/api/v1/worker/calls/{role_call_id}/votes",
                          headers=voter, json={"decision": "approve"}),
              200, "role-sensitive Worker vote")
    check(client.patch(f"/api/v1/projects/{p1}/members/{carol['id']}/role",
                       headers=ha, json={"role": "viewer"}), 200, "demote reviewer")
    assert check(client.post("/api/v1/worker/poll", headers=hb,
                             json={"project_id": p1}), 200,
                 "poll after reviewer demotion")["call"] is None
    assert check(client.get(f"/api/v1/worker/calls/{role_call_id}", headers=ha),
                 200, "read call after demotion")["approvals"] == 1
    check(client.patch(f"/api/v1/projects/{p1}/members/{carol['id']}/role",
                       headers=ha, json={"role": "member"}), 200, "restore reviewer")
    assert check(client.post("/api/v1/worker/poll", headers=hb,
                             json={"project_id": p1}), 200,
                 "poll before restored reviewer votes again")["call"] is None
    assert check(client.get(f"/api/v1/worker/calls/{role_call_id}", headers=ha),
                 200, "read call after reviewer restoration")["approvals"] == 1
    check(client.post(f"/api/v1/worker/calls/{role_call_id}/votes", headers=hc,
                      json={"decision": "approve"}), 200, "fresh reviewer vote")
    resumed_offer = check(client.post("/api/v1/worker/poll", headers=hb,
                                      json={"project_id": p1}), 200,
                          "poll after reviewer restoration")["call"]
    assert resumed_offer and resumed_offer["call_id"] == role_call_id
    check(client.post(f"/api/v1/worker/calls/{role_call_id}/complete", headers=hb,
                      json={"lease_token": resumed_offer["lease_token"],
                            "args_sha256": resumed_offer["args_sha256"],
                            "result": {"text": "ok"}}), 200,
          "complete role-sensitive Worker call")

    # Removal revokes access and prevents reuse of still-issued invitations.
    pending = invite_project(ha, p1, bob, bob_device)
    accept_project(hb, invite_project(hc, p2, bob, bob_device))
    assert client.post(f"/api/v1/projects/{p2}/owner-transfer", headers=hc,
                       json={"user_id": bob["id"]}).status_code == 409
    two_person = check(client.post(
        f"/api/v1/projects/{p2}/governance-proposals", headers=hc, json={
            "action": "owner_transfer", "target_user_id": bob["id"],
            "request_id": "two-person-" + uuid.uuid4().hex[:12]}),
        200, "two person owner transfer proposal")["proposal"]
    assert two_person["required_approvals"] == 2
    one_vote = check(client.post(
        f"/api/v1/projects/{p2}/governance-proposals/{two_person['id']}/votes",
        headers=hb, json={"decision": "approve"}), 200,
        "only non-proposer votes in two person project")["proposal"]
    assert one_vote["status"] == "pending" and one_vote["approvals"] == 1
    assert check(client.get(f"/api/v1/projects/{p2}/owner-transfer/pending",
                            headers=hb), 200,
                 "no transfer while quorum is unmet")["transfer"] is None
    assert client.post(f"/api/v1/projects/{p2}/owner-transfer/accept",
                       headers=hb).status_code == 404
    accept_project(ha, invite_project(hc, p2, alice, alice_device))
    first_approved = check(client.post(
        f"/api/v1/projects/{p2}/governance-proposals/{two_person['id']}/votes",
        headers=ha, json={"decision": "approve"}), 200,
        "second owner transfer approval")["proposal"]
    assert first_approved["status"] == "approved"
    transfer_pending = check(client.get(
        f"/api/v1/projects/{p2}/owner-transfer/pending", headers=hb), 200,
        "approved transfer pending for target")["transfer"]
    assert transfer_pending["governance_proposal_id"] == two_person["id"]
    check(client.patch(f"/api/v1/projects/{p2}/members/{bob['id']}/role",
                       headers=hc, json={"role": "viewer"}), 200,
          "invalidate owner transfer approval")
    assert client.post(f"/api/v1/projects/{p2}/owner-transfer/accept",
                       headers=hb).status_code == 409
    invalid_transfer = check(client.get(
        f"/api/v1/projects/{p2}/governance-proposals/{two_person['id']}",
        headers=ha), 200, "owner transfer invalidated at acceptance")["proposal"]
    assert invalid_transfer["status"] == "stale"
    assert invalid_transfer["approvals"] == 1
    assert [vote["valid"] for vote in invalid_transfer["votes"]] == [False, True]
    assert check(client.get(f"/api/v1/projects/{p2}/owner-transfer/pending",
                            headers=hb), 200,
                 "stale owner transfer is no longer pending")["transfer"] is None
    check(client.patch(f"/api/v1/projects/{p2}/members/{bob['id']}/role",
                       headers=hc, json={"role": "member"}), 200,
          "restore owner transfer approver")
    restored_transfer = check(client.get(
        f"/api/v1/projects/{p2}/governance-proposals/{two_person['id']}",
        headers=hb), 200, "old owner transfer votes stay stale")["proposal"]
    assert restored_transfer["status"] == "stale"
    assert [vote["valid"] for vote in restored_transfer["votes"]] == [False, True]

    transfer_proposal = check(client.post(
        f"/api/v1/projects/{p2}/governance-proposals", headers=hc, json={
            "action": "owner_transfer", "target_user_id": bob["id"],
            "request_id": "retry-transfer-" + uuid.uuid4().hex[:12]}),
        200, "fresh owner transfer proposal after stale votes")["proposal"]
    check(client.post(f"/api/v1/projects/{p2}/governance-proposals/"
                      f"{transfer_proposal['id']}/votes", headers=hb,
                      json={"decision": "approve"}), 200,
          "fresh Bob owner transfer vote")
    approved_transfer = check(client.post(
        f"/api/v1/projects/{p2}/governance-proposals/{transfer_proposal['id']}/votes",
        headers=ha, json={"decision": "approve"}), 200,
        "fresh Alice owner transfer vote")["proposal"]
    assert approved_transfer["status"] == "approved"
    transferred = check(client.post(f"/api/v1/projects/{p2}/owner-transfer/accept",
                                    headers=hb), 200, "owner transfer accept")
    assert transferred["transfer"]["owner_id"] == bob["id"]
    accepted_proposal = check(client.get(
        f"/api/v1/projects/{p2}/governance-proposals/{transfer_proposal['id']}",
        headers=hb), 200, "read applied owner transfer proposal")["proposal"]
    assert accepted_proposal["status"] == "applied"
    assert accepted_proposal["approvals"] == 2
    owner_vote_retry = check(client.post(
        f"/api/v1/projects/{p2}/governance-proposals/{transfer_proposal['id']}/votes",
        headers=ha, json={"decision": "approve"}), 200,
        "idempotent owner transfer vote retry")["proposal"]
    assert owner_vote_retry["already_processed"] is True
    assert owner_vote_retry["approvals"] == 2
    expiring_proposal = check(client.post(
        f"/api/v1/projects/{p2}/governance-proposals", headers=hb, json={
            "action": "promote_admin", "target_user_id": alice["id"],
            "request_id": "expiry-" + uuid.uuid4().hex[:12]}),
        200, "expiring governance proposal")["proposal"]
    clock_before_governance_expiry = hub.project_access._now
    hub.project_access._now = lambda: clock_before_governance_expiry() + 25 * 60 * 60
    try:
        expired_proposal = check(client.get(
            f"/api/v1/projects/{p2}/governance-proposals/{expiring_proposal['id']}",
            headers=ha), 200, "governance proposal expiry")["proposal"]
    finally:
        hub.project_access._now = clock_before_governance_expiry
    assert expired_proposal["status"] == "expired"
    assert client.delete(f"/api/v1/projects/{p2}/members/{bob['id']}",
                         headers=hb).status_code == 409
    check(client.delete(f"/api/v1/projects/{p1}/members/{bob['id']}", headers=ha),
          200, "remove Bob")
    assert client.get(f"/api/v1/projects/{p1}", headers=hb).status_code == 404
    assert client.post(f"/api/v1/project-invites/{pending['invite']['id']}/accept",
                       headers=hb, json={"invite_code": pending["invite_code"]}).status_code == 410
    assert client.get(f"/api/v1/projects/{p2}", headers=hb).status_code == 200

    # Archive is owner-only, retains authorized read access, and performs
    # repairable cancellation of pending Worker work across the two stores.
    assert client.post(f"/api/v1/projects/{p1}/archive", headers=hc).status_code == 403
    ok, _ = hub._projects.add_milestone(p1, "Retained milestone")
    assert ok
    ok, _ = hub._projects.save_checkpoint(p1, "Retained checkpoint")
    assert ok
    archive_invite = invite_project(ha, p1, bob, bob_device)
    pending_governance = check(client.post(
        f"/api/v1/projects/{p1}/governance-proposals", headers=ha, json={
            "action": "promote_admin", "target_user_id": carol["id"],
            "request_id": "archive-governance-" + uuid.uuid4().hex[:12]}),
        200, "pending proposal before archive")["proposal"]
    check(client.post(f"/api/v1/projects/{p1}/governance-proposals/"
                      f"{pending_governance['id']}/votes", headers=hc,
                      json={"decision": "approve"}), 200,
          "first vote before archive")
    pending_worker = check(client.post("/api/v1/worker/tasks", headers=ha, json={
        "project_id": p1, "target_device_id": carol_device["id"],
        "tool": "workspace.write", "args": {"path": "archived.txt", "content": "x"},
        "ttl_seconds": 300}), 200, "pending Worker job before archive")
    original_reconcile = worker_hub._reconcile_worker_task
    worker_hub._reconcile_worker_task = lambda _call_id: (_ for _ in ()).throw(
        RuntimeError("injected cross-store interruption"))
    try:
        interrupted = client.post(f"/api/v1/projects/{p1}/archive", headers=ha)
    finally:
        worker_hub._reconcile_worker_task = original_reconcile
    assert interrupted.status_code == 503
    with worker_hub._connection() as conn:
        call_state = worker_hub._get_row(conn, pending_worker["call"]["call_id"])
        assert call_state["status"] == "revoked"
        assert agent_jobs.get_job(pending_worker["job"]["id"])["status"] == "queued"

    # Simulate stopping after the Worker Job terminal status is written but
    # before its terminal event is stored in the separate Job file.
    append_event_once = agent_jobs.append_event_once
    agent_jobs.append_event_once = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("injected Job-event interruption"))
    try:
        interrupted_after_status = client.post(
            f"/api/v1/projects/{p1}/archive", headers=ha)
    finally:
        agent_jobs.append_event_once = append_event_once
    assert interrupted_after_status.status_code == 503
    partially_reconciled = agent_jobs.get_job(pending_worker["job"]["id"])
    assert partially_reconciled["status"] == "failed"
    assert not any(event.get("kind") == "worker_call_revoked"
                   for event in partially_reconciled.get("events") or [])

    archived = check(client.post(f"/api/v1/projects/{p1}/archive", headers=ha),
                     200, "archive retry")
    assert archived["already_archived"] is True
    assert archived["worker_calls_revoked"] == 0
    repaired_job = agent_jobs.get_job(pending_worker["job"]["id"])
    assert repaired_job["status"] == "failed"
    assert sum(event.get("kind") == "worker_call_revoked"
               for event in repaired_job.get("events") or []) == 1
    check(client.post(f"/api/v1/projects/{p1}/archive", headers=ha),
          200, "idempotent repeated archive")
    detail = check(client.get(f"/api/v1/projects/{p1}", headers=ha),
                   200, "read archived project")
    assert detail["project"]["status"] == "archived"
    assert any(row.get("title") == "Retained milestone"
               for row in detail.get("milestones") or [])
    assert any(row.get("summary") == "Retained checkpoint"
               for row in detail.get("checkpoints") or [])
    listed = check(client.get("/api/v1/projects", headers=ha),
                   200, "list archived project")["projects"]
    assert any(row["id"] == p1 and row["status"] == "archived" for row in listed)
    members = check(client.get(f"/api/v1/projects/{p1}/members", headers=hc),
                    200, "read archived members")["members"]
    assert any(row.get("user_id") == carol["id"] for row in members)
    assert client.delete(f"/api/v1/projects/{p1}/members/{carol['id']}",
                         headers=ha).status_code == 409
    assert client.patch(f"/api/v1/projects/{p1}/members/{carol['id']}/role",
                        headers=ha, json={"role": "viewer"}).status_code == 409
    assert client.put(f"/api/v1/projects/{p1}/policy", headers=ha,
                      json={"required_approvals": 1}).status_code == 409
    check(client.get(f"/api/v1/projects/{p1}/versions", headers=hc),
          200, "read archived version status")
    assert check(client.get(f"/api/v1/projects/{p1}/changes", headers=hc),
                 200, "read archived changes")["changes"] == []
    audit = check(client.get(f"/api/v1/projects/{p1}/audit", headers=ha),
                  200, "read archive audit")
    assert any(row["action"] == "project.archive" for row in audit["entries"])
    assert sum(row["action"] == "project.archive" for row in audit["entries"]) == 1
    archive_invites = check(client.get(f"/api/v1/projects/{p1}/invites", headers=ha),
                            200, "read archived invite history")["invites"]
    assert any(row.get("id") == archive_invite["invite"]["id"]
               and row.get("status") == "revoked" for row in archive_invites)
    assert client.post(f"/api/v1/projects/{p1}/archive", headers=hc).status_code == 403
    cancelled_governance = check(client.get(
        f"/api/v1/projects/{p1}/governance-proposals/{pending_governance['id']}",
        headers=ha), 200, "archived proposal history")["proposal"]
    assert cancelled_governance["status"] == "cancelled"
    assert client.post(f"/api/v1/projects/{p1}/governance-proposals", headers=ha,
                       json={"action": "promote_admin", "target_user_id": carol["id"],
                             "request_id": "archived-new-proposal"}).status_code == 409
    assert client.post(f"/api/v1/projects/{p1}/governance-proposals/"
                       f"{pending_governance['id']}/votes", headers=hc,
                       json={"decision": "approve"}).status_code == 409
    assert client.get(f"/api/v1/jobs/{pending_worker['job']['id']}",
                      headers=ha).status_code == 200
    team_jobs = check(client.get("/api/v1/jobs?scope=team", headers=hc),
                      200, "read archived Job history")["jobs"]
    assert any(row.get("id") == pending_worker["job"]["id"]
               and row.get("status") == "failed" for row in team_jobs)
    assert client.get(f"/api/v1/worker/calls?project_id={p1}",
                      headers=hc).status_code == 403
    assert client.get(f"/api/v1/worker/calls/{pending_worker['call']['call_id']}",
                      headers=hc).status_code == 403
    assert client.post("/api/v1/jobs", headers=ha, json={
        "project_id": p1, "visibility": "team",
        "messages": [{"role": "user", "content": "new job"}]}).status_code == 409
    assert client.post(f"/api/v1/projects/{p1}/invites", headers=ha, json={
        "terminal_display_code": bob_device["display_code"],
        "user_id": bob["id"], "role": "member"}).status_code == 409
    assert client.post(f"/api/v1/project-invites/{archive_invite['invite']['id']}/accept",
                       headers=hb,
                       json={"invite_code": archive_invite["invite_code"]}).status_code == 410
    assert client.post(f"/api/v1/projects/{p1}/versions/init", headers=ha,
                       json={"shared_paths": []}).status_code == 409
    assert client.post("/api/v1/worker/tasks", headers=ha, json={
        "project_id": p1, "target_device_id": carol_device["id"],
        "tool": "workspace.read", "args": {"path": "archived.txt"},
        "ttl_seconds": 300}).status_code == 409
    assert client.post(f"/api/v1/worker/calls/{pending_worker['call']['call_id']}/votes",
                       headers=hc, json={"decision": "approve"}).status_code == 409
    assert client.post("/api/v1/worker/poll", headers=hc,
                       json={"project_id": p1}).status_code == 409
    for result in (
        hub._projects.add_milestone(p1, "Forbidden after archive"),
        hub._projects.update_milestone(p1, 1, notes="Forbidden after archive"),
        hub._projects.save_checkpoint(p1, "Forbidden after archive"),
        hub._projects.link_todo(p1, 1),
    ):
        assert not result[0]
    ok, _ = hub._projects.update_project(p1, status="active")
    assert not ok and hub._projects.get_project(p1)["meta"]["status"] == "archived"
    print("PASS multi-project claim, membership, isolation, Serve boundary and Worker contract")


if __name__ == "__main__":
    try:
        run()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
