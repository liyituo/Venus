from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from .enums import (
    ApprovalScope,
    BrokerOrderStatus,
    Direction,
    OrderType,
    ReportStatus,
    RiskSeverity,
    SignalDirection,
)
from .models import (
    AccountSnapshot,
    Approval,
    AuditEvent,
    BrokerOrder,
    DailyPlan,
    DailyReport,
    ExecutionResult,
    Fill,
    MarketBar,
    MarketSnapshot,
    Position,
    ProposedOrder,
    ReconciliationResult,
    RiskCheck,
    RiskDecision,
    StrategySignal,
    TargetPosition,
)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def market_bar_from_dict(value: dict[str, Any]) -> MarketBar:
    return MarketBar(
        symbol=str(value["symbol"]),
        timestamp=_dt(value["timestamp"]),
        open=_dec(value["open"]),
        high=_dec(value["high"]),
        low=_dec(value["low"]),
        close=_dec(value["close"]),
        volume=_dec(value["volume"]),
        currency=str(value.get("currency", "USD")),
        timeframe=str(value.get("timeframe", "1d")),
        source=str(value.get("source", "unknown")),
        is_synthetic=bool(value.get("is_synthetic", False)),
        session=str(value.get("session", "regular")),
        snapshot_id=str(value.get("snapshot_id", "")),
    )


def market_snapshot_from_dict(value: dict[str, Any]) -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_id=str(value["snapshot_id"]),
        as_of=_dt(value["as_of"]),
        source=str(value["source"]),
        bars=tuple(market_bar_from_dict(item) for item in value.get("bars", [])),
    )


def position_from_dict(value: dict[str, Any]) -> Position:
    return Position(
        symbol=str(value["symbol"]),
        quantity=_dec(value["quantity"]),
        average_price=_dec(value["average_price"]),
        market_price=_dec(value["market_price"]),
        currency=str(value.get("currency", "USD")),
    )


def account_from_dict(value: dict[str, Any]) -> AccountSnapshot:
    return AccountSnapshot(
        account_id=str(value["account_id"]),
        as_of=_dt(value["as_of"]),
        cash=_dec(value["cash"]),
        equity=_dec(value["equity"]),
        currency=str(value["currency"]),
        positions=tuple(position_from_dict(item) for item in value.get("positions", [])),
        status=str(value.get("status", "VERIFIED")),
        source=str(value.get("source", "offline-fixture")),
    )


def signal_from_dict(value: dict[str, Any]) -> StrategySignal:
    return StrategySignal(
        symbol=str(value["symbol"]),
        direction=SignalDirection(value["direction"]),
        strength=_dec(value["strength"]),
        reason_code=str(value["reason_code"]),
        input_start=_dt(value["input_start"]),
        input_end=_dt(value["input_end"]),
        strategy_id=str(value["strategy_id"]),
        strategy_version=str(value["strategy_version"]),
        invalidation_conditions=tuple(
            str(item) for item in value.get("invalidation_conditions", [])
        ),
        reference_price=_dec(value["reference_price"]),
    )


def target_from_dict(value: dict[str, Any]) -> TargetPosition:
    return TargetPosition(
        symbol=str(value["symbol"]),
        current_quantity=_dec(value["current_quantity"]),
        target_quantity=_dec(value["target_quantity"]),
        target_notional=_dec(value["target_notional"]),
        reason_code=str(value["reason_code"]),
    )


def order_from_dict(value: dict[str, Any]) -> ProposedOrder:
    limit_price = value.get("limit_price")
    return ProposedOrder(
        order_id=str(value["order_id"]),
        client_order_id=str(value["client_order_id"]),
        symbol=str(value["symbol"]),
        side=Direction(value["side"]),
        order_type=OrderType(value["order_type"]),
        quantity=_dec(value["quantity"]),
        limit_price=None if limit_price is None else _dec(limit_price),
        reference_price=_dec(value["reference_price"]),
        notional=_dec(value["notional"]),
        estimated_fee=_dec(value["estimated_fee"]),
        estimated_slippage=_dec(value["estimated_slippage"]),
        pre_quantity=_dec(value["pre_quantity"]),
        post_quantity=_dec(value["post_quantity"]),
        reason_code=str(value["reason_code"]),
    )


def risk_check_from_dict(value: dict[str, Any]) -> RiskCheck:
    return RiskCheck(
        check_id=str(value["check_id"]),
        name=str(value["name"]),
        passed=bool(value["passed"]),
        severity=RiskSeverity(value["severity"]),
        reason_code=str(value["reason_code"]),
        message=str(value["message"]),
        order_id=value.get("order_id"),
    )


def risk_decision_from_dict(value: dict[str, Any]) -> RiskDecision:
    return RiskDecision(
        decision_id=str(value["decision_id"]),
        allowed=bool(value["allowed"]),
        checks=tuple(risk_check_from_dict(item) for item in value.get("checks", [])),
        allowed_order_ids=tuple(str(item) for item in value.get("allowed_order_ids", [])),
        blocked_order_ids=tuple(str(item) for item in value.get("blocked_order_ids", [])),
    )


