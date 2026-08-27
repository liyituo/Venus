"""VenusChat V1 classical-minimal design tokens.

This module is deliberately standalone.  It does not import the legacy chat
theme or any backend module, which keeps the V1 visual experiment isolated.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk


# Surfaces -----------------------------------------------------------------
WINDOW = "#EEEAE4"
HEADER = "#FCFBF8"
CANVAS = "#FBFAF7"
SIDEBAR = "#F7F4EF"
SURFACE = "#FFFFFF"
SURFACE_ALT = "#FAF8F4"
HOVER = "#F4EFE9"
ACTIVE = "#FAECE7"

# Lines --------------------------------------------------------------------
LINE_FAINT = "#EEE9E2"
LINE = "#E1DAD2"
LINE_STRONG = "#CFC3B9"

# Ink ----------------------------------------------------------------------
INK = "#292520"
INK_SOFT = "#59524C"
INK_MUTED = "#837A72"
INK_FAINT = "#AAA198"

# Accent and semantic colors -----------------------------------------------
TERRACOTTA = "#C9573D"
TERRACOTTA_HOVER = "#B84B34"
TERRACOTTA_PRESS = "#A6402D"
TERRACOTTA_SOFT = "#F8E9E4"
ON_ACCENT = "#FFFCF9"
SUCCESS = "#2F8A5B"
SUCCESS_SOFT = "#EAF4EE"
WARNING = "#B8782C"
DANGER = "#B84A43"

# Message colors -----------------------------------------------------------
USER_MESSAGE = "#F8ECE7"
AGENT_MESSAGE = "#FFFFFF"
CODE_SURFACE = "#F5F1EB"


def enable_dpi_awareness() -> None:
    """Make Tk dimensions crisp on Windows while remaining portable."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HiDPI scaling
# ---------------------------------------------------------------------------
# Tk treats negative font sizes and all padding values as *physical* pixels.
# On a 175% display that makes the UI look tiny with disproportionately large
# whitespace.  Every view therefore declares its numbers in logical pixels
# (96dpi design units) and converts them once through s().
SCALE = 1.0


def init_scale(root: tk.Misc) -> float:
    """Measure the effective display scale; call right after Tk() + awareness."""
    global SCALE
    dpi = 96
    if sys.platform == "win32":
        try:
            dpi = int(ctypes.windll.user32.GetDpiForSystem()) or 96
        except Exception:
            try:
                dpi = int(float(root.tk.call('tk', 'scaling')) * 72) or 96
            except Exception:
                dpi = 96
    else:
        try:
            dpi = int(float(root.tk.call('tk', 'scaling')) * 72) or 96
        except Exception:
            dpi = 96
    SCALE = max(1.0, round(dpi / 96.0, 4))
    return SCALE


def s(value: float) -> int:
    """Logical design px -> physical px (identity at 100% scale)."""
    return max(1, int(round(value * SCALE)))


def scale_factor() -> float:
    return SCALE


def choose_family(root: tk.Misc, *candidates: str) -> str:
    """Return the first installed family, with a deterministic fallback."""
    try:
        installed = {name.casefold(): name for name in tkfont.families(root)}
        for candidate in candidates:
            match = installed.get(candidate.casefold())
            if match:
                return match
    except tk.TclError:
        pass
    return candidates[-1]


@dataclass(slots=True)
class Fonts:
    """Named font set shared by every V1 view."""

    display_family: str
    ui_family: str
    mono_family: str
    brand: tkfont.Font
    display_xl: tkfont.Font
    display_lg: tkfont.Font
    display_md: tkfont.Font
    title: tkfont.Font
    body: tkfont.Font
    body_medium: tkfont.Font
    body_bold: tkfont.Font
    small: tkfont.Font
    small_bold: tkfont.Font
    caption: tkfont.Font
    kicker: tkfont.Font
    mono: tkfont.Font

    @classmethod
    def create(cls, root: tk.Misc) -> "Fonts":
        ui = choose_family(
            root,
            "Microsoft YaHei UI",
            "Segoe UI Variable Text",
            "Segoe UI",
            "Arial",
        )
        # Large Chinese serifs: prefer real TrueType serifs; SimSun must never
        # be used for display sizes (its bitmap strikes become visibly jagged).
        display = choose_family(
            root,
            "Source Han Serif SC",
            "Noto Serif CJK SC",
            "思源宋体",
            "FangSong",
            "仿宋",
            "KaiTi",
            "楷体",
            "Microsoft YaHei UI Light",
            "Microsoft YaHei UI",
        )
        mono = choose_family(
            root,
            "Cascadia Code",
            "Cascadia Mono",
            "Consolas",
            "Courier New",
        )
        return cls(
            display_family=display,
            ui_family=ui,
            mono_family=mono,
            # Negative Tk font sizes are physical pixels and do NOT scale with
            # DPI; every design number below is 96dpi logical px passed s().
            brand=tkfont.Font(root=root, family="Georgia", size=-s(25)),
            display_xl=tkfont.Font(root=root, family=display, size=-s(42)),
            display_lg=tkfont.Font(root=root, family=display, size=-s(29)),
            display_md=tkfont.Font(root=root, family=display, size=-s(20)),
            title=tkfont.Font(root=root, family=display, size=-s(18)),
            body=tkfont.Font(root=root, family=ui, size=-s(15)),
            body_medium=tkfont.Font(root=root, family=ui, size=-s(15), weight="bold"),
            body_bold=tkfont.Font(root=root, family=ui, size=-s(16), weight="bold"),
            small=tkfont.Font(root=root, family=ui, size=-s(14)),
            small_bold=tkfont.Font(root=root, family=ui, size=-s(14), weight="bold"),
            caption=tkfont.Font(root=root, family=ui, size=-s(13)),
            kicker=tkfont.Font(root=root, family=mono, size=-s(11)),
            mono=tkfont.Font(root=root, family=mono, size=-s(12)),
        )


def configure_ttk(root: tk.Misc, fonts: Fonts) -> None:
    """Flatten the small number of ttk controls used by the new frontend."""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(
        "Venus.Horizontal.TScale",
        background=CANVAS,
        troughcolor=LINE,
        bordercolor=CANVAS,
        lightcolor=TERRACOTTA,
        darkcolor=TERRACOTTA,
        sliderlength=s(16),
        thickness=s(14),
    )
    style.configure(
        "Venus.Vertical.TScrollbar",
        background=LINE_STRONG,
        troughcolor=CANVAS,
        bordercolor=CANVAS,
        arrowcolor=INK_MUTED,
        lightcolor=LINE_STRONG,
        darkcolor=LINE_STRONG,
        width=s(7),
    )
    style.map(
        "Venus.Vertical.TScrollbar",
        background=[("active", TERRACOTTA)],
    )
    style.configure(
        "Venus.TCombobox",
        fieldbackground=SURFACE,
        background=SURFACE,
        foreground=INK,
        arrowcolor=INK_MUTED,
        bordercolor=LINE,
        lightcolor=LINE,
        darkcolor=LINE,
        padding=(s(10), s(8)),
        font=fonts.body,
    )


def luminance(hex_color: str) -> float:
    """Approximate luminance, useful for lightweight contrast tests."""
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
