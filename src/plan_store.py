"""计划审批可视化：Todo DAG 最小存储（MVP）。

每个计划 = {id, title, steps[{id, title, status}], created_at}，
step 状态：pending / approved / rejected / done。
前端时间线渲染 + 单步通过/驳回；agent 循环后续可按 approved 步骤执行。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path


_lock = threading.Lock()


def _file() -> Path:
    from data_paths import data_file
    return data_file("plans.json")


def _load() -> dict:
    p = _file()
    try:
        import json as _json
        if p.exists():
            return {"plans": [], **_json.loads(p.read_text(encoding="utf-8") or "{}")}
    except Exception:
        pass
    return {"plans": []}


def _save(data: dict) -> None:
    p = _file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    import json
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(p)


def list_plans() -> list[dict]:
    with _lock:
        return list(_load().get("plans") or [])


def create_plan(title: str, steps: list[str]) -> dict:
    pid = uuid.uuid4().hex[:12]
    plan = {
        "id": pid,
        "title": (title or "未命名计划")[:120],
        "steps": [
            {"id": uuid.uuid4().hex[:8], "title": str(s)[:200], "status": "pending"}
            for s in (steps or [])[:50]
        ],
        "created_at": time.time(),
    }
    with _lock:
        data = _load()
        data.setdefault("plans", []).insert(0, plan)
        _save(data)
    return plan


def set_step(plan_id: str, step_id: str, status: str) -> dict | None:
    if status not in ("pending", "approved", "rejected", "done"):
        return None
    with _lock:
        data = _load()
        for plan in data.get("plans") or []:
            if plan.get("id") == plan_id:
                for st in plan.get("steps") or []:
                    if st.get("id") == step_id:
                        st["status"] = status
                        _save(data)
                        return plan
    return None


def approve_all(plan_id: str) -> dict | None:
    with _lock:
        data = _load()
        for plan in data.get("plans") or []:
            if plan.get("id") == plan_id:
                for st in plan.get("steps") or []:
                    if st.get("status") == "pending":
                        st["status"] = "approved"
                _save(data)
                return plan
    return None
