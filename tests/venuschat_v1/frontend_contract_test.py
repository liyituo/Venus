"""Display-free contract checks for the isolated VenusChat V1 frontend."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "venuschat_v1"
sys.path.insert(0, str(ROOT / "src"))

from venuschat_v1 import theme  # noqa: E402


def main() -> None:
    files = {path.name: path.read_text(encoding="utf-8") for path in SRC.glob("*.py")}
    combined = "\n".join(files.values())
    assert "import chat" not in combined
    assert "import llm_server" not in combined
    assert "from chat_theme" not in combined
    assert "class VenusChatV1" in files["app.py"]
    assert "class ChatView" in files["chat_view.py"]
    assert "class SettingsView" in files["settings_view.py"]
    assert "VENUS 智能工作台" in files["chat_view.py"]
    assert "backend_bridge" in files["chat_view.py"] or "ApiClient" in combined
    assert "模型与推理" in files["settings_view.py"]
    assert theme.luminance(theme.CANVAS) > theme.luminance(theme.INK)
    assert theme.luminance(theme.SURFACE) > theme.luminance(theme.INK_SOFT)
    assert theme.TERRACOTTA != theme.SUCCESS
    print("PASS VenusChat V1 isolated frontend contract")


if __name__ == "__main__":
    main()

