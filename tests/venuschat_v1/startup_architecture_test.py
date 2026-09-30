"""Regression checks for the API-only backend and independent desktop launcher."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


def main() -> None:
    # -S keeps site-packages (including all server dependencies) off sys.path.
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    isolated = subprocess.run(
        [sys.executable, "-S", "-c",
         "import venuschat_v1; import venuschat_v1.app; "
         "import venuschat_v1.startup_backend; "
         "assert 'fastapi' not in __import__('sys').modules"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert isolated.returncode == 0, isolated.stderr

    os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
    os.environ.setdefault("PCAGENT_ALLOW_TEST_HOST", "1")
    os.environ.setdefault("PCAGENT_DATA_DIR", tempfile.mkdtemp(prefix="venus_api_only_"))
    sys.path.insert(0, str(SRC))
    import llm_server  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402

    client = TestClient(llm_server.app)
    for path in ("/", "/venus", "/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path
    response = client.get("/api/v1/ready")
    assert response.status_code == 200, response.text
    assert response.json()["service"] == "venus-llm"
    api_routes = {getattr(route, "path", None) for route in llm_server.app.routes}
    assert {"/api/v1/ready", "/api/v1/sessions", "/api/v1/chat/stream",
            "/api/v1/team/public"} <= api_routes

    gui = (ROOT / "scripts/Start-VenusChat.ps1").read_text(encoding="utf-8")
    assert "Get-LocalBackendState" not in gui
    assert "Read-Host" not in gui
    assert "VENUS_STARTUP_LOCAL_STATE" not in gui
    assert "Start-Process -FilePath $pythonwExe" in gui
    startup = (ROOT / "src/venuschat_v1/startup_backend.py").read_text(encoding="utf-8")
    assert "启动本机后端" in startup and "配置服务器连接" in startup
    assert startup.index("def start_local(self)") < startup.index(
        "start_local_backend(self.base)")
    import venuschat_v1.startup_backend as startup_backend  # noqa: E402
    normalize_remote_origin = startup_backend.normalize_remote_origin
    assert normalize_remote_origin("https://hub.example/") == "https://hub.example:8001"
    for bad in ("", "http://0.0.0.0:8001",
                "https://user:secret@hub.example",
                "https://hub.example/path", "https://hub.example?token=x"):
        try:
            normalize_remote_origin(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid remote origin: {bad!r}")
    # Both local and remote use the selected endpoint, regardless of launcher hints.
    for base in ("http://127.0.0.1:8001", "http://192.0.2.1:8001", "https://hub.example:8001"):
        assert normalize_remote_origin(base) == base
        fake_app = SimpleNamespace(client=SimpleNamespace(base=base),
                                   root=SimpleNamespace(after=lambda delay, callback: callback()))
        def immediate_thread(*, target, **kwargs):
            return SimpleNamespace(start=target)
        for ready in (True, False):
            with patch.object(startup_backend, "StartupBackendDialog") as dialog, \
                    patch.object(startup_backend, "probe_backend", return_value=ready) as probe, \
                    patch.object(startup_backend.threading, "Thread", side_effect=immediate_thread):
                startup_backend.show_startup_backend_prompt(fake_app)
                probe.assert_called_once_with(base)
                assert dialog.call_count == (0 if ready else 1)
    assert "Start-VenusChat.ps1" in (ROOT / "scripts/启动VenusChat V1.bat").read_text(encoding="utf-8")
    assert not (ROOT / "scripts/一键启动控制台.bat").exists()
    assert not (ROOT / "scripts/Start-VenusConsole.ps1").exists()

    if os.name == "nt":
        paths = [str(ROOT / "scripts" / name) for name in ("Start-VenusChat.ps1", "Start-VenusServer.ps1", "start_team_hub.ps1", "check_team_hub.ps1")]
        quoted = ",".join("'" + path.replace("'", "''") + "'" for path in paths)
        command = ("$errors = @(); foreach ($path in @(" + quoted + ")) { "
                   "$tokens = $null; $parseErrors = $null; "
                   "[System.Management.Automation.Language.Parser]::ParseFile("
                   "$path, [ref]$tokens, [ref]$parseErrors) | Out-Null; "
                   "$errors += $parseErrors }; if ($errors.Count) { "
                   "$errors | Out-String | Write-Error; exit 1 }")
        parsed = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", check=False)
        assert parsed.returncode == 0, parsed.stderr

    print("PASS API-only routes, desktop import, GUI startup choices and launcher syntax")


if __name__ == "__main__":
    main()
