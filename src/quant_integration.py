"""Thin, loopback-only controller for the isolated Quant Agent Lab.

This module deliberately knows nothing about quant-agent-lab's Python domain
objects.  It owns only service discovery, health checks, process lifecycle and
navigation.  Trading, approval, risk and broker operations remain exclusively
inside the quant-agent-lab backend.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping


DEFAULT_QUANT_PROJECT = Path(__file__).resolve().parents[1] / "quant-agent-lab"
DEFAULT_BACKEND_URL = "http://127.0.0.1:8014"
DEFAULT_GUI_URL = "http://127.0.0.1:4173"
DEFAULT_OPEN_MODE = "auto"
QUANT_STARTUP_TIMEOUT = 15.0
_FORBIDDEN_PORTS = frozenset({8000, 8001})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class QuantLaunchError(RuntimeError):
    """A safe, stable error that can be shown by the main GUI."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str = "",
        detail: str = "",
        manual_command: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.detail = detail
        self.manual_command = manual_command

    @property
    def user_message(self) -> str:
        suffix = f"（阶段：{self.stage}）" if self.stage else ""
        return f"{self.args[0]}{suffix}"


@dataclass(frozen=True)
class QuantIntegrationConfig:
    enabled: bool = True
    auto_start: bool = True
    project_path: Path = DEFAULT_QUANT_PROJECT
    backend_url: str = DEFAULT_BACKEND_URL
    gui_url: str = DEFAULT_GUI_URL
    open_mode: str = DEFAULT_OPEN_MODE
    stop_owned_processes_on_exit: bool = True

    @classmethod
    def from_mapping(
        cls,
        mapping: Mapping[str, Any] | None,
        *,
        project_root: Path | None = None,
        require_project: bool = True,
    ) -> "QuantIntegrationConfig":
        raw = mapping or {}
        root = Path(project_root or DEFAULT_QUANT_PROJECT)
        project_value = str(raw.get("quant_project_path") or root).strip()
        project = Path(project_value).expanduser()
        if not project.is_absolute():
            project = (Path.cwd() / project).resolve()
        else:
            project = project.resolve()
        if require_project and (
            not project.is_dir()
            or not (project / "src" / "quant_agent").is_dir()
            or not (project / "plugins" / "quant-agent-dashboard").is_dir()
        ):
            raise ValueError(f"quant_project_path 不是有效的 quant-agent-lab：{project}")

        backend = _validate_loopback_url(
            str(raw.get("quant_backend_url") or DEFAULT_BACKEND_URL),
            "quant_backend_url",
            forbidden_ports=_FORBIDDEN_PORTS,
        )
        gui = _validate_loopback_url(
            str(raw.get("quant_gui_url") or DEFAULT_GUI_URL),
            "quant_gui_url",
            forbidden_ports=_FORBIDDEN_PORTS,
        )
        mode = str(raw.get("quant_open_mode") or DEFAULT_OPEN_MODE).strip().lower()
        if mode not in {"auto", "browser", "embedded"}:
            raise ValueError("quant_open_mode 必须是 auto、browser 或 embedded")
        return cls(
            enabled=_as_bool(raw.get("quant_enabled"), True),
            auto_start=_as_bool(raw.get("quant_auto_start"), True),
            project_path=project,
            backend_url=backend,
            gui_url=gui,
            open_mode=mode,
            stop_owned_processes_on_exit=_as_bool(
                raw.get("quant_stop_owned_processes_on_exit"), True
            ),
        )


@dataclass(frozen=True)
class QuantServiceStatus:
    backend_ok: bool
    gui_ok: bool
    phase: str
    code: str
    message: str
    backend_url: str
    gui_url: str
    owned_pids: tuple[int, ...] = field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        return self.backend_ok and self.gui_ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_ok": self.backend_ok,
            "gui_ok": self.gui_ok,
            "phase": self.phase,
            "code": self.code,
            "message": self.message,
            "backend_url": self.backend_url,
            "gui_url": self.gui_url,
            "owned_pids": list(self.owned_pids),
        }


@dataclass
class _OwnedProcess:
    role: str
    process: subprocess.Popen[Any]
    pid: int
    log_path: Path


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "开启"}


