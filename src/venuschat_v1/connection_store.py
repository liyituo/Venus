"""Per-origin transport options; passwords are held in the OS secure store."""

from __future__ import annotations

import hashlib
import ssl
from pathlib import Path

from direct_connection import normalize_origin
from .config_store import load_config, save_local_config


def _key(base: str) -> str:
    origin = normalize_origin(base, default_port=None)
    return "connection_password_" + hashlib.sha256(origin.encode()).hexdigest()[:32]


def connection_options(base: str) -> dict:
    origin = normalize_origin(base, default_port=None)
    options = load_config().get("server_connections") or {}
    return dict(options.get(origin) or {}) if isinstance(options, dict) else {}


def connection_password(base: str) -> str:
    if not connection_options(base).get("password_enabled"):
        return ""
    from secure_store import load
    return load(_key(base)) or ""


def tls_context(base: str) -> ssl.SSLContext:
    ca_file = str(connection_options(base).get("ca_file") or "")
    return ssl.create_default_context(cafile=ca_file or None)


def save_connection(base: str, *, password_enabled: bool, password: str | None,
                    ca_file: str = "") -> None:
    origin = normalize_origin(base, default_port=None)
    ca_file = str(Path(ca_file).expanduser().resolve()) if ca_file.strip() else ""
    if origin.startswith("https://"):
        ssl.create_default_context(cafile=ca_file or None)
    if password_enabled and password == "":
        raise ValueError("已启用密码，请填写连接密码。")
    from secure_store import delete, store
    options = load_config().get("server_connections") or {}
    options = dict(options) if isinstance(options, dict) else {}
    if password_enabled and password is not None:
        store(_key(origin), password)
    elif not password_enabled:
        delete(_key(origin))
    options[origin] = {"password_enabled": password_enabled, "ca_file": ca_file}
    save_local_config({"server_connections": options})
