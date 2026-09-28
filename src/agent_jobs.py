"""异步 Agent 任务台：队列、持久化、状态机。

设计：
- 任务数据落在 ``.venus/jobs/``（index + 单任务 JSON）
- 单 Worker 串行消费（与 llm_server ``_agent_lock`` 对齐，MVP 不并行）
- 执行逻辑由 llm_server 注入 ``set_job_handler``，避免循环导入
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

from data_paths import data_dir

log = logging.getLogger("llm-backend")

JOB_STATUSES = frozenset({
    "queued", "running", "waiting_confirm", "completed", "failed", "cancelled",
})
_MAX_JOBS = 200
_MAX_JOB_EVENTS = 100
_TITLE_MAX = 120
_JOB_ID_RE = re.compile(r"^job_[a-f0-9]{12}$")
_ACTIVE_STATUSES = frozenset({"queued", "running", "waiting_confirm"})
_MAX_ACTIVE_JOBS = 100
_MAX_ACTIVE_PER_USER = 20
_MAX_ACTIVE_PER_PROJECT = 50
_INTERRUPTED_STATUSES = frozenset({"running", "waiting_confirm"})

_lock = threading.RLock()
_queue: deque[str] | None = None  # 延迟初始化，避免 import 顺序问题
_queued_ids: set[str] = set()
_inflight_ids: set[str] = set()
_queue_wakeup = threading.Event()
_last_project_key = ""
_last_owner_by_project: dict[str, str] = {}
_worker: AgentJobWorker | None = None
_handler: Callable[[str], None] | None = None
_cancel_events: dict[str, threading.Event] = {}
_hub_process_lock_handle = None


class JobQueueLimitError(Exception):
    """Raised when Hub queue or persisted-job capacity has been reached."""


class JobIdempotencyConflict(Exception):
    """Raised when a request_id is reused for a different create request."""


def _jobs_root() -> Path:
    return data_dir() / "jobs"


def _index_file() -> Path:
    return _jobs_root() / "index.json"


def _job_file(job_id: str) -> Path:
    jid = str(job_id or "")
    if not _JOB_ID_RE.fullmatch(jid):
        raise ValueError("job_id 格式无效")
    return _jobs_root() / f"{jid}.json"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _acquire_single_hub_process_lock() -> None:
    """Keep the JSON-backed Job queue to one Hub process until SQLite leases exist."""
    global _hub_process_lock_handle
    if _hub_process_lock_handle is not None:
        return
    lock_path = _jobs_root().parent / "hub-process.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as exc:
        handle.close()
        raise RuntimeError(
            "检测到另一个 Hub 进程正在使用 JSON Job 队列；当前版本只支持单进程 Hub"
        ) from exc
    _hub_process_lock_handle = handle


def _load_index() -> dict:
    data = _read_json(_index_file(), {"jobs": []})
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return {"jobs": []}
    return data


def _save_index(data: dict) -> None:
    _write_json(_index_file(), data)


def _new_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:12]}"


def new_job_id() -> str:
    """Public ID allocator for integrations that must prepare a job workspace first."""
    return _new_job_id()


def _title_from_messages(messages: list[dict], title: str | None) -> str:
    if title and title.strip():
        return title.strip()[:_TITLE_MAX]
    for m in reversed(messages):
        if m.get("role") == "user":
            text = re.sub(r"\s+", " ", str(m.get("content") or "")).strip()
            if text:
                return text[:_TITLE_MAX]
    return "未命名任务"


def _trim_index(data: dict) -> None:
    """Prune only disposable terminal history; active and Worker Jobs survive."""
    jobs = list(data.get("jobs") or [])
    while len(jobs) > _MAX_JOBS:
        candidate = next((row for row in jobs
                          if row.get("status") not in _ACTIVE_STATUSES
                          and not row.get("worker_task")
                          and not row.get("change_id")), None)
        if candidate is None:
            break
        jobs.remove(candidate)
        jid = str(candidate.get("id") or "")
        if _JOB_ID_RE.fullmatch(jid):
            try:
                _job_file(jid).unlink(missing_ok=True)
            except OSError:
                # Leave the index record when its backing file could not be
                # removed; capacity checks will then reject a new Job safely.
                jobs.append(candidate)
                break
    data["jobs"] = jobs


def _summary(job: dict) -> dict:
    return {
        "id": job["id"],
        "status": job["status"],
        "title": job.get("title") or "",
        "owner": job.get("owner") or "owner",
        "owner_name": job.get("owner_name") or "Owner",
        "visibility": job.get("visibility") or "private",
        "project_id": job.get("project_id") or "",
        "change_id": job.get("change_id") or "",
        "worker_task": bool(job.get("worker_task")),
        "task_id": job.get("task_id") or "",
        "session_id": job.get("session_id"),
        "workspace": job.get("workspace") or "",
        "request_id": job.get("request_id") or "",
        "schedule_id": job.get("schedule_id") or "",
        "channel": job.get("channel") or "",
        "chat_id": job.get("chat_id"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "result_summary": job.get("result_summary") or "",
        "error": job.get("error") or "",
        "progress": job.get("progress") or {},
    }


def _normalize_event_sequences(job: dict) -> bool:
    """Backfill monotonically increasing sequence numbers for older Jobs."""
    events = job.setdefault("events", [])
    if not isinstance(events, list):
        events = []
        job["events"] = events
    try:
        sequence = max(0, int(job.get("event_seq") or 0))
    except (TypeError, ValueError):
        sequence = 0
    changed = False
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            event_sequence = int(event.get("seq") or 0)
        except (TypeError, ValueError):
            event_sequence = 0
        if event_sequence <= 0:
            sequence += 1
            event["seq"] = sequence
            changed = True
        else:
            sequence = max(sequence, event_sequence)
    if job.get("event_seq") != sequence:
        job["event_seq"] = sequence
        changed = True
    return changed


def _append_event_to_job(job: dict, kind: str, data: Any = None) -> dict:
    _normalize_event_sequences(job)
    sequence = int(job.get("event_seq") or 0) + 1
    job["event_seq"] = sequence
    event = {"seq": sequence, "ts": time.time(), "kind": kind, "data": data}
    events = job.setdefault("events", [])
    events.append(event)
    if len(events) > _MAX_JOB_EVENTS:
        del events[:len(events) - _MAX_JOB_EVENTS]
    return event


def _scan_jobs_locked() -> list[dict]:
    """Read the authoritative per-Job files, including records missed by index writes."""
    root = _jobs_root()
    if not root.is_dir():
        return []
    jobs = []
    for path in root.glob("job_*.json"):
        jid = path.stem
        if not _JOB_ID_RE.fullmatch(jid):
            continue
        job = _read_json(path, None)
        if isinstance(job, dict) and job.get("id") == jid:
            jobs.append(job)
    def _created_order(job: dict) -> tuple[float, str]:
        try:
            created = float(job.get("created_at") or 0)
        except (TypeError, ValueError):
            created = 0.0
        return created, job["id"]

    jobs.sort(key=_created_order)
    return jobs


def _sync_index_locked(jobs: list[dict] | None = None) -> dict:
    rows = jobs if jobs is not None else _scan_jobs_locked()
    index = {"jobs": [_summary(job) for job in rows]}
    _save_index(index)
    return index


def _ensure_storage_room_locked() -> None:
    jobs = _scan_jobs_locked()
    while len(jobs) >= _MAX_JOBS:
        candidate = next((job for job in jobs
                          if job.get("status") not in _ACTIVE_STATUSES
                          and not job.get("worker_task")
                          and not job.get("change_id")), None)
        if candidate is None:
            raise JobQueueLimitError("任务存储已满，当前任务记录均处于保留状态")
        try:
            _job_file(str(candidate.get("id") or "")).unlink(missing_ok=True)
        except OSError as exc:
            raise JobQueueLimitError("任务存储已满，无法清理旧的已结束任务") from exc
        jobs.remove(candidate)
    _sync_index_locked(jobs)


def _active_jobs_locked() -> list[dict]:
    return [job for job in _scan_jobs_locked()
            if job.get("status") in _ACTIVE_STATUSES and not job.get("worker_task")]


def ensure_queue_capacity(*, owner: str, project_id: str = "",
                          exclude_job_id: str = "", include_new: bool = True) -> None:
    """Enforce bounded queued/running Agent work by Hub, owner, and project."""
    uid = str(owner or "owner")
    pid = str(project_id or "")
    with _lock:
        active = [job for job in _active_jobs_locked()
                  if job.get("id") != exclude_job_id]
        add = 1 if include_new else 0
        user_count = sum(1 for job in active if str(job.get("owner") or "owner") == uid)
        project_count = sum(1 for job in active if str(job.get("project_id") or "") == pid)
        if len(active) + add > _MAX_ACTIVE_JOBS:
            raise JobQueueLimitError("Hub 排队任务已满，请稍后重试")
        if user_count + add > _MAX_ACTIVE_PER_USER:
            raise JobQueueLimitError("当前用户未结束任务已达上限")
        if pid and project_count + add > _MAX_ACTIVE_PER_PROJECT:
            raise JobQueueLimitError("当前项目未结束任务已达上限")


def ensure_storage_capacity() -> None:
    """Prune eligible terminal history or reject before allocating workspaces."""
    with _lock:
        _ensure_storage_room_locked()


def find_idempotent_job(*, owner: str, project_id: str = "", request_id: str,
                        request_fingerprint: str = "") -> dict | None:
    """Find a Job by its request scope and reject key reuse with a new payload."""
    rid = str(request_id or "")
    if not rid:
        return None
    uid = str(owner or "owner")
    pid = str(project_id or "")
    with _lock:
        for job in reversed(_scan_jobs_locked()):
            if (job.get("worker_task")
                    or str(job.get("request_id") or "") != rid
                    or str(job.get("owner") or "owner") != uid
                    or str(job.get("project_id") or "") != pid):
                continue
            existing_fingerprint = str(job.get("_request_fingerprint") or "")
            if (request_fingerprint and existing_fingerprint
                    and request_fingerprint != existing_fingerprint):
                raise JobIdempotencyConflict("request_id 已用于不同的任务请求")
            return dict(job)
    return None


def create_job(
    *,
    messages: list[dict],
    title: str | None = None,
    session_id: int | None = None,
    workspace: str | None = None,
    project_id: str | None = None,
    session_version: int | None = None,
    model: str | None = None,
    temperature: float = 0.7,
    request_id: str | None = None,
    owner: str = "owner",
    owner_name: str = "Owner",
    owner_device_id: str = "",
    visibility: str = "private",
    job_id: str | None = None,
    request_fingerprint: str = "",
    worker_task: bool = False,
    schedule_id: str = "",
    channel: str = "",
    chat_id: int | None = None,
) -> dict:
    """创建 queued 任务并写入磁盘。"""
    if not messages:
        raise ValueError("messages 不能为空")
    now = time.time()
    jid = str(job_id or _new_job_id())
    if not _JOB_ID_RE.fullmatch(jid):
        raise ValueError("job_id 格式无效")
    if request_id and len(str(request_id)) > 100:
        raise ValueError("request_id 最长为 100 个字符")
    job = {
        "id": jid,
        "status": "queued",
        "title": _title_from_messages(messages, title),
        "messages": [dict(m) for m in messages],
        "session_id": session_id,
        "workspace": workspace or "",
        "project_id": project_id or "",
        "owner": str(owner or "owner"),
        "owner_name": str(owner_name or owner or "Owner"),
        # Internal execution binding, removed by get_job() before API output.
        "_owner_device_id": str(owner_device_id or ""),
        "visibility": visibility if visibility in ("private", "team") else "private",
        "worker_task": bool(worker_task),
        "change_id": "",
        "session_version": session_version,
        "model": model,
        "temperature": temperature,
        "request_id": request_id or "",
        "_request_fingerprint": str(request_fingerprint or ""),
        "schedule_id": str(schedule_id or ""),
        "channel": str(channel or ""),
        "chat_id": chat_id,
        "task_id": "",
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "result_summary": "",
        "error": "",
        "confirm_request_id": "",
        "pending_ask": None,
        "events": [],
        "event_seq": 0,
        "progress": {"tool_calls": 0, "last_tool": ""},
    }
    with _lock:
        if request_id:
            existing = find_idempotent_job(
                owner=job["owner"], project_id=job["project_id"],
                request_id=str(request_id), request_fingerprint=request_fingerprint)
            if existing is not None:
                existing.pop("_owner_device_id", None)
                existing.pop("_request_fingerprint", None)
                return existing
        if _job_file(job["id"]).exists():
            raise ValueError("job_id 已存在")
        _ensure_storage_room_locked()
        _write_json(_job_file(job["id"]), job)
        idx = _load_index()
        idx["jobs"].append(_summary(job))
        _trim_index(idx)
        _save_index(idx)
    return dict(job)


def get_job(job_id: str) -> dict | None:
    job = get_job_internal(job_id)
    if job is not None:
        job.pop("_owner_device_id", None)
        job.pop("_request_fingerprint", None)
    return job


def get_job_internal(job_id: str) -> dict | None:
    """Read full persisted job state for trusted worker code only."""
    if not _JOB_ID_RE.fullmatch(str(job_id or "")):
        return None
    with _lock:
        job = _read_json(_job_file(job_id), None)
        if not isinstance(job, dict):
            return None
        if _normalize_event_sequences(job):
            _write_json(_job_file(job_id), job)
        return dict(job)


def list_jobs(status: str | None = None, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), _MAX_JOBS))
    with _lock:
        rows = list(_load_index().get("jobs") or [])
    rows.reverse()
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return rows[:limit]


def update_job(job_id: str, **fields: Any) -> dict | None:
    if not _JOB_ID_RE.fullmatch(str(job_id or "")):
        return None
    status_transition = None
    with _lock:
        job = _read_json(_job_file(job_id), None)
        if not isinstance(job, dict):
            return None
        previous_status = job.get("status")
        _normalize_event_sequences(job)
        requested_status = fields.get("status")
        if (previous_status in {"completed", "failed", "cancelled"}
                and requested_status in JOB_STATUSES
                and requested_status != previous_status):
            # Terminal states are durable: late workers cannot resurrect a
            # cancelled or interrupted Job after a restart or cancellation race.
            return dict(job)
        for key, val in fields.items():
            if key == "status" and val not in JOB_STATUSES:
                continue
            job[key] = val
        if job.get("status") != previous_status:
            status_transition = (previous_status, job.get("status"))
            _append_event_to_job(job, "status", {
                "from": previous_status, "status": job.get("status"),
                "actor_id": job.get("owner") or "owner"})
        _write_json(_job_file(job_id), job)
        idx = _load_index()
        for i, row in enumerate(idx.get("jobs") or []):
            if row.get("id") == job_id:
                idx["jobs"][i] = _summary(job)
                break
        else:
            idx["jobs"].append(_summary(job))
        _save_index(idx)
    if status_transition:
        try:
            import team_collab
            team_collab.audit(str(job.get("owner") or "owner"), "job.status", {
                "job_id": job_id, "project_id": job.get("project_id") or "",
                "from": status_transition[0], "to": status_transition[1]})
        except Exception:
            pass
    return dict(job)


def append_event(job_id: str, kind: str, data: Any = None) -> None:
    """追加任务事件（供 SSE / 前端进度条）。"""
    if not _JOB_ID_RE.fullmatch(str(job_id or "")):
        return
    with _lock:
        job = _read_json(_job_file(job_id), None)
        if not isinstance(job, dict):
            return
        _append_event_to_job(job, kind, data)
        _write_json(_job_file(job_id), job)
        idx = _load_index()
        for i, row in enumerate(idx.get("jobs") or []):
            if row.get("id") == job_id:
                idx["jobs"][i] = _summary(job)
                break
        _save_index(idx)


def append_event_once(job_id: str, kind: str, data: Any = None) -> bool:
    """Append an event only if the same kind and payload are not already stored.

    Cross-store repair paths use this after updating the Job itself. If the
    process stops between the status write and event write, a retry can finish
    the Job record without duplicating its terminal event.
    """
    if not _JOB_ID_RE.fullmatch(str(job_id or "")):
        return False
    with _lock:
        job = _read_json(_job_file(job_id), None)
        if not isinstance(job, dict):
            return False
        _normalize_event_sequences(job)
        events = job.setdefault("events", [])
        if any(event.get("kind") == kind and event.get("data") == data
               for event in events if isinstance(event, dict)):
            return False
        _append_event_to_job(job, kind, data)
        _write_json(_job_file(job_id), job)
        idx = _load_index()
        for i, row in enumerate(idx.get("jobs") or []):
            if row.get("id") == job_id:
                idx["jobs"][i] = _summary(job)
                break
        _save_index(idx)
        return True


def get_cancel_event(job_id: str) -> threading.Event | None:
    return _cancel_events.get(job_id)


def remove_from_queue(job_id: str) -> bool:
    """从内存队列移除（取消排队任务时用）。"""
    with _lock:
        removed = False
        if _queue is not None:
            try:
                _queue.remove(job_id)
                removed = True
            except ValueError:
                pass
        if job_id in _queued_ids:
            _queued_ids.discard(job_id)
            removed = True
        _queue_wakeup.set()
        return removed


def find_job_by_task_id(task_id: str) -> dict | None:
    if not task_id:
        return None
    with _lock:
        for row in reversed(_load_index().get("jobs") or []):
            if row.get("task_id") == task_id:
                jid = str(row.get("id") or "")
                if not _JOB_ID_RE.fullmatch(jid):
                    continue
                job = _read_json(_job_file(jid), None)
                return dict(job) if isinstance(job, dict) else None
    return None


def cancel_job(job_id: str) -> tuple[bool, str]:
    """取消 queued 任务，或对 running 任务发出取消信号。"""
    if not _JOB_ID_RE.fullmatch(str(job_id or "")):
        return False, "任务不存在"
    with _lock:
        job = _read_json(_job_file(job_id), None)
        if not isinstance(job, dict):
            return False, "任务不存在"
        status = job.get("status")
        if status == "queued":
            remove_from_queue(job_id)
            update_job(job_id, status="cancelled", finished_at=time.time(),
                       error="用户取消（排队中）")
            return True, "已取消排队任务"
        if status in ("completed", "failed", "cancelled"):
            return False, f"任务已结束（{status}）"
        ev = _cancel_events.get(job_id)
        if ev is not None:
            ev.set()
            update_job(job_id, error="用户请求取消")
            return True, "已发送取消信号"
        return False, f"无法取消（status={status}）"


def enqueue_job(job_id: str) -> bool:
    """Queue a Job once; persisted Worker Jobs are intentionally excluded."""
    if not _JOB_ID_RE.fullmatch(str(job_id or "")):
        return False
    job = get_job_internal(job_id)
    if not job or job.get("status") != "queued" or job.get("worker_task"):
        return False
    try:
        ensure_queue_capacity(owner=str(job.get("owner") or "owner"),
                              project_id=str(job.get("project_id") or ""),
                              include_new=False)
    except JobQueueLimitError as exc:
        update_job(job_id, status="failed", finished_at=time.time(), error=str(exc))
        append_event_once(job_id, "queue_rejected", {"detail": str(exc)})
        return False
    _ensure_worker()
    assert _queue is not None
    with _lock:
        current = _read_json(_job_file(job_id), None)
        if not isinstance(current, dict):
            return False
        if current.get("status") != "queued":
            # A newly started worker may have recovered and begun this Job
            # between _ensure_worker() and this enqueue call.
            return True
        if job.get("worker_task") or job_id in _queued_ids or job_id in _inflight_ids:
            return True
        _queue.append(job_id)
        _queued_ids.add(job_id)
        _queue_wakeup.set()
        return True


def _add_queued_id_locked(job_id: str) -> bool:
    if _queue is None or job_id in _queued_ids or job_id in _inflight_ids:
        return False
    _queue.append(job_id)
    _queued_ids.add(job_id)
    return True


def _fair_choice_locked() -> str | None:
    """Choose FIFO work by round-robin project, then owner within a project."""
    global _last_project_key
    if _queue is None:
        return None
    jobs_by_id: dict[str, dict] = {}
    stale = []
    for jid in list(_queue):
        job = _read_json(_job_file(jid), None)
        if (not isinstance(job, dict) or job.get("status") != "queued"
                or job.get("worker_task") or jid in _inflight_ids):
            stale.append(jid)
            continue
        jobs_by_id[jid] = job
    for jid in stale:
        try:
            _queue.remove(jid)
        except ValueError:
            pass
        _queued_ids.discard(jid)
    if not jobs_by_id:
        return None

    by_project: dict[str, dict[str, list[tuple[float, str]]]] = {}
    for jid, job in jobs_by_id.items():
        project = str(job.get("project_id") or "")
        owner = str(job.get("owner") or "owner")
        by_project.setdefault(project, {}).setdefault(owner, []).append(
            (float(job.get("created_at") or 0), jid))
    for owners in by_project.values():
        for rows in owners.values():
            rows.sort(key=lambda item: (item[0], item[1]))

    projects = sorted(by_project)
    project = next((key for key in projects if key > _last_project_key), projects[0])
    owners = sorted(by_project[project])
    last_owner = _last_owner_by_project.get(project, "")
    owner = next((key for key in owners if key > last_owner), owners[0])
    jid = by_project[project][owner][0][1]
    _last_project_key = project
    _last_owner_by_project[project] = owner
    try:
        _queue.remove(jid)
    except ValueError:
        return None
    _queued_ids.discard(jid)
    _inflight_ids.add(jid)
    return jid


def _recover_persisted_jobs() -> int:
    """Rebuild the in-memory queue and fail unsafe in-flight work after restart.

    Only Agent Jobs are recovered. Worker Jobs remain owned by worker_hub's
    durable call/reconciliation state and must never reach the model handler.
    """
    global _queue
    queued_count = 0
    interrupted: list[str] = []
    with _lock:
        if _queue is None:
            _queue = deque()
        jobs = _scan_jobs_locked()
        _sync_index_locked(jobs)
        for job in jobs:
            jid = str(job.get("id") or "")
            if job.get("worker_task"):
                continue
            if job.get("status") == "queued":
                if _add_queued_id_locked(jid):
                    queued_count += 1
            elif job.get("status") in _INTERRUPTED_STATUSES:
                interrupted.append(jid)

    for jid in interrupted:
        job = get_job_internal(jid) or {}
        prior_status = str(job.get("status") or "running")
        error = "Hub 重启时任务仍在执行；为避免重复文件或命令副作用，未自动重跑"
        update_job(jid, status="failed", finished_at=time.time(), error=error,
                   pending_ask=None, confirm_request_id="")
        append_event_once(jid, "interrupted", {"previous_status": prior_status,
                                                "reason": "hub_restart"})
    if queued_count:
        _queue_wakeup.set()
    return queued_count


# ---- Worker ----


class AgentJobWorker(threading.Thread):
    """单消费者：串行执行 Agent 任务。"""

    def __init__(self, job_queue: deque):
        super().__init__(daemon=True, name="agent-job-worker")
        self._q = job_queue
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            with _lock:
                job_id = _fair_choice_locked()
            if not job_id:
                _queue_wakeup.wait(0.5)
                _queue_wakeup.clear()
                continue
            try:
                handler = _handler
                if handler is None:
                    log.error("agent job worker：未注册 handler，终止 %s", job_id)
                    update_job(job_id, status="failed", finished_at=time.time(),
                               error="任务执行器未初始化")
                    continue
                try:
                    handler(job_id)
                except Exception as exc:
                    log.exception("agent job %s 执行异常", job_id)
                    update_job(job_id, status="failed", finished_at=time.time(),
                               error=f"{type(exc).__name__}: {exc}")
                    clear_cancel_event(job_id)
            finally:
                with _lock:
                    _inflight_ids.discard(job_id)


def set_job_handler(fn: Callable[[str], None] | None) -> None:
    global _handler
    _handler = fn


def _ensure_worker() -> None:
    global _queue, _worker
    with _lock:
        if _queue is None:
            _queue = deque()
        if _worker is not None and _worker.is_alive():
            return
    _acquire_single_hub_process_lock()
    _recover_persisted_jobs()
    with _lock:
        if _worker is None or not _worker.is_alive():
            assert _queue is not None
            _worker = AgentJobWorker(_queue)
            _worker.start()


def start_job_worker() -> None:
    """llm_server 启动时调用：确保 worker 线程存在。"""
    _ensure_worker()


def bind_cancel_event(job_id: str) -> threading.Event:
    with _lock:
        ev = _cancel_events.get(job_id)
        if ev is None:
            ev = threading.Event()
            _cancel_events[job_id] = ev
        return ev


def clear_cancel_event(job_id: str) -> None:
    with _lock:
        _cancel_events.pop(job_id, None)
