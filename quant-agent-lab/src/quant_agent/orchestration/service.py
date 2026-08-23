from __future__ import annotations

from datetime import date, timedelta

from quant_agent.approval.service import ApprovalService
from quant_agent.audit.logger import AuditLogger
from quant_agent.data.providers import FileDataProvider, seed_demo_data
from quant_agent.data.validation import ValidationIssue, validate_account, validate_market
from quant_agent.domain.enums import ReportStatus, RiskSeverity
from quant_agent.domain.errors import DataValidationError
from quant_agent.domain.models import (
    DailyPlan,
    DailyReport,
    ProposedOrder,
    RiskCheck,
    RiskDecision,
    StrategySignal,
    TargetPosition,
    canonical_hash,
    to_dict,
    with_plan_hash,
)
from quant_agent.domain.state_machine import transition
from quant_agent.execution.service import ExecutionService
from quant_agent.infrastructure.clock import Clock, SystemClock
from quant_agent.infrastructure.config import DemoConfig, load_demo_config
from quant_agent.infrastructure.paths import ProjectPaths
from quant_agent.infrastructure.store import SQLiteStore
from quant_agent.llm.client import LlmClient
from quant_agent.llm.rag_client import RagClient
from quant_agent.portfolio.planner import PortfolioPlanner
from quant_agent.reporting.narrative import DeterministicNarrativeProvider
from quant_agent.reporting.renderer import write_report
from quant_agent.research.service import ResearchService
from quant_agent.risk.engine import RiskEngine
from quant_agent.strategies.base import Strategy
from quant_agent.strategies.llm_fundamental import LlmFundamentalStrategy
from quant_agent.strategies.moving_average import MovingAverageStrategy
from quant_agent.strategies.tiny_moe_ranker import TinyMoeRankerStrategy


