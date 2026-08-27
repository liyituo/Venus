"""Tool confirmation dialog for VenusChat V1.

Frameless classical modal (see widgets.ApprovalDialog): hairline border,
terracotta accent stripe, ClearType copy, styled allow/deny buttons.  Kept as
a thin adapter so the bridge-facing callback signature (allowed, request_id)
stays stable.
"""

from __future__ import annotations

import tkinter as tk

from .widgets import ApprovalDialog


def show_confirm(root: tk.Misc, data: dict, on_choice, fonts=None) -> None:
    """on_choice(allowed: bool, request_id: str)"""

    if fonts is None:
        from . import theme as _t
        fonts = _t.Fonts.create(root)

    def _bridge_choice(choice: str) -> None:
        on_choice(choice == "yes", str(data.get("id") or ""))

    ApprovalDialog(root, fonts, data or {}, _bridge_choice)