def plan_from_dict(value: dict[str, Any]) -> DailyPlan:
    return DailyPlan(
        plan_id=str(value["plan_id"]),
        account_id=str(value["account_id"]),
        market_snapshot_id=str(value["market_snapshot_id"]),
        generated_at=_dt(value["generated_at"]),
        expires_at=_dt(value["expires_at"]),
        strategy_id=str(value["strategy_id"]),
        strategy_version=str(value["strategy_version"]),
        risk_config_version=str(value["risk_config_version"]),
        signals=tuple(signal_from_dict(item) for item in value.get("signals", [])),
        targets=tuple(target_from_dict(item) for item in value.get("targets", [])),
        orders=tuple(order_from_dict(item) for item in value.get("orders", [])),
        risk_decision=risk_decision_from_dict(value["risk_decision"]),
        data_source=str(value["data_source"]),
        data_as_of=_dt(value["data_as_of"]),
        code_version=str(value["code_version"]),
        plan_hash=str(value.get("plan_hash", "")),
    )


def report_from_dict(value: dict[str, Any]) -> DailyReport:
    return DailyReport(
        report_id=str(value["report_id"]),
        report_version=int(value["report_version"]),
        status=ReportStatus(value["status"]),
        generated_at=_dt(value["generated_at"]),
        expires_at=_dt(value["expires_at"]),
        plan=plan_from_dict(value["plan"]),
        account=account_from_dict(value["account"]),
        market=market_snapshot_from_dict(value["market"]),
        local_timezone=str(value.get("local_timezone", "UTC")),
        warnings=tuple(str(item) for item in value.get("warnings", [])),
    )


def approval_from_dict(value: dict[str, Any]) -> Approval:
    revoked_at = value.get("revoked_at")
    return Approval(
        approval_id=str(value["approval_id"]),
        report_id=str(value["report_id"]),
        report_version=int(value["report_version"]),
        plan_hash=str(value["plan_hash"]),
        account_id=str(value["account_id"]),
        strategy_id=str(value["strategy_id"]),
        strategy_version=str(value["strategy_version"]),
        risk_config_version=str(value["risk_config_version"]),
        approved_order_ids=tuple(str(item) for item in value.get("approved_order_ids", [])),
        approved_at=_dt(value["approved_at"]),
        expires_at=_dt(value["expires_at"]),
        approver=str(value["approver"]),
        approval_scope=ApprovalScope(value["approval_scope"]),
        revoked=bool(value.get("revoked", False)),
        revoked_at=None if revoked_at is None else _dt(revoked_at),
    )


def fill_from_dict(value: dict[str, Any]) -> Fill:
    return Fill(
        fill_id=str(value["fill_id"]),
        order_id=str(value["order_id"]),
        symbol=str(value["symbol"]),
        side=Direction(value["side"]),
        quantity=_dec(value["quantity"]),
        price=_dec(value["price"]),
        fee=_dec(value["fee"]),
        executed_at=_dt(value["executed_at"]),
    )


def broker_order_from_dict(value: dict[str, Any]) -> BrokerOrder:
    return BrokerOrder(
        order_id=str(value["order_id"]),
        client_order_id=str(value["client_order_id"]),
        symbol=str(value["symbol"]),
        side=Direction(value["side"]),
        quantity=_dec(value["quantity"]),
        status=BrokerOrderStatus(value["status"]),
        submitted_at=_dt(value["submitted_at"]),
        filled_quantity=_dec(value["filled_quantity"]),
        remaining_quantity=_dec(value["remaining_quantity"]),
        fills=tuple(fill_from_dict(item) for item in value.get("fills", [])),
        rejection_reason=value.get("rejection_reason"),
    )


def reconciliation_from_dict(value: dict[str, Any]) -> ReconciliationResult:
    return ReconciliationResult(
        ok=bool(value["ok"]),
        messages=tuple(str(item) for item in value.get("messages", [])),
        before_cash=_dec(value["before_cash"]),
        after_cash=_dec(value["after_cash"]),
        before_positions=tuple(
            position_from_dict(item) for item in value.get("before_positions", [])
        ),
        after_positions=tuple(
            position_from_dict(item) for item in value.get("after_positions", [])
        ),
        remaining_order_ids=tuple(str(item) for item in value.get("remaining_order_ids", [])),
    )


def execution_from_dict(value: dict[str, Any]) -> ExecutionResult:
    return ExecutionResult(
        execution_id=str(value["execution_id"]),
        report_id=str(value["report_id"]),
        request_id=str(value["request_id"]),
        mode=str(value["mode"]),
        status=str(value["status"]),
        started_at=_dt(value["started_at"]),
        completed_at=_dt(value["completed_at"]),
        broker_orders=tuple(
            broker_order_from_dict(item) for item in value.get("broker_orders", [])
        ),
        fills=tuple(fill_from_dict(item) for item in value.get("fills", [])),
        reconciliation=reconciliation_from_dict(value["reconciliation"]),
        idempotency_key=str(value["idempotency_key"]),
        error=value.get("error"),
    )


def audit_from_dict(value: dict[str, Any]) -> AuditEvent:
    return AuditEvent(
        event_id=str(value["event_id"]),
        timestamp=_dt(value["timestamp"]),
        event_type=str(value["event_type"]),
        actor=str(value["actor"]),
        request_id=str(value["request_id"]),
        report_id=value.get("report_id"),
        order_id=value.get("order_id"),
        before_state=value.get("before_state"),
        after_state=value.get("after_state"),
        reason_code=str(value["reason_code"]),
        input_summary=str(value["input_summary"]),
        result_summary=str(value["result_summary"]),
    )
