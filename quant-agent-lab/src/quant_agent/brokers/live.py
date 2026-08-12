from __future__ import annotations

from quant_agent.domain.errors import LiveBrokerDisabledError
from quant_agent.domain.models import BrokerOrder, ProposedOrder


class LiveBroker:
    """Explicitly disabled placeholder. No credential or network path exists."""

    def submit(self, order: ProposedOrder) -> BrokerOrder:
        raise LiveBrokerDisabledError(
            "LiveBroker is disabled in this MVP; only PaperBroker is available"
        )

    def cancel(self, order_id: str) -> BrokerOrder:
        raise LiveBrokerDisabledError("LiveBroker is disabled in this MVP")

    def get_order(self, order_id: str) -> BrokerOrder | None:
        raise LiveBrokerDisabledError("LiveBroker is disabled in this MVP")
