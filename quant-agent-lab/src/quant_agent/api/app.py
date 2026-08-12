from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from quant_agent.domain.errors import DomainError
from quant_agent.domain.models import to_dict
from quant_agent.orchestration.service import ApplicationService


class DailyPlanRequest(BaseModel):
    date: str | None = None
    request_id: str = "api-daily-plan"


class ApproveRequest(BaseModel):
    approve_all: bool = Field(default=False, alias="all")
    order_ids: list[str] = Field(default_factory=list)
    approver: str = "api-user"
    request_id: str | None = None

    model_config = {"populate_by_name": True}


class RejectRequest(BaseModel):
    approver: str = "api-user"
    request_id: str | None = None


class ExecuteRequest(BaseModel):
    mode: str = "paper"
    request_id: str | None = None


class KillSwitchRequest(BaseModel):
    enabled: bool
    reason: str = "api request"
    actor: str = "api-user"
    request_id: str | None = None


class ChartDataRequest(BaseModel):
    symbol: str = "AAPL"
    timeframe: str = "1d"
    strategy_id: str = "moving-average-demo"
    version: str | None = None
    snapshot_id: str | None = None
    start: str | None = None
    end: str | None = None
    max_bars: int = Field(default=500, ge=1, le=500)
    report_id: str | None = None


class StrategyPayload(BaseModel):
    strategy_id: str | None = None
    version: str | None = None
    dsl: dict[str, Any] | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class StrategyRunPayload(StrategyPayload):
    symbol: str = "AAPL"
    timeframe: str = "1d"
    snapshot_id: str | None = None
    start: str | None = None
    end: str | None = None
    max_bars: int = Field(default=500, ge=1, le=500)
    run_id: str | None = None


class BacktestPayload(StrategyRunPayload):
    initial_cash: str = "10000"
    fee_bps: str | None = None
    slippage_bps: str | None = None
    max_position_notional: str | None = None


class CompareBacktestsRequest(BaseModel):
    run_ids: list[str] = Field(min_length=1, max_length=8)
    request_id: str | None = None


class PromoteStrategyRequest(BaseModel):
    strategy_id: str
    version: str
    backtest_run_id: str
    request_id: str


class EnablePaperStrategyRequest(BaseModel):
    strategy_id: str
    version: str
    confirm: bool = False
    request_id: str


def _error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code:
        return str(code)
    name = type(exc).__name__
    explicit = {
        "ApprovalError": "APPROVAL_INVALID",
        "DataValidationError": "DATA_VALIDATION_ERROR",
        "ExecutionError": "EXECUTION_ERROR",
        "LiveBrokerDisabledError": "LIVE_BROKER_DISABLED",
        "RiskBlockedError": "RISK_BLOCKED",
        "StateTransitionError": "ILLEGAL_STATE_TRANSITION",
    }
    message = str(exc).lower()
    if "report not found" in message:
        return "REPORT_NOT_FOUND"
    return explicit.get(name, name.upper())


def _http_error(status_code: int, error: str, message: str, *, code: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "error": error, "message": message},
    )


def _call(function, *args, **kwargs) -> Any:
    try:
        return to_dict(function(*args, **kwargs))
    except DomainError as exc:
        error_code = _error_code(exc)
        status = (
            404
            if error_code.endswith("_NOT_FOUND")
            else (
                400
                if error_code
                in {
                    "INVALID_REQUEST",
                    "SCHEMA_INVALID",
                    "SCHEMA_REQUIRED",
                    "STRATEGY_SCHEMA_INVALID",
                    "TIMEFRAME_UNSUPPORTED",
                    "SYMBOL_NOT_FOUND",
                    "RESOURCE_LIMIT",
                    "CONFIRMATION_REQUIRED",
                }
                else 409
            )
        )
        raise _http_error(
            status,
            type(exc).__name__,
            str(exc),
            code=_error_code(exc),
        ) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise _http_error(
            400,
            type(exc).__name__,
            str(exc),
            code="DATA_MISSING" if isinstance(exc, FileNotFoundError) else "INVALID_REQUEST",
        ) from exc


