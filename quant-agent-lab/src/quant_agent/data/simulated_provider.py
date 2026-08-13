"""SimulatedMarketProvider：模拟市场（无网络、每天自动推进、可复现）。

解决真实行情依赖与静态 demo 数据过期两大痛点：
- 每次 load_market：从上次快照的最后交易日推进到「今天」，按确定性随机游走
  生成新 bar（seed = symbol+date，同一日期永远生成相同价格——可复现）
- as_of 永远是今天 → 通过数据新鲜度校验，可每日跑通全链路
- 完全离线：不依赖 yfinance/akshare/网络
- 持久化到 market_snapshot.json（与 file 模式同格式，可随时切回真实行情）

配置：market_data.source: simulated + symbols（如 AAPL/MSFT/600519）
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quant_agent.data.providers import FileDataProvider
from quant_agent.domain.models import MarketBar, MarketSnapshot

# 基准价：首次初始化时的起点（symbol → 起始价/日波动率/币种）
_BASELINE = {
    "AAPL": (Decimal("100"), Decimal("0.015"), "USD"),
    "MSFT": (Decimal("200"), Decimal("0.012"), "USD"),
    "DEMO": (Decimal("50"), Decimal("0.020"), "USD"),
    # A股默认按人民币（代码为数字时）
}
_CN_BASELINE = (Decimal("60"), Decimal("0.018"), "CNY")
_HISTORY_DAYS = 40


class SimulatedMarketProvider(FileDataProvider):
    """确定性随机游走模拟市场。now_fn 可注入（测试/压力测试快进时钟）。"""

    def __init__(self, data_dir: Path, symbols: tuple[str, ...] = (), now_fn=None):
        super().__init__(data_dir)
        self.symbols = symbols
        self._now_fn = now_fn or (lambda: datetime.now(UTC).replace(microsecond=0))

    def load_market(self) -> MarketSnapshot:
        symbols = list(self.symbols) or ["AAPL", "MSFT"]
        now = self._now_fn()
        prev = self._load_previous()

        bars: list[MarketBar] = []
        for symbol in symbols:
            if prev and symbol in prev:
                history = list(prev[symbol])
            else:
                history = self._init_history(symbol, now)
            bars.extend(self._advance(symbol, history, now))
        snapshot = MarketSnapshot(
            snapshot_id=f"sim-{uuid.uuid4().hex[:8]}",
            as_of=now,  # 真实当前时刻：永远新鲜且不晚于评估时钟
            source="simulated-market",
            bars=tuple(bars),
        )
        self.save_market(snapshot)
        return snapshot

    # ---- 内部 ----
    def _load_previous(self) -> dict[str, list[MarketBar]] | None:
        import json

        from quant_agent.domain.codec import market_snapshot_from_dict

        if not self.market_path.exists():
            return None
        snap = market_snapshot_from_dict(json.loads(self.market_path.read_text(encoding="utf-8")))
        if snap.source == "simulated-market":
            out: dict[str, list[MarketBar]] = {}
            for b in snap.bars:
                out.setdefault(b.symbol, []).append(b)
            return {k: sorted(v, key=lambda x: x.timestamp) for k, v in out.items()}
        return None

    def _baseline(self, symbol: str) -> tuple[Decimal, Decimal, str]:
        if symbol.isdigit():
            return _CN_BASELINE
        return _BASELINE.get(symbol, (Decimal("100"), Decimal("0.015"), "USD"))

    def _init_history(self, symbol: str, now: datetime) -> list[MarketBar]:
        base, vol, currency = self._baseline(symbol)
        bars: list[MarketBar] = []
        start = now - timedelta(days=_HISTORY_DAYS)
        price = base
        for i in range(_HISTORY_DAYS):
            day = start + timedelta(days=i)
            drift = self._daily_return(symbol, day) * vol * price
            price = max(price + drift, base * Decimal("0.2"))
            bars.append(self._bar(symbol, day, price, currency, i))
        return bars

    def _advance(self, symbol: str, history: list[MarketBar], now: datetime) -> list[MarketBar]:
        """从历史最后一天推进到当前日期（bar 时间戳统一当日 00:00 UTC，
        保证 ≤ as_of=now，避免 DATA_TIME_IN_FUTURE）。"""
        if not history:
            return []
        base, vol, currency = self._baseline(symbol)
        last_day = history[-1].timestamp.date()
        target = now.date()
        if last_day >= target:
            return history
        bars = list(history)
        price = history[-1].close
        day = last_day + timedelta(days=1)
        seq = len(history)
        while day <= target:
            drift = self._daily_return(symbol, day) * vol * price
            price = max(price + drift, base * Decimal("0.2"))
            bars.append(
                self._bar(
                    symbol, datetime(day.year, day.month, day.day, tzinfo=UTC), price, currency, seq
                )
            )
            seq += 1
            day += timedelta(days=1)
        return bars

    def _daily_return(self, symbol: str, day) -> Decimal:
        """确定性日收益 ∈ [-1, 1]：seed = symbol+date（可复现，无随机库）。"""
        seed = f"{symbol}:{day}"
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        v = int.from_bytes(digest[:8], "big") / 2**64
        return (Decimal(str(v)) * 2 - 1).quantize(Decimal("0.0001"))

    @staticmethod
    def _bar(symbol: str, day: datetime, price: Decimal, currency: str, seq: int) -> MarketBar:
        return MarketBar(
            symbol=symbol,
            timestamp=day,
            open=price,
            high=price * (1 + Decimal("0.005")),
            low=price * (1 - Decimal("0.005")),
            close=price,
            volume=Decimal("100000") + seq * 1000,
            currency=currency,
            timeframe="1d",
            source="simulated-market",
            is_synthetic=True,
            session="regular",
            snapshot_id="",
        )
