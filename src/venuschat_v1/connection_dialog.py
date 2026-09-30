"""One address/port dialog for local and remote servers."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog
from urllib.parse import urlsplit

from direct_connection import normalize_origin
from .api_client import ApiClient
from .connection_store import connection_options, connection_password, save_connection
from .config_store import load_config


def activate_connection(app, base: str, info: dict) -> None:
    if info.get("mode") == "team":
        joined = next((row for row in load_config().get("team_connections", [])
                       if row.get("origin") == base and row.get("status") == "joined"), None)
        if not joined:
            app.show_settings_page("team")
            field = app.settings_view._team_fields.get("Hub 地址")
            if field is not None:
                field.set(base)
            app.toast("服务器已连接，请在此加入或初始化团队")
            return
        label = str(joined.get("team_name") or "团队 Hub")
    else:
        app._personal_backend_base = base
        label = "个人对话"
    if app.client.base.rstrip("/") == base:
        # Reload TLS trust settings even when the origin has not changed.
        app.client = ApiClient(base)
        app.bridge.refresh_all()
    else:
        app.switch_backend_context(base, label)


class ConnectionDialog:
    def __init__(self, app, base: str = "", *, on_connected=None) -> None:
        self.app = app
        self.on_connected = on_connected or (lambda origin, info: activate_connection(app, origin, info))
        self.results: queue.Queue = queue.Queue()
        self.closed = False
        initial = base or app.client.base or "http://127.0.0.1:8001"
        parsed = urlsplit(initial)
        options = connection_options(initial)
        self.window = tk.Toplevel(app.root)
        self.window.title("连接 Venus 服务器")
        self.window.transient(app.root)
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        body = tk.Frame(self.window, padx=24, pady=20)
        body.pack(fill="both", expand=True)
        self.host = tk.StringVar(value=parsed.hostname or "127.0.0.1")
        self.port = tk.StringVar(value=str(parsed.port or 8001))
        self.tls = tk.BooleanVar(value=parsed.scheme == "https")
        self.password_enabled = tk.BooleanVar(value=bool(options.get("password_enabled")))
        self.password = tk.StringVar()
        self.ca_file = tk.StringVar(value=str(options.get("ca_file") or ""))
        for row, (label, variable, secret) in enumerate((
                ("服务器 IP / 域名", self.host, False), ("端口", self.port, False),
                ("连接密码", self.password, True), ("信任证书（可选 PEM）", self.ca_file, False))):
            tk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=6)
            entry = tk.Entry(body, textvariable=variable, width=36, show="*" if secret else "")
            entry.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=6)
        tk.Button(body, text="选择…", command=self.choose_ca).grid(row=3, column=2, padx=(6, 0))
        tk.Checkbutton(body, text="TLS 加密（HTTPS）", variable=self.tls,
                       command=self.update_hint).grid(row=4, column=0, columnspan=2, sticky="w")
        tk.Checkbutton(body, text="使用连接密码", variable=self.password_enabled,
                       command=self.update_hint).grid(row=5, column=0, columnspan=2, sticky="w")
        self.hint = tk.Label(body, wraplength=470, justify="left", anchor="w")
        self.hint.grid(row=6, column=0, columnspan=3, sticky="w", pady=8)
        self.status = tk.Label(body, text="", wraplength=470, justify="left", anchor="w")
        self.status.grid(row=7, column=0, columnspan=3, sticky="w")
        self.connect_button = tk.Button(body, text="连接并保存", command=self.connect)
        self.connect_button.grid(row=8, column=1, sticky="e", pady=(12, 0))
        self.update_hint()
        self.window.grab_set()
        self.window.after(100, self.poll)

    def update_hint(self) -> None:
        transport = ("TLS 已启用，将验证服务器证书；自签名证书可在上方选择。" if self.tls.get()
                     else "TLS 未启用：HTTP 不加密传输，连接密码也不提供传输加密。")
        self.hint.configure(text=transport + "\n已保存的密码可留空保留；取消密码勾选可清除。")

    def choose_ca(self) -> None:
        path = filedialog.askopenfilename(parent=self.window, title="选择可信服务器或 CA 证书",
                                         filetypes=[("PEM 证书", "*.pem *.crt"), ("全部文件", "*")])
        if path:
            self.ca_file.set(path)

    def close(self) -> None:
        self.closed = True
        self.window.destroy()

    def connect(self) -> None:
        if self.app.bridge._streaming or self.app.chat_view._creating_session:
            self.status.configure(text="请先完成或停止当前对话，再修改连接。")
            return
        try:
            port = int(self.port.get())
            host = self.host.get().strip()
            if "://" in host:
                raise ValueError("地址栏只填写 IP 或域名；端口和 TLS 在下方设置。")
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            base = normalize_origin(f"{host}:{port}", tls=self.tls.get())
            enabled = self.password_enabled.get()
            password = (self.password.get() or connection_password(base)) if enabled else ""
            if enabled and not password:
                raise ValueError("请填写连接密码。")
            ca_file = self.ca_file.get().strip()
        except (ValueError, OSError) as exc:
            self.status.configure(text=str(exc))
            return
        self.connect_button.configure(state="disabled")
        self.status.configure(text="正在连接…")

        def work():
            try:
                client = ApiClient(base, password=password, ca_file=ca_file)
                code, info = client.get("/api/v1/ready", timeout=6)
                if code != 200 or info.get("service") != "venus-llm" or info.get("ok") is not True:
                    self.results.put((False, str(info.get("detail") or "此地址不是可用的 Venus 服务器")))
                    return
                if self.closed:
                    return
                save_connection(base, password_enabled=enabled, password=password, ca_file=ca_file)
                self.results.put((True, (base, info)))
            except Exception as exc:
                self.results.put((False, str(exc)))
        threading.Thread(target=work, daemon=True, name="venus-connect").start()

    def poll(self) -> None:
        if self.closed:
            return
        try:
            success, result = self.results.get_nowait()
        except queue.Empty:
            self.window.after(100, self.poll)
            return
        self.connect_button.configure(state="normal")
        if success:
            self.close()
            self.on_connected(*result)
        else:
            self.status.configure(text=result)
            self.window.after(100, self.poll)
