"""Reusable native Tk widgets for the independent VenusChat V1 frontend.

Anti-aliasing strategy
----------------------
Tk's Canvas draws text and shapes without any smoothing on Windows, which is
what produced the visible staircase edges in V1.  Every glyph in this module
therefore lives in a real Label (GDI + ClearType), and every curved surface
(rounded rectangles, circles, switches, badges) is rendered with Pillow at a
3× supersampled size and downsampled with LANCZOS.  Without Pillow the
widgets fall back to the classic canvas drawing so the app still runs.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from typing import Callable, Iterable

from . import theme as t

try:
    from PIL import Image, ImageDraw, ImageTk
    HAS_PIL = True
except ModuleNotFoundError:      # pragma: no cover - Pillow is optional
    Image = ImageDraw = ImageTk = None
    HAS_PIL = False

_SS = 3  # supersample factor for curved geometry


# ---------------------------------------------------------------------------
# Supersampled drawing helpers
# ---------------------------------------------------------------------------
def _canvas_size(widget: tk.Misc, fallback_w: int, fallback_h: int) -> tuple[int, int]:
    w = widget.winfo_width()
    h = widget.winfo_height()
    if w < 3 or h < 3:
        w, h = fallback_w, fallback_h
    return max(2, int(w)), max(2, int(h))


def _rounded_image(
    width: int,
    height: int,
    radius: float,
    fill: str,
    outline: str | None = None,
    line_width: int = 1,
) -> "Image.Image":
    w, h = max(2, width) * _SS, max(2, height) * _SS
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = max(0.0, min(radius, width / 2, height / 2)) * _SS
    if outline and line_width:
        inset = line_width * _SS / 2
        d.rounded_rectangle(
            [inset, inset, w - 1 - inset, h - 1 - inset],
            radius=r,
            fill=fill,
            outline=outline,
            width=max(1, int(line_width * _SS)),
        )
    else:
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=fill)
    return img.resize((max(2, width), max(2, height)), Image.LANCZOS)


def _circle_image(diameter: int, color: str, inset: int = 1) -> "Image.Image":
    d = max(3, int(diameter)) * _SS
    img = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    i = inset * _SS
    dr.ellipse([i, i, d - 1 - i, d - 1 - i], fill=color, outline=color)
    return img.resize((max(3, int(diameter)), max(3, int(diameter))), Image.LANCZOS)


def mix_color(a: str, b: str, k: float) -> str:
    """Blend two hex colors; k=0 -> a, k=1 -> b."""
    pa = tuple(int(a.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    pb = tuple(int(b.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    return "#%02X%02X%02X" % tuple(round(pa[i] + (pb[i] - pa[i]) * k) for i in range(3))


def spaced(text: str, gap: str = "\u2009") -> str:
    """Letter-spaced typesetting for kicker labels."""
    return gap.join(str(text))


def rounded_rect(
    canvas: tk.Canvas,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    radius: float,
    **kwargs,
) -> int:
    """Fallback smoothed rounded rectangle for environments without Pillow."""
    radius = max(0.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
    points = (
        x0 + radius, y0, x0 + radius, y0, x1 - radius, y0, x1 - radius, y0,
        x1, y0, x1, y0 + radius, x1, y0 + radius, x1, y1 - radius,
        x1, y1 - radius, x1, y1, x1 - radius, y1, x1 - radius, y1,
        x0 + radius, y1, x0 + radius, y1, x0, y1, x0, y1 - radius,
        x0, y1 - radius, x0, y0 + radius, x0, y0 + radius, x0, y0,
    )
    kwargs.setdefault("smooth", True)
    kwargs.setdefault("splinesteps", 18)
    return canvas.create_polygon(points, **kwargs)


def _pointer_inside(widget: tk.Misc) -> bool:
    try:
        x, y = widget.winfo_pointerx(), widget.winfo_pointery()
        left, top = widget.winfo_rootx(), widget.winfo_rooty()
        return left <= x <= left + widget.winfo_width() and top <= y <= top + widget.winfo_height()
    except tk.TclError:
        return False


def _alive(widget: tk.Misc) -> bool:
    """winfo_exists() itself raises on destroyed widgets; this never does."""
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------
class FlatButton(tk.Canvas):
    """Rounded canvas button with an anti-aliased body and a ClearType label."""

    PALETTES = {
        "ghost": (None, t.HOVER, t.SURFACE_ALT, t.INK_SOFT, "", ""),
        "outline": (t.SURFACE, t.SURFACE_ALT, t.HOVER, t.INK_SOFT, t.LINE, t.LINE_STRONG),
        "soft": (t.TERRACOTTA_SOFT, "#F5DDD6", "#EFCFC5", t.TERRACOTTA, t.LINE_FAINT, t.TERRACOTTA),
        "primary": (t.TERRACOTTA, t.TERRACOTTA_HOVER, t.TERRACOTTA_PRESS, t.ON_ACCENT, "", ""),
        "danger": (t.DANGER, "#A9413B", "#933832", t.ON_ACCENT, "", ""),
    }

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], object] | None = None,
        *,
        font: tkfont.Font | tuple | None = None,
        variant: str = "ghost",
        height: int = 34,
        padx: int = 15,
        radius: int = 8,
        parent_bg: str | None = None,
        min_width: int = 0,
        anchor: str = "center",
    ) -> None:
        self.parent_bg = parent_bg or str(parent.cget("bg"))
        self.font = font if isinstance(font, tkfont.Font) else tkfont.Font(font=font)
        self.min_width = t.s(min_width)
        self.padx = t.s(padx)
        self.radius = t.s(radius)
        self.height_px = t.s(height)
        width = max(self.min_width, self.font.measure(text) + self.padx * 2 + 2)
        super().__init__(
            parent,
            width=width,
            height=self.height_px,
            bg=self.parent_bg,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            takefocus=1,
        )
        self.text = text
        self.command = command
        self.variant = variant if variant in self.PALETTES else "ghost"
        self.anchor = anchor
        self.enabled = True
        self.hovered = False
        self.pressed = False
        self.active = False
        self._photo: "ImageTk.PhotoImage | None" = None
        self._settle_job: str | None = None
        self._label = tk.Label(self, text=text, bg=self.parent_bg, font=self.font, cursor="hand2")
        self.bind("<Configure>", self._render, add="+")
        self._label.bind("<Enter>", self._enter, add="+")
        self._label.bind("<Leave>", self._leave, add="+")
        self._label.bind("<ButtonPress-1>", self._press, add="+")
        self._label.bind("<ButtonRelease-1>", self._release, add="+")
        self.bind("<Enter>", self._enter, add="+")
        self.bind("<Leave>", self._leave, add="+")
        self.bind("<ButtonPress-1>", self._press, add="+")
        self.bind("<ButtonRelease-1>", self._release, add="+")
        self.bind("<space>", lambda _event: self.invoke(), add="+")
        self.bind("<Return>", lambda _event: self.invoke(), add="+")
        self._render()

    def _palette(self) -> tuple[str, str, str]:
        base, hover, pressed, ink, line, hover_line = self.PALETTES[self.variant]
        base = self.parent_bg if base is None else base
        if not self.enabled:
            return t.SURFACE_ALT, t.INK_FAINT, t.LINE_FAINT
        if self.pressed:
            return pressed, ink, hover_line or line or pressed
        if self.hovered or self.active:
            if self.active and self.variant == "ghost":
                return t.TERRACOTTA_SOFT, t.TERRACOTTA, t.TERRACOTTA_SOFT
            return hover, ink, hover_line or line or hover
        return base, ink, line or base

    def _enter(self, _event=None) -> None:
        if self._settle_job:
            try:
                self.after_cancel(self._settle_job)
            except tk.TclError:
                pass
            self._settle_job = None
        if self.enabled:
            self.hovered = True
            self._render()

    def _leave(self, _event=None) -> None:
        self._settle_job = self.after(24, self._settle)

    def _settle(self) -> None:
        self._settle_job = None
        if not _pointer_inside(self):
            self.hovered = False
            self.pressed = False
            self._render()

    def _press(self, _event=None) -> None:
        if self.enabled:
            self.pressed = True
            self._render()

    def _release(self, _event=None) -> None:
        was_pressed = self.pressed
        self.pressed = False
        self._render()
        if was_pressed and self.enabled and _pointer_inside(self):
            self.invoke()

    def _render(self, _event=None) -> None:
        if not _alive(self):
            return
        w, h = _canvas_size(self, int(self.cget("width")), self.height_px)
        fill, ink, line = self._palette()
        self.delete("all")
        if HAS_PIL:
            self._photo = ImageTk.PhotoImage(
                _rounded_image(w, h, self.radius, fill, line or None, 1 if line else 0))
            self.create_image(0, 0, image=self._photo, anchor="nw")
        else:
            rounded_rect(self, 1, 1, w - 1, h - 1, self.radius,
                         fill=fill, outline=line or fill, width=1)
            self._label.place_forget()
            self.create_text(
                self.padx if self.anchor == "w" else w / 2, h / 2 + 1,
                text=self.text, fill=ink, font=self.font,
                anchor="w" if self.anchor == "w" else "center")
            return
        self._label.configure(text=self.text, fg=ink, bg=fill,
                              cursor="hand2" if self.enabled else "arrow")
        if self.anchor == "w":
            self._label.place(x=self.padx, y=h / 2, anchor="w")
        else:
            self._label.place(x=w / 2, y=h / 2 + 1, anchor="center")

    def invoke(self) -> None:
        if self.enabled and callable(self.command):
            self.command()

    def set_text(self, text: str) -> None:
        self.text = text
        super().configure(width=max(self.min_width, self.font.measure(text) + self.padx * 2 + 2))
        self._render()

    def set_active(self, active: bool) -> None:
        self.active = bool(active)
        self._render()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._render()


class RoundButton(tk.Canvas):
    """Circular primary action; the disc is supersampled, the glyph is a Label."""

    def __init__(
        self,
        parent: tk.Misc,
        glyph: str,
        command: Callable[[], object],
        *,
        font: tkfont.Font | tuple,
        size: int = 48,
        parent_bg: str | None = None,
    ) -> None:
        self.parent_bg = parent_bg or str(parent.cget("bg"))
        self.glyph = glyph
        self.command = command
        self.font = font
        self.size_px = t.s(size)
        super().__init__(
            parent,
            width=self.size_px,
            height=self.size_px,
            bg=self.parent_bg,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            takefocus=1,
        )
        self.hovered = False
        self.pressed = False
        self._photo: "ImageTk.PhotoImage | None" = None
        self._label = tk.Label(self, text=glyph, font=self.font, fg=t.ON_ACCENT,
                               bg=t.TERRACOTTA, cursor="hand2")
        self.bind("<Configure>", self._draw, add="+")
        for widget in (self, self._label):
            widget.bind("<Enter>", lambda _e: self._set_state(True, self.pressed), add="+")
            widget.bind("<Leave>", self._leave, add="+")
            widget.bind("<ButtonPress-1>", lambda _e: self._set_state(True, True), add="+")
            widget.bind("<ButtonRelease-1>", self._release, add="+")
        self.bind("<Return>", lambda _e: self.command(), add="+")
        self._draw()

    def _set_state(self, hover: bool, press: bool) -> None:
        self.hovered, self.pressed = hover, press
        self._draw()

    def _leave(self, _event=None) -> None:
        self.after(24, self._settle)

    def _settle(self) -> None:
        if not _pointer_inside(self):
            self._set_state(False, False)

    def _release(self, _event=None) -> None:
        was_pressed = self.pressed
        self._set_state(self.hovered, False)
        if was_pressed and _pointer_inside(self):
            self.command()

    def _draw(self, _event=None) -> None:
        if not _alive(self):
            return
        size = min(_canvas_size(self, self.size_px, self.size_px))
        fill = t.TERRACOTTA_PRESS if self.pressed else (
            t.TERRACOTTA_HOVER if self.hovered else t.TERRACOTTA)
        self.delete("all")
        if HAS_PIL:
            self._photo = ImageTk.PhotoImage(_circle_image(size, fill, inset=2))
            self.create_image(size / 2, size / 2, image=self._photo, anchor="center")
        else:
            self.create_oval(2, 2, size - 2, size - 2, fill=fill, outline=fill)
            self._label.place_forget()
            self.create_text(size / 2, size / 2 - 1, text=self.glyph,
                             fill=t.ON_ACCENT, font=self.font)
            return
        self._label.configure(bg=fill)
        self._label.place(x=size / 2, y=size / 2 - 1, anchor="center")


class Dot(tk.Canvas):
    """Anti-aliased status dot (replaces jagged "●" glyphs)."""

    def __init__(self, parent: tk.Misc, *, color: str = t.SUCCESS, size: int = 7,
                 bg: str | None = None) -> None:
        self.parent_bg = bg or str(parent.cget("bg"))
        self.size_px = t.s(size)
        super().__init__(parent, width=self.size_px, height=self.size_px,
                         bg=self.parent_bg, bd=0, highlightthickness=0)
        self.color = color
        self._photo: "ImageTk.PhotoImage | None" = None
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        if HAS_PIL:
            self._photo = ImageTk.PhotoImage(_circle_image(self.size_px, self.color))
            self.create_image(0, 0, image=self._photo, anchor="nw")
        else:
            self.create_oval(1, 1, self.size_px - 1, self.size_px - 1,
                             fill=self.color, outline=self.color)

    def set_color(self, color: str) -> None:
        self.color = color
        self._draw()


class SearchGlyph(tk.Canvas):
    """Small supersampled magnifier drawn from primitives (no font glyph)."""

    def __init__(self, parent: tk.Misc, *, size: int = 16, color: str = t.INK_MUTED,
                 bg: str | None = None) -> None:
        self.parent_bg = bg or str(parent.cget("bg"))
        self.size_px = t.s(size)
        super().__init__(parent, width=self.size_px, height=self.size_px,
                         bg=self.parent_bg, bd=0, highlightthickness=0, cursor="xterm")
        self.color = color
        self._photo: "ImageTk.PhotoImage | None" = None
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        s = self.size_px
        if HAS_PIL:
            img = Image.new("RGBA", (s * _SS, s * _SS), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            lw = max(2, int(_SS * 1.2))
            r = int(s * 0.36) * _SS
            cx, cy = int(s * 0.42) * _SS, int(s * 0.42) * _SS
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=self.color, width=lw)
            k = int(r * 0.7071)
            d.line([cx + k, cy + k, int(s * 0.86) * _SS, int(s * 0.86) * _SS],
                   fill=self.color, width=lw)
            self._photo = ImageTk.PhotoImage(img.resize((s, s), Image.LANCZOS))
            self.create_image(0, 0, image=self._photo, anchor="nw")
        else:
            r = s * 0.36
            c = s * 0.42
            self.create_oval(c - r, c - r, c + r, c + r, outline=self.color)
            self.create_line(c + r * .7, c + r * .7, s * .85, s * .85, fill=self.color)


class PeopleBadge(tk.Canvas):
    """Two-collaborator emblem with supersampled outlines."""

    def __init__(self, parent: tk.Misc, *, size: int = 42,
                 bg: str = t.SURFACE_ALT, cursor: str = "hand2") -> None:
        self.size_px = t.s(size)
        super().__init__(parent, width=self.size_px, height=self.size_px, bg=bg,
                         bd=0, highlightthickness=0, cursor=cursor)
        self._photo: "ImageTk.PhotoImage | None" = None
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        size = self.size_px
        if not HAS_PIL:
            k = size / 42.0
            self.create_oval(9 * k, 8 * k, 22 * k, 21 * k, outline=t.INK_SOFT, width=1)
            self.create_arc(4 * k, 17 * k, 27 * k, 37 * k, start=0, extent=180,
                            outline=t.INK_SOFT, width=1, style="arc")
            self.create_oval(24 * k, 13 * k, 34 * k, 23 * k, outline=t.INK_MUTED, width=1)
            self.create_arc(20 * k, 21 * k, 39 * k, 37 * k, start=0, extent=180,
                            outline=t.INK_MUTED, width=1, style="arc")
            return
        ss = _SS
        img = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        k = size * ss / 42.0
        lw = max(2, int(k))
        d.ellipse([9 * k, 8 * k, 22 * k, 21 * k], outline=t.INK_SOFT, width=lw)
        d.arc([4 * k, 17 * k, 27 * k, 37 * k], 180, 360, fill=t.INK_SOFT, width=lw)
        d.ellipse([24 * k, 13 * k, 34 * k, 23 * k], outline=t.INK_MUTED, width=lw)
        d.arc([20 * k, 21 * k, 39 * k, 37 * k], 180, 360, fill=t.INK_MUTED, width=lw)
        self._photo = ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))
        self.create_image(0, 0, image=self._photo, anchor="nw")


# ---------------------------------------------------------------------------
# Surfaces and fields
# ---------------------------------------------------------------------------
class HoverSurface(tk.Frame):
    """Flat surface whose hairline becomes visible only while hovered."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        bg: str = t.SURFACE,
        resting_line: str = t.LINE_FAINT,
        hover_line: str = t.LINE_STRONG,
        active_line: str = t.TERRACOTTA,
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            bg=bg,
            highlightthickness=1,
            highlightbackground=resting_line,
            highlightcolor=active_line,
            bd=0,
            **kwargs,
        )
        self.resting_line = resting_line
        self.hover_line = hover_line
        self.active_line = active_line
        self.active = False
        self._hover_job: str | None = None
        self.watch(self)

    def watch(self, *widgets: tk.Widget) -> None:
        for widget in widgets:
            widget.bind("<Enter>", self._on_enter, add="+")
            widget.bind("<Leave>", self._on_leave, add="+")

    def _on_enter(self, _event=None) -> None:
        if self._hover_job:
            try:
                self.after_cancel(self._hover_job)
            except tk.TclError:
                pass
            self._hover_job = None
        self.configure(highlightbackground=self.active_line if self.active else self.hover_line)

    def _on_leave(self, _event=None) -> None:
        self._hover_job = self.after(20, self._settle_hover)

    def _settle_hover(self) -> None:
        self._hover_job = None
        if not _pointer_inside(self):
            try:
                self.configure(
                    highlightbackground=self.active_line if self.active else self.resting_line)
            except tk.TclError:
                pass

    def set_active(self, active: bool) -> None:
        self.active = bool(active)
        self.configure(highlightbackground=self.active_line if active else self.resting_line)


