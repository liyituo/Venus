"""MarketSnapshot → Tiny-MoE 特征矩阵。"""
from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd

from quant_agent.domain.models import MarketBar, MarketSnapshot
from quant_agent.ml.feature_builder import FeatureBuilder


def _bar_date(bar: MarketBar) -> str:
    return bar.timestamp.date().isoformat()


def market_bars_to_ohlcv(market: MarketSnapshot) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for bar in market.bars:
        rows.append(
            {
                "date": _bar_date(bar),
                "symbol": str(bar.symbol),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
            }
        )
    return pd.DataFrame(rows)


def latest_cross_section(
    market: MarketSnapshot,
    *,
    lookback: int = 60,
    min_stocks: int = 50,
    stock_feature_names: list[str],
    market_feature_names: list[str],
) -> tuple[np.ndarray, np.ndarray, list[str], str, dict[str, Decimal]] | None:
    """构建最近一个可用交易日的横截面特征。

    返回 (stock_matrix [N,F], market_vector [M], symbols, trade_date, ref_prices) 或 None。
    """
    if not market.bars:
        return None

    ohlcv = market_bars_to_ohlcv(market)
    if ohlcv.empty:
        return None

    builder = FeatureBuilder(lookback=lookback)
    stock_feat, market_feat, _built_stock_names, _built_market_names = builder.build(ohlcv)

    as_of_date = market.as_of.date().isoformat()
    candidate_dates = sorted(stock_feat["date"].unique())
    trade_date = None
    for date in reversed(candidate_dates):
        if date > as_of_date:
            continue
        day = stock_feat[stock_feat["date"] == date]
        if len(day) >= min_stocks:
            trade_date = date
            break
    if trade_date is None:
        return None

    day_stock = stock_feat[stock_feat["date"] == trade_date].copy()
    day_market = market_feat[market_feat["date"] == trade_date]
    if day_market.empty:
        return None

    missing_stock = [c for c in stock_feature_names if c not in day_stock.columns]
    if missing_stock:
        return None

    day_stock = day_stock.sort_values("symbol").reset_index(drop=True)
    symbols = day_stock["symbol"].astype(str).tolist()
    stock_matrix = day_stock[stock_feature_names].to_numpy(dtype=np.float64)
    if not np.isfinite(stock_matrix).all():
        return None

    market_row = day_market.iloc[0]
    market_vector = market_row[market_feature_names].to_numpy(dtype=np.float64)
    if not np.isfinite(market_vector).all():
        return None

    ref_prices: dict[str, Decimal] = {}
    latest = market.latest_by_symbol()
    for sym in symbols:
        bar = latest.get(sym)
        if bar is not None:
            ref_prices[sym] = bar.close

    return stock_matrix, market_vector, symbols, trade_date, ref_prices
