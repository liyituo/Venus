"""团队协作用户、审计与多人审批策略（M0/M1）。

个人模式：无 users.json 时行为与旧版单 token 一致。
团队 Hub：users.json + collab_config.json 启用身份与 N 票复核。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from data_paths import data_dir

OWNER_ID = "owner"
OWNER_USER = {
    "id": OWNER_ID,
    "name": "Owner",
    "role": "admin",
    "color": "#888888",
}

HIGH_RISK_TOOLS = frozenset({
    "delete_file", "git_commit", "git_push", "replace_in_file",
    "move_file", "run_shell", "start_process", "create_file",
})

_lock = threading.RLock()


class AuthError(Exception):
    """Token 无法解析为合法身份。"""


def _users_path() -> Path:
    return data_dir() / "users.json"


def _collab_config_path() -> Path:
    return data_dir() / "collab_config.json"


def _audit_path() -> Path:
    return data_dir() / "audit.jsonl"


def hash_token(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def verify_token(token: str, token_hash: str) -> bool:
    if not token or not token_hash:
        return False
    if token_hash.startswith("sha256:"):
        expected = token_hash
        actual = hash_token(token)
        return hmac.compare_digest(actual, expected)
    return hmac.compare_digest(hash_token(token), hash_token(token_hash))


def load_users() -> list[dict]:
    path = _users_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    users = data.get("users") if isinstance(data, dict) else None
    return list(users) if isinstance(users, list) else []


def save_users(users: list[dict]) -> None:
    path = _users_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"users": users}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def load_collab_config() -> dict:
    path = _collab_config_path()
    if not path.is_file():
        return {"default_required": 1, "tool_required": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"default_required": 1, "tool_required": {}}
    return data if isinstance(data, dict) else {"default_required": 1, "tool_required": {}}


def save_collab_config(cfg: dict) -> None:
    path = _collab_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def team_mode_active() -> bool:
    """显式团队模式：collab_config.team_mode=true 且成员 ≥2 才启用多人复核。"""
    cfg = load_collab_config()
    if not cfg.get("team_mode"):
        return False
    return len(load_users()) >= 2


def team_enabled() -> bool:
    """是否处于多人复核模式（同 team_mode_active，供 API 使用）。"""
    return team_mode_active()


def required_votes(tool_name: str, *, project_id: str | None = None) -> int:
    """团队项目 + Hub 协作就绪时对敏感工具要求 N 票；个人项目始终 1 票。"""
    if not team_mode_active():
        return 1
    try:
        import project_store as _ps
        if not _ps.is_team_project(project_id):
            return 1
    except Exception:
        return 1
    cfg = load_collab_config()
    tool_required = cfg.get("tool_required") or {}
    if tool_name in tool_required:
        return max(1, int(tool_required[tool_name]))
    if tool_name in HIGH_RISK_TOOLS:
        return max(1, int(cfg.get("destructive_required") or 2))
    return max(1, int(cfg.get("default_required") or 1))


def match_token(token: str) -> dict | None:
    token = (token or "").strip()
    if not token:
        return None
    for row in load_users():
        if verify_token(token, str(row.get("token_hash") or "")):
            return {
                "id": str(row.get("id") or ""),
                "name": str(row.get("name") or row.get("id") or ""),
                "role": str(row.get("role") or "member"),
                "color": str(row.get("color") or "#888888"),
            }
    return None


def resolve_request_user(
    user_token: str,
    api_token: str,
    legacy_auth_token: str,
) -> dict:
    """解析请求身份：X-User-Token / X-Api-Token / 旧 owner token。"""
    token = (user_token or api_token or "").strip()
    users = load_users()

    if token and users:
        matched = match_token(token)
        if matched:
            return matched

    if legacy_auth_token and token and hmac.compare_digest(token, legacy_auth_token):
        return dict(OWNER_USER)

    if not users and not legacy_auth_token:
        return dict(OWNER_USER)

    if not users and legacy_auth_token:
        if not token:
            raise AuthError("missing token")
        raise AuthError("invalid token")

    if not token:
        raise AuthError("missing token")
    raise AuthError("invalid token")


def public_users() -> list[dict]:
    return [
        {
            "id": u.get("id"),
            "name": u.get("name"),
            "role": u.get("role", "member"),
            "color": u.get("color", "#888888"),
        }
        for u in load_users()
    ]


def is_admin(user: dict | None) -> bool:
    if not user:
        return False
    if user.get("id") == OWNER_ID:
        return True
    return str(user.get("role") or "") == "admin"


def audit(user_id: str, action: str, detail: Any = None) -> None:
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "user": user_id or OWNER_ID,
        "action": action,
        "detail": detail or {},
    }
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_audit(*, limit: int = 100) -> list[dict]:
    path = _audit_path()
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[dict] = []
    for line in lines[-max(1, limit):]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def create_user(
    *,
    name: str,
    role: str = "member",
    color: str = "#2F8A5B",
) -> tuple[dict, str]:
    name = (name or "").strip()
    if not name:
        raise ValueError("name 不能为空")
    role = role if role in ("admin", "member") else "member"
    token = secrets.token_urlsafe(24)
    user_id = f"u_{uuid.uuid4().hex[:8]}"
    row = {
        "id": user_id,
        "name": name,
        "token_hash": hash_token(token),
        "role": role,
        "color": color,
    }
    with _lock:
        users = load_users()
        users.append(row)
        save_users(users)
    public = {"id": user_id, "name": name, "role": role, "color": color}
    return public, token


def confirm_vote_status(entry: dict) -> dict:
    required = max(1, int(entry.get("required") or 1))
    approvals = entry.get("approvals") or {}
    yes_votes = sum(1 for v in approvals.values() if v == "yes")
    no_votes = sum(1 for v in approvals.values() if v == "no")
    return {
        "required": required,
        "approvals": dict(approvals),
        "yes_votes": yes_votes,
        "no_votes": no_votes,
        "resolved": entry.get("choice") is not None,
        "choice": entry.get("choice"),
    }


def pending_for_user(user_id: str, confirm_table: dict) -> list[dict]:
    now = time.monotonic()
    out: list[dict] = []
    for request_id, entry in confirm_table.items():
        if entry.get("choice") is not None:
            continue
        if float(entry.get("expires") or 0) < now:
            continue
        approvals = entry.get("approvals") or {}
        if user_id in approvals:
            continue
        status = confirm_vote_status(entry)
        out.append({
            "request_id": request_id,
            "tool": entry.get("tool") or "",
            "task_id": entry.get("task_id") or "",
            **status,
        })
    return out
