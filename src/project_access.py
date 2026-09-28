"""Project-scoped membership, terminal grants, claims, and invitations.

Hub identity and device credentials remain owned by ``team_enrollment``. This
module stores only project authorization metadata and hashes of one-time codes.
All write transitions use SQLite transactions so claim/invite replay is safe.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from data_paths import data_dir

_DB_NAME = "project_access.db"
_SCHEMA_VERSION = 2
_ROLES = {"owner", "admin", "member", "viewer"}
_CLAIM_TTL = 15 * 60
_CLAIM_MAX_ATTEMPTS = 5
_DEFAULT_INVITE_TTL = 24 * 60 * 60
_MAX_INVITE_TTL = 30 * 24 * 60 * 60
_GOVERNANCE_TTL = 24 * 60 * 60
_GOVERNANCE_ACTIONS = {"promote_admin", "lower_approval_threshold", "owner_transfer"}
_GOVERNANCE_ELIGIBLE_ROLES = {"owner", "admin", "member"}
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")


class ProjectAccessError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _db_path() -> Path:
    return data_dir() / _DB_NAME


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def _connection():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def _audit(actor_id: str, action: str, detail: dict[str, Any]) -> None:
    try:
        import team_collab
        team_collab.audit(actor_id or "system", action, detail)
    except Exception:
        # Authorization state remains authoritative even if optional audit I/O
        # is temporarily unavailable. Callers never get secret material here.
        pass


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _now() -> float:
    return time.time()


def _begin(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


def ensure_schema(*, migrate_legacy: bool = False) -> None:
    """Create schema only; old projects migrate through the explicit CLI path."""
    with _connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS project_access_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_memberships (
                project_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('owner','admin','member','viewer')),
                status TEXT NOT NULL CHECK(status IN ('active','removed')),
                created_by TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                removed_by TEXT,
                removed_at REAL,
                PRIMARY KEY(project_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS project_device_grants (
                project_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','revoked')),
                granted_by TEXT NOT NULL,
                granted_at REAL NOT NULL,
                revoked_by TEXT,
                revoked_at REAL,
                PRIMARY KEY(project_id, device_id)
            );
            CREATE TABLE IF NOT EXISTS project_claims (
                project_id TEXT PRIMARY KEY,
                creator_user_id TEXT NOT NULL,
                creator_device_id TEXT NOT NULL,
                secret_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('issued','claimed','expired','revoked')),
                expires_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL,
                created_at REAL NOT NULL,
                claimed_by TEXT,
                claimed_device_id TEXT,
                claimed_at REAL
            );
            CREATE TABLE IF NOT EXISTS project_invites (
                invite_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                expected_user_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin','member','viewer')),
                secret_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('issued','accepted','rejected','expired','revoked')),
                created_by TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                accepted_by TEXT,
                accepted_device_id TEXT,
                accepted_at REAL
            );
            CREATE INDEX IF NOT EXISTS project_invites_project_idx
                ON project_invites(project_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS project_policies (
                project_id TEXT PRIMARY KEY,
                required_approvals INTEGER NOT NULL DEFAULT 1,
                policy_version INTEGER NOT NULL DEFAULT 1,
                updated_by TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS active_projects (
                user_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(user_id, device_id)
            );
            CREATE TABLE IF NOT EXISTS project_owner_transfers (
                transfer_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                from_user_id TEXT NOT NULL,
                to_user_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','accepted','cancelled')),
                created_at REAL NOT NULL,
                accepted_at REAL,
                accepted_by TEXT,
                governance_proposal_id TEXT
            );
            CREATE TABLE IF NOT EXISTS project_governance_proposals (
                proposal_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN
                    ('promote_admin','lower_approval_threshold','owner_transfer')),
                proposer_user_id TEXT NOT NULL,
                proposer_device_id TEXT NOT NULL,
                proposer_updated_at REAL NOT NULL,
                proposer_granted_at REAL NOT NULL,
                target_user_id TEXT NOT NULL DEFAULT '',
                target_role TEXT NOT NULL DEFAULT '',
                target_status TEXT NOT NULL DEFAULT '',
                target_updated_at REAL,
                previous_approvals INTEGER,
                requested_approvals INTEGER,
                parameters_json TEXT NOT NULL,
                parameters_sha256 TEXT NOT NULL,
                policy_version INTEGER NOT NULL,
                required_approvals INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('pending','approved','applied','rejected','expired','stale','cancelled')),
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                resolved_at REAL,
                result_json TEXT,
                UNIQUE(project_id,proposer_user_id,request_id)
            );
            CREATE INDEX IF NOT EXISTS project_governance_project_idx
                ON project_governance_proposals(project_id,created_at DESC);
            CREATE TABLE IF NOT EXISTS project_governance_votes (
                vote_id TEXT PRIMARY KEY,
                proposal_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
                membership_updated_at REAL NOT NULL,
                grant_granted_at REAL NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_access_migration_issues (
                project_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at REAL NOT NULL
            );
        """)
        current = conn.execute(
            "SELECT value FROM project_access_meta WHERE key='schema_version'"
        ).fetchone()
        if not current:
            conn.execute(
                "INSERT INTO project_access_meta(key,value) VALUES('schema_version',?)",
                (str(_SCHEMA_VERSION),),
            )
        elif int(current["value"]) > _SCHEMA_VERSION:
            raise RuntimeError("project access database was created by a newer Venus version")
        elif int(current["value"]) < _SCHEMA_VERSION:
            conn.execute("UPDATE project_access_meta SET value=? WHERE key='schema_version'",
                         (str(_SCHEMA_VERSION),))
        transfer_columns = {str(row[1]) for row in conn.execute(
            "PRAGMA table_info(project_owner_transfers)").fetchall()}
        if "governance_proposal_id" not in transfer_columns:
            conn.execute("ALTER TABLE project_owner_transfers "
                         "ADD COLUMN governance_proposal_id TEXT")
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS project_owner_transfer_proposal_idx
            ON project_owner_transfers(governance_proposal_id)
            WHERE governance_proposal_id IS NOT NULL""")
    if migrate_legacy:
        with _connection() as conn:
            migrated = conn.execute(
                "SELECT value FROM project_access_meta WHERE key='legacy_migrated_at'"
            ).fetchone()
        if not migrated:
            _migrate_legacy_projects()
            with _connection() as conn:
                conn.execute("INSERT OR IGNORE INTO project_access_meta(key,value) VALUES('legacy_migrated_at',?)",
                             (str(_now()),))


def _migrate_legacy_projects(*, dry_run: bool = False) -> list[dict]:
    """Import each old team's declared owner only; never grant peers by default."""
    migration_by_project = {str(row.get("project_id") or ""): row
                            for row in legacy_migration_plan()}
    import project_store
    projects = project_store.list_projects()
    plan: list[dict] = []
    for item in projects:
        project_id = str(item.get("id") or "")
        project = project_store.get_project(project_id, include_checkpoints=0)
        meta = (project or {}).get("meta") or {}
        if not project_id or not meta.get("is_team"):
            continue
        if meta.get("access_control_managed") or meta.get("status") == "pending_claim":
            continue
        owner_id = str(meta.get("owner_id") or "")
        if not owner_id:
            continue
        # A string written by a pre-Hub local client is not necessarily an
        # enrollment-managed identity. Never manufacture an owner membership
        # for an identity that has no currently usable Hub terminal.
        migration = migration_by_project.get(project_id)
        if migration and migration.get("action") == "needs_admin_mapping":
            plan.append(migration)
            if not dry_run:
                with _connection() as conn:
                    conn.execute("""INSERT INTO project_access_migration_issues
                        (project_id,owner_id,reason,created_at)
                        VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
                        owner_id=excluded.owner_id,reason=excluded.reason,
                        created_at=excluded.created_at""",
                        (project_id, owner_id, "owner_has_no_active_hub_device", _now()))
            continue
        if dry_run:
            plan.append({"project_id": project_id, "owner_id": owner_id,
                         "status": str(meta.get("status") or "active"),
                         "action": "grant-owner-only"})
            continue
        now = _now()
        conn = _connect()
        try:
            _begin(conn)
            exists = conn.execute(
                "SELECT 1 FROM project_memberships WHERE project_id=? AND user_id=?",
                (project_id, owner_id),
            ).fetchone()
            if not exists:
                conn.execute("""INSERT INTO project_memberships
                    (project_id,user_id,role,status,created_by,created_at,updated_at)
                    VALUES(?,?,'owner','active',?,?,?)""",
                    (project_id, owner_id, owner_id, now, now))
            conn.execute("""INSERT OR IGNORE INTO project_policies
                (project_id,required_approvals,policy_version,updated_by,updated_at)
                VALUES(?,1,1,?,?)""", (project_id, owner_id, now))
            for device_id in (migration or {}).get("active_device_ids", []):
                conn.execute("""INSERT OR IGNORE INTO project_device_grants
                    (project_id,device_id,status,granted_by,granted_at)
                    VALUES(?,?,'active',?,?)""",
                    (project_id, device_id, owner_id, now))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        # The existing project owner may keep using their own currently active
        # Hub devices. Other legacy users/devices receive no project grant.
        plan.append({"project_id": project_id, "owner_id": owner_id,
                     "status": str(meta.get("status") or "active"),
                     "action": "applied-owner-only"})
    return plan


def legacy_migration_plan() -> list[dict]:
    """Read-only preview of projects eligible for conservative owner migration."""
    path = _db_path()
    if path.is_file():
        try:
            uri = f"file:{path.as_posix()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=2) as conn:
                marker = conn.execute("""SELECT 1 FROM project_access_meta
                    WHERE key='legacy_migrated_at'""").fetchone()
            if marker:
                return []
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                pass
            else:
                # A malformed or older database is not modified during preview;
                # report the file issue to the apply path instead.
                return [{"action": "database-unreadable", "database": str(path)}]
        except sqlite3.Error:
            # A malformed or older database is not modified during preview;
            # report the file issue to the apply path instead.
            return [{"action": "database-unreadable", "database": str(path)}]
    import project_store
    import team_collab
    import team_enrollment
    # This preview must remain strictly read-only. Read the enrollment JSON
    # structures without calling APIs that lazily backfill display codes.
    users = {str(row.get("id") or ""): row for row in team_collab.load_users()
             if row.get("id") and row.get("status", "active") == "active"
             and row.get("enrollment_managed")}
    try:
        access = team_enrollment._load_access()
    except Exception:
        access = {"devices": []}
    devices_by_user: dict[str, list[str]] = {}
    for device in access.get("devices", []):
        uid = str(device.get("user_id") or "")
        device_id = str(device.get("id") or "")
        if (uid in users and device_id and device.get("status") == "active"
                and device.get("token_hash")):
            devices_by_user.setdefault(uid, []).append(device_id)
    rows = []
    for item in project_store.list_projects():
        project_id = str(item.get("id") or "")
        project = project_store.get_project(project_id, include_checkpoints=0)
        meta = (project or {}).get("meta") or {}
        if (project_id and meta.get("is_team") and not meta.get("access_control_managed")
                and meta.get("status") != "pending_claim" and meta.get("owner_id")):
            owner_id = str(meta.get("owner_id") or "")
            device_ids = devices_by_user.get(owner_id, [])
            rows.append({"project_id": project_id,
                         "owner_id": owner_id,
                         "status": str(meta.get("status") or "active"),
                         "action": ("grant-owner-only" if device_ids
                                    else "needs_admin_mapping"),
                         **({"active_device_ids": device_ids} if device_ids else {})})
    return rows


