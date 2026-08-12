from __future__ import annotations

from enum import StrEnum


class SignalDirection(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Direction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class RiskSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCK = "BLOCK"


class ReportStatus(StrEnum):
    DRAFT = "DRAFT"
    GENERATED = "GENERATED"
    RISK_BLOCKED = "RISK_BLOCKED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    PARTIALLY_APPROVED = "PARTIALLY_APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTING = "EXECUTING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ApprovalScope(StrEnum):
    ALL = "ALL"
    PARTIAL = "PARTIAL"
    REJECT = "REJECT"


class BrokerOrderStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


class StrategyStatus(StrEnum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    BACKTESTED = "BACKTESTED"
    PAPER_CANDIDATE = "PAPER_CANDIDATE"
    PAPER_ENABLED = "PAPER_ENABLED"
    DISABLED = "DISABLED"
    ARCHIVED = "ARCHIVED"


class StrategyKind(StrEnum):
    BUILTIN = "BUILTIN"
    DECLARATIVE = "DECLARATIVE"
    PYTHON = "PYTHON"
