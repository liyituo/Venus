from __future__ import annotations

from decimal import Decimal

from quant_agent.domain.enums import SignalDirection
from quant_agent.domain.models import MarketSnapshot, StrategySignal
from quant_agent.infrastructure.config import StrategyConfig


class MovingAverageStrategy:
    def __init__(self, config: StrategyConfig) -> None:
        if config.fast_window <= 0 or config.slow_window < config.fast_window:
            raise ValueError("moving average windows are invalid")
        self.config = config
        self.strategy_id = config.strategy_id
        self.version = config.version

    def generate(self, market: MarketSnapshot) -> tuple[StrategySignal, ...]:
        signals: list[StrategySignal] = []
        for symbol, bars in sorted(market.bars_by_symbol().items()):
            if len(bars) < self.config.slow_window:
                continue
            window = bars[-self.config.slow_window :]
            fast = (
                sum((bar.close for bar in window[-self.config.fast_window :]), Decimal("0"))
                / self.config.fast_window
            )
            slow = sum((bar.close for bar in window), Decimal("0")) / self.config.slow_window
            if slow <= 0:
                continue
            spread = fast - slow
            strength = abs(spread) / slow
            if strength < self.config.minimum_strength:
                direction = SignalDirection.HOLD
                reason = "MA_NO_ACTION"
            elif spread > 0:
                direction = SignalDirection.BUY
                reason = "MA_FAST_ABOVE_SLOW"
            else:
                direction = SignalDirection.SELL
                reason = "MA_FAST_BELOW_SLOW"
            signals.append(
                StrategySignal(
                    symbol=symbol,
                    direction=direction,
                    strength=strength,
                    reason_code=reason,
                    input_start=window[0].timestamp,
                    input_end=window[-1].timestamp,
                    strategy_id=self.strategy_id,
                    strategy_version=self.version,
                    invalidation_conditions=(
                        "new validated snapshot changes the moving-average relation",
                        "market data becomes stale or invalid",
                    ),
                    reference_price=window[-1].close,
                )
            )
        return tuple(signals)
