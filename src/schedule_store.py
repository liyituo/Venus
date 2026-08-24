"""定时任务存储（统一到后端，Telegram/CLI/Chat 共用）。"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any

from data_paths import data_file

_LOCK = threading.RLock()
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _path():
    return data_file("schedules.json")


def _read() -> dict:
    p = _path()
    if not p.exists():
        return {"schedules": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {"schedules": data}
        if isinstance(data, dict) and isinstance(data.get("schedules"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"schedules": []}


def _write(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def list_schedules() -> list[dict]:
    with _LOCK:
        return list(_read().get("schedules") or [])


def get_schedule(schedule_id: str) -> dict | None:
    with _LOCK:
        for s in _read().get("schedules") or []:
            if str(s.get("id")) == str(schedule_id):
                return dict(s)
    return None


def _next_id(schedules: list[dict]) -> str:
    n = 0
    for s in schedules:
        try:
            n = max(n, int(str(s.get("id", "0"))))
        except ValueError:
            pass
    return str(n + 1)


def add_schedule(*, time_hhmm: str, prompt: str, channel: str = "api",
                 chat_id: int | None = None, session_id: int | None = None,
                 enabled: bool = True) -> dict:
    if not _HHMM.match(time_hhmm.strip()):
        raise ValueError("time 格式须为 HH:MM（24 小时）")
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt 不能为空")
    with _LOCK:
        data = _read()
        schedules = data.setdefault("schedules", [])
        row = {
            "id": _next_id(schedules),
            "time": time_hhmm.strip(),
            "prompt": prompt,
            "enabled": enabled,
            "channel": channel,
            "chat_id": chat_id,
            "session_id": session_id,
            "last_run": "",
            "created_at": time.time(),
        }
        schedules.append(row)
        _write(data)
    return dict(row)


def update_schedule(schedule_id: str, **fields: Any) -> dict | None:
    with _LOCK:
        data = _read()
        for s in data.get("schedules") or []:
            if str(s.get("id")) == str(schedule_id):
                if "time" in fields and not _HHMM.match(str(fields["time"]).strip()):
                    raise ValueError("time 格式须为 HH:MM")
                for k, v in fields.items():
                    if v is not None:
                        s[k] = v
                _write(data)
                return dict(s)
    return None


def delete_schedule(schedule_id: str) -> bool:
    with _LOCK:
        data = _read()
        schedules = data.get("schedules") or []
        new_list = [s for s in schedules if str(s.get("id")) != str(schedule_id)]
        if len(new_list) == len(schedules):
            return False
        data["schedules"] = new_list
        _write(data)
    return True


def due_schedules(now_hhmm: str | None = None, today: str | None = None) -> list[dict]:
    """返回今日到点且未执行过的任务（调用方负责标记 last_run）。"""
    now_hhmm = now_hhmm or time.strftime("%H:%M")
    today = today or time.strftime("%Y-%m-%d")
    with _LOCK:
        out = []
        for s in _read().get("schedules") or []:
            if not s.get("enabled", True):
                continue
            if s.get("time") != now_hhmm:
                continue
            if (s.get("last_run") or "").startswith(today):
                continue
            out.append(dict(s))
        return out


def mark_ran(schedule_id: str, when: str | None = None) -> None:
    stamp = when or f"{time.strftime('%Y-%m-%d %H:%M')}"
    update_schedule(schedule_id, last_run=stamp)
