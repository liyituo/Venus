from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from enum import Enum
from typing import Any

from .enums import (
    ApprovalScope,
    BrokerOrderStatus,
    Direction,
    OrderType,
    ReportStatus,
    RiskSeverity,
    SignalDirection,
    StrategyKind,
    StrategyStatus,
)

UTC = UTC


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(UTC)


def parse_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def quantize_down(value: Decimal, unit: Decimal) -> Decimal:
    if unit <= 0:
        raise ValueError("unit must be positive")
    return (value / unit).to_integral_value(rounding=ROUND_DOWN) * unit


def _serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            # Preserve malformed external timestamps verbatim so a blocked
            # report can still be persisted and audited without inventing UTC.
            return value.isoformat()
        return as_utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value):
        return {item.name: _serialize(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _serialize(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_serialize(item) for item in value]
    return value


def to_dict(value: Any) -> dict[str, Any] | Any:
    return _serialize(value)


def canonical_json(value: Any) -> str:
    return json.dumps(_serialize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MarketBar:
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    currency: str = "USD"
    timeframe: str = "1d"
    source: str = "unknown"
    is_synthetic: bool = False
    session: str = "regular"
    snapshot_id: str = ""


@dataclass(frozen=True)
class MarketSnapshot:
    snapshot_id: str
    as_of: datetime
    source: str
    bars: tuple[MarketBar, ...]

    def latest_by_symbol(self) -> dict[str, MarketBar]:
        latest: dict[str, MarketBar] = {}
        for bar in sorted(self.bars, key=lambda item: (item.symbol, item.timestamp)):
            latest[bar.symbol] = bar
        return latest

    def bars_by_symbol(self) -> dict[str, tuple[MarketBar, ...]]:
        result: dict[str, list[MarketBar]] = {}
        for bar in self.bars:
            result.setdefault(bar.symbol, []).append(bar)
        return {
            symbol: tuple(sorted(items, key=lambda item: item.timestamp))
            for symbol, items in result.items()
        }


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: Decimal
    average_price: Decimal
    market_price: Decimal
    currency: str = "USD"

    @property
    def market_value(self) -> Decimal:
        return self.quantity * self.market_price


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    as_of: datetime
    cash: Decimal
    equity: Decimal
    currency: str
    positions: tuple[Position, ...] = field(default_factory=tuple)
    status: str = "VERIFIED"
    source: str = "offline-fixture"

    @property
    def position_value(self) -> Decimal:
        return sum((position.market_value for position in self.positions), Decimal("0"))


@dataclass(frozen=True)
class StrategySignal:
    symbol: str
    direction: SignalDirection
    strength: Decimal
    reason_code: str
    input_start: datetime
    input_end: datetime
    strategy_id: str
    strategy_version: str
    invalidation_conditions: tuple[str, ...]
    reference_price: Decimal


@dataclass(frozen=True)
class TargetPosition:
    symbol: str
    current_quantity: Decimal
    target_quantity: Decimal
    target_notional: Decimal
    reason_code: str


@dataclass(frozen=True)
class ProposedOrder:
    order_id: str
    client_order_id: str
    symbol: str
    side: Direction
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None
    reference_price: Decimal
    notional: Decimal
    estimated_fee: Decimal
    estimated_slippage: Decimal
    pre_quantity: Decimal
    post_quantity: Decimal
    reason_code: str


@dataclass(frozen=True)
class RiskCheck:
    check_id: str
    name: str
    passed: bool
    severity: RiskSeverity
    reason_code: str
    message: str
    order_id: str | None = None


@dataclass(frozen=True)
class RiskDecision:
    decision_id: str
    allowed: bool
    checks: tuple[RiskCheck, ...]
    allowed_order_ids: tuple[str, ...]
    blocked_order_ids: tuple[str, ...]

    @property
    def blocking_checks(self) -> tuple[RiskCheck, ...]:
        return tuple(
            check
            for check in self.checks
            if not check.passed and check.severity == RiskSeverity.BLOCK
        )


@dataclass
class DailyPlan:
    plan_id: str
    account_id: str
    market_snapshot_id: str
    generated_at: datetime
    expires_at: datetime
    strategy_id: str
    strategy_version: str
    risk_config_version: str
    signals: tuple[StrategySignal, ...]
    targets: tuple[TargetPosition, ...]
    orders: tuple[ProposedOrder, ...]
    risk_decision: RiskDecision
    data_source: str
    data_as_of: datetime
    code_version: str
    plan_hash: str = ""


@dataclass
class DailyReport:
    report_id: str
    report_version: int
    status: ReportStatus
    generated_at: datetime
    expires_at: datetime
    plan: DailyPlan
    account: AccountSnapshot
    market: MarketSnapshot
    local_timezone: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Approval:
    approval_id: str
    report_id: str
    report_version: int
    plan_hash: str
    account_id: str
    strategy_id: str
    strategy_version: str
    risk_config_version: str
    approved_order_ids: tuple[str, ...]
    approved_at: datetime
    expires_at: datetime
    approver: str
    approval_scope: ApprovalScope
    revoked: bool = False
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class ExecutionRequest:
    execution_id: str
    report_id: str
    approval_id: str
    mode: str
    request_id: str
    requested_at: datetime
    idempotency_key: str


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    side: Direction
    quantity: Decimal
    price: Decimal
    fee: Decimal
    executed_at: datetime


@dataclass(frozen=True)
class BrokerOrder:
    order_id: str
    client_order_id: str
    symbol: str
    side: Direction
    quantity: Decimal
    status: BrokerOrderStatus
    submitted_at: datetime
    filled_quantity: Decimal
    remaining_quantity: Decimal
    fills: tuple[Fill, ...] = field(default_factory=tuple)
    rejection_reason: str | None = None


@dataclass(frozen=True)
class ReconciliationResult:
    ok: bool
    messages: tuple[str, ...]
    before_cash: Decimal
    after_cash: Decimal
    before_positions: tuple[Position, ...]
    after_positions: tuple[Position, ...]
    remaining_order_ids: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: str
    report_id: str
    request_id: str
    mode: str
    status: str
    started_at: datetime
    completed_at: datetime
    broker_orders: tuple[BrokerOrder, ...]
    fills: tuple[Fill, ...]
    reconciliation: ReconciliationResult
    idempotency_key: str
    error: str | None = None


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    timestamp: datetime
    event_type: str
    actor: str
    request_id: str
    report_id: str | None
    order_id: str | None
    before_state: str | None
    after_state: str | None
    reason_code: str
    input_summary: str
    result_summary: str


@dataclass(frozen=True)
class StrategyParameterSpec:
    name: str
    value_type: str
    default: str
    minimum: str | None = None
    maximum: str | None = None
    description: str = ""


@dataclass(frozen=True)
class StrategyManifest:
    strategy_id: str
    version: str
    display_name: str
    description: str
    strategy_kind: StrategyKind
    required_market_fields: tuple[str, ...]
    supported_timeframes: tuple[str, ...]
    warmup_bars: int
    parameter_schema: tuple[StrategyParameterSpec, ...]
    output_schema: dict[str, Any]
    risk_compatibility: str
    created_at: datetime
    source_hash: str
    status: StrategyStatus
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyVersion:
    strategy_id: str
    version: str
    source_hash: str
    status: StrategyStatus
    parent_version: str | None = None


@dataclass(frozen=True)
class StrategyDraft:
    manifest: StrategyManifest
    dsl: dict[str, Any]
    parameters: dict[str, Any]


@dataclass(frozen=True)
class StrategyValidationResult:
    valid: bool
    strategy_id: str
    version: str
    status: StrategyStatus
    source_hash: str
    errors: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    warnings: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class StrategyRunRequest:
    strategy_id: str
    version: str
    symbol: str
    timeframe: str
    snapshot_id: str
    start: datetime | None = None
    end: datetime | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class StrategyRunResult:
    run_id: str
    strategy_id: str
    version: str
    snapshot_id: str
    symbol: str
    timeframe: str
    status: str
    signals: tuple[dict[str, Any], ...]
    trace: tuple[dict[str, Any], ...]
    assumptions: tuple[str, ...]


@dataclass(frozen=True)
class BacktestRequest:
    strategy_id: str
    version: str
    symbol: str
    timeframe: str
    snapshot_id: str
    initial_cash: Decimal
    fee_bps: Decimal
    slippage_bps: Decimal
    max_position_notional: Decimal | None = None
    start: datetime | None = None
    end: datetime | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class BacktestResult:
    run_id: str
    strategy_id: str
    version: str
    snapshot_id: str
    symbol: str
    timeframe: str
    status: str
    metrics: dict[str, Any]
    equity_curve: tuple[dict[str, Any], ...]
    drawdown_curve: tuple[dict[str, Any], ...]
    trades: tuple[dict[str, Any], ...]
    assumptions: tuple[str, ...]
    formulas: dict[str, str]


def plan_payload(plan: DailyPlan) -> dict[str, Any]:
    payload = to_dict(plan)
    payload.pop("plan_hash", None)
    return payload


def compute_plan_hash(plan: DailyPlan) -> str:
    return canonical_hash(plan_payload(plan))


def with_plan_hash(plan: DailyPlan) -> DailyPlan:
    plan.plan_hash = compute_plan_hash(plan)
    return plan
