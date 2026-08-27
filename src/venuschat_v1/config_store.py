"""VenusChat V1 — local connection config (chat_config.json at repo root)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = _REPO_ROOT / "chat_config.json"


def load_config() -> dict:
    defaults = {
        "llm_base": "http://127.0.0.1:8001",
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
        "quant_enabled": True,
        "quant_project_path": str(_REPO_ROOT / "quant-agent-lab"),
        "quant_backend_url": "http://127.0.0.1:8014",
        "quant_gui_url": "http://127.0.0.1:4173",
    }
    if not CONFIG_PATH.exists():
        return defaults
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg = {**defaults, **raw}
        try:
            from secure_store import load as ss_load
            for key in ("api_token", "daemon_token", "api_key", "vision_api_key"):
                if (cfg.get(key) or "") == "__secure__":
                    cfg[key] = ss_load(key)
        except Exception:
            pass
        return cfg
    except Exception:
        return defaults


def save_local_config(updates: dict) -> None:
    """Merge updates into chat_config.json (non-secret fields only)."""
    cfg = load_config()
    for key, val in updates.items():
        if key in ("api_key", "vision_api_key", "api_token", "daemon_token"):
            continue
        cfg[key] = val
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    if sys.platform != "win32":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    tmp.replace(CONFIG_PATH)


def llm_base() -> str:
    return str(load_config().get("llm_base") or "http://127.0.0.1:8001").rstrip("/")


def token_for_base(base_url: str) -> str:
    cfg = load_config()
    base = base_url.rstrip("/")
    llm = str(cfg.get("llm_base") or "").rstrip("/")
    daemon = str(cfg.get("daemon_base") or "").rstrip("/")
    if llm and base == llm:
        key = "api_token"
    elif daemon and base == daemon:
        key = "daemon_token"
    else:
        key = "api_token" if ":8001" in base else "daemon_token"
    val = str(cfg.get(key) or "").strip()
    if val == "__secure__":
        try:
            from secure_store import load as ss_load
            return ss_load(key) or ""
        except Exception:
            return ""
    return val
