"""Project-scoped Remote Worker call queue.

The router is included by ``llm_server`` after its team-serve middleware is
installed. It relies on ``request.state.user`` and never accepts a user or
device identity from the request body. Calls are bound to a live team Job,
project membership, the caller device, and the target device grant.

This module deliberately offers only workspace list/read/new-file creation.
Command execution and screen/keyboard/mouse control are unsupported.
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

from fastapi import APIRouter, HTTPException, Request

from data_paths import data_dir

router = APIRouter(prefix="/api/v1/worker", tags=["remote-worker"])

_CALL_ID_RE = re.compile(r"^wcall_[a-f0-9]{32}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TOOLS = {"workspace.list", "workspace.read", "workspace.write"}
_HIGH_RISK_TOOLS = {"workspace.write"}
_ACTIVE_JOB_STATUSES = {"queued", "running", "waiting_confirm"}
_MAX_TTL_SECONDS = 60 * 60
_LEASE_SECONDS = 120
_MAX_ARGS_BYTES = 256 * 1024
_MAX_RESULT_BYTES = 256 * 1024


class WorkerHubError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = str(detail)


def _db_path() -> Path:
    return data_dir() / "worker_calls.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def _connection():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def ensure_schema() -> None:
    with _connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS worker_calls (
                call_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                target_device_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                tool TEXT NOT NULL,
                args_json TEXT NOT NULL,
                args_sha256 TEXT NOT NULL,
                requested_by_user_id TEXT NOT NULL,
                requested_by_device_id TEXT NOT NULL,
                required_approvals INTEGER NOT NULL,
                expires_at REAL NOT NULL,
                status TEXT NOT NULL,
                lease_hash TEXT,
                lease_until REAL,
                created_at REAL NOT NULL,
                claimed_at REAL,
                completed_at REAL,
                result_json TEXT,
                error TEXT,
                UNIQUE(project_id, job_id, target_device_id, tool_call_id)
            );
            CREATE INDEX IF NOT EXISTS worker_calls_poll_idx
                ON worker_calls(project_id, target_device_id, status, created_at);
            CREATE TABLE IF NOT EXISTS worker_votes (
                call_id TEXT NOT NULL REFERENCES worker_calls(call_id) ON DELETE CASCADE,
                user_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
                created_at REAL NOT NULL,
                PRIMARY KEY(call_id, user_id)
            );
        """)


def _now() -> float:
    return time.time()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def args_digest(args: dict) -> str:
    try:
        raw = _canonical_json(args).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkerHubError(422, "工具参数必须是有效 JSON 对象") from exc
    if len(raw) > _MAX_ARGS_BYTES:
        raise WorkerHubError(413, "工具参数过大")
    return hashlib.sha256(raw).hexdigest()


