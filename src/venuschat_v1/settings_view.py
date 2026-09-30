"""Classical-minimal settings center for VenusChat V1."""

from __future__ import annotations

import json
import secrets
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox
import urllib.error
import urllib.request
from urllib.parse import urlparse

from . import theme as t
from .api_client import ApiClient, installation_code_confirmation
from .connection_dialog import ConnectionDialog
from .config_store import (
    clear_team_device_token,
    delete_team_claim_secret,
    load_config,
    normalize_team_origin,
    personal_backend_for_team,
    save_local_config,
    save_team_claim_secret,
    save_team_connection,
    team_claim_secret,
    team_connection,
    team_token_for_connection,
)
from .project_hub_view import open_project_hub
from .terminal_identity import get_installation_code
from .widgets import (
    Dot,
    FlatButton,
    MinimalField,
    NavRow,
    ScrollArea,
    SearchField,
    SegmentedControl,
    SelectField,
    Switch,
    kicker,
    separator,
)


NAV_GROUPS = (
    (
        "基础",
        (
            ("common", "常规与工作区"),
            ("model", "模型与推理"),
            ("vision", "视觉模型"),
        ),
    ),
    (
        "AGENT",
        (
            ("permissions", "权限与确认"),
            ("sandbox", "执行沙箱"),
            ("router", "工具路由"),
            ("subagents", "子 Agent"),
        ),
    ),
    (
        "能力",
        (
            ("memory", "记忆与 Skill"),
            ("codegraph", "CodeGraph"),
            ("integrations", "MCP 与浏览器"),
            ("extensions", "扩展"),
        ),
    ),
    (
        "服务",
        (
            ("projects", "项目中心"),
            ("team", "团队与成员"),
            ("diagnostics", "诊断与用量"),
            ("advanced", "高级"),
        ),
    ),
)

EDITABLE_PAGES = {"common", "model", "vision", "permissions", "sandbox", "router", "memory", "advanced"}


PAGE_META = {
    "common": ("常规与工作区", "选择新任务默认使用的工作区"),
    "model": ("模型与推理", "配置主模型连接，可一键拉取可用模型并测试"),
    "vision": ("视觉模型", "为图像理解与视觉任务配置独立模型"),
    "permissions": ("权限与确认", "定义工具操作的确认范围与执行上限"),
    "sandbox": ("执行沙箱", "选择本机、工作区隔离或 WSL 执行环境"),
    "router": ("工具路由", "配置轻量模型进行工具预路由"),
    "subagents": ("子 Agent", "查看后端当前可调用的 Agent"),
    "memory": ("记忆与 Skill", "控制分层记忆、自动提取与动态 Skill"),
    "codegraph": ("CodeGraph", "管理代码符号索引与影响分析"),
    "integrations": ("MCP 与浏览器", "查看外部工具连接与浏览器状态"),
    "extensions": ("扩展", "管理本地扩展、Skill 与工具资产"),
    "diagnostics": ("诊断与用量", "查看运行健康、上下文与 Token 用量"),
    "advanced": ("高级", "配置本地或远程服务器地址"),
    "team": ("团队与成员", "邀请成员、申请加入、管理设备和查看信任状态"),
    "projects": ("项目中心", "管理当前 Hub 的项目、认领、邀请与成员授权"),
}


GENERIC_PAGES = {
    "common": (
        (
            "工作区",
            "切换后旧工作区任务会停止，请填写已存在的绝对路径。",
            (
                ("field", "默认工作区", "", "绝对路径，须存在"),
            ),
        ),
    ),
    "vision": (
        (
            "视觉模型",
            "图像理解使用独立连接，避免影响主对话模型。",
            (
                ("field", "Vision URL", "", "https://api.vision-provider.com/v1"),
                ("secret", "Vision Key", "", "输入 Vision API Key"),
                ("select", "Vision Model", ("选择或输入模型", "qwen-vl-max", "gpt-vision"), "选择或输入模型"),
            ),
        ),
    ),
    "permissions": (
        (
            "确认策略",
            "高风险操作始终需要明确确认。",
            (
                ("segment", "默认模式", ("智能", "严格", "只读"), "智能"),
            ),
        ),
    ),
    "sandbox": (
        (
            "默认执行环境",
            "新任务会继承此环境设置。",
            (
                ("segment", "环境", ("工作区", "本机", "WSL"), "工作区"),
                ("info", "工作区隔离", "已启用", t.SUCCESS),
                ("info", "WSL 可用性", "等待检测", t.WARNING),
            ),
        ),
    ),
    "router": (
        (
            "本地工具路由",
            "使用轻量模型判断是否需要调用工具。",
            (
                ("switch", "启用工具路由", "减少无需工具的上下文注入", True),
                ("field", "Ollama URL", "http://127.0.0.1:11434", "本地服务地址"),
                ("select", "路由模型", ("qwen2.5:3b", "qwen2.5:7b", "关闭"), "qwen2.5:3b"),
            ),
        ),
    ),
    "subagents": (
        (
            "可用 Agent",
            "来自后端 agents 目录与子 Agent 路由。",
            (
                ("info", "可用 Agent", "读取中", t.INK_MUTED),
            ),
        ),
    ),
    "memory": (
        (
            "分层记忆",
            "L0–L3 记忆与动态 Skill，数据来自后端。",
            (
                ("switch", "启用记忆", "允许从历史中检索相关内容", True),
                ("switch", "自动提取", "在对话结束后提取可复用记忆", False),
                ("info", "原子记忆", "—", t.INK_SOFT),
                ("info", "长期画像", "—", t.INK_SOFT),
                ("info", "动态 Skill", "—", t.INK_SOFT),
                ("action", "刷新统计", "读取最新后端健康统计", "刷新"),
            ),
        ),
    ),
    "codegraph": (
        (
            "代码索引",
            "符号索引用于定位定义、调用与影响范围。",
            (
                ("info", "索引状态", "读取中", t.INK_MUTED),
                ("info", "文件", "—", t.INK_SOFT),
                ("info", "符号", "—", t.INK_SOFT),
                ("action", "重建索引", "清理并重新生成当前工作区索引", "立即重建"),
            ),
        ),
    ),
    "integrations": (
        (
            "外部能力",
            "MCP 与浏览器工具连接状态。",
            (
                ("info", "浏览器 MCP", "检测中", t.INK_MUTED),
                ("info", "MCP 服务", "检测中", t.INK_MUTED),
                ("action", "刷新连接", "重新读取 MCP / 浏览器状态", "刷新"),
            ),
        ),
    ),
    "extensions": (
        (
            "本地扩展",
            "插件 catalog 与启用状态。",
            (
                ("info", "扩展数量", "读取中", t.INK_MUTED),
                ("action", "刷新扩展", "读取 /api/v1/extensions", "刷新列表"),
            ),
        ),
    ),
    "diagnostics": (
        (
            "运行概览",
            "health / usage / diagnostics 聚合。",
            (
                ("info", "模型服务", "—", t.INK_MUTED),
                ("info", "版本", "—", t.INK_SOFT),
                ("info", "本次用量", "—", t.INK_SOFT),
                ("action", "刷新诊断", "读取后端诊断摘要", "刷新"),
            ),
        ),
    ),
    "advanced": (
        (
            "本地服务",
            "本地服务地址；保存后写入 chat_config.json。",
            (
                ("field", "Desktop URL", "http://127.0.0.1:8000", "本地地址"),
                ("field", "Agent URL", "http://127.0.0.1:8001", "本地或远程地址"),
            ),
        ),
    ),
}