def migrate_legacy_projects(*, dry_run: bool = True) -> list[dict]:
    """CLI-friendly migration entry point; default operation is read-only."""
    if dry_run:
        return legacy_migration_plan()
    ensure_schema(migrate_legacy=False)
    with _connection() as conn:
        marker = conn.execute("SELECT value FROM project_access_meta WHERE key='legacy_migrated_at'").fetchone()
    if marker:
        return []
    plan = _migrate_legacy_projects()
    with _connection() as conn:
        conn.execute("INSERT OR IGNORE INTO project_access_meta(key,value) VALUES('legacy_migrated_at',?)",
                     (str(_now()),))
    return plan


def create_claim(project_id: str, creator_user_id: str, creator_device_id: str,
                 *, ttl: int = _CLAIM_TTL) -> str:
    ensure_schema()
    pid = str(project_id or "").strip()
    uid = str(creator_user_id or "").strip()
    did = str(creator_device_id or "").strip()
    if not pid or not uid or not did:
        raise ProjectAccessError(422, "项目或创建者身份无效")
    code = "VPC-" + secrets.token_urlsafe(32)
    now = _now()
    expires = now + max(60, min(int(ttl or _CLAIM_TTL), _CLAIM_TTL))
    with _connection() as conn:
        _begin(conn)
        conn.execute("""INSERT INTO project_claims
            (project_id,creator_user_id,creator_device_id,secret_hash,status,expires_at,attempts,
             max_attempts,created_at)
            VALUES(?,?,?,?,'issued',?,0,?,?)
            ON CONFLICT(project_id) DO UPDATE SET creator_user_id=excluded.creator_user_id,
            creator_device_id=excluded.creator_device_id,
            secret_hash=excluded.secret_hash,status='issued',expires_at=excluded.expires_at,
            attempts=0,max_attempts=excluded.max_attempts,created_at=excluded.created_at,
            claimed_by=NULL,claimed_device_id=NULL,claimed_at=NULL""",
            (pid, uid, did, _hash_secret(code), expires, _CLAIM_MAX_ATTEMPTS, now))
        conn.commit()
    return code


def claim_project(project_id: str, *, user_id: str, device_id: str,
                  claim_code: str) -> dict:
    """Commit the claim atomically, then finish the JSON metadata idempotently.

    SQLite is committed before the project JSON changes. If the process stops
    between stores, the same creator/device can retry the same code to complete
    the JSON transition without creating another owner or grant.
    """
    ensure_schema()
    pid = str(project_id or "").strip()
    uid = str(user_id or "").strip()
    did = str(device_id or "").strip()
    code = str(claim_code or "")
    if not uid:
        raise ProjectAccessError(401, "需要已认证的 Hub 用户")
    if len(code) > 256 or not code:
        raise ProjectAccessError(401, "项目认领码无效")
    now = _now()
    import project_store
    conn = _connect()
    already_processed = False
    try:
        _begin(conn)
        row = conn.execute("SELECT * FROM project_claims WHERE project_id=?", (pid,)).fetchone()
        if not row:
            raise ProjectAccessError(404, "待认领项目不存在")
        if row["creator_user_id"] != uid or row["creator_device_id"] != did:
            raise ProjectAccessError(403, "只有项目创建者的指定终端可以认领")
        if row["status"] == "claimed":
            project = project_store.get_project(pid, include_checkpoints=0)
            if project and (project.get("meta") or {}).get("status") == "active":
                raise ProjectAccessError(410, "项目认领码已使用")
            if not hmac.compare_digest(str(row["secret_hash"]), _hash_secret(code)):
                raise ProjectAccessError(410, "项目认领码已使用、撤销或失效")
            already_processed = True
        elif row["status"] != "issued":
            raise ProjectAccessError(410, "项目认领码已使用、撤销或失效")
        elif float(row["expires_at"]) <= now:
            conn.execute("UPDATE project_claims SET status='expired' WHERE project_id=?", (pid,))
            conn.commit()
            raise ProjectAccessError(410, "项目认领码已过期")
        elif int(row["attempts"]) >= int(row["max_attempts"]):
            raise ProjectAccessError(429, "认领尝试次数已用完")
        elif not hmac.compare_digest(str(row["secret_hash"]), _hash_secret(code)):
            attempts = int(row["attempts"]) + 1
            conn.execute("UPDATE project_claims SET attempts=? WHERE project_id=?",
                         (attempts, pid))
            if attempts >= int(row["max_attempts"]):
                conn.execute("UPDATE project_claims SET status='revoked' WHERE project_id=?", (pid,))
            conn.commit()
            _audit(uid, "project.claim.failed", {"project_id": pid, "attempts": attempts})
            if attempts >= int(row["max_attempts"]):
                raise ProjectAccessError(429, "认领尝试次数已用完")
            raise ProjectAccessError(401, "项目认领码无效")
        if not already_processed:
            project = project_store.get_project(pid, include_checkpoints=0)
            if not project or (project.get("meta") or {}).get("status") != "pending_claim":
                raise ProjectAccessError(409, "项目当前状态不能认领")
            conn.execute("""UPDATE project_claims SET status='claimed',claimed_by=?,
                claimed_device_id=?,claimed_at=? WHERE project_id=?""",
                (uid, did, now, pid))
            conn.execute("""INSERT INTO project_memberships
                (project_id,user_id,role,status,created_by,created_at,updated_at)
                VALUES(?,?,'owner','active',?,?,?)
                ON CONFLICT(project_id,user_id) DO UPDATE SET role='owner',status='active',
                updated_at=excluded.updated_at,removed_by=NULL,removed_at=NULL""",
                (pid, uid, uid, now, now))
            conn.execute("""INSERT INTO project_device_grants
                (project_id,device_id,status,granted_by,granted_at)
                VALUES(?,?,'active',?,?) ON CONFLICT(project_id,device_id)
                DO UPDATE SET status='active',granted_by=excluded.granted_by,
                granted_at=excluded.granted_at,revoked_by=NULL,revoked_at=NULL""",
                (pid, did, uid, now))
            conn.execute("""INSERT OR IGNORE INTO project_policies
                (project_id,required_approvals,policy_version,updated_by,updated_at)
                VALUES(?,1,1,?,?)""", (pid, uid, now))
            conn.commit()
        else:
            conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    project = project_store.get_project(pid, include_checkpoints=0)
    if not project:
        raise ProjectAccessError(404, "认领项目元数据不存在")
    meta = project["meta"]
    if meta.get("status") != "active" or meta.get("owner_id") != uid:
        ok, _ = project_store.update_project(pid, status="active", owner_id=uid,
                                              owner_name=str(meta.get("creator_name") or uid))
        if not ok:
            raise ProjectAccessError(500, "认领已记录，但无法完成项目激活；请使用同一终端重试")
    _audit(uid, "project.claim", {"project_id": pid, "device_id": did,
                                   "already_processed": already_processed})
    return {"project_id": pid, "user_id": uid, "device_id": did,
            "role": "owner", "status": "active",
            "already_processed": already_processed}


def access(project_id: str, user_id: str, device_id: str) -> dict | None:
    ensure_schema()
    if not project_id or not user_id or not device_id:
        return None
    with _connection() as conn:
        row = conn.execute("""SELECT m.role,m.status membership_status,
            g.status device_status FROM project_memberships m
            JOIN project_device_grants g ON g.project_id=m.project_id
            WHERE m.project_id=? AND m.user_id=? AND g.device_id=?""",
            (project_id, user_id, device_id)).fetchone()
    if not row or row["membership_status"] != "active" or row["device_status"] != "active":
        return None
    return {"role": str(row["role"]), "membership_status": str(row["membership_status"]),
            "device_status": str(row["device_status"])}


def approval_vote_current(project_id: str, user_id: str, device_id: str,
                          voted_at: float) -> bool:
    """A vote expires when its member role or device grant is changed."""
    ensure_schema()
    if not project_id or not user_id or not device_id:
        return False
    with _connection() as conn:
        row = conn.execute("""SELECT m.role,m.status membership_status,
            m.updated_at,g.status device_status,g.granted_at
            FROM project_memberships m JOIN project_device_grants g
            ON g.project_id=m.project_id
            WHERE m.project_id=? AND m.user_id=? AND g.device_id=?""",
            (project_id, user_id, device_id)).fetchone()
    return bool(row and row["membership_status"] == "active"
                and row["device_status"] == "active"
                and row["role"] in {"owner", "admin", "member"}
                and float(row["updated_at"]) <= voted_at
                and float(row["granted_at"]) <= voted_at)


def role(project_id: str, user_id: str) -> str:
    ensure_schema()
    with _connection() as conn:
        row = conn.execute("""SELECT role FROM project_memberships WHERE project_id=?
            AND user_id=? AND status='active'""", (project_id, user_id)).fetchone()
    return str(row["role"]) if row else ""


def _require_actor_conn(conn: sqlite3.Connection, project_id: str,
                        actor: dict, allowed: set[str]) -> tuple[str, str]:
    uid = str((actor or {}).get("id") or "")
    did = str((actor or {}).get("device_id") or "")
    if not uid:
        raise ProjectAccessError(401, "需要已认证的 Hub 用户")
    row = conn.execute("""SELECT m.role,m.status membership_status,g.status device_status
        FROM project_memberships m JOIN project_device_grants g
        ON g.project_id=m.project_id WHERE m.project_id=? AND m.user_id=?
        AND g.device_id=?""", (project_id, uid, did)).fetchone()
    if not row or row["membership_status"] != "active" or row["device_status"] != "active":
        raise ProjectAccessError(403, "当前终端没有此项目的有效授权")
    if str(row["role"]) not in allowed:
        raise ProjectAccessError(403, "当前项目角色无权执行此操作")
    import project_store
    project = project_store.get_project(project_id, include_checkpoints=0)
    if not project:
        raise ProjectAccessError(404, "项目不存在")
    if str((project.get("meta") or {}).get("status") or "") != "active":
        raise ProjectAccessError(409, "归档或非活动项目为只读状态")
    return uid, did


