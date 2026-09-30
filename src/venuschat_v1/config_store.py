"""VenusChat V1 — local connection config (chat_config.json at repo root)."""

from __future__ import annotations

import json
import os
import sys
import threading
import hashlib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = _REPO_ROOT / "chat_config.json"
SECRET_KEYS = ("api_token", "daemon_token", "api_key", "vision_api_key")


def load_config() -> dict:
    defaults = {
        "llm_base": "http://127.0.0.1:8001",
        "personal_llm_base": "",
        "team_hub_local_base": "",
        "daemon_base": "http://127.0.0.1:8000",
        "api_token": "",
        "daemon_token": "",
        "workspace": "",
        "confirm_mode": "auto",
        "sandbox_mode": "workspace",
        "memory_enabled": True,
        "llm_memory_extract": False,
        "tool_router": False,
        "tool_router_url": "http://127.0.0.1:11434",
        "tool_router_model": "gemma3:1b",
        "team_connections": [],
    }
    if not CONFIG_PATH.exists():
        return defaults
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg = {**defaults, **raw}
        try:
            from secure_store import load as ss_load
            for key in SECRET_KEYS:
                if (cfg.get(key) or "") == "__secure__":
                    cfg[key] = ss_load(key)
        except Exception:
            pass
        return cfg
    except Exception:
        return defaults


def save_local_config(updates: dict) -> None:
    """Merge updates into chat_config.json (non-secret fields only)."""
    # load_config() expands secure placeholders to plaintext for API calls.
    # Read the raw JSON here so editing an unrelated setting cannot write
    # those decrypted values back to disk.
    cfg: dict = {}
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("配置文件格式错误：顶层必须是对象")
    if any(cfg.get(key) not in (None, "", "__secure__") for key in SECRET_KEYS):
        from secure_store import migrate_from_plaintext
        cfg = migrate_from_plaintext(cfg, SECRET_KEYS)
    # Bind an existing personal API credential to the endpoint it belonged to
    # before a caller changes llm_base. An unbound legacy token must never be
    # carried to the newly selected Hub.
    if ("llm_base" in updates and
            str(updates.get("llm_base") or "").rstrip("/")
            != str(cfg.get("llm_base") or "").rstrip("/")
            and not cfg.get("api_token_origin")):
        old_base = str(cfg.get("llm_base") or "").rstrip("/")
        old_token = str(cfg.get("api_token") or "")
        if old_token == "__secure__":
            try:
                from secure_store import load as ss_load
                old_token = ss_load("api_token") or ""
            except Exception:
                old_token = ""
        if old_base and old_token:
            cfg["api_token_origin"] = old_base
    for key, val in updates.items():
        if key in SECRET_KEYS:
            continue
        cfg[key] = val
    tmp = CONFIG_PATH.with_name(
        f"{CONFIG_PATH.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    if sys.platform != "win32":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    tmp.replace(CONFIG_PATH)


def llm_base() -> str:
    return str(load_config().get("llm_base") or "http://127.0.0.1:8001").rstrip("/")


def personal_backend_for_team(origin: str, previous: str = "",
                              config: dict | None = None) -> str:
    """Resolve a personal endpoint while excluding every configured Hub URL."""
    cfg = config if isinstance(config, dict) else load_config()
    hub_origins = {
        str(row.get("origin") or "").rstrip("/")
        for row in (cfg.get("team_connections") or [])
        if isinstance(row, dict) and str(row.get("origin") or "").strip()
    }
    hub_origins.add(str(origin or "").rstrip("/"))
    local_hub = str(cfg.get("team_hub_local_base") or "").rstrip("/")
    if local_hub:
        hub_origins.add(local_hub)
    for candidate in (previous, cfg.get("personal_llm_base"), cfg.get("llm_base"),
                      "http://127.0.0.1:8001"):
        value = str(candidate or "").rstrip("/")
        if value and value not in hub_origins:
            return value
    fallback = (""
                if local_hub == "http://127.0.0.1:8001"
                else "http://127.0.0.1:8001")
    return fallback


def project_preference_key(origin: str) -> str:
    """Scope a local project preference to this Hub user and device."""
    origin = str(origin or "").rstrip("/")
    config = load_config()
    for row in reversed(config.get("team_connections") or []):
        if not isinstance(row, dict) or str(row.get("origin") or "").rstrip("/") != origin:
            continue
        team_id = str(row.get("team_id") or "")
        user_id = str(row.get("user_id") or "")
        device_id = str(row.get("device_id") or "")
        if team_id or user_id or device_id:
            return "\n".join((origin, team_id, user_id, device_id))
        break
    return origin


def active_project_for_origin(origin: str) -> str:
    """Return the locally selected project for this user/device at one Hub."""
    key = project_preference_key(origin)
    rows = load_config().get("active_project_by_hub") or {}
    if not isinstance(rows, dict):
        return ""
    return str(rows.get(key) or "")


def save_active_project_for_origin(origin: str, project_id: str) -> None:
    """Persist the current project on this client, scoped to its Hub origin."""
    key = project_preference_key(origin)
    if not key:
        return
    rows = load_config().get("active_project_by_hub") or {}
    rows = dict(rows) if isinstance(rows, dict) else {}
    if project_id:
        rows[key] = str(project_id)
    else:
        rows.pop(key, None)
    save_local_config({"active_project_by_hub": rows})


def token_for_base(base_url: str) -> str:
    cfg = load_config()
    base = base_url.rstrip("/")
    llm = str(cfg.get("llm_base") or "").rstrip("/")
    daemon = str(cfg.get("daemon_base") or "").rstrip("/")
    if llm and base == llm:
        key = "api_token"
        bound = str(cfg.get("api_token_origin") or "").rstrip("/")
        if bound and bound != base:
            return ""
        if not bound:
            try:
                from urllib.parse import urlparse
                host = (urlparse(base).hostname or "").casefold()
            except Exception:
                host = ""
            # Preserve legacy personal remotes whose URL was already stored
            # in the user's config, while never sending an unbound generic
            # token to the *.ts.net origins required for team Serve Hubs.
            if host.endswith(".ts.net"):
                return ""
    elif daemon and base == daemon:
        key = "daemon_token"
        bound = str(cfg.get("daemon_token_origin") or "").rstrip("/")
        if bound and bound != base:
            return ""
        if not bound:
            try:
                from urllib.parse import urlparse
                host = (urlparse(base).hostname or "").casefold()
            except Exception:
                host = ""
            if host.endswith(".ts.net"):
                return ""
    else:
        # Never guess which saved personal credential belongs to an unknown
        # endpoint. In particular, changing to a new Hub must not leak the
        # token configured for the previous server.
        return ""
    val = str(cfg.get(key) or "").strip()
    if val == "__secure__":
        try:
            from secure_store import load as ss_load
            return ss_load(key) or ""
        except Exception:
            return ""
    return val


def normalize_team_origin(value: str, *, allow_loopback_http: bool = True) -> str:
    from direct_connection import normalize_origin
    # Preserve explicit historical origins for stored credentials; bare hosts
    # use the same 8001 default as the new connection dialog.
    return normalize_origin(value, default_port=None if "://" in value else 8001)


def _team_secret_key(origin: str, team_id: str) -> str:
    source = f"{normalize_team_origin(origin)}\n{str(team_id or '').strip()}"
    return "team_device_" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]


