"""Tiny-MoE 横截面排序策略：Top-K 买入 / 尾部卖出，接入 Paper 交易链路。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from quant_agent.domain.enums import SignalDirection
from quant_agent.domain.models import MarketSnapshot, StrategySignal
from quant_agent.infrastructure.config import TinyMoeConfig
from quant_agent.ml.predictor import QuantPredictor
from quant_agent.ml.snapshot_adapter import latest_cross_section

_MAX_REASON = 40
_MAX_STRENGTH = Decimal("1.0")


def map_rank_to_direction(rank: int, universe_size: int, top_k: int) -> SignalDirection:
    if rank <= top_k:
        return SignalDirection.BUY
    if rank > max(universe_size - top_k, top_k):
        return SignalDirection.SELL
    return SignalDirection.HOLD


def normalize_strength(rank: int, universe_size: int) -> Decimal:
    if universe_size <= 1:
        return Decimal("0")
    # rank=1 最强 → 接近 1.0；rank=N 最弱 → 接近 0
    value = (universe_size - rank + 1) / universe_size
    try:
        strength = Decimal(str(value)).quantize(Decimal("0.0001"))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    if strength <= 0 or strength > _MAX_STRENGTH:
        return Decimal("0")
    return strength


class TinyMoeRankerStrategy:
    strategy_id = "tiny-moe-ranker"
    version = "2.0.0"

    def __init__(
        self,
        config: TinyMoeConfig,
        *,
        project_root: Path,
        audit=None,
        version: str = "2.0.0",
    ) -> None:
        self.config = config
        self.project_root = project_root
        self.audit = audit
        self.version = version
        self._predictor: QuantPredictor | None = None
        self._predictor_error = ""

    def _checkpoint_path(self) -> Path:
        path = Path(self.config.checkpoint_path)
        if path.is_absolute():
            return path
        return self.project_root / path

    def _get_predictor(self) -> QuantPredictor | None:
        if self._predictor is not None:
            return self._predictor
        checkpoint = self._checkpoint_path()
        if not checkpoint.exists():
            self._predictor_error = f"CHECKPOINT_MISSING: {checkpoint}"
            return None
        try:
            self._predictor = QuantPredictor(str(checkpoint), device=self.config.device)
            self._predictor_error = ""
            return self._predictor
        except Exception as exc:  # noqa: BLE001 — 降级 HOLD，不阻塞报告
            self._predictor_error = f"PREDICTOR_LOAD_FAILED: {type(exc).__name__}"
            return None

    def generate(self, market: MarketSnapshot) -> tuple[StrategySignal, ...]:
        predictor = self._get_predictor()
        end = market.as_of
        symbols_in_market = sorted(market.bars_by_symbol())

        def _hold_all(reason: str) -> tuple[StrategySignal, ...]:
            signals: list[StrategySignal] = []
            latest = market.latest_by_symbol()
            for symbol in symbols_in_market:
                bar = latest.get(symbol)
                ref = bar.close if bar is not None else Decimal("0")
                start = bar.timestamp if bar is not None else end
                signals.append(
                    StrategySignal(
                        symbol=symbol,
                        direction=SignalDirection.HOLD,
                        strength=Decimal("0"),
                        reason_code=reason[:_MAX_REASON],
                        input_start=start,
                        input_end=end,
                        strategy_id=self.strategy_id,
                        strategy_version=self.version,
                        invalidation_conditions=("ranking inputs or model unavailable",),
                        reference_price=ref,
                    )
                )
            return tuple(signals)

        if predictor is None:
            if self.audit is not None:
                self._audit("ALL", self._predictor_error, "HOLD")
            return _hold_all(self._predictor_error or "MODEL_UNAVAILABLE")

        section = latest_cross_section(
            market,
            lookback=self.config.lookback,
            min_stocks=self.config.min_stocks_per_day,
            stock_feature_names=predictor.feature_names,
            market_feature_names=predictor.market_feature_names,
        )
        if section is None:
            if self.audit is not None:
                self._audit("ALL", "INSUFFICIENT_CROSS_SECTION", "HOLD")
            return _hold_all("INSUFFICIENT_CROSS_SECTION")

        stock_matrix, market_vector, symbols, trade_date, ref_prices = section
        try:
            result = predictor.predict_daily(stock_matrix, market_vector, symbols)
        except Exception as exc:  # noqa: BLE001
            reason = f"INFERENCE_FAILED:{type(exc).__name__}"
            if self.audit is not None:
                self._audit("ALL", reason, "HOLD")
            return _hold_all(reason)

        rank_by_symbol = {item["symbol"]: item["rank"] for item in result["stocks"]}
        score_by_symbol = {item["symbol"]: item["score"] for item in result["stocks"]}
        universe_size = len(symbols)
        input_start = datetime.fromisoformat(trade_date).replace(tzinfo=end.tzinfo)

        signals: list[StrategySignal] = []
        for symbol in symbols_in_market:
            bar = market.latest_by_symbol().get(symbol)
            ref = ref_prices.get(symbol, bar.close if bar is not None else Decimal("0"))
            rank = rank_by_symbol.get(symbol)
            if rank is None:
                signals.append(
                    StrategySignal(
                        symbol=symbol,
                        direction=SignalDirection.HOLD,
                        strength=Decimal("0"),
                        reason_code="NOT_IN_UNIVERSE",
                        input_start=input_start,
                        input_end=end,
                        strategy_id=self.strategy_id,
                        strategy_version=self.version,
                        invalidation_conditions=("symbol dropped from model universe",),
                        reference_price=ref,
                    )
                )
                continue

            direction = map_rank_to_direction(rank, universe_size, self.config.top_k)
            strength = (
                normalize_strength(rank, universe_size)
                if direction != SignalDirection.HOLD
                else Decimal("0")
            )
            score = score_by_symbol.get(symbol, 0.0)
            reason = f"RANK_{rank}_SCORE_{score:.4f}"[:_MAX_REASON]
            signals.append(
                StrategySignal(
                    symbol=symbol,
                    direction=direction,
                    strength=strength,
                    reason_code=reason,
                    input_start=input_start,
                    input_end=end,
                    strategy_id=self.strategy_id,
                    strategy_version=self.version,
                    invalidation_conditions=(
                        f"rank leaves top-{self.config.top_k} band",
                        "cross-section features become stale",
                    ),
                    reference_price=ref,
                )
            )
            if self.audit is not None and direction != SignalDirection.HOLD:
                self._audit(symbol, f"rank={rank} score={score:.4f}", direction.value)

        return tuple(sorted(signals, key=lambda item: item.symbol))

    def _audit(self, symbol: str, result: str, reason: str) -> None:
        try:
            self.audit.record(
                "tiny_moe_signal.generated",
                actor="tiny-moe-ranker",
                reason_code=reason,
                input_summary=f"{symbol}",
                result_summary=result[:500],
            )
        except Exception:
            pass