def can_access(project_id: str, user: dict) -> bool:
    """Check both user membership and current terminal grant."""
    ensure_schema()
    pid = str(project_id or "")
    uid = str((user or {}).get("id") or "")
    did = str((user or {}).get("device_id") or "")
    if not pid or not uid:
        return False
    import project_store
    project = project_store.get_project(pid, include_checkpoints=0)
    meta = (project or {}).get("meta") or {}
    if not project:
        return False
    if meta.get("status") == "pending_claim":
        # Pending metadata is exposed only through can_view_pending_claim().
        return False
    if meta.get("status") != "active":
        return False
    if not meta.get("is_team"):
        # Personal projects retain their owner check. Managed Hub projects
        # are always team projects and therefore require both grant records.
        return str(meta.get("owner_id") or "owner") == uid
    return bool(access(pid, uid, did))


def can_view_archived(project_id: str, user: dict) -> bool:
    """Read-only visibility for an archived project and its retained history.

    This is intentionally separate from ``can_access`` so write paths keep
    their active-project requirement.
    """
    ensure_schema()
    pid = str(project_id or "")
    uid = str((user or {}).get("id") or "")
    did = str((user or {}).get("device_id") or "")
    if not pid or not uid or not did:
        return False
    import project_store
    project = project_store.get_project(pid, include_checkpoints=0)
    meta = (project or {}).get("meta") or {}
    return bool(project and meta.get("is_team")
                and meta.get("status") == "archived"
                and access(pid, uid, did))


def finalize_archive(project_id: str, *, actor: dict) -> dict:
    """Idempotently close invitations/transfers after the JSON status is archived.

    The project store and access DB are separate stores. The API first closes
    the project JSON status, then calls this repairable cleanup; repeated calls
    by the active owner device finish any interrupted cleanup.
    """
    ensure_schema()
    pid = str(project_id or "").strip()
    uid = str((actor or {}).get("id") or "")
    did = str((actor or {}).get("device_id") or "")
    import project_store
    project = project_store.get_project(pid, include_checkpoints=0)
    if not project or not (project.get("meta") or {}).get("is_team"):
        raise ProjectAccessError(404, "团队项目不存在")
    if (project.get("meta") or {}).get("status") != "archived":
        raise ProjectAccessError(409, "项目尚未归档")
    if not uid or not did:
        raise ProjectAccessError(401, "需要已认证的 Hub 用户和终端")
    with _connection() as conn:
        _begin(conn)
        try:
            row = (conn.execute("""SELECT m.role,m.status membership_status,
                g.status device_status FROM project_memberships m
                JOIN project_device_grants g ON g.project_id=m.project_id
                WHERE m.project_id=? AND m.user_id=? AND g.device_id=?""",
                (pid, uid, did)).fetchone() if did else None)
            legacy_owner = (
                not (project.get("meta") or {}).get("access_control_managed")
                and str((project.get("meta") or {}).get("owner_id") or "") == uid)
            if ((row and row["membership_status"] == "active"
                    and row["device_status"] == "active"
                    and str(row["role"]) == "owner") or legacy_owner):
                pass
            elif (not row or row["membership_status"] != "active"
                  or row["device_status"] != "active"):
                raise ProjectAccessError(403, "当前终端没有此项目的有效授权")
            else:
                raise ProjectAccessError(403, "只有项目 owner 可以归档项目")
            now = _now()
            invites = conn.execute("""UPDATE project_invites SET status='revoked'
                WHERE project_id=? AND status='issued'""", (pid,)).rowcount
            transfers = conn.execute("""UPDATE project_owner_transfers SET status='cancelled'
                WHERE project_id=? AND status='pending'""", (pid,)).rowcount
            proposals = conn.execute("""UPDATE project_governance_proposals
                SET status='cancelled',resolved_at=? WHERE project_id=?
                AND status IN ('pending','approved')""", (now, pid)).rowcount
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    if invites or transfers or proposals:
        _audit(uid, "project.archive.cleanup", {
            "project_id": pid, "invites_revoked": int(invites),
            "transfers_cancelled": int(transfers),
            "governance_proposals_cancelled": int(proposals), "at": now})
    return {"project_id": pid, "invites_revoked": int(invites),
            "transfers_cancelled": int(transfers),
            "governance_proposals_cancelled": int(proposals)}


def can_view_pending_claim(project_id: str, user: dict) -> bool:
    """Narrow exception for creator project list/detail, never for resources."""
    ensure_schema()
    pid = str(project_id or "")
    uid = str((user or {}).get("id") or "")
    did = str((user or {}).get("device_id") or "")
    if not pid or not uid or not did:
        return False
    import project_store
    project = project_store.get_project(pid, include_checkpoints=0)
    if not project or (project.get("meta") or {}).get("status") != "pending_claim":
        return False
    with _connection() as conn:
        row = conn.execute("""SELECT creator_user_id,creator_device_id,status,expires_at
            FROM project_claims WHERE project_id=?""", (pid,)).fetchone()
    return bool(row and row["status"] in ("issued", "expired", "revoked")
                and row["creator_user_id"] == uid and row["creator_device_id"] == did)


def pending_claim_expires_at(project_id: str, user: dict) -> float | None:
    if not can_view_pending_claim(project_id, user):
        return None
    with _connection() as conn:
        row = conn.execute("SELECT expires_at FROM project_claims WHERE project_id=?",
                           (project_id,)).fetchone()
    return float(row["expires_at"]) if row else None


def reissue_claim(project_id: str, *, user_id: str, device_id: str) -> str:
    ensure_schema()
    if not can_view_pending_claim(project_id, {"id": user_id, "device_id": device_id}):
        raise ProjectAccessError(404, "待认领项目不存在")
    with _connection() as conn:
        row = conn.execute("SELECT status FROM project_claims WHERE project_id=?",
                           (project_id,)).fetchone()
    if not row or row["status"] == "claimed":
        raise ProjectAccessError(409, "项目认领已完成")
    return create_claim(project_id, user_id, device_id)


def grant_device(project_id: str, device_id: str, *, actor_id: str,
                 audit: bool = True) -> None:
    ensure_schema(migrate_legacy=False)
    if not project_id or not device_id:
        return
    now = _now()
    with _connection() as conn:
        conn.execute("""INSERT INTO project_device_grants
            (project_id,device_id,status,granted_by,granted_at)
            VALUES(?,?,'active',?,?) ON CONFLICT(project_id,device_id)
            DO UPDATE SET status='active',granted_by=excluded.granted_by,
            granted_at=excluded.granted_at,revoked_by=NULL,revoked_at=NULL""",
            (project_id, device_id, actor_id, now))
    if audit:
        _audit(actor_id, "project.device.grant", {"project_id": project_id,
                                                   "device_id": device_id})


def resolve_display_code(display_code: str) -> dict:
    ensure_schema()
    import team_enrollment
    device = team_enrollment.resolve_display_code(display_code)
    if not device:
        raise ProjectAccessError(404, "终端显示码不存在或终端已停用")
    return device


def create_invite(project_id: str, *, actor: dict, terminal_display_code: str,
                  expected_user_id: str, role_name: str = "member",
                  expires_in: int | None = None) -> dict:
    ensure_schema()
    pid = str(project_id or "").strip()
    uid = str(actor.get("id") or "")
    target_uid = str(expected_user_id or "").strip()
    role_name = str(role_name or "member").strip().lower()
    if role_name not in {"admin", "member", "viewer"}:
        raise ProjectAccessError(422, "角色须为 admin、member 或 viewer")
    if role_name == "admin":
        raise ProjectAccessError(409, "不能直接邀请 admin；请先邀请为 member，再发起 admin 提权治理提案")
    target = resolve_display_code(terminal_display_code)
    device = target.get("device") or {}
    target_user = target.get("user") or {}
    target_device_id = str(device.get("device_id") or "")
    actual_uid = str(target_user.get("id") or "")
    if not actual_uid or target_uid != actual_uid:
        raise ProjectAccessError(403, "终端显示码与指定用户身份不匹配")
    ttl = _DEFAULT_INVITE_TTL if expires_in is None else int(expires_in)
    if ttl < 60 or ttl > _MAX_INVITE_TTL:
        raise ProjectAccessError(422, "邀请有效期须为 60 秒至 30 天")
    code = "VPI-" + secrets.token_urlsafe(24)
    invite_id = f"pinv_{uuid.uuid4().hex[:20]}"
    now = _now()
    expires = now + ttl
    with _connection() as conn:
        _begin(conn)
        try:
            actor_role, _ = _require_actor_conn(conn, pid, actor, {"owner", "admin"})
            if role_name == "admin" and actor_role != "owner":
                raise ProjectAccessError(403, "只有项目 owner 可以邀请 admin")
            conn.execute("""INSERT INTO project_invites
                (invite_id,project_id,expected_user_id,device_id,role,secret_hash,
                 status,created_by,created_at,expires_at)
                VALUES(?,?,?,?,?,?,'issued',?,?,?)""",
                (invite_id, pid, actual_uid, target_device_id, role_name,
                 _hash_secret(code), uid, now, expires))
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    _audit(uid, "project.invite.create", {"project_id": pid, "invite_id": invite_id,
                                           "user_id": actual_uid,
                                           "device_id": target_device_id,
                                           "role": role_name, "expires_at": expires})
    return {"invite": {"id": invite_id, "project_id": pid,
                        "user_id": actual_uid, "role": role_name,
                        "status": "issued", "created_by": uid,
                        "created_at": now, "expires_at": expires,
                        "terminal_display_code": device.get("display_code", "")},
            "invite_code": code}


def _find_invite_by_code(conn: sqlite3.Connection, code: str):
    if not code or len(code) > 256:
        return None
    digest = _hash_secret(code)
    rows = conn.execute("SELECT * FROM project_invites WHERE status='issued'").fetchall()
    return next((row for row in rows
                 if hmac.compare_digest(str(row["secret_hash"]), digest)), None)