class ApplicationService:
    """Single application service shared by CLI and API."""

    def __init__(
        self,
        *,
        paths: ProjectPaths | None = None,
        config: DemoConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.paths = paths or ProjectPaths.default()
        self.paths.ensure()
        self.config = config or load_demo_config(self.paths.config_dir)
        self.clock = clock or SystemClock()
        self.store = SQLiteStore(self.paths.state_db, self.paths.audit_log)
        self.store.init()
        if self.store.get_value("kill_switch") is None:
            self.store.set_kill_switch(
                self.config.risk.kill_switch_default, "configured default", "system"
            )
        # 行情源：file（默认）/ simulated（离线模拟市场，每日自动推进）/
        # live（yfinance+akshare 自动拉取，失败回退缓存）
        self.provider: FileDataProvider
        if self.config.market_data.source == "live":
            from quant_agent.data.live_provider import LiveMarketDataProvider

            self.provider = LiveMarketDataProvider(
                self.paths.data_dir,
                market=self.config.market_data.market,
                symbols=self.config.market_data.symbols,
            )
        elif self.config.market_data.source == "simulated":
            from quant_agent.data.simulated_provider import SimulatedMarketProvider

            self.provider = SimulatedMarketProvider(
                self.paths.data_dir, symbols=self.config.market_data.symbols
            )
        else:
            self.provider = FileDataProvider(self.paths.data_dir)
        self.audit = AuditLogger(self.store, self.clock)
        # 策略工厂：llm-fundamental / tiny-moe-ranker / 默认均线演示
        self.strategy: Strategy
        if self.config.strategy.strategy_id == "llm-fundamental":
            self.strategy = LlmFundamentalStrategy(
                self.config.llm,
                LlmClient(self.config.llm),
                RagClient(self.config.llm.rag_url, self.config.llm.rag_collection),
                audit=self.audit,
                clock=self.clock,
            )
        elif self.config.strategy.strategy_id == "tiny-moe-ranker":
            self.strategy = TinyMoeRankerStrategy(
                self.config.tiny_moe,
                project_root=self.paths.root,
                audit=self.audit,
                version=self.config.strategy.version,
            )
        else:
            self.strategy = MovingAverageStrategy(self.config.strategy)
        self.planner = PortfolioPlanner(self.config.portfolio)
        self.risk = RiskEngine(self.config.risk)
        self.approvals = ApprovalService(self.store, self.config, self.clock, self.audit)
        self.execution = ExecutionService(
            self.store,
            self.provider,
            self.config,
            self.clock,
            self.risk,
            self.approvals,
            self.audit,
        )
        self.narrative = DeterministicNarrativeProvider()
        self.research = ResearchService(self.paths, self.provider, self.config, self.clock)

    def init_db(self) -> dict[str, str]:
        self.store.init()
        return {"state_db": str(self.paths.state_db), "audit_log": str(self.paths.audit_log)}

    def seed_demo(self, *, reset_runtime: bool = False) -> dict[str, str]:
        if reset_runtime:
            self.store.reset_runtime()
            self.store.set_kill_switch(False, "demo reset", "system")
        market, account = seed_demo_data(self.paths.data_dir)
        self.audit.record(
            "data.seeded",
            reason_code="DETERMINISTIC_FIXTURE",
            input_summary=f"market={market.snapshot_id}; account={account.account_id}",
            result_summary="offline demo data written",
        )
        return {
            "market_snapshot_id": market.snapshot_id,
            "account_id": account.account_id,
            "data_dir": str(self.paths.data_dir),
        }

    def seed_account(self, *, reset_runtime: bool = False) -> dict[str, str]:
        """只写账户快照（simulated/live 行情模式用；行情由 provider 提供）。"""
        from quant_agent.data.providers import seed_account_data

        if reset_runtime:
            self.store.reset_runtime()
            self.store.set_kill_switch(False, "demo reset", "system")
        account = seed_account_data(self.paths.data_dir)
        self.audit.record(
            "data.seeded",
            reason_code="ACCOUNT_ONLY",
            input_summary=f"account={account.account_id}",
            result_summary="account data written; market left to provider",
        )
        return {"account_id": account.account_id, "data_dir": str(self.paths.data_dir)}

    def _blocked_decision(self, issues: tuple[ValidationIssue, ...]) -> RiskDecision:
        checks_list: list[RiskCheck] = []
        for issue in issues:
            check_identity = {"code": issue.code, "message": issue.message}
            checks_list.append(
                RiskCheck(
                    check_id=f"chk_{canonical_hash(check_identity)[:12]}",
                    name="input_validation",
                    passed=False,
                    severity=RiskSeverity.BLOCK if issue.blocking else RiskSeverity.WARNING,
                    reason_code=issue.code,
                    message=issue.message,
                )
            )
        checks = tuple(checks_list)
        return RiskDecision(
            decision_id=f"risk_{canonical_hash(checks)[:16]}",
            allowed=False,
            checks=checks,
            allowed_order_ids=(),
            blocked_order_ids=(),
        )

    def generate_report(
        self, report_date: str | date | None = None, *, request_id: str = "cli"
    ) -> DailyReport:
        if report_date is None:
            report_date = self.clock.now().date()
        if isinstance(report_date, str):
            report_date = date.fromisoformat(report_date)
        now = self.clock.now()
        expires_at = now + timedelta(seconds=self.config.portfolio.approval_ttl_seconds)
        try:
            # live 模式优先走 load_market_or_cache：拉取失败回退上次缓存（离线可用）；
            # file/simulated 无此方法时直接 load_market
            loader = getattr(self.provider, "load_market_or_cache", None)
            market = loader() if loader is not None else self.provider.load_market()
            account = self.provider.load_account()
        except FileNotFoundError as exc:
            self.audit.record(
                "report.failed",
                request_id=request_id,
                reason_code="DATA_MISSING",
                result_summary=str(exc),
            )
            raise DataValidationError("demo data is missing; run seed-demo first") from exc
        market_issues = validate_market(
            market, now, self.config.risk.max_data_age_seconds, self.config.portfolio.currency
        )
        account_issues = validate_account(
            account,
            now,
            self.config.portfolio.currency,
            self.config.risk.max_data_age_seconds,
        )
        issues = market_issues + account_issues
        signals: tuple[StrategySignal, ...]
        targets: tuple[TargetPosition, ...]
        orders: tuple[ProposedOrder, ...]
        if issues:
            signals = ()
            targets = ()
            orders = ()
            decision = self._blocked_decision(issues)
        else:
            signals = self.strategy.generate(market)
            targets, orders = self.planner.build(signals, account, market)
            decision = self.risk.evaluate(
                market,
                account,
                orders,
                now,
                kill_switch=bool(self.store.kill_switch().get("enabled", False)),
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
                strategy_id=self.config.strategy.strategy_id,
                strategy_version=self.config.strategy.version,
                risk_config_version=self.config.risk.version,
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
        existing = self.store.get_report(report_id)
        if existing is not None:
            return existing
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
            local_timezone=self.config.local_timezone,
            warnings=tuple(issue.message for issue in issues)
            + tuple(check.message for check in decision.checks if not check.passed),
        )
        report.status = transition(report.status, ReportStatus.GENERATED)
        report.status = transition(report.status, status)
        self.store.save_report(report)
        paths = write_report(report, self.paths.reports_dir, self.narrative)
        self.audit.record(
            "report.generated",
            request_id=request_id,
            report_id=report_id,
            before_state=ReportStatus.DRAFT.value,
            after_state=report.status.value,
            reason_code="REPORT_READY" if decision.allowed else "RISK_BLOCKED",
            input_summary=f"snapshot={market.snapshot_id}; plan_hash={plan.plan_hash}",
            result_summary=f"json={paths[0].name}; markdown={paths[1].name}",
        )
        return report

    def get_report(self, report_id: str) -> DailyReport:
        report = self.store.get_report(report_id)
        if report is None:
            raise DataValidationError(f"report not found: {report_id}")
        return report

    def _refresh_report_file(self, report: DailyReport) -> None:
        write_report(report, self.paths.reports_dir, self.narrative)

    def approve_all(self, report_id: str, approver: str = "user", request_id: str | None = None):
        approval = self.approvals.approve_all(report_id, approver, request_id)
        self._refresh_report_file(self.get_report(report_id))
        return approval

    def approve_partial(
        self,
        report_id: str,
        order_ids: tuple[str, ...],
        approver: str = "user",
        request_id: str | None = None,
    ):
        approval = self.approvals.approve_partial(report_id, order_ids, approver, request_id)
        self._refresh_report_file(self.get_report(report_id))
        return approval

    def reject(
        self, report_id: str, approver: str = "user", request_id: str | None = None
    ) -> DailyReport:
        report = self.approvals.reject(report_id, approver, request_id)
        self._refresh_report_file(report)
        return report

    def revoke(self, report_id: str, approver: str = "user"):
        approval = self.approvals.revoke(report_id, approver)
        self._refresh_report_file(self.get_report(report_id))
        return approval

    def execute(self, report_id: str, *, mode: str = "paper", request_id: str | None = None):
        result = self.execution.execute(report_id, mode=mode, request_id=request_id)
        self._refresh_report_file(self.get_report(report_id))
        return result

    def get_execution(self, execution_id: str):
        return self.store.get_execution(execution_id)

    def get_audit_events(
        self, report_id: str | None = None, limit: int = 100
    ) -> list[dict[str, object]]:
        return [
            to_dict(event)
            for event in reversed(self.store.list_audit(report_id, limit=max(0, limit)))
        ]

    def set_kill_switch(
        self,
        enabled: bool,
        reason: str = "operator request",
        actor: str = "user",
        request_id: str | None = None,
    ) -> dict[str, object]:
        self.store.set_kill_switch(enabled, reason, actor)
        self.audit.record(
            "kill_switch.changed",
            actor=actor,
            request_id=request_id or f"kill-switch:{enabled}",
            reason_code="KILL_SWITCH_ON" if enabled else "KILL_SWITCH_OFF",
            result_summary=f"enabled={enabled}; reason={reason}",
        )
        return self.store.kill_switch()

    def get_chart_data(self, payload: dict[str, object]) -> dict[str, object]:
        report_id = payload.get("report_id")
        report = (
            self.store.get_report(str(report_id)) if report_id else self.store.get_latest_report()
        )
        return self.research.get_chart_data(payload, report)

    def list_strategies(self) -> dict[str, object]:
        return self.research.list_strategies()

    def get_strategy(self, strategy_id: str, version: str | None = None) -> dict[str, object]:
        return self.research.get_strategy(strategy_id, version)

    def validate_strategy(self, payload: dict[str, object]) -> dict[str, object]:
        return self.research.validate_strategy(payload)

    def save_strategy_draft(self, payload: dict[str, object]) -> dict[str, object]:
        return self.research.save_strategy_draft(payload)

    def run_strategy_debug(self, payload: dict[str, object]) -> dict[str, object]:
        return self.research.run_strategy_debug(payload)

    def get_debug_trace(self, run_id: str, start: int = 0, limit: int = 100) -> dict[str, object]:
        return self.research.get_debug_trace(run_id, start, limit)

    def run_backtest(self, payload: dict[str, object]) -> dict[str, object]:
        return self.research.run_backtest(payload)

    def get_backtest_result(self, run_id: str) -> dict[str, object]:
        return self.research.get_backtest_result(run_id)

    def compare_backtests(self, run_ids: list[str]) -> dict[str, object]:
        return self.research.compare_backtests(run_ids)

    def promote_strategy_candidate(self, payload: dict[str, object]) -> dict[str, object]:
        return self.research.promote_strategy_candidate(payload)

    def enable_paper_strategy(self, payload: dict[str, object]) -> dict[str, object]:
        return self.research.enable_paper_strategy(payload)

    def status(self, report_id: str) -> dict[str, object]:
        report = self.get_report(report_id)
        approval = self.store.get_latest_approval(report_id)
        executions = self.store.get_execution_by_key(
            f"{report_id}:{approval.approval_id}:{report.plan.plan_hash}" if approval else "missing"
        )
        return {
            "report": to_dict(report),
            "approval": to_dict(approval) if approval else None,
            "execution": to_dict(executions) if executions else None,
        }

    def dashboard(self, report_id: str | None = None) -> dict[str, object]:
        report = self.store.get_report(report_id) if report_id else self.store.get_latest_report()
        approval = self.store.get_latest_approval(report.report_id) if report else None
        execution = None
        if report is not None and approval is not None:
            execution = self.store.get_execution_by_key(
                f"{report.report_id}:{approval.approval_id}:{report.plan.plan_hash}"
            )
        current_account = None
        try:
            current_account = self.provider.load_account()
        except (FileNotFoundError, ValueError):
            current_account = None
        audit_events = self.store.list_audit(report.report_id if report else None, limit=100)
        risk_checks = report.plan.risk_decision.checks if report else ()
        freshness = next((check for check in risk_checks if check.name == "data_freshness"), None)
        account_check = next(
            (check for check in risk_checks if check.name == "account_status"), None
        )
        return {
            "schema_version": "dashboard.v1",
            "mode": "PAPER_TRADING",
            "paper_only": True,
            "live_broker": "disabled",
            "connection": {"status": "connected", "transport": "local-offline"},
            "kill_switch": self.store.kill_switch(),
            "report_id": report.report_id if report else None,
            "report": to_dict(report) if report else None,
            "approval": to_dict(approval) if approval else None,
            "execution": to_dict(execution) if execution else None,
            "account": to_dict(current_account) if current_account else None,
            "data_freshness": {
                "market_as_of": to_dict(report.market.as_of) if report else None,
                "account_as_of": to_dict(current_account.as_of)
                if current_account
                else (to_dict(report.account.as_of) if report else None),
                "market_status": freshness.reason_code if freshness else "UNKNOWN",
                "account_status": account_check.reason_code if account_check else "UNKNOWN",
            },
            "audit_events": [to_dict(event) for event in reversed(audit_events)],
        }

    def demo(self, report_date: str | date | None = None) -> dict[str, object]:
        # 行情源选择：file 用 seed 静态数据；simulated/live 由 provider 自己
        # 提供新鲜行情（seed 只负责账户与运行时状态）
        if self.config.market_data.source == "file":
            self.seed_demo(reset_runtime=True)
        else:
            self.seed_account(reset_runtime=True)
        report = self.generate_report(report_date, request_id="demo")
        if not report.plan.risk_decision.allowed_order_ids:
            raise DataValidationError("demo report was blocked; see generated report")
        approval = self.approve_all(report.report_id, "demo-user")
        execution = self.execute(report.report_id, mode="paper", request_id="demo-execute")
        return {
            "report_id": report.report_id,
            "approval_id": approval.approval_id,
            "execution_id": execution.execution_id,
            "execution_status": execution.status,
            "json_report": str(self.paths.reports_dir / f"{report.report_id}.json"),
            "markdown_report": str(self.paths.reports_dir / f"{report.report_id}.md"),
            "audit_log": str(self.paths.audit_log),
        }
