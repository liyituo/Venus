"""LiveMarketDataProvider 测试：yfinance/akshare 转换、缓存回退、市场校验。

全部 mock yfinance/akshare 模块（不触网）。
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import ModuleType
from unittest import mock

import pytest

from quant_agent.data.live_provider import (
    LiveMarketDataProvider,
    MarketDataUnavailable,
    _is_cn_symbol,
)


def _make_provider(tmp_path, market="us", symbols=("AAPL",)):
    return LiveMarketDataProvider(tmp_path, market=market, symbols=symbols)


class FakeDF:
    """最小 DataFrame 替身（避免测试依赖 pandas）。"""

    def __init__(self, rows: list[dict]):
        self.rows = rows

    @property
    def empty(self) -> bool:
        return not self.rows

    def iterrows(self):
        # 与 pandas 语义一致：yield (时间索引, 行数据)；无 __ts 时用枚举
        for i, r in enumerate(self.rows):
            yield r.get("__ts", i), r

    def tail(self, n: int):
        return FakeDF(self.rows[-n:])


def _fake_yfinance_df():
    rows = []
    start = datetime(2026, 7, 1, tzinfo=UTC)
    for i in range(5):
        ts = start + timedelta(days=i)
        rows.append({"__ts": ts, "Open": 100.0, "High": 101.0,
                     "Low": 99.0, "Close": 100.5, "Volume": 1000})
    return FakeDF(rows)


# ============ symbol 判定 ============
def test_cn_symbol_detection():
    assert _is_cn_symbol("600519")
    assert not _is_cn_symbol("AAPL")
    assert not _is_cn_symbol("600519.SH")   # 带后缀按美股处理（调用方负责归一化）


# ============ yfinance 转换 ============
def test_yfinance_conversion(tmp_path):
    fake_yf = ModuleType("yfinance")
    fake_yf.download = mock.Mock(return_value=_fake_yfinance_df())
    with mock.patch.dict(sys.modules, {"yfinance": fake_yf}):
        snap = _make_provider(tmp_path).load_market()
    assert len(snap.bars) == 5
    bar = snap.bars[0]
    assert bar.symbol == "AAPL"
    assert bar.close == Decimal("100.5000")
    assert bar.currency == "USD"
    assert bar.source == "yfinance"
    assert not bar.is_synthetic
    assert snap.as_of == snap.bars[-1].timestamp
    # 缓存已落盘（file 格式兼容）
    cached = json.loads((tmp_path / "market_snapshot.json").read_text(encoding="utf-8"))
    assert cached["source"] == "live-yfinance/akshare"


# ============ akshare 转换 ============
def test_akshare_conversion(tmp_path):
    df = FakeDF([
        {"日期": "2026-07-01", "开盘": 10.0, "最高": 11.0, "最低": 9.0,
         "收盘": 10.5, "成交量": 500},
        {"日期": "2026-07-02", "开盘": 10.0, "最高": 11.0, "最低": 9.0,
         "收盘": 10.5, "成交量": 500},
        {"日期": "2026-07-03", "开盘": 10.0, "最高": 11.0, "最低": 9.0,
         "收盘": 10.5, "成交量": 500},
    ])
    fake_ak = ModuleType("akshare")
    fake_ak.stock_zh_a_hist = mock.Mock(return_value=df)
    with mock.patch.dict(sys.modules, {"akshare": fake_ak}):
        snap = LiveMarketDataProvider(
            tmp_path, market="cn", symbols=("600519",)).load_market()
    assert len(snap.bars) == 3
    assert snap.bars[0].currency == "CNY"
    assert snap.bars[0].source == "akshare"


# ============ 市场校验 ============
def test_market_mismatch_rejected(tmp_path):
    with pytest.raises(MarketDataUnavailable):
        _make_provider(tmp_path, market="us", symbols=("600519",)).load_market()


# ============ 缓存回退 ============
def test_cache_fallback(tmp_path):
    fake_yf = ModuleType("yfinance")
    fake_yf.download = mock.Mock(return_value=_fake_yfinance_df())
    with mock.patch.dict(sys.modules, {"yfinance": fake_yf}):
        provider = _make_provider(tmp_path)
        first = provider.load_market()
    # 第二次：网络全挂 → 回退缓存
    broken = ModuleType("yfinance")
    broken.download = mock.Mock(side_effect=RuntimeError("network down"))
    with mock.patch.dict(sys.modules, {"yfinance": broken}):
        fallback = provider.load_market_or_cache()
    assert fallback.bars == first.bars
    # 无缓存且失败 → 抛错
    empty = _make_provider(tmp_path / "empty")
    with mock.patch.dict(sys.modules, {"yfinance": broken}):
        with pytest.raises(MarketDataUnavailable):
            empty.load_market_or_cache()


# ============ 无 symbols 配置 ============
def test_missing_symbols(tmp_path):
    with pytest.raises(MarketDataUnavailable):
        LiveMarketDataProvider(tmp_path, market="us", symbols=()).load_market()
