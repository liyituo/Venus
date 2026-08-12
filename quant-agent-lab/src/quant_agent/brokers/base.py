from __future__ import annotations

from typing import Protocol

from quant_agent.domain.models import BrokerOrder, ProposedOrder


class Broker(Protocol):
    def submit(self, order: ProposedOrder) -> BrokerOrder: ...

    def cancel(self, order_id: str) -> BrokerOrder: ...

    def get_order(self, order_id: str) -> BrokerOrder | None: ...