class SettingsView(tk.Frame):
    """A complete settings frontend sharing the V1 global application shell."""

    @property
    def client(self) -> ApiClient:
        # During bridge work, app.client resolves to the request's captured
        # origin via BackendBridge's thread-local context.
        return self.app.client

    @client.setter
    def client(self, value: ApiClient) -> None:
        self._initial_client = value

    def __init__(self, parent: tk.Misc, app, fonts: t.Fonts, client: ApiClient) -> None:
        super().__init__(parent, bg=t.CANVAS)
        self.app = app
        self.fonts = fonts
        self.client = client
        self.page_keys = frozenset(PAGE_META)
        self.active_page = "model"
        self.nav_rows: dict[str, NavRow] = {}
        self.local_controls: dict[str, object] = {}
        self._dynamic_info: dict[str, tk.Label] = {}
        self._drafts: dict[str, dict[str, object]] = {}
        self._dirty_pages: set[str] = set()
        self._applying_data = False
        self._page_ready = False
        self._load_serial = 0
        self._load_pending = False
        self._load_failed = False
        self._save_serial = 0
        self._saving = False
        self._action_busy = False
        self._needs_reload = False
        try:
            self._installation_code = get_installation_code()
            self._installation_code_error = ""
        except Exception as exc:
            self._installation_code = ""
            self._installation_code_error = str(exc)
        self._team_fields: dict[str, object] = {}
        self._team_action_buttons: list[FlatButton] = []
        self._team_state = "local_only"
        self._team_primary_button: FlatButton | None = None
        self._team_confirmed: dict | None = None
        self._team_apps: list[dict] = []
        self._team_invites: list[dict] = []
        self._team_members: list[dict] = []
        self._team_app_rows: list[dict] = []
        self._team_invite_rows: list[dict] = []
        self._team_member_rows: list[dict] = []
        self._team_status_label: tk.Label | None = None
        self._team_hub_label: tk.Label | None = None
        self._team_identity_label: tk.Label | None = None
        self._team_apps_list: tk.Listbox | None = None
        self._team_invites_list: tk.Listbox | None = None
        self._team_members_list: tk.Listbox | None = None
        self._project_hub_window = None
        self._build()

    # Layout ----------------------------------------------------------------
    def _build(self) -> None:
        self.columnconfigure(0, weight=0, minsize=t.s(310))
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self.sidebar = tk.Frame(self, bg=t.SIDEBAR, width=t.s(310))
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.pack_propagate(False)
        separator(self, vertical=True, color=t.LINE).grid(row=0, column=0, sticky="nse")
        self._build_sidebar()

        right = tk.Frame(self, bg=t.CANVAS)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        content = tk.Frame(right, bg=t.CANVAS)
        content.grid(row=0, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=0)
        content.rowconfigure(0, weight=1)

        self.stage = tk.Frame(content, bg=t.CANVAS)
        self.stage.grid(row=0, column=0, sticky="nsew")
        self.stage.columnconfigure(0, weight=1)
        self.stage.rowconfigure(1, weight=1)

        heading = tk.Frame(self.stage, bg=t.CANVAS, height=t.s(84))
        heading.grid(row=0, column=0, sticky="ew", padx=(t.s(30), t.s(24)))
        heading.pack_propagate(False)
        self.page_title = tk.Label(
            heading,
            text="",
            bg=t.CANVAS,
            fg=t.INK,
            font=self.fonts.display_lg,
        )
        self.page_title.pack(anchor="w", pady=(t.s(16), t.s(1)))
        self.page_subtitle = tk.Label(
            heading,
            text="",
            bg=t.CANVAS,
            fg=t.INK_MUTED,
            font=self.fonts.small,
        )
        self.page_subtitle.pack(anchor="w", pady=(t.s(2), 0))

        self.page_scroll = ScrollArea(self.stage, bg=t.CANVAS, scrollbar=True)
        self.page_scroll.grid(row=1, column=0, sticky="nsew",
                              padx=(t.s(26), t.s(18)), pady=(0, t.s(4)))

        self.rail = tk.Frame(content, bg=t.CANVAS, width=t.s(296))
        self.rail.grid(row=0, column=1, sticky="nsew",
                       padx=(0, t.s(20)), pady=(t.s(22), t.s(18)))
        self.rail.pack_propagate(False)
        separator(self.rail, vertical=True, color=t.LINE).pack(side="left", fill="y")
        self._build_capability_rail()

        footer = tk.Frame(right, bg=t.HEADER, height=t.s(72))
        footer.grid(row=1, column=0, sticky="ew")
        footer.pack_propagate(False)
        separator(footer, color=t.LINE).pack(fill="x")
        actions = tk.Frame(footer, bg=t.HEADER)
        actions.pack(side="right", padx=t.s(26), pady=t.s(15))
        FlatButton(
            actions,
            "取消",
            self._cancel,
            font=self.fonts.body,
            variant="ghost",
            height=40,
            min_width=112,
            parent_bg=t.HEADER,
        ).pack(side="left", padx=(0, 10))
        self.save_button = FlatButton(
            actions,
            "保存本页",
            self._save,
            font=self.fonts.body_medium,
            variant="primary",
            height=40,
            min_width=160,
            parent_bg=t.HEADER,
        )
        self.save_button.pack(side="left")
        self.footer_hint = tk.Label(
            footer,
            text="更改当前页面后保存",
            bg=t.HEADER,
            fg=t.INK_MUTED,
            font=self.fonts.caption,
        )
        self.footer_hint.pack(side="left", padx=t.s(28), pady=t.s(25))

        self.bind("<Configure>", self._responsive_rail, add="+")
        # Build the hidden page without putting its network read ahead of the
        # chat bootstrap. The first visit fetches current settings.
        self.select_page("model", refresh=False)
        self._needs_reload = True

    def _build_sidebar(self) -> None:
        FlatButton(self.sidebar, "连接服务器 · TLS / 密码",
                   lambda: ConnectionDialog(self.app), font=self.fonts.small,
                   variant="outline", parent_bg=t.SIDEBAR).pack(fill="x", padx=t.s(24), pady=t.s(12))
        tk.Label(
            self.sidebar,
            text="设置中心",
            bg=t.SIDEBAR,
            fg=t.INK,
            font=self.fonts.display_lg,
        ).pack(anchor="w", padx=t.s(26), pady=(t.s(20), t.s(12)))
        self.search = SearchField(
            self.sidebar,
            font=self.fonts.small,
            placeholder="搜索设置…",
        )
        self.search.pack(fill="x", padx=t.s(24), pady=(0, t.s(12)))
        self.search.variable.trace_add("write", lambda *_args: self._rebuild_navigation())
        self.nav_scroll = ScrollArea(self.sidebar, bg=t.SIDEBAR, scrollbar=False)
        self.nav_scroll.pack(fill="both", expand=True,
                             padx=(t.s(10), t.s(8)), pady=(0, t.s(12)))
        self._rebuild_navigation()

    def _rebuild_navigation(self) -> None:
        for child in self.nav_scroll.inner.winfo_children():
            child.destroy()
        self.nav_rows.clear()
        query = self.search.get().strip().casefold() if hasattr(self, "search") else ""
        found = False
        for group_title, items in NAV_GROUPS:
            matches = [item for item in items if not query or any(
                query in part.casefold() for part in (item[0], item[1], PAGE_META[item[0]][1])
            )]
            if not matches:
                continue
            found = True
            group = tk.Frame(self.nav_scroll.inner, bg=t.SIDEBAR)
            group.pack(fill="x", pady=(t.s(3), t.s(7)))
            kicker(
                group,
                group_title,
                font=self.fonts.kicker,
                bg=t.SIDEBAR,
                fg=t.INK_FAINT,
            ).pack(anchor="w", padx=t.s(13), pady=(t.s(2), t.s(5)))
            for key, label in matches:
                row = NavRow(
                    group,
                    label,
                    lambda page=key: self.select_page(page),
                    font=self.fonts.small,
                    height=37,
                    bg=t.SIDEBAR,
                )
                row.pack(fill="x", pady=1)
                row.set_active(key == self.active_page)
                self.nav_rows[key] = row
        if not found:
            tk.Label(self.nav_scroll.inner, text="没有匹配的设置", bg=t.SIDEBAR,
                     fg=t.INK_MUTED, font=self.fonts.small).pack(anchor="w", padx=t.s(20), pady=t.s(16))

    def _build_capability_rail(self) -> None:
        body = tk.Frame(self.rail, bg=t.CANVAS)
        body.pack(side="left", fill="both", expand=True, padx=(t.s(25), t.s(2)))
        kicker(
            body,
            "后端能力",
            font=self.fonts.kicker,
            bg=t.CANVAS,
            fg=t.INK_SOFT,
        ).pack(anchor="w", pady=(t.s(4), t.s(16)))
        separator(body, color=t.LINE).pack(fill="x", pady=(0, t.s(10)))
        statuses = (
            ("记忆系统", "—", t.INK_MUTED),
            ("工具路由", "—", t.INK_MUTED),
            ("MCP 服务", "—", t.INK_MUTED),
            ("执行沙箱", "—", t.INK_MUTED),
        )
        for name, value, color in statuses:
            row = tk.Frame(body, bg=t.CANVAS, height=t.s(44))
            row.pack(fill="x")
            row.pack_propagate(False)
            tk.Label(
                row,
                text=name,
                bg=t.CANVAS,
                fg=t.INK_SOFT,
                font=self.fonts.small,
            ).pack(side="left")
            right = tk.Frame(row, bg=t.CANVAS)
            right.pack(side="right")
            if color == t.SUCCESS:
                Dot(right, color=t.SUCCESS, size=6, bg=t.CANVAS).pack(
                    side="left", padx=(0, t.s(6)))
            tk.Label(
                right,
                text=value,
                bg=t.CANVAS,
                fg=color,
                font=self.fonts.caption,
            ).pack(side="left")
        FlatButton(
            body,
            "查看诊断  ›",
            lambda: self.select_page("diagnostics"),
            font=self.fonts.small,
            variant="ghost",
            height=38,
            anchor="w",
            parent_bg=t.CANVAS,
        ).pack(fill="x", pady=(t.s(14), 0))

    # Page routing -----------------------------------------------------------
    def select_page(self, key: str, *, refresh: bool = True) -> None:
        if key not in PAGE_META:
            return
        self._needs_reload = False
        if self._page_ready and self.active_page in self._dirty_pages:
            self._drafts[self.active_page] = self._snapshot_controls()
        self._page_ready = False
        self._load_pending = False
        self._load_failed = False
        self.active_page = key
        title, subtitle = PAGE_META[key]
        self.page_title.configure(text=title)
        self.page_subtitle.configure(text=subtitle)
        for row_key, row in self.nav_rows.items():
            row.set_active(row_key == key)
        for child in self.page_scroll.inner.winfo_children():
            child.destroy()
        self.local_controls.clear()
        self._dynamic_info.clear()
        if key == "model":
            self._build_model_page()
        elif key == "projects":
            self._build_projects_page()
        elif key == "team":
            self._build_team_page()
        else:
            self._build_generic_page(key)
        draft = self._drafts.get(key)
        if draft:
            self._apply_values(draft)
        self._page_ready = True
        self._update_footer()
        self.page_scroll.scroll_top()
        self.app.set_header_section("settings")
        if refresh:
            self._refresh_page_data(key)

    def _refresh_page_data(self, key: str) -> None:
        if key == "team":
            self._refresh_team_state()
            return
        if key == "projects":
            return
        paths = {
            "model": ("/api/v1/config",),
            "vision": ("/api/v1/config",),
            "router": ("/api/v1/config",),
            "memory": ("/api/v1/config", "/api/v1/health"),
            "common": ("/api/v1/workspace",),
            "permissions": ("/api/v1/confirm-mode",),
            "sandbox": ("/api/v1/sandbox/status",),
            "codegraph": ("/api/v1/codegraph/stats",),
            "integrations": ("/api/v1/browser/status", "/api/v1/mcp/status"),
            "extensions": ("/api/v1/extensions",),
            "diagnostics": ("/api/v1/health", "/api/v1/usage"),
            "subagents": ("/api/v1/agents",),
        }.get(key, ())
        self._load_serial += 1
        serial = self._load_serial
        if not paths:
            self._load_pending = False
            self._apply_page_data(key, {"local": (200, load_config())})
            return
        self._load_pending = True
        self.footer_hint.configure(text="正在读取设置…")
        if key in EDITABLE_PAGES:
            self.save_button.set_enabled(False)
        def job():
            try:
                return (serial, key, {path: self.client.get(path, timeout=6) for path in paths})
            except Exception as exc:
                return (serial, key, {"request": (0, {"detail": str(exc)})})
        self.app.bridge.submit("settings_load", job)

    def handle_backend(self, kind: str, payload) -> None:
        if kind == "settings_load":
            if not isinstance(payload, tuple) or len(payload) != 3:
                self._load_pending = False
                self.footer_hint.configure(text="读取失败，请重试")
                detail = payload[1] if isinstance(payload, tuple) and len(payload) == 2 else payload
                self.app.toast(f"读取设置失败：{detail}", duration=4200)
                return
            serial, key, results = payload
            if serial == self._load_serial and key == self.active_page:
                self._load_pending = False
                self._apply_page_data(key, results)
                if key in EDITABLE_PAGES:
                    self.save_button.set_enabled(not self._saving and
                                                 key in self._dirty_pages)
        elif kind == "settings_save":
            self._handle_save_result(payload)
        elif kind == "settings_action":
            self._handle_action_result(payload)
        elif kind == "settings_team":
            self._handle_team_result(payload)

    def _handle_team_result(self, payload) -> None:
        self._action_busy = False
        self._restore_team_action_buttons()
        self.footer_hint.configure(text="团队请求已完成")
        if isinstance(payload, tuple) and payload and payload[0] == "error":
            self._set_team_status("连接失败", t.WARNING)
            self.app.toast(self._friendly_team_error(0, {"detail": payload[1]}), duration=5000)
            return
        if not isinstance(payload, tuple) or len(payload) != 2:
            self.app.toast("团队操作返回格式异常", duration=4200)
            return
        action, result = payload
        if not isinstance(result, dict):
            self.app.toast("团队操作返回格式异常", duration=4200)
            return
        if action == "admin_refresh":
            self._apply_team_admin_result(result)
            return
        code = int(result.get("code") or 0)
        data = result.get("data") or {}
        if action == "check":
            origin = str(result.get("origin") or "")
            if code == 200:
                self._team_confirmed = {"origin": origin, "team": data,
                                        "tailscale_login": data.get("tailscale_login") or ""}
                save_local_config({"team_last_origin": origin})
                name = data.get("team_name") or ("未初始化" if data.get("initialized") is False else "Venus 团队")
                team_id = data.get("team_id") or ""
                record = team_connection(origin, str(team_id))
                self._render_team_identity(origin, data, record)
                if record and record.get("device_id"):
                    self._set_team_status("已加入", t.SUCCESS)
                elif record and record.get("status") == "pending":
                    self._set_team_status("申请待批准", t.WARNING)
                elif record and record.get("status") == "approved":
                    self._set_team_status("已批准，等待领取设备凭证", t.SUCCESS)
                else:
                    self._set_team_status("已连接未初始化" if data.get("initialized") is False
                                          else "已连接未加入",
                                          t.WARNING if data.get("initialized") is False
                                          else t.INK_SOFT)
                self.app.toast(f"已核对团队 Hub：{name}")
            else:
                self._team_confirmed = None
                self._set_team_status("连接失败", t.WARNING)
                self.app.toast(self._friendly_team_error(code, data), duration=5000)
            return
        if action == "refresh":
            origin = str(result.get("origin") or "")
            public_code = int(result.get("public_code") or 0)
            public = result.get("public") or {}
            if public_code != 200:
                self._set_team_status("未连接" if public_code == 0 else "Hub 未就绪", t.WARNING)
                self.app.toast(self._friendly_team_error(public_code, public), duration=4500)
                return
            self._team_confirmed = {"origin": origin, "team": public,
                                    "tailscale_login": public.get("tailscale_login") or ""}
            record = result.get("record") or team_connection(
                origin, str(public.get("team_id") or "")) or {}
            self._render_team_identity(origin, public, record)
            if public.get("initialized") is False:
                self._set_team_status("已连接未初始化", t.WARNING)
                self.app.toast("Hub 已核对，请在 Hub 本机初始化团队")
                return
            if result.get("application_code") is not None:
                app_code = int(result.get("application_code") or 0)
                application = result.get("application") or {}
                app_data = application.get("application") or {}
                if app_code == 200 and record and app_data.get("status"):
                    saved_status = str(app_data.get("status"))
                    installation_code = (str(app_data.get("installation_code") or "")
                                          or str(record.get("installation_code") or ""))
                    binding, bound = installation_code_confirmation(
                        application, installation_code)
                    if not binding:
                        binding = str(record.get("installation_code_binding") or "")
                    if saved_status == "approved":
                        if app_data.get("claim_used"):
                            saved_status = "claimed"
                        elif not application.get("claim_available"):
                            saved_status = "invalidated"
                    record = save_team_connection({**record, "status": saved_status,
                                                   "installation_code": installation_code or None,
                                                   "installation_code_binding": binding or None})
                    self._render_team_identity(origin, public, record)
                    if (app_data.get("status") == "rejected"
                            or app_data.get("claim_used")
                            or (app_data.get("status") == "approved"
                                and not application.get("claim_available"))):
                        delete_team_claim_secret(origin,
                                                 str(record.get("application_id") or ""))
                if app_code != 200:
                    self._set_team_status("申请状态暂不可用", t.WARNING)
                    self.app.toast(self._friendly_team_error(app_code, application), duration=4500)
                elif app_data.get("status") == "pending":
                    self._set_team_status("申请待批准", t.WARNING)
                    self.app.toast("加入申请仍在等待管理员审核")
                elif app_data.get("status") == "rejected":
                    self._set_team_status("申请被拒绝", t.WARNING)
                    self.app.toast("申请被拒绝；请联系管理员重新邀请", duration=5000)
                elif app_data.get("status") == "approved" and result.get("application", {}).get("claim_available"):
                    self._set_team_status("已批准，等待领取设备凭证", t.SUCCESS)
                    self.app.toast("申请已批准，请点击“领取已批准的设备凭证并连接”")
                elif app_data.get("status") == "approved" and app_data.get("claim_used"):
                    self._set_team_status("领取已完成；如凭证丢失请联系管理员重新邀请", t.WARNING)
                elif app_data.get("status") == "approved":
                    self._set_team_status("批准的设备申请已失效", t.WARNING)
                    self.app.toast("该设备申请已被撤销或成员已停用，请联系管理员重新邀请", duration=5000)
                return
            if result.get("identity_code") is not None:
                identity_code = int(result.get("identity_code") or 0)
                identity = result.get("identity") or {}
                if identity_code == 200:
                    self._set_team_status("已加入", t.SUCCESS)
                    team_public = result.get("public") or {}
                    self._render_team_identity(origin, team_public, record, identity)
                    if identity.get("user"):
                        save_team_connection({**record, "status": "joined"})
                    device_rows = (result.get("devices") or {}).get("devices") or []
                    self._team_member_rows = [{"kind": "device", **row}
                                              for row in device_rows]
                    if self._team_members_list is not None:
                        self._team_members_list.delete(0, "end")
                        for row in device_rows:
                            self._team_members_list.insert(
                                "end", f"本人成员设备 · {row.get('status')} · "
                                f"{row.get('name')} · {row.get('id')}")
                    self.app.toast(f"已连接团队：{public.get('team_name') or ''}")
                    if (identity.get("user") or {}).get("role") == "admin":
                        self._refresh_team_admin()
                else:
                    detail = self._friendly_team_error(identity_code, identity)
                    server_detail = str(identity.get("detail") or "")
                    if (identity_code == 401 and
                            ("此设备凭证已撤销" in server_detail or "团队成员已停用" in server_detail)):
                        clear_team_device_token(origin, str(record.get("team_id") or ""))
                        record = save_team_connection({**record, "device_id": "",
                                                       "status": "revoked"})
                    self._set_team_status("已停用" if "已停用" in detail else "凭证失效", t.WARNING)
                    self.app.toast(detail, duration=5000)
                return
            self._set_team_status("已连接未加入", t.INK_SOFT)
            self.app.toast("此 Hub 已核对；输入管理员提供的邀请码后申请加入")
            return
        if action == "join":
            if code == 200:
                application = data.get("application") or {}
                self._team_fields.get("邀请码") and self._team_fields["邀请码"].set("")
                self._set_team_status("申请待批准", t.WARNING)
                record = result.get("record") or {}
                self._render_team_identity(
                    str(result.get("origin") or ""), result.get("team"), record)
                confirmation = ("Hub 已确认绑定此 VI 码" if result.get("installation_code_bound")
                                else "Hub 未确认绑定此 VI 码（可能为旧协议）")
                self.app.toast(f"申请已提交；{confirmation}；编号 {application.get('id') or ''}",
                               duration=5500)
            else:
                self._set_team_status("申请未提交", t.WARNING)
                self.app.toast(self._friendly_team_error(code, data), duration=5000)
            return
        if action in ("bootstrap", "claim"):
            if code == 200:
                record = result.get("record") or {}
                self._activate_team_hub(str(result.get("origin") or ""), record)
                self._set_team_status("已加入", t.SUCCESS)
                return
            self._set_team_status("已批准，凭证验证失败" if action == "claim" else "初始化失败",
                                  t.WARNING)
            self.app.toast(self._friendly_team_error(code, data), duration=5000)
            return
        if action == "invite":
            if code == 200:
                invite = data.get("invite") or {}
                invite_code = str(data.get("invite_code") or "")
                team = data.get("team") or {}
                self._show_team_invite_once(str(result.get("origin") or ""), team, invite, invite_code)
                self.app.toast("邀请已创建；请通过可信渠道把弹窗内容交给受邀人")
                self._refresh_team_admin()
            else:
                self.app.toast(self._friendly_team_error(code, data), duration=5000)
            return
        if action in {"application_approve", "application_reject", "invite_revoke",
                      "device_revoke", "member_deactivate", "leave"}:
            if code == 200:
                if action == "leave":
                    record = result.get("record") or {}
                    fallback = str(record.get("previous_llm_base") or "http://127.0.0.1:8001")
                    if self.app._team_backend_origin == str(record.get("origin") or ""):
                        self.app._team_backend_origin = ""
                        self.app._refresh_backend_switch_control()
                    self.app.switch_backend_context(fallback, "个人对话")
                    self._set_team_status("未连接", t.INK_MUTED)
                    self.app.toast("本机设备凭证已撤销")
                else:
                    self.app.toast({
                        "application_approve": "已批准加入申请；申请人可领取设备凭证",
                        "application_reject": "已拒绝加入申请",
                        "invite_revoke": "邀请码已撤销",
                        "device_revoke": "设备凭证已撤销",
                        "member_deactivate": "成员已停用，其设备凭证已撤销",
                    }[action])
                    self._refresh_team_admin()
            else:
                self.app.toast(self._friendly_team_error(code, data), duration=5000)

    def _apply_team_admin_result(self, result: dict) -> None:
        results = result.get("results") or {}
        errors = []
        def unpack(name: str, key: str):
            code, data = results.get(name, (0, {}))
            if code == 200:
                return data
            errors.append((code, data))
            return {}
        app_data = unpack("apps", "applications")
        invite_data = unpack("invites", "invites")
        member_data = unpack("members", "members")
        self._team_app_rows = list(app_data.get("applications") or [])
        self._team_invite_rows = list(invite_data.get("invites") or [])
        self._team_member_rows = []
        if self._team_apps_list is not None:
            self._team_apps_list.delete(0, "end")
            for row in self._team_app_rows:
                try:
                    created_at = datetime.fromtimestamp(
                        float(row.get("created_at") or 0)).strftime("%Y-%m-%d %H:%M")
                except (TypeError, ValueError, OverflowError, OSError):
                    created_at = "时间未知"
                self._team_apps_list.insert(
                    "end", f"{row.get('status')} · {row.get('expected_login')} → "
                    f"{row.get('actual_login')} · {row.get('device_name')} · "
                    f"VI {row.get('installation_code') or '旧申请未提供'} · "
                    f"{row.get('installation_code_binding') or '未确认绑定'} · "
                    f"{created_at} · {row.get('id')}")
        if self._team_invites_list is not None:
            self._team_invites_list.delete(0, "end")
            for row in self._team_invite_rows:
                self._team_invites_list.insert(
                    "end", f"{row.get('status')} · {row.get('expected_login')} · "
                    f"{row.get('role')} · 使用 {row.get('uses')}/{row.get('max_uses')} · {row.get('id')}")
        if self._team_members_list is not None:
            self._team_members_list.delete(0, "end")
            for member in member_data.get("members") or []:
                self._team_member_rows.append({"kind": "member", **member})
                self._team_members_list.insert(
                    "end", f"成员 · {member.get('status')} · {member.get('name')} · "
                    f"{member.get('tailscale_login')} · {member.get('role')} · {member.get('id')}")
                for device in member.get("devices") or []:
                    self._team_member_rows.append({"kind": "device", **device})
                    self._team_members_list.insert(
                        "end", f"    设备 · {device.get('status')} · {device.get('name')} · {device.get('id')}")
        if errors:
            code, data = errors[0]
            self.app.toast(self._friendly_team_error(code, data), duration=5000)
            self._set_team_status("已连接；管理员权限不足或凭证失效", t.WARNING)
            return
        self._set_team_status("已连接 · 管理员", t.SUCCESS)
        self.app.toast("已刷新邀请、待审申请和成员设备")

    def _show_team_invite_once(self, origin: str, team: dict,
                               invite: dict, code: str) -> None:
        window = tk.Toplevel(self)
        window.title("一次性团队邀请")
        window.configure(bg=t.CANVAS)
        window.transient(self.winfo_toplevel())
        window.resizable(False, False)
        body = tk.Frame(window, bg=t.CANVAS, padx=t.s(24), pady=t.s(20))
        body.pack(fill="both", expand=True)
        tk.Label(body, text="请通过已有可信渠道转交", bg=t.CANVAS,
                 fg=t.INK, font=self.fonts.display_md).pack(anchor="w")
        details = (f"团队：{team.get('team_name')}\nteam_id：{team.get('team_id')}\n"
                   f"Hub：{origin}\n指定身份：{invite.get('expected_login')}\n"
                   "有效期：24 小时，最多使用一次")
        tk.Label(body, text=details, bg=t.CANVAS, fg=t.INK_SOFT,
                 font=self.fonts.small, justify="left", anchor="w").pack(fill="x", pady=(t.s(10), t.s(12)))
        code_field = MinimalField(body, font=self.fonts.body, value=code,
                                  placeholder="邀请码")
        code_field.pack(fill="x", pady=(0, t.s(12)))
        buttons = tk.Frame(body, bg=t.CANVAS)
        buttons.pack(fill="x")
        def copy_code():
            window.clipboard_clear()
            window.clipboard_append(code)
            window.update_idletasks()
            self.app.toast("邀请码已复制到剪贴板；请仅通过可信渠道发送")
        FlatButton(buttons, "复制邀请码", copy_code, font=self.fonts.small,
                   variant="primary", height=36, min_width=112,
                   parent_bg=t.CANVAS).pack(side="left", padx=(0, t.s(8)))
        FlatButton(buttons, "完成", window.destroy, font=self.fonts.small,
                   variant="ghost", height=36, min_width=88,
                   parent_bg=t.CANVAS).pack(side="left")
        window.protocol("WM_DELETE_WINDOW", window.destroy)

    def _apply_page_data(self, key: str, results: dict) -> None:
        failed = [(path, data.get("detail", code)) for path, (code, data) in results.items() if code != 200]
        self._load_failed = bool(failed)
        self.footer_hint.configure(text="读取失败，可重试" if failed else
                                   ("更改当前页面后保存" if key in EDITABLE_PAGES else "数据已更新"))
        if failed:
            self.app.toast(f"读取失败：{failed[0][1]}", duration=4200)
        def data(path: str) -> dict:
            code, body = results.get(path, (0, {}))
            return body if code == 200 and isinstance(body, dict) else {}

        cfg = data("/api/v1/config").get("config") or {}
        if cfg and key not in self._dirty_pages:
            if key == "model":
                self._apply_values({
                    "API URL": str(cfg.get("api_url") or ""),
                    "API Key": "",
                    "model": str(cfg.get("model") or ""),
                    "上下文窗口": str(cfg.get("context_window") or 65536),
                    "reasoning": {"max": "最高", "high": "高", "off": "关闭"}.get(cfg.get("reasoning_mode"), "最高"),
                })
            elif key == "vision":
                self._apply_values({"Vision URL": str(cfg.get("vision_api_url") or ""),
                                    "Vision Key": "", "Vision Model": str(cfg.get("vision_model") or "")})
            elif key == "router":
                self._apply_values({"tool_router": bool(cfg.get("tool_router")),
                                    "Ollama URL": str(cfg.get("tool_router_url") or ""),
                                    "路由模型": str(cfg.get("tool_router_model") or "")})
            elif key == "memory":
                self._apply_values({"启用记忆": bool(cfg.get("memory_enabled", True)),
                                    "自动提取": bool(cfg.get("llm_memory_extract"))})
        if key == "common" and key not in self._dirty_pages:
            self._apply_values({"默认工作区": str(data("/api/v1/workspace").get("workspace") or "")})
        elif key == "permissions" and key not in self._dirty_pages:
            mode = str(data("/api/v1/confirm-mode").get("mode") or "auto")
            self._apply_values({"confirm_mode": {"auto": "智能", "strict": "严格", "query": "只读"}.get(mode, "智能")})
        elif key == "sandbox":
            state = data("/api/v1/sandbox/status")
            if state and key not in self._dirty_pages:
                self._apply_values({"sandbox_mode": {"workspace": "工作区", "host": "本机", "wsl": "WSL"}.get(state.get("default_mode"), "工作区")})
            self._set_info("工作区隔离", "可用" if state.get("workspace_available", True) else "不可用", t.SUCCESS if state.get("workspace_available", True) else t.WARNING)
            self._set_info("WSL 可用性", "可用" if state.get("wsl_available") else "不可用", t.SUCCESS if state.get("wsl_available") else t.WARNING)
        elif key == "memory":
            mem = data("/api/v1/health").get("memory_stats") or {}
            if mem:
                self._set_info("原子记忆", f"{int(mem.get('l1_memories') or 0)} 条", t.SUCCESS)
                self._set_info("长期画像", "已启用" if mem.get("enabled", True) else "已关闭", t.SUCCESS)
                self._set_info("动态 Skill", f"{int(mem.get('dynamic_skills') or 0)} 个", t.SUCCESS)
        elif key == "codegraph":
            stats = data("/api/v1/codegraph/stats")
            if stats:
                built = bool(stats.get("built"))
                self._set_info("索引状态", "已就绪" if built else "未构建", t.SUCCESS if built else t.WARNING)
                self._set_info("文件", str(stats.get("files", 0)), t.INK_SOFT)
                self._set_info("符号", str(stats.get("symbols", 0)), t.INK_SOFT)
        elif key == "integrations":
            browser = data("/api/v1/browser/status")
            if browser:
                enabled = bool(browser.get("enabled"))
                self._set_info("浏览器 MCP", "已启用" if enabled else "未启用", t.SUCCESS if enabled else t.WARNING)
            mcp = data("/api/v1/mcp/status")
            if mcp:
                servers = mcp.get("servers") or []
                connected = sum(1 for server in servers if server.get("connected"))
                self._set_info("MCP 服务", f"{connected}/{len(servers)} 已连接", t.SUCCESS if connected else t.WARNING)
        elif key == "diagnostics":
            health = data("/api/v1/health")
            usage = data("/api/v1/usage")
            if health:
                configured = bool(health.get("configured"))
                self._set_info("模型服务", "正常" if configured else "未配置", t.SUCCESS if configured else t.WARNING)
                self._set_info("版本", str(health.get("version") or "—"), t.INK_SOFT)
            if usage:
                self._set_info("本次用量", f"{int(usage.get('total_tokens') or 0):,} tokens", t.INK_SOFT)
        elif key == "subagents":
            agents = data("/api/v1/agents").get("agents") or []
            self._set_info("可用 Agent", f"{len(agents)} 个", t.SUCCESS if agents else t.INK_MUTED)
        elif key == "extensions":
            extensions = data("/api/v1/extensions")
            if extensions:
                count = len(extensions.get("plugins") or extensions.get("extensions") or [])
                self._set_info("扩展数量", f"{count} 个", t.SUCCESS if count else t.INK_MUTED)
        elif key == "advanced" and key not in self._dirty_pages:
            local = data("local")
            self._apply_values({"Desktop URL": str(local.get("daemon_base") or ""),
                                "Agent URL": str(local.get("llm_base") or "")})

    def _set_info(self, key: str, value: str, color: str) -> None:
        lbl = self._dynamic_info.get(key)
        if lbl is not None:
            lbl.configure(text=value, fg=color)

    def _mark_dirty(self) -> None:
        if self._page_ready and not self._applying_data:
            self._dirty_pages.add(self.active_page)
            self._update_footer()

    def _snapshot_controls(self) -> dict[str, object]:
        values = {}
        for key, control in self.local_controls.items():
            if hasattr(control, "get"):
                values[key] = control.get()
            elif hasattr(control, "value"):
                values[key] = control.value
        return values

    def _apply_values(self, values: dict[str, object]) -> None:
        self._applying_data = True
        try:
            for key, value in values.items():
                control = self.local_controls.get(key)
                if control is None:
                    continue
                if hasattr(control, "set"):
                    control.set(value)
                elif hasattr(control, "variable"):
                    control.variable.set(value)
        finally:
            self._applying_data = False

    def _update_footer(self) -> None:
        if self.active_page in EDITABLE_PAGES:
            if not self.save_button.winfo_manager():
                self.save_button.pack(side="left")
            self.save_button.set_enabled(not self._saving and not self._load_pending and
                                         self.active_page in self._dirty_pages)
            self.footer_hint.configure(text="有未保存更改" if self.active_page in self._dirty_pages else "更改当前页面后保存")
        else:
            self.save_button.pack_forget()
            self.footer_hint.configure(text="此页展示实时数据")

    def _cancel(self) -> None:
        if self._saving:
            self.app.toast("正在保存，请稍候")
            return
        self._drafts.clear()
        self._dirty_pages.clear()
        self._page_ready = False
        self._needs_reload = True
        self.app.show_chat()

    def on_open(self) -> None:
        if self._needs_reload:
            self.select_page(self.active_page)
        else:
            self._refresh_page_data(self.active_page)

    def reset_backend_context_state(self) -> None:
        """Release UI locks whose old-origin replies will be epoch-discarded."""
        self._load_serial += 1
        self._load_pending = False
        self._saving = False
        self._action_busy = False
        self._needs_reload = True
        self._restore_team_action_buttons()
        try:
            self._update_footer()
        except (AttributeError, tk.TclError):
            pass

    def _responsive_rail(self, event: tk.Event) -> None:
        try:
            if event.width < t.s(1450):
                self.rail.grid_remove()
            else:
                self.rail.grid()
        except tk.TclError:
            pass

    # Model page -------------------------------------------------------------
    def _build_model_page(self) -> None:
        page = self.page_scroll.inner
        section, body = self._section(page, "主模型", "")
        self._field_row(body, "API URL", "", "OpenAI 兼容地址")
        self._field_row(body, "API Key", "", "留空保留现有密钥", secret=True)
        row = self._row_shell(body, "模型")
        model = MinimalField(
            row,
            font=self.fonts.body,
            value=self.app.model_name,
            placeholder="输入模型标识，例如 deepseek-v4-flash",
        )
        model.grid(row=0, column=1, sticky="ew")
        self.local_controls["model"] = model
        model.variable.trace_add("write", lambda *_args: self._mark_dirty())
        FlatButton(
            row,
            "获取模型",
            self._fetch_models,
            font=self.fonts.small,
            variant="outline",
            height=38,
            min_width=96,
            parent_bg=t.CANVAS,
        ).grid(row=0, column=2, padx=(t.s(12), 0))
        tk.Label(
            body,
            text="用上方填写的 URL / Key 直接拉取，无需先保存",
            bg=t.CANVAS,
            fg=t.INK_MUTED,
            font=self.fonts.caption,
        ).pack(anchor="w", padx=(t.s(140), 0), pady=(t.s(5), 0))
        row = self._row_shell(body, "可用模型")
        choices = SelectField(
            row,
            [],
            font=self.fonts.body,
            value="点击「获取模型」拉取列表",
        )
        choices.grid(row=0, column=1, sticky="ew")
        self.local_controls["model_choices"] = choices
        choices.variable.trace_add("write", lambda *_args: self._on_choice_picked())
        FlatButton(
            row,
            "测试连接",
            self._probe_current,
            font=self.fonts.small,
            variant="outline",
            height=38,
            min_width=96,
            parent_bg=t.CANVAS,
        ).grid(row=0, column=2, padx=(t.s(12), 0))
        self._action_row(body, "已保存配置", "通过后端测试已保存的模型连接",
                         "测试", self._test_connection)
        self._finish_section(section)

        section, body = self._section(page, "推理与上下文", "")
        row = self._row_shell(body, "推理强度")
        reasoning = SegmentedControl(
            row,
            ("最高", "高", "关闭"),
            font=self.fonts.small,
            value="最高",
            command=lambda _value: self._mark_dirty(),
            bg=t.CANVAS,
        )
        reasoning.grid(row=0, column=1, columnspan=2, sticky="ew")
        self.local_controls["reasoning"] = reasoning
        self._field_row(body, "上下文窗口", "", "例如 65536")
        tk.Label(
            body,
            text="请输入正整数；实际可用长度仍由模型提供方决定",
            bg=t.CANVAS,
            fg=t.INK_MUTED,
            font=self.fonts.caption,
        ).pack(anchor="w", padx=(t.s(140), 0), pady=(t.s(5), 0))
        self._finish_section(section, final=True)

    # Generic pages ----------------------------------------------------------
    def _build_generic_page(self, key: str) -> None:
        page = self.page_scroll.inner
        sections = GENERIC_PAGES.get(key, ())
        for section_index, (title, subtitle, items) in enumerate(sections):
            section, body = self._section(page, title, subtitle)
            for item in items:
                kind = item[0]
                if kind in {"field", "secret"}:
                    _, label, value, placeholder = item
                    self._field_row(
                        body,
                        label,
                        value,
                        placeholder,
                        secret=kind == "secret",
                    )
                elif kind == "select":
                    _, label, values, current = item
                    row = self._row_shell(body, label)
                    if label in {"Vision Model", "路由模型"}:
                        field = MinimalField(row, font=self.fonts.body,
                                             value="" if current.startswith("选择或输入") else current,
                                             placeholder="输入模型标识")
                    else:
                        field = SelectField(row, values, font=self.fonts.body, value=current)
                    field.grid(row=0, column=1, columnspan=2, sticky="ew")
                    self.local_controls[label] = field
                    field.variable.trace_add("write", lambda *_args: self._mark_dirty())
                elif kind == "segment":
                    _, label, values, current = item
                    row = self._row_shell(body, label)
                    field = SegmentedControl(
                        row,
                        values,
                        font=self.fonts.small,
                        value=current,
                        command=lambda _value: self._mark_dirty(),
                        bg=t.CANVAS,
                    )
                    field.grid(row=0, column=1, columnspan=2, sticky="ew")
                    control_key = {"默认模式": "confirm_mode", "环境": "sandbox_mode"}.get(label, label)
                    self.local_controls[control_key] = field
                elif kind == "switch":
                    _, label, description, value = item
                    row = tk.Frame(body, bg=t.CANVAS)
                    row.pack(fill="x", pady=t.s(7))
                    copy = tk.Frame(row, bg=t.CANVAS)
                    copy.pack(side="left", fill="x", expand=True)
                    tk.Label(copy, text=label, bg=t.CANVAS, fg=t.INK_SOFT, font=self.fonts.body).pack(anchor="w")
                    tk.Label(copy, text=description, bg=t.CANVAS, fg=t.INK_MUTED, font=self.fonts.caption).pack(anchor="w", pady=(t.s(2), 0))
                    sw = Switch(row, value=value, bg=t.CANVAS)
                    sw.command = lambda _value: self._mark_dirty()
                    sw.pack(side="right", padx=(t.s(20), t.s(4)))
                    self.local_controls[label] = sw
                    if label == "启用工具路由":
                        self.local_controls["tool_router"] = sw
                elif kind == "info":
                    _, label, value, color = item
                    self._info_row(body, label, value, color)
                elif kind == "action":
                    _, label, description, button_text = item
                    cmd = self._action_command(button_text)
                    self._action_row(body, label, description, button_text, cmd)
            self._finish_section(section, final=section_index == len(sections) - 1)

    # Multi-project Hub ------------------------------------------------------
    def _build_projects_page(self) -> None:
        page = self.page_scroll.inner
        section, body = self._section(
            page, "Hub 项目中心",
            "查看当前用户可访问的项目，设置本机派活项目，并管理认领、邀请和项目成员授权。")
        origin = str(self.app.client.base or "").rstrip("/")
        connections = [row for row in (load_config().get("team_connections") or [])
                       if isinstance(row, dict) and row.get("origin")]
        if not any(str(row.get("origin") or "").rstrip("/") == origin
                   for row in connections):
            origin = str(load_config().get("team_last_origin") or
                         (connections[-1].get("origin") if connections else "尚未连接 Hub"))
        self._info_row(body, "Hub 地址", origin, t.INK_SOFT)
        self._info_row(body, "当前对话空间", str(self.app.client.base or ""), t.INK_SOFT)
        tk.Label(
            body,
            text=("项目加入只授予项目访问权限，不会开启本机 Worker。\n"
                  "一次性认领码和项目邀请码只在创建成功后显示一次；请在代码弹窗关闭前复制，"
                  "并通过可信渠道交给指定用户。"),
            bg=t.CANVAS, fg=t.INK_MUTED, font=self.fonts.caption,
            justify="left", anchor="w", wraplength=t.s(850),
        ).pack(fill="x", pady=(t.s(7), t.s(10)))
        self._action_row(
            body, "多项目管理", "打开 Hub 项目中心，查看角色、项目状态、待审批数量、成员和终端授权。",
            "打开项目中心", self._open_project_hub)
        self._finish_section(section, final=True)

    def _open_project_hub(self) -> None:
        if self._project_hub_window is not None:
            try:
                if self._project_hub_window.winfo_exists():
                    self._project_hub_window.deiconify()
                    self._project_hub_window.lift()
                    self._project_hub_window.focus_force()
                    return
            except tk.TclError:
                self._project_hub_window = None
        try:
            self._project_hub_window = open_project_hub(
                self, self.app, self.fonts, self.client)
        except Exception as exc:
            self.app.toast(f"无法打开项目中心：{exc}", duration=5000)

    # Team membership --------------------------------------------------------
    def _build_team_page(self) -> None:
        page = self.page_scroll.inner
        self._team_fields.clear()
        self._team_apps = []
        self._team_invites = []
        self._team_members = []
        self._team_action_buttons = []
        last = (load_config().get("team_connections") or [])
        last_origin = str(last[-1].get("origin") or "") if last and isinstance(last[-1], dict) else ""

        section, body = self._section(
            page, "连接 Hub", "本地和远程均使用地址与端口连接；加入团队后领取独立设备凭证。")
        self._team_status_label = tk.Label(
            body, text="状态：仅本机可用 · 尚未连接 Hub", bg=t.CANVAS, fg=t.INK_MUTED,
            font=self.fonts.body_medium, anchor="w")
        self._team_status_label.pack(fill="x", pady=(0, t.s(8)))
        installation_text = (self._installation_code or
                             f"本机码不可用：{self._installation_code_error or '本地文件不可读'}")
        self._team_identity_label = tk.Label(
            body, text=f"本机安装码（VI，公开标识）：{installation_text}",
            bg=t.CANVAS, fg=t.INK_SOFT, font=self.fonts.body_medium,
            anchor="w", justify="left", wraplength=t.s(900))
        self._team_identity_label.pack(fill="x", pady=(0, t.s(3)))
        tk.Label(
            body,
            text=("VI 本机安装码只供管理员核对申请来源；它不是凭证。\n"
                  "VN 终端显示码由每个 Hub 在批准后分配；设备令牌是秘密，仅保存在安全存储中。"),
            bg=t.CANVAS, fg=t.INK_MUTED, font=self.fonts.caption,
            anchor="w", justify="left", wraplength=t.s(900),
        ).pack(fill="x", pady=(0, t.s(7)))
        self._team_hub_label = tk.Label(
            body, text="Hub：尚未核对\n此 Hub 终端码（VN）：尚未注册\n设备凭证：未显示",
            bg=t.CANVAS, fg=t.INK_SOFT,
            font=self.fonts.small, anchor="w", justify="left", wraplength=t.s(900))
        self._team_hub_label.pack(fill="x", pady=(0, t.s(7)))
        self._team_field(body, "Hub 地址", last_origin,
                         "http://192.168.1.10:8001")
        self._team_button_row(body, (("配置地址、TLS 与密码", self._configure_team_connection, "outline"),))
        self._team_button_row(body, (("复制本机安装码", self.copy_installation_code, "outline"),))
        primary, _refresh = self._team_button_row(body, (
            ("核对 Hub", self._team_primary_action, "primary"),
            ("刷新状态", self._refresh_team_state, "outline"),
        ))
        self._team_primary_button = primary
        self._update_team_primary_action()
        self._team_field(body, "邀请码", "", "由管理员通过可信渠道提供", secret=True)
        self._team_field(body, "显示名", "", "团队成员显示名")
        self._team_field(body, "设备名称", "", "例如 Alice 笔记本")
        self._team_button_row(body, (("提交加入申请", self._submit_team_join, "primary"),))
        self._team_button_row(body, (("领取已批准的设备凭证并连接", self._claim_team_device, "outline"),))
        self._finish_section(section)

        section, body = self._section(
            page, "Hub 本机初始化", "仅在 Hub 电脑使用；启动凭证从本机 secure_store 读取，不显示给受邀成员。")
        self._team_field(body, "团队名称", "", "例如 Venus 产品组")
        self._team_field(body, "管理员账号", "", "团队内唯一的账号标识")
        self._team_field(body, "管理员显示名", "", "团队内显示名")
        self._team_field(body, "Hub 本机地址", "http://127.0.0.1:8001", "仅本机地址，HTTP 或 HTTPS")
        self._team_button_row(body, (("在本机初始化团队", self._bootstrap_team, "outline"),))
        self._finish_section(section)

        section, body = self._section(
            page, "管理员邀请与申请审核", "邀请码有效 24 小时且仅能使用一次。明文只在创建后弹窗显示一次。")
        self._team_field(body, "受邀成员账号", "", "例如 member@example.com")
        role_row = self._row_shell(body, "邀请角色")
        role = SelectField(role_row, ("member", "admin"), font=self.fonts.body,
                           value="member")
        role.grid(row=0, column=1, sticky="ew")
        self._team_fields["邀请角色"] = role
        self._team_button_row(body, (("创建邀请", self._create_team_invite, "primary"),
                                     ("刷新待审申请", self._refresh_team_admin, "outline")))
        tk.Label(body,
                 text="申请列表中的 VI 码只辅助核对终端来源；审批前仍须核对实际 Serve 身份。",
                 bg=t.CANVAS, fg=t.INK_MUTED, font=self.fonts.caption,
                 anchor="w", justify="left", wraplength=t.s(900)).pack(fill="x")
        self._team_apps_list = self._team_listbox(body, 4)
        self._team_button_row(body, (("批准所选申请", lambda: self._review_selected_application("approve"), "primary"),
                                     ("拒绝所选申请", lambda: self._review_selected_application("reject"), "outline")))
        self._team_invites_list = self._team_listbox(body, 3)
        self._team_button_row(body, (("撤销所选邀请码", self._revoke_selected_invite, "outline"),))
        self._finish_section(section)

        section, body = self._section(
            page, "成员与设备", "撤销设备会立即使该设备凭证失效；停用成员会撤销其所有设备。")
        self._team_members_list = self._team_listbox(body, 7)
        self._team_button_row(body, (("撤销所选设备", self._revoke_selected_device, "outline"),
                                     ("停用所选成员", self._deactivate_selected_member, "outline"),
                                     ("退出此设备", self._leave_team_device, "ghost")))
        self._finish_section(section, final=True)

    def _team_field(self, parent: tk.Misc, label: str, value: str,
                    placeholder: str, *, secret: bool = False) -> MinimalField:
        row = self._row_shell(parent, label)
        field = MinimalField(row, font=self.fonts.body, value=value,
                             placeholder=placeholder, show="•" if secret else "")
        field.grid(row=0, column=1, sticky="ew")
        self._team_fields[label] = field
        if label == "Hub 地址":
            field.variable.trace_add("write", lambda *_args: self._clear_team_hub_confirmation())
        return field

    def _team_button_row(self, parent: tk.Misc,
                         buttons: tuple[tuple[str, object, str], ...]) -> list[FlatButton]:
        row = tk.Frame(parent, bg=t.CANVAS)
        row.pack(fill="x", pady=(t.s(8), t.s(10)))
        created = []
        for label, command, variant in buttons:
            button = FlatButton(row, label, command, font=self.fonts.small,
                                variant=variant, height=36, min_width=118,
                                parent_bg=t.CANVAS)
            button.pack(side="left", padx=(0, t.s(8)))
            if self._action_busy:
                button.set_enabled(False)
            created.append(button)
            self._team_action_buttons.append(button)
        return created

    def _team_listbox(self, parent: tk.Misc, height: int) -> tk.Listbox:
        holder = tk.Frame(parent, bg=t.SURFACE, highlightthickness=1,
                          highlightbackground=t.LINE, height=t.s(height * 31))
        holder.pack(fill="x", pady=(t.s(4), t.s(6)))
        holder.pack_propagate(False)
        box = tk.Listbox(holder, height=height, font=self.fonts.small,
                         bg=t.SURFACE, fg=t.INK, selectbackground=t.TERRACOTTA_SOFT,
                         selectforeground=t.INK, relief="flat", bd=0,
                         activestyle="none", highlightthickness=0)
        box.pack(fill="both", expand=True, padx=t.s(8), pady=t.s(5))
        return box

    def _team_value(self, label: str) -> str:
        field = self._team_fields.get(label)
        return str(field.get()).strip() if field is not None else ""

    def _configure_team_connection(self) -> None:
        try:
            base = self._team_origin()
        except ValueError:
            base = "http://127.0.0.1:8001"
        def connected(origin, info):
            self._team_fields["Hub 地址"].set(origin)
            self._check_team_hub()
        ConnectionDialog(self.app, base, on_connected=connected)

    def _team_origin(self) -> str:
        try:
            return normalize_team_origin(self._team_value("Hub 地址"))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    @staticmethod
    def _team_guest_client(origin: str) -> ApiClient:
        return ApiClient(origin, use_default_token=False, deny_redirects=True)

    @staticmethod
    def _team_member_client(origin: str, team_id: str = "") -> ApiClient:
        record = team_connection(origin, team_id or None)
        token = (team_token_for_connection(origin, str(record.get("team_id") or ""))
                 if record else "")
        if token:
            return ApiClient(origin, token=token, token_header="X-Team-Device-Token",
                             use_default_token=False, deny_redirects=True)
        return ApiClient(origin, use_default_token=False, deny_redirects=True)

    def _clear_team_hub_confirmation(self) -> None:
        self._team_confirmed = None
        self._team_state = "local_only"
        self._update_team_primary_action()
        if self._team_status_label is not None and self.active_page == "team":
            self._team_status_label.configure(text="状态：地址已修改，请重新核对", fg=t.WARNING)

    def _set_team_status(self, text: str, color: str = t.INK_MUTED) -> None:
        state = {
            "已加入": "active",
            "已连接 · 管理员": "active",
            "已连接未加入": "hub_verified",
            "已连接未初始化": "uninitialized",
            "申请待批准": "pending",
            "已批准，等待领取设备凭证": "approved",
            "已批准，凭证验证失败": "approved",
            "申请被拒绝": "local_only",
            "批准的设备申请已失效": "local_only",
            "领取已完成；如凭证丢失请联系管理员重新邀请": "local_only",
            "已停用": "local_only",
            "凭证失效": "local_only",
            "未连接": "local_only",
            "连接失败": "local_only",
            "申请未提交": "local_only",
        }.get(text)
        if state:
            self._team_state = state
            self._update_team_primary_action()
        if self._team_status_label is not None and self.active_page == "team":
            self._team_status_label.configure(text=f"状态：{text}", fg=color)

    def _update_team_primary_action(self) -> None:
        if self._team_primary_button is None:
            return
        label = {
            "local_only": "核对 Hub",
            "hub_verified": "提交加入申请",
            "uninitialized": "Hub 尚未初始化",
            "pending": "查看申请状态",
            "approved": "领取凭证并连接",
            "active": "进入项目中心",
        }.get(self._team_state, "核对 Hub")
        try:
            if self._team_primary_button.winfo_exists():
                self._team_primary_button.set_text(label)
        except tk.TclError:
            pass

    def _team_primary_action(self) -> None:
        actions = {
            "local_only": self._check_team_hub,
            "hub_verified": self._submit_team_join,
            "pending": self._refresh_team_state,
            "approved": self._claim_team_device,
            "active": self._open_project_hub,
            "uninitialized": self._explain_hub_uninitialized,
        }
        actions.get(self._team_state, self._check_team_hub)()

    def _explain_hub_uninitialized(self) -> None:
        self.app.toast("此 Hub 尚未初始化团队；请在 Hub 电脑的‘Hub 本机初始化’区完成初始化。",
                       duration=5000)

    def copy_installation_code(self) -> None:
        if not self._installation_code:
            self.app.toast(f"本机安装码不可用：{self._installation_code_error or '无法读取本地标识文件'}",
                           duration=5000)
            return
        self.clipboard_clear()
        self.clipboard_append(self._installation_code)
        self.update_idletasks()
        self.app.toast("本机安装码 VI 已复制；它是公开标识，不是设备凭证")

    def _render_team_identity(self, origin: str, team: dict | None = None,
                              record: dict | None = None,
                              identity: dict | None = None) -> None:
        if self._team_hub_label is None:
            return
        team = team or {}
        record = record or {}
        identity = identity or {}
        user = identity.get("user") or {}
        device = identity.get("device") or {}
        active = bool(record.get("device_id") or device.get("device_id")
                      or device.get("id"))
        display_code = (device.get("display_code")
                        or identity.get("terminal_display_code")
                        or identity.get("display_code"))
        display_text = (str(display_code) if active and display_code
                        else "审批并领取设备凭证后由此 Hub 分配")
        credential_text = ("已保存到安全存储（令牌不显示）" if active
                           else "尚未领取；令牌不显示")
        if not record.get("application_id"):
            binding_text = "Hub 初始化设备"
        elif record.get("installation_code_binding") == "bound":
            binding_text = "Hub 已确认绑定"
        elif record.get("installation_code_binding") == "legacy":
            binding_text = "旧协议申请，Hub 未绑定本机码"
        else:
            binding_text = "Hub 尚未确认绑定（旧协议可能忽略此字段）"
        user_text = (user.get("display_name") or user.get("name")
                     or user.get("user_id") or user.get("id") or "未认证")
        trusted_identity = str(team.get("tailscale_login") or "未提供")
        code_text = self._installation_code or "本机码不可用"
        self._team_hub_label.configure(
            text=(f"Hub：{origin or '尚未核对'}\n"
                  f"团队：{team.get('team_name') or '—'} · team_id：{team.get('team_id') or record.get('team_id') or '—'}\n"
                  f"连接方式：{team.get('connection_mode') or trusted_identity} · 用户：{user_text}\n"
                  f"本机安装码（VI，公开标识）：{code_text}\n"
                  f"申请绑定状态：{binding_text}\n"
                  f"此 Hub 终端码（VN）：{display_text}\n"
                  f"设备凭证：{credential_text}"),
            fg=t.INK_SOFT if active else t.INK_MUTED)

    def _start_team_action(self, action: str, work) -> None:
        if self._action_busy:
            self.app.toast("团队操作正在进行，请稍候")
            return
        self._action_busy = True
        self._team_buttons_before = [(button, button.enabled)
                                     for button in self._team_action_buttons]
        for button, _enabled in self._team_buttons_before:
            button.set_enabled(False)
        self.footer_hint.configure(text="正在安全连接团队 Hub…")
        self.app.bridge.submit("settings_team", lambda: (action, work()))

    def _restore_team_action_buttons(self) -> None:
        for button, enabled in getattr(self, "_team_buttons_before", []):
            try:
                if button.winfo_exists():
                    button.set_enabled(enabled)
            except tk.TclError:
                pass
        self._team_buttons_before = []

    def _check_team_hub(self) -> None:
        try:
            origin = self._team_origin()
        except ValueError as exc:
            self.app.toast(str(exc), duration=4200)
            return
        def work():
            code, data = self._team_guest_client(origin).get("/api/v1/team/public", timeout=8)
            return {"origin": origin, "code": code, "data": data}
        self._start_team_action("check", work)

    def _refresh_team_state(self) -> None:
        try:
            origin = self._team_origin()
        except ValueError:
            connections = load_config().get("team_connections") or []
            if not connections:
                self._set_team_status("未连接")
                return
            try:
                origin = normalize_team_origin(str(connections[-1].get("origin") or ""))
                self._team_fields["Hub 地址"].set(origin)
            except Exception:
                self._set_team_status("未连接")
                return
        def work():
            client = self._team_guest_client(origin)
            pub_code, public = client.get("/api/v1/team/public", timeout=8)
            if pub_code != 200:
                return {"origin": origin, "public_code": pub_code, "public": public}
            record = team_connection(origin, str(public.get("team_id") or ""))
            if record and record.get("application_id") and not record.get("device_id"):
                client = ApiClient(origin, token=team_claim_secret(origin, record["application_id"]),
                                   token_header="X-Team-Claim-Secret", use_default_token=False,
                                   deny_redirects=True)
                code, status = client.get(
                    f"/api/v1/team/join-requests/{record['application_id']}", timeout=8)
                return {"origin": origin, "public_code": pub_code, "public": public,
                        "record": record, "application_code": code, "application": status}
            if record and record.get("device_id"):
                token = team_token_for_connection(origin, str(record.get("team_id") or ""))
                member_client = ApiClient(
                    origin, token=token, token_header="X-Team-Device-Token",
                    use_default_token=False, deny_redirects=True) if token else client
                code, identity = member_client.get("/api/v1/team/me", timeout=8)
                if code == 200:
                    device_code, devices = member_client.get("/api/v1/team/me/devices", timeout=8)
                else:
                    device_code, devices = code, {}
                return {"origin": origin, "public_code": pub_code, "public": public,
                        "record": record, "identity_code": code, "identity": identity,
                        "device_code": device_code, "devices": devices}
            return {"origin": origin, "public_code": pub_code, "public": public,
                    "record": record}
        self._start_team_action("refresh", work)

    def _submit_team_join(self) -> None:
        try:
            origin = self._team_origin()
        except ValueError as exc:
            self.app.toast(str(exc), duration=4200)
            return
        confirmed = self._team_confirmed or {}
        if confirmed.get("origin") != origin:
            self.app.toast("请先核对 Hub 地址和团队名称")
            return
        if (confirmed.get("team") or {}).get("initialized") is False:
            self.app.toast("此 Hub 尚未初始化团队；请先在 Hub 本机完成初始化")
            return
        code = self._team_value("邀请码")
        display = self._team_value("显示名")
        device = self._team_value("设备名称")
        if not code or not display or not device:
            self.app.toast("请填写邀请码、显示名和设备名称")
            return
        if not self._installation_code:
            self.app.toast(f"本机安装码不可用，暂不能提交新申请：{self._installation_code_error or '无法写入本地标识文件'}",
                           duration=6000)
            return
        team = confirmed.get("team") or {}
        saved = team_connection(origin, str(team.get("team_id") or ""))
        if saved and saved.get("device_id"):
            self.app.toast("这台设备已经加入此团队；请在另一台设备上申请新设备凭证")
            return
        if (saved and saved.get("application_id")
                and saved.get("status") in ("pending", "approved")):
            if saved.get("status") == "approved":
                self.app.toast("申请已批准，请领取设备凭证后再操作")
            else:
                self.app.toast("已有加入申请，请先刷新申请状态并等待管理员审核")
            return
        if saved and saved.get("application_id") and saved.get("status") == "submitting":
            installation_code = str(saved.get("installation_code") or "")
        else:
            installation_code = self._installation_code
        prompt = (f"确认把加入申请发送到此 Hub？\n\n"
                  f"团队：{team.get('team_name') or '未知'}\n"
                  f"team_id：{team.get('team_id') or '未知'}\n"
                  f"地址：{origin}\n"
                  f"本机安装码（VI，公开标识）：{installation_code or '此旧申请未绑定本机码'}\n\n"
                  "确认后会发送邀请码、本机码和一次性领取密钥；本机码本身不能授权。")
        if not messagebox.askyesno("确认加入团队", prompt, parent=self):
            return
        if saved and saved.get("application_id") and saved.get("status") == "submitting":
            application_id = str(saved.get("application_id") or "")
            claim = team_claim_secret(origin, application_id)
            if not claim or not application_id.startswith("app_"):
                self.app.toast("上次申请的临时密钥不可用，请让管理员撤销原申请后重新邀请", duration=5000)
                return
            request_id = application_id.removeprefix("app_")
            installation_code = str(saved.get("installation_code") or "")
        else:
            claim = secrets.token_urlsafe(32)
            request_id = secrets.token_hex(16)
            application_id = f"app_{request_id}"
            installation_code = self._installation_code
        # Store the applicant-only temporary secret before the server consumes
        # the one-use invite, so an immediate approval cannot race the save.
        # Persist the stable ID before networking as well: if the response is
        # lost after the Hub creates the application, a retry must reuse it.
        save_team_claim_secret(origin, application_id, claim)
        save_team_connection({
            "origin": origin, "team_id": team.get("team_id"),
            "team_name": team.get("team_name"),
            "application_id": application_id, "display_name": display,
            "status": "submitting",
            "installation_code": installation_code or None,
        })
        def work():
            client = self._team_guest_client(origin)
            request_payload = {
                "invite_code": code, "display_name": display,
                "device_name": device, "claim_secret": claim,
                "request_id": request_id,
            }
            if installation_code:
                request_payload["installation_code"] = installation_code
            code_status, data, code_sent = client.post_with_optional_installation_code(
                "/api/v1/team/join-requests", request_payload, timeout=12)
            saved_record = None
            binding = ""
            bound = False
            if code_status == 200:
                application = data.get("application") or {}
                binding, bound = installation_code_confirmation(data, installation_code)
                saved_record = save_team_connection({
                    "origin": origin, "team_id": team.get("team_id"),
                    "team_name": team.get("team_name"),
                    "application_id": application.get("id") or application_id,
                    "display_name": display, "status": "pending",
                    "installation_code": installation_code or None,
                    "installation_code_binding": "bound" if bound else binding or None,
                })
            return {"origin": origin, "code": code_status, "data": data,
                    "team": team, "installation_code": installation_code,
                    "installation_code_binding": ("bound" if bound else binding),
                    "installation_code_bound": bound if code_status == 200 else False,
                    "installation_code_sent": code_sent,
                    "record": saved_record}
        self._start_team_action("join", work)

    def _claim_team_device(self) -> None:
        try:
            origin = self._team_origin()
        except ValueError as exc:
            self.app.toast(str(exc), duration=4200)
            return
        record = team_connection(origin)
        if not record or not record.get("application_id"):
            self.app.toast("没有待领取的加入申请")
            return
        app_id = str(record.get("application_id") or "")
        claim = team_claim_secret(origin, app_id)
        if not claim:
            self.app.toast("本机没有申请临时密钥，请联系管理员撤销设备后重新邀请", duration=5000)
            return
        installation_code = str(record.get("installation_code") or "")
        if installation_code and installation_code != self._installation_code:
            message = ("当前本机码不可用，无法核对已审批申请；请恢复原本机标识后重试。"
                       if not self._installation_code else
                       "当前本机码与已审批申请不一致；请用原终端领取或联系管理员复核。")
            self.app.toast(message, duration=6000)
            return
        def work():
            client = self._team_guest_client(origin)
            claim_payload = {"claim_secret": claim}
            if installation_code:
                claim_payload["installation_code"] = installation_code
            code, data, code_sent = client.post_with_optional_installation_code(
                f"/api/v1/team/join-requests/{app_id}/claim",
                claim_payload, timeout=12)
            if code != 200:
                return {"origin": origin, "code": code, "data": data,
                        "record": record}
            token = str(data.get("device_token") or "")
            team = data.get("team") or {}
            claim_binding, _claim_bound = installation_code_confirmation(
                data, installation_code)
            if not claim_binding:
                claim_binding = str(record.get("installation_code_binding") or "")
            updated = save_team_connection({
                **record, "team_id": team.get("team_id") or record.get("team_id"),
                "team_name": team.get("team_name") or record.get("team_name"),
                "user_id": data.get("user_id"), "device_id": data.get("device_id"),
                "status": "joined",
                "previous_llm_base": self.client.base,
                "installation_code_binding": claim_binding or None,
            }, device_token=token)
            member = ApiClient(origin, token=token,
                               token_header="X-Team-Device-Token",
                               use_default_token=False, deny_redirects=True)
            verify_code, verify_data = member.get("/api/v1/team/me", timeout=10)
            if verify_code == 200:
                delete_team_claim_secret(origin, app_id)
            data.pop("device_token", None)
            return {"origin": origin, "code": verify_code, "data": verify_data,
                    "record": updated, "claim_data": data,
                    "installation_code_sent": code_sent}
        self._start_team_action("claim", work)

    def _bootstrap_team(self) -> None:
        try:
            origin = self._team_origin()
            confirmed = self._team_confirmed or {}
            actual_login = str(confirmed.get("tailscale_login") or "").casefold()
            local_base = normalize_team_origin(self._team_value("Hub 本机地址"))
        except ValueError as exc:
            self.app.toast(str(exc), duration=4200)
            return
        name = self._team_value("团队名称")
        login = self._team_value("管理员账号").casefold()
        admin_name = self._team_value("管理员显示名")
        if not name or not login or not admin_name:
            self.app.toast("请填写团队名、管理员登录名和显示名")
            return
        if confirmed.get("origin") != origin:
            self.app.toast("请先核对 Hub 地址和团队名称")
            return
        if actual_login and login != actual_login:
            self.app.toast(f"管理员登录名与 Serve 身份不符：当前身份是 {actual_login}", duration=5000)
            return
        if urlparse(local_base).hostname not in {"127.0.0.1", "localhost", "::1"}:
            self.app.toast("初始化接口必须使用 Hub 本机地址")
            return
        identity_note = (f"本机 Serve 身份：{actual_login}" if actual_login
                         else "直连模式：请确认管理员账号标识")
        if not messagebox.askyesno("初始化 Venus 团队",
                                   f"将在此 Hub 创建一次性团队：\n{name}\n{origin}\n管理员：{login}\n{identity_note}",
                                   parent=self):
            return
        def work():
            from secure_store import load as secure_load
            credential = secure_load("team_bootstrap_token") or ""
            if not credential:
                return {"code": 401, "data": {"detail": "本机没有可用的启动初始化凭证"},
                        "origin": origin}
            local = ApiClient(local_base, token=credential,
                              token_header="X-Team-Bootstrap-Token",
                              use_default_token=False, deny_redirects=True)
            local_check = ApiClient(local_base, use_default_token=False,
                                    deny_redirects=True)
            local_code, local_info = local_check.get("/api/v1/team/public", timeout=6)
            expected_host = (urlparse(origin).hostname or "").casefold()
            if (local_code != 200 or local_info.get("initialized") is not False
                    or (local_info.get("connection_mode") == "direct"
                        and local_info.get("instance_id") != (confirmed.get("team") or {}).get("instance_id"))
                    or (local_info.get("connection_mode") != "direct"
                        and str(local_info.get("serve_host") or "").casefold() != expected_host)):
                return {"code": 409, "data": {"detail":
                        "本机后端不是可初始化的 Hub；未发送启动凭证"},
                        "origin": origin}
            code, data = local.post("/api/v1/team/bootstrap", {
                "team_name": name, "admin_login": login,
                "admin_name": admin_name, "device_name": "Hub 管理设备",
            }, timeout=10)
            if code == 200:
                result_team = data.get("team") or {}
                token = str(data.get("device_token") or "")
                record = save_team_connection({
                    "origin": origin, "team_id": result_team.get("team_id"),
                    "team_name": result_team.get("team_name"),
                    "user_id": (data.get("user") or {}).get("id"),
                    "device_id": data.get("device_id"),
                    "display_name": admin_name, "status": "joined",
                    "previous_llm_base": self.client.base,
                }, device_token=token)
                member = ApiClient(origin, token=token,
                                   token_header="X-Team-Device-Token",
                                   use_default_token=False, deny_redirects=True)
                verify_code, verify_data = member.get("/api/v1/team/me", timeout=10)
                data.pop("device_token", None)
                return {"origin": origin, "code": verify_code if verify_code != 0 else code,
                        "data": data, "verify": verify_data,
                        "record": record, "initialized": True}
            return {"origin": origin, "code": code, "data": data}
        self._start_team_action("bootstrap", work)

    def _create_team_invite(self) -> None:
        try:
            origin = self._team_origin()
        except ValueError as exc:
            self.app.toast(str(exc), duration=4200)
            return
        record = team_connection(origin)
        if not record:
            self.app.toast("请先加入并连接此团队")
            return
        login = self._team_value("受邀成员账号")
        role = self._team_value("邀请角色") or "member"
        if not login:
            self.app.toast("请填写受邀成员账号")
            return
        def work():
            client = self._team_member_client(origin, str(record.get("team_id") or ""))
            code, data = client.post("/api/v1/team/invites", {
                "expected_login": login, "role": role,
            }, timeout=10)
            return {"origin": origin, "code": code, "data": data,
                    "record": record}
        self._start_team_action("invite", work)

    def _refresh_team_admin(self) -> None:
        try:
            origin = self._team_origin()
        except ValueError as exc:
            self.app.toast(str(exc), duration=4200)
            return
        record = team_connection(origin)
        if not record:
            self.app.toast("当前 Hub 尚未保存已加入的团队身份")
            return
        def work():
            client = self._team_member_client(origin, str(record.get("team_id") or ""))
            results = {}
            for key, path in (("apps", "/api/v1/team/applications"),
                              ("invites", "/api/v1/team/invites"),
                              ("members", "/api/v1/team/members")):
                results[key] = client.get(path, timeout=10)
            return {"origin": origin, "record": record, "results": results}
        self._start_team_action("admin_refresh", work)

    def _review_selected_application(self, decision: str) -> None:
        if not self._team_apps_list:
            return
        selection = self._team_apps_list.curselection()
        if not selection or selection[0] >= len(self._team_app_rows):
            self.app.toast("请选择一条加入申请")
            return
        application = self._team_app_rows[selection[0]]
        if application.get("status") != "pending":
            self.app.toast("所选申请已处理，请刷新列表")
            return
        action = "批准" if decision == "approve" else "拒绝"
        prompt = (f"{action}加入申请？\n\n预期登录名：{application.get('expected_login')}\n"
                  f"实际 Serve 身份：{application.get('actual_login')}\n"
                  f"设备：{application.get('device_name')}\n编号：{application.get('id')}")
        if not messagebox.askyesno(f"{action}加入申请", prompt, parent=self):
            return
        self._run_admin_action(
            f"application_{decision}",
            f"/api/v1/team/applications/{application.get('id')}/review",
            {"decision": decision})

    def _revoke_selected_invite(self) -> None:
        if not self._team_invites_list:
            return
        selection = self._team_invites_list.curselection()
        if not selection or selection[0] >= len(self._team_invite_rows):
            self.app.toast("请选择一条邀请码")
            return
        invite = self._team_invite_rows[selection[0]]
        if invite.get("status") == "revoked":
            self.app.toast("该邀请码已撤销")
            return
        if not messagebox.askyesno("撤销邀请码", "此邀请码将立即失效，继续吗？", parent=self):
            return
        self._run_admin_action("invite_revoke",
                               f"/api/v1/team/invites/{invite.get('id')}", None,
                               method="DELETE")

    def _revoke_selected_device(self) -> None:
        if not self._team_members_list:
            return
        selection = self._team_members_list.curselection()
        if not selection or selection[0] >= len(self._team_member_rows):
            self.app.toast("请选择一台设备")
            return
        row = self._team_member_rows[selection[0]]
        if row.get("kind") != "device":
            self.app.toast("请选择设备行")
            return
        if not messagebox.askyesno("撤销设备", f"撤销设备“{row.get('name')}”？", parent=self):
            return
        self._run_admin_action("device_revoke",
                               f"/api/v1/team/devices/{row.get('id')}/revoke", {})

    def _deactivate_selected_member(self) -> None:
        if not self._team_members_list:
            return
        selection = self._team_members_list.curselection()
        if not selection or selection[0] >= len(self._team_member_rows):
            self.app.toast("请选择一名成员")
            return
        row = self._team_member_rows[selection[0]]
        if row.get("kind") != "member":
            self.app.toast("请选择成员行")
            return
        if not messagebox.askyesno("停用团队成员",
                                   f"停用 {row.get('name')} 会撤销其所有设备凭证。继续吗？",
                                   parent=self):
            return
        self._run_admin_action("member_deactivate",
                               f"/api/v1/team/members/{row.get('id')}/deactivate", {})

    def _run_admin_action(self, action: str, path: str, payload: dict | None,
                          *, method: str = "POST") -> None:
        try:
            origin = self._team_origin()
        except ValueError as exc:
            self.app.toast(str(exc), duration=4200)
            return
        record = team_connection(origin)
        if not record:
            self.app.toast("当前团队连接不存在")
            return
        def work():
            client = self._team_member_client(origin, str(record.get("team_id") or ""))
            code, data = client.request(method, path, payload, timeout=12)
            return {"origin": origin, "code": code, "data": data,
                    "record": record}
        self._start_team_action(action, work)

    def _leave_team_device(self) -> None:
        try:
            origin = self._team_origin()
        except ValueError as exc:
            self.app.toast(str(exc), duration=4200)
            return
        record = team_connection(origin)
        if not record or not record.get("device_id"):
            self.app.toast("当前设备没有加入此团队")
            return
        if not messagebox.askyesno("退出此设备", "撤销本机当前团队凭证？此设备之后需要重新邀请加入。", parent=self):
            return
        def work():
            client = self._team_member_client(origin, str(record.get("team_id") or ""))
            code, data = client.post(
                f"/api/v1/team/devices/{record['device_id']}/revoke", {}, timeout=10)
            if code == 200:
                clear_team_device_token(origin, str(record.get("team_id") or ""))
                save_team_connection({**record, "device_id": "", "status": "revoked"})
            elif code == 401 and any(text in str(data.get("detail") or "")
                                     for text in ("此设备凭证已撤销", "团队成员已停用")):
                clear_team_device_token(origin, str(record.get("team_id") or ""))
                save_team_connection({**record, "device_id": "", "status": "revoked"})
                return {"origin": origin, "code": 200, "data": data,
                        "record": record, "already_revoked": True}
            return {"origin": origin, "code": code, "data": data, "record": record}
        self._start_team_action("leave", work)

    def _activate_team_hub(self, origin: str, record: dict) -> None:
        previous = personal_backend_for_team(
            origin,
            record.get("previous_llm_base") or self.app._personal_backend_base
            or self.client.base,
            load_config())
        record = {**record, "previous_llm_base": previous, "status": "joined"}
        save_team_connection(record)
        save_local_config({"team_last_origin": origin})
        self.app.configure_team_backend(
            origin, str(record.get("team_name") or "团队 Hub"), previous)
        self.app.toast(f"已连接团队 Hub：{record.get('team_name') or origin}")

    @staticmethod
    def _friendly_team_error(code: int, data: dict) -> str:
        detail = str(data.get("detail") or "") if isinstance(data, dict) else ""
        if code == 0:
            return "无法连接 Hub，请检查地址、端口和 TLS 证书设置"
        if code in (301, 302, 303, 307, 308):
            return "Hub 地址发生跳转，已阻止凭证跨源发送；请使用管理员提供的服务器地址"
        if code == 401:
            if "成员已停用" in detail:
                return "已停用：请联系管理员重新邀请"
            if "连接密码" in detail:
                return "连接密码错误，请在连接设置中更新密码"
            if "Tailscale" in detail or "身份" in detail:
                return "当前身份与此团队成员不匹配"
            return "凭证失效或申请尚未授权，请刷新状态或联系管理员"
        if code == 403 and ("登录名" in detail or "身份" in detail):
            return "身份不匹配：邀请码与当前成员不匹配"
        if code == 410:
            return detail or "邀请码或设备领取已过期/使用"
        if code == 429:
            return "请求过于频繁，请稍后再试"
        return detail or f"团队请求失败（HTTP {code}）"

    def _action_command(self, button_text: str):
        mapping = {
            "刷新": lambda: self._refresh_page_data(self.active_page),
            "刷新列表": lambda: self._run_action("extensions", "读取扩展", "GET", "/api/v1/extensions"),
            "立即重建": lambda: self._run_action("codegraph", "重建索引", "POST", "/api/v1/codegraph/rebuild"),
        }
        return mapping.get(button_text, lambda: self.app.toast("此操作暂不可用"))

    def _test_connection(self) -> None:
        if "model" in self._dirty_pages:
            self.app.toast("请先保存模型设置，再测试连接")
            return
        self._run_action("model", "测试模型连接", "POST", "/api/v1/test")

    # Provider-direct model fetch & probe (no save needed) --------------------
    def _current_provider_inputs(self) -> tuple[str, str, str]:
        url_ctrl = self.local_controls.get("API URL")
        key_ctrl = self.local_controls.get("API Key")
        model_ctrl = self.local_controls.get("model")
        return (
            str(url_ctrl.get()).strip() if url_ctrl is not None and hasattr(url_ctrl, "get") else "",
            str(key_ctrl.get()).strip() if key_ctrl is not None and hasattr(key_ctrl, "get") else "",
            str(model_ctrl.get()).strip() if model_ctrl is not None and hasattr(model_ctrl, "get") else "",
        )

    def _on_choice_picked(self) -> None:
        if self._applying_data or not self._page_ready:
            return
        choices = self.local_controls.get("model_choices")
        model = self.local_controls.get("model")
        if choices is None or model is None:
            return
        picked = str(choices.get()).strip()
        if picked and hasattr(model, "set") and picked != str(model.get()).strip():
            model.set(picked)  # 经模型输入框的 trace 标脏，提示保存

    def _populate_model_choices(self, models: list[str]) -> None:
        choices = self.local_controls.get("model_choices")
        model = self.local_controls.get("model")
        if choices is None or model is None:
            return
        self._applying_data = True
        try:
            choices.set_values(models)
            current = str(model.get()).strip()
            if current in models:
                choices.variable.set(current)
            elif models:
                choices.variable.set(models[0])
        finally:
            self._applying_data = False

    def _fetch_models(self) -> None:
        api_url, api_key, _model = self._current_provider_inputs()
        if not api_url:
            self.app.toast("请先填写 API URL")
            return
        try:
            base = self._valid_url(api_url, "API URL")
        except ValueError as exc:
            self.app.toast(str(exc), duration=4200)
            return
        self._run_provider_job("获取模型列表", {"op": "models", "base": base, "key": api_key})

    def _probe_current(self) -> None:
        api_url, api_key, model = self._current_provider_inputs()
        if not api_url:
            self.app.toast("请先填写 API URL")
            return
        try:
            base = self._valid_url(api_url, "API URL")
        except ValueError as exc:
            self.app.toast(str(exc), duration=4200)
            return
        if not model:
            picked = self.local_controls.get("model_choices")
            if picked is not None and hasattr(picked, "get"):
                candidate = str(picked.get()).strip()
                if candidate and not candidate.startswith("点击"):
                    model = candidate
        self._run_provider_job("测试当前连接",
                               {"op": "probe", "base": base, "key": api_key, "model": model})

    def _run_provider_job(self, label: str, spec: dict) -> None:
        if self._action_busy:
            self.app.toast("正在执行上一项操作…")
            return
        self._action_busy = True
        self.footer_hint.configure(text=f"{label}中…")

        def job():
            try:
                if spec.get("op") == "models":
                    models = fetch_provider_models(str(spec["base"]), str(spec.get("key") or ""))
                    return ("model", label, 200, {"models": models})
                ok, message, _ms = probe_provider(str(spec["base"]), str(spec.get("key") or ""),
                                                  str(spec.get("model") or ""))
                return ("model", label, 200 if ok else 503, {"message": message})
            except Exception as exc:
                return ("model", label, 0, {"detail": str(exc)})

        self.app.bridge.submit("settings_action", job)

    def _run_action(self, page: str, label: str, method: str, path: str) -> None:
        if self._action_busy:
            self.app.toast("正在执行上一项操作…")
            return
        self._action_busy = True
        self.footer_hint.configure(text=f"{label}中…")
        def job():
            code, data = self.client.get(path) if method == "GET" else self.client.post(path)
            return (page, label, code, data)
        self.app.bridge.submit("settings_action", job)

    def _handle_action_result(self, payload) -> None:
        self._action_busy = False
        if not isinstance(payload, tuple) or len(payload) != 4:
            detail = payload[1] if isinstance(payload, tuple) and len(payload) == 2 else payload
            self.app.toast(f"操作失败：{detail}", duration=4200)
            self.footer_hint.configure(text="操作失败")
            return
        page, label, code, data = payload
        if code != 200:
            self.app.toast(f"{label}失败：{data.get('detail') or data.get('message') or code}", duration=4200)
            self.footer_hint.configure(text="操作失败，可重试")
            return
        if label == "重建索引":
            message = f"索引已重建：{data.get('files', 0)} 个文件"
        elif label == "读取扩展":
            message = f"已读取 {len(data.get('plugins') or data.get('extensions') or [])} 个扩展项"
        elif label == "获取模型列表":
            models = [str(m) for m in (data.get("models") or [])]
            self._populate_model_choices(models)
            message = f"已获取 {len(models)} 个可用模型，请在下拉中选择"
        elif label == "测试当前连接":
            message = str(data.get("message") or "连接正常")
        else:
            message = f"连接成功：{data.get('model') or ''}"
        self.app.toast(message)
        if page == self.active_page:
            if label in {"获取模型列表", "测试当前连接"}:
                self.footer_hint.configure(text="就绪")
            else:
                self._refresh_page_data(page)

    # Form helpers -----------------------------------------------------------
    def _section(
        self,
        parent: tk.Misc,
        title: str,
        subtitle: str,
        *,
        status: str = "",
    ) -> tuple[tk.Frame, tk.Frame]:
        section = tk.Frame(parent, bg=t.CANVAS)
        section.pack(fill="x", padx=t.s(3))
        heading = tk.Frame(section, bg=t.CANVAS)
        heading.pack(fill="x", pady=(t.s(2), t.s(8)))
        copy = tk.Frame(heading, bg=t.CANVAS)
        copy.pack(side="left")
        tk.Label(
            copy,
            text=title,
            bg=t.CANVAS,
            fg=t.INK,
            font=self.fonts.display_md,
        ).pack(anchor="w")
        if subtitle:
            tk.Label(
                copy,
                text=subtitle,
                bg=t.CANVAS,
                fg=t.INK_MUTED,
                font=self.fonts.caption,
            ).pack(anchor="w", pady=(t.s(3), 0))
        if status:
            status_row = tk.Frame(heading, bg=t.CANVAS)
            status_row.pack(side="right", pady=t.s(5))
            Dot(status_row, color=t.WARNING, size=6, bg=t.CANVAS).pack(side="left", padx=(0, t.s(6)))
            tk.Label(
                status_row,
                text=status,
                bg=t.CANVAS,
                fg=t.WARNING,
                font=self.fonts.caption,
            ).pack(side="left")
        body = tk.Frame(section, bg=t.CANVAS)
        body.pack(fill="x")
        return section, body

    def _finish_section(self, section: tk.Frame, *, final: bool = False) -> None:
        if not final:
            separator(section, color=t.LINE).pack(fill="x", pady=(t.s(10), t.s(11)))
        else:
            tk.Frame(section, bg=t.CANVAS, height=t.s(14)).pack(fill="x")

    def _row_shell(self, parent: tk.Misc, label: str) -> tk.Frame:
        row = tk.Frame(parent, bg=t.CANVAS)
        row.pack(fill="x", pady=t.s(5))
        row.columnconfigure(0, weight=0, minsize=t.s(140))
        row.columnconfigure(1, weight=1)
        tk.Label(
            row,
            text=label,
            bg=t.CANVAS,
            fg=t.INK_SOFT,
            font=self.fonts.body,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=(t.s(1), t.s(14)))
        return row

    def _field_row(
        self,
        parent: tk.Misc,
        label: str,
        value: str,
        placeholder: str,
        *,
        secret: bool = False,
    ) -> MinimalField:
        row = self._row_shell(parent, label)
        field = MinimalField(
            row,
            font=self.fonts.body,
            value=value,
            placeholder=placeholder,
            show="•" if secret else "",
        )
        field.grid(row=0, column=1, columnspan=2, sticky="ew")
        self.local_controls[label] = field
        field.variable.trace_add("write", lambda *_args: self._mark_dirty())
        return field

    def _switch_row(self, parent: tk.Misc, label: str, description: str, value: bool) -> None:
        row = tk.Frame(parent, bg=t.CANVAS)
        row.pack(fill="x", pady=t.s(7))
        copy = tk.Frame(row, bg=t.CANVAS)
        copy.pack(side="left", fill="x", expand=True)
        tk.Label(
            copy,
            text=label,
            bg=t.CANVAS,
            fg=t.INK_SOFT,
            font=self.fonts.body,
        ).pack(anchor="w")
        tk.Label(
            copy,
            text=description,
            bg=t.CANVAS,
            fg=t.INK_MUTED,
            font=self.fonts.caption,
        ).pack(anchor="w", pady=(t.s(2), 0))
        Switch(row, value=value, bg=t.CANVAS).pack(side="right", padx=(t.s(20), t.s(4)))

    def _info_row(self, parent: tk.Misc, label: str, value: str, color: str) -> None:
        row = tk.Frame(parent, bg=t.CANVAS, height=t.s(39))
        row.pack(fill="x")
        row.pack_propagate(False)
        tk.Label(
            row,
            text=label,
            bg=t.CANVAS,
            fg=t.INK_SOFT,
            font=self.fonts.body,
        ).pack(side="left")
        right = tk.Frame(row, bg=t.CANVAS)
        right.pack(side="right", padx=t.s(4))
        if color in {t.SUCCESS, t.WARNING, t.TERRACOTTA}:
            Dot(right, color=color, size=6, bg=t.CANVAS).pack(side="left", padx=(0, t.s(7)))
        val_lbl = tk.Label(
            right,
            text=value,
            bg=t.CANVAS,
            fg=color,
            font=self.fonts.small,
        )
        val_lbl.pack(side="left")
        self._dynamic_info[label] = val_lbl

    def _action_row(self, parent: tk.Misc, label: str, description: str, button_text: str, command) -> None:
        row = tk.Frame(parent, bg=t.CANVAS)
        row.pack(fill="x", pady=t.s(8))
        copy = tk.Frame(row, bg=t.CANVAS)
        copy.pack(side="left", fill="x", expand=True)
        tk.Label(
            copy,
            text=label,
            bg=t.CANVAS,
            fg=t.INK_SOFT,
            font=self.fonts.body,
        ).pack(anchor="w")
        tk.Label(
            copy,
            text=description,
            bg=t.CANVAS,
            fg=t.INK_MUTED,
            font=self.fonts.caption,
        ).pack(anchor="w", pady=(t.s(2), 0))
        FlatButton(
            row,
            button_text,
            command,
            font=self.fonts.small,
            variant="outline",
            height=36,
            min_width=108,
            parent_bg=t.CANVAS,
        ).pack(side="right", padx=t.s(4))

    def _save(self) -> None:
        page = self.active_page
        if self._saving or page not in EDITABLE_PAGES:
            return
        values = self._snapshot_controls()
        try:
            method, path, payload = self._save_request(page, values)
        except ValueError as exc:
            self.app.toast(str(exc), duration=4200)
            return
        self._saving = True
        self._save_serial += 1
        serial = self._save_serial
        self._save_snapshot = values
        self.footer_hint.configure(text="正在保存…")
        self.save_button.set_enabled(False)
        request_epoch = self.app.bridge.context_epoch
        def job():
            if method == "LOCAL":
                local_payload = payload
                with self.app._backend_switch_lock:
                    if (page == "advanced"
                            and request_epoch != self.app.bridge.context_epoch):
                        # A context switch committed its active llm_base while
                        # this old-context local save was queued.
                        local_payload = {k: v for k, v in payload.items()
                                         if k != "llm_base"}
                    save_local_config(local_payload)
                return (serial, page, 200, {"ok": True})
            code, data = self.client.post(path, payload)
            return (serial, page, code, data)
        self.app.bridge.submit("settings_save", job)

    @staticmethod
    def _valid_url(value: str, label: str, *, local: bool = False) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"{label}需要完整的 http(s) 地址")
        if local and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(f"{label}仅支持本机地址")
        return value.rstrip("/")

    def _save_request(self, page: str, values: dict[str, object]) -> tuple[str, str, dict]:
        get = lambda key: str(values.get(key) or "").strip()
        if page == "model":
            context = get("上下文窗口").replace(",", "")
            if not context.isdecimal() or int(context) <= 0:
                raise ValueError("上下文窗口请输入正整数")
            if not get("model"):
                raise ValueError("请填写模型标识")
            payload = {
                "api_url": self._valid_url(get("API URL"), "API URL"),
                "model": get("model"),
                "context_window": int(context),
                "reasoning_mode": {"最高": "max", "高": "high", "关闭": "off"}.get(get("reasoning"), "max"),
            }
            if get("API Key"):
                payload["api_key"] = get("API Key")
            return "POST", "/api/v1/config", payload
        if page == "vision":
            payload = {"vision_api_url": self._valid_url(get("Vision URL"), "Vision URL"),
                       "vision_model": get("Vision Model")}
            if not payload["vision_model"]:
                raise ValueError("请填写视觉模型标识")
            if get("Vision Key"):
                payload["vision_api_key"] = get("Vision Key")
            return "POST", "/api/v1/config", payload
        if page == "router":
            payload = {"tool_router": bool(values.get("tool_router")),
                       "tool_router_url": self._valid_url(get("Ollama URL"), "Ollama URL", local=True),
                       "tool_router_model": get("路由模型")}
            if payload["tool_router"] and not payload["tool_router_model"]:
                raise ValueError("请填写路由模型标识")
            return "POST", "/api/v1/config", payload
        if page == "memory":
            return "POST", "/api/v1/config", {"memory_enabled": bool(values.get("启用记忆")),
                                               "llm_memory_extract": bool(values.get("自动提取"))}
        if page == "permissions":
            mode = {"智能": "auto", "严格": "strict", "只读": "query"}.get(get("confirm_mode"), "auto")
            return "POST", "/api/v1/confirm-mode", {"mode": mode}
        if page == "sandbox":
            mode = {"工作区": "workspace", "本机": "host", "WSL": "wsl"}.get(get("sandbox_mode"), "workspace")
            return "POST", "/api/v1/sandbox/default", {"mode": mode}
        if page == "common":
            if not get("默认工作区"):
                raise ValueError("请填写工作区绝对路径")
            return "POST", "/api/v1/workspace", {"path": get("默认工作区")}
        if page == "advanced":
            return "LOCAL", "", {"daemon_base": self._valid_url(get("Desktop URL"), "Desktop URL", local=True),
                                   "llm_base": self._valid_url(get("Agent URL"), "Agent URL")}
        raise ValueError("当前页面没有可保存的设置")

    def _handle_save_result(self, payload) -> None:
        self._saving = False
        if not isinstance(payload, tuple) or len(payload) != 4:
            self._save_snapshot = {}
            self.footer_hint.configure(text="保存失败，可重试")
            detail = payload[1] if isinstance(payload, tuple) and len(payload) == 2 else payload
            self.app.toast(f"保存失败：{detail}", duration=4200)
            self._update_footer()
            return
        serial, page, code, data = payload
        if serial != self._save_serial:
            return
        if code != 200 or not data.get("ok", True):
            self._save_snapshot = {}
            self.footer_hint.configure(text="保存失败，可重试")
            self.app.toast(f"保存失败：{data.get('detail', code)}", duration=4200)
            self._update_footer()
            return
        if page == "advanced":
            config = load_config()
            configured = str(config.get("llm_base") or self.client.base).rstrip("/")
            team_origins = {
                str(row.get("origin") or "").rstrip("/")
                for row in (config.get("team_connections") or [])
                if isinstance(row, dict) and row.get("origin")
            }
            current = self.app.client.base.rstrip("/")
            team_origin = (current if current in team_origins
                           else self.app._team_backend_origin)
            configured = personal_backend_for_team(team_origin, configured, config)
            if current in team_origins:
                self.app._personal_backend_base = personal_backend_for_team(
                    current, configured, config)
                self.app._refresh_backend_switch_control()
            else:
                self.app._personal_backend_base = configured
                if current != configured:
                    self.app.switch_backend_context(configured, "个人对话")
        if page == "model":
            self.app.model_name = str(self._save_snapshot.get("model") or self.app.model_name)
        current_values = (self._snapshot_controls() if page == self.active_page
                          else self._drafts.get(page, self._save_snapshot))
        if current_values == self._save_snapshot:
            self._dirty_pages.discard(page)
            self._drafts.pop(page, None)
        self._save_snapshot = {}
        self.app.toast("设置已保存")
        self._update_footer()
        self.app.bridge.refresh_all()
        if page == self.active_page:
            self._refresh_page_data(page)


