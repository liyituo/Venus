"""Capture the standalone VenusChat V1 main and settings views."""

from __future__ import annotations

import sys
import time
import ctypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import tkinter as tk  # noqa: E402
from PIL import ImageGrab  # noqa: E402

from venuschat_v1 import VenusChatV1  # noqa: E402
from venuschat_v1 import theme  # noqa: E402


OUT = ROOT / ".venus" / "ui-preview-v1"


def capture(root: tk.Tk, name: str) -> Path:
    root.update_idletasks()
    root.update()
    time.sleep(.35)
    root.update()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    width, height = root.winfo_width(), root.winfo_height()
    if sys.platform == "win32":
        try:
            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            rect = RECT()
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                x, y = rect.left, rect.top
                width, height = rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
    path = OUT / f"{name}.png"
    ImageGrab.grab(bbox=(x, y, x + width, y + height)).save(path)
    print(path)
    return path


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    theme.enable_dpi_awareness()
    root = tk.Tk()
    theme.init_scale(root)
    app = VenusChatV1(root)
    root.geometry(f"{theme.s(1500)}x{theme.s(900)}+{theme.s(30)}+{theme.s(30)}")
    root.attributes("-topmost", True)
    capture(root, "1-main")
    app.show_settings()
    capture(root, "2-settings")
    app.show_chat()
    app.chat_view.open_conversation(0)
    capture(root, "3-conversation")
    root.destroy()


if __name__ == "__main__":
    main()
