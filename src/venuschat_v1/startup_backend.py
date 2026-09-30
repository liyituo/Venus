"""Desktop startup choices when the local Venus API is unavailable."""

from __future__ import annotations

import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from urllib.parse import urlsplit

from . import theme as t
from .api_client import ApiClient
from .config_store import load_config
from .connection_dialog import ConnectionDialog, activate_connection
from direct_connection import normalize_origin


REPO_ROOT = Path(__file__).resolve().parents[2]


def local_backend_base() -> str:
    """Local and remote services share the default port; never silently move it."""
    cfg = load_config()
    for key in ("llm_base", "personal_llm_base"):
        value = str(cfg.get(key) or "").rstrip("/")
        try:
            parsed = urlsplit(value)
            if parsed.scheme in ("http", "https") and parsed.hostname in ("127.0.0.1", "localhost"):
                return normalize_origin(value)
        except ValueError:
            pass
    return "http://127.0.0.1:8001"


def probe_backend(base: str, timeout: float = 2) -> bool:
    try:
        client = ApiClient(base=base)
        code, body = client.get("/api/v1/ready", timeout=timeout)
        if code == 404:
            code, body = client.get("/api/v1/health", timeout=max(6.0, timeout))
            return (code == 200 and isinstance(body, dict) and body.get("ok") is True
                    and bool(body.get("version")))
        return (code == 200 and isinstance(body, dict) and body.get("ok") is True
                and body.get("service") == "venus-llm")
    except (OSError, ValueError):
        return False


def normalize_remote_origin(raw: str) -> str:
    """Compatibility name: local and remote now use identical address rules."""
    return normalize_origin(raw)


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=.6):
            return True
    except OSError:
        return False


def _python_exe() -> Path:
    current = Path(sys.executable)
    console = current.with_name("python.exe")
    return console if console.is_file() else current


def start_local_backend(base: str) -> tuple[bool, str]:
    """Run in a worker thread; all backend packages stay outside the GUI process."""
    try:
        port = urlsplit(base).port
        if port is None:
            return False, "本机后端地址缺少端口。"
        if probe_backend(base):
            return True, "本机后端已就绪。"
        if _port_open(port):
            return False, f"本机端口 {port} 已被占用，请先检查占用程序。"
        python = _python_exe()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        required = subprocess.run(
            [str(python), "-c", "import fastapi, uvicorn, PIL, pyautogui, fastmcp, yaml"],
            cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags, check=False,
        )
        log_dir = REPO_ROOT / ".venus" / "launcher"
        log_dir.mkdir(parents=True, exist_ok=True)
        if required.returncode:
            install_log = log_dir / "backend-install.log"
            with install_log.open("ab") as output:
                installed = subprocess.run(
                    [str(python), "-m", "pip", "install", "-r", str(REPO_ROOT / "requirements.txt")],
                    cwd=REPO_ROOT, stdout=output, stderr=subprocess.STDOUT,
                    creationflags=flags, check=False,
                )
            if installed.returncode:
                return False, f"后端依赖安装失败，请查看 {install_log}。"
        server = REPO_ROOT / "src" / "llm_server.py"
        stdout_log = log_dir / f"backend-{port}.out.log"
        stderr_log = log_dir / f"backend-{port}.err.log"
        with stdout_log.open("ab") as stdout, stderr_log.open("ab") as stderr:
            process = subprocess.Popen(
                [str(python), "-u", str(server), "--host", "127.0.0.1", "--port", str(port)],
                cwd=REPO_ROOT, stdout=stdout, stderr=stderr, creationflags=flags,
            )
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            if probe_backend(base, timeout=2):
                return True, "本机后端已启动。"
            if process.poll() is not None:
                break
            time.sleep(.7)
        return False, f"本机后端未就绪，请查看 {stderr_log}。"
    except (OSError, ValueError) as exc:
        return False, f"本机后端启动失败：{exc}"


