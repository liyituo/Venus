"""Regression checks for queued context switches and stale task UI."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from venuschat_v1.app import VenusChatV1  # noqa: E402
from venuschat_v1.backend_bridge import BackendBridge  # noqa: E402
from venuschat_v1.config_store import personal_backend_for_team  # noqa: E402
from venuschat_v1.backend_bridge import SessionState  # noqa: E402
from venuschat_v1.settings_view import SettingsView  # noqa: E402


def main() -> None:
    app = VenusChatV1.__new__(VenusChatV1)
    app.client = SimpleNamespace(base="http://personal")
    app._pending_backend_switch = ("http://team", "Team")
    app._team_backend_origin = "http://team"
    app._personal_backend_base = "http://personal"
    app._backend_context_pending = False
    app.bridge = SimpleNamespace(client=app._client, _streaming=False,
                                 is_idle=lambda: False)
    app.chat_view = SimpleNamespace(_creating_session=False)
    app._refresh_backend_switch_control = lambda: None
    notices = []
    app.toast = lambda message, **_kw: notices.append(message)
    app.root = SimpleNamespace(after=lambda *_args: None)

    # Returning to the current origin must cancel the queued opposite switch.
    app.switch_backend_context("http://personal", "Personal")
    assert app._pending_backend_switch is None

    # Slow old-origin work must retain its original client and its result must
    # not reach the UI after changing context. A fresh request uses the new one.
    started, release = threading.Event(), threading.Event()
    new_origin_returned = threading.Event()
    hits, ui_events = [], []

    class FakeClient:
        def __init__(self, base, slow=False):
            self.base, self.slow = base, slow

        def get(self, path, **_kwargs):
            hits.append((self.base, path))
            if self.slow:
                started.set()
                assert release.wait(2)
            if self.base == "http://personal" and path == "/sessions":
                new_origin_returned.set()
            return "ok", (200, {"origin": self.base})

        def post(self, path, payload=None, **_kwargs):
            hits.append((self.base, path, payload))
            return 200, {"origin": self.base}

    old = FakeClient("http://team", slow=True)
    bridge = BackendBridge(old, lambda *args: ui_events.append(args))
    request_app = VenusChatV1.__new__(VenusChatV1)
    request_app._client = old
    request_app.bridge = bridge
    settings = SettingsView.__new__(SettingsView)
    settings.app = request_app
    bridge.submit("health", lambda: request_app.client.get("/health"))
    assert started.wait(1)
    bridge.submit("jobs", lambda: settings.client.get("/jobs"))
    bridge.submit("settings_save", lambda: settings.client.post(
        "/api/v1/config", {"model": "old-context"}))
    bridge._loading_sids.add(7)
    old_worker, old_queue = bridge._worker, bridge._work_queue
    bridge.set_client(FakeClient("http://personal"))
    assert not bridge._loading_sids
    bridge.ui = lambda kind, payload: ui_events.append((kind, payload))
    bridge.submit("sessions", lambda: request_app.client.get("/sessions"))
    assert new_origin_returned.wait(1), "new origin refresh must not wait for old slow requests"
    bridge.poll()
    release.set()
    deadline = time.monotonic() + 2
    while bridge._work_queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(.01)
    while old_queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(.01)
    assert not old_worker.is_alive(), "retired worker should exit after draining its queue"
    bridge.poll()
    assert hits == [("http://team", "/health"), ("http://personal", "/sessions"),
                    ("http://team", "/jobs"),
                    ("http://team", "/api/v1/config", {"model": "old-context"}),
                    ], hits
    assert ui_events == [("sessions", ("ok", (200, {"origin": "http://personal"})))], ui_events

    # Personal endpoint discovery rejects empty and other-team Hub values.
    cfg = {"llm_base": "https://hub-one.example",
           "team_connections": [{"origin": "https://hub-one.example"},
                                {"origin": "https://hub-two.example"}]}
    assert personal_backend_for_team("https://hub-one.example", "", cfg) == \
        "http://127.0.0.1:8001"
    assert personal_backend_for_team("https://hub-one.example",
                                     "https://hub-two.example", cfg) == \
        "http://127.0.0.1:8001"
    cfg["llm_base"] = "http://127.0.0.1:9001"
    assert personal_backend_for_team("https://hub-one.example",
                                     "https://hub-two.example", cfg) == \
        "http://127.0.0.1:9001"
    hub_host_config = {
        "llm_base": "https://hub.example",
        "personal_llm_base": "http://127.0.0.1:8002",
        "team_hub_local_base": "http://127.0.0.1:8001",
        "team_connections": [{"origin": "https://hub.example"}],
    }
    assert personal_backend_for_team("https://hub.example",
                                     "http://127.0.0.1:8001",
                                     hub_host_config) == "http://127.0.0.1:8002"
    assert personal_backend_for_team(
        "https://hub.example", "", {"llm_base": "http://127.0.0.1:8001",
                                      "team_hub_local_base": "http://127.0.0.1:8001",
                                      "team_connections": [{"origin": "https://hub.example"}]}
    ) == "http://127.0.0.1:8002"

    # Exercise the actual app transition in both directions while the retired
    # Hub worker is blocked, and verify per-origin drafts/projects/tasks.
    class ContextClient:
        def __init__(self, base, tasks):
            self.base, self.tasks = base, tasks

        def get(self, path, **_kwargs):
            if path.endswith("/health"):
                data = {"configured": True}
            elif path.endswith("/sessions"):
                data = {"sessions": []}
            elif path.endswith("/projects"):
                data = {"projects": [], "active": ""}
            elif path.endswith("/jobs?limit=20"):
                data = {"jobs": self.tasks, "active_count": 0}
            else:
                data = {}
            return "ok", (200, data)

    class FakeChat:
        _creating_session = False

        def __init__(self):
            self._drafts = {10: "Hub draft"}
            self.tasks = [{"id": "hub-task"}]
            self.unbound = "Hub unbound"
            self.resets = []

        def prepare_backend_context_switch(self):
            return self.unbound

        def reset_backend_context(self, label, drafts, unbound):
            self.resets.append((label, dict(drafts), unbound))
            self._drafts = dict(drafts)
            self.unbound = unbound
            self.tasks = []

        def refresh_backend_switch_button(self):
            pass

        def refresh_jobs(self, jobs):
            self.tasks = list(jobs)

    hub = ContextClient("http://team", [{"id": "hub-task"}])
    personal = ContextClient("http://personal", [{"id": "personal-task"}])
    context_app = VenusChatV1.__new__(VenusChatV1)
    context_app.client = hub
    context_app._team_backend_origin = hub.base
    context_app._personal_backend_base = personal.base
    context_app._team_backend_name = "Team"
    context_app._pending_backend_switch = None
    context_app._backend_session_preferences = {}
    context_app._backend_drafts = {}
    context_app._backend_unbound_drafts = {}
    context_app._backend_project_preferences = {}
    context_app._backend_context_pending = False
    context_app._model_change_pending = False
    context_app._active_project_change_serial = 0
    context_app.chat_view = FakeChat()
    context_app.toast = lambda *_args, **_kwargs: None
    context_app._backend_switch_lock = threading.RLock()
    context_events = []

    def on_context_event(kind, payload):
        context_events.append(kind)
        if kind == "jobs":
            context_app.chat_view.refresh_jobs(
                payload[1][1][1].get("jobs") or [])

    context_app.bridge = BackendBridge(hub, on_context_event)
    context_app.bridge.sessions = {10: SessionState(10, loaded=True)}
    context_app.bridge.current_sid = 10
    context_app.bridge.active_project_id = "hub-project"
    context_app.chat_view._drafts = {10: "Hub draft"}
    context_app.chat_view.unbound = "Hub unbound"

    slow_started, slow_release = threading.Event(), threading.Event()
    context_app.bridge.submit("health", lambda: (
        slow_started.set(), slow_release.wait(2), context_app.client.get("/slow"))[2])
    assert slow_started.wait(1)
    retired_worker, retired_queue = context_app.bridge._worker, context_app.bridge._work_queue
    clients = {hub.base: hub, personal.base: personal}
    with (patch("venuschat_v1.app.ApiClient", side_effect=lambda base: clients[base]),
          patch("venuschat_v1.app.project_preference_key", side_effect=lambda base: base),
          patch("venuschat_v1.app.active_project_for_origin", return_value=""),
          patch("venuschat_v1.app.save_active_project_for_origin"),
          patch("venuschat_v1.app.save_local_config")):
        context_app.switch_backend_context(personal.base, "Personal")
        assert context_app.client.base == personal.base
        assert context_app._backend_drafts[hub.base] == {10: "Hub draft"}
        context_app.bridge.current_sid = 20
        context_app.bridge.sessions = {20: SessionState(20, loaded=True)}
        context_app.bridge.active_project_id = "personal-project"
        context_app.chat_view._drafts = {20: "Personal draft"}
        context_app.chat_view.unbound = "Personal unbound"

        # The personal queue must finish despite the old Hub request remaining blocked.
        deadline = time.monotonic() + 1
        while "jobs" not in context_events and time.monotonic() < deadline:
            context_app.bridge.poll()
            time.sleep(.01)
        assert context_app.chat_view.tasks == [{"id": "personal-task"}]
        context_app.switch_backend_context(hub.base, "Team")
        assert context_app.chat_view._drafts == {10: "Hub draft"}
        assert context_app.chat_view.unbound == "Hub unbound"
        assert context_app.bridge.active_project_id == "hub-project"
        assert context_app._backend_drafts[personal.base] == {20: "Personal draft"}
        events_before = len(context_events)
        deadline = time.monotonic() + 1
        while len(context_events) == events_before and time.monotonic() < deadline:
            context_app.bridge.poll()
            time.sleep(.01)
        assert context_app.chat_view.tasks == [{"id": "hub-task"}]
    slow_release.set()
    deadline = time.monotonic() + 1
    while retired_queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(.01)
    assert not retired_worker.is_alive()

    chat_view = (ROOT / "src/venuschat_v1/chat_view.py").read_text(encoding="utf-8")
    reset = chat_view.split("def reset_backend_context", 1)[1].split(
        "def show_empty_backend_context", 1)[0]
    assert "_clear_server_jobs()" in reset
    clear_jobs = chat_view.split("def _clear_server_jobs", 1)[1].split(
        "def show_empty_backend_context", 1)[0]
    assert "after_cancel" in clear_jobs and "jobs_box.winfo_children()" in clear_jobs

    launcher = (ROOT / "scripts/Start-VenusChat.ps1").read_text(encoding="utf-8")
    assert launcher.index("$existingGui = Get-GuiProcess") < launcher.index(
        "$backendState = Get-BackendState")
    assert "远端 Hub $baseUrl 当前不可达，将打开 VenusChat" in launcher
    assert "Venus 后端" in launcher
    ensure = launcher.split("function Ensure-PersonalBackend", 1)[1].split(
        "\n}\n\ntry {", 1)[0]
    assert "Test-LocalVenusHub 8001" in ensure
    assert "savedConfig.team_hub_local_base" in ensure
    assert "if ($hubLocalUrl -or (Test-LocalVenusHub 8001))" in ensure
    assert "$personalPort = 8002" in ensure
    assert "Save-PersonalBackendConfig $personalUrl $hubLocalUrl" in ensure
    assert launcher.count("[void](Ensure-PersonalBackend)") == 1
    assert "(-not $localBackend) -or $reservedHubLocal -or (Test-LocalVenusHub $port)" in launcher
    assert "$reservedHubLocal = (([string]$config.team_hub_local_base).TrimEnd('/') -eq" in launcher
    assert "if ((-not $localBackend) -or $reservedHubLocal)" in launcher
    assert "if ((-not $localBackend) -or $reservedHubLocal -or (Test-LocalVenusHub $port))" in launcher
    assert "不会占用 8001 启动个人后端" in launcher
    assert '"personal_llm_base":os.environ["VENUS_PERSONAL_BACKEND_BASE"]' in launcher
    assert '"llm_base"' not in launcher.split("function Save-PersonalBackendConfig", 1)[1].split(
        "function Test-LocalVenusHub", 1)[0]
    save_config = launcher.split("function Save-PersonalBackendConfig", 1)[1].split(
        "function Test-LocalVenusHub", 1)[0]
    assert 'updates.update({"team_hub_local_base":hub} if hub else {})' in save_config
    app_source = (ROOT / "src/venuschat_v1/app.py").read_text(encoding="utf-8")
    assert "local_hub_base and initial_base == local_hub_base" in app_source
    assert "self.client = ApiClient(base=origin)" in app_source
    print("PASS backend switch intent, slow-request origin isolation, stale-response discard")


if __name__ == "__main__":
    main()
