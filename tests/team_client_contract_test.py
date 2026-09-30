"""Team Hub client credential isolation checks using a temporary config."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="venus_team_client_"))
os.environ["VENUS_DATA_DIR"] = str(TMP / "data")
sys.path.insert(0, str(ROOT / "src"))

from venuschat_v1 import config_store as C  # noqa: E402
from venuschat_v1.api_client import ApiClient, _DenyRedirectHandler  # noqa: E402
from venuschat_v1.project_api import ProjectHubApi  # noqa: E402
import secure_store as S  # noqa: E402


class RecordingClient:
    def __init__(self):
        self.calls = []

    def get(self, path, **kw):
        self.calls.append(("GET", path, None, kw))
        return 200, {}

    def post(self, path, payload=None, **kw):
        self.calls.append(("POST", path, payload, kw))
        return 200, {}

    def delete(self, path, payload=None, **kw):
        self.calls.append(("DELETE", path, payload, kw))
        return 200, {}


def check_project_api_contract() -> None:
    recorder = RecordingClient()
    api = ProjectHubApi(recorder)  # type: ignore[arg-type]
    api.list_projects()
    api.list_hub_users()
    api.project_detail("p1")
    api.set_active_project("p1")
    api.create_project("Atlas", "shared files")
    api.claim_project("p1", "claim-once")
    api.list_invites("p1")
    api.create_invite("p1", "VN-7C4M-2Q9P", "user-b", "member")
    api.preview_invite("invite-once")
    api.accept_invite("invite-id", "invite-once")
    api.list_members("p1")
    api.remove_member("p1", "user-b")
    api.revoke_device("p1", "device-b")
    api.list_worker_calls("p1")
    api.create_worker_task(
        "p1", "device-b", "workspace.write",
        {"path": "notes/new.txt", "content": "approved content"}, "Create note")
    api.vote_worker_call("wcall_abc", "approve")
    api.archive_project("p1")
    api.list_governance_proposals("p1")
    api.governance_proposal("p1", "gp_1")
    api.create_governance_proposal(
        "p1", "promote_admin", "req-1", target_user_id="user-b")
    api.create_governance_proposal(
        "p1", "lower_approval_threshold", "req-2", required_approvals=1)
    api.vote_governance_proposal("p1", "gp_1", "approve")
    api.pending_owner_transfer("p1")
    api.accept_owner_transfer("p1")

    assert [(method, path) for method, path, _payload, _kw in recorder.calls] == [
        ("GET", "/api/v1/projects"),
        ("GET", "/api/v1/team"),
        ("GET", "/api/v1/projects/p1"),
        ("POST", "/api/v1/projects/active"),
        ("POST", "/api/v1/projects"),
        ("POST", "/api/v1/projects/p1/claim"),
        ("GET", "/api/v1/projects/p1/invites"),
        ("POST", "/api/v1/projects/p1/invites"),
        ("POST", "/api/v1/project-invites/preview"),
        ("POST", "/api/v1/project-invites/invite-id/accept"),
        ("GET", "/api/v1/projects/p1/members"),
        ("DELETE", "/api/v1/projects/p1/members/user-b"),
        ("POST", "/api/v1/projects/p1/devices/device-b/revoke"),
        ("GET", "/api/v1/worker/calls?project_id=p1"),
        ("POST", "/api/v1/worker/tasks"),
        ("POST", "/api/v1/worker/calls/wcall_abc/votes"),
        ("POST", "/api/v1/projects/p1/archive"),
        ("GET", "/api/v1/projects/p1/governance-proposals"),
        ("GET", "/api/v1/projects/p1/governance-proposals/gp_1"),
        ("POST", "/api/v1/projects/p1/governance-proposals"),
        ("POST", "/api/v1/projects/p1/governance-proposals"),
        ("POST", "/api/v1/projects/p1/governance-proposals/gp_1/votes"),
        ("GET", "/api/v1/projects/p1/owner-transfer/pending"),
        ("POST", "/api/v1/projects/p1/owner-transfer/accept"),
    ]
    assert recorder.calls[3][2] == {"project_id": "p1"}
    assert recorder.calls[4][2] == {"name": "Atlas", "description": "shared files"}
    assert recorder.calls[5][2] == {"claim_code": "claim-once"}
    assert recorder.calls[7][2] == {
        "terminal_display_code": "VN-7C4M-2Q9P", "user_id": "user-b", "role": "member"}
    assert recorder.calls[8][2] == {"invite_code": "invite-once"}
    assert recorder.calls[9][2] == {"invite_code": "invite-once"}
    assert recorder.calls[14][2] == {
        "project_id": "p1", "target_device_id": "device-b",
        "tool": "workspace.write",
        "args": {"path": "notes/new.txt", "content": "approved content"},
        "title": "Create note",
    }
    assert recorder.calls[15][2] == {"decision": "approve"}
    assert recorder.calls[16][2] == {}
    assert recorder.calls[19][2] == {
        "action": "promote_admin", "request_id": "req-1",
        "target_user_id": "user-b",
    }
    assert recorder.calls[20][2] == {
        "action": "lower_approval_threshold", "request_id": "req-2",
        "required_approvals": 1,
    }
    assert recorder.calls[21][2] == {"decision": "approve"}
    assert recorder.calls[23][2] == {}


def main() -> None:
    check_project_api_contract()
    C.CONFIG_PATH = TMP / "chat_config.json"
    S._loaded = False
    S._secrets = {}
    C.save_local_config({"llm_base": "http://127.0.0.1:8001"})
    S.store("api_token", "personal-local-token")
    raw = json.loads(C.CONFIG_PATH.read_text(encoding="utf-8"))
    raw["api_token"] = "__secure__"
    C.CONFIG_PATH.write_text(json.dumps(raw), encoding="utf-8")
    assert ApiClient()._headers().get("X-Api-Token") == "personal-local-token"

    # Switching the server binds the existing token to the old local origin.
    hub = "https://hub.example.ts.net"
    C.save_local_config({"llm_base": hub})
    before_join = ApiClient(hub)._headers()
    assert "X-Api-Token" not in before_join
    assert "X-Team-Device-Token" not in before_join

    C.save_team_connection({"origin": hub, "team_id": "team_contract",
                            "team_name": "Contract", "user_id": "user-a",
                            "device_id": "device-a"},
                           device_token="team-device-token")
    C.save_active_project_for_origin(hub, "project-for-user-a-device-a")
    assert C.active_project_for_origin(hub) == "project-for-user-a-device-a"
    C.save_team_connection({"origin": hub, "team_id": "team_contract",
                            "team_name": "Contract", "user_id": "user-a",
                            "device_id": "device-b"})
    assert C.active_project_for_origin(hub) == ""
    C.save_team_connection({"origin": hub, "team_id": "team_contract",
                            "team_name": "Contract", "user_id": "user-b",
                            "device_id": "device-b"})
    assert C.active_project_for_origin(hub) == ""
    headers = ApiClient(hub)._headers()
    assert headers.get("X-Team-Device-Token") == "team-device-token"
    assert "X-Api-Token" not in headers
    assert ApiClient(hub, use_default_token=False)._headers() == {"Content-Type": "application/json"}

    assert C.normalize_team_origin("http://192.0.2.10:8001") == "http://192.0.2.10:8001"
    assert C.normalize_team_origin("192.0.2.10") == "http://192.0.2.10:8001"
    assert C.normalize_team_origin("http://127.0.0.1:8001") == "http://127.0.0.1:8001"
    redirect = _DenyRedirectHandler()
    assert redirect.redirect_request(None, None, 302, "Found", {}, "https://other.invalid/") is None
    # Retain a legacy, explicitly configured non-Tailscale personal endpoint,
    # while refusing to send an unbound generic token to a new Serve Hub.
    raw = json.loads(C.CONFIG_PATH.read_text(encoding="utf-8"))
    raw["llm_base"] = "https://legacy-personal.example.com"
    raw.pop("api_token_origin", None)
    C.CONFIG_PATH.write_text(json.dumps(raw), encoding="utf-8")
    S.store("api_token", "legacy-personal-token")
    assert ApiClient()._headers().get("X-Api-Token") == "legacy-personal-token"
    raw["llm_base"] = "https://new-team.ts.net"
    C.CONFIG_PATH.write_text(json.dumps(raw), encoding="utf-8")
    assert "X-Api-Token" not in ApiClient()._headers()
    print("PASS project Hub endpoint contract; team credentials remain isolated")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