class MinimalField(HoverSurface):
    """Border-light text field with placeholder and optional secret reveal."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        font: tkfont.Font,
        value: str = "",
        placeholder: str = "",
        show: str = "",
        width: int = 28,
        bg: str = t.SURFACE,
    ) -> None:
        super().__init__(parent, bg=bg, resting_line=t.LINE, hover_line=t.LINE_STRONG)
        self.variable = tk.StringVar(value=value)
        self.placeholder_text = placeholder
        self.secret_char = show
        self.revealed = not bool(show)
        self.entry = tk.Entry(
            self,
            textvariable=self.variable,
            font=font,
            width=width,
            bg=bg,
            fg=t.INK,
            insertbackground=t.INK,
            selectbackground=t.TERRACOTTA_SOFT,
            selectforeground=t.INK,
            relief="flat",
            bd=0,
            highlightthickness=0,
            show=show,
        )
        self._pad_x = t.s(12)
        self._pad_y = t.s(9)
        self.entry.pack(side="left", fill="x", expand=True,
                        padx=(self._pad_x, t.s(8)), pady=self._pad_y)
        self.hint = tk.Label(
            self,
            text=placeholder,
            bg=bg,
            fg=t.INK_FAINT,
            font=font,
            cursor="xterm",
        )
        if show:
            self.reveal = tk.Label(
                self,
                text="○",
                bg=bg,
                fg=t.INK_MUTED,
                font=font,
                cursor="hand2",
                width=2,
            )
            self.reveal.pack(side="right", padx=(0, t.s(7)))
            self.reveal.bind("<Button-1>", self._toggle_secret, add="+")
            self.watch(self.reveal)
        self.variable.trace_add("write", self._update_hint)
        self.entry.bind("<FocusIn>", self._focus_in, add="+")
        self.entry.bind("<FocusOut>", self._focus_out, add="+")
        self.hint.bind("<Button-1>", lambda _event: self.entry.focus_set(), add="+")
        self.watch(self.entry, self.hint)
        self.after_idle(self._update_hint)

    def _focus_in(self, _event=None) -> None:
        self.configure(highlightbackground=t.TERRACOTTA)
        self._update_hint()

    def _focus_out(self, _event=None) -> None:
        self.configure(highlightbackground=self.resting_line)
        self._update_hint()

    def _update_hint(self, *_args) -> None:
        try:
            if self.variable.get() or self.focus_get() is self.entry:
                self.hint.place_forget()
            else:
                self.hint.place(x=self._pad_x, rely=.5, anchor="w")
        except tk.TclError:
            pass

    def _toggle_secret(self, _event=None) -> None:
        self.revealed = not self.revealed
        self.entry.configure(show="" if self.revealed else self.secret_char)
        self.reveal.configure(text="◉" if self.revealed else "○")

    def get(self) -> str:
        return self.variable.get()

    def set(self, value: str) -> None:
        self.variable.set(value)


class SelectField(HoverSurface):
    """Minimal menu field backed by the classical MenuPopup."""

    def __init__(
        self,
        parent: tk.Misc,
        values: Iterable[str],
        *,
        font: tkfont.Font,
        value: str | None = None,
        bg: str = t.SURFACE,
    ) -> None:
        super().__init__(parent, bg=bg, resting_line=t.LINE, hover_line=t.LINE_STRONG)
        self.values = list(values)
        self._font = font
        self.variable = tk.StringVar(value=value or (self.values[0] if self.values else ""))
        self.label = tk.Label(
            self,
            textvariable=self.variable,
            bg=bg,
            fg=t.INK,
            font=font,
            anchor="w",
            cursor="hand2",
        )
        self.label.pack(side="left", fill="x", expand=True,
                        padx=(t.s(12), t.s(4)), pady=t.s(9))
        self.arrow = tk.Label(
            self,
            text="▾",
            bg=bg,
            fg=t.INK_MUTED,
            font=font,
            cursor="hand2",
        )
        self.arrow.pack(side="right", padx=(t.s(4), t.s(11)))
        for widget in (self, self.label, self.arrow):
            widget.bind("<Button-1>", self._open_menu, add="+")
        self.watch(self.label, self.arrow)

    def _open_menu(self, _event=None) -> None:
        from types import SimpleNamespace
        if not self.values:
            return
        self.configure(highlightbackground=t.TERRACOTTA)
        fonts = SimpleNamespace(small=self._font, kicker=self._font)
        items = [{"label": v, "current": v == self.variable.get()}
                 for v in self.values]

        def choose(index: int) -> None:
            self.variable.set(self.values[index])
            self.configure(highlightbackground=self.resting_line)

        MenuPopup(self, items, fonts, choose, min_width=170)

    def get(self) -> str:
        return self.variable.get()


class SearchField(MinimalField):
    """MinimalField led by an anti-aliased magnifier glyph."""

    def __init__(self, parent: tk.Misc, *, font: tkfont.Font, placeholder: str) -> None:
        super().__init__(parent, font=font, placeholder=placeholder, bg=t.SURFACE)
        self.entry.pack_forget()
        self.icon = SearchGlyph(self, size=15, bg=t.SURFACE)
        self.icon.pack(side="left", padx=(t.s(11), t.s(3)), pady=self._pad_y)
        self.entry.pack(side="left", fill="x", expand=True,
                        padx=(t.s(2), t.s(8)), pady=self._pad_y)
        self.watch(self.icon)
        self._hint_x = t.s(33)
        self.hint.place(x=self._hint_x, rely=.5, anchor="w")

    def _update_hint(self, *_args) -> None:
        try:
            if self.variable.get() or self.focus_get() is self.entry:
                self.hint.place_forget()
            else:
                self.hint.place(x=self._hint_x, rely=.5, anchor="w")
        except tk.TclError:
            pass


class Switch(tk.Canvas):
    """Anti-aliased, lightly animated switch."""

    # 逻辑像素基准；物理尺寸在 __init__ 时按当前 SCALE 计算。
    W, H = 42, 23

    def __init__(
        self,
        parent: tk.Misc,
        *,
        value: bool = False,
        command: Callable[[bool], object] | None = None,
        bg: str | None = None,
    ) -> None:
        self.parent_bg = bg or str(parent.cget("bg"))
        W, H = t.s(42), t.s(23)
        self.W, self.H = W, H
        super().__init__(
            parent,
            width=W,
            height=self.H,
            bg=self.parent_bg,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            takefocus=1,
        )
        self.value = bool(value)
        self.command = command
        self._pos = 1.0 if value else 0.0
        self._job: str | None = None
        self._photo: "ImageTk.PhotoImage | None" = None
        self.bind("<Button-1>", lambda _event: self.toggle(), add="+")
        self.bind("<space>", lambda _event: self.toggle(), add="+")
        self._draw()

    def toggle(self) -> None:
        self.value = not self.value
        self._animate()
        if callable(self.command):
            self.command(self.value)

    def set(self, value: bool) -> None:
        self.value = bool(value)
        self._animate()

    def _animate(self) -> None:
        if not HAS_PIL:
            self._pos = 1.0 if self.value else 0.0
            self._draw()
            return
        if self._job:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
        self._step()

    def _step(self) -> None:
        self._job = None
        if not _alive(self):
            return
        target = 1.0 if self.value else 0.0
        delta = target - self._pos
        if abs(delta) < 0.06:
            self._pos = target
            self._draw()
            return
        self._pos += delta * 0.34
        self._draw()
        try:
            self._job = self.after(16, self._step)
        except tk.TclError:
            pass

    def _draw(self) -> None:
        self.delete("all")
        u = t.SCALE  # one logical px -> physical px
        if not HAS_PIL:
            fill = t.TERRACOTTA if self.value else t.LINE_STRONG
            rounded_rect(self, 1, 2 * u, self.W - 1, self.H - 2 * u,
                         10 * u, fill=fill, outline=fill)
            x = (11 + 19 * self._pos) * u
            self.create_oval(x - 7 * u, 5 * u, x + 7 * u, 19 * u,
                             fill=t.SURFACE, outline=t.SURFACE)
            return
        img = Image.new("RGBA", (self.W * _SS, self.H * _SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        track = mix_color(t.LINE_STRONG, t.TERRACOTTA, self._pos)
        sc = _SS * u
        d.rounded_rectangle([sc, 2 * sc, (self.W - 1) * _SS, (self.H - 1) * _SS],
                            radius=10 * sc, fill=track)
        x = (11 + 19 * self._pos) * sc
        d.ellipse([x - 7 * sc, 5 * sc, x + 7 * sc, 19 * sc], fill="#FFFFFF")
        self._photo = ImageTk.PhotoImage(img.resize((self.W, self.H), Image.LANCZOS))
        self.create_image(0, 0, image=self._photo, anchor="nw")


class SegmentedControl(tk.Frame):
    """Flat segmented choice with a thin active terracotta border."""

    def __init__(
        self,
        parent: tk.Misc,
        values: Iterable[str],
        *,
        font: tkfont.Font,
        value: str | None = None,
        command: Callable[[str], object] | None = None,
        bg: str = t.CANVAS,
    ) -> None:
        super().__init__(parent, bg=t.SURFACE, highlightthickness=1, highlightbackground=t.LINE)
        self.values = list(values)
        self.value = value or (self.values[0] if self.values else "")
        self.command = command
        self.labels: dict[str, tk.Label] = {}
        for index, item in enumerate(self.values):
            if index:
                tk.Frame(self, bg=t.LINE_FAINT, width=1).pack(side="left", fill="y")
            label = tk.Label(
                self,
                text=item,
                bg=t.SURFACE,
                fg=t.INK_SOFT,
                font=font,
                cursor="hand2",
                padx=t.s(26),
                pady=t.s(8),
            )
            label.pack(side="left", fill="both", expand=True)
            label.bind("<Button-1>", lambda _event, selected=item: self.set(selected), add="+")
            label.bind("<Enter>", lambda _event, selected=item: self._hover(selected, True), add="+")
            label.bind("<Leave>", lambda _event, selected=item: self._hover(selected, False), add="+")
            self.labels[item] = label
        self._render()

    def _hover(self, item: str, entered: bool) -> None:
        if item != self.value:
            self.labels[item].configure(bg=t.HOVER if entered else t.SURFACE)

    def _render(self) -> None:
        for item, label in self.labels.items():
            active = item == self.value
            label.configure(
                bg=t.TERRACOTTA_SOFT if active else t.SURFACE,
                fg=t.TERRACOTTA if active else t.INK_SOFT,
            )

    def set(self, value: str) -> None:
        if value not in self.labels:
            return
        self.value = value
        self._render()
        if callable(self.command):
            self.command(value)

    def get(self) -> str:
        return self.value


class NavRow(tk.Frame):
    """Reusable settings/sidebar navigation row (icon is optional)."""

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], object],
        *,
        font: tkfont.Font,
        icon: str = "",
        height: int = 38,
        bg: str = t.SIDEBAR,
    ) -> None:
        super().__init__(parent, bg=bg, height=t.s(height), cursor="hand2")
        self.pack_propagate(False)
        self.base_bg = bg
        self.command = command
        self.active = False
        self.marker = tk.Frame(self, bg=bg, width=2)
        self.marker.pack(side="left", fill="y")
        self.icon = None
        if icon:
            self.icon = tk.Label(
                self, text=icon, bg=bg, fg=t.INK_MUTED, font=font,
                width=3, cursor="hand2",
            )
            self.icon.pack(side="left", padx=(t.s(7), t.s(1)))
        self.label = tk.Label(
            self,
            text=text,
            bg=bg,
            fg=t.INK_SOFT,
            font=font,
            anchor="w",
            cursor="hand2",
        )
        self.label.pack(side="left", fill="both", expand=True,
                        padx=(t.s(10) if self.icon is None else t.s(2), t.s(10)))
        children = [self, self.marker, self.label] + ([self.icon] if self.icon else [])
        for widget in children:
            widget.bind("<Button-1>", lambda _event: self.command(), add="+")
            widget.bind("<Enter>", lambda _event: self._hover(True), add="+")
            widget.bind("<Leave>", lambda _event: self.after(16, self._settle), add="+")

    def _settle(self) -> None:
        try:
            if not _pointer_inside(self):
                self._hover(False)
        except tk.TclError:
            pass

    def _hover(self, entered: bool) -> None:
        bg = t.ACTIVE if self.active else (t.HOVER if entered else self.base_bg)
        ink = t.TERRACOTTA if self.active else t.INK_SOFT
        widgets = [self, self.label] + ([self.icon] if self.icon else [])
        for widget in widgets:
            widget.configure(bg=bg)
        self.marker.configure(bg=t.TERRACOTTA if self.active else bg)
        if self.icon:
            self.icon.configure(fg=t.TERRACOTTA if self.active else t.INK_MUTED)
        self.label.configure(fg=ink)

    def set_active(self, active: bool) -> None:
        self.active = bool(active)
        self._hover(False)


def kicker(
    parent: tk.Misc,
    text: str,
    *,
    font: tkfont.Font,
    bg: str,
    fg: str = t.INK_MUTED,
    bar: str = t.TERRACOTTA,
    spacing: str = "\u2009",
) -> tk.Frame:
    """Classical section mark: short terracotta rule + spaced small caps."""
    row = tk.Frame(parent, bg=bg)
    tk.Frame(row, bg=bar, width=t.s(14), height=2).pack(side="left", padx=(0, t.s(9)))
    tk.Label(row, text=spaced(text, spacing), bg=bg, fg=fg, font=font).pack(side="left")
    return row


class ScrollArea(tk.Frame):
    """Canvas-backed scrolling frame with a scrollbar that appears on hover."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        bg: str,
        scrollbar: bool = True,
    ) -> None:
        super().__init__(parent, bg=bg)
        self.bg_color = bg
        self.canvas = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.scrollbar = None
        if scrollbar:
            from tkinter import ttk

            self.scrollbar = ttk.Scrollbar(
                self,
                orient="vertical",
                command=self.canvas.yview,
                style="Venus.Vertical.TScrollbar",
            )
            self.canvas.configure(yscrollcommand=self._on_yview)
        self.inner.bind("<Configure>", self._sync_region, add="+")
        self.canvas.bind("<Configure>", self._sync_width, add="+")
        self.canvas.bind("<Enter>", self._enter, add="+")
        self.canvas.bind("<Leave>", self._leave, add="+")
        self.inner.bind("<Enter>", self._enter, add="+")
        self.inner.bind("<Leave>", self._leave, add="+")
        # Permanent global wheel hook, pointer-guarded.  (The old code called
        # unbind_class() with an illegal fourth argument on every leave, which
        # spammed tracebacks; a guarded handler never needs to be removed.)
        self.canvas.bind_all("<MouseWheel>", self._wheel, add="+")

    def _sync_region(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_width(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.window, width=max(1, event.width))

    def _on_yview(self, first: str, last: str) -> None:
        if self.scrollbar is not None:
            self.scrollbar.set(first, last)
            try:
                if float(first) <= 0.001 and float(last) >= .999:
                    self.scrollbar.pack_forget()   # 仅当完全无溢出
            except (TypeError, ValueError):
                pass

    def _enter(self, _event=None) -> None:
        if self.scrollbar is not None:
            first, last = self.canvas.yview()
            if not (first <= 0.001 and last >= .999):
                self.scrollbar.pack(side="right", fill="y")

    def _leave(self, _event=None) -> None:
        self.after(25, self._settle_leave)

    def _settle_leave(self) -> None:
        try:
            if not _pointer_inside(self) and self.scrollbar is not None:
                self.scrollbar.pack_forget()
        except tk.TclError:
            pass

    def _wheel(self, event: tk.Event):
        if _pointer_inside(self.canvas) or _pointer_inside(self.inner):
            first, last = self.canvas.yview()
            if first <= 0.0 and last >= 1.0:
                return None      # 内容未超过视口：完全不滚
            if event.delta:
                self.canvas.yview_scroll(-int(event.delta / 120) * 3, "units")
            return "break"
        return None

    def scroll_top(self) -> None:
        self.canvas.yview_moveto(0)


def separator(parent: tk.Misc, *, vertical: bool = False, color: str = t.LINE_FAINT) -> tk.Frame:
    """Create a one-pixel layout separator."""
    return tk.Frame(parent, bg=color, width=1 if vertical else 0, height=0 if vertical else 1)


# ---------------------------------------------------------------------------
# Execution surfaces (jobs / tools / todos / approval)
# ---------------------------------------------------------------------------
class ProgressBar(tk.Canvas):
    """Supersampled progress track; determinate value or indeterminate sweep."""

    def __init__(self, parent: tk.Misc, *, width: int = 140, height: int = 5,
                 trough: str = t.LINE_FAINT, fill: str = t.TERRACOTTA,
                 bg: str | None = None) -> None:
        self.parent_bg = bg or str(parent.cget("bg"))
        self.w_px = t.s(width)
        self.h_px = max(3, t.s(height))
        super().__init__(parent, width=self.w_px, height=self.h_px, bg=self.parent_bg,
                         bd=0, highlightthickness=0)
        self.trough = trough
        self.fill = fill
        self._value: float | None = None
        self._indeterminate = False
        self._pos = 0.0
        self._job: str | None = None
        self._photo: "ImageTk.PhotoImage | None" = None
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._draw()

    def _on_destroy(self, event) -> None:
        if event.widget is self:
            self._stop_anim()

    def _stop_anim(self) -> None:
        self._indeterminate = False
        if self._job:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def set_value(self, value: float | None) -> None:
        """0..1 determinate; None shows an empty track."""
        self._stop_anim()
        self._value = None if value is None else max(0.0, min(1.0, value))
        self._draw()

    def start_indeterminate(self) -> None:
        if self._indeterminate:
            return
        self._stop_anim()
        self._indeterminate = True
        self._value = None
        self._step()

    def _step(self) -> None:
        self._job = None
        if not _alive(self) or not self._indeterminate:
            return
        self._pos = (self._pos + 0.035) % 1.4 - 0.2
        self._draw()
        self._job = self.after(40, self._step)

    def _draw(self) -> None:
        self.delete("all")
        if not HAS_PIL:
            self.create_rectangle(0, 0, self.w_px, self.h_px, fill=self.trough, outline=self.trough)
            return
        img = Image.new("RGBA", (self.w_px * _SS, self.h_px * _SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        r = self.h_px * _SS / 2
        d.rounded_rectangle([0, 0, self.w_px * _SS - 1, self.h_px * _SS - 1],
                            radius=r, fill=self.trough)
        if self._indeterminate:
            bw = self.w_px * 0.28
            x0 = self._pos * self.w_px
            x1 = x0 + bw
            d.rounded_rectangle([max(0, x0) * _SS, 0, min(self.w_px, x1) * _SS - 1,
                                 self.h_px * _SS - 1], radius=r, fill=self.fill)
        elif self._value:
            d.rounded_rectangle([0, 0, max(2, self._value * self.w_px) * _SS - 1,
                                 self.h_px * _SS - 1], radius=r, fill=self.fill)
        self._photo = ImageTk.PhotoImage(img.resize((self.w_px, self.h_px), Image.LANCZOS))
        self.create_image(0, 0, image=self._photo, anchor="nw")


class TodoIcon(tk.Canvas):
    """PIL-rendered todo state: pending ring / in-progress core / check."""

    def __init__(self, parent: tk.Misc, *, status: str = "pending",
                 size: int = 15, bg: str = t.SIDEBAR) -> None:
        super().__init__(parent, width=t.s(size), height=t.s(size), bg=bg,
                         bd=0, highlightthickness=0)
        self.status = status
        self.size_px = t.s(size)
        self._photo: "ImageTk.PhotoImage | None" = None
        self._draw()

    def set_status(self, status: str) -> None:
        self.status = status
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        s = self.size_px
        if not HAS_PIL:
            if self.status == "done":
                self.create_text(s / 2, s / 2, text="✓", fill=t.SUCCESS)
            elif self.status == "in_progress":
                self.create_oval(2, 2, s - 2, s - 2, outline=t.TERRACOTTA, fill=t.TERRACOTTA_SOFT)
            else:
                self.create_oval(2, 2, s - 2, s - 2, outline=t.INK_FAINT)
            return
        img = Image.new("RGBA", (s * _SS, s * _SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        lw = max(2, int(_SS * 1.4))
        if self.status == "done":
            d.line([(s * .20 * _SS, s * .55 * _SS), (s * .42 * _SS, s * .76 * _SS),
                    (s * .82 * _SS, s * .28 * _SS)], fill=t.SUCCESS, width=lw + 1, joint="curve")
        elif self.status == "in_progress":
            d.ellipse([lw, lw, s * _SS - lw, s * _SS - lw], outline=t.TERRACOTTA, width=lw)
            k = s * .22 * _SS
            c = s * _SS / 2
            d.ellipse([c - k, c - k, c + k, c + k], fill=t.TERRACOTTA)
        else:
            d.ellipse([lw, lw, s * _SS - lw, s * _SS - lw], outline=t.INK_FAINT, width=lw)
        self._photo = ImageTk.PhotoImage(img.resize((s, s), Image.LANCZOS))
        self.create_image(0, 0, image=self._photo, anchor="nw")


class ToolCard(tk.Frame):
    """Collapsed tool-call card embedded in an agent message."""

    def __init__(self, parent: tk.Misc, *, name: str, args_text: str,
                 step: int = 0, max_steps: int = 0, fonts: "t.Fonts",
                 bg: str = t.SURFACE) -> None:
        super().__init__(parent, bg=bg, highlightthickness=1, highlightbackground=t.LINE)
        self.fonts = fonts
        self._open = False
        self._result_text = ""
        self._args_text = args_text
        head = tk.Frame(self, bg=bg, cursor="hand2")
        head.pack(fill="x", padx=t.s(10), pady=t.s(6))
        self.dot = Dot(head, color=t.TERRACOTTA, size=6, bg=bg)
        self.dot.pack(side="left", padx=(0, t.s(8)))
        self.name_label = tk.Label(head, text=name, bg=bg, fg=t.INK, font=fonts.mono)
        self.name_label.pack(side="left")
        if step and max_steps:
            tk.Label(head, text=f"{step}/{max_steps}", bg=bg, fg=t.INK_FAINT,
                     font=fonts.kicker).pack(side="left", padx=t.s(7))
        self.chev = tk.Label(head, text="▸", bg=bg, fg=t.INK_FAINT, font=fonts.small)
        self.chev.pack(side="right")
        self.status = tk.Label(head, text="执行中", bg=bg, fg=t.TERRACOTTA, font=fonts.kicker)
        self.status.pack(side="right", padx=t.s(8))
        self.args = tk.Label(head, text=args_text, bg=bg, fg=t.INK_MUTED, font=fonts.mono)
        self.args.pack(side="left", fill="x", expand=True, padx=(t.s(9), t.s(9)))
        self.body = tk.Text(
            self, bg=t.CODE_SURFACE, fg=t.INK_SOFT, font=fonts.mono, relief="flat",
            bd=0, highlightthickness=0, wrap="word", height=1, padx=t.s(10),
            pady=t.s(7), cursor="arrow", takefocus=0, state="disabled")
        for w in (self, head, self.name_label, self.args, self.status, self.chev):
            w.bind("<Button-1>", self._toggle, add="+")

    def _toggle(self, _event=None) -> None:
        self._open = not self._open
        self.chev.configure(text="▾" if self._open else "▸")
        if self._open:
            self._render_body()
            self.body.pack(fill="x")
        else:
            self.body.pack_forget()

    def _render_body(self) -> None:
        text = "参数  " + (self._args_text or "{}")
        if self._result_text:
            text += "\n\n结果  " + self._result_text
        self.body.configure(state="normal", height=min(10, text.count("\n") + 2))
        self.body.delete("1.0", "end")
        self.body.insert("1.0", text)
        self.body.configure(state="disabled")

    def finish(self, ok: bool, result: str) -> None:
        self.dot.set_color(t.SUCCESS if ok else t.DANGER)
        self.status.configure(text="✓ 成功" if ok else "✗ 失败",
                              fg=t.SUCCESS if ok else t.DANGER)
        clean = str(result or "").strip()
        if len(clean) > 4000:
            clean = clean[:4000] + "\n…（已截断）"
        self._result_text = clean
        if self._open:
            self._render_body()


class MenuPopup(tk.Toplevel):
    """Frameless classical dropdown: hairline border, soft hover rows,
    terracotta current mark, keyboard navigation, outside-click dismissal.

    ``items``: dicts of {label, desc="", current=False, disabled=False}.
    ``command(index)`` fires for the chosen row before the popup closes.
    """

    _instances: list["MenuPopup"] = []

    def __init__(self, anchor: tk.Misc, items: list[dict], fonts: "t.Fonts",
                 command: Callable[[int], None], *, min_width: int = 180,
                 align_right: bool = False) -> None:
        root = anchor.winfo_toplevel()
        for old in list(MenuPopup._instances):   # 任何时刻只保留一个菜单
            if old is not self:
                old._dismiss()
        super().__init__(root)
        MenuPopup._instances.append(self)
        self.command = command
        self.fonts = fonts
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=t.LINE_STRONG)
        self._rows: list[tk.Frame] = []
        self._items = items
        self._hover = -1

        body = tk.Frame(self, bg=t.SURFACE)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        label_font = fonts.small
        widest = t.s(min_width)
        for index, item in enumerate(items):
            disabled = bool(item.get("disabled"))
            current = bool(item.get("current"))
            row = tk.Frame(body, bg=t.SURFACE, cursor="arrow" if disabled else "hand2")
            row.pack(fill="x")
            inner = tk.Frame(row, bg=t.SURFACE)
            inner.pack(fill="x", padx=t.s(11), pady=t.s(7))
            mark = tk.Frame(inner, bg=t.SURFACE, width=t.s(13))
            mark.pack(side="left", padx=(t.s(2), t.s(7)))
            dot = None
            if current:
                dot = Dot(mark, color=t.TERRACOTTA, size=6, bg=t.SURFACE)
                dot.pack(anchor="center", pady=(t.s(6), 0))
            col = tk.Frame(inner, bg=t.SURFACE)
            col.pack(side="left", fill="x", expand=True)
            lab = tk.Label(col, text=str(item.get("label") or ""), bg=t.SURFACE,
                           fg=t.INK_FAINT if disabled
                           else (t.TERRACOTTA if current else t.INK),
                           font=label_font, anchor="w")
            lab.pack(anchor="w")
            dl = None
            desc = str(item.get("desc") or "")
            if desc:
                dl = tk.Label(col, text=desc, bg=t.SURFACE, fg=t.INK_FAINT,
                              font=fonts.kicker, anchor="w", justify="left",
                              wraplength=t.s(min_width + 140))
                dl.pack(anchor="w", pady=(t.s(1), 0))
            row._parts = [row, inner, mark, col]
            if dot is not None:
                row._parts.append(dot)
            row._lab, row._dl = lab, dl
            row._disabled = disabled
            row._current = current
            for w in (row, inner, mark, col, lab):
                w.bind("<Enter>", lambda _e, i=index: self._set_hover(i), add="+")
                w.bind("<Button-1>", lambda _e, i=index: self._pick(i), add="+")
            self._rows.append(row)
            widest = max(widest, t.s(46) + label_font.measure(str(item.get("label") or "")))

        self.update_idletasks()
        req_w = min(max(widest, body.winfo_reqwidth()), t.s(360))
        req_h = self.winfo_reqheight()
        try:
            ax, ay = anchor.winfo_rootx(), anchor.winfo_rooty()
            aw, ah = anchor.winfo_width(), anchor.winfo_height()
            sx, sy = root.winfo_rootx(), root.winfo_rooty()
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
        except tk.TclError:
            ax = ay = aw = ah = 0
            sx, sy, sw, sh = 100, 100, 1920, 1080
        x = ax + aw - req_w if align_right else ax
        y = ay + ah + t.s(3)
        if y + req_h > sh - t.s(10):
            y = ay - req_h - t.s(3)
        x = max(4, min(x, sw - req_w - 4))
        self.geometry(f"{req_w}x{req_h}+{x}+{y}")
        self.bind("<Escape>", lambda _e: self._dismiss())
        self.bind("<Up>", lambda _e: self._move(-1))
        self.bind("<Down>", lambda _e: self._move(1))
        self.bind("<Return>", lambda _e: self._pick(self._hover, fire=True))
        self.bind("<Button-1>", self._outside_guard, add="+")
        self._watcher_active = False
        self.after(20, self._engage)

    def _engage(self) -> None:
        """No pointer grab: overrideredirect windows on Windows cannot rely
        on grab_set (it frequently fails silently, leaving the popup
        undismissable).  A permanent bind_all press-watcher is the same
        mechanism the scroll wheel hook uses."""
        if not _alive(self):
            return
        try:
            self.bind_all("<ButtonPress-1>", self._global_press, add="+")
            self._watcher_active = True
            self.focus_force()
        except tk.TclError:
            pass

    def _inside_px(self, x_root: int, y_root: int) -> bool:
        try:
            x, y = self.winfo_rootx(), self.winfo_rooty()
            return (x <= x_root <= x + self.winfo_width()
                    and y <= y_root <= y + self.winfo_height())
        except tk.TclError:
            return True

    def _global_press(self, event) -> None:
        x = getattr(event, "x_root", None)
        y = getattr(event, "y_root", None)
        if x is None or y is None:
            x, y = self.winfo_pointerx(), self.winfo_pointery()
        if not self._inside_px(int(x), int(y)):
            self._dismiss()
        # 行内点击由每行自己的 <Button-1> 处理

    def _outside_guard(self, event) -> None:
        x = getattr(event, "x_root", None)
        y = getattr(event, "y_root", None)
        if x is None or y is None:
            x, y = self.winfo_pointerx(), self.winfo_pointery()
        if not self._inside_px(int(x), int(y)):
            self._dismiss()

    def _set_hover(self, index: int) -> None:
        if self._items[index].get("disabled"):
            return
        self._hover = index
        for i, row in enumerate(self._rows):
            hover = i == index
            bg = t.TERRACOTTA_SOFT if hover else t.SURFACE
            for part in getattr(row, "_parts", ()):
                try:
                    part.configure(bg=bg)
                except tk.TclError:
                    pass
            lab = getattr(row, "_lab", None)
            if lab is not None:
                if row._disabled:
                    fg = t.INK_FAINT
                elif hover or row._current:
                    fg = t.TERRACOTTA
                else:
                    fg = t.INK
                try:
                    lab.configure(fg=fg)
                except tk.TclError:
                    pass
            dl = getattr(row, "_dl", None)
            if dl is not None:
                try:
                    dl.configure(fg=t.INK_SOFT if hover else t.INK_FAINT)
                except tk.TclError:
                    pass

    def _move(self, delta: int) -> None:
        n = len(self._items)
        i = self._hover if self._hover >= 0 else (0 if delta > 0 else n - 1)
        for _ in range(n):
            i = (i + delta) % n
            if not self._items[i].get("disabled"):
                break
        self._set_hover(i)

    def _pick(self, index: int, fire: bool = False) -> None:
        if index < 0:
            return
        if not fire and self._items[index].get("disabled"):
            return
        command, self.command = self.command, None
        self._dismiss()
        if command:
            command(index)

    def _dismiss(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        try:
            if self._watcher_active:
                self.unbind_all("<ButtonPress-1>")
                self._watcher_active = False
        except tk.TclError:
            pass
        if self in MenuPopup._instances:
            MenuPopup._instances.remove(self)
        try:
            master = self.master
            self.destroy()
            if master is not None and _alive(master):
                master.focus_force()
        except tk.TclError:
            pass


class MessageDialog(tk.Toplevel):
    """Frameless classical confirm dialog (delete prompts, destructive ops)."""

    WIDTH = 400

    def __init__(self, root: tk.Misc, fonts: "t.Fonts", *, title: str,
                 message: str, confirm_text: str = "确定",
                 cancel_text: str = "取消", danger: bool = False,
                 on_choice: Callable[[bool], None]) -> None:
        super().__init__(root)
        self.on_choice = on_choice
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=t.LINE_STRONG)
        body = tk.Frame(self, bg=t.HEADER)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Frame(body, bg=t.DANGER if danger else t.TERRACOTTA,
                 width=3).pack(side="left", fill="y")
        card = tk.Frame(body, bg=t.HEADER)
        card.pack(side="left", fill="both", expand=True,
                  padx=t.s(18), pady=t.s(15))
        head = tk.Frame(card, bg=t.HEADER, cursor="fleur")
        head.pack(fill="x")
        kicker(head, "Confirm · 确认", font=fonts.kicker, bg=t.HEADER,
               fg=t.INK_MUTED).pack(side="left")
        close = tk.Label(head, text="✕", bg=t.HEADER, fg=t.INK_FAINT,
                         font=fonts.small, cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: self._answer(False))
        for w in (head, card):
            w.bind("<ButtonPress-1>", self._drag_start, add="+")
            w.bind("<B1-Motion>", self._drag_move, add="+")
        tk.Label(card, text=title, bg=t.HEADER, fg=t.INK,
                 font=fonts.display_md).pack(anchor="w", pady=(t.s(10), t.s(4)))
        tk.Label(card, text=message, bg=t.HEADER, fg=t.INK_SOFT,
                 font=fonts.small, justify="left", anchor="w",
                 wraplength=t.s(self.WIDTH - 52)).pack(fill="x", pady=(0, t.s(4)))
        actions = tk.Frame(card, bg=t.HEADER)
        actions.pack(fill="x", pady=(t.s(12), 0))
        FlatButton(actions, cancel_text, lambda: self._answer(False),
                   font=fonts.small, variant="ghost", height=34, min_width=88,
                   parent_bg=t.HEADER).pack(side="right", padx=(t.s(8), 0))
        FlatButton(actions, confirm_text, lambda: self._answer(True),
                   font=fonts.small_bold, variant="danger" if danger else "primary",
                   height=34, min_width=88,
                   parent_bg=t.HEADER).pack(side="right")
        self.update_idletasks()
        try:
            rw, rh = root.winfo_rootx(), root.winfo_rooty()
            ww, wh = root.winfo_width(), root.winfo_height()
            dw, dh = self.winfo_reqwidth(), self.winfo_reqheight()
            x = rw + max(0, (ww - dw) // 2)
            y = rh + max(0, (wh - dh) // 3)
        except tk.TclError:
            x = y = 200
        self.geometry(f"+{x}+{y}")
        self.bind("<Escape>", lambda _e: self._answer(False))
        self.bind("<Return>", lambda _e: self._answer(True))
        self.after(60, self._safe_grab)

    def _safe_grab(self) -> None:
        try:
            if _alive(self):
                self.grab_set()
                self.focus_force()
        except tk.TclError:
            pass

    def _answer(self, ok: bool) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        callback, self.on_choice = self.on_choice, None
        self.destroy()
        if callback:
            callback(ok)

    def _drag_start(self, event) -> None:
        self._drag = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag_move(self, event) -> None:
        ox, oy = getattr(self, "_drag", (0, 0))
        self.geometry(f"+{event.x_root - ox}+{event.y_root - oy}")


class ApprovalDialog(tk.Toplevel):
    """Frameless classical modal for tool-call confirmations (ask events)."""

    WIDTH = 460

    def __init__(self, root: tk.Misc, fonts: "t.Fonts", data: dict,
                 on_choice) -> None:
        super().__init__(root)
        self.on_choice = on_choice
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=t.LINE_STRONG)
        body = tk.Frame(self, bg=t.HEADER)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Frame(body, bg=t.WARN if data.get("plan") else t.TERRACOTTA,
                 width=3).pack(side="left", fill="y")

        card = tk.Frame(body, bg=t.HEADER)
        card.pack(side="left", fill="both", expand=True,
                  padx=t.s(18), pady=t.s(15))
        head = tk.Frame(card, bg=t.HEADER, cursor="fleur")
        head.pack(fill="x")
        kicker(head, "Approval · 需要确认", font=fonts.kicker,
               bg=t.HEADER, fg=t.INK_MUTED).pack(side="left")
        close = tk.Label(head, text="✕", bg=t.HEADER, fg=t.INK_FAINT,
                         font=fonts.small, cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: self._answer("no"))
        for w in (head, card):
            w.bind("<ButtonPress-1>", self._drag_start, add="+")
            w.bind("<B1-Motion>", self._drag_move, add="+")

        name = str(data.get("name") or "操作")
        row = tk.Frame(card, bg=t.HEADER)
        row.pack(fill="x", pady=(t.s(12), t.s(4)))
        tk.Label(row, text=name, bg=t.HEADER, fg=t.INK,
                 font=fonts.mono).pack(side="left")
        args = str(data.get("arguments") or "")
        if args and len(args) < 90:
            tk.Label(row, text=args, bg=t.HEADER, fg=t.INK_FAINT,
                     font=fonts.mono).pack(side="left", padx=t.s(10))

        tk.Label(card, text=str(data.get("question") or "确认执行该操作吗？"),
                 bg=t.HEADER, fg=t.INK_SOFT, font=fonts.small, justify="left",
                 anchor="w", wraplength=t.s(self.WIDTH - 60)).pack(fill="x",
                                                                   pady=(0, t.s(10)))

        if data.get("plan"):
            for i, step in enumerate(data["plan"]):
                srow = tk.Frame(card, bg=t.SURFACE_ALT)
                srow.pack(fill="x", pady=t.s(2))
                tk.Label(srow, text=f"{i + 1:02d}", bg=t.SURFACE_ALT, fg=t.TERRACOTTA,
                         font=fonts.kicker).pack(side="left", padx=t.s(8), pady=t.s(5))
                tools = "、".join(str(x) for x in (step.get("tools") or []))
                tk.Label(srow, text=str(step.get("step", "")) +
                         (f"    （{tools}）" if tools else ""),
                         bg=t.SURFACE_ALT, fg=t.INK_SOFT, font=fonts.small,
                         anchor="w", justify="left",
                         wraplength=t.s(self.WIDTH - 110)).pack(side="left", fill="x",
                                                                expand=True, pady=t.s(5))
        elif data.get("diff"):
            diff = tk.Text(card, bg=t.CODE_SURFACE, fg=t.INK_SOFT, font=fonts.mono,
                           relief="flat", bd=0, highlightthickness=0, height=7,
                           wrap="none", padx=t.s(10), pady=t.s(8), cursor="arrow")
            diff.pack(fill="x")
            diff.insert("1.0", str(data["diff"])[:2500])
            diff.configure(state="disabled")

        actions = tk.Frame(card, bg=t.HEADER)
        actions.pack(fill="x", pady=(t.s(16), 0))
        FlatButton(actions, "拒绝", lambda: self._answer("no"), font=fonts.small,
                   variant="ghost", height=36, min_width=96, parent_bg=t.HEADER
                   ).pack(side="right", padx=(t.s(8), 0))
        FlatButton(actions, "允许", lambda: self._answer("yes"), font=fonts.small_bold,
                   variant="primary", height=36, min_width=96, parent_bg=t.HEADER
                   ).pack(side="right")

        self.update_idletasks()
        try:
            rw, rh = root.winfo_rootx(), root.winfo_rooty()
            ww, wh = root.winfo_width(), root.winfo_height()
            dw, dh = self.winfo_reqwidth(), self.winfo_reqheight()
            x = rw + max(0, (ww - dw) // 2)
            y = rh + max(0, (wh - dh) // 3)
        except tk.TclError:
            x = y = 200
        self.geometry(f"+{x}+{y}")
        self.bind("<Escape>", lambda _e: self._answer("no"))
        self.after(60, self._safe_grab)

    def _safe_grab(self) -> None:
        # 无边框窗口在 Windows 上的 grab 不可靠（经常静默失败导致按钮失灵），
        # 模态感由 topmost + 强制焦点保证。
        try:
            if _alive(self):
                self.focus_force()
                self.lift()
        except tk.TclError:
            pass

    def _answer(self, choice: str) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        callback, self.on_choice = self.on_choice, None
        self.destroy()
        if callback:
            callback(choice)

    def _drag_start(self, event) -> None:
        self._drag = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag_move(self, event) -> None:
        ox, oy = getattr(self, "_drag", (0, 0))
        self.geometry(f"+{event.x_root - ox}+{event.y_root - oy}")
