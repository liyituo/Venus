"""Tiny-MoE 策略：排序映射 / 配置加载 / 推理集成。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("numpy")
pytest.importorskip("torch")

from quant_agent.infrastructure.config import TinyMoeConfig, _load_symbols, load_demo_config
from quant_agent.strategies.tiny_moe_ranker import (
    TinyMoeRankerStrategy,
    map_rank_to_direction,
    normalize_strength,
)


def test_map_rank_to_direction_top_and_bottom():
    assert (
        map_rank_to_direction(1, 100, 20)
        == __import__("quant_agent.domain.enums", fromlist=["SignalDirection"]).SignalDirection.BUY
    )
    assert map_rank_to_direction(20, 100, 20).value == "BUY"
    assert map_rank_to_direction(81, 100, 20).value == "SELL"
    assert map_rank_to_direction(50, 100, 20).value == "HOLD"


def test_normalize_strength_monotonic():
    s1 = normalize_strength(1, 100)
    s50 = normalize_strength(50, 100)
    s100 = normalize_strength(100, 100)
    assert s1 > s50 > s100
    assert s1 <= Decimal("1")


def test_load_symbols_from_file(tmp_path):
    symbols_path = tmp_path / "symbols.txt"
    symbols_path.write_text("600519\n1\n000001\n", encoding="utf-8")
    loaded = _load_symbols(
        tmp_path,
        {"symbols_file": str(symbols_path.name)},
    )
    assert loaded == ("600519", "000001", "000001")


def test_demo_config_loads_tiny_moe_and_symbols():
    root = Path(__file__).resolve().parents[2]
    cfg = load_demo_config(root / "config")
    assert cfg.strategy.strategy_id == "tiny-moe-ranker"
    assert cfg.tiny_moe.top_k == 20
    assert len(cfg.market_data.symbols) >= 50
    assert cfg.portfolio.currency == "CNY"


def _synthetic_market(n_symbols: int = 60, n_days: int = 80):
    from quant_agent.domain.models import MarketBar, MarketSnapshot

    bars = []
    start = datetime(2026, 1, 2, tzinfo=UTC)
    for s in range(n_symbols):
        symbol = f"{600000 + s}"
        price = Decimal("10") + Decimal(s)
        for d in range(n_days):
            day = start + timedelta(days=d)
            close = price + Decimal(d) * Decimal("0.01")
            bars.append(
                MarketBar(
                    symbol=symbol,
                    timestamp=day,
                    open=close,
                    high=close + Decimal("0.1"),
                    low=close - Decimal("0.1"),
                    close=close,
                    volume=Decimal("1000000"),
                    currency="CNY",
                    timeframe="1d",
                    source="test",
                    is_synthetic=True,
                    snapshot_id="t",
                )
            )
    return MarketSnapshot(
        snapshot_id="t",
        as_of=bars[-1].timestamp,
        source="test",
        bars=tuple(bars),
    )


def test_strategy_emits_buy_and_sell_with_mock_predictor(tmp_path):
    checkpoint = tmp_path / "best_model.pt"
    checkpoint.write_bytes(b"fake")

    market = _synthetic_market()
    cfg = TinyMoeConfig(
        checkpoint_path=str(checkpoint),
        top_k=5,
        min_stocks_per_day=50,
        lookback=60,
    )
    strat = TinyMoeRankerStrategy(cfg, project_root=tmp_path)

    mock_stocks = []
    for i, sym in enumerate(sorted(market.bars_by_symbol())[:60]):
        mock_stocks.append({"symbol": sym, "score": float(60 - i), "rank": i + 1})

    mock_predictor = MagicMock()
    mock_predictor.feature_names = [
        "return_1d",
        "return_5d",
        "return_10d",
        "return_20d",
        "volatility_5d",
        "volatility_20d",
        "ma5_ratio",
        "ma10_ratio",
        "ma20_ratio",
        "rsi",
        "momentum_5d",
        "momentum_20d",
        "volume_ratio",
        "high_low_range",
    ]
    mock_predictor.market_feature_names = [
        "market_return_1d",
        "market_return_5d",
        "market_return_20d",
        "market_volatility_5d",
        "market_volatility_20d",
        "advance_ratio",
        "cross_section_dispersion",
    ]
    mock_predictor.predict_daily.return_value = {"stocks": mock_stocks, "gate_weights": None}

    with patch.object(strat, "_get_predictor", return_value=mock_predictor):
        signals = strat.generate(market)

    buys = [s for s in signals if s.direction.value == "BUY"]
    sells = [s for s in signals if s.direction.value == "SELL"]
    assert len(buys) == 5
    assert len(sells) == 5
    assert all(s.strategy_id == "tiny-moe-ranker" for s in signals)