def _validate_loopback_url(
    value: str, label: str, *, forbidden_ports: frozenset[int]
) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"{label} 必须是 http/https loopback 地址")
    if parsed.username or parsed.password:
        raise ValueError(f"{label} 不能包含用户名或密码")
    if parsed.hostname is None or parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        raise ValueError(f"{label} 只允许 127.0.0.1 或 ::1")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{label} 不能包含 query 或 fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} 端口非法") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValueError(f"{label} 必须包含 1-65535 端口")
    if port in forbidden_ports:
        raise ValueError(f"{label} 不得占用主 Agent 端口 {port}")
    return candidate


class QuantServiceController:
    """Own and operate only the quant services started by this controller."""

    def __init__(
        self,
        config: QuantIntegrationConfig,
        *,
        logger: logging.Logger | None = None,
        startup_timeout: float = QUANT_STARTUP_TIMEOUT,
    ) -> None:
        self.config = config
        self.logger = logger or logging.getLogger("pcagent.quant")
        self.startup_timeout = startup_timeout
        self._lock = threading.RLock()
        self._owned: dict[str, _OwnedProcess] = {}

    @classmethod
    def from_mapping(
        cls,
        mapping: Mapping[str, Any] | None,
        *,
        project_root: Path | None = None,
        require_project: bool = True,
        **kwargs: Any,
    ) -> "QuantServiceController":
        return cls(
            QuantIntegrationConfig.from_mapping(
                mapping, project_root=project_root, require_project=require_project
            ),
            **kwargs,
        )

    @property
    def owned_pids(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(sorted(item.pid for item in self._owned.values()))

    @property
    def diagnostic_log_paths(self) -> tuple[Path, ...]:
        """Stable, project-local paths suitable for a user-facing log action."""
        root = self.config.project_path / "var" / "integration"
        return (
            root / "quant-backend.log",
            root / "quant-gui.log",
            root / "quant-build.log",
        )

    def probe(self) -> QuantServiceStatus:
        backend_ok, backend_code, backend_message = self._probe_endpoint(
            self.config.backend_url, "/api/v1/health", expected="backend"
        )
        gui_ok, gui_code, gui_message = self._probe_endpoint(
            self.config.gui_url, "/healthz", expected="gui"
        )
        if backend_ok and gui_ok:
            code, message = "READY", "量化后端和 GUI 均可用"
        elif not backend_ok:
            code, message = backend_code, f"量化后端不可用：{backend_message}"
        else:
            code, message = gui_code, f"量化 GUI 不可用：{gui_message}"
        return QuantServiceStatus(
            backend_ok=backend_ok,
            gui_ok=gui_ok,
            phase="ready" if backend_ok and gui_ok else "offline",
            code=code,
            message=message,
            backend_url=self.config.backend_url,
            gui_url=self.config.gui_url,
            owned_pids=self.owned_pids,
        )

    def open_quant_center(
        self, progress: Callable[[str], None] | None = None
    ) -> QuantServiceStatus:
        with self._lock:
            if not self.config.enabled:
                raise QuantLaunchError(
                    "QUANT_DISABLED", "量化中心已在设置中禁用", stage="配置"
                )
            _progress(progress, "checking")
            status = self.probe()
            if not status.backend_ok or not status.gui_ok:
                if not self.config.auto_start:
                    raise QuantLaunchError(
                        "SERVICES_OFFLINE",
                        "量化服务未运行，且自动启动已关闭",
                        stage="健康检查",
                        manual_command="\n".join(self.manual_commands()),
                    )
                if not status.backend_ok:
                    _progress(progress, "starting-backend")
                    self._start_backend()
                if not status.gui_ok:
                    _progress(progress, "starting-gui")
                    self._start_gui()
                _progress(progress, "checking")
                status = self.probe()
                if not status.ready:
                    raise QuantLaunchError(
                        status.code,
                        status.message,
                        stage="启动后复核",
                    )
            _progress(progress, "opening")
            self._open_browser(status.gui_url)
            return status

    def manual_commands(self) -> tuple[str, str]:
        return (
            "python -m uvicorn quant_agent.api.app:app --host 127.0.0.1 --port 8014",
            "node standalone/server.mjs",
        )

    def close_owned_processes(self, *, timeout: float = 4.0) -> None:
        with self._lock:
            owned = list(self._owned.values())
            self._owned.clear()
        for item in reversed(owned):
            process = item.process
            if process.poll() is not None:
                continue
            try:
                process.terminate()
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # This is still the exact Popen/PID owned by this controller.
                try:
                    process.kill()
                    process.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    self.logger.warning("quant %s pid=%s did not stop", item.role, item.pid)
            except OSError:
                self.logger.warning("quant %s pid=%s stop failed", item.role, item.pid)

    def _probe_endpoint(
        self, base_url: str, path: str, *, expected: str
    ) -> tuple[bool, str, str]:
        request = urllib.request.Request(
            f"{base_url}{path}", headers={"Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=1.5) as response:
                if response.status != 200:
                    return False, f"{expected.upper()}_UNHEALTHY", f"HTTP {response.status}"
                payload = json_loads(response.read())
                if expected == "backend" and payload.get("status") != "ok":
                    return False, "BACKEND_UNHEALTHY", "health status is not ok"
                if expected == "gui" and payload.get("status") != "ok":
                    return False, "GUI_UNHEALTHY", "health status is not ok"
                return True, "READY", "ok"
        except urllib.error.HTTPError as exc:
            return False, f"{expected.upper()}_UNHEALTHY", f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            return False, "CONNECTION_REFUSED", str(getattr(exc, "reason", exc))[:160]
        except (OSError, TimeoutError) as exc:
            return False, "CONNECTION_REFUSED", str(exc)[:160]
        except ValueError as exc:
            return False, f"{expected.upper()}_UNHEALTHY", str(exc)[:160]

    def _start_backend(self) -> None:
        role = "backend"
        if role in self._owned and self._owned[role].process.poll() is None:
            return
        port = _url_port(self.config.backend_url)
        if not _port_available(port):
            raise QuantLaunchError(
                "PORT_CONFLICT", f"量化后端端口 {port} 已被占用", stage="后端启动"
            )
        python = self._discover_python()
        self.config.project_path.joinpath("var", "integration").mkdir(
            parents=True, exist_ok=True
        )
        log_path = self.config.project_path / "var" / "integration" / "quant-backend.log"
        env = _safe_child_env()
        env["PYTHONPATH"] = str(self.config.project_path / "src")
        args = [
            python,
            "-m",
            "uvicorn",
            "quant_agent.api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        try:
            process = _popen_logged(args, self.config.project_path, env, log_path)
        except OSError as exc:
            raise QuantLaunchError(
                "BACKEND_UNHEALTHY", "量化后端启动失败", stage="后端启动", detail=str(exc)
            ) from exc
        self._owned[role] = _OwnedProcess(role, process, process.pid, log_path)
        self._wait_for(role, process, self.config.backend_url, "/api/v1/health")

    def _start_gui(self) -> None:
        role = "gui"
        if role in self._owned and self._owned[role].process.poll() is None:
            return
        port = _url_port(self.config.gui_url)
        if not _port_available(port):
            raise QuantLaunchError(
                "PORT_CONFLICT", f"量化 GUI 端口 {port} 已被占用", stage="GUI 启动"
            )
        node = shutil.which("node")
        if not node:
            raise QuantLaunchError("NODE_NOT_FOUND", "未找到 Node.js", stage="GUI 启动")
        plugin = self.config.project_path / "plugins" / "quant-agent-dashboard"
        dist_index = plugin / "ui" / "dist" / "index.html"
        if not dist_index.is_file():
            self._build_gui(node, plugin)
        log_path = self.config.project_path / "var" / "integration" / "quant-gui.log"
        env = _safe_child_env()
        env["QUANT_AGENT_BACKEND_URL"] = self.config.backend_url
        env["QUANT_AGENT_STANDALONE_PORT"] = str(port)
        args = [node, "standalone/server.mjs"]
        try:
            process = _popen_logged(args, plugin, env, log_path)
        except OSError as exc:
            raise QuantLaunchError(
                "GUI_UNHEALTHY", "量化 GUI 启动失败", stage="GUI 启动", detail=str(exc)
            ) from exc
        self._owned[role] = _OwnedProcess(role, process, process.pid, log_path)
        self._wait_for(role, process, self.config.gui_url, "/healthz")

    def _build_gui(self, node: str, plugin: Path) -> None:
        log_path = self.config.project_path / "var" / "integration" / "quant-build.log"
        try:
            result = subprocess.run(
                [node, "scripts/build.mjs"],
                cwd=str(plugin),
                env=_safe_child_env(),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            log_path.write_text(
                (result.stdout or "") + "\n" + (result.stderr or ""), encoding="utf-8"
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise QuantLaunchError(
                "BUILD_MISSING", "量化 GUI 构建失败", stage="GUI 构建", detail=str(exc)
            ) from exc
        if result.returncode != 0 or not (plugin / "ui" / "dist" / "index.html").is_file():
            raise QuantLaunchError(
                "BUILD_MISSING",
                "量化 GUI 构建资产缺失",
                stage="GUI 构建",
                detail="请运行 node scripts/build.mjs",
            )

    def _wait_for(
        self,
        role: str,
        process: subprocess.Popen[Any],
        base_url: str,
        health_path: str,
    ) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                code = "BACKEND_UNHEALTHY" if role == "backend" else "GUI_UNHEALTHY"
                raise QuantLaunchError(
                    code, f"量化 {role} 进程提前退出", stage=f"{role} 启动"
                )
            ok, _, _ = self._probe_endpoint(
                base_url, health_path, expected="backend" if role == "backend" else "gui"
            )
            if ok:
                return
            time.sleep(0.25)
        code = "STARTUP_TIMEOUT"
        raise QuantLaunchError(
            code, f"量化 {role} 服务启动超时（{self.startup_timeout:.0f}s）", stage=f"{role} 启动"
        )

    def _discover_python(self) -> str:
        candidates = [
            self.config.project_path / ".venv" / "Scripts" / "python.exe",
            self.config.project_path.parent / ".venv" / "Scripts" / "python.exe",
            Path(sys.executable),
        ]
        probe = "import fastapi, uvicorn, quant_agent; print('ok')"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.config.project_path / "src")
        existing = False
        for candidate in dict.fromkeys(str(path) for path in candidates):
            if not Path(candidate).is_file():
                continue
            existing = True
            try:
                result = subprocess.run(
                    [candidate, "-c", probe],
                    cwd=str(self.config.project_path),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
                if result.returncode == 0 and "ok" in result.stdout:
                    return candidate
            except (OSError, subprocess.TimeoutExpired):
                continue
        raise QuantLaunchError(
            "DEPENDENCY_MISSING" if existing else "PYTHON_NOT_FOUND",
            "Python 存在但缺少量化后端依赖" if existing else "未找到可用的 Python 运行时",
            stage="后端启动",
            detail="需要 fastapi、uvicorn 和 quant_agent 可导入",
        )

    def _open_browser(self, gui_url: str) -> None:
        target = f"{gui_url}/#/dashboard"
        try:
            # auto 当前没有已验证的原生 WebView，安全降级到系统浏览器。
            if self.config.open_mode in {"auto", "browser", "embedded"}:
                if webbrowser.open(target, new=2) is False:
                    raise RuntimeError("系统浏览器未接受打开请求")
        except Exception as exc:
            raise QuantLaunchError(
                "GUI_UNHEALTHY", "量化 GUI 已启动，但无法打开浏览器", stage="打开量化中心", detail=str(exc)
            ) from exc


def json_loads(raw: bytes) -> dict[str, Any]:
    import json

    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("health response must be an object")
    return value


def _progress(callback: Callable[[str], None] | None, stage: str) -> None:
    if callback is not None:
        callback(stage)


def _url_port(value: str) -> int:
    port = urllib.parse.urlparse(value).port
    if port is None:
        raise ValueError(f"URL 缺少端口：{value}")
    return port


def _port_available(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _popen_logged(
    args: list[str], cwd: Path, env: Mapping[str, str], log_path: Path
) -> subprocess.Popen[Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8")
    try:
        return subprocess.Popen(
            args,
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
    finally:
        handle.close()


def _safe_child_env() -> dict[str, str]:
    """Pass runtime plumbing to quant children, never the main Agent secrets."""
    allowed = {
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    }
    # Windows environment names are case-insensitive.  Canonicalize keys so a
    # duplicated PATH/path in the parent process cannot make CreateProcess
    # reject an otherwise safe child environment.
    safe: dict[str, str] = {}
    for key, value in os.environ.items():
        normalized = key.upper()
        if normalized in allowed and normalized not in safe:
            safe[normalized] = value
    return safe


__all__ = [
    "DEFAULT_BACKEND_URL",
    "DEFAULT_GUI_URL",
    "DEFAULT_QUANT_PROJECT",
    "QuantIntegrationConfig",
    "QuantLaunchError",
    "QuantServiceController",
    "QuantServiceStatus",
]