# Provider-direct helpers (OpenAI-compatible; backend untouched) ---------------

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_local_base(base: str) -> bool:
    try:
        return (urlparse(base).hostname or "") in _LOCAL_HOSTS
    except Exception:
        return False


def _provider_error(code: int, data: dict) -> str:
    if code == 0:
        return f"网络错误：{data.get('detail') or '无法连接'}"
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return f"HTTP {code}：{err['message']}"
        for key in ("message", "detail"):
            if data.get(key):
                return f"HTTP {code}：{data[key]}"
    return f"HTTP {code}"


def _provider_request(base: str, key: str, path: str, *, timeout: float,
                      method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    url = base.rstrip("/") + path
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw)
                return resp.status, parsed if isinstance(parsed, dict) else {"data": parsed}
            except ValueError:
                return resp.status, {"detail": "non-JSON response"}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace"))
            if not isinstance(detail, dict):
                detail = {"detail": str(detail)}
        except Exception:
            detail = {"detail": f"HTTP {exc.code}"}
        return exc.code, detail
    except Exception as exc:
        return 0, {"detail": str(exc)}


def _extract_model_ids(data: dict) -> list[str]:
    buckets: list = []
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            buckets = data["data"]
        elif isinstance(data.get("models"), list):
            buckets = data["models"]
    ids: list[str] = []
    for item in buckets:
        if isinstance(item, dict):
            for key in ("id", "name"):
                value = item.get(key)
                if value:
                    ids.append(str(value))
                    break
        elif isinstance(item, str):
            ids.append(item)
    return sorted({i.strip() for i in ids if str(i).strip()})


