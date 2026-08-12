from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from quant_agent.audit.logger import AuditLogger
from quant_agent.domain.enums import ApprovalScope, ReportStatus
from quant_agent.domain.errors import ApprovalError
from quant_agent.domain.models import Approval, DailyReport, canonical_hash, compute_plan_hash
from quant_agent.domain.state_machine import transition
from quant_agent.infrastructure.clock import Clock
from quant_agent.infrastructure.config import DemoConfig
from quant_agent.infrastructure.store import SQLiteStore


class ApprovalService:
    def __init__(
        self, store: SQLiteStore, config: DemoConfig, clock: Clock, audit: AuditLogger
    ) -> None:
        self.store = store
        self.config = config
        self.clock = clock
        self.audit = audit

    def _report(self, report_id: str) -> DailyReport:
        report = self.store.get_report(report_id)
        if report is None:
            raise ApprovalError(f"report not found: {report_id}")
        return report

    def _create(
        self,
        report: DailyReport,
        order_ids: tuple[str, ...],
        approver: str,
        scope: ApprovalScope,
        request_id: str | None = None,
    ) -> Approval:
        if report.status != ReportStatus.PENDING_APPROVAL:
            raise ApprovalError(f"report is not awaiting approval: {report.status.value}")
        computed_hash = compute_plan_hash(report.plan)
        if computed_hash != report.plan.plan_hash:
            raise ApprovalError("plan hash is internally inconsistent; generate a new report")
        eligible = set(report.plan.risk_decision.allowed_order_ids)
        requested = tuple(sorted(set(order_ids)))
        if not requested or not set(requested).issubset(eligible):
            raise ApprovalError("approval may contain only non-empty, risk-allowed order IDs")
        now = self.clock.now()
        expires_at = min(
            report.expires_at, now + timedelta(seconds=self.config.portfolio.approval_ttl_seconds)
        )
        approval_identity = {
            "report_id": report.report_id,
            "plan_hash": computed_hash,
            "order_ids": requested,
            "approver": approver,
            "scope": scope.value,
        }
        approval_id = f"appr_{canonical_hash(approval_identity)[:20]}"
        approval = Approval(
            approval_id=approval_id,
            report_id=report.report_id,
            report_version=report.report_version,
            plan_hash=computed_hash,
            account_id=report.plan.account_id,
            strategy_id=report.plan.strategy_id,
            strategy_version=report.plan.strategy_version,
            risk_config_version=report.plan.risk_config_version,
            approved_order_ids=requested,
            approved_at=now,
            expires_at=expires_at,
            approver=approver,
            approval_scope=scope,
        )
        self.store.save_approval(approval)
        target_status = (
            ReportStatus.APPROVED if set(requested) == eligible else ReportStatus.PARTIALLY_APPROVED
        )
        before = report.status
        report.status = transition(report.status, target_status)
        self.store.save_report(report)
        self.audit.record(
            "approval.created",
            actor=approver,
            request_id=request_id or approval.approval_id,
            report_id=report.report_id,
            before_state=before.value,
            after_state=report.status.value,
            reason_code="APPROVAL_BOUND_TO_PLAN",
            input_summary=f"scope={scope.value}; orders={','.join(requested)}",
            result_summary=f"approval_id={approval.approval_id}; plan_hash={computed_hash}",
        )
        return approval

    def approve_all(
        self, report_id: str, approver: str = "user", request_id: str | None = None
    ) -> Approval:
        report = self._report(report_id)
        return self._create(
            report,
            report.plan.risk_decision.allowed_order_ids,
            approver,
            ApprovalScope.ALL,
            request_id,
        )

    def approve_partial(
        self,
        report_id: str,
        order_ids: tuple[str, ...],
        approver: str = "user",
        request_id: str | None = None,
    ) -> Approval:
        report = self._report(report_id)
        return self._create(report, order_ids, approver, ApprovalScope.PARTIAL, request_id)

    def reject(
        self, report_id: str, approver: str = "user", request_id: str | None = None
    ) -> DailyReport:
        report = self._report(report_id)
        if report.status != ReportStatus.PENDING_APPROVAL:
            raise ApprovalError(f"report is not awaiting rejection: {report.status.value}")
        before = report.status
        report.status = transition(report.status, ReportStatus.REJECTED)
        self.store.save_report(report)
        self.audit.record(
            "approval.rejected",
            actor=approver,
            request_id=request_id or f"reject:{report_id}",
            report_id=report_id,
            before_state=before.value,
            after_state=report.status.value,
            reason_code="USER_REJECTED",
            result_summary="no execution is permitted",
        )
        return report

    def revoke(self, report_id: str, approver: str = "user") -> Approval:
        report = self._report(report_id)
        approval = self.store.get_latest_approval(report_id)
        if approval is None:
            raise ApprovalError("no approval exists")
        if approval.revoked:
            return approval
        revoked = replace(approval, revoked=True, revoked_at=self.clock.now())
        self.store.save_approval(revoked)
        if report.status in {ReportStatus.APPROVED, ReportStatus.PARTIALLY_APPROVED}:
            before = report.status
            report.status = transition(report.status, ReportStatus.CANCELLED)
            self.store.save_report(report)
            self.audit.record(
                "approval.revoked",
                actor=approver,
                report_id=report_id,
                before_state=before.value,
                after_state=report.status.value,
                reason_code="APPROVAL_REVOKED",
                result_summary="execution is cancelled",
            )
        return revoked

    def validate(self, report: DailyReport, approval: Approval | None = None) -> Approval:
        approval = approval or self.store.get_latest_approval(report.report_id)
        if approval is None:
            raise ApprovalError("no approval exists")
        now = self.clock.now()
        if approval.revoked:
            raise ApprovalError("approval has been revoked")
        if now >= approval.expires_at:
            before = report.status
            if report.status in {
                ReportStatus.PENDING_APPROVAL,
                ReportStatus.APPROVED,
                ReportStatus.PARTIALLY_APPROVED,
            }:
                report.status = transition(report.status, ReportStatus.EXPIRED)
                self.store.save_report(report)
            self.audit.record(
                "approval.expired",
                report_id=report.report_id,
                before_state=before.value,
                after_state=report.status.value,
                reason_code="APPROVAL_EXPIRED",
            )
            raise ApprovalError("approval has expired")
        computed_hash = compute_plan_hash(report.plan)
        if any(
            (
                approval.report_id != report.report_id,
                approval.report_version != report.report_version,
                approval.plan_hash != computed_hash,
                report.plan.plan_hash != computed_hash,
                approval.account_id != report.plan.account_id,
                approval.strategy_id != report.plan.strategy_id,
                approval.strategy_version != report.plan.strategy_version,
                approval.risk_config_version != report.plan.risk_config_version,
            )
        ):
            raise ApprovalError("approval binding does not match the current report plan")
        order_ids = {order.order_id for order in report.plan.orders}
        if not set(approval.approved_order_ids).issubset(order_ids):
            raise ApprovalError("approval references an unknown order")
        if not set(approval.approved_order_ids).issubset(
            set(report.plan.risk_decision.allowed_order_ids)
        ):
            raise ApprovalError("approval references a risk-blocked order")
        if report.status not in {ReportStatus.APPROVED, ReportStatus.PARTIALLY_APPROVED}:
            raise ApprovalError(f"report cannot execute from state {report.status.value}")
        return approval
