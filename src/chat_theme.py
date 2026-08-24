"""Venus ChatApp 设计系统 ——「深空 · 冰蓝」(Deep Space / Ice Cyan)。

设计原则
--------
- 四级中性灰表面（BG0→BG5）承担层次，边框只做发丝级分隔，不用重线。
- 单一主强调色（冰青 CY）+ 次级身份色（紫 VI，代表 Agent），
  语义色（绿/琥珀/红）只在状态出现，绝不装饰。
- 8pt 间距网格；统一字阶：kicker（等宽小号大写）/ caption / body / title / display。
- 胶囊按钮与圆角卡片用 Canvas 绘制，给 tkinter 一个现代、克制、可呼吸的外观。

该模块只依赖标准库（tkinter），与 chat.py 保持零第三方依赖的前提一致。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

# ---------------------------------------------------------------------------
# 色板（Design Tokens）
# ---------------------------------------------------------------------------
# 中性表面阶（由深到浅）：窗口底色 → 大表面 → 抬升卡片 → 控件 → 悬停 → 选中
BG0 = "#04060c"     # 窗口底色
BG1 = "#070a13"     # 一级表面（聊天卡 / 工具栏 / 页眉页脚）
BG2 = "#0a0f1b"     # 抬升卡片（输入卡 / 侧栏脚 / 设置页头）
BG3 = "#0e1524"     # 控件基色（按钮 / 输入框 / 搜索框 / 下拉）
BG4 = "#131c30"     # 悬停
BG5 = "#182338"     # 选中 / 菜单面板

# 发丝边框阶
LINE0 = "#121b2d"   # 最弱分隔（区域间）
LINE1 = "#1c2941"   # 常规边框
LINE2 = "#2a3c5e"   # 悬停边框 / 强调边框
FOCUS = "#4a7fa6"   # 聚焦环（冰蓝灰）

SEL = "#274b61"     # 文本选中背景

# 文字阶
INK1 = "#f4f7fb"    # 主文字
INK2 = "#c9d4e2"    # 次级文字
INK3 = "#8b99ad"    # 弱化文字（说明 / 元信息）
INK4 = "#5d6c83"    # 最弱文字（占位 / 计数）

# 主强调：冰青
CY1 = "#8cecff"
CY2 = "#bdf5ff"     # 悬停提亮
CY_DEEP = "#2c6f8f" # 低亮冰蓝（发光条 / 装饰）
CY_FAINT = "#0d2434"  # 冰青着色底（徽章 / soft 按钮）
ON_CY = "#032530"   # 强调色背景上的文字

# 次级身份：紫（Agent 侧）
VI1 = "#b3a7ff"
VI_DEEP = "#4a3f7e"
VI_FAINT = "#16132e"

# 语义色
POS = "#4ee0b0"       # 成功 / 在线
POS_SOFT = "#0c2b23"
WARN = "#ffc26b"      # 提醒 / 待处理
WARN_SOFT = "#2f2310"
NEG = "#ff7285"       # 错误 / 停止 / 危险
NEG_SOFT = "#331420"

# 场景专用
SIDEBAR_BG = "#060a13"
CONTENT_BG = "#05080f"   # 消息流背景（比卡片再深半档，托住气泡）
CODE_BG = "#060a13"
CODE_FG = "#d2dbe8"
USER_BUBBLE = "#12334a"
USER_BUBBLE_HI = "#163c57"
AGENT_BUBBLE = "#0d1421"
AGENT_BUBBLE_HI = "#111a2b"
BUBBLE_LINE_U = "#28556b"
BUBBLE_LINE_A = "#1b2838"
BUBBLE_HI_LINE_U = "#5d8da5"
BUBBLE_HI_LINE_A = "#4a617d"

# ---------------------------------------------------------------------------
# 兼容别名：chat.py 旧常量名全部保留，指向新令牌（未迁移处自动跟随新色板）
# ---------------------------------------------------------------------------
BG = BG0
SURFACE = BG1
PANEL = BG2
PANEL_LIGHT = BG5
PANEL_HOVER = BG4
BORDER = LINE1
BORDER_ACTIVE = FOCUS
TEXT = INK1
TEXT_SOFT = INK2
TEXT_DIM = INK3
TEXT_MUTED = INK4
ACCENT = CY1
ACCENT_HOVER = CY2
ACCENT_DIM = CY_DEEP
USER_BUBBLE_BORDER = BUBBLE_LINE_U
STOP = NEG
OK = POS
VIOLET = VI1
VIOLET_DIM = VI_DEEP


# ---------------------------------------------------------------------------
# 颜色工具
# ---------------------------------------------------------------------------
def _parse_hex(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(ch * 2 for ch in color)
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _to_hex(r: int, g: int, b: int) -> str:
    clamp = lambda v: max(0, min(255, int(v)))
    return f"#{clamp(r):02x}{clamp(g):02x}{clamp(b):02x}"


def shift_color(color: str, factor: float) -> str:
    """按比例缩放颜色亮度：factor>1 提亮（向白混合），<1 压暗。"""
    r, g, b = _parse_hex(color)
    if factor >= 1.0:
        k = factor - 1.0
        return _to_hex(r + (255 - r) * k, g + (255 - g) * k, b + (255 - b) * k)
    return _to_hex(r * factor, g * factor, b * factor)


def luminance(color: str) -> float:
    r, g, b = _parse_hex(color)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def auto_fg(bg: str) -> str:
    """给填充色挑对比文字色：亮底用深墨，暗底用近白。"""
    return ON_CY if luminance(bg) > 0.45 else "#f4f7fb"


def pick_font_family(root: tk.Misc, *candidates: str) -> str:
    """选择本机存在的字体，避免把 Windows 11 字体硬编码成运行前提。"""
    try:
        available = {name.casefold(): name for name in tkfont.families(root)}
        for candidate in candidates:
            if candidate.casefold() in available:
                return available[candidate.casefold()]
    except tk.TclError:
        pass
    return candidates[-1]


UI_FAMILIES = ("Microsoft YaHei UI", "Segoe UI Variable Text", "Segoe UI")
DISPLAY_FAMILIES = ("Microsoft YaHei UI", "Segoe UI Variable Display", "Segoe UI")
MONO_FAMILIES = ("Cascadia Code", "Cascadia Mono", "Consolas", "Courier New")


def entry_opts(font, show: str = "") -> dict:
    """统一输入框样式：深控件底 + 发丝边 + 聚焦冰蓝环。"""
    opts = dict(
        bg=BG3, fg=INK1, relief="flat",
        highlightthickness=1, highlightbackground=LINE1, highlightcolor=FOCUS,
        insertbackground=CY1, selectbackground=SEL, selectforeground=INK1,
        font=font,
    )
    if show:
        opts["show"] = show
    return opts


def rounded_rect(canvas: tk.Canvas, x0: float, y0: float, x1: float, y1: float,
                 r: float, **kw) -> int:
    """在 Canvas 上画圆角矩形（平滑多边形逼近，Tk 原生无圆角矩形）。"""
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts = [
        x0 + r, y0, x1 - r, y0,
        x1, y0 + r, x1, y1 - r,
        x1 - r, y1, x0 + r, y1,
        x0, y1 - r, x0, y0 + r,
    ]
    kw.setdefault("smooth", True)
    return canvas.create_polygon(pts, **kw)


# ---------------------------------------------------------------------------
# 胶囊按钮（RoundedButton）
# ---------------------------------------------------------------------------
class RoundedButton(tk.Canvas):
    """圆角胶囊按钮：悬停/按压/禁用态齐全，可替换 tk.Button。

    兼容 chat.py 的动态用法：``config(text=, state=, command=, bg=, fg=)``。
    - 传 ``bg`` = 自定义填充色（文字对比色自动计算，也可用 ``fg`` 指定）
    - ``accent=True`` 等价 ``variant="primary"``
    """

    VARIANTS: dict[str, dict] = {
        "primary": dict(fill=CY1, hover=CY2, press="#6fdcef", text=ON_CY,
                        line="", hover_line="",
                        dis_fill="#152532", dis_text="#51707e", dis_line=""),
        "ghost": dict(fill=BG3, hover=BG4, press="#162036", text=INK2,
                      hover_text=INK1, line=LINE1, hover_line=LINE2,
                      dis_fill=BG2, dis_text=INK4, dis_line=LINE0),
        "soft": dict(fill=CY_FAINT, hover="#123146", press="#0a1c29", text=CY1,
                     line="#1d4763", hover_line="#2a6488",
                     dis_fill=BG2, dis_text=INK4, dis_line=LINE0),
        "violet": dict(fill=VI_FAINT, hover="#1e1840", press="#120f26", text=VI1,
                       line="#37306b", hover_line="#4a4190",
                       dis_fill=BG2, dis_text=INK4, dis_line=LINE0),
        "danger": dict(fill=NEG, hover="#ff8fa0", press="#e35a6e", text="#ffffff",
                       line="", hover_line="",
                       dis_fill="#2a1420", dis_text="#6c4a52", dis_line=""),
    }

    def __init__(self, parent, text: str = "", command=None, variant: str = "ghost",
                 accent: bool = False, radius: int = 9, padx: int = 16,
                 height: int = 30, font=None, bg: str | None = None,
                 fg: str | None = None, canvas_bg: str | None = None, **_ignored):
        if accent and variant == "ghost":
            variant = "primary"
        if canvas_bg is None:
            try:
                canvas_bg = str(parent.cget("bg"))
            except Exception:
                canvas_bg = BG2
        super().__init__(parent, bg=canvas_bg, highlightthickness=0, bd=0,
                         height=height, cursor="hand2")
        self._label = text
        self._command = command
        self._variant = variant if variant in self.VARIANTS else "ghost"
        self._radius = radius
        self._padx = padx
        self._height = height
        self._state = "normal"
        self._bg_override = bg
        self._fg_override = fg
        self._hover = False
        self._press = False
        spec = font or ("", 9, "bold")
        if isinstance(spec, str):
            spec = (spec, 9, "bold")
        self._font_spec = tuple(spec)
        self._font = tkfont.Font(
            root=self, family=self._font_spec[0] or pick_font_family(self, *UI_FAMILIES),
            size=self._font_spec[1] if len(self._font_spec) > 1 else 9,
            weight=self._font_spec[2] if len(self._font_spec) > 2 else "normal")
        self._resize_to_text()
        # 映射/伸缩时重绘：构造时窗口尚未布局，after_idle 首绘可能拿到 1px 尺寸，
        # 必须以 <Configure> 为准（fill="x" 拉伸也靠它）
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _on_configure(self, event) -> None:
        if event.width >= 4 and event.height >= 4:
            self._redraw()

    # ---- 外观状态 ----------------------------------------------------------
    def _palette(self) -> dict:
        if self._bg_override:
            fill = self._bg_override
            pal = dict(fill=fill, hover=shift_color(fill, 1.14), press=shift_color(fill, 0.9),
                       text=self._fg_override or auto_fg(fill),
                       hover_text=self._fg_override or auto_fg(fill),
                       line="", hover_line="",
                       dis_fill=shift_color(fill, 0.35) if luminance(fill) > .4 else BG2,
                       dis_text=INK4, dis_line="")
            return pal
        return dict(self.VARIANTS[self._variant])

    def _colors(self) -> tuple[str, str, str]:
        """当前状态下的 (填充, 边框, 文字)。"""
        pal = self._palette()
        if self._state == "disabled":
            return (str(pal.get("dis_fill", BG2)), str(pal.get("dis_line", LINE0)),
                    str(pal.get("dis_text", INK4)))
        if self._press:
            return (str(pal["press"]), str(pal.get("hover_line") or pal.get("line") or ""),
                    str(pal.get("hover_text", pal["text"])))
        if self._hover:
            return (str(pal["hover"]), str(pal.get("hover_line") or pal.get("line") or ""),
                    str(pal.get("hover_text", pal["text"])))
        return (str(pal["fill"]), str(pal.get("line") or ""), str(pal["text"]))

    def _resize_to_text(self) -> None:
        width = self._font.measure(self._label or " ") + self._padx * 2
        width = max(width, 68)
        super().configure(width=width, height=self._height)
        self.after_idle(self._redraw)

    def _redraw(self) -> None:
        try:
            w = self.winfo_width()
            h = self.winfo_height()
        except tk.TclError:
            return
        if w < 4 or h < 4:
            return
        self.delete("all")
        fill, line, text = self._colors()
        if self._state == "disabled":
            self.configure(cursor="arrow")
        else:
            self.configure(cursor="hand2")
        rounded_rect(self, 1, 1, w - 1, h - 1, self._radius, fill=fill,
                     outline=line or fill, width=1)
        self.create_text(w / 2, h / 2 + 1, text=self._label, fill=text,
                         font=self._font)

    # ---- 交互 ---------------------------------------------------------------
    def _set_hover(self, on: bool) -> None:
        if self._state == "disabled":
            return
        self._hover = on
        if not on:
            self._press = False
        self._redraw()

    def _on_press(self, _event) -> None:
        if self._state == "disabled":
            return
        self._press = True
        self._redraw()

    def _on_release(self, event) -> None:
        if self._state == "disabled":
            return
        was_press = self._press
        self._press = False
        self._redraw()
        if not was_press:
            return
        inside = (0 <= event.x <= self.winfo_width()
                  and 0 <= event.y <= self.winfo_height())
        if inside and callable(self._command):
            self._command()

    def invoke(self) -> None:
        if self._state != "disabled" and callable(self._command):
            self._command()

    # ---- tk.Button 兼容层 ----------------------------------------------------
    def configure(self, cnf=None, **kw):  # noqa: N802（保持 tkinter 命名）
        if isinstance(cnf, dict):
            kw.update(cnf)
        handled = {}
        for key in ("text", "state", "command", "bg", "fg", "background", "foreground"):
            if key in kw:
                handled[key] = kw.pop(key)
        if "text" in handled:
            self._label = str(handled["text"])
            self._resize_to_text()
        if "state" in handled:
            self._state = str(handled["state"])
        if "command" in handled:
            self._command = handled["command"]
        if "bg" in handled or "background" in handled:
            self._bg_override = handled.get("bg", handled.get("background"))
        if "fg" in handled or "foreground" in handled:
            self._fg_override = handled.get("fg", handled.get("foreground"))
        if handled:
            self._redraw()
        if kw:
            try:
                super().configure(**kw)
            except tk.TclError:
                pass
        return self

    config = configure


class Kicker(tk.Frame):
    """等宽小号题注（`SYSTEM / CONFIGURATION` 风格）+ 前置强调短线。"""

    def __init__(self, parent, text: str, *, bg: str, mono_family: str,
                 fg: str = CY_DEEP, bar: str = CY_DEEP):
        super().__init__(parent, bg=bg)
        tk.Frame(self, bg=bar, width=14, height=2).pack(
            side="left", anchor="center", padx=(0, 6))
        tk.Label(self, text=text.upper(), bg=bg, fg=fg,
                 font=(mono_family, 8, "bold")).pack(side="left")
