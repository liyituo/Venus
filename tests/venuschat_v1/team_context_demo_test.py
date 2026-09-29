"""Team context can dispatch through Serve without exposing Hub sessions."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from venuschat_v1.backend_bridge import BackendBridge  # noqa: E402
from venuschat_v1.chat_view import ChatView  # noqa: E402


class FakeClient:
    base = "https://hub.example.ts.net"

    def __init__(self):
        self.calls = []

    def get(self, path, **_kwargs):
        self.calls.append(("GET", path))
        if path == "/api/v1/ready":
            return 200, {"ok": True, "configured": False, "model": ""}
        if path == "/api/v1/team/me":
            return 200, {"ok": True, "user": {"role": "admin"}}
        if path == "/api/v1/projects":
            return 200, {"projects": []}
        if path.startswith("/api/v1/jobs"):
            return 200, {"jobs": []}
        raise AssertionError(f"Unexpected Team API: {path}")

    def post(self, path, body, **_kwargs):
        self.calls.append(("POST", path, body))
        return 200, {"ok": True, "job": {"id": "job_demo"}}


class FakeInput:
    def __init__(self, text):
        self.text = text

    def get(self, *_args):
        return self.text

    def delete(self, *_args):
        self.text = ""


def main():
    client = FakeClient()
    events = []
    with patch("venuschat_v1.backend_bridge.team_connection", return_value={"status": "joined"}):
        bridge = BackendBridge(client, lambda kind, payload: events.append((kind, payload)))
        bridge.refresh_all()
        bridge._work_queue.join()
        bridge.poll()
        assert ("GET", "/api/v1/ready") in client.calls
        assert ("GET", "/api/v1/health") not in client.calls
        assert not any(path == "/api/v1/sessions"
                       for method, path, *_rest in client.calls if method == "GET")
        assert next(payload for kind, payload in events if kind == "sessions")[1][1] == {"sessions": []}
        health = next(payload for kind, payload in events if kind == "health")[1][1]
        assert not health["configured"], "model readiness stays separate from Hub access"

        bridge.projects = [{"id": "demo", "is_team": True}]
        bridge.active_project_id = "demo"
        bridge.dispatch("演示任务")
        bridge._work_queue.join()
        sent = [call for call in client.calls if call[0] == "POST"][-1]
        assert sent[1] == "/api/v1/jobs"
        assert sent[2]["project_id"] == "demo" and sent[2]["session_id"] is None
        assert sent[2]["messages"] == [{"role": "user", "content": "演示任务"}]

    notices, dispatched = [], []
    view = ChatView.__new__(ChatView)
    view.input_box = FakeInput("! 演示任务")
    view.app = SimpleNamespace(toast=lambda message, **_kw: notices.append(message))
    view.bridge = SimpleNamespace(client=client, _streaming=False,
                                  active_project_id="demo",
                                  dispatch=lambda *args, **kwargs: dispatched.append((args, kwargs)),
                                  create_session=lambda: (_ for _ in ()).throw(AssertionError("Hub session created")))
    view.agent_name = "通用智能体"
    view._creating_session = False
    view._update_placeholder = lambda: None
    with patch("venuschat_v1.chat_view.team_connection", return_value={"status": "joined"}):
        view.send_message()
        assert dispatched[0][0] == ("演示任务",)
        assert view.input_box.text == ""
        view.new_chat()
        assert any("个人空间" in notice for notice in notices)
    print("team context demo routes passed")


if __name__ == "__main__":
    main()