class StartupBackendDialog:
    def __init__(self, app, base: str) -> None:
        self.app = app
        self.base = base
        self.closed = False
        self._results: queue.Queue = queue.Queue()
        self.window = tk.Toplevel(app.root)
        self.window.title("连接 Venus 后端")
        self.window.configure(bg=t.WINDOW)
        self.window.resizable(False, False)
        self.window.transient(app.root)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        body = tk.Frame(self.window, bg=t.WINDOW, padx=28, pady=24)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="当前服务器无法连接", bg=t.WINDOW, fg=t.INK,
                 font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        tk.Label(body, text=f"当前地址：{app.client.base}\n可以启动本机服务，或配置服务器地址、TLS 和密码。",
                 bg=t.WINDOW, fg=t.INK_SOFT, justify="left",
                 font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(12, 18))

        buttons = tk.Frame(body, bg=t.WINDOW)
        buttons.pack(fill="x")
        self.local_button = tk.Button(buttons, text="启动本机后端", command=self.start_local)
        self.local_button.pack(side="left", padx=(0, 8))
        self.remote_button = tk.Button(buttons, text="配置服务器连接", command=self.show_remote)
        self.remote_button.pack(side="left", padx=(0, 8))
        tk.Button(buttons, text="暂时离线打开", command=self.close).pack(side="left")

        self.status = tk.Label(body, text="", bg=t.WINDOW, fg=t.INK_SOFT,
                               wraplength=470, justify="left")
        self.status.pack(anchor="w", pady=(15, 0))
        self.window.update_idletasks()
        width, height = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        x = app.root.winfo_rootx() + max(0, (app.root.winfo_width() - width) // 2)
        y = app.root.winfo_rooty() + max(0, (app.root.winfo_height() - height) // 2)
        self.window.geometry(f"+{x}+{y}")
        self.window.grab_set()
        self.window.lift()
        self.window.after(100, self._poll_result)

    def close(self) -> None:
        self.closed = True
        self.window.destroy()

    def show_remote(self) -> None:
        self.close()
        ConnectionDialog(self.app)

    def _activate(self, info: dict) -> None:
        self.close()
        activate_connection(self.app, self.base, info)

    def start_local(self) -> None:
        self.local_button.configure(state="disabled")
        self.remote_button.configure(state="disabled")
        self.status.configure(text="正在准备并启动本机后端，请稍候…")

        def work() -> None:
            success, message = start_local_backend(self.base)
            if success:
                code, info = ApiClient(self.base).get("/api/v1/ready", timeout=4)
                self._results.put((code == 200, info if code == 200 else "本机后端连接失败"))
            else:
                self._results.put((False, message))

        threading.Thread(target=work, daemon=True, name="venus-start-local-backend").start()

    def _poll_result(self) -> None:
        if self.closed:
            return
        try:
            success, message = self._results.get_nowait()
        except queue.Empty:
            self.window.after(100, self._poll_result)
            return
        if success:
            self._activate(message)
        else:
            self.status.configure(text=message)
            self.local_button.configure(state="normal")
            self.remote_button.configure(state="normal")
            self.window.after(100, self._poll_result)


def show_startup_backend_prompt(app) -> None:
    # Always probe the selected connection; a remote-only client does not need
    # a second local server. Launcher environment hints are no longer needed.
    os.environ.pop("VENUS_STARTUP_LOCAL_BASE", None)
    os.environ.pop("VENUS_STARTUP_LOCAL_STATE", None)
    base = app.client.base
    results: queue.Queue[bool] = queue.Queue(maxsize=1)

    def probe() -> None:
        results.put(probe_backend(base))

    def finish_probe() -> None:
        try:
            ready = results.get_nowait()
        except queue.Empty:
            app.root.after(100, finish_probe)
            return
        if not ready:
            StartupBackendDialog(app, local_backend_base())

    threading.Thread(target=probe, daemon=True, name="venus-probe-backend").start()
    app.root.after(100, finish_probe)
