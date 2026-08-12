from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from quant_agent.domain.enums import BrokerOrderStatus, Direction
from quant_agent.domain.models import (
    AccountSnapshot,
    BrokerOrder,
    Fill,
    MarketSnapshot,
    Position,
    ProposedOrder,
    ReconciliationResult,
)


def apply_fills(
    account: AccountSnapshot, fills: tuple[Fill, ...], market: MarketSnapshot, now: datetime
) -> AccountSnapshot:
    positions = {position.symbol: position for position in account.positions}
    cash = account.cash
    latest_prices = market.latest_by_symbol()
    for fill in fills:
        current = positions.get(fill.symbol)
        if fill.side == Direction.BUY:
            cash -= fill.quantity * fill.price + fill.fee
            old_quantity = current.quantity if current else Decimal("0")
            old_value = current.average_price * old_quantity if current else Decimal("0")
            quantity = old_quantity + fill.quantity
            average = (old_value + fill.quantity * fill.price) / quantity
            latest_bar = latest_prices.get(fill.symbol)
            latest_price = latest_bar.close if latest_bar is not None else fill.price
            positions[fill.symbol] = Position(
                fill.symbol, quantity, average, latest_price, account.currency
            )
        else:
            if current is None or fill.quantity > current.quantity:
                raise ValueError(f"fill exceeds position for {fill.symbol}")
            cash += fill.quantity * fill.price - fill.fee
            quantity = current.quantity - fill.quantity
            if quantity == 0:
                positions.pop(fill.symbol, None)
            else:
                latest_bar = latest_prices.get(fill.symbol)
                latest_price = latest_bar.close if latest_bar is not None else fill.price
                positions[fill.symbol] = Position(
                    fill.symbol, quantity, current.average_price, latest_price, account.currency
                )
    final_positions = tuple(sorted(positions.values(), key=lambda position: position.symbol))
    equity = cash + sum((position.market_value for position in final_positions), Decimal("0"))
    return AccountSnapshot(
        account_id=account.account_id,
        as_of=now,
        cash=cash,
        equity=equity,
        currency=account.currency,
        positions=final_positions,
        status="VERIFIED",
        source="paper-execution-reconciliation",
    )


def reconcile(
    account: AccountSnapshot,
    proposed_orders: tuple[ProposedOrder, ...],
    broker_orders: tuple[BrokerOrder, ...],
    fills: tuple[Fill, ...],
    market: MarketSnapshot,
    now: datetime,
) -> tuple[ReconciliationResult, AccountSnapshot]:
    expected = {order.order_id: order for order in proposed_orders}
    messages: list[str] = []
    seen_fill_ids: set[str] = set()
    valid = True
    for broker_order in broker_orders:
        if broker_order.order_id not in expected:
            valid = False
            messages.append(f"unknown broker order {broker_order.order_id}")
        if broker_order.filled_quantity > broker_order.quantity:
            valid = False
            messages.append(f"overfill on {broker_order.order_id}")
    for fill in fills:
        if fill.fill_id in seen_fill_ids:
            valid = False
            messages.append(f"duplicate fill {fill.fill_id}")
        seen_fill_ids.add(fill.fill_id)
        if fill.order_id not in expected:
            valid = False
            messages.append(f"fill references unknown order {fill.order_id}")
    try:
        after = apply_fills(account, fills, market, now)
    except ValueError as exc:
        valid = False
        messages.append(str(exc))
        after = account
    remaining = tuple(
        sorted(
            order.order_id
            for order in broker_orders
            if order.status in {BrokerOrderStatus.PARTIALLY_FILLED, BrokerOrderStatus.ACCEPTED}
            and order.remaining_quantity > 0
        )
    )
    if remaining:
        messages.append(f"remaining orders: {','.join(remaining)}")
    if not messages:
        messages.append("all broker orders and fills reconciled")
    return (
        ReconciliationResult(
            ok=valid,
            messages=tuple(messages),
            before_cash=account.cash,
            after_cash=after.cash,
            before_positions=account.positions,
            after_positions=after.positions,
            remaining_order_ids=remaining,
        ),
        after,
    )
