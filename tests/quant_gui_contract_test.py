"""Static GUI contract checks that do not require a display server."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHAT = (ROOT / "src" / "chat.py").read_text(encoding="utf-8")
WEB = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def main() -> None:
    assert '"quant_enabled": True' in CHAT
    assert '"quant_backend_url": "http://127.0.0.1:8014"' in CHAT
    assert '"quant_gui_url": "http://127.0.0.1:4173"' in CHAT
    assert '"量化中心", self._open_quant_center' in CHAT
    assert "<<OpenQuantCenter>>" in CHAT
    assert "threading.Thread(target=self._quant_open_worker" in CHAT
    assert "root.after(0, self._quant_open_done" in CHAT
    assert "quant_agent" not in re.sub(r'from quant_integration import.*?(?=\n\n)', '', CHAT, flags=re.S)
    assert "generate_daily_plan" not in CHAT
    assert "approve" not in CHAT.lower().split("def _open_quant_center", 1)[1].split("def _set_quant_button", 1)[0]
    assert "quant_center_btn" in CHAT
    assert 'href="http://127.0.0.1:4173/#/dashboard"' in WEB
    assert 'rel="noopener noreferrer"' in WEB
    print("PASS main GUI quant button/static integration contract")


if __name__ == "__main__":
    main()
