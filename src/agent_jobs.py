"""异步 Agent 任务台：队列、持久化、状态机。

设计：
- 任务数据落在 ``.venus/jobs/``（index + 单任务 JSON）
- 单 Worker 串行消费（与 llm_server ``_agent_lock`` 对齐，MVP 不并行）
- 执行逻辑由 llm_server 注入 ``set_job_handler``，避免循环导入
"""

from __future__ import annotations

import json
import logging
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

_lock = threading.RLock()
_queue: deque[str] | None = None  # 延迟初始化，避免 import 顺序问题
_worker: AgentJobWorker | None = None
_handler: Callable[[str], None] | None = None
_cancel_events: dict[str, threading.Event] = {}


def _jobs_root() -> Path:
    return data_dir() / "jobs"


def _index_file() -> Path:
    return _jobs_root() / "index.json"


def _job_file(job_id: str) -> Path:
    return _jobs_root() / f"{job_id}.json"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_index() -> dict:
    data = _read_json(_index_file(), {"jobs": []})
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return {"jobs": []}
    return data


def _save_index(data: dict) -> None:
    _write_json(_index_file(), data)


def _new_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:12]}"


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
    jobs = data.get("jobs") or []
    if len(jobs) <= _MAX_JOBS:
        return
    drop = jobs[: len(jobs) - _MAX_JOBS]
    data["jobs"] = jobs[len(jobs) - _MAX_JOBS :]
    for row in drop:
        jid = str(row.get("id") or "")
        if jid:
            try:
                _job_file(jid).unlink(missing_ok=True)
            except OSError:
                pass


def _summary(job: dict) -> dict:
    return {
        "id": job["id"],
        "status": job["status"],
        "title": job.get("title") or "",
        "session_id": job.get("session_id"),
        "workspace": job.get("workspace") or "",
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "result_summary": job.get("result_summary") or "",
        "error": job.get("error") or "",
        "progress": job.get("progress") or {},
    }


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
) -> dict:
    """创建 queued 任务并写入磁盘。"""
    if not messages:
        raise ValueError("messages 不能为空")
    now = time.time()
    job = {
        "id": _new_job_id(),
        "status": "queued",
        "title": _title_from_messages(messages, title),
        "messages": [dict(m) for m in messages],
        "session_id": session_id,
        "workspace": workspace or "",
        "project_id": project_id or "",
        "session_version": session_version,
        "model": model,
        "temperature": temperature,
        "request_id": request_id or "",
        "task_id": "",
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "result_summary": "",
        "error": "",
        "confirm_request_id": "",
        "pending_ask": None,
        "events": [],
        "progress": {"tool_calls": 0, "last_tool": ""},
    }
    with _lock:
        _write_json(_job_file(job["id"]), job)
        idx = _load_index()
        idx["jobs"].append(_summary(job))
        _trim_index(idx)
        _save_index(idx)
    return dict(job)


def get_job(job_id: str) -> dict | None:
    with _lock:
        job = _read_json(_job_file(job_id), None)
        return dict(job) if isinstance(job, dict) else None


def list_jobs(status: str | None = None, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), _MAX_JOBS))
    with _lock:
        rows = list(_load_index().get("jobs") or [])
    rows.reverse()
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return rows[:limit]


def update_job(job_id: str, **fields: Any) -> dict | None:
    with _lock:
        job = _read_json(_job_file(job_id), None)
        if not isinstance(job, dict):
            return None
        for key, val in fields.items():
            if key == "status" and val not in JOB_STATUSES:
                continue
            job[key] = val
        _write_json(_job_file(job_id), job)
        idx = _load_index()
        for i, row in enumerate(idx.get("jobs") or []):
            if row.get("id") == job_id:
                idx["jobs"][i] = _summary(job)
                break
        else:
            idx["jobs"].append(_summary(job))
        _save_index(idx)
    return dict(job)


def append_event(job_id: str, kind: str, data: Any = None) -> None:
    """追加任务事件（供 SSE / 前端进度条）。"""
    with _lock:
        job = _read_json(_job_file(job_id), None)
        if not isinstance(job, dict):
            return
        events = job.setdefault("events", [])
        events.append({"ts": time.time(), "kind": kind, "data": data})
        if len(events) > _MAX_JOB_EVENTS:
            del events[: len(events) - _MAX_JOB_EVENTS]
        _write_json(_job_file(job_id), job)
        idx = _load_index()
        for i, row in enumerate(idx.get("jobs") or []):
            if row.get("id") == job_id:
                idx["jobs"][i] = _summary(job)
                break
        _save_index(idx)


def get_cancel_event(job_id: str) -> threading.Event | None:
    return _cancel_events.get(job_id)


def remove_from_queue(job_id: str) -> bool:
    """从内存队列移除（取消排队任务时用）。"""
    with _lock:
        if _queue is None:
            return False
        try:
            _queue.remove(job_id)
            return True
        except ValueError:
            return False


def find_job_by_task_id(task_id: str) -> dict | None:
    if not task_id:
        return None
    with _lock:
        for row in reversed(_load_index().get("jobs") or []):
            if row.get("task_id") == task_id:
                jid = str(row.get("id") or "")
                job = _read_json(_job_file(jid), None)
                return dict(job) if isinstance(job, dict) else None
    return None


def cancel_job(job_id: str) -> tuple[bool, str]:
    """取消 queued 任务，或对 running 任务发出取消信号。"""
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


def enqueue_job(job_id: str) -> None:
    _ensure_worker()
    assert _queue is not None
    with _lock:
        _queue.append(job_id)


# ---- Worker ----


class AgentJobWorker(threading.Thread):
    """单消费者：串行执行 Agent 任务。"""

    def __init__(self, job_queue: deque):
        super().__init__(daemon=True, name="agent-job-worker")
        self._q = job_queue
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            job_id = None
            with _lock:
                while self._q:
                    candidate = self._q.popleft()
                    job = _read_json(_job_file(candidate), None)
                    if isinstance(job, dict) and job.get("status") == "queued":
                        job_id = candidate
                        break
            if not job_id:
                time.sleep(0.3)
                continue
            handler = _handler
            if handler is None:
                log.error("agent job worker：未注册 handler，跳过 %s", job_id)
                update_job(job_id, status="failed", finished_at=time.time(),
                           error="任务执行器未初始化")
                continue
            try:
                handler(job_id)
            except Exception as exc:
                log.exception("agent job %s 执行异常", job_id)
                update_job(job_id, status="failed", finished_at=time.time(),
                           error=f"{type(exc).__name__}: {exc}")


def set_job_handler(fn: Callable[[str], None] | None) -> None:
    global _handler
    _handler = fn


def _ensure_worker() -> None:
    global _queue, _worker
    if _queue is None:
        _queue = deque()
    if _worker is None or not _worker.is_alive():
        _worker = AgentJobWorker(_queue)
        _worker.start()


def start_job_worker() -> None:
    """llm_server 启动时调用：确保 worker 线程存在。"""
    _ensure_worker()


def bind_cancel_event(job_id: str) -> threading.Event:
    ev = threading.Event()
    _cancel_events[job_id] = ev
    return ev


def clear_cancel_event(job_id: str) -> None:
    _cancel_events.pop(job_id, None)
