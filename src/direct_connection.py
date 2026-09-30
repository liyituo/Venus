"""Shared HTTP(S) origins and optional server password authentication."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from collections import OrderedDict
from urllib.parse import urlsplit

DEFAULT_PORT = 8001
PASSWORD_HEADER = "X-Venus-Password"


def normalize_origin(value: str, *, tls: bool | None = None,
                     default_port: int | None = DEFAULT_PORT) -> str:
    raw = str(value or "").strip()
    if not raw or any(c.isspace() or ord(c) < 32 for c in raw):
        raise ValueError("请输入服务器 IP 或域名。")
    if "://" not in raw:
        raw = ("https://" if tls else "http://") + raw
    try:
        parsed = urlsplit(raw)
        port = parsed.port
        host = parsed.hostname
    except ValueError as exc:
        raise ValueError("服务器地址或端口无效。") from exc
    if (parsed.scheme not in ("http", "https") or not host
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or "\\" in host or "%" in host):
        raise ValueError("请输入 IP 或域名，不要包含路径、账号或参数。")
    host = host.lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and address.is_unspecified:
        raise ValueError("客户端请填写实际 IP；0.0.0.0 和 :: 仅用于服务端监听。")
    if port == 0:
        raise ValueError("端口须为 1–65535。")
    scheme = parsed.scheme if tls is None else ("https" if tls else "http")
    port = port if port is not None else default_port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("端口须为 1–65535。")
    netloc = f"[{host}]" if ":" in host else host
    return f"{scheme}://{netloc}{':' + str(port) if port else ''}"


class PasswordGate:
    """Keep only a salted password hash; bound failed attempts per peer.

    Password authentication does not encrypt HTTP. TLS is configured separately.
    A keyed digest cache avoids repeating scrypt for every successful poll.
    """

    def __init__(self, password: str = "") -> None:
        self.enabled = bool(password)
        self._salt = secrets.token_bytes(16)
        self._key = secrets.token_bytes(32)
        self._hash = self._derive(password) if self.enabled else b""
        self._accepted: bytes | None = None
        self._attempts: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _derive(self, password: str) -> bytes:
        return hashlib.scrypt(password.encode("utf-8"), salt=self._salt,
                              n=16384, r=8, p=1, dklen=32)

    def check(self, password: str, peer: str) -> int:
        if not self.enabled:
            return 200
        if not password or len(password) > 1024:
            return 401
        digest = hmac.digest(self._key, password.encode("utf-8"), "sha256")
        with self._lock:
            if self._accepted and hmac.compare_digest(digest, self._accepted):
                return 200
            now = time.monotonic()
            attempts = [t for t in self._attempts.pop(peer, []) if now - t < 60]
            self._attempts[peer] = attempts
            while len(self._attempts) > 1024:
                self._attempts.popitem(last=False)
            if len(attempts) >= 10:
                return 429
            if hmac.compare_digest(self._derive(password), self._hash):
                self._accepted = digest
                self._attempts.pop(peer, None)
                return 200
            attempts.append(now)
            return 401
