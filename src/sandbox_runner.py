"""执行沙箱：host / workspace / wsl 档位，审计日志。"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from data_paths import data_dir, workspace_data_dir

log = logging.getLogger("sandbox_runner")

_LOCK = threading.RLock()
VALID_MODES = frozenset({"host", "workspace", "wsl"})
DEFAULT_TIMEOUT = 120.0
DEFAULT_OUTPUT_LIMIT = 3000
AUDIT_FILE = lambda: data_dir() / "sandbox_audit.jsonl"

# 与 llm_server 对齐的危险命令模式（沙箱内同样拦截）
DANGEROUS_PATTERNS = [
    r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/\b",
    r"\brm\s+-rf\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bformat\s+[a-zA-Z]:",
    r"\bdel\s+/[sfq]",
]


def get_sandbox_default() -> str:
    from extension_registry import load_state
    state = load_state()
    mode = str((state.get("settings") or {}).get("sandbox_default") or "host")
    return mode if mode in VALID_MODES else "host"


def set_sandbox_default(mode: str) -> tuple[bool, str]:
    if mode not in VALID_MODES:
        return False, f"无效档位：{mode}（可选 host/workspace/wsl）"
    from extension_registry import load_state, save_state
    state = load_state()
    settings = dict(state.get("settings") or {})
    settings["sandbox_default"] = mode
    state["settings"] = settings
    save_state(state)
    return True, f"默认沙箱档位已设为 {mode}"


def sandbox_status() -> dict:
    wsl = shutil.which("wsl") if sys.platform == "win32" else None
    return {
        "ok": True,
        "default_mode": get_sandbox_default(),
        "modes": sorted(VALID_MODES),
        "wsl_available": bool(wsl),
        "audit_file": str(AUDIT_FILE()),
    }


def _audit(entry: dict) -> None:
    entry = {**entry, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    path = AUDIT_FILE()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _controlled_env(allow_network: bool, extra: dict | None = None) -> dict:
    allow_keys = {
        "PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP",
        "SYSTEMROOT", "SystemRoot", "WINDIR", "APPDATA", "LOCALAPPDATA",
        "USERNAME", "USER", "LANG", "LC_ALL", "COMSPEC", "PATHEXT",
        "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS", "COMPUTERNAME",
    }
    env = {k: v for k, v in os.environ.items() if k in allow_keys}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    if not allow_network:
        # v1：通过无效代理降低意外出网概率（非强隔离）
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
            env[key] = "http://127.0.0.1:9"
        env["NO_PROXY"] = ""
        env["no_proxy"] = ""
    if extra:
        env.update(extra)
    return env


def _is_dangerous(command: str) -> str | None:
    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, command, re.IGNORECASE):
            return pat
    return None


def _decode(data) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data or ""


def _run_subprocess(
    cmd,
    cwd: Path,
    timeout: float,
    shell: bool,
    env: dict | None,
) -> tuple[int, str, str, bool]:
    kwargs: dict = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd),
        shell=shell,
    )
    if env is not None:
        kwargs["env"] = env
    if sys.platform != "win32":
        if shell:
            kwargs["executable"] = "/bin/bash"
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode or 0, _decode(out), _decode(err), False
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return proc.returncode or -1, _decode(out), _decode(err), True


def _safe_join(workspace: Path, rel: str) -> Path | None:
    raw = (rel or "").strip()
    if not raw:
        return None
    if raw.startswith(("/", "\\")) or ":" in raw.split("/")[0].split("\\")[0]:
        return None
    parts = raw.replace("\\", "/").strip("/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    target = workspace.joinpath(*parts)
    try:
        target.resolve().relative_to(workspace.resolve())
    except (ValueError, OSError):
        return None
    return target


def _resolve_cwd(workspace: Path, rel: str | None) -> tuple[Path | None, str | None]:
    rel = (rel or "").strip()
    if not rel or rel == ".":
        return workspace, None
    target = _safe_join(workspace, rel)
    if target is None:
        return None, "非法 cwd：必须是工作区内的相对路径"
    if not target.is_dir():
        return None, f"目录不存在：{rel}"
    return target, None


def _wsl_available() -> bool:
    return sys.platform == "win32" and bool(shutil.which("wsl"))


def _to_wsl_path(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["wsl", "wslpath", "-a", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


class SandboxRunner:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    def run_shell(
        self,
        command: str,
        *,
        mode: str = "workspace",
        cwd_rel: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        output_limit: int = DEFAULT_OUTPUT_LIMIT,
        allow_network: bool = False,
    ) -> tuple[bool, str]:
        command = (command or "").strip()
        if not command:
            return False, "没有提供命令"
        hit = _is_dangerous(command)
        if hit:
            return False, f"危险命令已被拦截：{command[:80]}"
        if mode not in VALID_MODES:
            return False, f"无效沙箱档位：{mode}"
        cwd, err = _resolve_cwd(self.workspace, cwd_rel)
        if err:
            return False, err
        assert cwd is not None

        audit_base = {
            "kind": "shell",
            "mode": mode,
            "command": command[:500],
            "cwd": str(cwd),
            "allow_network": allow_network,
        }

        if mode == "host":
            rc, out, err_out, timed_out = _run_subprocess(
                command, cwd, timeout, shell=True, env=None)
        elif mode == "workspace":
            env = _controlled_env(allow_network)
            rc, out, err_out, timed_out = _run_subprocess(
                command, cwd, timeout, shell=True, env=env)
        else:  # wsl
            if not _wsl_available():
                return False, "WSL 不可用（仅 Windows 且已安装 WSL 时支持 wsl 档位）"
            wsl_cwd = _to_wsl_path(cwd)
            if not wsl_cwd:
                return False, "无法将工作区路径转换为 WSL 路径"
            inner = command.replace("\\", "\\\\").replace('"', '\\"')
            proxy = ""
            if not allow_network:
                proxy = (
                    'export HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9; '
                )
            wsl_cmd = f'{proxy}cd "{wsl_cwd}" && {command}'
            rc, out, err_out, timed_out = _run_subprocess(
                ["wsl", "--", "bash", "-lc", wsl_cmd],
                self.workspace,
                timeout,
                shell=False,
                env=_controlled_env(allow_network),
            )

        stdout = out[:output_limit]
        stderr = err_out[:output_limit]
        _audit({**audit_base, "exit_code": rc, "timed_out": timed_out})

        if timed_out:
            return False, json.dumps({
                "error": f"沙箱执行超时（>{timeout}s）",
                "exit_code": rc,
                "partial_stdout": stdout,
                "partial_stderr": stderr,
                "mode": mode,
            }, ensure_ascii=False)
        if rc != 0:
            return False, json.dumps({
                "exit_code": rc,
                "stderr": stderr or stdout,
                "mode": mode,
            }, ensure_ascii=False)
        return True, json.dumps({
            "exit_code": 0,
            "stdout": stdout,
            "mode": mode,
            "sandbox": True,
        }, ensure_ascii=False)

    def run_code(
        self,
        *,
        code: str | None = None,
        file_rel: str | None = None,
        mode: str = "workspace",
        timeout: float = DEFAULT_TIMEOUT,
        output_limit: int = DEFAULT_OUTPUT_LIMIT,
        allow_network: bool = False,
    ) -> tuple[bool, str]:
        source = (code or "").strip()
        if file_rel:
            target = _safe_join(self.workspace, file_rel)
            if target is None or not target.is_file():
                return False, f"文件不存在或路径非法：{file_rel}"
            source = target.read_text(encoding="utf-8", errors="replace")
        if not source:
            return False, "没有可执行的代码（请提供 code 或 file）"

        sandbox_dir = workspace_data_dir(self.workspace) / "sandbox_tmp"
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            delete=False,
            dir=str(sandbox_dir),
            encoding="utf-8",
        ) as tmp:
            tmp.write(source)
            script = Path(tmp.name)

        try:
            if mode == "wsl" and _wsl_available():
                wsl_script = _to_wsl_path(script)
                if not wsl_script:
                    return False, "无法转换沙箱脚本路径到 WSL"
                cmd = ["wsl", "--", "python3", wsl_script]
                env = _controlled_env(allow_network)
                cwd = self.workspace
                shell = False
            else:
                cmd = [sys.executable, str(script)]
                env = _controlled_env(allow_network) if mode != "host" else _controlled_env(allow_network)
                if mode == "host":
                    env = _controlled_env(allow_network)
                cwd = self.workspace
                shell = False

            rc, out, err_out, timed_out = _run_subprocess(
                cmd, cwd, timeout, shell=shell, env=env)
            stdout = out[:output_limit]
            stderr = err_out[:output_limit]
            _audit({
                "kind": "code",
                "mode": mode,
                "file": file_rel,
                "chars": len(source),
                "exit_code": rc,
                "timed_out": timed_out,
                "allow_network": allow_network,
            })
            if timed_out:
                return False, json.dumps({
                    "error": f"沙箱代码执行超时（>{timeout}s）",
                    "partial_stdout": stdout,
                    "partial_stderr": stderr,
                    "mode": mode,
                }, ensure_ascii=False)
            if rc != 0:
                return False, json.dumps({
                    "exit_code": rc,
                    "stderr": stderr,
                    "mode": mode,
                }, ensure_ascii=False)
            return True, json.dumps({
                "exit_code": 0,
                "stdout": stdout,
                "mode": mode,
                "sandbox": True,
            }, ensure_ascii=False)
        finally:
            try:
                script.unlink(missing_ok=True)
            except OSError:
                pass


def run_sandboxed_shell(
    workspace: Path,
    command: str,
    *,
    mode: str | None = None,
    cwd_rel: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    output_limit: int = DEFAULT_OUTPUT_LIMIT,
    allow_network: bool = False,
) -> tuple[bool, str]:
    mode = mode or get_sandbox_default()
    if mode == "host":
        mode = "workspace"  # 显式沙箱工具不走 host，默认 workspace
    return SandboxRunner(workspace).run_shell(
        command,
        mode=mode,
        cwd_rel=cwd_rel,
        timeout=timeout,
        output_limit=output_limit,
        allow_network=allow_network,
    )


def run_sandboxed_code(
    workspace: Path,
    *,
    code: str | None = None,
    file_rel: str | None = None,
    mode: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    output_limit: int = DEFAULT_OUTPUT_LIMIT,
    allow_network: bool = False,
) -> tuple[bool, str]:
    mode = mode or get_sandbox_default()
    if mode == "host":
        mode = "workspace"
    return SandboxRunner(workspace).run_code(
        code=code,
        file_rel=file_rel,
        mode=mode,
        timeout=timeout,
        output_limit=output_limit,
        allow_network=allow_network,
    )