def _team_claim_key(origin: str, application_id: str) -> str:
    source = f"{normalize_team_origin(origin)}\n{str(application_id or '').strip()}"
    return "team_claim_" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]


def team_connections() -> list[dict]:
    rows = load_config().get("team_connections") or []
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            origin = normalize_team_origin(str(row.get("origin") or ""))
        except ValueError:
            continue
        out.append({**row, "origin": origin})
    return out


def team_connection(origin: str, team_id: str | None = None) -> dict | None:
    target = normalize_team_origin(origin)
    for row in reversed(team_connections()):
        if row.get("origin") == target and (team_id is None or row.get("team_id") == team_id):
            return row
    return None


def team_token_for_base(base_url: str) -> tuple[bool, str]:
    """Return (is_known_team_origin, token), including a tokenless known hub."""
    try:
        origin = normalize_team_origin(base_url)
    except ValueError:
        return False, ""
    matches = [row for row in team_connections() if row.get("origin") == origin]
    if not matches:
        return False, ""
    row = matches[-1]
    try:
        from secure_store import load as ss_load
        return True, ss_load(_team_secret_key(origin, str(row.get("team_id") or ""))) or ""
    except Exception:
        return True, ""


def team_token_for_connection(origin: str, team_id: str) -> str:
    try:
        normalized = normalize_team_origin(origin)
        from secure_store import load as ss_load
        return ss_load(_team_secret_key(normalized, str(team_id or ""))) or ""
    except Exception:
        return ""


def save_team_connection(record: dict, *, device_token: str | None = None) -> dict:
    origin = normalize_team_origin(str(record.get("origin") or ""))
    team_id = str(record.get("team_id") or "").strip()
    if not team_id or len(team_id) > 80:
        raise ValueError("团队 team_id 无效")
    safe = {key: record.get(key) for key in (
        "team_id", "team_name", "user_id", "device_id", "application_id",
        "display_name", "status", "previous_llm_base", "installation_code",
        "installation_code_binding")
            if record.get(key) is not None}
    safe["origin"] = origin
    rows = [row for row in team_connections()
            if not (row.get("origin") == origin and row.get("team_id") == team_id)]
    rows.append(safe)
    save_local_config({"team_connections": rows})
    if device_token is not None:
        from secure_store import store as ss_store
        ss_store(_team_secret_key(origin, team_id), str(device_token))
    return safe


def clear_team_device_token(origin: str, team_id: str) -> None:
    from secure_store import delete as ss_delete
    ss_delete(_team_secret_key(origin, team_id))


def team_claim_secret(origin: str, application_id: str) -> str:
    from secure_store import load as ss_load
    return ss_load(_team_claim_key(origin, application_id)) or ""


def save_team_claim_secret(origin: str, application_id: str, secret: str) -> None:
    from secure_store import store as ss_store
    ss_store(_team_claim_key(origin, application_id), secret)


def delete_team_claim_secret(origin: str, application_id: str) -> None:
    from secure_store import delete as ss_delete
    ss_delete(_team_claim_key(origin, application_id))