def _safe_identifier(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not _ID_RE.fullmatch(text):
        raise WorkerHubError(422, f"{label}格式无效")
    return text


def validate_tool_args(tool: str, args: Any) -> dict:
    if tool not in _TOOLS:
        raise WorkerHubError(422, "此工具不受支持")
    if not isinstance(args, dict):
        raise WorkerHubError(422, "工具参数必须是 JSON 对象")
    if tool == "workspace.list":
        if set(args) - {"path"} or not isinstance(args.get("path", ""), str):
            raise WorkerHubError(422, "workspace.list 只接受可选 path")
        if len(args.get("path", "")) > 1024:
            raise WorkerHubError(422, "工作区路径过长")
    elif tool == "workspace.read":
        if set(args) != {"path"} or not isinstance(args.get("path"), str):
            raise WorkerHubError(422, "workspace.read 需要 path")
        if not args["path"] or len(args["path"]) > 1024:
            raise WorkerHubError(422, "工作区路径无效")
    elif tool == "workspace.write":
        if set(args) != {"path", "content"}:
            raise WorkerHubError(422, "workspace.write 需要 path 和 content")
        if not isinstance(args.get("path"), str) or not args["path"] or len(args["path"]) > 1024:
            raise WorkerHubError(422, "工作区路径无效")
        if not isinstance(args.get("content"), str):
            raise WorkerHubError(422, "workspace.write content 必须是文本")
        try:
            size = len(args["content"].encode("utf-8"))
        except UnicodeError as exc:
            raise WorkerHubError(422, "workspace.write content 编码无效") from exc
        if size > _MAX_ARGS_BYTES - 4096:
            raise WorkerHubError(413, "写入内容过大")
    return dict(args)


def _identity(user: Any) -> tuple[str, str]:
    if not isinstance(user, dict):
        raise WorkerHubError(401, "需要 Hub 设备身份")
    uid = str(user.get("id") or "").strip()
    did = str(user.get("device_id") or "").strip()
    if not uid or not did:
        raise WorkerHubError(401, "Hub 身份缺少用户或设备 ID")
    return uid, did


def _project_access(project_id: str, user: dict, *, can_submit: bool = False,
                    write: bool = False) -> tuple[str, str]:
    import project_access
    uid, did = _identity(user)
    active_access = project_access.can_access(project_id, user)
    if not active_access:
        if write and project_access.can_view_archived(project_id, user):
            raise WorkerHubError(409, "归档项目为只读状态")
        raise WorkerHubError(403, "当前 Hub 设备没有此项目访问权")
    access = project_access.access(project_id, uid, did)
    if not access:
        raise WorkerHubError(403, "当前 Hub 设备没有此项目授权")
    role = str(project_access.member_role(project_id, uid) or "")
    if can_submit and role not in {"owner", "admin", "member"}:
        raise WorkerHubError(403, "当前项目角色不能提交 Worker 调用")
    return uid, did


def _live_device(project_id: str, device_id: str) -> dict | None:
    import project_access
    import team_enrollment
    try:
        device = team_enrollment.get_device(device_id)
    except Exception:
        return None
    if not device or device.get("status") != "active":
        return None
    uid = str(device.get("user_id") or "")
    if not uid or not project_access.access(project_id, uid, device_id):
        return None
    return device


def _job_live(project_id: str, job_id: str) -> bool:
    import agent_jobs
    try:
        job = agent_jobs.get_job(job_id)
    except Exception:
        return False
    return bool(job and str(job.get("project_id") or "") == project_id
                and str(job.get("visibility") or "") == "team"
                and str(job.get("status") or "") in _ACTIVE_JOB_STATUSES)


def _get_job(project_id: str, job_id: str) -> dict | None:
    import agent_jobs
    try:
        job = agent_jobs.get_job(job_id)
    except Exception:
        return None
    # Submission is restricted to a live Job. A call already queued while
    # the Job was live may finish after that Job completes, until the call's
    # own TTL. Cancellation or failure still revokes the pending call.
    if (not job or str(job.get("project_id") or "") != project_id
            or str(job.get("visibility") or "") != "team"
            or str(job.get("status") or "") not in _ACTIVE_JOB_STATUSES | {"completed"}):
        return None
    return job


def _call_live(row: sqlite3.Row) -> bool:
    """Recheck both ends, including revocation of the submitting device."""
    requester = {"id": str(row["requested_by_user_id"]),
                 "device_id": str(row["requested_by_device_id"])}
    try:
        _project_access(str(row["project_id"]), requester,
                        can_submit=True, write=True)
    except WorkerHubError:
        return False
    job = _get_job(str(row["project_id"]), str(row["job_id"]))
    if not job:
        return False
    import project_access
    role = project_access.member_role(str(row["project_id"]), str(row["requested_by_user_id"]))
    if str(job.get("owner") or "") != str(row["requested_by_user_id"]) \
            and role not in {"owner", "admin"}:
        return False
    return bool(_live_device(str(row["project_id"]), str(row["target_device_id"])))


def _revoke_if_not_live(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    if _call_live(row):
        return False
    conn.execute("UPDATE worker_calls SET status='revoked',lease_hash=NULL,lease_until=NULL "
                 "WHERE call_id=? AND status NOT IN ('completed','failed','rejected','expired','revoked')",
                 (row["call_id"],))
    return True


def _approval_count(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    import project_access
    count = 0
    for vote in conn.execute("SELECT user_id,device_id,created_at FROM worker_votes "
                             "WHERE call_id=? AND decision='approve'", (row["call_id"],)):
        uid, did = str(vote["user_id"]), str(vote["device_id"])
        if uid == str(row["requested_by_user_id"]):
            continue
        device = _live_device(str(row["project_id"]), did)
        if (device and str(device.get("user_id") or "") == uid
                and project_access.approval_vote_current(
                    str(row["project_id"]), uid, did, float(vote["created_at"]))):
            count += 1
    return count


def _reconcile_worker_task(call_id: str) -> None:
    """Mirror a terminal call state to its dedicated Job after SQLite commits.

    The call and Job live in separate stores, so this is deliberately
    repeatable. A later read repairs a process interruption between writes.
    Normal model Jobs are never changed here.
    """
    with _connection() as conn:
        row = _get_row(conn, call_id)
        call_status = str(row["status"])
        if call_status not in {"completed", "failed", "rejected", "expired", "revoked"}:
            return
        job_id = str(row["job_id"])
        project_id = str(row["project_id"])
        tool = str(row["tool"])
        error = str(row["error"] or "")
    import agent_jobs
    job = agent_jobs.get_job(job_id)
    if not job or not job.get("worker_task"):
        return
    job_status = "completed" if call_status == "completed" else "failed"
    summary = ("Worker 调用已完成" if job_status == "completed"
               else f"Worker 调用已{call_status}")
    if job.get("status") in _ACTIVE_JOB_STATUSES:
        agent_jobs.update_job(job_id, status=job_status, finished_at=_now(),
                              result_summary=summary,
                              error="" if job_status == "completed" else (error or call_status))
        job = agent_jobs.get_job(job_id) or {}
    if job.get("status") != job_status:
        return
    agent_jobs.append_event_once(job_id, "worker_call_" + call_status, {
        "call_id": call_id, "project_id": project_id,
        "tool": tool, "status": call_status})


def cancel_project_calls(project_id: str, *, reason: str = "project_archived") -> int:
    """Idempotently revoke calls and repair their Worker Jobs after interruption."""
    pid = _safe_identifier(project_id, "项目 ID")
    ensure_schema()
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute("""SELECT call_id FROM worker_calls
            WHERE project_id=? AND status IN ('awaiting_approval','approved','leased')""",
            (pid,)).fetchall()
        pending_ids = [str(row["call_id"]) for row in pending]
        repair = conn.execute("""SELECT call_id FROM worker_calls
            WHERE project_id=? AND status='revoked' AND error=?""",
            (pid, str(reason or "project_archived")[:1000])).fetchall()
        call_ids = list(dict.fromkeys(
            pending_ids + [str(row["call_id"]) for row in repair]))
        if pending_ids:
            conn.executemany("""UPDATE worker_calls SET status='revoked',error=?,
                lease_hash=NULL,lease_until=NULL WHERE call_id=?
                AND status IN ('awaiting_approval','approved','leased')""",
                [(str(reason or "project_archived")[:1000], call_id)
                 for call_id in pending_ids])
        conn.commit()
    for call_id in call_ids:
        _reconcile_worker_task(call_id)
    return len(pending_ids)


def submit_call(*, project_id: str, job_id: str, target_device_id: str,
                tool_call_id: str, tool: str, args: dict, ttl_seconds: int,
                user: dict) -> dict:
    """Create a pending call bound to an existing active team Job."""
    pid = _safe_identifier(project_id, "项目 ID")
    jid = _safe_identifier(job_id, "Job ID")
    target = _safe_identifier(target_device_id, "目标设备 ID")
    tcid = _safe_identifier(tool_call_id, "tool_call_id")
    uid, did = _project_access(pid, user, can_submit=True, write=True)
    if not _job_live(pid, jid):
        raise WorkerHubError(409, "只能在团队 Job 执行期间提交 Worker 调用")
    job = _get_job(pid, jid)
    if not job:
        raise WorkerHubError(409, "Worker 调用必须绑定同一项目中仍有效的 team Job")
    import project_access
    role = project_access.member_role(pid, uid)
    if str(job.get("owner") or "") != uid and role not in {"owner", "admin"}:
        raise WorkerHubError(403, "成员只能为自己发起的 team Job 提交 Worker 调用")
    if not _live_device(pid, target):
        raise WorkerHubError(403, "目标设备未获此项目授权或已撤销")
    clean_args = validate_tool_args(str(tool or ""), args)
    digest = args_digest(clean_args)
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError) as exc:
        raise WorkerHubError(422, "ttl_seconds 必须是整数") from exc
    if ttl < 1 or ttl > _MAX_TTL_SECONDS:
        raise WorkerHubError(422, f"ttl_seconds 须为 1 到 {_MAX_TTL_SECONDS}")
    import project_access
    threshold = max(1, int(project_access.required_approvals(pid)))
    if tool in _HIGH_RISK_TOOLS:
        threshold = max(2, threshold)
    now = _now()
    call_id = f"wcall_{uuid.uuid4().hex}"
    try:
        ensure_schema()
        with _connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""INSERT INTO worker_calls
                (call_id,project_id,job_id,target_device_id,tool_call_id,tool,
                 args_json,args_sha256,requested_by_user_id,requested_by_device_id,
                 required_approvals,expires_at,status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'awaiting_approval',?)""",
                (call_id, pid, jid, target, tcid, tool, _canonical_json(clean_args),
                 digest, uid, did, threshold, now + ttl, now))
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise WorkerHubError(409, "相同 tool_call_id 已提交，拒绝重放") from exc
    return {"call_id": call_id, "project_id": pid, "job_id": jid,
            "target_device_id": target, "tool_call_id": tcid, "tool": tool,
            "args_sha256": digest, "required_approvals": threshold,
            "expires_at": now + ttl, "status": "awaiting_approval"}


def create_worker_task(*, project_id: str, target_device_id: str,
                       tool_call_id: str, tool: str, args: dict,
                       ttl_seconds: int, title: str, user: dict) -> dict:
    """Create a task and its first Worker call together for interactive users.

    The task is deliberately not sent to the Hub's model Job queue. It stays
    queued while project reviewers vote; the targeted, opt-in terminal polls
    the call. A failed submission cancels the newly created task.
    """
    pid = _safe_identifier(project_id, "项目 ID")
    uid, did = _project_access(pid, user, can_submit=True, write=True)
    target = _safe_identifier(target_device_id, "目标设备 ID")
    if not _live_device(pid, target):
        raise WorkerHubError(403, "目标设备未获此项目授权或已撤销")
    validate_tool_args(str(tool or ""), args)
    args_digest(args)
    import agent_jobs
    short_title = str(title or f"Worker {tool}").strip()[:100]
    job = agent_jobs.create_job(
        messages=[{"role": "user", "content": f"项目 Worker 请求：{tool}"}],
        title=short_title, project_id=pid, owner=uid,
        owner_name=str(user.get("name") or uid), owner_device_id=did,
        visibility="team", worker_task=True)
    agent_jobs.update_job(job["id"], worker_task=True,
                          result_summary="等待项目审批及目标终端领取")
    try:
        call = submit_call(project_id=pid, job_id=job["id"],
                           target_device_id=target, tool_call_id=tool_call_id,
                           tool=tool, args=args, ttl_seconds=ttl_seconds,
                           user=user)
    except Exception:
        agent_jobs.cancel_job(job["id"])
        raise
    agent_jobs.append_event(job["id"], "worker_call_requested", {
        "call_id": call["call_id"], "project_id": pid, "tool": tool,
        "target_device_id": target, "required_approvals": call["required_approvals"]})
    return {"job": agent_jobs.get_job(job["id"]), "call": call}


def _get_row(conn: sqlite3.Connection, call_id: str) -> sqlite3.Row:
    if not _CALL_ID_RE.fullmatch(str(call_id or "")):
        raise WorkerHubError(404, "Worker 调用不存在")
    row = conn.execute("SELECT * FROM worker_calls WHERE call_id=?", (call_id,)).fetchone()
    if not row:
        raise WorkerHubError(404, "Worker 调用不存在")
    return row


def _expire_if_needed(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    now = _now()
    if now >= float(row["expires_at"]):
        conn.execute("UPDATE worker_calls SET status='expired',lease_hash=NULL,lease_until=NULL "
                     "WHERE call_id=? AND status NOT IN ('completed','failed','rejected','revoked','expired')",
                     (row["call_id"],))
        return True
    if row["status"] == "leased" and row["lease_until"] is not None \
            and now >= float(row["lease_until"]):
        # Claims are one-shot. An abandoned lease is never reassigned.
        conn.execute("UPDATE worker_calls SET status='expired',lease_hash=NULL,lease_until=NULL "
                     "WHERE call_id=? AND status='leased'", (row["call_id"],))
        return True
    return False


def vote_call(call_id: str, *, decision: str, user: dict) -> dict:
    if decision not in {"approve", "reject"}:
        raise WorkerHubError(422, "decision 须为 approve 或 reject")
    ensure_schema()
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _get_row(conn, call_id)
        _identity(user)
        _project_access(str(row["project_id"]), user, write=True)
        if _expire_if_needed(conn, row):
            conn.commit()
            _reconcile_worker_task(call_id)
            raise WorkerHubError(410, "Worker 调用已过期")
        if _revoke_if_not_live(conn, row):
            conn.commit()
            _reconcile_worker_task(call_id)
            raise WorkerHubError(410, "项目、设备或 team Job 已撤销/失效")
        if row["status"] == "approved" and _approval_count(conn, row) \
                < int(row["required_approvals"]):
            conn.execute("UPDATE worker_calls SET status='awaiting_approval' WHERE call_id=?",
                         (call_id,))
            row = _get_row(conn, call_id)
        if row["status"] != "awaiting_approval":
            conn.commit()
            raise WorkerHubError(409, "Worker 调用当前不接受投票")
        uid, did = _project_access(str(row["project_id"]), user, write=True)
        import project_access
        if project_access.member_role(str(row["project_id"]), uid) not in {"owner", "admin", "member"}:
            conn.commit()
            raise WorkerHubError(403, "当前项目角色不能投票")
        if uid == str(row["requested_by_user_id"]):
            conn.commit()
            raise WorkerHubError(403, "调用发起人不能为自己的 Worker 调用投票")
        previous = conn.execute("SELECT device_id,created_at FROM worker_votes "
                                "WHERE call_id=? AND user_id=?", (call_id, uid)).fetchone()
        if previous:
            prior_did = str(previous["device_id"])
            prior_device = _live_device(str(row["project_id"]), prior_did)
            if (prior_device and str(prior_device.get("user_id") or "") == uid
                    and project_access.approval_vote_current(
                        str(row["project_id"]), uid, prior_did,
                        float(previous["created_at"]))):
                conn.commit()
                raise WorkerHubError(409, "每位项目成员只能对此调用投票一次")
            conn.execute("DELETE FROM worker_votes WHERE call_id=? AND user_id=?",
                         (call_id, uid))
        try:
            conn.execute("INSERT INTO worker_votes(call_id,user_id,device_id,decision,created_at) "
                         "VALUES(?,?,?,?,?)", (call_id, uid, did, decision, _now()))
        except sqlite3.IntegrityError as exc:
            conn.commit()
            raise WorkerHubError(409, "每位项目成员只能对此调用投票一次") from exc
        if decision == "reject":
            conn.execute("UPDATE worker_calls SET status='rejected' WHERE call_id=?", (call_id,))
        elif _approval_count(conn, row) >= int(row["required_approvals"]):
            conn.execute("UPDATE worker_calls SET status='approved' WHERE call_id=?", (call_id,))
        updated = _get_row(conn, call_id)
        approvals = _approval_count(conn, updated)
        conn.commit()
        if decision == "reject":
            _reconcile_worker_task(call_id)
        return {"call_id": call_id, "status": str(updated["status"]),
                "approvals": approvals,
                "required_approvals": int(updated["required_approvals"])}


def get_call(call_id: str, *, user: dict) -> dict:
    ensure_schema()
    with _connection() as conn:
        row = _get_row(conn, call_id)
        _project_access(str(row["project_id"]), user)
        if _expire_if_needed(conn, row):
            conn.commit()
            row = _get_row(conn, call_id)
        elif _revoke_if_not_live(conn, row):
            conn.commit()
            row = _get_row(conn, call_id)
        if row["status"] in {"awaiting_approval", "approved"}:
            ready = _approval_count(conn, row) >= int(row["required_approvals"])
            new_status = "approved" if ready else "awaiting_approval"
            if new_status != row["status"]:
                conn.execute("UPDATE worker_calls SET status=? WHERE call_id=?",
                             (new_status, call_id))
                row = _get_row(conn, call_id)
        votes = conn.execute("SELECT user_id,decision,created_at FROM worker_votes "
                             "WHERE call_id=? ORDER BY created_at", (call_id,)).fetchall()
        approvals = _approval_count(conn, row)
        out = {key: row[key] for key in (
            "call_id", "project_id", "job_id", "target_device_id", "tool_call_id",
            "tool", "args_sha256", "required_approvals", "expires_at", "status",
            "created_at", "completed_at", "error")}
        out["args"] = json.loads(row["args_json"])
        out["approvals"] = approvals
        out["votes"] = [{"user_id": v["user_id"], "decision": v["decision"],
                         "created_at": v["created_at"]} for v in votes]
        if row["result_json"]:
            out["result"] = json.loads(row["result_json"])
        _reconcile_worker_task(call_id)
        return out


def list_calls(project_id: str, *, user: dict, limit: int = 100) -> list[dict]:
    pid = _safe_identifier(project_id, "项目 ID")
    _project_access(pid, user)
    ensure_schema()
    amount = max(1, min(int(limit or 100), 200))
    with _connection() as conn:
        ids = [str(row["call_id"]) for row in conn.execute(
            "SELECT call_id FROM worker_calls WHERE project_id=? "
            "ORDER BY created_at DESC LIMIT ?", (pid, amount)).fetchall()]
    return [get_call(call_id, user=user) for call_id in ids]


def poll_call(*, project_id: str, user: dict) -> dict | None:
    pid = _safe_identifier(project_id, "项目 ID")
    uid, did = _project_access(pid, user, write=True)
    device = _live_device(pid, did)
    if not device or str(device.get("user_id") or "") != uid:
        raise WorkerHubError(403, "当前设备不再获此项目授权")
    ensure_schema()
    now = _now()
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT * FROM worker_calls WHERE project_id=? "
                            "AND target_device_id=? AND status IN ('approved','awaiting_approval') "
                            "ORDER BY CASE status WHEN 'approved' THEN 0 ELSE 1 END, "
                            "created_at LIMIT 32", (pid, did)).fetchall()
        for row in rows:
            if _expire_if_needed(conn, row):
                continue
            if _revoke_if_not_live(conn, row):
                continue
            if _approval_count(conn, row) < int(row["required_approvals"]):
                conn.execute("UPDATE worker_calls SET status='awaiting_approval' WHERE call_id=?",
                             (row["call_id"],))
                continue
            if row["status"] == "awaiting_approval":
                conn.execute("UPDATE worker_calls SET status='approved' WHERE call_id=?",
                             (row["call_id"],))
            lease = secrets.token_urlsafe(32)
            lease_until = min(float(row["expires_at"]), now + _LEASE_SECONDS)
            conn.execute("UPDATE worker_calls SET status='leased',lease_hash=?,lease_until=?,claimed_at=? "
                         "WHERE call_id=? AND status='approved'",
                         (hashlib.sha256(lease.encode("ascii")).hexdigest(), lease_until,
                          now, row["call_id"]))
            conn.commit()
            return {"call_id": row["call_id"], "project_id": pid,
                    "job_id": row["job_id"], "target_device_id": did,
                    "tool_call_id": row["tool_call_id"], "tool": row["tool"],
                    "args_sha256": row["args_sha256"], "expires_at": row["expires_at"],
                    "lease_until": lease_until, "lease_token": lease}
        conn.commit()
    return None


def _validate_lease(row: sqlite3.Row, lease_token: str, user: dict) -> tuple[str, str]:
    uid, did = _identity(user)
    if did != str(row["target_device_id"]):
        raise WorkerHubError(403, "此调用绑定到另一台 Worker 设备")
    _project_access(str(row["project_id"]), user, write=True)
    if row["status"] != "leased":
        raise WorkerHubError(409, "Worker 调用没有有效的一次性租约")
    if _now() >= float(row["expires_at"]) or row["lease_until"] is None \
            or _now() >= float(row["lease_until"]):
        raise WorkerHubError(410, "Worker 调用租约已过期")
    digest = hashlib.sha256(str(lease_token or "").encode("ascii", "ignore")).hexdigest()
    if not row["lease_hash"] or not hmac.compare_digest(digest, str(row["lease_hash"])):
        raise WorkerHubError(403, "Worker 租约凭证无效")
    return uid, did


def preflight_call(call_id: str, *, lease_token: str, user: dict) -> dict:
    ensure_schema()
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _get_row(conn, call_id)
        _validate_lease(row, lease_token, user)
        if _expire_if_needed(conn, row):
            conn.commit()
            raise WorkerHubError(410, "Worker 调用已过期")
        if _revoke_if_not_live(conn, row):
            conn.commit()
            raise WorkerHubError(410, "项目、设备或 team Job 已撤销/失效")
        if _approval_count(conn, row) < int(row["required_approvals"]):
            conn.execute("UPDATE worker_calls SET status='revoked',lease_hash=NULL,lease_until=NULL "
                         "WHERE call_id=?", (call_id,))
            conn.commit()
            raise WorkerHubError(410, "有效项目审批票数已不足，拒绝执行")
        args = json.loads(row["args_json"])
        if not hmac.compare_digest(args_digest(args), str(row["args_sha256"])):
            conn.execute("UPDATE worker_calls SET status='failed',error='args_digest_mismatch' "
                         "WHERE call_id=?", (call_id,))
            conn.commit()
            raise WorkerHubError(409, "Worker 调用参数摘要不匹配")
        conn.commit()
        return {"call_id": row["call_id"], "project_id": row["project_id"],
                "job_id": row["job_id"], "target_device_id": row["target_device_id"],
                "tool_call_id": row["tool_call_id"], "tool": row["tool"],
                "args": args, "args_sha256": row["args_sha256"],
                "expires_at": row["expires_at"], "lease_until": row["lease_until"]}


def complete_call(call_id: str, *, lease_token: str, args_sha256: str,
                  result: Any = None, error: str = "", user: dict) -> dict:
    try:
        raw_result = _canonical_json(result)
    except (TypeError, ValueError) as exc:
        raise WorkerHubError(422, "Worker 结果必须是有效 JSON") from exc
    if len(raw_result.encode("utf-8")) > _MAX_RESULT_BYTES:
        raise WorkerHubError(413, "Worker 结果过大")
    clean_error = str(error or "")[:1000]
    ensure_schema()
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _get_row(conn, call_id)
        _validate_lease(row, lease_token, user)
        if not hmac.compare_digest(str(args_sha256 or ""), str(row["args_sha256"])):
            conn.commit()
            raise WorkerHubError(409, "完成回执的参数摘要不匹配")
        if _expire_if_needed(conn, row):
            conn.commit()
            raise WorkerHubError(410, "Worker 调用或租约已过期")
        if _revoke_if_not_live(conn, row):
            conn.commit()
            raise WorkerHubError(410, "项目、设备或 team Job 已撤销/失效")
        if _approval_count(conn, row) < int(row["required_approvals"]):
            conn.execute("UPDATE worker_calls SET status='revoked',lease_hash=NULL,lease_until=NULL "
                         "WHERE call_id=?", (call_id,))
            conn.commit()
            raise WorkerHubError(410, "有效项目审批票数已不足，拒绝完成调用")
        status = "failed" if clean_error else "completed"
        conn.execute("UPDATE worker_calls SET status=?,completed_at=?,result_json=?,error=?, "
                      "lease_hash=NULL,lease_until=NULL WHERE call_id=? AND status='leased'",
                      (status, _now(), raw_result, clean_error, call_id))
        conn.commit()
    _reconcile_worker_task(call_id)
    return {"call_id": call_id, "status": status}


def _raise_http(exc: WorkerHubError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail)


async def _body(request: Request) -> dict:
    try:
        value = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    return value


@router.post("/calls")
async def api_submit_call(request: Request):
    body = await _body(request)
    try:
        return submit_call(project_id=body.get("project_id"), job_id=body.get("job_id"),
                           target_device_id=body.get("target_device_id"),
                           tool_call_id=body.get("tool_call_id"), tool=body.get("tool"),
                           args=body.get("args"), ttl_seconds=body.get("ttl_seconds", 300),
                           user=getattr(request.state, "user", None))
    except WorkerHubError as exc:
        _raise_http(exc)


@router.post("/tasks")
async def api_create_worker_task(request: Request):
    body = await _body(request)
    try:
        return create_worker_task(
            project_id=body.get("project_id"),
            target_device_id=body.get("target_device_id"),
            tool_call_id=body.get("tool_call_id") or uuid.uuid4().hex,
            tool=body.get("tool"), args=body.get("args"),
            ttl_seconds=body.get("ttl_seconds", 300),
            title=body.get("title") or "", user=getattr(request.state, "user", None))
    except WorkerHubError as exc:
        _raise_http(exc)


@router.get("/calls/{call_id}")
async def api_get_call(call_id: str, request: Request):
    try:
        return get_call(call_id, user=getattr(request.state, "user", None))
    except WorkerHubError as exc:
        _raise_http(exc)


@router.get("/calls")
async def api_list_calls(project_id: str, request: Request, limit: int = 100):
    try:
        return {"calls": list_calls(project_id,
                                    user=getattr(request.state, "user", None),
                                    limit=limit)}
    except WorkerHubError as exc:
        _raise_http(exc)


@router.post("/calls/{call_id}/votes")
async def api_vote_call(call_id: str, request: Request):
    body = await _body(request)
    try:
        return vote_call(call_id, decision=str(body.get("decision") or ""),
                         user=getattr(request.state, "user", None))
    except WorkerHubError as exc:
        _raise_http(exc)


@router.post("/poll")
async def api_poll(request: Request):
    body = await _body(request)
    try:
        call = poll_call(project_id=body.get("project_id"),
                         user=getattr(request.state, "user", None))
        return {"call": call}
    except WorkerHubError as exc:
        _raise_http(exc)


@router.post("/calls/{call_id}/preflight")
async def api_preflight(call_id: str, request: Request):
    body = await _body(request)
    try:
        return preflight_call(call_id, lease_token=str(body.get("lease_token") or ""),
                              user=getattr(request.state, "user", None))
    except WorkerHubError as exc:
        _raise_http(exc)


@router.post("/calls/{call_id}/complete")
async def api_complete(call_id: str, request: Request):
    body = await _body(request)
    try:
        return complete_call(call_id, lease_token=str(body.get("lease_token") or ""),
                             args_sha256=str(body.get("args_sha256") or ""),
                             result=body.get("result"), error=str(body.get("error") or ""),
                             user=getattr(request.state, "user", None))
    except WorkerHubError as exc:
        _raise_http(exc)
