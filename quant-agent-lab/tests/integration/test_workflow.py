import json
from dataclasses import replace
from datetime import timedelta

import pytest

from quant_agent.domain.enums import ReportStatus
from quant_agent.domain.errors import ApprovalError, DataValidationError, RiskBlockedError


def test_unapproved_execution_fails(service):
    report = service.generate_report("2026-08-11")
    with pytest.raises(ApprovalError):
        service.execute(report.report_id, mode="paper")


def test_rejected_execution_fails(service):
    report = service.generate_report("2026-08-11")
    service.reject(report.report_id)
    with pytest.raises(ApprovalError):
        service.execute(report.report_id, mode="paper")


def test_expired_approval_fails(service, clock):
    report = service.generate_report("2026-08-11")
    service.approve_all(report.report_id)
    clock.advance(seconds=3601)
    with pytest.raises(ApprovalError):
        service.execute(report.report_id, mode="paper")
    assert service.get_report(report.report_id).status == ReportStatus.EXPIRED


def test_plan_mutation_invalidates_approval(service):
    report = service.generate_report("2026-08-11")
    service.approve_all(report.report_id)
    current = service.get_report(report.report_id)
    changed_order = replace(current.plan.orders[0], quantity=current.plan.orders[0].quantity + 1)
    current.plan.orders = (changed_order, *current.plan.orders[1:])
    service.store.save_report(current)
    with pytest.raises(ApprovalError):
        service.execute(report.report_id, mode="paper")


def test_partial_approval_executes_only_selected_orders(service):
    report = service.generate_report("2026-08-11")
    selected = report.plan.risk_decision.allowed_order_ids[0]
    service.approve_partial(report.report_id, (selected,))
    result = service.execute(report.report_id, mode="paper")
    assert {order.order_id for order in result.broker_orders} == {selected}
    assert service.get_report(report.report_id).status == ReportStatus.PARTIALLY_FILLED


def test_kill_switch_blocks_execution(service):
    report = service.generate_report("2026-08-11")
    service.approve_all(report.report_id)
    service.set_kill_switch(True, "test stop")
    with pytest.raises(RiskBlockedError):
        service.execute(report.report_id, mode="paper")


def test_execution_reloads_and_blocks_stale_account(service, clock):
    report = service.generate_report("2026-08-11")
    service.approve_all(report.report_id)
    account = service.provider.load_account()
    service.provider.save_account(replace(account, as_of=clock.now() - timedelta(days=2)))

    with pytest.raises(RiskBlockedError):
        service.execute(report.report_id, mode="paper")

    assert service.get_report(report.report_id).status == ReportStatus.APPROVED


def test_duplicate_execute_is_idempotent(service):
    report = service.generate_report("2026-08-11")
    service.approve_all(report.report_id)
    first = service.execute(report.report_id, mode="paper", request_id="first")
    second = service.execute(report.report_id, mode="paper", request_id="second")
    assert second.execution_id == first.execution_id
    assert len(service.store.list_audit(report.report_id)) >= 3


def test_stale_data_is_blocked_and_cannot_be_approved(service, clock):
    market = service.provider.load_market()
    service.provider.save_market(replace(market, as_of=clock.now() - timedelta(days=2)))
    report = service.generate_report("2026-08-11")
    assert report.status == ReportStatus.RISK_BLOCKED
    assert any(check.reason_code == "DATA_STALE" for check in report.plan.risk_decision.checks)
    with pytest.raises(ApprovalError):
        service.approve_all(report.report_id)


def test_stale_account_is_blocked_and_cannot_be_approved(service, clock):
    account = service.provider.load_account()
    service.provider.save_account(replace(account, as_of=clock.now() - timedelta(days=2)))

    report = service.generate_report("2026-08-11")

    assert report.status == ReportStatus.RISK_BLOCKED
    assert any(check.reason_code == "ACCOUNT_STALE" for check in report.plan.risk_decision.checks)
    with pytest.raises(ApprovalError):
        service.approve_all(report.report_id)


def test_invalid_price_is_safe_failure(service):
    market = service.provider.load_market()
    invalid_bar = replace(market.bars[0], close=market.bars[0].close * 0)
    service.provider.save_market(replace(market, bars=(invalid_bar, *market.bars[1:])))
    report = service.generate_report("2026-08-11")
    assert report.status == ReportStatus.RISK_BLOCKED
    assert any(
        check.reason_code == "DATA_INVALID_PRICE" for check in report.plan.risk_decision.checks
    )


def test_timezone_missing_market_data_is_a_structured_block(service):
    payload = json.loads(service.provider.market_path.read_text(encoding="utf-8"))
    payload["as_of"] = payload["as_of"].replace("Z", "")
    service.provider.market_path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    report = service.generate_report("2026-08-11")

    assert report.status == ReportStatus.RISK_BLOCKED
    assert any(
        check.reason_code == "DATA_TIMEZONE_MISSING" for check in report.plan.risk_decision.checks
    )


def test_missing_data_fails_without_creating_orders(service):
    service.provider.market_path.unlink()
    with pytest.raises(DataValidationError):
        service.generate_report("2026-08-11")
    assert not service.store.get_value("last_submitted_order")


def test_audit_log_does_not_contain_secret_like_fixture_values(service):
    service.audit.record(
        "test.event",
        input_summary="safe summary only",
        result_summary="safe result only",
    )
    contents = service.paths.audit_log.read_text(encoding="utf-8")
    assert "SECRET" not in contents
    assert "token" not in contents.lower()


def test_duplicate_audit_event_is_mirrored_once(service, clock):
    first = service.audit.record(
        "duplicate.event", request_id="same-request", timestamp=clock.now()
    )
    second = service.audit.record(
        "duplicate.event", request_id="same-request", timestamp=clock.now()
    )

    assert second.event_id == first.event_id
    entries = [
        json.loads(line)
        for line in service.paths.audit_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert sum(entry["event_id"] == first.event_id for entry in entries) == 1
