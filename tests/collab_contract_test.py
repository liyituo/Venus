"""协作 M0/M1 契约测试：身份、审计、多人审批（免界面）。"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
os.environ.setdefault("PCAGENT_ALLOW_TEST_HOST", "1")

_TMP = tempfile.mkdtemp(prefix="venus_collab_")
os.environ["VENUS_DATA_DIR"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import llm_server as L  # noqa: E402
import team_collab as T  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

passed = failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


def setup_team() -> tuple[str, str, str]:
    tok_a = "test-token-alpha-001"
    tok_b = "test-token-beta-002"
    T.save_users([
        {
            "id": "u_a",
            "name": "Alpha",
            "token_hash": T.hash_token(tok_a),
            "role": "admin",
            "color": "#C9573D",
        },
        {
            "id": "u_b",
            "name": "Beta",
            "token_hash": T.hash_token(tok_b),
            "role": "member",
            "color": "#2F8A5B",
        },
    ])
    T.save_collab_config({
        "team_mode": True,
        "default_required": 1,
        "tool_required": {"delete_file": 2},
    })
    return tok_a, tok_b, tok_a  # admin token


# 每次用例前重建 client，避免模块级 confirm 表污染
client = TestClient(L.app)


print("== M0 身份与审计 ==")

# 个人模式：无 users.json
T.save_users([])
r = client.get("/api/v1/health")
check("personal mode open health", r.status_code == 200, r.text)

T.save_users([])
sid = client.post("/api/v1/sessions").json()["id"]
r = client.post(
    f"/api/v1/sessions/{sid}/messages",
    json={"messages": [{"role": "user", "content": "hello owner"}]},
)
check("owner append session", r.status_code == 200, r.text)
audit = client.get("/api/v1/audit").json().get("entries") or []
check("audit owner append", any(e.get("user") == "owner" for e in audit), audit)

tok_a, tok_b, admin_tok = setup_team()
r = client.get("/api/v1/team", headers={"X-User-Token": tok_a})
check("team list", r.status_code == 200 and len(r.json().get("users") or []) == 2, r.text)

r = client.post(
    f"/api/v1/sessions/{sid}/messages",
    json={"messages": [{"role": "user", "content": "from alpha"}]},
    headers={"X-User-Token": tok_a},
)
check("user A append", r.status_code == 200, r.text)
r = client.post(
    f"/api/v1/sessions/{sid}/messages",
    json={"messages": [{"role": "user", "content": "from beta"}]},
    headers={"X-User-Token": tok_b},
)
check("user B append", r.status_code == 200, r.text)
audit = client.get("/api/v1/audit", headers={"X-User-Token": tok_a}).json().get("entries") or []
check("audit two identities",
      any(e.get("user") == "u_a" for e in audit)
      and any(e.get("user") == "u_b" for e in audit),
      audit)

r = client.get("/api/v1/health", headers={"X-User-Token": "wrong-token"})
check("bad token 401", r.status_code == 401, r.text)

L.AUTH_TOKEN = "legacy-owner-secret"
try:
    r = client.get("/api/v1/health", headers={"X-Api-Token": "legacy-owner-secret"})
    check("legacy owner token", r.status_code == 200, r.text)
finally:
    L.AUTH_TOKEN = ""

print("== M0 team admin ==")
r = client.post(
    "/api/v1/team/users",
    json={"name": "Gamma", "role": "member"},
    headers={"X-User-Token": admin_tok},
)
check("admin create user", r.status_code == 200 and r.json().get("token"), r.text)
r = client.post(
    "/api/v1/team/users",
    json={"name": "Hack"},
    headers={"X-User-Token": tok_b},
)
check("member cannot create", r.status_code == 403, r.text)

print("== M1 多人审批 ==")
T.save_collab_config({"team_mode": False, "tool_required": {"delete_file": 2}})
check("personal project skips multi-vote", T.required_votes("delete_file") == 1, "")

import project_store as P  # noqa: E402
ok, created = P.create_project("个人任务", is_team=False)
check("create personal project", ok, created)
if ok:
    P.set_active_project(created["created"]["id"])
check("personal active no multi-vote", T.required_votes("delete_file") == 1, "")

ok, team_proj = P.create_project("团队协作", is_team=True)
check("create team project", ok, team_proj)
if ok:
    P.set_active_project(team_proj["created"]["id"])
T.save_collab_config({
    "team_mode": True,
    "default_required": 1,
    "tool_required": {"delete_file": 2},
})
check("team project enables multi-vote", T.required_votes("delete_file") == 2, "")

confirm_results: list[str | None] = []


def _wait_in_thread(rid: str) -> None:
    confirm_results.append(L._wait_confirm(rid, timeout=3, tool_name="delete_file"))


rid = "collab-test-confirm-1"
t = threading.Thread(target=_wait_in_thread, args=(rid,))
t.start()
time.sleep(0.05)

r = client.post(
    "/api/v1/agent/respond",
    json={"request_id": rid, "choice": "yes"},
    headers={"X-User-Token": tok_a},
)
check("first vote pending", r.status_code == 200 and r.json().get("pending") is True
      and r.json().get("yes_votes") == 1, r.text)

r = client.post(
    "/api/v1/agent/respond",
    json={"request_id": rid, "choice": "yes"},
    headers={"X-User-Token": tok_a},
)
check("duplicate vote idempotent", r.status_code == 200 and r.json().get("already_processed"), r.text)

r = client.post(
    "/api/v1/agent/respond",
    json={"request_id": rid, "choice": "yes"},
    headers={"X-User-Token": tok_b},
)
check("second vote resolves", r.status_code == 200 and r.json().get("choice") == "yes", r.text)
t.join(timeout=5)
check("waiter got yes", confirm_results and confirm_results[-1] == "yes", confirm_results)

confirm_results.clear()
rid2 = "collab-test-confirm-2"
t2 = threading.Thread(target=_wait_in_thread, args=(rid2,))
t2.start()
time.sleep(0.05)
client.post("/api/v1/agent/respond",
            json={"request_id": rid2, "choice": "yes"},
            headers={"X-User-Token": tok_a})
r = client.post("/api/v1/agent/respond",
                json={"request_id": rid2, "choice": "no"},
                headers={"X-User-Token": tok_b})
check("veto resolves no", r.status_code == 200 and r.json().get("choice") == "no", r.text)
t2.join(timeout=5)
check("waiter got no", confirm_results and confirm_results[-1] == "no", confirm_results)

rid3 = "collab-test-confirm-3"
ev3 = threading.Event()
with L._confirm_lock:
    L._confirm_table[rid3] = {
        "event": ev3,
        "choice": None,
        "task_id": "",
        "source": "http",
        "expires": time.monotonic() + 60,
        "tool": "delete_file",
        "required": 2,
        "approvals": {"u_a": "yes"},
    }
r = client.get("/api/v1/confirm/pending", headers={"X-User-Token": tok_b})
pending = r.json().get("pending") or []
check("pending for B", any(p.get("request_id") == rid3 for p in pending), pending)
with L._confirm_lock:
    L._confirm_table.pop(rid3, None)

print(f"\n{'=' * 40}\n  {passed} passed, {failed} failed\n{'=' * 40}")
sys.exit(1 if failed else 0)
