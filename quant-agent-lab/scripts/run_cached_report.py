"""用已缓存的真实行情生成报告（跳过重拉 300 只）。"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_agent.data.providers import FileDataProvider
from quant_agent.data.validation import validate_account, validate_market
from quant_agent.domain.enums import ReportStatus, SignalDirection
from quant_agent.domain.models import DailyPlan, DailyReport, canonical_hash, with_plan_hash
from quant_agent.domain.state_machine import transition
from quant_agent.orchestration.service import ApplicationService
from quant_agent.reporting.renderer import write_report


def main() -> int:
    svc = ApplicationService()
    now = svc.clock.now()
    report_date = now.date()
    expires_at = now + timedelta(seconds=svc.config.portfolio.approval_ttl_seconds)
    market = FileDataProvider(svc.paths.data_dir).load_market()
    account = svc.provider.load_account()
    issues = validate_market(
        market, now, svc.config.risk.max_data_age_seconds, svc.config.portfolio.currency
    )
    issues += validate_account(
        account, now, svc.config.portfolio.currency, svc.config.risk.max_data_age_seconds
    )
    if issues:
        signals = ()
        targets = ()
        orders = ()
        decision = svc._blocked_decision(issues)
    else:
        signals = svc.strategy.generate(market)
        targets, orders = svc.planner.build(signals, account, market)
        decision = svc.risk.evaluate(
            market,
            account,
            orders,
            now,
            kill_switch=bool(svc.store.kill_switch().get("enabled", False)),
            validation_issues=(),
        )
    plan_identity = {
        "date": report_date.isoformat(),
        "account": account.account_id,
        "snapshot": market.snapshot_id,
        "now": now,
    }
    plan = with_plan_hash(
        DailyPlan(
            plan_id=f"plan_{canonical_hash(plan_identity)[:20]}",
            account_id=account.account_id,
            market_snapshot_id=market.snapshot_id,
            generated_at=now,
            expires_at=expires_at,
            strategy_id=svc.config.strategy.strategy_id,
            strategy_version=svc.config.strategy.version,
            risk_config_version=svc.config.risk.version,
            signals=signals,
            targets=targets,
            orders=orders,
            risk_decision=decision,
            data_source=market.source,
            data_as_of=market.as_of,
            code_version="quant-agent-lab@0.1.0",
        )
    )
    report_id = f"rpt_{report_date.isoformat()}_{plan.plan_hash[:16]}"
    existing = svc.store.get_report(report_id)
    if existing is not None:
        report = existing
    else:
        status = (
            ReportStatus.PENDING_APPROVAL
            if decision.allowed_order_ids
            else ReportStatus.RISK_BLOCKED
        )
        report = DailyReport(
            report_id=report_id,
            report_version=1,
            status=ReportStatus.DRAFT,
            generated_at=now,
            expires_at=expires_at,
            plan=plan,
            account=account,
            market=market,
            local_timezone=svc.config.local_timezone,
            warnings=tuple(i.message for i in issues)
            + tuple(c.message for c in decision.checks if not c.passed),
        )
        report.status = transition(report.status, ReportStatus.GENERATED)
        report.status = transition(report.status, status)
        svc.store.save_report(report)
        write_report(report, svc.paths.reports_dir, svc.narrative)
    print(
        json.dumps(
            {
                "report_id": report.report_id,
                "status": report.status.value,
                "orders": len(report.plan.orders),
                "allowed_orders": len(report.plan.risk_decision.allowed_order_ids),
                "blocked_orders": len(report.plan.risk_decision.blocked_order_ids),
                "buy_signals": sum(
                    1 for s in report.plan.signals if s.direction == SignalDirection.BUY
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
