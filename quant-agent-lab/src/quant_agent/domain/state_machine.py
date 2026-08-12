from __future__ import annotations

from .enums import ReportStatus
from .errors import StateTransitionError

ALLOWED_TRANSITIONS: dict[ReportStatus, frozenset[ReportStatus]] = {
    ReportStatus.DRAFT: frozenset({ReportStatus.GENERATED, ReportStatus.RISK_BLOCKED}),
    ReportStatus.GENERATED: frozenset({ReportStatus.PENDING_APPROVAL, ReportStatus.RISK_BLOCKED}),
    ReportStatus.RISK_BLOCKED: frozenset(),
    ReportStatus.PENDING_APPROVAL: frozenset(
        {
            ReportStatus.APPROVED,
            ReportStatus.PARTIALLY_APPROVED,
            ReportStatus.REJECTED,
            ReportStatus.EXPIRED,
        }
    ),
    ReportStatus.APPROVED: frozenset(
        {ReportStatus.EXECUTING, ReportStatus.EXPIRED, ReportStatus.CANCELLED, ReportStatus.FAILED}
    ),
    ReportStatus.PARTIALLY_APPROVED: frozenset(
        {ReportStatus.EXECUTING, ReportStatus.EXPIRED, ReportStatus.CANCELLED, ReportStatus.FAILED}
    ),
    ReportStatus.REJECTED: frozenset(),
    ReportStatus.EXPIRED: frozenset(),
    ReportStatus.EXECUTING: frozenset(
        {
            ReportStatus.PARTIALLY_FILLED,
            ReportStatus.FILLED,
            ReportStatus.FAILED,
            ReportStatus.CANCELLED,
        }
    ),
    ReportStatus.PARTIALLY_FILLED: frozenset(
        {ReportStatus.FILLED, ReportStatus.FAILED, ReportStatus.CANCELLED}
    ),
    ReportStatus.FILLED: frozenset(),
    ReportStatus.FAILED: frozenset(),
    ReportStatus.CANCELLED: frozenset(),
}


def transition(current: ReportStatus, target: ReportStatus) -> ReportStatus:
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise StateTransitionError(f"illegal report transition: {current.value} -> {target.value}")
    return target
