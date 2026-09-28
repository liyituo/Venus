"""Stable, public identity for one local Venus installation.

The installation code only helps people identify which client submitted a
join request.  It is never an authentication credential.
"""

from __future__ import annotations

import base64
import os
import secrets
import threading
import time
from pathlib import Path


_FILE_NAME = "installation_code.txt"
_CODE_LOCK = threading.Lock()
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_STANDARD_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def _lock_file(path: Path):
    """Acquire an OS-level lock so two Venus processes share first-run setup."""
    handle = open(path, "a+b")
    deadline = time.monotonic() + 3.0
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        while True:
            try:
                handle.seek(0)
                # LK_LOCK retries internally for up to ten seconds on Windows.
                # Use the non-blocking mode so our deadline also bounds UI startup.
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError("等待本机安装标识文件锁超时")
                time.sleep(0.05)
    else:
        import fcntl

        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError("等待本机安装标识文件锁超时")
                time.sleep(0.05)
    return handle


def _unlock_file(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _format_random_bytes(value: bytes) -> str:
    if len(value) != 16:
        raise ValueError("安装标识必须由 128 位随机数生成")
    encoded = base64.b32encode(value).decode("ascii").rstrip("=")
    alphabet = {char: _CROCKFORD[index]
                for index, char in enumerate(_STANDARD_B32)}
    readable = "".join(alphabet[char] for char in encoded)
    groups = (readable[:5], readable[5:10], readable[10:15],
              readable[15:20], readable[20:])
    return "VI-" + "-".join(groups)


def _is_valid_installation_code(value: str) -> bool:
    if not value.startswith("VI-"):
        return False
    chars = value[3:].replace("-", "")
    return (len(chars) == 26
            and all(char in _CROCKFORD for char in chars)
            and value == "VI-" + "-".join((chars[:5], chars[5:10], chars[10:15],
                                            chars[15:20], chars[20:])))


def get_installation_code(path: str | Path | None = None) -> str:
    """Return this installation's stable public code, creating it once.

    A unique temporary file is flushed to disk and atomically replaced while
    holding both a process-local mutex and an OS file lock.  An existing but
    invalid file is reported instead of silently changing this installation's
    identity.
    """
    if path is None:
        from data_paths import data_file

        target = data_file(_FILE_NAME)
    else:
        target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")

    with _CODE_LOCK:
        lock_handle = _lock_file(lock_path)
        try:
            if target.exists():
                code = target.read_text(encoding="ascii").strip()
                if not _is_valid_installation_code(code):
                    raise ValueError(f"本机安装标识文件无效：{target}")
                return code

            code = _format_random_bytes(secrets.token_bytes(16))
            temporary = target.with_name(
                f"{target.name}.tmp-{os.getpid()}-{threading.get_ident()}-"
                f"{secrets.token_hex(6)}")
            try:
                with temporary.open("x", encoding="ascii", newline="\n") as handle:
                    handle.write(code + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                if os.name != "nt":
                    try:
                        os.chmod(temporary, 0o600)
                    except OSError:
                        pass
                os.replace(temporary, target)
                if os.name != "nt":
                    try:
                        os.chmod(target, 0o600)
                    except OSError:
                        pass
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            return code
        finally:
            _unlock_file(lock_handle)
