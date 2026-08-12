from __future__ import annotations

from decimal import Decimal

from quant_agent.domain.enums import Direction, OrderType, SignalDirection
from quant_agent.domain.models import (
    AccountSnapshot,
    MarketSnapshot,
    ProposedOrder,
    StrategySignal,
    TargetPosition,
    canonical_hash,
    quantize_down,
)
from quant_agent.infrastructure.config import PortfolioConfig


class PortfolioPlanner:
    def __init__(self, config: PortfolioConfig) -> None:
        self.config = config

    def build(
        self,
        signals: tuple[StrategySignal, ...],
        account: AccountSnapshot,
        market: MarketSnapshot,
    ) -> tuple[tuple[TargetPosition, ...], tuple[ProposedOrder, ...]]:
        prices = {symbol: bar.close for symbol, bar in market.latest_by_symbol().items()}
        current = {position.symbol: position.quantity for position in account.positions}
        targets: list[TargetPosition] = []
        orders: list[ProposedOrder] = []
        for signal in sorted(signals, key=lambda item: item.symbol):
            price = prices.get(signal.symbol, signal.reference_price)
            current_quantity = current.get(signal.symbol, Decimal("0"))
            if signal.direction == SignalDirection.BUY:
                target_quantity = quantize_down(
                    self.config.target_notional_per_signal / price, self.config.lot_size
                )
                target_quantity = max(target_quantity, current_quantity)
                reason = "TARGET_FROM_BUY_SIGNAL"
            elif signal.direction == SignalDirection.SELL:
                target_quantity = Decimal("0")
                reason = "TARGET_FLAT_FROM_SELL_SIGNAL"
            else:
                target_quantity = current_quantity
                reason = "NO_ACTION_SIGNAL"
            target_notional = target_quantity * price
            targets.append(
                TargetPosition(
                    symbol=signal.symbol,
                    current_quantity=current_quantity,
                    target_quantity=target_quantity,
                    target_notional=target_notional,
                    reason_code=reason,
                )
            )
            delta = target_quantity - current_quantity
            if delta == 0:
                continue
            side = Direction.BUY if delta > 0 else Direction.SELL
            quantity = abs(delta)
            notional = quantity * price
            fee = notional * self.config.fee_bps / Decimal("10000")
            slippage = notional * self.config.slippage_bps / Decimal("10000")
            identity = {
                "symbol": signal.symbol,
                "side": side.value,
                "quantity": format(quantity, "f"),
                "price": format(price, "f"),
                "strategy_version": signal.strategy_version,
            }
            order_id = f"ord_{canonical_hash(identity)[:16]}"
            orders.append(
                ProposedOrder(
                    order_id=order_id,
                    client_order_id=order_id,
                    symbol=signal.symbol,
                    side=side,
                    order_type=OrderType.MARKET,
                    quantity=quantity,
                    limit_price=price,
                    reference_price=price,
                    notional=notional,
                    estimated_fee=fee,
                    estimated_slippage=slippage,
                    pre_quantity=current_quantity,
                    post_quantity=target_quantity,
                    reason_code=signal.reason_code,
                )
            )
        return tuple(targets), tuple(orders)
