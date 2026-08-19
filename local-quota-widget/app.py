"""桌面额度组件：透明窗外、上下两张圆角卡片。"""

from __future__ import annotations

import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, simpledialog

from PIL import Image, ImageDraw, ImageFont

import autostart
import layered
from providers.codex import fetch_codex_quota
from providers.cursor import ProviderQuota, fetch_cursor_quota
from storage import get_secret, load_settings, save_settings, set_secret

W, H = 228, 252
CARD_W, CARD_H = 216, 116
GAP = 8
MARGIN = 6
MENU = 22

# Cursor：海雾蓝；Codex：杏茶金。卡片不透明，窗外全透明。
CURSOR_CARD = (22, 42, 56, 235)
CURSOR_ACCENT = (126, 201, 196)
CURSOR_TEXT = (236, 246, 247)
CURSOR_MUTED = (156, 186, 190)
CODEX_CARD = (48, 32, 22, 235)
CODEX_ACCENT = (232, 176, 112)
CODEX_TEXT = (250, 241, 228)
CODEX_MUTED = (196, 168, 138)
OK = (126, 196, 154)
WARN = (228, 186, 104)
DANGER = (224, 132, 118)
WHITE = (255, 255, 255)


def _format_reset(ts: int | None) -> str:
    if not ts:
        return ""
    try:
        value = ts // 1000 if ts > 10_000_000_000 else ts
        return datetime.fromtimestamp(value).strftime("%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def _remaining(used: float | None) -> float | None:
    if used is None:
        return None
    return max(0.0, min(100.0, 100.0 - float(used)))


def _tone(pct: float | None) -> tuple[int, int, int]:
    """pct 为剩余百分比：越少越红。"""
    if pct is None:
        return (140, 140, 140)
    if pct <= 15:
        return DANGER
    if pct <= 40:
        return WARN
    return OK


def _primary(quota: ProviderQuota) -> float | None:
    if not quota.ok:
        return None
    for win in quota.windows:
        if win.used_percent is not None:
            return _remaining(win.used_percent)
    return None


def _font(paths: list[str], size: int) -> ImageFont.ImageFont:
    for path in paths:
        p = Path(path)
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return ImageFont.load_default()


WIN = Path(r"C:\Windows\Fonts")
FONT_TITLE = _font([str(WIN / "msyh.ttc"), str(WIN / "segoeui.ttf")], 13)
FONT_NUM = _font(
    [str(WIN / "segoeuil.ttf"), str(WIN / "SitkaVF.ttf"), str(WIN / "georgia.ttf"), str(WIN / "segoeui.ttf")],
    32,
)
FONT_BODY = _font([str(WIN / "msyh.ttc"), str(WIN / "segoeui.ttf")], 11)
FONT_SMALL = _font([str(WIN / "msyh.ttc"), str(WIN / "segoeui.ttf")], 11)


def _ring(size: int, pct: float | None, accent: tuple[int, int, int], spin: float) -> Image.Image:
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 7 * scale
    width = 6 * scale
    box = (pad, pad, s - pad, s - pad)
    draw.arc(box, 0, 360, fill=(*accent, 48), width=width)
    if pct is None and spin:
        start = -90 + spin
        draw.arc(box, start, start + 64, fill=(*accent, 255), width=width)
    elif pct is not None:
        extent = max(5.0, pct * 3.6)
        draw.arc(box, -90, -90 + extent, fill=(*_tone(pct), 255), width=width)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def _bar(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, pct: float | None) -> None:
    draw.rounded_rectangle((x, y, x + w, y + 5), 2, fill=(255, 255, 255, 28))
    if pct is None:
        return
    fill = max(6, int(w * max(0.0, min(100.0, pct)) / 100.0))
    draw.rounded_rectangle((x, y, x + fill, y + 5), 2, fill=(*_tone(pct), 255))


class QuotaWidget:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.settings.setdefault("refresh_seconds", 300)
        self.settings.setdefault("always_on_top", True)

        self.root = tk.Tk()
        self.root.title("额度")
        self.root.geometry(f"{W}x{H}")
        self.root.overrideredirect(True)
        self.root.resizable(False, False)
        self.root.configure(bg="#000000")
        self.root.attributes("-topmost", bool(self.settings.get("always_on_top", True)))

        self.cursor_q = ProviderQuota(ok=False, title="Cursor", error="读取中")
        self.codex_q = ProviderQuota(ok=False, title="Codex", error="读取中")
        self._busy = False
        self._spin = 0.0
        self._hits: list[tuple[str, int, int, int, int]] = []
        self._card_regions: list[tuple[int, int, int, int]] = []
        self._refresh_job: str | None = None
        self._anim_job: str | None = None
        self._drag: tuple[int, int] | None = None
        self._save_pos_job: str | None = None
        self._hwnd = 0

        self._bind()
        self._restore_pos()
        self.root.after(50, self._init_layered)
        self.refresh_async()
        self._tick()

    def _bind(self) -> None:
        self.root.bind("<ButtonPress-1>", self._on_press)
        self.root.bind("<B1-Motion>", self._on_drag)
        self.root.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Escape>", lambda _e: self.close())
        self.root.bind("<F5>", lambda _e: self.refresh_async())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="刷新", command=self.refresh_async)
        menu.add_command(label="置顶", command=self.toggle_pin)
        menu.add_command(label="开机自启", command=self.toggle_autostart)
        menu.add_command(label="设置 Token", command=self.open_settings)
        menu.add_separator()
        menu.add_command(label="关闭", command=self.close)
        self._menu = menu
        self.root.bind("<Button-3>", lambda e: self._menu.tk_popup(e.x_root, e.y_root))

    def _init_layered(self) -> None:
        self.root.update_idletasks()
        self._hwnd = layered.hwnd_of(self.root)
        layered.enable_layered(self._hwnd)
        layered.install_click_through(self._hwnd)
        self._paint()

    def _restore_pos(self) -> None:
        x, y = self.settings.get("pos_x"), self.settings.get("pos_y")
        try:
            x_i, y_i = int(x), int(y)
        except (TypeError, ValueError):
            x_i = self.root.winfo_screenwidth() - W - 28
            y_i = self.root.winfo_screenheight() - H - 72
        self.root.geometry(f"{W}x{H}+{x_i}+{y_i}")

    def _save_pos(self) -> None:
        self.settings["pos_x"] = self.root.winfo_x()
        self.settings["pos_y"] = self.root.winfo_y()
        save_settings(self.settings)

    def _hit(self, x: int, y: int) -> str:
        for name, x1, y1, x2, y2 in self._hits:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return name
        return ""

    def _in_card(self, x: int, y: int) -> bool:
        for x1, y1, x2, y2 in self._card_regions:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return True
        return False

    def _on_press(self, event: tk.Event) -> None:
        name = self._hit(event.x, event.y)
        if name == "menu":
            self._open_menu(event)
        elif self._in_card(event.x, event.y):
            self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag is None:
            return
        self.root.geometry(f"+{event.x_root - self._drag[0]}+{event.y_root - self._drag[1]}")

    def _on_release(self, _event: tk.Event) -> None:
        if self._drag is None:
            return
        self._drag = None
        if self._save_pos_job:
            self.root.after_cancel(self._save_pos_job)
        self._save_pos_job = self.root.after(250, self._save_pos)

    def _open_menu(self, event: tk.Event | None = None) -> None:
        x = self.root.winfo_rootx() + W - MARGIN - 4
        y = self.root.winfo_rooty() + MARGIN + 28
        if event is not None:
            x, y = event.x_root, event.y_root
        self._menu.tk_popup(x, y)

    def toggle_pin(self) -> None:
        pinned = not bool(self.settings.get("always_on_top", True))
        self.settings["always_on_top"] = pinned
        save_settings(self.settings)
        self.root.attributes("-topmost", pinned)
        self._paint()

    def toggle_autostart(self) -> None:
        try:
            enabled = not autostart.is_enabled()
            autostart.set_enabled(enabled)
            self.settings["auto_start"] = enabled
            save_settings(self.settings)
        except OSError as exc:
            messagebox.showerror("开机自启", str(exc), parent=self.root)
        self._paint()

    def close(self) -> None:
        self._save_pos()
        if self._refresh_job:
            self.root.after_cancel(self._refresh_job)
        if self._anim_job:
            self.root.after_cancel(self._anim_job)
        self.root.destroy()

    def open_settings(self) -> None:
        current = get_secret("cursor_session_token")
        hint = "已保存，留空则不修改" if current else "粘贴 WorkosCursorSessionToken"
        value = simpledialog.askstring(
            "Cursor Token",
            "浏览器打开 cursor.com/dashboard/usage\n"
            "DevTools → Cookies → WorkosCursorSessionToken\n\n"
            f"{hint}",
            parent=self.root,
            show="*",
        )
        if value is None:
            return
        if value.strip():
            set_secret("cursor_session_token", value.strip())
        self.refresh_async()

    def refresh_async(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._paint()

        def worker() -> None:
            token = get_secret("cursor_session_token")
            cursor_q = fetch_cursor_quota(token)
            codex_q = fetch_codex_quota()
            self.root.after(0, lambda: self._apply(cursor_q, codex_q))

        threading.Thread(target=worker, daemon=True).start()

    def _apply(self, cursor_q: ProviderQuota, codex_q: ProviderQuota) -> None:
        self.cursor_q = cursor_q
        self.codex_q = codex_q
        self._busy = False
        self._paint()
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if self._refresh_job:
            self.root.after_cancel(self._refresh_job)
        seconds = max(60, min(int(self.settings.get("refresh_seconds") or 300), 3600))
        self._refresh_job = self.root.after(seconds * 1000, self.refresh_async)

    def _tick(self) -> None:
        if self._busy:
            self._spin = (self._spin + 10) % 360
            self._paint()
            delay = 40
        else:
            delay = 600
        self._anim_job = self.root.after(delay, self._tick)

    def _card(
        self,
        canvas: Image.Image,
        y: int,
        quota: ProviderQuota,
        fill: tuple[int, int, int, int],
        accent: tuple[int, int, int],
        text: tuple[int, int, int],
        muted: tuple[int, int, int],
        with_menu: bool = False,
    ) -> None:
        x = MARGIN
        draw = ImageDraw.Draw(canvas)
        x2, y2 = x + CARD_W, y + CARD_H
        self._card_regions.append((x, y, x2, y2))
        draw.rounded_rectangle((x, y, x2, y2), 16, fill=fill)
        draw.rounded_rectangle((x + 1, y + 1, x2 - 1, y + 2), 1, fill=(*WHITE, 38))
        draw.rectangle((x + 8, y + 16, x + 11, y2 - 16), fill=(*accent, 255))

        draw.text((x + 20, y + 10), quota.title, font=FONT_TITLE, fill=accent)
        plan = (quota.plan or "").strip()
        if plan and not with_menu:
            draw.text((x2 - 12, y + 12), plan, font=FONT_SMALL, fill=muted, anchor="ra")

        if with_menu:
            bx1, by1, bx2, by2 = x2 - 28, y + 8, x2 - 8, y + 28
            draw.rounded_rectangle((bx1, by1, bx2, by2), 8, fill=fill)
            draw.text(((bx1 + bx2) / 2, (by1 + by2) / 2 - 1), "···", font=FONT_TITLE, fill=text, anchor="mm")
            self._hits = [("menu", bx1, by1, bx2, by2)]

        ring = _ring(58, None if self._busy else _primary(quota), accent, self._spin)
        canvas.paste(ring, (x + 16, y + 36), ring)

        pct = _primary(quota)
        nx, ny = x + 82, y + 38
        if self._busy:
            draw.text((nx, ny), "…", font=FONT_NUM, fill=muted)
            draw.text((nx, ny + 40), "读取中", font=FONT_BODY, fill=muted)
        elif not quota.ok:
            draw.text((nx, ny), "—", font=FONT_NUM, fill=muted)
            err = (quota.error or "无法读取")[:12]
            draw.text((nx, ny + 40), err, font=FONT_BODY, fill=DANGER)
        elif pct is None:
            draw.text((nx, ny), "—", font=FONT_NUM, fill=muted)
            draw.text((nx, ny + 40), "暂无数据", font=FONT_BODY, fill=muted)
        else:
            draw.text((nx, ny), f"{pct:.0f}", font=FONT_NUM, fill=text)
            label = quota.windows[0].label if quota.windows else "额度"
            draw.text((nx + 1, ny + 40), f"{label} · 剩余%", font=FONT_BODY, fill=muted)

        rows = [w for w in quota.windows[1:] if w.used_percent is not None][:1]
        ry = y + 94
        for win in rows:
            left = _remaining(float(win.used_percent or 0))
            draw.text((x + 20, ry), win.label, font=FONT_SMALL, fill=muted)
            _bar(draw, x + 72, ry + 6, 88, left)
            extra = win.detail or _format_reset(win.reset_at) or (f"{left:.0f}%" if left is not None else "")
            draw.text((x2 - 12, ry), extra[:10], font=FONT_SMALL, fill=muted, anchor="ra")

        if quota.ok and quota.extra_lines and not rows:
            draw.text((x + 20, ry), "  ·  ".join(quota.extra_lines)[:22], font=FONT_SMALL, fill=muted)

    def _paint(self) -> None:
        if not self._hwnd:
            return
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        self._hits = []
        self._card_regions = []
        self._card(
            canvas, MARGIN, self.cursor_q,
            CURSOR_CARD, CURSOR_ACCENT, CURSOR_TEXT, CURSOR_MUTED, True,
        )
        self._card(
            canvas, MARGIN + CARD_H + GAP, self.codex_q,
            CODEX_CARD, CODEX_ACCENT, CODEX_TEXT, CODEX_MUTED,
        )
        try:
            layered.set_hit_mask(canvas)
            layered.present(self._hwnd, canvas)
        except Exception as exc:
            from storage import LOCAL_DIR, ensure_local_dir
            ensure_local_dir()
            (LOCAL_DIR / "widget.log").write_text(repr(exc), encoding="utf-8")

    def run(self) -> None:
        self.root.mainloop()


def _ensure_single_instance() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, "Local\\AIQuotaWidget-SingleInstance")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        raise SystemExit(0)


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("当前透明桌面组件仅支持 Windows")
    _ensure_single_instance()
    QuotaWidget().run()


if __name__ == "__main__":
    main()
