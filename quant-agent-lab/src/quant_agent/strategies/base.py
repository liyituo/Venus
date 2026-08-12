from __future__ import annotations

from typing import Protocol

from quant_agent.domain.models import MarketSnapshot, StrategySignal


class Strategy(Protocol):
    strategy_id: str
    version: str

    def generate(self, market: MarketSnapshot) -> tuple[StrategySignal, ...]: ...