def preview_invite(code: str, *, user_id: str, device_id: str) -> dict:
    ensure_schema()
    now = _now()
    with _connection() as conn:
        row = _find_invite_by_code(conn, str(code or ""))
        if not row:
            raise ProjectAccessError(404, "项目邀请不存在")
        if str(row["expected_user_id"]) != str(user_id or ""):
            raise ProjectAccessError(403, "此邀请指定给其他 Hub 用户")
        if str(row["device_id"]) != str(device_id or ""):
            raise ProjectAccessError(403, "此邀请指定给其他 Venus 终端")
        if float(row["expires_at"]) <= now:
            conn.execute("UPDATE project_invites SET status='expired' WHERE invite_id=?",
                         (row["invite_id"],))
            raise ProjectAccessError(410, "项目邀请已过期")
        project_id = str(row["project_id"])
        import project_store
        project = project_store.get_project(project_id, include_checkpoints=0)
        if not project or (project.get("meta") or {}).get("status") != "active":
            raise ProjectAccessError(410, "项目当前不可加入")
        meta = project["meta"]
        creator_role = role(project_id, str(row["created_by"]))
        return {"id": row["invite_id"],
                "project": {"id": project_id,
                            "name": str(meta.get("name") or meta.get("title") or project_id),
                            "owner_id": str(meta.get("owner_id") or "")},
                "role": str(row["role"]), "status": str(row["status"]),
                "expires_at": float(row["expires_at"]),
                "terminal_display_code": _device_display_code(device_id),
                "user_id": str(row["expected_user_id"]),
                "inviter_role": creator_role}


def _device_display_code(device_id: str) -> str:
    try:
        import team_enrollment
        device = team_enrollment.get_device(device_id)
        return str((device or {}).get("display_code") or "")
    except Exception:
        return ""


def accept_invite(invite_id: str, *, code: str, user_id: str,
                  device_id: str) -> dict:
    ensure_schema()
    now = _now()
    import project_store
    conn = _connect()
    try:
        _begin(conn)
        row = conn.execute("SELECT * FROM project_invites WHERE invite_id=?",
                           (str(invite_id or ""),)).fetchone()
        if not row:
            raise ProjectAccessError(404, "项目邀请不存在")
        if str(row["expected_user_id"]) != str(user_id or ""):
            raise ProjectAccessError(403, "此邀请指定给其他 Hub 用户")
        if str(row["device_id"]) != str(device_id or ""):
            raise ProjectAccessError(403, "此邀请指定给其他 Venus 终端")
        if row["status"] != "issued":
            raise ProjectAccessError(410 if row["status"] in ("expired", "revoked", "accepted")
                                     else 409, "项目邀请已处理")
        if float(row["expires_at"]) <= now:
            conn.execute("UPDATE project_invites SET status='expired' WHERE invite_id=?",
                         (invite_id,))
            conn.commit()
            raise ProjectAccessError(410, "项目邀请已过期")
        if not hmac.compare_digest(str(row["secret_hash"]), _hash_secret(str(code or ""))):
            raise ProjectAccessError(401, "项目邀请凭证无效")
        project_id = str(row["project_id"])
        project = project_store.get_project(project_id, include_checkpoints=0)
        if not project or (project.get("meta") or {}).get("status") != "active":
            raise ProjectAccessError(410, "项目当前不可加入")
        if str(row["role"]) == "admin":
            # Old databases may still contain a direct admin invitation issued
            # before governance was introduced. Revoke it instead of letting
            # accepting it bypass the proposal workflow.
            conn.execute("UPDATE project_invites SET status='revoked' WHERE invite_id=? \
                         AND status='issued'", (invite_id,))
            conn.commit()
            _audit(str(user_id), "project.invite.reject_legacy_admin", {
                "project_id": project_id, "invite_id": invite_id,
                "user_id": str(user_id)})
            raise ProjectAccessError(409, "旧版 admin 邀请已撤销；请先作为 member 加入后发起提权提案")
        now = _now()
        prior = conn.execute("""SELECT role,status FROM project_memberships
            WHERE project_id=? AND user_id=?""", (project_id, user_id)).fetchone()
        if prior and prior["status"] == "active":
            # A second terminal adds only its own grant; role changes require a
            # separately authorized admin operation.
            resulting_role = str(prior["role"])
        else:
            resulting_role = str(row["role"])
        conn.execute("""INSERT INTO project_memberships
            (project_id,user_id,role,status,created_by,created_at,updated_at)
            VALUES(?,?,?,'active',?,?,?)
            ON CONFLICT(project_id,user_id) DO UPDATE SET role=excluded.role,
            status='active',created_by=excluded.created_by,updated_at=excluded.updated_at,
            removed_by=NULL,removed_at=NULL""",
            (project_id, user_id, resulting_role, row["created_by"], now, now))
        conn.execute("""INSERT INTO project_device_grants
            (project_id,device_id,status,granted_by,granted_at)
            VALUES(?,?,'active',?,?) ON CONFLICT(project_id,device_id)
            DO UPDATE SET status='active',granted_by=excluded.granted_by,
            granted_at=excluded.granted_at,revoked_by=NULL,revoked_at=NULL""",
            (project_id, device_id, row["created_by"], now))
        conn.execute("""UPDATE project_invites SET status='accepted',accepted_by=?,
            accepted_device_id=?,accepted_at=? WHERE invite_id=?""",
            (user_id, device_id, now, invite_id))
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    _audit(str(user_id), "project.invite.accept", {"project_id": project_id,
                                                    "invite_id": invite_id,
                                                    "device_id": device_id})
    return {"project_id": project_id, "user_id": user_id,
            "device_id": device_id, "role": resulting_role,
            "status": "active"}


def list_invites(project_id: str) -> list[dict]:
    ensure_schema()
    now = _now()
    with _connection() as conn:
        rows = conn.execute("""SELECT * FROM project_invites WHERE project_id=?
            ORDER BY created_at DESC""", (project_id,)).fetchall()
        expired = [str(row["invite_id"]) for row in rows
                   if row["status"] == "issued" and float(row["expires_at"]) <= now]
        for invite_id in expired:
            conn.execute("UPDATE project_invites SET status='expired' WHERE invite_id=?",
                         (invite_id,))
    return [{"id": row["invite_id"], "project_id": row["project_id"],
             "user_id": row["expected_user_id"], "device_id": row["device_id"],
             "role": row["role"],
             "status": "expired" if row["invite_id"] in expired else row["status"],
             "created_by": row["created_by"], "created_at": row["created_at"],
             "expires_at": row["expires_at"],
             "terminal_display_code": _device_display_code(row["device_id"])}
            for row in rows]


def revoke_invite(project_id: str, invite_id: str, *, actor_id: str) -> dict:
    ensure_schema()
    if role(project_id, actor_id) not in {"owner", "admin"}:
        raise ProjectAccessError(403, "只有项目 owner 或 admin 可以撤销邀请")
    with _connection() as conn:
        row = conn.execute("SELECT status FROM project_invites WHERE invite_id=? AND project_id=?",
                           (invite_id, project_id)).fetchone()
        if not row:
            raise ProjectAccessError(404, "项目邀请不存在")
        if row["status"] == "revoked":
            return {"id": invite_id, "status": "revoked", "already_processed": True}
        if row["status"] != "issued":
            raise ProjectAccessError(409, "当前状态不能撤销该邀请")
        conn.execute("UPDATE project_invites SET status='revoked' WHERE invite_id=?",
                     (invite_id,))
    _audit(actor_id, "project.invite.revoke", {"project_id": project_id,
                                                "invite_id": invite_id})
    return {"id": invite_id, "status": "revoked"}


def list_members(project_id: str) -> list[dict]:
    ensure_schema()
    with _connection() as conn:
        rows = conn.execute("""SELECT user_id,role,status,created_by,created_at
            FROM project_memberships WHERE project_id=? ORDER BY created_at""",
            (project_id,)).fetchall()
        device_rows = conn.execute("""SELECT device_id,status FROM project_device_grants
            WHERE project_id=?""", (project_id,)).fetchall()
    try:
        import team_enrollment
        users = {str(row.get("id") or ""): row for row in team_enrollment.list_members()}
        devices = {str(row.get("id") or ""): row
                   for user in users.values() for row in user.get("devices", [])}
    except Exception:
        users, devices = {}, {}
    by_user: dict[str, list[dict]] = {}
    for grant in device_rows:
        device = devices.get(str(grant["device_id"]), {})
        owner_id = str(device.get("user_id") or "")
        if owner_id:
            by_user.setdefault(owner_id, []).append({
                "device_id": str(grant["device_id"]),
                "display_code": str(device.get("display_code") or ""),
                "name": str(device.get("name") or ""),
                "status": "active" if grant["status"] == "active"
                and device.get("status") == "active" else "revoked"})
    output = []
    for row in rows:
        uid = str(row["user_id"])
        user = users.get(uid) or {}
        output.append({"user_id": uid,
                       "display_name": str(user.get("name") or uid),
                       "role": str(row["role"]), "status": str(row["status"]),
                       "devices": by_user.get(uid, []),
                       "created_by": str(row["created_by"]),
                       "created_at": float(row["created_at"])})
    return output


def remove_member(project_id: str, user_id: str, *, actor: dict,
                  reason: str = "") -> dict:
    ensure_schema()
    actor_id = str((actor or {}).get("id") or "")
    import team_enrollment
    with _connection() as conn:
        _begin(conn)
        try:
            actor_id, _ = _require_actor_conn(conn, project_id, actor, {"owner", "admin"})
            # Read device ownership while the SQLite write lock is held. An
            # invitation acceptance cannot race this transition and leave a
            # stale grant behind for a later rejoin.
            device_ids = [str(row.get("id") or "")
                          for row in team_enrollment._load_access().get("devices", [])
                          if row.get("user_id") == user_id and row.get("id")]
            row = conn.execute("""SELECT role,status FROM project_memberships
                WHERE project_id=? AND user_id=?""", (project_id, user_id)).fetchone()
            if not row:
                raise ProjectAccessError(404, "项目成员不存在")
            if row["role"] == "owner":
                raise ProjectAccessError(409, "项目唯一 owner 不能被移除")
            if row["status"] == "removed":
                conn.commit()
                return {"project_id": project_id, "user_id": user_id,
                        "status": "removed", "already_processed": True}
            now = _now()
            conn.execute("""UPDATE project_memberships SET status='removed',updated_at=?,
                removed_by=?,removed_at=? WHERE project_id=? AND user_id=?""",
                (now, actor_id, now, project_id, user_id))
            for device_id in device_ids:
                conn.execute("""UPDATE project_device_grants SET status='revoked',
                    revoked_by=?,revoked_at=? WHERE project_id=? AND device_id=?""",
                    (actor_id, now, project_id, device_id))
            conn.execute("""UPDATE project_invites SET status='revoked'
                WHERE project_id=? AND expected_user_id=? AND status='issued'""",
                (project_id, user_id))
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    _audit(actor_id, "project.member.remove", {"project_id": project_id,
                                                "user_id": user_id,
                                                "reason": str(reason or "")[:500]})
    return {"project_id": project_id, "user_id": user_id, "status": "removed"}