def fetch_provider_models(base: str, key: str, *, timeout: float = 15) -> list[str]:
    """GET {base}/models（OpenAI 兼容），本机地址 404 时回退 Ollama /api/tags。"""
    code, data = _provider_request(base, key, "/models", timeout=timeout)
    if code == 404 and _is_local_base(base):
        code, data = _provider_request(base, key, "/api/tags", timeout=timeout)
    if code == 401:
        raise RuntimeError("鉴权失败（401）：请检查 API Key")
    if code != 200:
        raise RuntimeError(_provider_error(code, data))
    models = _extract_model_ids(data)
    if not models:
        raise RuntimeError("接口返回成功，但没有解析到可用模型")
    return models


def probe_provider(base: str, key: str, model: str, *,
                   timeout: float = 10) -> tuple[bool, str, int]:
    """先验连通与鉴权，再用指定模型做一次最小 chat 调用。返回 (ok, message, ms)。"""
    started = time.monotonic()
    elapsed = lambda: int((time.monotonic() - started) * 1000)
    code, data = _provider_request(base, key, "/models", timeout=timeout)
    if code == 404 and _is_local_base(base):
        code, data = _provider_request(base, key, "/api/tags", timeout=timeout)
    if code == 401:
        return False, "鉴权失败（401）：请检查 API Key", elapsed()
    list_note = ""
    if code == 404:
        list_note = "（该地址不支持列表接口，已跳过）"
    elif code != 200:
        return False, f"服务不可达：{_provider_error(code, data)}", elapsed()
    if not model:
        return True, f"连接正常{list_note}：{elapsed()} ms（未指定模型，仅验证连通）", elapsed()
    code2, data2 = _provider_request(
        base, key, "/chat/completions", timeout=25, method="POST",
        payload={"model": model, "messages": [{"role": "user", "content": "hi"}],
                 "max_tokens": 1, "stream": False},
    )
    if code2 == 200:
        return True, f"连接正常：{model} 可用（{elapsed()} ms）{list_note}", elapsed()
    if code2 == 401:
        return False, "鉴权失败（401）：请检查 API Key", elapsed()
    if code2 == 404 and _is_local_base(base):
        code3, data3 = _provider_request(
            base, key, "/api/chat", timeout=25, method="POST",
            payload={"model": model, "messages": [{"role": "user", "content": "hi"}],
                     "stream": False},
        )
        if code3 == 200:
            return True, f"连接正常：{model} 可用（{elapsed()} ms）", elapsed()
        return False, f"模型不可用：{_provider_error(code3, data3)}", elapsed()
    return False, f"模型调用失败：{_provider_error(code2, data2)}", elapsed()
