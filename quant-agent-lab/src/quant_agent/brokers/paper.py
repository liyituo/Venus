from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from quant_agent.domain.enums import BrokerOrderStatus
from quant_agent.domain.models import BrokerOrder, Fill, ProposedOrder, canonical_hash
from quant_agent.infrastructure.clock import Clock


class PaperBroker:
    """Deterministic in-memory broker for paper-only execution."""

    def __init__(
        self,
        clock: Clock,
        *,
        fee_bps: Decimal = Decimal("5"),
        default_fill_policy: str = "full",
        policies: dict[str, str] | None = None,
    ) -> None:
        self.clock = clock
        self.fee_bps = fee_bps
        self.default_fill_policy = default_fill_policy
        self.policies = policies or {}
        self._orders: dict[str, BrokerOrder] = {}

    def submit(self, order: ProposedOrder) -> BrokerOrder:
        existing = self._orders.get(order.client_order_id)
        if existing is not None:
            return existing
        submitted_at = self.clock.now()
        policy = self.policies.get(order.order_id, self.default_fill_policy).lower()
        if order.quantity <= 0 or order.reference_price <= 0:
            broker_order = BrokerOrder(
                order_id=order.order_id,
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                status=BrokerOrderStatus.REJECTED,
                submitted_at=submitted_at,
                filled_quantity=Decimal("0"),
                remaining_quantity=order.quantity,
                rejection_reason="INVALID_ORDER",
            )
            self._orders[order.client_order_id] = broker_order
            return broker_order
        if policy == "reject":
            broker_order = BrokerOrder(
                order_id=order.order_id,
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                status=BrokerOrderStatus.REJECTED,
                submitted_at=submitted_at,
                filled_quantity=Decimal("0"),
                remaining_quantity=order.quantity,
                rejection_reason="PAPER_SCENARIO_REJECTED",
            )
            self._orders[order.client_order_id] = broker_order
            return broker_order
        if policy == "cancel":
            broker_order = BrokerOrder(
                order_id=order.order_id,
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                status=BrokerOrderStatus.CANCELLED,
                submitted_at=submitted_at,
                filled_quantity=Decimal("0"),
                remaining_quantity=order.quantity,
            )
            self._orders[order.client_order_id] = broker_order
            return broker_order
        quantity = order.quantity
        status = BrokerOrderStatus.FILLED
        if policy == "partial" and order.quantity > Decimal("1"):
            quantity = (order.quantity / Decimal("2")).quantize(
                Decimal("0.00000001"), rounding=ROUND_DOWN
            )
            status = BrokerOrderStatus.PARTIALLY_FILLED
        price = order.limit_price or order.reference_price
        fee = quantity * price * self.fee_bps / Decimal("10000")
        fill_identity = {"order_id": order.order_id, "quantity": quantity, "price": price}
        fill = Fill(
            fill_id=f"fill_{canonical_hash(fill_identity)[:20]}",
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            price=price,
            fee=fee,
            executed_at=submitted_at,
        )
        broker_order = BrokerOrder(
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            status=status,
            submitted_at=submitted_at,
            filled_quantity=quantity,
            remaining_quantity=order.quantity - quantity,
            fills=(fill,),
        )
        self._orders[order.client_order_id] = broker_order
        return broker_order

    def cancel(self, order_id: str) -> BrokerOrder:
        for client_id, order in self._orders.items():
            if order.order_id == order_id:
                cancelled = BrokerOrder(
                    order_id=order.order_id,
                    client_order_id=order.client_order_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    status=BrokerOrderStatus.CANCELLED,
                    submitted_at=order.submitted_at,
                    filled_quantity=order.filled_quantity,
                    remaining_quantity=order.remaining_quantity,
                    fills=order.fills,
                    rejection_reason=order.rejection_reason,
                )
                self._orders[client_id] = cancelled
                return cancelled
        raise KeyError(order_id)

    def get_order(self, order_id: str) -> BrokerOrder | None:
        return next((order for order in self._orders.values() if order.order_id == order_id), None)