def revoke_device(project_id: str, device_id: str, *, actor: dict,
                  reason: str = "") -> dict:
    ensure_schema()
    import team_enrollment
    access_rows = team_enrollment._load_access().get("devices", [])
    terminal = next((item for item in access_rows
                     if str(item.get("id") or "") == device_id), None)
    device_user_id = str((terminal or {}).get("user_id") or "")
    actor_id = str((actor or {}).get("id") or "")
    with _connection() as conn:
        _begin(conn)
        try:
            actor_id, _ = _require_actor_conn(conn, project_id, actor, {"owner", "admin"})
            row = conn.execute("""SELECT status FROM project_device_grants
                WHERE project_id=? AND device_id=?""", (project_id, device_id)).fetchone()
            if not row:
                raise ProjectAccessError(404, "项目终端授权不存在")
            if row["status"] == "revoked":
                conn.commit()
                return {"project_id": project_id, "device_id": device_id,
                        "status": "revoked", "already_processed": True}
            owner_membership = (conn.execute("""SELECT role,status FROM project_memberships
                WHERE project_id=? AND user_id=?""", (project_id, device_user_id)).fetchone()
                if device_user_id else None)
            if (owner_membership and owner_membership["role"] == "owner"
                    and owner_membership["status"] == "active"):
                owner_devices = team_enrollment.active_device_ids_by_user().get(
                    device_user_id, set())
                remaining = conn.execute("""SELECT device_id FROM project_device_grants
                    WHERE project_id=? AND status='active' AND device_id<>?""",
                    (project_id, device_id)).fetchall()
                if (device_id in owner_devices and not any(
                        str(item["device_id"]) in owner_devices for item in remaining)):
                    raise ProjectAccessError(409, "不能撤销项目 owner 的最后一台已授权终端")
            now = _now()
            conn.execute("""UPDATE project_device_grants SET status='revoked',revoked_by=?,
                revoked_at=? WHERE project_id=? AND device_id=?""",
                (actor_id, now, project_id, device_id))
            conn.execute("""UPDATE project_invites SET status='revoked'
                WHERE project_id=? AND device_id=? AND status='issued'""",
                (project_id, device_id))
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    _audit(actor_id, "project.device.revoke", {"project_id": project_id,
                                                 "device_id": device_id,
                                                 "reason": str(reason or "")[:500]})
    return {"project_id": project_id, "device_id": device_id, "status": "revoked"}


def _active_project_devices(conn: sqlite3.Connection, project_id: str,
                            excluding_device_id: str = "") -> set[str]:
    rows = conn.execute("""SELECT device_id FROM project_device_grants
        WHERE project_id=? AND status='active' AND device_id<>?""",
        (project_id, excluding_device_id)).fetchall()
    return {str(row["device_id"]) for row in rows}


def _ensure_governance_project_active(project_id: str) -> None:
    import project_store
    project = project_store.get_project(project_id, include_checkpoints=0)
    if not project or not (project.get("meta") or {}).get("is_team"):
        raise ProjectAccessError(404, "团队项目不存在")
    if str((project.get("meta") or {}).get("status") or "") != "active":
        raise ProjectAccessError(409, "归档或非活动项目不能进行治理操作")


def _policy_state_conn(conn: sqlite3.Connection, project_id: str) -> tuple[int, int]:
    row = conn.execute("SELECT required_approvals,policy_version FROM project_policies "
                       "WHERE project_id=?", (project_id,)).fetchone()
    return (int(row["required_approvals"]), int(row["policy_version"])) if row else (1, 1)


def _active_enrollment_device(user_id: str, device_id: str) -> bool:
    try:
        import team_enrollment
        device = team_enrollment.get_device(device_id)
    except Exception:
        return False
    return bool(device and device.get("status") == "active"
                and str(device.get("user_id") or "") == user_id)


def _governance_member_snapshot_conn(conn: sqlite3.Connection, project_id: str,
                                     user_id: str, device_id: str,
                                     *, roles: set[str] | None = None) -> dict | None:
    row = conn.execute("""SELECT m.role,m.status membership_status,m.updated_at,
        g.status device_status,g.granted_at FROM project_memberships m
        JOIN project_device_grants g ON g.project_id=m.project_id
        WHERE m.project_id=? AND m.user_id=? AND g.device_id=?""",
        (project_id, user_id, device_id)).fetchone()
    eligible_roles = roles or _GOVERNANCE_ELIGIBLE_ROLES
    if (not row or row["membership_status"] != "active"
            or row["device_status"] != "active"
            or str(row["role"]) not in eligible_roles
            or not _active_enrollment_device(user_id, device_id)):
        return None
    return {"role": str(row["role"]), "updated_at": float(row["updated_at"]),
            "granted_at": float(row["granted_at"])}


