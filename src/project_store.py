"""长任务项目存储：目标、里程碑、检查点、关联 todo。"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from data_paths import data_dir

_LOCK = threading.RLock()
_PROJECT_STATUSES = frozenset({"active", "paused", "completed", "archived"})
_MILESTONE_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})
_MAX_CHECKPOINT_NOTE = 4000
_MAX_GOAL = 2000
_MAX_TITLE = 200
_INJECT_MAX_CHARS = 1200


def _projects_root() -> Path:
    return data_dir() / "projects"


def _index_file() -> Path:
    return _projects_root() / "index.json"


def _active_file() -> Path:
    return data_dir() / "active_project.json"


def _project_dir(project_id: str) -> Path:
    return _projects_root() / project_id


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


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _slug_id(title: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", title.strip().lower())[:24].strip("-")
    return f"proj_{base or 'task'}_{uuid.uuid4().hex[:8]}"


def _load_index() -> dict:
    data = _read_json(_index_file(), {"projects": []})
    if not isinstance(data, dict):
        return {"projects": []}
    if not isinstance(data.get("projects"), list):
        data["projects"] = []
    return data


def _save_index(data: dict) -> None:
    _write_json(_index_file(), data)


def get_active_project_id() -> str:
    data = _read_json(_active_file(), {})
    if isinstance(data, dict):
        return str(data.get("project_id") or "").strip()
    return ""


def set_active_project(project_id: str) -> tuple[bool, str]:
    pid = (project_id or "").strip()
    if pid and _project_dir(pid).joinpath("meta.json").exists() is False:
        return False, f"项目不存在：{pid}"
    _write_json(_active_file(), {"project_id": pid, "updated": int(time.time())})
    return True, pid or "（无活跃项目）"


def list_projects(status: str | None = None) -> list[dict]:
    with _LOCK:
        items = list(_load_index().get("projects") or [])
    if status:
        items = [p for p in items if p.get("status") == status]
    return sorted(items, key=lambda p: p.get("updated", 0), reverse=True)


def get_project(project_id: str, *, include_checkpoints: int = 5) -> dict | None:
    pid = (project_id or "").strip()
    if not pid:
        return None
    meta = _read_json(_project_dir(pid) / "meta.json", None)
    if not isinstance(meta, dict):
        return None
    milestones = _read_json(_project_dir(pid) / "milestones.json", [])
    linked = _read_json(_project_dir(pid) / "linked_todos.json", [])
    cps = []
    cp_file = _project_dir(pid) / "checkpoints.jsonl"
    if cp_file.exists() and include_checkpoints > 0:
        try:
            lines = cp_file.read_text(encoding="utf-8").strip().splitlines()
            for line in lines[-include_checkpoints:]:
                row = json.loads(line)
                if isinstance(row, dict):
                    cps.append(row)
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "meta": meta,
        "milestones": milestones if isinstance(milestones, list) else [],
        "linked_todos": linked if isinstance(linked, list) else [],
        "checkpoints": cps,
    }


def create_project(
    title: str,
    goal: str = "",
    milestones: list[dict] | None = None,
) -> tuple[bool, str | dict]:
    title = (title or "").strip()
    goal = (goal or "").strip()[:_MAX_GOAL]
    if not title:
        return False, "title 不能为空"
    if len(title) > _MAX_TITLE:
        return False, "title 过长（≤200 字符）"
    pid = _slug_id(title)
    now = int(time.time())
    meta = {
        "id": pid,
        "title": title,
        "goal": goal,
        "status": "active",
        "created": now,
        "updated": now,
    }
    ms: list[dict] = []
    for i, raw in enumerate(milestones or [], start=1):
        if not isinstance(raw, dict):
            continue
        label = (raw.get("title") or raw.get("label") or f"里程碑 {i}").strip()[:120]
        if not label:
            continue
        ms.append({
            "id": i,
            "title": label,
            "status": "pending",
            "notes": (raw.get("notes") or "")[:500],
        })
    with _LOCK:
        _project_dir(pid).mkdir(parents=True, exist_ok=True)
        (_project_dir(pid) / "artifacts").mkdir(exist_ok=True)
        _write_json(_project_dir(pid) / "meta.json", meta)
        _write_json(_project_dir(pid) / "milestones.json", ms)
        _write_json(_project_dir(pid) / "linked_todos.json", [])
        idx = _load_index()
        summary = {k: meta[k] for k in ("id", "title", "goal", "status", "created", "updated")}
        summary["milestone_count"] = len(ms)
        idx["projects"].append(summary)
        _save_index(idx)
    return True, {"created": meta, "milestones": ms}


def update_project(project_id: str, **fields: Any) -> tuple[bool, str | dict]:
    pid = (project_id or "").strip()
    proj = get_project(pid, include_checkpoints=0)
    if proj is None:
        return False, f"项目不存在：{pid}"
    meta = proj["meta"]
    if "title" in fields and fields["title"]:
        meta["title"] = str(fields["title"]).strip()[:_MAX_TITLE]
    if "goal" in fields:
        meta["goal"] = str(fields["goal"] or "").strip()[:_MAX_GOAL]
    if "status" in fields:
        st = str(fields["status"] or "").strip()
        if st not in _PROJECT_STATUSES:
            return False, f"无效状态：{st}"
        meta["status"] = st
    meta["updated"] = int(time.time())
    with _LOCK:
        _write_json(_project_dir(pid) / "meta.json", meta)
        idx = _load_index()
        for item in idx.get("projects") or []:
            if item.get("id") == pid:
                item.update({k: meta[k] for k in ("title", "goal", "status", "updated")})
        _save_index(idx)
    return True, {"updated": meta}


def add_milestone(project_id: str, title: str, notes: str = "") -> tuple[bool, str | dict]:
    pid = (project_id or "").strip()
    proj = get_project(pid, include_checkpoints=0)
    if proj is None:
        return False, f"项目不存在：{pid}"
    label = (title or "").strip()[:120]
    if not label:
        return False, "title 不能为空"
    ms = proj["milestones"]
    mid = max((m.get("id", 0) for m in ms), default=0) + 1
    item = {"id": mid, "title": label, "status": "pending", "notes": (notes or "")[:500]}
    ms.append(item)
    with _LOCK:
        _write_json(_project_dir(pid) / "milestones.json", ms)
        update_project(pid)
    return True, {"milestone": item, "milestones": ms}


def update_milestone(
    project_id: str,
    milestone_id: int,
    status: str | None = None,
    notes: str | None = None,
) -> tuple[bool, str | dict]:
    pid = (project_id or "").strip()
    proj = get_project(pid, include_checkpoints=0)
    if proj is None:
        return False, f"项目不存在：{pid}"
    ms = proj["milestones"]
    found = None
    for m in ms:
        if int(m.get("id", -1)) == int(milestone_id):
            found = m
            break
    if found is None:
        return False, f"里程碑不存在：{milestone_id}"
    if status is not None:
        st = str(status).strip()
        if st not in _MILESTONE_STATUSES:
            return False, f"无效状态：{st}"
        found["status"] = st
    if notes is not None:
        found["notes"] = str(notes)[:500]
    with _LOCK:
        _write_json(_project_dir(pid) / "milestones.json", ms)
        update_project(pid)
    return True, {"milestone": found, "milestones": ms}


def save_checkpoint(
    project_id: str,
    summary: str,
    next_step: str = "",
    blockers: str = "",
) -> tuple[bool, str | dict]:
    pid = (project_id or "").strip()
    proj = get_project(pid, include_checkpoints=0)
    if proj is None:
        return False, f"项目不存在：{pid}"
    text = (summary or "").strip()
    if not text:
        return False, "summary 不能为空"
    row = {
        "ts": int(time.time()),
        "summary": text[:_MAX_CHECKPOINT_NOTE],
        "next_step": (next_step or "")[:500],
        "blockers": (blockers or "")[:500],
    }
    with _LOCK:
        _append_jsonl(_project_dir(pid) / "checkpoints.jsonl", row)
        update_project(pid)
    return True, {"checkpoint": row}


def link_todo(project_id: str, todo_id: int) -> tuple[bool, str | dict]:
    pid = (project_id or "").strip()
    proj = get_project(pid, include_checkpoints=0)
    if proj is None:
        return False, f"项目不存在：{pid}"
    try:
        tid = int(todo_id)
    except (TypeError, ValueError):
        return False, "todo_id 必须是整数"
    linked = proj["linked_todos"]
    if tid not in linked:
        linked.append(tid)
    with _LOCK:
        _write_json(_project_dir(pid) / "linked_todos.json", linked)
        update_project(pid)
    return True, {"project_id": pid, "linked_todos": linked}


def project_system_note(project_id: str | None = None) -> str:
    pid = (project_id or get_active_project_id()).strip()
    if not pid:
        return ""
    proj = get_project(pid, include_checkpoints=3)
    if proj is None:
        return ""
    meta = proj["meta"]
    lines = [
        f"\n\n【当前项目】{meta.get('title', pid)}（{meta.get('status', 'active')}）",
        f"目标：{meta.get('goal') or '（未填写）'}",
    ]
    pending_ms = [m for m in proj["milestones"] if m.get("status") not in ("completed", "cancelled")]
    if pending_ms:
        lines.append("未完成里程碑：")
        for m in pending_ms[:6]:
            lines.append(f"  - [{m.get('status', 'pending')}] {m.get('title', '')}")
    if proj["checkpoints"]:
        last = proj["checkpoints"][-1]
        lines.append(f"最近检查点：{last.get('summary', '')[:200]}")
        if last.get("next_step"):
            lines.append(f"下一步：{last.get('next_step', '')[:120]}")
    if proj["linked_todos"]:
        lines.append(f"关联 todo id：{', '.join(str(x) for x in proj['linked_todos'][:10])}")
    note = "\n".join(lines)
    if len(note) > _INJECT_MAX_CHARS:
        note = note[:_INJECT_MAX_CHARS] + "\n...（项目上下文过长已截断）"
    return note
