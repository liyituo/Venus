from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from quant_agent.brokers.live import LiveBroker
from quant_agent.brokers.paper import PaperBroker
from quant_agent.domain.enums import ReportStatus
from quant_agent.domain.errors import LiveBrokerDisabledError, StateTransitionError
from quant_agent.domain.models import compute_plan_hash
from quant_agent.domain.state_machine import transition


def test_plan_hash_is_stable(service):
    first = service.generate_report("2026-08-11")
    second = service.generate_report("2026-08-11")
    assert first.plan.plan_hash == second.plan.plan_hash
    assert compute_plan_hash(first.plan) == first.plan.plan_hash


def test_state_machine_rejects_illegal_transition():
    with pytest.raises(StateTransitionError):
        transition(ReportStatus.DRAFT, ReportStatus.FILLED)


def test_risk_checks_are_structured(service):
    report = service.generate_report("2026-08-11")
    assert report.plan.risk_decision.allowed
    assert report.plan.risk_decision.checks
    assert all(check.reason_code and check.message for check in report.plan.risk_decision.checks)


def test_risk_engine_blocks_timezone_invalid_market_without_type_error(service):
    report = service.generate_report("2026-08-11")
    market = replace(report.market, as_of=datetime(2026, 8, 10, 9, 30))

    decision = service.risk.evaluate(
        market,
        report.account,
        report.plan.orders,
        service.clock.now(),
    )

    assert decision.allowed is False
    assert any(check.reason_code == "DATA_TIMEZONE_MISSING" for check in decision.checks)


def test_live_broker_is_explicitly_disabled(service):
    order = service.generate_report("2026-08-11").plan.orders[0]
    with pytest.raises(LiveBrokerDisabledError):
        LiveBroker().submit(order)


def test_paper_broker_partial_fill_is_deterministic(service):
    order = service.generate_report("2026-08-11").plan.orders[0]
    broker = PaperBroker(service.clock, default_fill_policy="partial")
    result = broker.submit(
        replace(order, quantity=Decimal("10"), notional=order.reference_price * Decimal("10"))
    )
    assert result.status.value == "PARTIALLY_FILLED"
    assert result.filled_quantity == Decimal("5.00000000")
    assert (
        broker.submit(
            replace(order, quantity=Decimal("10"), notional=order.reference_price * Decimal("10"))
        )
        == result
    )
