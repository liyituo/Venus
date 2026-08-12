from __future__ import annotations

from quant_agent.approval.service import ApprovalService
from quant_agent.audit.logger import AuditLogger
from quant_agent.brokers.paper import PaperBroker
from quant_agent.data.providers import FileDataProvider
from quant_agent.data.validation import validate_account, validate_market
from quant_agent.domain.enums import BrokerOrderStatus, ReportStatus
from quant_agent.domain.errors import (
    ApprovalError,
    ExecutionError,
    LiveBrokerDisabledError,
    RiskBlockedError,
)
from quant_agent.domain.models import ExecutionResult, canonical_hash, compute_plan_hash, to_dict
from quant_agent.domain.state_machine import transition
from quant_agent.infrastructure.clock import Clock
from quant_agent.infrastructure.config import DemoConfig
from quant_agent.infrastructure.store import SQLiteStore
from quant_agent.risk.engine import RiskEngine

from .reconciliation import reconcile


class ExecutionService:
    def __init__(
        self,
        store: SQLiteStore,
        provider: FileDataProvider,
        config: DemoConfig,
        clock: Clock,
        risk: RiskEngine,
        approvals: ApprovalService,
        audit: AuditLogger,
    ) -> None:
        self.store = store
        self.provider = provider
        self.config = config
        self.clock = clock
        self.risk = risk
        self.approvals = approvals
        self.audit = audit

    def execute(
        self, report_id: str, *, mode: str = "paper", request_id: str | None = None
    ) -> ExecutionResult:
        report = self.store.get_report(report_id)
        if report is None:
            raise ExecutionError(f"report not found: {report_id}")
        if mode.lower() != "paper":
            self.audit.record(
                "execution.blocked",
                request_id=request_id or "unknown",
                report_id=report_id,
                reason_code="LIVE_BROKER_DISABLED",
                result_summary="only paper mode is permitted",
            )
            raise LiveBrokerDisabledError(
                "only --paper execution is supported; live trading is disabled"
            )
        approval = self.store.get_latest_approval(report_id)
        if approval is None:
            raise ApprovalError("no approval exists")
        if compute_plan_hash(report.plan) != report.plan.plan_hash:
            raise ApprovalError("current report plan hash is inconsistent; execution is refused")
        key = f"{report.report_id}:{approval.approval_id}:{report.plan.plan_hash}"
        existing = self.store.get_execution_by_key(key)
        if existing is not None:
            self.audit.record(
                "execution.idempotent",
                request_id=request_id or existing.request_id,
                report_id=report_id,
                reason_code="IDEMPOTENT_REPLAY",
                result_summary=f"returned {existing.execution_id}",
            )
            return existing
        if self.store.kill_switch().get("enabled", False):
            self.audit.record(
                "execution.blocked",
                request_id=request_id or "unknown",
                report_id=report_id,
                reason_code="KILL_SWITCH_ON",
                result_summary="no broker call was made",
            )
            raise RiskBlockedError("kill switch is enabled")
        approval = self.approvals.validate(report, approval)
        account = self.provider.load_account()
        market = self.provider.load_market()
        validation_issues = validate_market(
            market,
            self.clock.now(),
            self.config.risk.max_data_age_seconds,
            self.config.portfolio.currency,
        ) + validate_account(
            account,
            self.clock.now(),
            self.config.portfolio.currency,
            self.config.risk.max_data_age_seconds,
        )
        orders = tuple(
            order for order in report.plan.orders if order.order_id in approval.approved_order_ids
        )
        decision = self.risk.evaluate(
            market,
            account,
            orders,
            self.clock.now(),
            kill_switch=False,
            validation_issues=validation_issues,
        )
        if set(decision.allowed_order_ids) != {order.order_id for order in orders}:
            self.audit.record(
                "execution.blocked",
                request_id=request_id or "unknown",
                report_id=report_id,
                reason_code="EXECUTION_RISK_BLOCKED",
                result_summary=str(to_dict(decision.blocking_checks)),
            )
            raise RiskBlockedError("execution-time risk check blocked one or more approved orders")
        execution_identity = {
            "report_id": report_id,
            "approval_id": approval.approval_id,
            "plan_hash": report.plan.plan_hash,
        }
        execution_id = f"exec_{canonical_hash(execution_identity)[:20]}"
        request = request_id or f"req_{execution_id}"
        started = self.clock.now()
        before = report.status
        report.status = transition(report.status, ReportStatus.EXECUTING)
        self.store.save_report(report)
        self.audit.record(
            "execution.started",
            request_id=request,
            report_id=report_id,
            before_state=before.value,
            after_state=report.status.value,
            reason_code="APPROVAL_VALID",
            input_summary=f"orders={','.join(approval.approved_order_ids)}",
        )
        broker = PaperBroker(
            self.clock,
            fee_bps=self.config.portfolio.fee_bps,
            default_fill_policy=self.config.paper_broker.default_fill_policy,
        )
        broker_orders = tuple(broker.submit(order) for order in orders)
        fills = tuple(fill for broker_order in broker_orders for fill in broker_order.fills)
        reconciliation, updated_account = reconcile(
            account, orders, broker_orders, fills, market, self.clock.now()
        )
        self.provider.save_account(updated_account)
        if not reconciliation.ok:
            final_status = ReportStatus.FAILED
            result_status = "FAILED"
            error = "reconciliation failed"
        elif any(order.status == BrokerOrderStatus.REJECTED for order in broker_orders):
            final_status = ReportStatus.FAILED
            result_status = "FAILED"
            error = "paper broker rejected one or more orders"
        elif reconciliation.remaining_order_ids or set(approval.approved_order_ids) != set(
            report.plan.risk_decision.allowed_order_ids
        ):
            final_status = ReportStatus.PARTIALLY_FILLED
            result_status = "PARTIALLY_FILLED"
            error = None
        else:
            final_status = ReportStatus.FILLED
            result_status = "FILLED"
            error = None
        completed = self.clock.now()
        execution = ExecutionResult(
            execution_id=execution_id,
            report_id=report_id,
            request_id=request,
            mode="paper",
            status=result_status,
            started_at=started,
            completed_at=completed,
            broker_orders=broker_orders,
            fills=fills,
            reconciliation=reconciliation,
            idempotency_key=key,
            error=error,
        )
        self.store.save_execution(execution)
        report.status = transition(report.status, final_status)
        self.store.save_report(report)
        self.audit.record(
            "execution.completed",
            request_id=request,
            report_id=report_id,
            before_state=ReportStatus.EXECUTING.value,
            after_state=report.status.value,
            reason_code="EXECUTION_RECONCILED" if reconciliation.ok else "RECONCILIATION_FAILED",
            result_summary=f"execution_id={execution_id}; status={result_status}",
        )
        return execution
