from __future__ import annotations

from typing import Protocol

from quant_agent.domain.models import DailyReport


class NarrativeProvider(Protocol):
    def summarize(self, report: DailyReport) -> str: ...


class DeterministicNarrativeProvider:
    """Template-safe explanation layer; it does not invent news or probabilities."""

    def summarize(self, report: DailyReport) -> str:
        allowed = len(report.plan.risk_decision.allowed_order_ids)
        blocked = len(report.plan.risk_decision.blocked_order_ids)
        return (
            f"The versioned {report.plan.strategy_id} strategy produced {len(report.plan.signals)} signals. "
            f"{allowed} candidate orders passed the current deterministic risk checks and {blocked} were blocked. "
            "This is an engineering demonstration using an offline data snapshot."
        )
