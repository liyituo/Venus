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

    def _max_per_signal(self) -> Decimal:
        cap = self.config.max_notional_per_signal
        if cap is not None and cap > 0:
            return cap
        return self.config.target_notional_per_signal

    def _buy_budget_per_signal(
        self,
        buy_signals: tuple[StrategySignal, ...],
        sell_signals: tuple[StrategySignal, ...],
        account: AccountSnapshot,
        prices: dict[str, Decimal],
        current: dict[str, Decimal],
    ) -> Decimal:
        """按剩余可部署现金（含计划卖出释放）均分买入预算。"""
        deployable = max(Decimal("0"), account.cash - self.config.cash_reserve)
        for signal in sell_signals:
            sym = signal.symbol
            qty = current.get(sym, Decimal("0"))
            price = prices.get(sym, signal.reference_price)
            if qty > 0 and price > 0:
                deployable += qty * price
        actionable = 0
        for signal in buy_signals:
            price = prices.get(signal.symbol, signal.reference_price)
            if price <= 0:
                continue
            current_quantity = current.get(signal.symbol, Decimal("0"))
            min_lot_notional = price * self.config.lot_size
            cap = self._max_per_signal()
            target_quantity = quantize_down(cap / price, self.config.lot_size)
            target_quantity = max(target_quantity, current_quantity)
            if target_quantity > current_quantity and min_lot_notional > 0:
                actionable += 1
        if actionable == 0:
            return self.config.target_notional_per_signal
        per_signal = quantize_down(deployable / Decimal(actionable), Decimal("0.01"))
        return min(self._max_per_signal(), per_signal)

    def build(
        self,
        signals: tuple[StrategySignal, ...],
        account: AccountSnapshot,
        market: MarketSnapshot,
    ) -> tuple[tuple[TargetPosition, ...], tuple[ProposedOrder, ...]]:
        prices = {symbol: bar.close for symbol, bar in market.latest_by_symbol().items()}
        current = {position.symbol: position.quantity for position in account.positions}
        buy_signals = tuple(s for s in signals if s.direction == SignalDirection.BUY)
        sell_signals = tuple(s for s in signals if s.direction == SignalDirection.SELL)
        per_signal_budget = self._buy_budget_per_signal(
            buy_signals, sell_signals, account, prices, current
        )

        # 先卖后买，便于释放敞口与现金
        ordered_signals = sorted(
            signals,
            key=lambda item: (
                0 if item.direction == SignalDirection.SELL else 1,
                -item.strength,
                item.symbol,
            ),
        )

        targets: list[TargetPosition] = []
        orders: list[ProposedOrder] = []
        for signal in ordered_signals:
            price = prices.get(signal.symbol, signal.reference_price)
            current_quantity = current.get(signal.symbol, Decimal("0"))
            if signal.direction == SignalDirection.BUY:
                target_quantity = quantize_down(
                    per_signal_budget / price, self.config.lot_size
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
            current[signal.symbol] = target_quantity
        return tuple(targets), tuple(orders)
