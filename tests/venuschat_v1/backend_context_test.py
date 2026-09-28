"""Contract checks for keeping private chats separate from team projects."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from venuschat_v1.backend_bridge import BackendBridge, SessionState  # noqa: E402


class RecordingClient:
    base = "http://127.0.0.1:8001"

    def __init__(self):
        self.posts = []

    def post(self, path, payload=None, **_kwargs):
        self.posts.append((path, payload))
        return 200, {"ok": True}


def main() -> None:
    client = RecordingClient()
    bridge = BackendBridge(client, lambda *_args: None)
    with (patch("venuschat_v1.backend_bridge.team_connection", return_value=None),
          patch("venuschat_v1.backend_bridge.active_project_for_origin", return_value="")):
        bridge._apply_projects(200, {"projects": [
            {"id": "team_alpha", "title": "Alpha", "is_team": True},
        ], "active": "team_alpha"})
    assert bridge.active_project_id == "", "server-global selection must not bind GUI sessions"

    with (patch("venuschat_v1.backend_bridge.team_connection", return_value={"team_id": "team"}),
          patch("venuschat_v1.backend_bridge.save_active_project_for_origin")):
        bridge._apply_projects(200, {"projects": [
            {"id": "team_alpha", "title": "Alpha", "is_team": True},
        ], "active": "team_alpha"})
    assert bridge.active_project_id == "team_alpha", "Hub device selection should be restored"

    bridge.active_project_id = "team_alpha"
    bridge.sessions[7] = SessionState(
        sid=7, loaded=True,
        messages=[{"role": "user", "content": "private conversation secret"}],
    )
    bridge.current_sid = 7
    submitted = []
    bridge.submit = lambda kind, fn: submitted.append((kind, fn))
    bridge.dispatch("write shared release notes", bridge.sessions[7])
    assert len(submitted) == 1
    kind, work = submitted.pop()
    assert kind == "dispatch"
    work()
    path, payload = client.posts[-1]
    assert path == "/api/v1/jobs"
    assert payload["project_id"] == "team_alpha"
    assert payload["session_id"] is None
    assert payload["messages"] == [
        {"role": "user", "content": "write shared release notes"}
    ], "team jobs must not receive the open private chat history"

    captured = {}

    class FakeStream:
        def __init__(self, _client, *, on_event, on_done, on_error):
            self.on_event, self.on_done, self.on_error = on_event, on_done, on_error

        def start(self, body):
            captured.update(body)

    bridge._persist_messages = lambda *_args, **_kwargs: None
    with (patch("venuschat_v1.backend_bridge.ChatStreamWorker", FakeStream),
          patch("venuschat_v1.backend_bridge.load_config", return_value={"workspace": ""})):
        bridge.send_message("continue privately", lambda *_args: object())
    assert captured["project_id"] is None
    assert captured["session_id"] == 7
    print("PASS private/team conversation boundary")


if __name__ == "__main__":
    main()
