"""Classical-minimal settings center for VenusChat V1."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import theme as t
from .api_client import ApiClient
from .config_store import load_config, save_local_config
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
            ("quant", "量化中心"),
            ("diagnostics", "诊断与用量"),
            ("advanced", "高级"),
        ),
    ),
)


PAGE_META = {
    "common": ("常规与工作区", "管理默认工作区、启动行为与界面语言"),
    "model": ("模型与推理", "配置主模型连接、上下文窗口与推理强度"),
    "vision": ("视觉模型", "为图像理解与视觉任务配置独立模型"),
    "permissions": ("权限与确认", "定义工具操作的确认范围与执行上限"),
    "sandbox": ("执行沙箱", "选择本机、工作区隔离或 WSL 执行环境"),
    "router": ("工具路由", "配置轻量模型进行工具预路由"),
    "subagents": ("子 Agent", "管理可调用的专业 Agent 与默认路由"),
    "memory": ("记忆与 Skill", "控制分层记忆、自动提取与动态 Skill"),
    "codegraph": ("CodeGraph", "管理代码符号索引与影响分析"),
    "integrations": ("MCP 与浏览器", "查看外部工具连接与浏览器状态"),
    "extensions": ("扩展", "管理本地扩展、Skill 与工具资产"),
    "quant": ("量化中心", "配置隔离的量化研究工作台"),
    "diagnostics": ("诊断与用量", "查看运行健康、上下文与 Token 用量"),
    "advanced": ("高级", "配置本地服务地址、令牌与数据目录"),
}


GENERIC_PAGES = {
    "common": (
        (
            "工作区",
            "决定新对话默认使用的位置。",
            (
                ("field", "默认工作区", "", "绝对路径，须存在"),
                ("switch", "恢复上次会话", "启动时回到最近打开的任务", True),
            ),
        ),
        (
            "界面",
            "只调整 VenusChat V1 的显示方式。",
            (
                ("select", "界面语言", ("简体中文", "English"), "简体中文"),
                ("select", "内容密度", ("舒适", "紧凑"), "舒适"),
                ("switch", "减少动态效果", "关闭非必要的过渡反馈", False),
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
                ("switch", "自动路由图片", "检测到图片时切换视觉模型", True),
            ),
        ),
    ),
    "permissions": (
        (
            "确认策略",
            "高风险操作始终需要明确确认。",
            (
                ("segment", "默认模式", ("智能", "严格", "只读"), "智能"),
                ("switch", "读取文件免确认", "仅限当前工作区内的只读操作", True),
                ("switch", "外部写入需确认", "离开工作区前显示明确预览", True),
            ),
        ),
        (
            "执行上限",
            "用于避免失控任务占用前台。",
            (
                ("field", "最大工具步数", "64", "正整数"),
                ("field", "任务超时", "1800 秒", "秒"),
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
                ("switch", "限制网络访问", "工具需要时单独申请网络权限", True),
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
                ("action", "路由测试", "使用一条示例请求验证分类结果", "开始测试"),
            ),
        ),
    ),
    "subagents": (
        (
            "可用 Agent",
            "来自后端 agents 目录与子 Agent 路由。",
            (
                ("info", "加载中", "…", t.INK_MUTED),
            ),
        ),
    ),
    "memory": (
        (
            "分层记忆",
            "L0–L3 记忆与动态 Skill，数据来自后端。",
            (
                ("info", "原子记忆", "—", t.INK_SOFT),
                ("info", "长期画像", "—", t.INK_SOFT),
                ("info", "动态 Skill", "—", t.INK_SOFT),
                ("action", "刷新统计", "从 /api/v1/memory 读取", "刷新"),
            ),
        ),
    ),
    "codegraph": (
        (
            "代码索引",
            "符号索引用于定位定义、调用与影响范围。",
            (
                ("info", "索引状态", "已就绪", t.SUCCESS),
                ("info", "文件", "246", t.INK_SOFT),
                ("info", "符号", "3,842", t.INK_SOFT),
                ("switch", "工作区变更后自动更新", "只处理发生变化的文件", True),
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
                ("switch", "启用内置浏览器", "允许 Agent 打开与检查页面", True),
                ("action", "刷新连接", "重新读取 MCP / 浏览器状态", "刷新"),
            ),
        ),
    ),
    "extensions": (
        (
            "本地扩展",
            "插件 catalog 与启用状态。",
            (
                ("action", "刷新扩展", "读取 /api/v1/extensions", "刷新列表"),
            ),
        ),
    ),
    "quant": (
        (
            "量化中心",
            "隔离运行的研究工作台，不参与主聊天布局。",
            (
                ("switch", "启用量化中心", "在顶部导航保留入口", True),
                ("field", "项目路径", "D:\\私人agent\\quant-agent-lab", "本地路径"),
                ("field", "服务地址", "http://127.0.0.1:8014", "Loopback URL"),
                ("select", "打开方式", ("自动", "浏览器", "原生窗口"), "自动"),
                ("action", "连接测试", "检查独立服务与界面可用性", "测试连接"),
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
            "本地服务地址与令牌；保存后写入 chat_config.json。",
            (
                ("field", "Desktop URL", "http://127.0.0.1:8000", "本地地址"),
                ("field", "Agent URL", "http://127.0.0.1:8001", "本地地址"),
                ("secret", "API Token", "", "Windows 安全存储"),
                ("secret", "Daemon Token", "", "Windows 安全存储"),
                ("switch", "开发者诊断", "显示更详细的前端状态", False),
            ),
        ),
    ),
}


class SettingsView(tk.Frame):
    """A complete settings frontend sharing the V1 global application shell."""

    def __init__(self, parent: tk.Misc, app, fonts: t.Fonts, client: ApiClient) -> None:
        super().__init__(parent, bg=t.CANVAS)
        self.app = app
        self.fonts = fonts
        self.client = client
        self.active_page = "model"
        self.nav_rows: dict[str, NavRow] = {}
        self.local_controls: dict[str, object] = {}
        self._dynamic_info: dict[str, tk.Label] = {}
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
            self.app.show_chat,
            font=self.fonts.body,
            variant="ghost",
            height=40,
            min_width=112,
            parent_bg=t.HEADER,
        ).pack(side="left", padx=(0, 10))
        FlatButton(
            actions,
            "保存更改",
            self._save,
            font=self.fonts.body_medium,
            variant="primary",
            height=40,
            min_width=160,
            parent_bg=t.HEADER,
        ).pack(side="left")
        tk.Label(
            footer,
            text="保存将写入 llm_server 配置（chat_config.json）",
            bg=t.HEADER,
            fg=t.INK_FAINT,
            font=self.fonts.caption,
        ).pack(side="left", padx=t.s(28), pady=t.s(25))

        self.bind("<Configure>", self._responsive_rail, add="+")
        self.select_page("model")

    def _build_sidebar(self) -> None:
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
        for group_title, items in NAV_GROUPS:
            matches = [item for item in items if not query or query in item[1].casefold()]
            if not matches:
                continue
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
    def select_page(self, key: str) -> None:
        if key not in PAGE_META:
            return
        self.active_page = key
        title, subtitle = PAGE_META[key]
        self.page_title.configure(text=title)
        self.page_subtitle.configure(text=subtitle)
        for row_key, row in self.nav_rows.items():
            row.set_active(row_key == key)
        for child in self.page_scroll.inner.winfo_children():
            child.destroy()
        self.local_controls.clear()
        if key == "model":
            self._build_model_page()
        else:
            self._build_generic_page(key)
        self.page_scroll.scroll_top()
        self.app.set_header_section("settings")
        self._refresh_page_data(key)

    def _refresh_page_data(self, key: str) -> None:
        if key == "model":
            code, data = self.client.get("/api/v1/config")
            if code == 200:
                cfg = data.get("config") or {}
                for label, field in self.local_controls.items():
                    if label == "API URL" and hasattr(field, "set"):
                        field.set(str(cfg.get("api_url") or ""))
                    elif label == "模型" and hasattr(field, "set"):
                        field.set(str(cfg.get("model") or ""))
                rm = str(cfg.get("reasoning_mode") or "max")
                seg = self.local_controls.get("reasoning")
                if seg is not None and hasattr(seg, "set"):
                    seg.set({"max": "最高", "high": "高", "off": "关闭"}.get(rm, "最高"))
        elif key == "common":
            code, data = self.client.get("/api/v1/workspace")
            if code == 200:
                for label, field in self.local_controls.items():
                    if label == "默认工作区" and hasattr(field, "set"):
                        field.set(str(data.get("workspace") or ""))
        elif key == "memory":
            code, data = self.client.get("/api/v1/health")
            if code == 200:
                mem = data.get("memory_stats") or {}
                self._set_info("原子记忆", f"{int(mem.get('l1_memories') or 0)} 条", t.SUCCESS)
                self._set_info("长期画像", "已启用" if mem.get("enabled", True) else "已关闭", t.SUCCESS)
                self._set_info("动态 Skill", f"{int(mem.get('dynamic_skills') or 0)} 个", t.SUCCESS)
        elif key == "codegraph":
            code, data = self.client.get("/api/v1/codegraph/stats")
            if code == 200:
                if data.get("built"):
                    self._set_info("索引状态", "已就绪", t.SUCCESS)
                    self._set_info("文件", str(data.get("files", 0)), t.INK_SOFT)
                    self._set_info("符号", str(data.get("symbols", 0)), t.INK_SOFT)
                else:
                    self._set_info("索引状态", "未构建", t.WARNING)
        elif key == "integrations":
            bcode, bdata = self.client.get("/api/v1/browser/status")
            if bcode == 200:
                st = "已启用" if bdata.get("enabled") else "未启用"
                self._set_info("浏览器 MCP", st, t.SUCCESS if bdata.get("enabled") else t.WARNING)
            mcode, mdata = self.client.get("/api/v1/mcp/status")
            if mcode == 200:
                n = len(mdata.get("servers") or [])
                conn = sum(1 for s in mdata.get("servers") or [] if s.get("connected"))
                self._set_info("MCP 服务", f"{conn}/{n} 已连接", t.SUCCESS if conn else t.WARNING)
        elif key == "diagnostics":
            hcode, hdata = self.client.get("/api/v1/health")
            ucode, udata = self.client.get("/api/v1/usage")
            if hcode == 200:
                self._set_info("模型服务", "正常" if hdata.get("configured") else "未配置",
                               t.SUCCESS if hdata.get("configured") else t.WARNING)
                self._set_info("版本", str(hdata.get("version") or "—"), t.INK_SOFT)
            if ucode == 200:
                total = int(udata.get("total_tokens") or 0)
                self._set_info("本次用量", f"{total:,} tokens", t.INK_SOFT)
        elif key == "subagents":
            code, data = self.client.get("/api/v1/agents")
            if code == 200:
                agents = data.get("agents") or []
                for i, a in enumerate(agents[:6]):
                    self._set_info(f"agent_{i}", str(a.get("name") or a.get("id")), t.SUCCESS)

    def _set_info(self, key: str, value: str, color: str) -> None:
        lbl = self._dynamic_info.get(key)
        if lbl is not None:
            lbl.configure(text=value, fg=color)

    def _responsive_rail(self, event: tk.Event) -> None:
        try:
            if event.width < t.s(1120):
                self.rail.grid_remove()
            else:
                self.rail.grid()
        except tk.TclError:
            pass

    # Model page -------------------------------------------------------------
    def _build_model_page(self) -> None:
        page = self.page_scroll.inner
        section, body = self._section(page, "主模型", "")
        self._field_row(body, "API URL", "https://api.deepseek.com/v1", "OpenAI 兼容地址")
        self._field_row(body, "API Key", "sk-venuschat-v1-preview", "输入 API Key", secret=True)
        row = self._row_shell(body, "模型")
        model = SelectField(
            row,
            ("deepseek-v4-flash", "deepseek-reasoner", "自定义模型"),
            font=self.fonts.body,
            value=self.app.model_name,
        )
        model.grid(row=0, column=1, sticky="ew")
        self.local_controls["model"] = model
        FlatButton(
            row,
            "测试连接",
            self._test_connection,
            font=self.fonts.small,
            variant="outline",
            height=38,
            min_width=105,
            parent_bg=t.CANVAS,
        ).grid(row=0, column=2, padx=(t.s(12), 0))
        self._finish_section(section)

        section, body = self._section(page, "推理与上下文", "")
        row = self._row_shell(body, "推理强度")
        reasoning = SegmentedControl(
            row,
            ("最高", "高", "关闭"),
            font=self.fonts.small,
            value="最高",
            bg=t.CANVAS,
        )
        reasoning.grid(row=0, column=1, columnspan=2, sticky="ew")
        self.local_controls["reasoning"] = reasoning
        value_row = self._row_shell(body, "上下文窗口")
        context_value = tk.StringVar(value="1,048,576 tokens")
        context = tk.Entry(
            value_row,
            textvariable=context_value,
            bg=t.SURFACE,
            fg=t.INK,
            font=self.fonts.body,
            relief="flat",
            highlightthickness=1,
            highlightbackground=t.LINE,
            highlightcolor=t.TERRACOTTA,
            insertbackground=t.INK,
        )
        context.grid(row=0, column=1, columnspan=2, sticky="ew", ipady=t.s(9))
        slider_row = tk.Frame(body, bg=t.CANVAS)
        slider_row.pack(fill="x", padx=(t.s(140), t.s(3)), pady=(t.s(3), t.s(2)))
        scale = ttk.Scale(
            slider_row,
            from_=0,
            to=100,
            value=100,
            orient="horizontal",
            style="Venus.Horizontal.TScale",
        )
        scale.pack(fill="x")
        labels = tk.Frame(slider_row, bg=t.CANVAS)
        labels.pack(fill="x")
        tk.Label(labels, text="0", bg=t.CANVAS, fg=t.INK_FAINT,
                 font=self.fonts.caption).pack(side="left")
        tk.Label(labels, text="512K", bg=t.CANVAS, fg=t.INK_FAINT,
                 font=self.fonts.caption).pack(side="left", expand=True)
        tk.Label(labels, text="1,048,576", bg=t.CANVAS, fg=t.INK_FAINT,
                 font=self.fonts.caption).pack(side="right")
        tk.Label(
            body,
            text="用于压缩阈值与容量提示，不改变模型本身限制",
            bg=t.CANVAS,
            fg=t.INK_FAINT,
            font=self.fonts.caption,
        ).pack(anchor="w", padx=(t.s(140), 0), pady=(t.s(5), 0))
        self._finish_section(section)

        section, body = self._section(page, "视觉模型", "", status="未配置")
        self._field_row(body, "Vision URL", "", "https://api.vision-provider.com/v1")
        self._field_row(body, "Vision Key", "", "输入 Vision API Key", secret=True)
        row = self._row_shell(body, "Vision Model")
        vision = SelectField(
            row,
            ("选择或输入视觉模型标识", "qwen-vl-max", "gpt-vision"),
            font=self.fonts.body,
            value="选择或输入视觉模型标识",
        )
        vision.grid(row=0, column=1, columnspan=2, sticky="ew")
        tk.Label(
            body,
            text="供图片预览与视觉子 Agent 使用",
            bg=t.CANVAS,
            fg=t.INK_FAINT,
            font=self.fonts.caption,
        ).pack(anchor="w", padx=(t.s(140), 0), pady=(t.s(7), 0))
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
                    field = SelectField(
                        row,
                        values,
                        font=self.fonts.body,
                        value=current,
                    )
                    field.grid(row=0, column=1, columnspan=2, sticky="ew")
                elif kind == "segment":
                    _, label, values, current = item
                    row = self._row_shell(body, label)
                    field = SegmentedControl(
                        row,
                        values,
                        font=self.fonts.small,
                        value=current,
                        bg=t.CANVAS,
                    )
                    field.grid(row=0, column=1, columnspan=2, sticky="ew")
                    key = {"默认模式": "confirm_mode", "环境": "sandbox_mode"}.get(label, label)
                    self.local_controls[key] = field
                elif kind == "switch":
                    _, label, description, value = item
                    row = tk.Frame(body, bg=t.CANVAS)
                    row.pack(fill="x", pady=t.s(7))
                    copy = tk.Frame(row, bg=t.CANVAS)
                    copy.pack(side="left", fill="x", expand=True)
                    tk.Label(copy, text=label, bg=t.CANVAS, fg=t.INK_SOFT, font=self.fonts.body).pack(anchor="w")
                    tk.Label(copy, text=description, bg=t.CANVAS, fg=t.INK_FAINT, font=self.fonts.caption).pack(anchor="w", pady=(t.s(2), 0))
                    sw = Switch(row, value=value, bg=t.CANVAS)
                    sw.pack(side="right", padx=(t.s(20), t.s(4)))
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

    def _action_command(self, button_text: str):
        mapping = {
            "刷新": lambda: self._refresh_page_data(self.active_page),
            "刷新列表": lambda: self._refresh_extensions(),
            "立即重建": self._rebuild_codegraph,
            "开始测试": self._test_router,
            "测试连接": self._test_quant,
        }
        return mapping.get(button_text, lambda: self.app.toast(f"{button_text}"))

    def _test_connection(self) -> None:
        code, data = self.client.post("/api/v1/test")
        if code == 200 and data.get("ok"):
            self.app.toast(f"连接成功：{data.get('model', '')}")
        else:
            self.app.toast(f"连接失败：{data.get('detail', code)}")

    def _rebuild_codegraph(self) -> None:
        code, data = self.client.post("/api/v1/codegraph/rebuild")
        if code == 200:
            self.app.toast(f"索引已重建：{data.get('files', 0)} 文件")
            self._refresh_page_data("codegraph")
        else:
            self.app.toast(f"重建失败：{data.get('detail', code)}")

    def _refresh_extensions(self) -> None:
        code, data = self.client.get("/api/v1/extensions")
        if code != 200:
            self.app.toast("读取扩展失败")
            return
        n = len(data.get("plugins") or data.get("extensions") or [])
        self.app.toast(f"已加载 {n} 个扩展项")

    def _test_router(self) -> None:
        self.app.toast("工具路由测试：请在对话中发送一条消息观察工具选择")

    def _test_quant(self) -> None:
        cfg = load_config()
        url = str(cfg.get("quant_backend_url") or "http://127.0.0.1:8014")
        code, data = self.client.get("/api/v1/health")
        _ = url, data
        self.app.toast("量化中心需独立启动 quant-agent-lab 服务")

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
                fg=t.INK_FAINT,
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
            fg=t.INK_FAINT,
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
            fg=t.INK_FAINT,
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
        payload: dict = {}
        if self.active_page == "model":
            url_f = self.local_controls.get("API URL")
            key_f = self.local_controls.get("API Key")
            model = self.local_controls.get("模型")
            reasoning = self.local_controls.get("reasoning")
            if url_f is not None and hasattr(url_f, "get"):
                payload["api_url"] = url_f.get().strip()
            if key_f is not None and hasattr(key_f, "get"):
                k = key_f.get().strip()
                if k and not k.startswith("•"):
                    payload["api_key"] = k
            if model is not None and hasattr(model, "get"):
                payload["model"] = model.get().strip()
            if reasoning is not None:
                rev = {"最高": "max", "高": "高", "关闭": "off"}
                payload["reasoning_mode"] = rev.get(getattr(reasoning, "value", "最高"), "max")
        elif self.active_page == "permissions":
            seg = self.local_controls.get("confirm_mode")
            if seg is not None:
                modes = {"智能": "auto", "严格": "strict", "只读": "query"}
                self.client.post("/api/v1/confirm-mode", {"mode": modes.get(seg.value, "auto")})
        elif self.active_page == "sandbox":
            seg = self.local_controls.get("sandbox_mode")
            if seg is not None:
                sm = {"工作区": "workspace", "本机": "host", "WSL": "wsl"}
                self.client.post("/api/v1/sandbox/default", {"mode": sm.get(seg.value, "workspace")})
        elif self.active_page == "common":
            ws = self.local_controls.get("默认工作区")
            if ws is not None and hasattr(ws, "get"):
                path = ws.get().strip()
                if path:
                    self.client.post("/api/v1/workspace", {"path": path})
        elif self.active_page == "router":
            for label, field in self.local_controls.items():
                if label == "Ollama URL" and hasattr(field, "get"):
                    payload["tool_router_url"] = field.get().strip()
                elif label == "路由模型" and hasattr(field, "get"):
                    payload["tool_router_model"] = field.get().strip()
            sw = self.local_controls.get("tool_router")
            if sw is not None:
                payload["tool_router"] = bool(sw.value)

        if payload:
            code, data = self.client.post("/api/v1/config", payload)
            if code != 200:
                self.app.toast(f"保存失败：{data.get('detail', code)}")
                return
            cfg = data.get("config") or {}
            if cfg.get("model"):
                self.app.set_model(str(cfg["model"]))
        save_local_config({"llm_base": load_config().get("llm_base")})
        self.app.toast("设置已保存")
        self.app.show_chat()
        self.app.bridge.refresh_all()
