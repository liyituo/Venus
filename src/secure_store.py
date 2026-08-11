"""
密钥安全存储 — API Key / Token 等敏感凭据不进明文配置文件。

- Windows：DPAPI（CryptProtectData，当前用户级加密），密文 base64 存 .pcagent/secrets.json；
- 其他平台：明文写入权限受限文件（0600），并返回警告（不静默降级）；
- 配置文件只保存占位符（"__secure__"），真实凭据在安全存储中；
- 提供旧明文配置迁移入口（_migrate_plaintext）。

纯标准库 + ctypes，无第三方依赖。
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
import threading
from pathlib import Path

log = logging.getLogger("secure-store")

SECRETS_FILE = "secrets.json"
PLACEHOLDER = "__secure__"

_secrets: dict[str, str] = {}
_loaded = False
_load_warning = ""
_store_lock = threading.Lock()   # 读写互斥（多线程并发存取安全）


def _secrets_path() -> Path:
    from data_paths import data_file
    return data_file(SECRETS_FILE)


# ---------------------------------------------------------------------------
# DPAPI（Windows，当前用户级加密）
# ---------------------------------------------------------------------------
def _dpapi_protect(data: bytes) -> bytes:
    """CryptProtectData：加密数据（绑定当前 Windows 用户）。"""
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.c_void_p)]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.restype = ctypes.c_void_p
    crypt32.CryptProtectData.restype = ctypes.c_int
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(DATA_BLOB)]
    blob_in = DATA_BLOB(len(data), ctypes.cast(
        ctypes.create_string_buffer(data), ctypes.c_void_p))
    blob_out = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None,
                                    None, 0, ctypes.byref(blob_out)):
        raise OSError("CryptProtectData 失败（无法加密凭据）")
    try:
        out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.c_void_p(blob_out.pbData))
    return out


def _dpapi_unprotect(data: bytes) -> bytes:
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.c_void_p)]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.restype = ctypes.c_void_p
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(DATA_BLOB)]
    blob_in = DATA_BLOB(len(data), ctypes.cast(
        ctypes.create_string_buffer(data), ctypes.c_void_p))
    blob_out = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None,
                                      None, 0, ctypes.byref(blob_out)):
        raise OSError("CryptUnprotectData 失败（无法解密凭据）")
    try:
        out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.c_void_p(blob_out.pbData))
    return out


def _backend() -> str:
    return "dpapi" if sys.platform == "win32" else "file-0600"


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
    return encoded    # 兼容旧明文（迁移前读取）


def _load() -> None:
    """加载密钥库；损坏时重命名 .corrupt-时间 并告警（不清空静默）。"""
    global _secrets, _loaded, _load_warning
    _loaded = True
    p = _secrets_path()
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _secrets = {k: v for k, v in data.items()}
    except (OSError, json.JSONDecodeError) as exc:
        _load_warning = f"secrets.json 损坏（{exc}），已重命名备份"
        log.warning(_load_warning)
        try:
            import time
            p.rename(p.with_name(f"secrets.json.corrupt-{int(time.time())}"))
        except OSError:
            pass


def _save() -> None:
    """原子写 + 权限限制（Windows 默认用户级；POSIX chmod 600）。

    使用唯一临时文件（pid + 线程号）再原子 replace，避免并发写冲突。
    """
    p = _secrets_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"secrets.json.tmp-{os.getpid()}-{threading.get_ident()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_secrets, ensure_ascii=False))
        fh.flush()
        os.fsync(fh.fileno())   # 条件允许时 fsync
    _restrict_permissions(tmp)
    tmp.replace(p)
    _restrict_permissions(p)


def _restrict_permissions(path: Path) -> None:
    """文件权限限制为当前用户（POSIX 0600；Windows 无 POSIX 权限概念）。"""
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def store(key: str, value: str) -> None:
    """加密存储密钥。空值删除（清空密钥时同时删除旧安全存储值）。"""
    global _secrets
    with _store_lock:
        if not _loaded:
            _load()
        if not value:
            _secrets.pop(key, None)
        else:
            _secrets[key] = _encode(value)
        _save()


def load(key: str) -> str:
    """读取密钥；不存在返回 ''。"""
    global _loaded
    with _store_lock:
        if not _loaded:
            _load()
        enc = _secrets.get(key, "")
        if not enc:
            return ""
        try:
            return _decode(enc)
        except Exception as exc:
            log.warning("密钥 %s 解密失败：%s", key, exc)
            return ""


def delete(key: str) -> None:
    store(key, "")


def migrate_from_plaintext(config: dict, keys: tuple[str, ...]) -> dict:
    """旧明文配置迁移：把明文密钥移入安全存储，配置写占位符。返回新配置。"""
    cfg = dict(config)
    for k in keys:
        val = (cfg.get(k) or "").strip()
        if val and val != PLACEHOLDER:
            store(k, val)
            cfg[k] = PLACEHOLDER
            log.info("密钥 %s 已迁移到安全存储（配置仅保留占位符）", k)
    return cfg


def platform_warning() -> str:
    """非 Windows 平台无法使用 DPAPI 时的明确警告（不静默降级）。"""
    if sys.platform == "win32":
        return ""
    return ("注意：当前平台无法使用 Windows DPAPI，密钥以受限权限文件"
            "（0600）存储于 .pcagent/secrets.json，请确保磁盘与备份安全。")
