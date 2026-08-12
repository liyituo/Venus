from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from quant_agent.data.validation import ValidationIssue
from quant_agent.domain.enums import Direction, RiskSeverity
from quant_agent.domain.models import (
    AccountSnapshot,
    MarketSnapshot,
    ProposedOrder,
    RiskCheck,
    RiskDecision,
    canonical_hash,
)
from quant_agent.infrastructure.config import RiskConfig


class RiskEngine:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    @staticmethod
    def _check(
        name: str,
        passed: bool,
        reason_code: str,
        message: str,
        order_id: str | None = None,
    ) -> RiskCheck:
        check_identity = {"name": name, "order_id": order_id}
        return RiskCheck(
            check_id=f"chk_{canonical_hash(check_identity)[:12]}",
            name=name,
            passed=passed,
            severity=RiskSeverity.INFO if passed else RiskSeverity.BLOCK,
            reason_code=reason_code,
            message=message,
            order_id=order_id,
        )

    def evaluate(
        self,
        market: MarketSnapshot,
        account: AccountSnapshot,
        orders: tuple[ProposedOrder, ...],
        now: datetime,
        *,
        kill_switch: bool = False,
        existing_client_order_ids: tuple[str, ...] = (),
        validation_issues: tuple[ValidationIssue, ...] = (),
    ) -> RiskDecision:
        checks: list[RiskCheck] = []
        market_time_valid = market.as_of.tzinfo is not None and market.as_of.utcoffset() is not None
        if market_time_valid:
            age = (now - market.as_of).total_seconds()
            freshness_passed = 0 <= age <= self.config.max_data_age_seconds
            freshness_reason = "DATA_FRESHNESS_OK" if freshness_passed else "DATA_STALE"
            freshness_message = (
                f"market snapshot age is {age:.0f}s; limit is {self.config.max_data_age_seconds}s"
            )
        else:
            freshness_passed = False
            freshness_reason = "DATA_TIMEZONE_MISSING"
            freshness_message = "market snapshot time has no timezone"
        checks.append(
            self._check(
                "data_freshness",
                freshness_passed,
                freshness_reason,
                freshness_message,
            )
        )
        checks.append(
            self._check(
                "account_status",
                account.status == "VERIFIED",
                "ACCOUNT_VERIFIED" if account.status == "VERIFIED" else "ACCOUNT_UNVERIFIED",
                f"account status is {account.status}",
            )
        )
        balance_ok = account.cash >= 0 and account.equity >= 0
        checks.append(
            self._check(
                "account_balance",
                balance_ok,
                "ACCOUNT_BALANCE_OK" if balance_ok else "ACCOUNT_BALANCE_INVALID",
                "cash and equity are non-negative" if balance_ok else "cash or equity is negative",
            )
        )
        equity_ok = abs(account.equity - (account.cash + account.position_value)) <= Decimal("0.01")
        checks.append(
            self._check(
                "account_consistency",
                equity_ok,
                "ACCOUNT_CONSISTENT" if equity_ok else "ACCOUNT_MISMATCH",
                "equity matches cash plus marked positions"
                if equity_ok
                else "equity does not match cash plus positions",
            )
        )
        checks.append(
            self._check(
                "order_count",
                len(orders) <= self.config.max_orders,
                "ORDER_COUNT_OK" if len(orders) <= self.config.max_orders else "ORDER_COUNT_LIMIT",
                f"{len(orders)} orders proposed; limit is {self.config.max_orders}",
            )
        )
        checks.append(
            self._check(
                "kill_switch",
                not kill_switch,
                "KILL_SWITCH_OFF" if not kill_switch else "KILL_SWITCH_ON",
                "kill switch is off" if not kill_switch else "kill switch is enabled",
            )
        )
        for issue in validation_issues:
            checks.append(self._check("input_validation", False, issue.code, issue.message))

        latest = market.latest_by_symbol()
        positions = {position.symbol: position.quantity for position in account.positions}
        values = {position.symbol: position.market_value for position in account.positions}
        buy_cash = Decimal("0")
        turnover = Decimal("0")
        order_blocked: dict[str, bool] = {}
        for order in orders:
            blocked = False
            price = order.limit_price or order.reference_price
            checks_for_order = [
                self._check(
                    "order_notional",
                    order.notional <= self.config.max_order_notional,
                    "ORDER_NOTIONAL_OK"
                    if order.notional <= self.config.max_order_notional
                    else "ORDER_NOTIONAL_LIMIT",
                    f"order notional is {order.notional}; limit is {self.config.max_order_notional}",
                    order.order_id,
                ),
                self._check(
                    "order_quantity",
                    order.quantity > 0,
                    "ORDER_QUANTITY_OK" if order.quantity > 0 else "ORDER_QUANTITY_INVALID",
                    "quantity is positive" if order.quantity > 0 else "quantity must be positive",
                    order.order_id,
                ),
                self._check(
                    "order_price",
                    price > 0 and order.reference_price > 0,
                    "ORDER_PRICE_OK"
                    if price > 0 and order.reference_price > 0
                    else "ORDER_PRICE_INVALID",
                    "order price is positive"
                    if price > 0 and order.reference_price > 0
                    else "order price must be positive",
                    order.order_id,
                ),
                self._check(
                    "duplicate_order",
                    order.client_order_id not in existing_client_order_ids,
                    "ORDER_IDEMPOTENCY_OK"
                    if order.client_order_id not in existing_client_order_ids
                    else "DUPLICATE_ORDER",
                    "client order id is new"
                    if order.client_order_id not in existing_client_order_ids
                    else "client order id already exists",
                    order.order_id,
                ),
                self._check(
                    "pre_position",
                    positions.get(order.symbol, Decimal("0")) == order.pre_quantity,
                    "POSITION_MATCH"
                    if positions.get(order.symbol, Decimal("0")) == order.pre_quantity
                    else "ACCOUNT_POSITION_CHANGED",
                    "pre-trade position matches account"
                    if positions.get(order.symbol, Decimal("0")) == order.pre_quantity
                    else "pre-trade position changed",
                    order.order_id,
                ),
            ]
            bar = latest.get(order.symbol)
            if bar is not None:
                deviation = (
                    abs(price - bar.close) / bar.close * Decimal("10000")
                    if bar.close > 0
                    else Decimal("999999")
                )
                checks_for_order.append(
                    self._check(
                        "price_deviation",
                        deviation <= self.config.max_price_deviation_bps,
                        "PRICE_DEVIATION_OK"
                        if deviation <= self.config.max_price_deviation_bps
                        else "PRICE_DEVIATION_LIMIT",
                        f"price deviation is {deviation:.2f} bps; limit is {self.config.max_price_deviation_bps} bps",
                        order.order_id,
                    )
                )
            projected_value = values.get(order.symbol, Decimal("0"))
            projected_value += order.notional if order.side == Direction.BUY else -order.notional
            checks_for_order.append(
                self._check(
                    "symbol_exposure",
                    Decimal("0") <= projected_value <= self.config.max_symbol_notional,
                    "SYMBOL_EXPOSURE_OK"
                    if Decimal("0") <= projected_value <= self.config.max_symbol_notional
                    else "SYMBOL_EXPOSURE_LIMIT",
                    f"projected {order.symbol} exposure is {projected_value}; limit is {self.config.max_symbol_notional}",
                    order.order_id,
                )
            )
            for check in checks_for_order:
                checks.append(check)
                blocked = blocked or (not check.passed and check.severity == RiskSeverity.BLOCK)
            order_blocked[order.order_id] = blocked
            turnover += order.notional
            if order.side == Direction.BUY:
                buy_cash += order.notional + order.estimated_fee

        projected_total = sum(values.values(), Decimal("0")) + sum(
            (
                order.notional if order.side == Direction.BUY else -order.notional
                for order in orders
            ),
            Decimal("0"),
        )
        global_checks = [
            self._check(
                "cash_buffer",
                account.cash - buy_cash >= self.config.min_cash_buffer,
                "CASH_BUFFER_OK"
                if account.cash - buy_cash >= self.config.min_cash_buffer
                else "CASH_BUFFER_LIMIT",
                f"cash after buys is {account.cash - buy_cash}; required buffer is {self.config.min_cash_buffer}",
            ),
            self._check(
                "total_exposure",
                projected_total <= self.config.max_total_exposure,
                "TOTAL_EXPOSURE_OK"
                if projected_total <= self.config.max_total_exposure
                else "TOTAL_EXPOSURE_LIMIT",
                f"projected total exposure is {projected_total}; limit is {self.config.max_total_exposure}",
            ),
            self._check(
                "daily_turnover",
                turnover <= self.config.max_daily_turnover,
                "DAILY_TURNOVER_OK"
                if turnover <= self.config.max_daily_turnover
                else "DAILY_TURNOVER_LIMIT",
                f"planned turnover is {turnover}; limit is {self.config.max_daily_turnover}",
            ),
        ]
        checks.extend(global_checks)
        global_blocked = any(
            not check.passed and check.severity == RiskSeverity.BLOCK
            for check in checks
            if check.order_id is None
        )
        allowed_ids = tuple(
            order.order_id
            for order in orders
            if not global_blocked and not order_blocked.get(order.order_id, True)
        )
        blocked_ids = tuple(order.order_id for order in orders if order.order_id not in allowed_ids)
        decision_identity = {
            "checks": checks,
            "allowed": bool(allowed_ids),
            "allowed_order_ids": allowed_ids,
            "blocked_order_ids": blocked_ids,
        }
        decision_id = f"risk_{canonical_hash(decision_identity)[:16]}"
        decision = RiskDecision(
            decision_id=decision_id,
            allowed=bool(allowed_ids),
            checks=tuple(checks),
            allowed_order_ids=allowed_ids,
            blocked_order_ids=blocked_ids,
        )
        return decision
