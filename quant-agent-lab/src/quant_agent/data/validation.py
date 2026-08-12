from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from quant_agent.domain.models import AccountSnapshot, MarketSnapshot


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    blocking: bool = True


def validate_market(
    snapshot: MarketSnapshot, now: datetime, max_age_seconds: int, currency: str
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    snapshot_time_valid = (
        snapshot.as_of.tzinfo is not None and snapshot.as_of.utcoffset() is not None
    )
    if not snapshot_time_valid:
        issues.append(
            ValidationIssue("DATA_TIMEZONE_MISSING", "market snapshot time has no timezone")
        )
    else:
        age = (now - snapshot.as_of).total_seconds()
        if age < 0:
            issues.append(
                ValidationIssue(
                    "DATA_TIME_IN_FUTURE", "market snapshot is later than the evaluation clock"
                )
            )
        elif age > max_age_seconds:
            issues.append(
                ValidationIssue(
                    "DATA_STALE", f"market snapshot is {age:.0f}s old; limit is {max_age_seconds}s"
                )
            )
    if not snapshot.bars:
        issues.append(ValidationIssue("DATA_EMPTY", "market snapshot contains no bars"))
    seen: set[tuple[str, str]] = set()
    for bar in snapshot.bars:
        if bar.timestamp.tzinfo is None or bar.timestamp.utcoffset() is None:
            issues.append(
                ValidationIssue(
                    "DATA_BAR_TIMEZONE_MISSING",
                    f"{bar.symbol} bar timestamp has no timezone",
                )
            )
        key = (bar.symbol, bar.timestamp.isoformat())
        if key in seen:
            issues.append(
                ValidationIssue(
                    "DATA_DUPLICATE_BAR", f"duplicate bar {bar.symbol} {bar.timestamp.isoformat()}"
                )
            )
        seen.add(key)
        if bar.currency != currency:
            issues.append(
                ValidationIssue(
                    "DATA_CURRENCY_MISMATCH",
                    f"{bar.symbol} uses {bar.currency}, expected {currency}",
                )
            )
        if any(price <= 0 for price in (bar.open, bar.high, bar.low, bar.close)):
            issues.append(
                ValidationIssue("DATA_INVALID_PRICE", f"{bar.symbol} contains a non-positive price")
            )
        if bar.high < bar.low or bar.high < bar.close or bar.low > bar.close:
            issues.append(
                ValidationIssue("DATA_OHLC_INVALID", f"{bar.symbol} has inconsistent OHLC values")
            )
        if bar.volume < 0:
            issues.append(
                ValidationIssue("DATA_INVALID_VOLUME", f"{bar.symbol} contains a negative volume")
            )
    for symbol, bars in snapshot.bars_by_symbol().items():
        ordered = tuple(sorted(bars, key=lambda item: item.timestamp))
        if tuple(bars) != ordered:
            issues.append(
                ValidationIssue(
                    "DATA_BAR_ORDER_INVALID",
                    f"{symbol} bars are not strictly time ordered",
                )
            )
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.timestamp <= previous.timestamp:
                issues.append(
                    ValidationIssue(
                        "DATA_DUPLICATE_OR_REVERSED_BAR",
                        f"{symbol} bars are not strictly increasing",
                    )
                )
                break
    return tuple(issues)


def validate_account(
    account: AccountSnapshot,
    now: datetime,
    currency: str,
    max_age_seconds: int | None = None,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    account_time_valid = account.as_of.tzinfo is not None and account.as_of.utcoffset() is not None
    if not account_time_valid:
        issues.append(
            ValidationIssue("ACCOUNT_TIMEZONE_MISSING", "account snapshot time has no timezone")
        )
    elif max_age_seconds is not None:
        age = (now - account.as_of).total_seconds()
        if age < 0:
            issues.append(
                ValidationIssue(
                    "ACCOUNT_TIME_IN_FUTURE", "account snapshot is later than the evaluation clock"
                )
            )
        elif age > max_age_seconds:
            issues.append(
                ValidationIssue(
                    "ACCOUNT_STALE",
                    f"account snapshot is {age:.0f}s old; limit is {max_age_seconds}s",
                )
            )
    if account.currency != currency:
        issues.append(
            ValidationIssue(
                "ACCOUNT_CURRENCY_MISMATCH", f"account uses {account.currency}, expected {currency}"
            )
        )
    if account.status != "VERIFIED":
        issues.append(ValidationIssue("ACCOUNT_UNVERIFIED", f"account status is {account.status}"))
    if account.cash < 0 or account.equity < 0:
        issues.append(
            ValidationIssue("ACCOUNT_NEGATIVE_BALANCE", "account cash or equity is negative")
        )
    if abs(account.equity - (account.cash + account.position_value)) > Decimal("0.01"):
        issues.append(
            ValidationIssue("ACCOUNT_MISMATCH", "equity does not equal cash plus marked positions")
        )
    for position in account.positions:
        if position.quantity < 0 or position.market_price <= 0:
            issues.append(
                ValidationIssue(
                    "ACCOUNT_INVALID_POSITION", f"invalid position for {position.symbol}"
                )
            )
        if position.currency != currency:
            issues.append(
                ValidationIssue(
                    "ACCOUNT_POSITION_CURRENCY",
                    f"position {position.symbol} uses {position.currency}",
                )
            )
    return tuple(issues)
