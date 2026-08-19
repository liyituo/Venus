"""本地凭据与配置存储（Windows DPAPI，仅在本子目录内）。"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCAL_DIR = ROOT / ".local"
SECRETS_FILE = LOCAL_DIR / "secrets.json"
SETTINGS_FILE = LOCAL_DIR / "settings.json"

_store_lock = threading.Lock()


def ensure_local_dir() -> None:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.c_void_p)]


def _dpapi_protect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.restype = ctypes.c_void_p
    blob_in = DATA_BLOB(
        len(data),
        ctypes.cast(ctypes.create_string_buffer(data), ctypes.c_void_p),
    )
    blob_out = DATA_BLOB()
    if not crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.c_void_p(blob_out.pbData))


def _dpapi_unprotect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.restype = ctypes.c_void_p
    blob_in = DATA_BLOB(
        len(data),
        ctypes.cast(ctypes.create_string_buffer(data), ctypes.c_void_p),
    )
    blob_out = DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.c_void_p(blob_out.pbData))


def _encode(value: str) -> str:
    raw = value.encode("utf-8")
    if sys.platform == "win32":
        return "dpapi:" + base64.b64encode(_dpapi_protect(raw)).decode("ascii")
    return "plain:" + base64.b64encode(raw).decode("ascii")


def _decode(encoded: str) -> str:
    if encoded.startswith("dpapi:"):
        return _dpapi_unprotect(base64.b64decode(encoded[6:])).decode("utf-8")
    if encoded.startswith("plain:"):
        return base64.b64decode(encoded[6:]).decode("utf-8")
    return encoded


def _load_secrets() -> dict[str, str]:
    ensure_local_dir()
    if not SECRETS_FILE.exists():
        return {}
    try:
        data = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key, val in data.items():
        if isinstance(val, str) and val:
            try:
                out[key] = _decode(val)
            except OSError:
                continue
    return out


def _save_secrets(secrets: dict[str, str]) -> None:
    ensure_local_dir()
    encoded = {k: _encode(v) for k, v in secrets.items() if v}
    tmp = SECRETS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(encoded, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SECRETS_FILE)
    try:
        os.chmod(SECRETS_FILE, 0o600)
    except OSError:
        pass


def get_secret(name: str) -> str:
    with _store_lock:
        return _load_secrets().get(name, "")


def set_secret(name: str, value: str) -> None:
    with _store_lock:
        secrets = _load_secrets()
        if value:
            secrets[name] = value.strip()
        else:
            secrets.pop(name, None)
        _save_secrets(secrets)


def load_settings() -> dict:
    ensure_local_dir()
    if not SETTINGS_FILE.exists():
        return {"refresh_seconds": 300, "always_on_top": True}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"refresh_seconds": 300, "always_on_top": True}
    return data if isinstance(data, dict) else {"refresh_seconds": 300, "always_on_top": True}


def save_settings(settings: dict) -> None:
    ensure_local_dir()
    SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