def _governance_proposer_current_conn(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    snapshot = _governance_member_snapshot_conn(
        conn, str(row["project_id"]), str(row["proposer_user_id"]),
        str(row["proposer_device_id"]), roles={"owner"})
    return bool(snapshot
                and snapshot["updated_at"] == float(row["proposer_updated_at"])
                and snapshot["granted_at"] == float(row["proposer_granted_at"]))


def _governance_target_current_conn(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    target_id = str(row["target_user_id"] or "")
    if not target_id:
        return True
    target = conn.execute("""SELECT role,status,updated_at FROM project_memberships
        WHERE project_id=? AND user_id=?""", (str(row["project_id"]), target_id)).fetchone()
    return bool(target and target["status"] == row["target_status"]
                and target["role"] == row["target_role"]
                and (row["target_updated_at"] is None
                     or float(target["updated_at"]) == float(row["target_updated_at"])))


def _governance_vote_current_conn(conn: sqlite3.Connection, proposal: sqlite3.Row,
                                  vote: sqlite3.Row) -> bool:
    uid = str(vote["user_id"])
    if uid == str(proposal["proposer_user_id"]):
        return False
    snapshot = _governance_member_snapshot_conn(
        conn, str(proposal["project_id"]), uid, str(vote["device_id"]))
    return bool(snapshot
                and snapshot["updated_at"] == float(vote["membership_updated_at"])
                and snapshot["granted_at"] == float(vote["grant_granted_at"]))


def _governance_votes_conn(conn: sqlite3.Connection, proposal: sqlite3.Row
                           ) -> tuple[list[dict], dict[str, sqlite3.Row]]:
    rows = conn.execute("""SELECT * FROM project_governance_votes
        WHERE proposal_id=? ORDER BY created_at,vote_id""",
        (str(proposal["proposal_id"]),)).fetchall()
    latest_current: dict[str, sqlite3.Row] = {}
    for vote in rows:
        if _governance_vote_current_conn(conn, proposal, vote):
            latest_current[str(vote["user_id"])] = vote
    output = [{"vote_id": str(vote["vote_id"]),
               "user_id": str(vote["user_id"]),
               "decision": str(vote["decision"]),
               "valid": bool(latest_current.get(str(vote["user_id"]))
                             and latest_current[str(vote["user_id"])]["vote_id"]
                             == vote["vote_id"]),
               "created_at": float(vote["created_at"])} for vote in rows]
    return output, latest_current


def _governance_payload_conn(conn: sqlite3.Connection, row: sqlite3.Row, *,
                             already_processed: bool = False) -> dict:
    votes, latest_current = _governance_votes_conn(conn, row)
    parameters = json.loads(str(row["parameters_json"] or "{}"))
    result = json.loads(str(row["result_json"] or "{}"))
    if str(row["status"]) == "applied" and isinstance(result, dict) \
            and isinstance(result.get("valid_vote_ids"), list):
        frozen_ids = {str(value) for value in result["valid_vote_ids"]}
        votes = [{**vote, "valid": str(vote["vote_id"]) in frozen_ids}
                 for vote in votes]
        approvals = sum(1 for vote in votes if vote["valid"]
                        and str(vote["decision"]) == "approve")
        rejections = sum(1 for vote in votes if vote["valid"]
                         and str(vote["decision"]) == "reject")
    else:
        approvals = sum(1 for vote in latest_current.values()
                        if str(vote["decision"]) == "approve")
        rejections = sum(1 for vote in latest_current.values()
                         if str(vote["decision"]) == "reject")
    votes = [{key: value for key, value in vote.items() if key != "vote_id"}
             for vote in votes]
    return {"id": str(row["proposal_id"]), "proposal_id": str(row["proposal_id"]),
            "project_id": str(row["project_id"]), "action": str(row["action"]),
            "target_user_id": str(row["target_user_id"] or ""),
            "status": str(row["status"]),
            "approvals": approvals, "rejections": rejections,
            "required_approvals": int(row["required_approvals"]),
            "votes": votes, "policy_version": int(row["policy_version"]),
            "parameters_sha256": str(row["parameters_sha256"]),
            "parameters": parameters,
            "proposer_user_id": str(row["proposer_user_id"]),
            "created_at": float(row["created_at"]),
            "expires_at": float(row["expires_at"]),
            "resolved_at": (float(row["resolved_at"])
                            if row["resolved_at"] is not None else None),
            "already_processed": already_processed}


def _cancel_governance_transfer_conn(conn: sqlite3.Connection, proposal_id: str) -> None:
    conn.execute("""UPDATE project_owner_transfers SET status='cancelled'
        WHERE governance_proposal_id=? AND status='pending'""", (proposal_id,))


def _refresh_governance_proposal_conn(conn: sqlite3.Connection, row: sqlite3.Row,
                                      now: float) -> str:
    status = str(row["status"])
    if status not in {"pending", "approved"}:
        return status
    new_status = ""
    _, current_version = _policy_state_conn(conn, str(row["project_id"]))
    if now >= float(row["expires_at"]):
        new_status = "expired"
    elif current_version != int(row["policy_version"]):
        new_status = "stale"
    elif not _governance_proposer_current_conn(conn, row):
        new_status = "stale"
    elif not _governance_target_current_conn(conn, row):
        new_status = "stale"
    if not new_status and status == "approved":
        _, valid_votes = _governance_votes_conn(conn, row)
        approvals = sum(1 for vote in valid_votes.values()
                        if str(vote["decision"]) == "approve")
        if approvals < int(row["required_approvals"]):
            new_status = "stale"
    if new_status:
        conn.execute("UPDATE project_governance_proposals SET status=?,resolved_at=? "
                     "WHERE proposal_id=? AND status IN ('pending','approved')",
                     (new_status, now, str(row["proposal_id"])))
        _cancel_governance_transfer_conn(conn, str(row["proposal_id"]))
        return new_status
    return status


def _bump_policy_version_conn(conn: sqlite3.Connection, project_id: str,
                              actor_id: str, now: float,
                              approvals: int | None = None) -> int:
    current_approvals, current_version = _policy_state_conn(conn, project_id)
    amount = current_approvals if approvals is None else int(approvals)
    next_version = current_version + 1
    conn.execute("""INSERT INTO project_policies
        (project_id,required_approvals,policy_version,updated_by,updated_at)
        VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
        required_approvals=excluded.required_approvals,
        policy_version=excluded.policy_version,updated_by=excluded.updated_by,
        updated_at=excluded.updated_at""",
        (project_id, amount, next_version, actor_id, now))
    return next_version


def _stale_other_governance_proposals_conn(conn: sqlite3.Connection, project_id: str,
                                           current_id: str, now: float) -> None:
    rows = conn.execute("""SELECT proposal_id FROM project_governance_proposals
        WHERE project_id=? AND proposal_id<>? AND status IN ('pending','approved')""",
        (project_id, current_id)).fetchall()
    ids = [str(row["proposal_id"]) for row in rows]
    conn.execute("""UPDATE project_governance_proposals SET status='stale',resolved_at=?
        WHERE project_id=? AND proposal_id<>? AND status IN ('pending','approved')""",
        (now, project_id, current_id))
    for proposal_id in ids:
        _cancel_governance_transfer_conn(conn, proposal_id)


def _apply_governance_proposal_conn(conn: sqlite3.Connection, row: sqlite3.Row,
                                    now: float) -> dict:
    project_id = str(row["project_id"])
    proposal_id = str(row["proposal_id"])
    actor_id = str(row["proposer_user_id"])
    parameters = json.loads(str(row["parameters_json"] or "{}"))
    action = str(row["action"])
    _, current_votes = _governance_votes_conn(conn, row)
    valid_vote_ids = sorted(str(vote["vote_id"])
                            for vote in current_votes.values()
                            if str(vote["decision"]) == "approve")
    if action == "promote_admin":
        target_id = str(row["target_user_id"])
        conn.execute("UPDATE project_memberships SET role='admin',updated_at=? "
                     "WHERE project_id=? AND user_id=? AND status='active'",
                     (now, project_id, target_id))
        version = _bump_policy_version_conn(conn, project_id, actor_id, now)
        result = {"role": "admin", "target_user_id": target_id,
                  "policy_version": version}
        status = "applied"
    elif action == "lower_approval_threshold":
        amount = int(parameters["required_approvals"])
        version = _bump_policy_version_conn(conn, project_id, actor_id, now,
                                            approvals=amount)
        result = {"required_approvals": amount, "policy_version": version}
        status = "applied"
    elif action == "owner_transfer":
        target_id = str(row["target_user_id"])
        conn.execute("UPDATE project_owner_transfers SET status='cancelled' "
                     "WHERE project_id=? AND status='pending'",
                     (project_id,))
        transfer_id = f"pown_{uuid.uuid4().hex[:20]}"
        conn.execute("""INSERT INTO project_owner_transfers
            (transfer_id,project_id,from_user_id,to_user_id,status,created_at,
             governance_proposal_id)
            VALUES(?,?,?,?, 'pending',?,?)""",
            (transfer_id, project_id, actor_id, target_id, now, proposal_id))
        result = {"transfer_id": transfer_id, "target_user_id": target_id}
        status = "approved"
    else:
        raise ProjectAccessError(409, "治理提案动作无效")
    stored_result = {**result, "approvals": len(valid_vote_ids),
                     "valid_vote_ids": valid_vote_ids}
    conn.execute("UPDATE project_governance_proposals SET status=?,resolved_at=?,result_json=? "
                 "WHERE proposal_id=? AND status='pending'",
                 (status, now, json.dumps(stored_result, ensure_ascii=False,
                                          sort_keys=True), proposal_id))
    _stale_other_governance_proposals_conn(conn, project_id, proposal_id, now)
    return {"action": action, **result, "status": status}


def _proposal_row_conn(conn: sqlite3.Connection, project_id: str,
                        proposal_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM project_governance_proposals "
                       "WHERE project_id=? AND proposal_id=?",
                       (project_id, proposal_id)).fetchone()
    if not row:
        raise ProjectAccessError(404, "治理提案不存在")
    return row


def _proposal_payload_by_id(conn: sqlite3.Connection, project_id: str,
                            proposal_id: str, now: float) -> tuple[dict, str]:
    row = _proposal_row_conn(conn, project_id, proposal_id)
    old_status = str(row["status"])
    status = _refresh_governance_proposal_conn(conn, row, now)
    row = _proposal_row_conn(conn, project_id, proposal_id)
    return _governance_payload_conn(conn, row), status if status != old_status else ""


def create_governance_proposal(project_id: str, *, action: str,
                               request_id: str, actor: dict,
                               target_user_id: str = "",
                               required_approvals: int | None = None) -> dict:
    """Persist a one-time proposal bound to the current membership and policy."""
    ensure_schema()
    pid = str(project_id or "").strip()
    uid = str((actor or {}).get("id") or "")
    did = str((actor or {}).get("device_id") or "")
    action_name = str(action or "").strip().lower()
    rid = str(request_id or "").strip()
    target_id = str(target_user_id or "").strip()
    if action_name not in _GOVERNANCE_ACTIONS:
        raise ProjectAccessError(422, "不支持的治理动作")
    if not _REQUEST_ID_RE.fullmatch(rid):
        raise ProjectAccessError(422, "request_id 格式无效")
    if action_name in {"promote_admin", "owner_transfer"} and not target_id:
        raise ProjectAccessError(422, "此治理动作需要 target_user_id")
    if action_name == "lower_approval_threshold":
        if target_id:
            raise ProjectAccessError(422, "降低审批阈值不接受 target_user_id")
        try:
            requested = int(required_approvals)
        except (TypeError, ValueError):
            raise ProjectAccessError(422, "required_approvals 必须是整数")
        if requested < 1 or requested > 50:
            raise ProjectAccessError(422, "required_approvals 须为 1 到 50")
    else:
        if required_approvals is not None:
            raise ProjectAccessError(422, "此治理动作不接受 required_approvals")
        requested = None
    if not pid or not uid or not did:
        raise ProjectAccessError(401, "需要已认证的 Hub 用户和终端")

    now = _now()
    created = False
    with _connection() as conn:
        _begin(conn)
        try:
            _ensure_governance_project_active(pid)
            actor_id, device_id = _require_actor_conn(conn, pid, actor, {"owner"})
            existing = conn.execute("""SELECT * FROM project_governance_proposals
                WHERE project_id=? AND proposer_user_id=? AND request_id=?""",
                (pid, actor_id, rid)).fetchone()
            if existing:
                parameters = json.loads(str(existing["parameters_json"] or "{}"))
                same_request = (str(existing["action"]) == action_name
                                and str(existing["target_user_id"] or "") == target_id
                                and (action_name != "lower_approval_threshold"
                                     or int(parameters.get("required_approvals", -1)) == requested))
                if not same_request:
                    raise ProjectAccessError(409, "request_id 已用于不同的治理提案")
                proposal = _governance_payload_conn(conn, existing, already_processed=True)
            else:
                pending_transfer = conn.execute("""SELECT 1 FROM project_owner_transfers
                    WHERE project_id=? AND status='pending'""", (pid,)).fetchone()
                if pending_transfer:
                    raise ProjectAccessError(409, "已有待接受的所有权转移")
                approvals, version = _policy_state_conn(conn, pid)
                target_role = target_status = ""
                target_updated_at = None
                if action_name in {"promote_admin", "owner_transfer"}:
                    target = conn.execute("""SELECT role,status,updated_at
                        FROM project_memberships WHERE project_id=? AND user_id=?""",
                        (pid, target_id)).fetchone()
                    if (not target or target["status"] != "active"
                            or target["role"] == "owner"):
                        raise ProjectAccessError(409, "目标必须是当前有效的非 owner 项目成员")
                    if action_name == "promote_admin" and target["role"] == "admin":
                        raise ProjectAccessError(409, "目标成员已经是 admin")
                    if action_name == "owner_transfer" and target["role"] not in {"admin", "member"}:
                        raise ProjectAccessError(409, "所有权只能转移给当前有效的 admin 或 member")
                    target_role = str(target["role"])
                    target_status = str(target["status"])
                    target_updated_at = float(target["updated_at"])
                if action_name == "lower_approval_threshold" and requested >= approvals:
                    raise ProjectAccessError(409, "治理提案只能降低当前审批阈值")
                parameters = ({"required_approvals": requested,
                               "previous_approvals": approvals}
                              if action_name == "lower_approval_threshold" else {})
                parameters_json = json.dumps(parameters, ensure_ascii=False,
                                             sort_keys=True, separators=(",", ":"))
                proposer = _governance_member_snapshot_conn(
                    conn, pid, actor_id, device_id, roles={"owner"})
                if not proposer:
                    raise ProjectAccessError(403, "当前终端没有有效的 owner 授权")
                proposal_id = f"pgov_{uuid.uuid4().hex[:24]}"
                required = max(2, approvals)
                conn.execute("""INSERT INTO project_governance_proposals
                    (proposal_id,project_id,request_id,action,proposer_user_id,
                     proposer_device_id,proposer_updated_at,proposer_granted_at,
                     target_user_id,target_role,target_status,target_updated_at,
                     previous_approvals,requested_approvals,parameters_json,
                     parameters_sha256,policy_version,required_approvals,status,
                     created_at,expires_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                    (proposal_id, pid, rid, action_name, actor_id, device_id,
                     proposer["updated_at"], proposer["granted_at"], target_id,
                     target_role, target_status, target_updated_at, approvals,
                     requested, parameters_json, hashlib.sha256(
                         parameters_json.encode("utf-8")).hexdigest(),
                     version, required, now, now + _GOVERNANCE_TTL))
                row = conn.execute("SELECT * FROM project_governance_proposals WHERE proposal_id=?",
                                   (proposal_id,)).fetchone()
                proposal = _governance_payload_conn(conn, row)
                created = True
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    if created:
        _audit(uid, "project.governance.proposal.create", {
            "project_id": pid, "proposal_id": proposal["id"],
            "action": action_name, "target_user_id": target_id,
            "policy_version": proposal["policy_version"],
            "parameters_sha256": proposal["parameters_sha256"],
            "required_approvals": proposal["required_approvals"]})
    return proposal


def list_governance_proposals(project_id: str) -> list[dict]:
    ensure_schema()
    now = _now()
    changes: list[tuple[str, str]] = []
    with _connection() as conn:
        _begin(conn)
        try:
            rows = conn.execute("""SELECT proposal_id,status FROM project_governance_proposals
                WHERE project_id=? ORDER BY created_at DESC,proposal_id DESC""",
                (project_id,)).fetchall()
            output = []
            for item in rows:
                payload, changed = _proposal_payload_by_id(
                    conn, project_id, str(item["proposal_id"]), now)
                output.append(payload)
                if changed:
                    changes.append((str(item["proposal_id"]), changed))
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    for proposal_id, status in changes:
        _audit("system", "project.governance.proposal.resolve", {
            "project_id": project_id, "proposal_id": proposal_id,
            "status": status})
    return output


def get_governance_proposal(project_id: str, proposal_id: str) -> dict:
    ensure_schema()
    now = _now()
    with _connection() as conn:
        _begin(conn)
        try:
            proposal, changed = _proposal_payload_by_id(conn, project_id, proposal_id, now)
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    if changed:
        _audit("system", "project.governance.proposal.resolve", {
            "project_id": project_id, "proposal_id": proposal_id,
            "status": changed})
    return proposal


def vote_governance_proposal(project_id: str, proposal_id: str, *,
                             decision: str, actor: dict) -> dict:
    ensure_schema()
    pid = str(project_id or "").strip()
    uid = str((actor or {}).get("id") or "")
    did = str((actor or {}).get("device_id") or "")
    choice = str(decision or "").strip().lower()
    if choice not in {"approve", "reject"}:
        raise ProjectAccessError(422, "decision 须为 approve 或 reject")
    if not uid or not did:
        raise ProjectAccessError(401, "需要已认证的 Hub 用户和终端")
    now = _now()
    inserted = False
    applied_action = ""
    failure_detail = ""
    with _connection() as conn:
        _begin(conn)
        try:
            _ensure_governance_project_active(pid)
            actor_id, device_id = _require_actor_conn(
                conn, pid, actor, _GOVERNANCE_ELIGIBLE_ROLES)
            row = _proposal_row_conn(conn, pid, proposal_id)
            status = _refresh_governance_proposal_conn(conn, row, now)
            row = _proposal_row_conn(conn, pid, proposal_id)
            if status in {"stale", "expired", "cancelled"}:
                failure_detail = f"治理提案已{status}"
                proposal = _governance_payload_conn(conn, row)
            elif actor_id == str(row["proposer_user_id"]):
                raise ProjectAccessError(403, "提案发起人不能为自己的提案投票")
            if not failure_detail:
                snapshot = _governance_member_snapshot_conn(
                    conn, pid, actor_id, device_id)
                if not snapshot:
                    raise ProjectAccessError(403, "当前成员角色或终端不具备治理投票资格")
                _, current = _governance_votes_conn(conn, row)
                previous = current.get(actor_id)
                if status == "applied":
                    result = json.loads(str(row["result_json"] or "{}"))
                    final_vote_ids = ({str(value) for value in result.get("valid_vote_ids", [])}
                                      if isinstance(result, dict) else set())
                    prior_final = conn.execute("""SELECT * FROM project_governance_votes
                        WHERE proposal_id=? AND user_id=? ORDER BY created_at DESC,vote_id DESC""",
                        (proposal_id, actor_id)).fetchall()
                    previous = next((vote for vote in prior_final
                                     if str(vote["vote_id"]) in final_vote_ids), None)
                if status != "pending":
                    if previous and str(previous["decision"]) == choice:
                        proposal = _governance_payload_conn(
                            conn, row, already_processed=True)
                    else:
                        raise ProjectAccessError(409, "治理提案已完成投票")
                elif previous:
                    if str(previous["decision"]) != choice:
                        raise ProjectAccessError(409, "当前有效终端已投过不同的治理票")
                    proposal = _governance_payload_conn(conn, row, already_processed=True)
                else:
                    vote_id = f"pgvt_{uuid.uuid4().hex[:24]}"
                    conn.execute("""INSERT INTO project_governance_votes
                        (vote_id,proposal_id,user_id,device_id,decision,
                         membership_updated_at,grant_granted_at,created_at)
                        VALUES(?,?,?,?,?,?,?,?)""",
                        (vote_id, proposal_id, actor_id, device_id, choice,
                         snapshot["updated_at"], snapshot["granted_at"], now))
                    inserted = True
                    refreshed = _proposal_row_conn(conn, pid, proposal_id)
                    _, current = _governance_votes_conn(conn, refreshed)
                    approvals = sum(1 for vote in current.values()
                                    if str(vote["decision"]) == "approve")
                    rejections = sum(1 for vote in current.values()
                                     if str(vote["decision"]) == "reject")
                    if rejections:
                        conn.execute("UPDATE project_governance_proposals SET status='rejected',"
                                     "resolved_at=? WHERE proposal_id=? AND status='pending'",
                                     (now, proposal_id))
                        _cancel_governance_transfer_conn(conn, proposal_id)
                    elif approvals >= int(refreshed["required_approvals"]):
                        # Recheck the complete binding and distinct current voters while
                        # holding BEGIN IMMEDIATE, immediately before applying the action.
                        state = _refresh_governance_proposal_conn(conn, refreshed, now)
                        refreshed = _proposal_row_conn(conn, pid, proposal_id)
                        _, current = _governance_votes_conn(conn, refreshed)
                        approvals = sum(1 for vote in current.values()
                                        if str(vote["decision"]) == "approve")
                        if state == "pending" and approvals >= int(refreshed["required_approvals"]):
                            result = _apply_governance_proposal_conn(conn, refreshed, now)
                            applied_action = str(result["action"])
                        elif state in {"stale", "expired"}:
                            failure_detail = f"治理提案已{state}"
                    row = _proposal_row_conn(conn, pid, proposal_id)
                    proposal = _governance_payload_conn(conn, row)
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    if failure_detail:
        _audit("system", "project.governance.proposal.resolve", {
            "project_id": pid, "proposal_id": proposal_id,
            "status": proposal["status"]})
        raise ProjectAccessError(409, failure_detail)
    if inserted:
        _audit(uid, "project.governance.proposal.vote", {
            "project_id": pid, "proposal_id": proposal_id,
            "decision": choice, "status": proposal["status"],
            "approvals": proposal["approvals"],
            "required_approvals": proposal["required_approvals"]})
    if applied_action:
        _audit(uid, "project.governance.proposal.apply", {
            "project_id": pid, "proposal_id": proposal_id,
            "action": applied_action, "target_user_id": proposal["target_user_id"],
            "parameters_sha256": proposal["parameters_sha256"],
            "status": proposal["status"]})
    return proposal


def update_member_role(project_id: str, user_id: str, role_name: str, *,
                       actor: dict) -> dict:
    ensure_schema()
    role_name = str(role_name or "").strip().lower()
    if role_name not in {"admin", "member", "viewer"}:
        raise ProjectAccessError(422, "角色须为 admin、member 或 viewer")
    now = _now()
    with _connection() as conn:
        _begin(conn)
        try:
            actor_id, _ = _require_actor_conn(conn, project_id, actor, {"owner"})
            row = conn.execute("""SELECT role,status FROM project_memberships
                WHERE project_id=? AND user_id=?""", (project_id, user_id)).fetchone()
            if not row:
                raise ProjectAccessError(404, "项目成员不存在")
            if row["role"] == "owner":
                raise ProjectAccessError(409, "项目 owner 角色须通过所有权转移修改")
            if row["status"] != "active":
                raise ProjectAccessError(409, "不能调整已移除成员的角色")
            if role_name == "admin" and str(row["role"]) != "admin":
                raise ProjectAccessError(409, "admin 提权必须通过项目治理提案")
            conn.execute("""UPDATE project_memberships SET role=?,updated_at=?
                WHERE project_id=? AND user_id=?""", (role_name, now, project_id, user_id))
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    _audit(actor_id, "project.member.role", {"project_id": project_id,
                                               "user_id": user_id,
                                               "role": role_name})
    return {"project_id": project_id, "user_id": user_id, "role": role_name,
            "status": "active"}


def request_owner_transfer(project_id: str, target_user_id: str, *,
                           actor: dict) -> dict:
    ensure_schema()
    raise ProjectAccessError(409, "所有权转移必须先通过项目治理提案")


def pending_owner_transfer(project_id: str, user_id: str) -> dict | None:
    ensure_schema()
    now = _now()
    proposal_id = ""
    resolved_status = ""
    with _connection() as conn:
        _begin(conn)
        try:
            row = conn.execute("""SELECT * FROM project_owner_transfers
                WHERE project_id=? AND to_user_id=? AND status='pending'
                ORDER BY created_at DESC LIMIT 1""", (project_id, user_id)).fetchone()
            if row:
                proposal_id = str(row["governance_proposal_id"] or "")
                if proposal_id:
                    proposal_row = conn.execute("""SELECT * FROM project_governance_proposals
                        WHERE project_id=? AND proposal_id=?""",
                        (project_id, proposal_id)).fetchone()
                    if proposal_row:
                        before = str(proposal_row["status"])
                        status = _refresh_governance_proposal_conn(conn, proposal_row, now)
                        if status != before:
                            resolved_status = status
                        if status != "approved":
                            row = None
                    else:
                        conn.execute("UPDATE project_owner_transfers SET status='cancelled' "
                                     "WHERE transfer_id=? AND status='pending'",
                                     (row["transfer_id"],))
                        row = None
                else:
                    conn.execute("UPDATE project_owner_transfers SET status='cancelled' "
                                 "WHERE transfer_id=? AND status='pending'",
                                 (row["transfer_id"],))
                    row = None
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    if resolved_status:
        _audit("system", "project.governance.proposal.resolve", {
            "project_id": project_id, "proposal_id": proposal_id,
            "status": resolved_status})
    if not row:
        return None
    return {"transfer_id": row["transfer_id"], "project_id": project_id,
            "from_user_id": row["from_user_id"], "to_user_id": row["to_user_id"],
            "status": row["status"], "created_at": row["created_at"],
            "governance_proposal_id": row["governance_proposal_id"]}


def accept_owner_transfer(project_id: str, *, user_id: str,
                          device_id: str) -> dict:
    ensure_schema()
    import project_store
    import team_enrollment
    conn = _connect()
    already_processed = False
    failure_detail = ""
    applied_proposal: dict | None = None
    final_vote_ids: list[str] = []
    try:
        _begin(conn)
        _ensure_governance_project_active(project_id)
        _require_actor_conn(conn, project_id,
                            {"id": user_id, "device_id": device_id},
                            {"owner", "admin", "member", "viewer"})
        row = conn.execute("""SELECT * FROM project_owner_transfers
            WHERE project_id=? AND to_user_id=? AND status='pending'
            ORDER BY created_at DESC LIMIT 1""", (project_id, user_id)).fetchone()
        if not row:
            row = conn.execute("""SELECT * FROM project_owner_transfers
                WHERE project_id=? AND to_user_id=? AND status='accepted'
                ORDER BY accepted_at DESC LIMIT 1""", (project_id, user_id)).fetchone()
            target = conn.execute("""SELECT role,status FROM project_memberships
                WHERE project_id=? AND user_id=?""", (project_id, user_id)).fetchone()
            if not row or not target or target["role"] != "owner" or target["status"] != "active":
                raise ProjectAccessError(404, "没有待接受的所有权转移")
            already_processed = True
        source = str(row["from_user_id"])
        if not already_processed:
            now = _now()
            proposal_id = str(row["governance_proposal_id"] or "")
            if not proposal_id:
                conn.execute("UPDATE project_owner_transfers SET status='cancelled' "
                             "WHERE transfer_id=? AND status='pending'",
                             (row["transfer_id"],))
                failure_detail = "旧版所有权转移没有治理审批，已取消"
            proposal = (conn.execute("SELECT * FROM project_governance_proposals "
                                     "WHERE project_id=? AND proposal_id=?",
                                     (project_id, proposal_id)).fetchone()
                        if proposal_id else None)
            if not failure_detail and not proposal:
                conn.execute("UPDATE project_owner_transfers SET status='cancelled' "
                             "WHERE transfer_id=? AND status='pending'",
                             (row["transfer_id"],))
                failure_detail = "所有权转移的治理提案不存在，已取消"
            if proposal and not failure_detail:
                if (str(proposal["action"]) != "owner_transfer"
                        or str(proposal["status"]) != "approved"
                        or str(proposal["target_user_id"]) != user_id
                        or str(proposal["proposer_user_id"]) != source):
                    conn.execute("UPDATE project_owner_transfers SET status='cancelled' "
                                 "WHERE transfer_id=? AND status='pending'",
                                 (row["transfer_id"],))
                    failure_detail = "所有权转移审批状态无效，已取消"
                else:
                    refreshed_status = _refresh_governance_proposal_conn(conn, proposal, now)
                    proposal = conn.execute("SELECT * FROM project_governance_proposals "
                                             "WHERE project_id=? AND proposal_id=?",
                                             (project_id, proposal_id)).fetchone()
                    if refreshed_status != "approved":
                        failure_detail = "所有权转移治理提案已失效或过期"
                    else:
                        _, valid_votes = _governance_votes_conn(conn, proposal)
                        approvals = sum(1 for vote in valid_votes.values()
                                        if str(vote["decision"]) == "approve")
                        final_vote_ids = sorted(str(vote["vote_id"])
                                                for vote in valid_votes.values()
                                                if str(vote["decision"]) == "approve")
                        if approvals < int(proposal["required_approvals"]):
                            conn.execute("UPDATE project_governance_proposals "
                                         "SET status='stale',resolved_at=? "
                                         "WHERE proposal_id=? AND status='approved'",
                                         (now, proposal_id))
                            _cancel_governance_transfer_conn(conn, proposal_id)
                            failure_detail = "审批人状态已变化，有效票数不足，所有权转移已失效"
            owner = conn.execute("""SELECT role,status FROM project_memberships
                WHERE project_id=? AND user_id=?""", (project_id, source)).fetchone()
            target = conn.execute("""SELECT role,status FROM project_memberships
                WHERE project_id=? AND user_id=?""", (project_id, user_id)).fetchone()
            if not failure_detail and (not owner or owner["role"] != "owner"
                                       or owner["status"] != "active"):
                failure_detail = "原 owner 状态已变化，转移已失效"
            if not failure_detail and (not target or target["status"] != "active"):
                failure_detail = "目标成员已离开项目，转移已失效"
            if not failure_detail:
                conn.execute("""UPDATE project_memberships SET role='admin',updated_at=?
                    WHERE project_id=? AND user_id=?""", (now, project_id, source))
                conn.execute("""UPDATE project_memberships SET role='owner',updated_at=?
                    WHERE project_id=? AND user_id=?""", (now, project_id, user_id))
                conn.execute("""UPDATE project_owner_transfers SET status='accepted',
                    accepted_by=?,accepted_at=? WHERE transfer_id=?""",
                    (user_id, now, row["transfer_id"]))
                _bump_policy_version_conn(conn, project_id, user_id, now)
                _stale_other_governance_proposals_conn(conn, project_id, proposal_id, now)
                conn.execute("UPDATE project_governance_proposals SET status='applied',"
                             "resolved_at=?,result_json=? WHERE proposal_id=? AND status='approved'",
                             (now, json.dumps({"transfer_id": row["transfer_id"],
                                               "target_user_id": user_id,
                                               "approvals": len(final_vote_ids),
                                               "valid_vote_ids": final_vote_ids}, sort_keys=True),
                              proposal_id))
                applied_proposal = {"proposal_id": proposal_id,
                                    "parameters_sha256": str(proposal["parameters_sha256"])}
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    if failure_detail:
        _audit(user_id, "project.owner_transfer.reject", {
            "project_id": project_id, "transfer_id": row["transfer_id"],
            "reason": failure_detail})
        raise ProjectAccessError(409, failure_detail)
    user_rows = {str(row.get("id") or ""): row for row in team_enrollment.list_members()}
    new_owner = user_rows.get(user_id) or {}
    ok, _ = project_store.update_project(project_id, owner_id=user_id,
                                          owner_name=str(new_owner.get("name") or user_id))
    if not ok:
        raise ProjectAccessError(500, "转移已记录，但项目资料未同步；请联系 Hub 管理员")
    if not already_processed:
        _audit(user_id, "project.owner_transfer.accept", {
            "project_id": project_id, "transfer_id": row["transfer_id"],
            "from_user_id": source, "to_user_id": user_id})
        if applied_proposal:
            _audit(user_id, "project.governance.proposal.apply", {
                "project_id": project_id,
                **applied_proposal, "action": "owner_transfer",
                "target_user_id": user_id, "status": "applied"})
    return {"project_id": project_id, "owner_id": user_id,
            "previous_owner_id": source, "role": "owner", "status": "active",
            "already_processed": already_processed}


def active_member_ids(project_id: str) -> set[str]:
    ensure_schema()
    with _connection() as conn:
        rows = conn.execute("""SELECT DISTINCT m.user_id,g.device_id
            FROM project_memberships m JOIN project_device_grants g
            ON g.project_id=m.project_id
            WHERE m.project_id=? AND m.status='active' AND g.status='active'
            AND m.role IN ('owner','admin','member')""", (project_id,)).fetchall()
    try:
        import team_enrollment
        devices_by_user = team_enrollment.active_device_ids_by_user()
        ids = {str(row["user_id"]) for row in rows
               if str(row["device_id"]) in devices_by_user.get(
                   str(row["user_id"]), set())}
    except Exception:
        return set()
    return ids


def required_approvals(project_id: str, fallback: int = 1) -> int:
    ensure_schema()
    with _connection() as conn:
        row = conn.execute("""SELECT required_approvals FROM project_policies
            WHERE project_id=?""", (project_id,)).fetchone()
    try:
        return max(1, min(50, int(row["required_approvals"]))) if row else max(1, int(fallback))
    except (ValueError, TypeError):
        return 1


def set_required_approvals(project_id: str, required: int, *, actor: dict) -> dict:
    ensure_schema()
    try:
        amount = int(required)
    except (TypeError, ValueError):
        raise ProjectAccessError(422, "required_approvals 必须是整数")
    if amount < 1 or amount > 50:
        raise ProjectAccessError(422, "required_approvals 须为 1 到 50")
    now = _now()
    with _connection() as conn:
        _begin(conn)
        try:
            actor_id, _ = _require_actor_conn(conn, project_id, actor, {"owner"})
            current_amount, _ = _policy_state_conn(conn, project_id)
            if amount < current_amount:
                raise ProjectAccessError(409, "降低审批阈值必须通过项目治理提案")
            conn.execute("""INSERT INTO project_policies
                (project_id,required_approvals,policy_version,updated_by,updated_at)
                VALUES(?,?,1,?,?) ON CONFLICT(project_id) DO UPDATE SET
                required_approvals=excluded.required_approvals,
                policy_version=project_policies.policy_version+1,
                updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                (project_id, amount, actor_id, now))
            row = conn.execute("SELECT policy_version FROM project_policies WHERE project_id=?",
                               (project_id,)).fetchone()
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    _audit(actor_id, "project.policy.update", {"project_id": project_id,
                                                 "required_approvals": amount,
                                                 "policy_version": int(row["policy_version"])})
    return {"project_id": project_id, "required_approvals": amount,
            "policy_version": int(row["policy_version"])}


def policy_version(project_id: str) -> int:
    ensure_schema()
    with _connection() as conn:
        row = conn.execute("SELECT policy_version FROM project_policies WHERE project_id=?",
                           (project_id,)).fetchone()
    return int(row["policy_version"]) if row else 1


def set_active_project(project_id: str, *, user_id: str, device_id: str) -> None:
    ensure_schema()
    if project_id and not can_access(project_id, {"id": user_id, "device_id": device_id}):
        raise ProjectAccessError(404, "项目不存在或当前终端无权访问")
    with _connection() as conn:
        if project_id:
            conn.execute("""INSERT INTO active_projects(user_id,device_id,project_id,updated_at)
                VALUES(?,?,?,?) ON CONFLICT(user_id,device_id) DO UPDATE SET
                project_id=excluded.project_id,updated_at=excluded.updated_at""",
                (user_id, device_id, project_id, _now()))
        else:
            conn.execute("DELETE FROM active_projects WHERE user_id=? AND device_id=?",
                         (user_id, device_id))


def get_active_project(user_id: str, device_id: str) -> str:
    ensure_schema()
    with _connection() as conn:
        row = conn.execute("SELECT project_id FROM active_projects WHERE user_id=? AND device_id=?",
                           (user_id, device_id)).fetchone()
    if not row:
        return ""
    project_id = str(row["project_id"])
    if not can_access(project_id, {"id": user_id, "device_id": device_id}):
        return ""
    return project_id


def invalidate_claim(project_id: str) -> None:
    ensure_schema()
    with _connection() as conn:
        conn.execute("UPDATE project_claims SET status='revoked' WHERE project_id=? AND status='issued'",
                     (project_id,))


def member_role(project_id: str, user_id: str) -> str:
    return role(project_id, user_id)
