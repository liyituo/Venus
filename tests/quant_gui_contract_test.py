"""Static GUI contract checks that do not require a display server."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUANT = (ROOT / "src" / "quant_integration.py").read_text(encoding="utf-8")
EXAMPLE = (ROOT / "chat_config.example.json").read_text(encoding="utf-8")
WEB = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def main() -> None:
    assert "DEFAULT_BACKEND_URL" in QUANT and "8014" in QUANT
    assert "DEFAULT_GUI_URL" in QUANT and "4173" in QUANT
    assert "QuantServiceController" in QUANT
    assert "generate_daily_plan" not in QUANT
    assert "LiveBroker" not in QUANT or "禁用" in QUANT or "forbidden" in QUANT.lower()
    assert '"quant_backend_url": "http://127.0.0.1:8014"' in EXAMPLE
    assert '"quant_gui_url": "http://127.0.0.1:4173"' in EXAMPLE
    assert 'href="http://127.0.0.1:4173/#/dashboard"' in WEB
    assert 'rel="noopener noreferrer"' in WEB
    print("PASS quant integration static contract")


if __name__ == "__main__":
    main()
