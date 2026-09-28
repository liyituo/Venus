"""Read-only preview and owner-only legacy project migration."""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="venus_project_migration_"))
DATA = TMP / "hub-data"
DATA.mkdir()
os.environ["VENUS_DATA_DIR"] = str(DATA)
sys.path.insert(0, str(ROOT / "src"))

import project_access as A  # noqa: E402
import project_store as P  # noqa: E402
import team_collab as C  # noqa: E402
import team_enrollment as E  # noqa: E402


def run() -> None:
    C.save_users([{"id": "u_real", "name": "Real owner", "role": "admin",
                   "status": "active", "enrollment_managed": True},
                  {"id": "u_peer", "name": "Peer", "role": "member",
                   "status": "active", "enrollment_managed": True}])
    E._save_access({"invites": [], "applications": [], "devices": [
        {"id": "d_real", "user_id": "u_real", "status": "active",
         "token_hash": "sha256:" + "a" * 64, "name": "Real terminal"},
        {"id": "d_peer", "user_id": "u_peer", "status": "active",
         "token_hash": "sha256:" + "b" * 64, "name": "Peer terminal"},
    ]})
    good = P.create_project("Old team", is_team=True, owner_id="u_real")[1]["created"]["id"]
    unknown = P.create_project("Unmapped team", is_team=True, owner_id="owner")[1]["created"]["id"]
    db = DATA / "project_access.db"
    preview = A.migrate_legacy_projects(dry_run=True)
    assert not db.exists(), "dry-run must not initialize SQLite"
    decisions = {row["project_id"]: row["action"] for row in preview}
    assert decisions == {good: "grant-owner-only", unknown: "needs_admin_mapping"}

    applied = A.migrate_legacy_projects(dry_run=False)
    assert {row["project_id"]: row["action"] for row in applied} == {
        good: "applied-owner-only", unknown: "needs_admin_mapping"}
    assert A.access(good, "u_real", "d_real")
    assert not A.access(good, "u_peer", "d_peer")
    assert not A.access(unknown, "owner", "d_real")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_memberships").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM project_access_migration_issues").fetchone()[0] == 1
        conn.execute("UPDATE project_device_grants SET status='revoked' WHERE project_id=?",
                     (good,))
        conn.commit()
    A.ensure_schema()
    assert not A.access(good, "u_real", "d_real"), "revoked grant must not be remigrated"
    assert A.migrate_legacy_projects(dry_run=True) == []
    assert A.migrate_legacy_projects(dry_run=False) == []
    print("PASS migration dry-run, owner-only apply, unmapped owner and revocation persistence")


if __name__ == "__main__":
    try:
        run()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