def create_app(service: ApplicationService | None = None) -> FastAPI:
    application = service or ApplicationService()
    app = FastAPI(title="Quant Agent Lab API", version="1.0")

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "paper-only", "live_broker": "disabled"}

    @app.post("/api/v1/daily-plans")
    def generate_daily_plan(request: DailyPlanRequest) -> Any:
        return _call(application.generate_report, request.date, request_id=request.request_id)

    @app.get("/api/v1/reports/{report_id}")
    def get_report(report_id: str) -> Any:
        return _call(application.get_report, report_id)

    @app.post("/api/v1/reports/{report_id}/approve")
    def approve_report(report_id: str, request: ApproveRequest) -> Any:
        if request.approve_all == bool(request.order_ids):
            raise _http_error(
                400,
                "InvalidApprovalRequest",
                "choose exactly one of all or order_ids",
                code="APPROVAL_REQUEST_INVALID",
            )
        if request.approve_all:
            return _call(
                application.approve_all,
                report_id,
                request.approver,
                request.request_id,
            )
        return _call(
            application.approve_partial,
            report_id,
            tuple(request.order_ids),
            request.approver,
            request.request_id,
        )

    @app.post("/api/v1/reports/{report_id}/reject")
    def reject_report(report_id: str, request: RejectRequest) -> Any:
        return _call(application.reject, report_id, request.approver, request.request_id)

    @app.post("/api/v1/reports/{report_id}/execute")
    def execute_report(report_id: str, request: ExecuteRequest) -> Any:
        return _call(
            application.execute, report_id, mode=request.mode, request_id=request.request_id
        )

    @app.get("/api/v1/executions/{execution_id}")
    def get_execution(execution_id: str) -> Any:
        execution = application.get_execution(execution_id)
        if execution is None:
            raise _http_error(
                404,
                "ExecutionNotFound",
                "execution not found",
                code="EXECUTION_NOT_FOUND",
            )
        return to_dict(execution)

    @app.post("/api/v1/kill-switch")
    def set_kill_switch(request: KillSwitchRequest) -> Any:
        return _call(
            application.set_kill_switch,
            request.enabled,
            request.reason,
            request.actor,
            request.request_id,
        )

    @app.get("/api/v1/dashboard")
    def get_dashboard(report_id: str | None = None) -> Any:
        return _call(application.dashboard, report_id)

    @app.get("/api/v1/audit")
    def get_audit(
        report_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=200),
    ) -> Any:
        return _call(application.get_audit_events, report_id, limit)

    @app.post("/api/v2/chart-data")
    def get_chart_data(request: ChartDataRequest) -> Any:
        return _call(application.get_chart_data, request.model_dump(exclude_none=True))

    @app.get("/api/v2/strategies")
    def list_strategies() -> Any:
        return _call(application.list_strategies)

    @app.get("/api/v2/strategies/{strategy_id}")
    def get_strategy(strategy_id: str, version: str | None = None) -> Any:
        return _call(application.get_strategy, strategy_id, version)

    @app.post("/api/v2/strategies/validate")
    def validate_strategy(request: StrategyPayload) -> Any:
        return _call(application.validate_strategy, request.model_dump(exclude_none=True))

    @app.post("/api/v2/strategies/drafts")
    def save_strategy_draft(request: StrategyPayload) -> Any:
        return _call(application.save_strategy_draft, request.model_dump(exclude_none=True))

    @app.post("/api/v2/strategies/debug")
    def run_strategy_debug(request: StrategyRunPayload) -> Any:
        return _call(application.run_strategy_debug, request.model_dump(exclude_none=True))

    @app.get("/api/v2/debug/{run_id}")
    def get_debug_trace(
        run_id: str,
        start: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> Any:
        return _call(application.get_debug_trace, run_id, start, limit)

    @app.post("/api/v2/backtests")
    def run_backtest(request: BacktestPayload) -> Any:
        return _call(application.run_backtest, request.model_dump(exclude_none=True))

    @app.get("/api/v2/backtests/{run_id}")
    def get_backtest_result(run_id: str) -> Any:
        return _call(application.get_backtest_result, run_id)

    @app.post("/api/v2/backtests/compare")
    def compare_backtests(request: CompareBacktestsRequest) -> Any:
        return _call(application.compare_backtests, request.run_ids)

    @app.post("/api/v2/strategies/promote")
    def promote_strategy_candidate(request: PromoteStrategyRequest) -> Any:
        return _call(application.promote_strategy_candidate, request.model_dump(exclude_none=True))

    @app.post("/api/v2/strategies/enable-paper")
    def enable_paper_strategy(request: EnablePaperStrategyRequest) -> Any:
        return _call(application.enable_paper_strategy, request.model_dump(exclude_none=True))

    return app


app = create_app()
