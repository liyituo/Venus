from __future__ import annotations

import json
from datetime import UTC, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_agent.domain.models import DailyReport, to_dict

from .narrative import NarrativeProvider


def _local_time(value, zone_name: str) -> str:
    try:
        zone: tzinfo = ZoneInfo(zone_name)
    except Exception:
        zone = UTC
    return value.astimezone(zone).isoformat()


def render_markdown(report: DailyReport, narrative: NarrativeProvider) -> str:
    plan = report.plan
    decision = plan.risk_decision
    lines = [
        f"# Daily Quantitative Paper-Trading Report: `{report.report_id}`",
        "",
        "> **Engineering demonstration only; not investment advice.** Simulated results do not represent live trading.",
        "",
        "## Report metadata",
        "",
        f"- Status: `{report.status.value}`",
        f"- Report version: `{report.report_version}`",
        f"- Generated (UTC): `{report.generated_at.isoformat()}`",
        f"- Generated ({report.local_timezone}): `{_local_time(report.generated_at, report.local_timezone)}`",
        f"- Expires (UTC): `{report.expires_at.isoformat()}`",
        f"- Data source: `{plan.data_source}`",
        f"- Data snapshot: `{plan.market_snapshot_id}`",
        f"- Data as of (UTC): `{plan.data_as_of.isoformat()}`",
        f"- Strategy: `{plan.strategy_id}@{plan.strategy_version}`",
        f"- Risk configuration: `{plan.risk_config_version}`",
        f"- Plan hash: `{plan.plan_hash}`",
        "",
        "## Deterministic summary",
        "",
        narrative.summarize(report),
        "",
        "## Account snapshot",
        "",
        f"- Account: `{report.account.account_id}`",
        f"- Cash: `{report.account.cash}` {report.account.currency}",
        f"- Equity: `{report.account.equity}` {report.account.currency}",
        "",
        "| Symbol | Quantity | Average price | Market price | Market value |",
        "|---|---:|---:|---:|---:|",
    ]
    for position in report.account.positions:
        lines.append(
            f"| {position.symbol} | {position.quantity} | {position.average_price} | {position.market_price} | {position.market_value} |"
        )
    lines.extend(
        ["", "## Signals", "", "| Symbol | Direction | Strength | Reason |", "|---|---|---:|---|"]
    )
    for signal in plan.signals:
        lines.append(
            f"| {signal.symbol} | {signal.direction.value} | {signal.strength} | {signal.reason_code} |"
        )
    lines.extend(
        [
            "",
            "## Candidate orders",
            "",
            "| Order ID | Side | Symbol | Quantity | Reference price | Notional | Reason |",
            "|---|---|---|---:|---:|---:|---|",
        ]
    )
    if not plan.orders:
        lines.append("| — | — | — | — | — | — | No candidate orders |")
    for order in plan.orders:
        lines.append(
            f"| `{order.order_id}` | {order.side.value} | {order.symbol} | {order.quantity} | {order.reference_price} | {order.notional} | {order.reason_code} |"
        )
    lines.extend(
        [
            "",
            "## Risk checks",
            "",
            "| Check | Result | Severity | Reason code | Message |",
            "|---|---|---|---|---|",
        ]
    )
    for check in decision.checks:
        result = "PASS" if check.passed else "FAIL"
        lines.append(
            f"| {check.name} | {result} | {check.severity.value} | `{check.reason_code}` | {check.message} |"
        )
    lines.extend(
        [
            "",
            "## Approval operations",
            "",
            "1. Review the JSON and Markdown report and verify the plan hash.",
            "2. Approve all eligible order IDs or submit a partial approval by exact `order_id`.",
            "3. Reject or let the approval expire when the plan is not accepted.",
            "4. Any order/account/config/report change invalidates the approval and requires a new plan.",
            "",
        ]
    )
    if report.warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report.warnings)
        lines.append("")
    return "\n".join(lines) + "\n"


def write_report(
    report: DailyReport, reports_dir: Path, narrative: NarrativeProvider
) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / f"{report.report_id}.json"
    markdown_path = reports_dir / f"{report.report_id}.md"
    json_path.write_text(
        json.dumps(to_dict(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(render_markdown(report, narrative), encoding="utf-8")
    return json_path, markdown_path
