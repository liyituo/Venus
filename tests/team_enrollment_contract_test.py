"""Contract checks for the one-Hub team enrollment lifecycle.

All Hub state and repositories use a temporary data directory. The HTTP client
uses a loopback peer and a fake, explicitly configured Serve hostname; no real
Tailscale account or user's Venus data is touched.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="venus_team_enrollment_"))
os.environ["VENUS_DATA_DIR"] = str(TMP / "hub-data")
os.environ["PCAGENT_DISABLE_MCP"] = "1"
os.environ["PCAGENT_ALLOW_TEST_HOST"] = "1"
sys.path.insert(0, str(ROOT / "src"))

import llm_server as L  # noqa: E402
import agent_jobs as J  # noqa: E402
import project_store as P  # noqa: E402
import team_collab as T  # noqa: E402
import team_enrollment as E  # noqa: E402
import team_versions as V  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

passed = failed = 0
HOST = "hub.example.ts.net"
LOCAL = "127.0.0.1:8001"


def check(name: str, condition: bool, extra: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name} {extra}")


def headers(login: str = "", token: str = "") -> dict[str, str]:
    result = {"Host": HOST}
    if login:
        result["Tailscale-User-Login"] = login
    if token:
        result["X-Team-Device-Token"] = token
    return result


def run() -> tuple[int, int]:
    global passed, failed
    client = TestClient(L.app, client=("127.0.0.1", 52100))
    L.ISOLATED = True
    L.TEAM_SERVE_MODE = True
    L.TEAM_SERVE_HOST = HOST
    L.AUTH_TOKEN = ""
    legacy_token = "legacy-user-token-do-not-trust"
    T.save_users([{"id": "u_legacy", "name": "Legacy", "role": "admin",
                   "token_hash": T.hash_token(legacy_token)}])
    legacy_project_ok, legacy_project_result = P.create_project(
        "Preserved private project", "", [], is_team=False,
        owner_id="u_legacy", owner_name="Legacy")
    legacy_project = (legacy_project_result.get("created") or {}) if legacy_project_ok else {}
    legacy_job = J.create_job(
        messages=[{"role": "user", "content": "preserve private job"}],
        title="Preserved private job", owner="u_legacy", owner_name="Legacy",
        project_id=str(legacy_project.get("id") or ""), visibility="private",
        request_id="legacy-job-before-migration")
    T.audit("u_legacy", "legacy.history", {"marker": "preserve-before-enrollment"})
    T.save_collab_config({"team_mode": True, "default_required": 1,
                          "destructive_required": 2, "tool_required": {}})
    E.ensure_bootstrap_credential()
    bootstrap_token = E.bootstrap_credential_local()
    check("startup credential is stored outside plain config", bool(bootstrap_token))

    uninitialized_public = client.get("/api/v1/team/public", headers={"Host": HOST})
    check("uninitialized Hub exposes only its uninitialized state",
          uninitialized_public.status_code == 200
          and uninitialized_public.json().get("initialized") is False
          and not uninitialized_public.json().get("team_id")
          and uninitialized_public.json().get("serve_host") == HOST)
    malformed_secret = "invite-secret-must-not-be-echoed-" * 12
    invalid_request = client.post("/api/v1/team/join-requests",
                                  headers=headers("alice@example.com"), json={
                                      "invite_code": malformed_secret,
                                      "display_name": "Alice", "device_name": "Hub",
                                      "claim_secret": "x" * 40, "request_id": "v" * 32,
                                  })
    check("request validation errors never echo an invitation code",
          invalid_request.status_code == 422 and malformed_secret not in invalid_request.text,
          invalid_request.text)
    wrong_host = client.get("/api/v1/team/public", headers={
        "Host": "hub.example.ts.net.evil", "Tailscale-User-Login": "alice@example.com"})
    remote_bootstrap = TestClient(L.app, client=("192.168.1.22", 52102)).post(
        "/api/v1/team/bootstrap",
        headers={"Host": LOCAL, "X-Team-Bootstrap-Token": bootstrap_token},
        json={"team_name": "Wrong", "admin_login": "alice@example.com",
              "admin_name": "Alice", "device_name": "Hub"})
    check("Serve identity requires the exact configured Host and loopback bootstrap",
          wrong_host.status_code == 403 and remote_bootstrap.status_code == 403
          and bool(E.bootstrap_credential_local()))
    init = client.post("/api/v1/team/bootstrap", headers={
        "Host": LOCAL, "X-Team-Bootstrap-Token": bootstrap_token,
    }, json={"team_name": "Contract Team", "admin_login": "alice@example.com",
             "admin_name": "Alice", "device_name": "Alice Hub"})
    admin_result = init.json() if init.status_code == 200 else {}
    admin = admin_result.get("user") or {}
    admin_token = str(admin_result.get("device_token") or "")
    team = admin_result.get("team") or {}
    check("one-time local bootstrap initializes team and first admin",
          init.status_code == 200 and team.get("team_id", "").startswith("team_")
          and admin.get("role") == "admin" and bool(admin_token), init.text)
    check("bootstrap secret is consumed and legacy users become untrusted",
          not E.bootstrap_credential_local()
          and next(u for u in T.load_users() if u["id"] == "u_legacy")["status"]
          == "legacy_untrusted")
    check("migration preserves existing projects, jobs, and audit history",
          P.get_project(str(legacy_project.get("id") or "")) is not None
          and J.get_job(legacy_job["id"]) is not None
          and any(row.get("action") == "legacy.history"
                  for row in T.read_audit(limit=200)))
    check("legacy direct member token no longer authenticates",
          T.match_token(legacy_token) is None)

    public = client.get("/api/v1/team/public", headers=headers("alice@example.com"))
    check("Serve Host and trusted loopback peer disclose team identity",
          public.status_code == 200 and public.json().get("team_id") == team.get("team_id")
          and public.json().get("tailscale_login") == "alice@example.com", public.text)
    check("new team endpoints reject old generic token", client.get(
        "/api/v1/team/me", headers={**headers("alice@example.com"),
                                    "X-Api-Token": admin_token}).status_code == 401)
    check("device token requires the bound Tailscale identity", client.get(
        "/api/v1/team/me", headers=headers("mallory@example.com", admin_token)
    ).status_code == 401)
    check("missing Tailscale identity cannot join", client.post(
        "/api/v1/team/join-requests", headers={"Host": HOST}, json={
            "invite_code": "not-a-real-code", "display_name": "Mallory",
            "device_name": "Laptop", "claim_secret": "x" * 40,
            "request_id": "r" * 32,
        }).status_code == 401)
    direct = TestClient(L.app, client=("192.168.1.22", 52101))
    check("direct LAN request cannot spoof the Serve identity header",
          direct.get("/api/v1/team/public", headers=headers("alice@example.com")).status_code == 403)

    admin_headers = headers("alice@example.com", admin_token)
    me = client.get("/api/v1/team/me", headers=admin_headers)
    check("first admin device authenticates and resolves server-side role",
          me.status_code == 200 and me.json()["user"]["role"] == "admin", me.text)
    invite_response = client.post("/api/v1/team/invites", headers=admin_headers,
                                  json={"expected_login": "bob@example.com"})
    invite_payload = invite_response.json() if invite_response.status_code == 200 else {}
    invite = invite_payload.get("invite") or {}
    invite_code = str(invite_payload.get("invite_code") or "")
    check("admin creates a one-use 24-hour invitation with 256-bit secret",
          invite_response.status_code == 200 and len(invite_code) >= 43
          and invite.get("max_uses") == 1
          and 23 * 3600 < invite.get("expires_at", 0) - invite.get("created_at", 0) < 25 * 3600,
          invite_response.text)
    stored_access = (TMP / "hub-data" / "team_access.json").read_text(encoding="utf-8")
    check("invitation plaintext is absent from Hub storage", invite_code not in stored_access)

    claim_secret = "b" * 48
    installation_code = "VI-01234-56789-ABCDE-FGHJK-MNPQRS"
    req_body = {"invite_code": invite_code, "display_name": "Bob",
                "device_name": "Bob Laptop", "claim_secret": claim_secret,
                "request_id": "b" * 32, "installation_code": installation_code}
    wrong_login = client.post("/api/v1/team/join-requests",
                              headers=headers("mallory@example.com"), json=req_body)
    wrong_code = client.post("/api/v1/team/join-requests",
                             headers=headers("bob@example.com"), json={**req_body,
                                                                         "invite_code": "wrong"})
    check("mismatched Tailscale identity and wrong code are rejected",
          wrong_login.status_code == 403 and wrong_code.status_code == 404,
          f"{wrong_login.status_code}/{wrong_code.status_code}")
    bad_installation_code = client.post(
        "/api/v1/team/join-requests", headers=headers("bob@example.com"),
        json={**req_body, "installation_code": "VI-TOO-SHORT"})
    check("installation code must use the exact grouped Crockford format",
          bad_installation_code.status_code == 422, bad_installation_code.text)
    apply = client.post("/api/v1/team/join-requests",
                        headers=headers("bob@example.com"), json=req_body)
    app = (apply.json().get("application") or {}) if apply.status_code == 200 else {}
    check("matching invite submits pending application with stable retry ID",
          apply.status_code == 200 and app.get("id") == "app_" + "b" * 32
          and app.get("status") == "pending"
          and app.get("installation_code") == installation_code
          and app.get("installation_code_binding") == "bound", apply.text)
    repeated = client.post("/api/v1/team/join-requests",
                           headers=headers("bob@example.com"), json=req_body)
    check("application retry is idempotent", repeated.status_code == 200
          and repeated.json().get("already_processed"))
    conflicting_invite = client.post("/api/v1/team/invites", headers=admin_headers,
                                     json={"expected_login": "bob@example.com"}).json()
    conflicting_application = client.post(
        "/api/v1/team/join-requests", headers=headers("bob@example.com"), json={
            "invite_code": conflicting_invite.get("invite_code"),
            "display_name": "Bob", "device_name": "Bob spare laptop",
            "claim_secret": "x" * 48, "request_id": "bob_duplicate_install_0001",
            "installation_code": installation_code,
        })
    check("same Hub rejects a second live application with the same installation code",
          conflicting_application.status_code == 409
          and (conflicting_invite.get("invite") or {}).get("uses") == 0,
          conflicting_application.text)
    reused = client.post("/api/v1/team/join-requests", headers=headers("bob@example.com"),
                         json={**req_body, "request_id": "c" * 32,
                               "device_name": "Bob Other Device"})
    check("consumed invite cannot be reused", reused.status_code == 410)
    check("pending applicant cannot read team jobs or members",
          client.get("/api/v1/jobs", headers=headers("bob@example.com")).status_code == 401
          and client.get("/api/v1/team/members", headers=headers("bob@example.com")).status_code == 401)
    apps_response = client.get("/api/v1/team/applications", headers=admin_headers)
    listed_app = (apps_response.json().get("applications") or [{}])[0]
    check("only an admin can list applications and sees requested identity",
          apps_response.status_code == 200 and listed_app.get("actual_login") == "bob@example.com"
          and listed_app.get("installation_code") == installation_code)
    check("pending applicant cannot read full audit", client.get(
        "/api/v1/audit", headers=headers("bob@example.com")
    ).status_code == 401)

    approved = client.post(f"/api/v1/team/applications/{app['id']}/review",
                           headers=admin_headers,
                           json={"decision": "approve", "note": "verified in tailnet"})
    approved_row = (approved.json().get("application") or {}) if approved.status_code == 200 else {}
    check("admin approves without receiving applicant token",
          approved.status_code == 200 and "device_token" not in approved.text
          and approved_row.get("reviewed_by") == admin.get("id"))
    approved_again = client.post(f"/api/v1/team/applications/{app['id']}/review",
                                 headers=admin_headers,
                                 json={"decision": "approve"})
    check("repeated approval is idempotent and does not mint a second device",
          approved_again.status_code == 200
          and approved_again.json().get("already_processed") is True)
    wrong_claim = client.post(f"/api/v1/team/join-requests/{app['id']}/claim",
                              headers=headers("bob@example.com"),
                              json={"claim_secret": "z" * 48,
                                    "installation_code": installation_code})
    mismatched_installation_claim = client.post(
        f"/api/v1/team/join-requests/{app['id']}/claim",
        headers=headers("bob@example.com"), json={
            "claim_secret": claim_secret,
            "installation_code": "VI-11234-56789-ABCDE-FGHJK-MNPQRS"})
    missing_installation_claim = client.post(
        f"/api/v1/team/join-requests/{app['id']}/claim",
        headers=headers("bob@example.com"), json={"claim_secret": claim_secret})
    claim = client.post(f"/api/v1/team/join-requests/{app['id']}/claim",
                        headers=headers("bob@example.com"),
                        json={"claim_secret": claim_secret,
                              "installation_code": installation_code})
    member_token = (claim.json().get("device_token") or "") if claim.status_code == 200 else ""
    device_id = claim.json().get("device_id") if claim.status_code == 200 else ""
    check("claim requires temporary key and original installation-code binding",
          wrong_claim.status_code == 401
          and mismatched_installation_claim.status_code == 409
          and missing_installation_claim.status_code == 409,
          f"{wrong_claim.status_code}/{mismatched_installation_claim.status_code}/"
          f"{missing_installation_claim.status_code}")
    check("approved applicant claims one independent device token",
          claim.status_code == 200 and len(member_token) >= 64 and bool(device_id), claim.text)
    repeated_claim = client.post(f"/api/v1/team/join-requests/{app['id']}/claim",
                                 headers=headers("bob@example.com"),
                                 json={"claim_secret": claim_secret})
    check("device token claim can be redeemed only once", repeated_claim.status_code == 410)
    member_headers = headers("bob@example.com", member_token)
    b_me = client.get("/api/v1/team/me", headers=member_headers)
    check("claimed device validates and sees approved member identity",
          b_me.status_code == 200 and b_me.json()["user"]["name"] == "Bob", b_me.text)
    check("installation code alone does not authenticate or grant Hub access",
          client.get("/api/v1/team/me", headers={
              "Host": HOST, "Tailscale-User-Login": "bob@example.com",
              "X-Installation-Code": installation_code,
          }).status_code == 401)
    member_roster = client.get("/api/v1/team", headers=member_headers)
    admin_roster = client.get("/api/v1/team", headers=admin_headers)
    member_roster_rows = member_roster.json().get("users") or []
    admin_roster_rows = admin_roster.json().get("users") or []
    check("member roster omits login/device inventory while admin can inspect it",
          member_roster.status_code == 200
          and all("tailscale_login" not in row and "devices" not in row
                  for row in member_roster_rows)
          and any(row.get("tailscale_login") == "bob@example.com" and row.get("devices")
                  for row in admin_roster_rows))
    check("approved member sees team, but not private resources by identity alone",
          member_roster.status_code == 200
          and client.get("/api/v1/team/applications", headers=member_headers).status_code == 403
          and legacy_job["id"] not in [row.get("id") for row in
                                       client.get("/api/v1/jobs?scope=all",
                                                  headers=member_headers).json().get("jobs", [])]
          and str(legacy_project.get("id") or "") not in [row.get("id") for row in
                                                           client.get("/api/v1/projects",
                                                                      headers=member_headers).json().get("projects", [])])
    member_audit_active = client.get("/api/v1/audit", headers=member_headers)
    check("active member audit view excludes other members' events",
          member_audit_active.status_code == 200
          and not any(row.get("action") == "team.invite.create"
                      for row in member_audit_active.json().get("entries", [])))

    code2, invite2 = (lambda response: (response.status_code, response.json()))(
        client.post("/api/v1/team/invites", headers=admin_headers,
                    json={"expected_login": "bob@example.com"}))
    code2, app2_resp = (lambda response: (response.status_code, response.json()))(
        client.post("/api/v1/team/join-requests", headers=headers("bob@example.com"), json={
            "invite_code": invite2.get("invite_code"), "display_name": "Bob",
            "device_name": "Bob Tablet", "claim_secret": "d" * 48,
            "request_id": "d" * 32,
        }))
    app2 = app2_resp.get("application") or {}
    client.post(f"/api/v1/team/applications/{app2.get('id')}/review", headers=admin_headers,
                json={"decision": "approve"})
    claim2 = client.post(f"/api/v1/team/join-requests/{app2.get('id')}/claim",
                         headers=headers("bob@example.com"),
                         json={"claim_secret": "d" * 48})
    token2 = claim2.json().get("device_token") if claim2.status_code == 200 else ""
    member_id = (claim2.json().get("user_id") if claim2.status_code == 200 else "")
    devices = client.get("/api/v1/team/me/devices", headers=headers("bob@example.com", token2))
    check("legacy clients without installation_code can claim another device",
          code2 == 200 and claim2.status_code == 200 and member_id == b_me.json()["user"]["id"]
          and app2.get("installation_code_binding") == "legacy"
          and len(devices.json().get("devices") or []) == 2)

    # Exercise the existing shared Git review lifecycle with trust-managed IDs.
    workspace = TMP / "initial-shared"
    (workspace / "shared").mkdir(parents=True)
    (workspace / "shared" / "README.md").write_text("initial\n", encoding="utf-8")
    ok, project_result = P.create_project(
        "Trust review", "", [], is_team=True,
        owner_id=admin["id"], owner_name="Alice")
    project_id = (project_result.get("created") or {}).get("id") if ok else ""
    V.initialize_project(project_id, workspace, ["shared/README.md"], admin)
    task = V.create_task_workspace(project_id, "job_123456abcdef", admin,
                                   required_approvals=1,
                                   eligible_reviewer_ids={admin["id"], member_id})
    task_path = Path(task["workspace"])
    (task_path / "shared" / "README.md").write_text("reviewed change\n", encoding="utf-8")
    change = V.commit_change(project_id, task["change"]["id"], admin,
                             paths=["shared/README.md"], purpose="Trust test",
                             request_id="trust-review")
    reviewed = V.review_change(project_id, change["id"],
                               {"id": member_id, "name": "Bob"},
                               decision="approve", reviewed_sha=change["current_sha"],
                               active_reviewer_ids=E.active_user_ids())
    check("active second member approval satisfies captured review policy",
          reviewed.get("status") == "approved" and reviewed.get("approval_count") == 1)

    revoked = client.post(f"/api/v1/team/devices/{device_id}/revoke", headers=admin_headers)
    check("single device revocation immediately returns 401 for that token",
          revoked.status_code == 200 and client.get(
              "/api/v1/team/me", headers=member_headers).status_code == 401)
    check("other approved device remains active", client.get(
        "/api/v1/team/me", headers=headers("bob@example.com", token2)).status_code == 200)

    reissue_invite = client.post("/api/v1/team/invites", headers=admin_headers,
                                 json={"expected_login": "bob@example.com"}).json()
    reissue = client.post("/api/v1/team/join-requests",
                          headers=headers("bob@example.com"), json={
                              "invite_code": reissue_invite.get("invite_code"),
                              "display_name": "Bob", "device_name": "Bob Laptop",
                              "claim_secret": "k" * 48,
                              "request_id": "bob_reissue_same_name_0001",
                          })
    reissue_app = (reissue.json().get("application") or {}) if reissue.status_code == 200 else {}
    reissue_approval = client.post(
        f"/api/v1/team/applications/{reissue_app.get('id')}/review",
        headers=admin_headers, json={"decision": "approve"})
    reissue_claim = client.post(
        f"/api/v1/team/join-requests/{reissue_app.get('id')}/claim",
        headers=headers("bob@example.com"), json={"claim_secret": "k" * 48})
    reissue_token = (reissue_claim.json().get("device_token")
                     if reissue_claim.status_code == 200 else "")
    check("revoked device name can be re-enrolled without duplicating its member",
          reissue.status_code == 200 and reissue_approval.status_code == 200
          and reissue_claim.status_code == 200
          and reissue_claim.json().get("user_id") == b_me.json()["user"]["id"])

    deactivate = client.post(f"/api/v1/team/members/{member_id}/deactivate",
                             headers=admin_headers)
    check("member deactivation revokes every device and removes active approval votes",
          deactivate.status_code == 200
          and client.get("/api/v1/team/me", headers=headers("bob@example.com", token2)).status_code == 401
          and client.get("/api/v1/team/me", headers=headers("bob@example.com", reissue_token)).status_code == 401
          and not T.team_mode_active())
    inactive_change = V.reconcile_active_reviews(project_id, change["id"], E.active_user_ids())
    check("inactive member approval is retained but invalidated",
          inactive_change.get("status") == "pending_review"
          and inactive_change.get("approval_count") == 0
          and any(r.get("invalidated_reason") == "reviewer_inactive"
                  for r in inactive_change.get("reviews", [])))
    try:
        V.merge_change(project_id, change["id"], admin,
                       active_reviewer_ids=E.active_user_ids())
        merge_blocked = False
    except V.VersionError as exc:
        merge_blocked = exc.status_code == 403 and "活跃审阅成员" in exc.detail
    check("merge fails closed when active reviewers cannot meet captured threshold", merge_blocked)

    last_admin = client.post(f"/api/v1/team/members/{admin['id']}/deactivate",
                             headers=admin_headers)
    last_device = client.post(f"/api/v1/team/devices/{admin_result['device_id']}/revoke",
                              headers=admin_headers)
    check("last admin and their final active device are protected",
          last_admin.status_code == 409 and last_device.status_code == 409)

    invite3 = client.post("/api/v1/team/invites", headers=admin_headers,
                          json={"expected_login": "carol@example.com"}).json()
    revoked_invite_id = invite3.get("invite", {}).get("id")
    rejected_app = E.submit_application(
        code=invite3["invite_code"], tailscale_login="carol@example.com",
        display_name="Carol", device_name="Carol PC", claim_secret="j" * 48,
        request_id="carol_rejected_0001")["application"]
    reject_once = client.post(
        f"/api/v1/team/applications/{rejected_app['id']}/review",
        headers=admin_headers, json={"decision": "reject", "note": "identity not confirmed"})
    reject_twice = client.post(
        f"/api/v1/team/applications/{rejected_app['id']}/review",
        headers=admin_headers, json={"decision": "reject"})
    check("rejection is audited with a real admin identity and is idempotent",
          reject_once.status_code == 200 and
          (reject_once.json().get("application") or {}).get("reviewed_by") == admin["id"]
          and reject_twice.status_code == 200
          and reject_twice.json().get("already_processed") is True)
    revoke_invite = client.delete(f"/api/v1/team/invites/{revoked_invite_id}",
                                  headers=admin_headers)
    revoked_join = client.post("/api/v1/team/join-requests", headers=headers("carol@example.com"),
                               json={"invite_code": invite3.get("invite_code"),
                                     "display_name": "Carol", "device_name": "Carol PC",
                                     "claim_secret": "e" * 48, "request_id": "e" * 32})
    check("revoked invite cannot create an application",
          revoke_invite.status_code == 200 and revoked_join.status_code == 410)
    expired = client.post("/api/v1/team/invites", headers=admin_headers,
                          json={"expected_login": "dave@example.com"}).json()
    access_path = TMP / "hub-data" / "team_access.json"
    access_data = json.loads(access_path.read_text(encoding="utf-8"))
    for row in access_data["invites"]:
        if row["id"] == expired.get("invite", {}).get("id"):
            row["expires_at"] = 1
    access_path.write_text(json.dumps(access_data), encoding="utf-8")
    expired_join = client.post("/api/v1/team/join-requests", headers=headers("dave@example.com"),
                               json={"invite_code": expired.get("invite_code"),
                                     "display_name": "Dave", "device_name": "Dave PC",
                                     "claim_secret": "f" * 48, "request_id": "f" * 32})
    check("expired invite cannot create an application", expired_join.status_code == 410)

    spam_codes = []
    for idx in range(9):
        response = client.post("/api/v1/team/join-requests",
                               headers=headers("spam@example.com"), json={
                                   "invite_code": "unknown", "display_name": "Spam",
                                   "device_name": "Spam PC", "claim_secret": "g" * 48,
                                   "request_id": f"spam_request_{idx:03d}",
                               })
        spam_codes.append(response.status_code)
    check("join endpoint rate limits repeated requests", spam_codes.count(404) == 8
          and spam_codes[-1] == 429, spam_codes)

    # Confirm the one-Hub team persists only hashed credentials/secrets.
    persisted = "".join(path.read_text(encoding="utf-8") for path in
                        (TMP / "hub-data").glob("*.json") if path.is_file())
    audit_text = (TMP / "hub-data" / "audit.jsonl").read_text(encoding="utf-8")
    check("member, device, and invitation secrets persist only as hashes",
          invite_code not in persisted and admin_token not in persisted
          and member_token not in persisted and token2 not in persisted
          and invite_code not in audit_text)
    full_audit = client.get("/api/v1/audit", headers=admin_headers).json().get("entries") or []
    member_audit = client.get("/api/v1/audit", headers=headers("bob@example.com", member_token))
    check("administrator reads full audit and member cannot read another member's audit",
          any(row.get("action") == "team.invite.create" for row in full_audit)
          and member_audit.status_code == 401)
    legacy_api = client.post("/api/v1/team/users", headers=admin_headers,
                             json={"name": "Bypass", "role": "member"})
    check("legacy direct user creation is disabled after enrollment bootstrap",
          legacy_api.status_code == 410)

    preview_statuses = [client.post("/api/v1/team/join/preview",
                                    headers=headers("preview@example.com"),
                                    json={"invite_code": "unknown"}).status_code
                        for _ in range(9)]
    check("invite preview shares the per-identity rate limit",
          preview_statuses.count(404) == 8 and preview_statuses[-1] == 429,
          preview_statuses)

    # Reinviting a deactivated former admin using the default member role must
    # not restore old privileges. Also ensure a previously approved but
    # unclaimed device cannot be activated after its member is deactivated.
    users = T.load_users()
    users.append({"id": "u_old_admin", "name": "Old Admin", "role": "admin",
                  "status": "inactive", "tailscale_login": "oldadmin@example.com",
                  "enrollment_managed": True, "devices": []})
    T.save_users(users)
    restore = E.create_invite(actor=admin, expected_login="oldadmin@example.com")
    restore_app = E.submit_application(
        code=restore["invite_code"], tailscale_login="oldadmin@example.com",
        display_name="Old Admin", device_name="Replacement laptop",
        claim_secret="h" * 48, request_id="oldadmin_restore_0001")
    E.review_application(restore_app["application"]["id"], actor=admin,
                         decision="approve")
    restored_user = next(u for u in T.load_users() if u["id"] == "u_old_admin")
    check("member-role reinvitation does not restore former admin privileges",
          restored_user.get("status") == "active" and restored_user.get("role") == "member")
    E.deactivate_member("u_old_admin", actor=admin)

    invite4 = E.create_invite(actor=admin, expected_login="erin@example.com")
    pending_app = E.submit_application(
        code=invite4["invite_code"], tailscale_login="erin@example.com",
        display_name="Erin", device_name="Erin PC", claim_secret="i" * 48,
        request_id="erin_pending_claim_0001")["application"]
    pending_approval = E.review_application(pending_app["id"], actor=admin,
                                            decision="approve")
    pending_user_id = pending_approval["application"]["user_id"]
    E.deactivate_member(pending_user_id, actor=admin)
    try:
        E.claim_device(pending_app["id"], tailscale_login="erin@example.com",
                       claim_secret="i" * 48)
        inactive_claim_status = 200
    except E.EnrollmentError as exc:
        inactive_claim_status = exc.status_code
    check("deactivation also invalidates approved but unclaimed device requests",
          inactive_claim_status == 410
          and E.application_status(pending_app["id"],
                                  tailscale_login="erin@example.com")["claim_available"] is False)

    # Keep cancellation behavior isolated to these tests.
    try:
        worker = getattr(L._agent_jobs, "_worker", None)
        if worker is not None:
            worker._stop_event.set()
        L._memory_worker._stop.set()
    except Exception:
        pass
    return passed, failed


if __name__ == "__main__":
    try:
        passed, failed = run()
        print(f"\n{passed} passed, {failed} failed")
        raise SystemExit(1 if failed else 0)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
