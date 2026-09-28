"""Local Remote Worker scope, confirmation and binding checks."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="venus_remote_worker_"))
os.environ["VENUS_DATA_DIR"] = str(TMP / "data")
sys.path.insert(0, str(ROOT / "src"))

import remote_worker as W  # noqa: E402


def rejected(fn) -> bool:
    try:
        fn()
    except W.RemoteWorkerError:
        return True
    return False


class FakeHub:
    def __init__(self, args: dict, *, project_id: str = "proj_a",
                 device_id: str = "device_a") -> None:
        self.args = args
        self.call_id = "wcall_" + "a" * 32
        self.digest = W.canonical_args_digest(args)
        self.project_id = project_id
        self.device_id = device_id
        self.completions: list[dict] = []

    def request(self, method: str, path: str, payload: dict | None = None):
        binding = {"call_id": self.call_id, "project_id": self.project_id,
                   "job_id": "job_abc", "target_device_id": self.device_id,
                   "tool_call_id": "tool_abc", "tool": "workspace.write",
                   "args_sha256": self.digest,
                   "expires_at": time.time() + 300,
                   "lease_until": time.time() + 120}
        if path == "/api/v1/worker/poll":
            return 200, {"call": {**binding, "lease_token": "lease-private"}}
        if path.endswith("/preflight"):
            return 200, {**binding, "args": dict(self.args)}
        if path.endswith("/complete"):
            self.completions.append(dict(payload or {}))
            return 200, {"status": "completed"}
        return 404, {"detail": "unexpected"}


def run() -> None:
    workspace = TMP / "workspace"
    workspace.mkdir()
    (workspace / "existing.txt").write_text("already here", encoding="utf-8")
    grant = {"enabled": True, "origin": "https://hub.example.ts.net",
             "team_id": "team_a", "project_id": "proj_a",
             "device_id": "device_a", "workspace": str(workspace),
             "tools": ["workspace.list", "workspace.read", "workspace.write"],
             "expires_at": time.time() + 300}
    W._write_grants([grant])

    assert rejected(lambda: W.resolve_workspace_path(workspace, "../outside"))
    assert rejected(lambda: W.resolve_workspace_path(workspace, "C:/windows/system.ini"))
    assert rejected(lambda: W.resolve_workspace_path(workspace, ".ssh/id_rsa"))
    assert rejected(lambda: W.resolve_workspace_path(workspace, ".env"))
    assert rejected(lambda: W.resolve_workspace_path(workspace, ".git/config"))
    assert rejected(lambda: W.resolve_workspace_path(workspace, "CON"))
    assert rejected(lambda: W.resolve_workspace_path(workspace, "COM1.txt"))
    assert rejected(lambda: W.resolve_workspace_path(workspace, "existing.txt."))
    assert rejected(lambda: W.resolve_workspace_path(workspace, "file?.txt"))
    assert W._execute_tool(grant, "workspace.read", {"path": "existing.txt"})["content"] \
        == "already here"
    assert rejected(lambda: W._execute_tool(grant, "workspace.write",
                                            {"path": "existing.txt", "content": "replace"}))
    assert (workspace / "existing.txt").read_text(encoding="utf-8") == "already here"

    args = {"path": "new.txt", "content": "approved content"}
    denied = FakeHub(args)
    assert W.process_once(grant, denied, confirm=lambda _call, _args: False,
                          stop_check=lambda: False)
    assert not (workspace / "new.txt").exists()
    assert denied.completions[0]["error"] == "local_confirmation_denied"

    bad_binding = FakeHub(args, project_id="proj_other")
    assert rejected(lambda: W.process_once(grant, bad_binding,
                                           confirm=lambda _call, _args: True,
                                           stop_check=lambda: False))
    assert not (workspace / "new.txt").exists()

    approved = FakeHub(args)
    assert W.process_once(grant, approved, confirm=lambda _call, _args: True,
                          stop_check=lambda: False)
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "approved content"
    assert approved.completions[0]["result"]["created"] is True

    # Revoking the saved grant while a write prompt is open must prevent the
    # already running Worker loop from touching the file afterwards.
    revoked_args = {"path": "revoked.txt", "content": "must not be written"}
    revoked = FakeHub(revoked_args)

    def revoke_during_prompt(_call, _args):
        W.revoke_local_grant("proj_a")
        return True

    assert rejected(lambda: W.process_once(
        grant, revoked, confirm=revoke_during_prompt, stop_check=lambda: False))
    assert not (workspace / "revoked.txt").exists()
    assert rejected(lambda: W.process_once(
        grant, revoked, confirm=lambda _call, _args: True,
        stop_check=lambda: False))
    assert rejected(lambda: W.process_once(grant, approved,
                                           confirm=lambda _call, _args: True,
                                           stop_check=lambda: True))
    print("PASS Worker path, binding, local confirmation, create-only and stop contract")


if __name__ == "__main__":
    try:
        run()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
