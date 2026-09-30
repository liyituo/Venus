"""Opt-in Windows Remote Worker client.

The process makes outbound HTTP(S) requests to an already configured Hub and
does not listen on a port. The local grant contains scope only; the existing
Hub device token remains in ``secure_store`` and is never put in arguments,
worker grant files, or diagnostic output.

Only ``workspace.list``, ``workspace.read`` and create-new
``workspace.write`` are implemented. Command execution and desktop control
are intentionally unsupported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from data_paths import data_dir

SUPPORTED_TOOLS = frozenset({"workspace.list", "workspace.read", "workspace.write"})
UNSUPPORTED_TOOLS = frozenset({"command.run", "screen.capture", "screen.control",
                               "keyboard.input", "mouse.input"})
_MAX_READ_BYTES = 256 * 1024
_MAX_LIST_ENTRIES = 200
_MAX_TTL_SECONDS = 30 * 24 * 60 * 60
_CALL_ID_RE = re.compile(r"^wcall_[a-f0-9]{32}$")
_STOP_MARKER = "remote_worker.stop"
_GRANTS_FILE = "remote_worker_grants.json"
_SENSITIVE_NAMES = {
    ".ssh", ".aws", ".azure", ".gnupg", ".config", ".codex", ".git",
    ".venv", ".env", ".npmrc", ".pypirc", ".netrc", "secrets",
    "credentials", "credentials.json", "secrets.json", "id_rsa",
    "id_ecdsa", "id_ed25519", "known_hosts",
}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".kdbx"}
_WINDOWS_DEVICE_RE = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)


class RemoteWorkerError(Exception):
    """A fail-closed local Worker error with no credential material."""


def _grant_file() -> Path:
    return data_dir() / _GRANTS_FILE


def _stop_file() -> Path:
    return data_dir() / _STOP_MARKER


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def canonical_args_digest(args: dict) -> str:
    return hashlib.sha256(_canonical_json(args).encode("utf-8")).hexdigest()


def _normalize_https_origin(origin: str) -> str:
    """Compatibility name; Worker now shares the HTTP(S) connection policy."""
    from venuschat_v1.config_store import normalize_team_origin
    try:
        return normalize_team_origin(str(origin or "").strip())
    except ValueError as exc:
        raise RemoteWorkerError("Hub origin 无效") from exc


def _read_grants(path: Path | None = None) -> list[dict]:
    source = path or _grant_file()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoteWorkerError("本机 Worker 授权文件不可读；已停用 Worker") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1 \
            or not isinstance(value.get("grants"), list):
        raise RemoteWorkerError("本机 Worker 授权文件格式无效；已停用 Worker")
    return [row for row in value["grants"] if isinstance(row, dict) and row.get("enabled") is True]


def _write_grants(rows: list[dict], path: Path | None = None) -> None:
    target = path or _grant_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp-{os.getpid()}-{int(time.time() * 1000)}")
    payload = json.dumps({"schema_version": 1, "grants": rows},
                         ensure_ascii=False, indent=2)
    try:
        with tmp.open("x", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        tmp.replace(target)
        if os.name != "nt":
            os.chmod(target, 0o600)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def authorize_local_grant(origin: str, team_id: str, project_id: str,
                           workspace: str | Path, tools: list[str] | tuple[str, ...],
                           expires_seconds: int, *, consent: bool = False,
                           path: Path | None = None, now: float | None = None) -> dict:
    """Persist a local scope after explicit consent; never accepts a raw token.

    ``consent`` is a caller's confirmation that the user explicitly enabled
    this scope (CLI confirmation or a UI confirmation action). The CLI always
    asks interactively. Applications should call this only after an explicit
    user gesture.
    """
    if not consent:
        raise RemoteWorkerError("需要本机用户明确确认 Worker 授权")
    normalized_origin = _normalize_https_origin(origin)
    tid = str(team_id or "").strip()
    pid = str(project_id or "").strip()
    if not tid or len(tid) > 80 or not pid or len(pid) > 128:
        raise RemoteWorkerError("team_id 或 project_id 无效")
    selected_tools = sorted({str(tool).strip() for tool in tools})
    if not selected_tools or not set(selected_tools) <= SUPPORTED_TOOLS:
        unsupported = sorted(set(selected_tools) - SUPPORTED_TOOLS)
        if unsupported:
            raise RemoteWorkerError("所选工具不受支持：" + ", ".join(unsupported))
        raise RemoteWorkerError("至少选择一个受支持工具")
    try:
        ttl = int(expires_seconds)
    except (TypeError, ValueError) as exc:
        raise RemoteWorkerError("授权有效期须为整数秒") from exc
    if ttl < 60 or ttl > _MAX_TTL_SECONDS:
        raise RemoteWorkerError("Worker 授权有效期须为 60 秒到 30 天")

    candidate = Path(workspace).expanduser()
    if _is_link_like(candidate):
        raise RemoteWorkerError("工作目录本身不能是符号链接或目录联接")
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise RemoteWorkerError("工作目录不存在或不可访问") from exc
    if not root.is_dir():
        raise RemoteWorkerError("工作路径必须是目录")

    from venuschat_v1.config_store import team_connections, team_token_for_connection
    connection = next((row for row in reversed(team_connections())
                       if row.get("origin") == normalized_origin
                       and str(row.get("team_id") or "") == tid), None)
    if not connection:
        raise RemoteWorkerError("此 HTTPS Hub 尚未通过 Venus 终端连接流程登记")
    device_id = str(connection.get("device_id") or "").strip()
    if not device_id:
        raise RemoteWorkerError("已登记 Hub 连接缺少本终端 ID")
    # Check the existing DPAPI/secure-store entry without copying it into the
    # Worker grant. It is loaded again immediately before network requests.
    if not team_token_for_connection(normalized_origin, tid):
        raise RemoteWorkerError("现有 Hub 设备凭证不可用；请先在 Venus 终端重新连接")

    timestamp = time.time() if now is None else float(now)
    record = {"enabled": True, "origin": normalized_origin, "team_id": tid,
              "project_id": pid, "device_id": device_id,
              "workspace": str(root), "tools": selected_tools,
              "created_at": timestamp, "expires_at": timestamp + ttl}
    rows = _read_grants(path)
    rows = [row for row in rows if not (
        row.get("origin") == normalized_origin and row.get("team_id") == tid
        and row.get("project_id") == pid and row.get("device_id") == device_id)]
    rows.append(record)
    _write_grants(rows, path)
    # A new explicit authorization is the only way to clear the global stop.
    stop_path = _stop_file() if path is None else path.with_name(_STOP_MARKER)
    stop_path.unlink(missing_ok=True)
    return dict(record)


def local_worker_status(*, path: Path | None = None,
                        stop_path: Path | None = None,
                        now: float | None = None) -> dict:
    current = time.time() if now is None else float(now)
    stopped = (stop_path or (_stop_file() if path is None else path.with_name(_STOP_MARKER))).exists()
    rows = _read_grants(path)
    active = [row for row in rows if float(row.get("expires_at") or 0) > current]
    return {"stopped": stopped, "active_grants": len(active),
            "expired_grants": len(rows) - len(active),
            "grants": [{"origin": row.get("origin"), "team_id": row.get("team_id"),
                        "project_id": row.get("project_id"),
                        "device_id": row.get("device_id"),
                        "workspace": row.get("workspace"), "tools": row.get("tools"),
                        "expires_at": row.get("expires_at"),
                        "active": float(row.get("expires_at") or 0) > current}
                       for row in rows]}


def stop_worker(*, stop_path: Path | None = None) -> None:
    marker = stop_path or _stop_file()
    marker.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(marker, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("stopped\n")
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        os.chmod(marker, 0o600)


def is_worker_stopped(*, stop_path: Path | None = None) -> bool:
    return (stop_path or _stop_file()).exists()


def revoke_local_grant(project_id: str, *, origin: str | None = None,
                       team_id: str | None = None, path: Path | None = None) -> int:
    pid = str(project_id or "")
    rows = _read_grants(path)
    before = len(rows)
    rows = [row for row in rows if not (
        row.get("project_id") == pid
        and (origin is None or row.get("origin") == _normalize_https_origin(origin))
        and (team_id is None or row.get("team_id") == str(team_id)))]
    _write_grants(rows, path)
    return before - len(rows)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward X-Team-Device-Token to a redirect target, even same-origin.
        return None


class HubClient:
    """HTTP(S) client using the desktop connection password and trust settings."""

    def __init__(self, origin: str, token: str, *, timeout: float = 10):
        self.origin = _normalize_https_origin(origin)
        self._token = str(token or "")
        if not self._token:
            raise RemoteWorkerError("Hub 设备凭证不可用")
        self.timeout = max(1.0, min(float(timeout), 30.0))
        from venuschat_v1.connection_store import tls_context
        handlers = [urllib.request.ProxyHandler({}), _NoRedirect()]
        if self.origin.startswith("https://"):
            handlers.append(urllib.request.HTTPSHandler(context=tls_context(self.origin)))
        self._opener = urllib.request.build_opener(*handlers)

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        if not path.startswith("/api/v1/worker/"):
            raise RemoteWorkerError("拒绝请求 Worker API 以外的 Hub 路径")
        body = None if payload is None else _canonical_json(payload).encode("utf-8")
        import base64
        from direct_connection import PASSWORD_HEADER
        from venuschat_v1.connection_store import connection_password
        headers = {"Content-Type": "application/json", "X-Team-Device-Token": self._token}
        password = connection_password(self.origin)
        if password:
            headers[PASSWORD_HEADER] = base64.b64encode(password.encode("utf-8")).decode("ascii")
        req = urllib.request.Request(
            self.origin + path, data=body, method=method.upper(), headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                raw = response.read(_MAX_READ_BYTES + 1)
                if len(raw) > _MAX_READ_BYTES:
                    raise RemoteWorkerError("Hub 响应过大")
                return response.status, _decode_json(raw)
        except urllib.error.HTTPError as exc:
            # Do not include the request object, headers, or token in diagnostics.
            if 300 <= exc.code < 400:
                raise RemoteWorkerError("Hub 返回了重定向；Worker 已拒绝跟随") from None
            return exc.code, {"detail": f"Hub HTTP {exc.code}"}
        except RemoteWorkerError:
            raise
        except Exception as exc:
            raise RemoteWorkerError(f"Hub 连接失败：{type(exc).__name__}") from None

    def post(self, path: str, payload: dict | None = None) -> tuple[int, dict]:
        return self.request("POST", path, payload or {})


def _decode_json(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return {"detail": "Hub returned a non-JSON response"}
    return value if isinstance(value, dict) else {"detail": "Hub returned an invalid response"}


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return True


def _check_relative_path(relative: str, *, allow_root: bool = False) -> tuple[str, ...]:
    raw = str(relative or "")
    if any(ord(ch) < 32 for ch in raw) or any(ch in raw for ch in ':<>|?*'):
        raise RemoteWorkerError("拒绝无效或带盘符/ADS 的工作区路径")
    normalized = raw.replace("\\", "/")
    if normalized in ("", ".") and allow_root:
        return ()
    posix = PurePosixPath(normalized)
    parts = posix.parts
    if posix.is_absolute() or not parts or any(part in ("", ".", "..") for part in parts):
        raise RemoteWorkerError("拒绝绝对路径或路径穿越")
    if len(normalized) > 1024:
        raise RemoteWorkerError("工作区路径过长")
    for part in parts:
        if part.rstrip(" .") != part or _WINDOWS_DEVICE_RE.match(part):
            raise RemoteWorkerError("拒绝 Windows 设备名或路径别名")
        lower = part.casefold()
        if (lower in _SENSITIVE_NAMES or lower.startswith(".env.")
                or Path(lower).suffix in _SENSITIVE_SUFFIXES):
            raise RemoteWorkerError("该敏感文件名不允许经 Worker 读取或写入")
    return tuple(parts)


def resolve_workspace_path(workspace: str | Path, relative: str, *,
                           allow_root: bool = False,
                           must_exist: bool = False) -> Path:
    raw_root = Path(workspace).expanduser()
    if _is_link_like(raw_root):
        raise RemoteWorkerError("工作区根目录不能是符号链接或目录联接")
    try:
        root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise RemoteWorkerError("工作区根目录不可用") from exc
    if not root.is_dir():
        raise RemoteWorkerError("授权工作区不再是目录")
    parts = _check_relative_path(relative, allow_root=allow_root)
    candidate = root
    for part in parts:
        candidate = candidate / part
        if _is_link_like(candidate):
            raise RemoteWorkerError("工作区路径包含符号链接/目录联接")
    try:
        resolved = candidate.resolve(strict=must_exist)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RemoteWorkerError("工作区路径越界或不存在") from exc
    if must_exist and not resolved.exists():
        raise RemoteWorkerError("工作区目标不存在")
    return resolved


def _tool_requires_confirmation(tool: str) -> bool:
    return tool == "workspace.write"


def _execute_tool(grant: dict, tool: str, args: dict) -> dict:
    root = str(grant["workspace"])
    if tool == "workspace.list":
        directory = resolve_workspace_path(root, str(args.get("path") or ""),
                                           allow_root=True, must_exist=True)
        if not directory.is_dir():
            raise RemoteWorkerError("workspace.list 的目标不是目录")
        result = []
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.casefold())
        except OSError as exc:
            raise RemoteWorkerError("无法读取授权目录") from exc
        for child in entries:
            lower = child.name.casefold()
            if (lower in _SENSITIVE_NAMES or lower.startswith(".env.")
                    or Path(lower).suffix in _SENSITIVE_SUFFIXES):
                continue
            try:
                link = _is_link_like(child)
                info = child.lstat()
                kind = "symlink" if link else ("directory" if child.is_dir() else "file")
                result.append({"name": child.name, "type": kind,
                               "size": 0 if kind != "file" else int(info.st_size)})
            except OSError:
                continue
            if len(result) >= _MAX_LIST_ENTRIES:
                break
        return {"entries": result, "truncated": len(entries) > len(result)}

    if tool == "workspace.read":
        path = resolve_workspace_path(root, str(args["path"]), must_exist=True)
        if not path.is_file():
            raise RemoteWorkerError("workspace.read 仅支持普通文件")
        try:
            size = path.stat().st_size
            if size > _MAX_READ_BYTES:
                raise RemoteWorkerError("文件超过本 Worker 的读取上限")
            content = path.read_bytes()
        except OSError as exc:
            raise RemoteWorkerError("无法读取授权文件") from exc
        if len(content) > _MAX_READ_BYTES:
            raise RemoteWorkerError("文件超过本 Worker 的读取上限")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RemoteWorkerError("workspace.read 只支持 UTF-8 文本") from exc
        return {"path": str(args["path"]), "content": text}

    if tool == "workspace.write":
        path = resolve_workspace_path(root, str(args["path"]))
        if not path.parent.is_dir():
            raise RemoteWorkerError("workspace.write 只允许写入已存在目录")
        if path.exists():
            raise RemoteWorkerError("workspace.write 只允许创建新文件，不会覆盖文件")
        content = str(args["content"])
        if len(content.encode("utf-8")) > _MAX_READ_BYTES:
            raise RemoteWorkerError("写入内容超过本 Worker 上限")
        # Exclusive creation is a final no-overwrite check. Parent symlink
        # components were checked by resolve_workspace_path immediately above.
        try:
            with path.open("x", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise RemoteWorkerError("workspace.write 只允许创建新文件，不会覆盖文件") from exc
        except OSError as exc:
            raise RemoteWorkerError("无法在授权目录创建文件") from exc
        return {"path": str(args["path"]), "created": True,
                "bytes_written": len(content.encode("utf-8"))}

    raise RemoteWorkerError("此 Worker 工具不受支持")


def _grant_is_current(grant: dict, *, now: float | None = None) -> bool:
    current = time.time() if now is None else float(now)
    return (grant.get("enabled") is True and float(grant.get("expires_at") or 0) > current
            and set(grant.get("tools") or []) <= SUPPORTED_TOOLS
             and bool(grant.get("project_id")) and bool(grant.get("device_id")))


def _grant_is_saved(grant: dict) -> bool:
    """A replaced or revoked local grant must stop an already running loop."""
    return _grant_is_current(grant) and any(
        row == grant for row in _read_grants())


def _validate_binding(grant: dict, offered: dict, preflight: dict) -> dict:
    expected = ("call_id", "project_id", "job_id", "target_device_id",
                "tool_call_id", "tool", "args_sha256")
    for field in expected:
        if str(offered.get(field) or "") != str(preflight.get(field) or ""):
            raise RemoteWorkerError("Hub 调用绑定信息在领取后发生变化")
    if str(preflight.get("project_id") or "") != str(grant.get("project_id") or ""):
        raise RemoteWorkerError("Worker 调用项目与本机授权不匹配")
    if str(preflight.get("target_device_id") or "") != str(grant.get("device_id") or ""):
        raise RemoteWorkerError("Worker 调用目标设备与本机授权不匹配")
    tool = str(preflight.get("tool") or "")
    if tool not in SUPPORTED_TOOLS or tool not in set(grant.get("tools") or []):
        raise RemoteWorkerError("Worker 工具未获本机授权或不受支持")
    args = preflight.get("args")
    if not isinstance(args, dict):
        raise RemoteWorkerError("Hub 工具参数不是 JSON 对象")
    digest = canonical_args_digest(args)
    if not _constant_time_equal(digest, str(preflight.get("args_sha256") or "")):
        raise RemoteWorkerError("Worker 工具参数摘要校验失败")
    try:
        deadline = min(float(preflight.get("expires_at") or 0),
                       float(preflight.get("lease_until") or 0))
    except (TypeError, ValueError) as exc:
        raise RemoteWorkerError("Worker 调用过期时间无效") from exc
    if deadline <= time.time():
        raise RemoteWorkerError("Worker 调用或租约已过期")
    if not _grant_is_saved(grant):
        raise RemoteWorkerError("本机 Worker 授权已过期或已撤销")
    return args


def _constant_time_equal(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(str(a), str(b))


def _call_hub(client: HubClient, method: str, path: str,
              payload: dict | None = None) -> dict:
    status, body = client.request(method, path, payload)
    if status < 200 or status >= 300:
        raise RemoteWorkerError(f"Hub 拒绝 Worker 请求（HTTP {status}）")
    return body


def process_once(grant: dict, client: HubClient, *,
                 confirm: Callable[[dict, dict], bool] | None = None,
                 stop_check: Callable[[], bool] = is_worker_stopped) -> bool:
    """Poll one project and process at most one one-shot lease.

    Returns False when there is no call. Errors are raised with secret-free
    messages so the caller can report a useful local status.
    """
    if stop_check():
        raise RemoteWorkerError("本机 Worker 已紧急停止")
    if not _grant_is_saved(grant):
        raise RemoteWorkerError("本机 Worker 授权已过期或已撤销")
    project_id = str(grant.get("project_id") or "")
    offered = _call_hub(client, "POST", "/api/v1/worker/poll",
                        {"project_id": project_id}).get("call")
    if offered is None:
        return False
    if not isinstance(offered, dict) or not _CALL_ID_RE.fullmatch(str(offered.get("call_id") or "")):
        raise RemoteWorkerError("Hub 返回了无效 Worker 调用")
    lease_token = str(offered.get("lease_token") or "")
    if not lease_token:
        raise RemoteWorkerError("Hub 未返回 Worker 租约凭证")
    call_id = str(offered["call_id"])

    def fetch_preflight() -> dict:
        return _call_hub(client, "POST", f"/api/v1/worker/calls/{call_id}/preflight",
                         {"lease_token": lease_token})

    preflight = fetch_preflight()
    args = _validate_binding(grant, offered, preflight)
    tool = str(preflight["tool"])
    if tool == "workspace.write":
        callback = confirm or _interactive_confirm
        if stop_check() or not _grant_is_saved(grant):
            raise RemoteWorkerError("本机 Worker 已停止、授权过期或已撤销")
        if not callback(preflight, args):
            _call_hub(client, "POST", f"/api/v1/worker/calls/{call_id}/complete", {
                "lease_token": lease_token, "args_sha256": preflight["args_sha256"],
                "result": None, "error": "local_confirmation_denied"})
            return True
        # User confirmation may take time. Recheck server-side revocation and
        # the full arguments immediately before the high-risk local operation.
        preflight = fetch_preflight()
        args = _validate_binding(grant, offered, preflight)
    if stop_check() or not _grant_is_saved(grant):
        raise RemoteWorkerError("本机 Worker 已停止、授权过期或已撤销")

    try:
        result = _execute_tool(grant, tool, args)
        error = ""
    except RemoteWorkerError as exc:
        result, error = None, str(exc)[:1000]
    except Exception as exc:
        result, error = None, f"local_tool_error:{type(exc).__name__}"
    _call_hub(client, "POST", f"/api/v1/worker/calls/{call_id}/complete", {
        "lease_token": lease_token, "args_sha256": preflight["args_sha256"],
        "result": result, "error": error})
    return True


def _interactive_confirm(call: dict, args: dict) -> bool:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    path = str(args.get("path") or "")
    content = str(args.get("content") or "").encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    print("\nRemote Worker write request")
    print(f"  project: {call.get('project_id')}  job: {call.get('job_id')}")
    print(f"  file to create: {path}")
    print(f"  content bytes: {len(content)}  sha256: {digest}")
    print("This creates a new file under your explicitly authorized workspace.")
    expected = f"CREATE {str(call.get('call_id') or '')}"
    try:
        answer = input(f"Type {expected} to allow this one write: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == expected


def _active_records(*, now: float | None = None) -> list[dict]:
    if is_worker_stopped():
        return []
    return [row for row in _read_grants() if _grant_is_current(row, now=now)]


def _make_client(grant: dict) -> HubClient:
    from venuschat_v1.config_store import team_token_for_connection
    token = team_token_for_connection(str(grant["origin"]), str(grant["team_id"]))
    return HubClient(str(grant["origin"]), token)


def _run_loop(poll_seconds: float = 3.0) -> None:
    delay = max(1.0, min(float(poll_seconds), 60.0))
    while not is_worker_stopped():
        worked = False
        for grant in _active_records():
            if is_worker_stopped():
                break
            try:
                client = _make_client(grant)
                worked = process_once(grant, client) or worked
            except RemoteWorkerError as exc:
                print(f"Remote Worker: {exc}", file=sys.stderr)
            if is_worker_stopped():
                break
        if not worked and not is_worker_stopped():
            time.sleep(delay)


def _authorize_command(args: argparse.Namespace) -> None:
    tools = [item.strip() for item in args.tools.split(",") if item.strip()]
    print("Enable a local Remote Worker grant with this exact scope:")
    print(f"  Hub: {args.origin}")
    print(f"  project: {args.project_id}")
    print(f"  workspace: {Path(args.workspace).expanduser().resolve()}")
    print(f"  tools: {', '.join(tools)}")
    print(f"  expires in: {args.expires_seconds} seconds")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RemoteWorkerError("授权必须在本机交互终端完成")
    try:
        response = input("Type ENABLE WORKER to grant exactly this scope: ")
    except (EOFError, KeyboardInterrupt):
        response = ""
    if response != "ENABLE WORKER":
        raise RemoteWorkerError("本机用户未确认授权")
    record = authorize_local_grant(
        args.origin, args.team_id, args.project_id, args.workspace,
        tools, args.expires_seconds, consent=True)
    print("Worker authorization saved.")
    print(f"  device: {record['device_id']}  expires: {int(record['expires_at'])}")
    print("Start the foreground worker with: python src/remote_worker.py run")


def _status_command() -> None:
    status = local_worker_status()
    print(json.dumps(status, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Opt-in Venus Remote Worker (workspace only)")
    sub = parser.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("authorize", help="interactively grant a scoped local Worker")
    auth.add_argument("--origin", required=True)
    auth.add_argument("--team-id", required=True)
    auth.add_argument("--project-id", required=True)
    auth.add_argument("--workspace", required=True)
    auth.add_argument("--tools", required=True,
                      help="comma-separated: workspace.list,workspace.read,workspace.write")
    auth.add_argument("--expires-seconds", type=int, default=8 * 60 * 60)
    run = sub.add_parser("run", help="run the outbound poller in this interactive session")
    run.add_argument("--poll-seconds", type=float, default=3.0)
    sub.add_parser("status", help="show local grant scopes and stop state")
    sub.add_parser("stop", help="emergency stop all local Worker polling")
    revoke = sub.add_parser("revoke", help="remove local grants for a project")
    revoke.add_argument("--project-id", required=True)
    revoke.add_argument("--origin")
    revoke.add_argument("--team-id")
    args = parser.parse_args(argv)
    try:
        if args.command == "authorize":
            _authorize_command(args)
        elif args.command == "run":
            _run_loop(args.poll_seconds)
        elif args.command == "status":
            _status_command()
        elif args.command == "stop":
            stop_worker()
            print("Remote Worker emergency stop is active.")
        elif args.command == "revoke":
            count = revoke_local_grant(args.project_id, origin=args.origin,
                                       team_id=args.team_id)
            print(f"Removed {count} local Worker grant(s).")
        return 0
    except RemoteWorkerError as exc:
        print(f"Remote Worker: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
