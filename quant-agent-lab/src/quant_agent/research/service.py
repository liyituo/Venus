from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant_agent.data.providers import FileDataProvider
from quant_agent.domain.enums import StrategyKind, StrategyStatus
from quant_agent.domain.errors import ResearchError
from quant_agent.domain.models import (
    BacktestRequest,
    DailyReport,
    MarketBar,
    StrategyDraft,
    StrategyManifest,
    StrategyParameterSpec,
    StrategyValidationResult,
    to_dict,
)
from quant_agent.infrastructure.clock import Clock, SystemClock
from quant_agent.infrastructure.config import DemoConfig
from quant_agent.infrastructure.paths import ProjectPaths
from quant_agent.research.backtest import run_backtest
from quant_agent.research.dsl import (
    ALLOWED_TIMEFRAMES,
    parameter_specs,
    resolve_parameters,
    run_strategy,
    source_hash,
    validate_strategy_dsl,
)

BUILTIN_MA_DSL: dict[str, Any] = {
    "schema_version": "strategy-dsl.v1",
    "strategy_id": "moving-average-demo",
    "version": "1.0.0",
    "display_name": "Moving Average Demo",
    "description": "Deterministic local moving-average relation demo; paper-only.",
    "strategy_kind": "BUILTIN",
    "timeframe": "1d",
    "parameters": {
        "fast": {"type": "integer", "default": 3, "minimum": 1, "maximum": 50},
        "slow": {"type": "integer", "default": 5, "minimum": 2, "maximum": 100},
        "minimum_strength": {"type": "number", "default": "0.001", "minimum": "0", "maximum": "1"},
    },
    "indicators": {
        "fast_ma": {"type": "sma", "source": "close", "window": {"param": "fast"}},
        "slow_ma": {"type": "sma", "source": "close", "window": {"param": "slow"}},
    },
    "rules": {
        "buy": {"op": "gt", "left": {"ref": "fast_ma"}, "right": {"ref": "slow_ma"}},
        "sell": {"op": "lt", "left": {"ref": "fast_ma"}, "right": {"ref": "slow_ma"}},
    },
    "outputs": {
        "buy": {"direction": "BUY", "reason_code": "MA_FAST_ABOVE_SLOW"},
        "sell": {"direction": "SELL", "reason_code": "MA_FAST_BELOW_SLOW"},
    },
}


def _safe_file_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", value)[:100]


def _json_write(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(to_dict(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _json_read(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"research record is not an object: {path.name}")
    return loaded


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ResearchError("datetime must include a timezone", "DATA_TIMEZONE_MISSING")
    return result.astimezone(UTC)


def _parameter_schema(dsl: dict[str, Any]) -> tuple[StrategyParameterSpec, ...]:
    result: list[StrategyParameterSpec] = []
    for name, spec in parameter_specs(dsl).items():
        result.append(
            StrategyParameterSpec(
                name=name,
                value_type=str(spec.get("type", "number")),
                default=str(spec.get("default", "")),
                minimum=None if spec.get("minimum") is None else str(spec["minimum"]),
                maximum=None if spec.get("maximum") is None else str(spec["maximum"]),
                description=str(spec.get("description", "")),
            )
        )
    return tuple(result)


class StrategyRegistry:
    def __init__(self, paths: ProjectPaths, clock: Clock) -> None:
        self.paths = paths
        self.clock = clock
        self._records: dict[tuple[str, str], StrategyDraft] = {}

    def load(self) -> None:
        self._records.clear()
        self._register_builtin()
        for path in sorted(self.paths.strategies_dir.glob("*.json")):
            try:
                self._records[self._key_from_payload(_json_read(path))] = self._draft_from_payload(
                    _json_read(path)
                )
            except (KeyError, TypeError, ValueError):
                continue

    @staticmethod
    def _key_from_payload(payload: dict[str, Any]) -> tuple[str, str]:
        manifest = payload.get("manifest", payload)
        return str(manifest["strategy_id"]), str(manifest["version"])

    def _register_builtin(self) -> None:
        resolved, errors = resolve_parameters(BUILTIN_MA_DSL)
        if errors:
            raise ResearchError(
                "built-in strategy configuration is invalid", "BUILTIN_STRATEGY_INVALID"
            )
        manifest = self._manifest(
            BUILTIN_MA_DSL,
            resolved,
            StrategyStatus.PAPER_ENABLED,
            StrategyKind.BUILTIN,
        )
        self._records[(manifest.strategy_id, manifest.version)] = StrategyDraft(
            manifest, BUILTIN_MA_DSL, resolved
        )

    def _manifest(
        self,
        dsl: dict[str, Any],
        parameters: dict[str, Any],
        status: StrategyStatus,
        kind: StrategyKind = StrategyKind.DECLARATIVE,
        *,
        parent_version: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> StrategyManifest:
        errors, _, _, warmup = validate_strategy_dsl(dsl, parameters)
        if errors and status != StrategyStatus.DRAFT:
            raise ResearchError(
                "strategy manifest cannot be built from invalid DSL", "STRATEGY_SCHEMA_INVALID"
            )
        strategy_id = str(dsl.get("strategy_id", dsl.get("id", "")))
        version = str(dsl.get("version", ""))
        return StrategyManifest(
            strategy_id=strategy_id,
            version=version,
            display_name=str(dsl.get("display_name", strategy_id)),
            description=str(dsl.get("description", "")),
            strategy_kind=kind,
            required_market_fields=("open", "high", "low", "close", "volume"),
            supported_timeframes=(str(dsl.get("timeframe", "1d")),),
            warmup_bars=warmup,
            parameter_schema=_parameter_schema(dsl),
            output_schema={"directions": ["BUY", "SELL", "HOLD"], "reason_code": "string"},
            risk_compatibility="signals-only; backend risk and approval remain authoritative",
            created_at=self.clock.now(),
            source_hash=source_hash(dsl, parameters),
            status=status,
            metadata={"parent_version": parent_version, **(metadata or {})},
        )

    @staticmethod
    def _draft_from_payload(payload: dict[str, Any]) -> StrategyDraft:
        manifest_payload = payload.get("manifest", payload)
        schema = tuple(
            StrategyParameterSpec(
                name=str(item["name"]),
                value_type=str(item["value_type"]),
                default=str(item.get("default", "")),
                minimum=None if item.get("minimum") is None else str(item["minimum"]),
                maximum=None if item.get("maximum") is None else str(item["maximum"]),
                description=str(item.get("description", "")),
            )
            for item in manifest_payload.get("parameter_schema", [])
        )
        manifest = StrategyManifest(
            strategy_id=str(manifest_payload["strategy_id"]),
            version=str(manifest_payload["version"]),
            display_name=str(manifest_payload.get("display_name", manifest_payload["strategy_id"])),
            description=str(manifest_payload.get("description", "")),
            strategy_kind=StrategyKind(str(manifest_payload.get("strategy_kind", "DECLARATIVE"))),
            required_market_fields=tuple(
                str(item) for item in manifest_payload.get("required_market_fields", [])
            ),
            supported_timeframes=tuple(
                str(item) for item in manifest_payload.get("supported_timeframes", ["1d"])
            ),
            warmup_bars=int(manifest_payload.get("warmup_bars", 0)),
            parameter_schema=schema,
            output_schema=dict(manifest_payload.get("output_schema", {})),
            risk_compatibility=str(manifest_payload.get("risk_compatibility", "")),
            created_at=_parse_datetime(manifest_payload["created_at"]) or datetime.now(UTC),
            source_hash=str(manifest_payload["source_hash"]),
            status=StrategyStatus(str(manifest_payload.get("status", "DRAFT"))),
            metadata=dict(manifest_payload.get("metadata", {})),
        )
        return StrategyDraft(
            manifest, dict(payload.get("dsl", {})), dict(payload.get("parameters", {}))
        )

    def save(self, draft: StrategyDraft) -> StrategyDraft:
        path = (
            self.paths.strategies_dir
            / f"{_safe_file_part(draft.manifest.strategy_id)}__{_safe_file_part(draft.manifest.version)}.json"
        )
        _json_write(
            path, {"manifest": draft.manifest, "dsl": draft.dsl, "parameters": draft.parameters}
        )
        self._records[(draft.manifest.strategy_id, draft.manifest.version)] = draft
        return draft

    def get(self, strategy_id: str, version: str | None = None) -> StrategyDraft | None:
        if version:
            return self._records.get((strategy_id, version))
        matches = [draft for (item_id, _), draft in self._records.items() if item_id == strategy_id]
        return sorted(matches, key=lambda item: item.manifest.version)[-1] if matches else None

    def all(self) -> tuple[StrategyDraft, ...]:
        return tuple(
            sorted(
                self._records.values(),
                key=lambda item: (item.manifest.strategy_id, item.manifest.version),
            )
        )


class ResearchService:
    """Research-only service. It never calls ApprovalService, PaperBroker, or LiveBroker."""

    def __init__(
        self,
        paths: ProjectPaths,
        provider: FileDataProvider,
        config: DemoConfig,
        clock: Clock | None = None,
    ) -> None:
        self.paths = paths
        self.provider = provider
        self.config = config
        self.clock = clock or SystemClock()
        self.registry = StrategyRegistry(paths, self.clock)
        self.registry.load()
        self.paths.ensure()

    @property
    def python_sandbox(self) -> dict[str, Any]:
        return {
            "available": False,
            "status": "SANDBOX_UNAVAILABLE",
            "message": "No independently enforced process/container sandbox is configured; arbitrary Python strategy execution is disabled.",
        }

    def _event(self, event_type: str, *, run_id: str | None = None, result: str = "") -> None:
        event = {
            "timestamp": self.clock.now(),
            "event_type": event_type,
            "actor": "research-service",
            "run_id": run_id,
            "result": result,
        }
        with self.paths.research_audit_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(to_dict(event), ensure_ascii=False, sort_keys=True) + "\n")

    def _record(self, strategy_id: str, version: str | None = None) -> StrategyDraft:
        record = self.registry.get(strategy_id, version)
        if record is None:
            raise ResearchError(
                f"strategy not found: {strategy_id}@{version or 'latest'}", "STRATEGY_NOT_FOUND"
            )
        return record

    def list_strategies(self) -> dict[str, Any]:
        return {
            "schema_version": "strategy-research.v2",
            "strategies": [to_dict(item.manifest) for item in self.registry.all()],
            "python_runner": self.python_sandbox,
            "allowed_timeframes": list(ALLOWED_TIMEFRAMES),
        }

    def get_strategy(self, strategy_id: str, version: str | None = None) -> dict[str, Any]:
        record = self._record(strategy_id, version)
        return {
            "manifest": to_dict(record.manifest),
            "dsl": to_dict(record.dsl),
            "parameters": to_dict(record.parameters),
            "python_runner": self.python_sandbox,
        }

    def save_strategy_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        dsl = payload.get("dsl")
        if not isinstance(dsl, dict):
            raise ResearchError("dsl must be a JSON object", "SCHEMA_INVALID")
        strategy_id = str(dsl.get("strategy_id", dsl.get("id", "")))
        version = str(dsl.get("version", ""))
        if not strategy_id or not version:
            raise ResearchError("strategy_id and version are required", "SCHEMA_REQUIRED")
        existing = self.registry.get(strategy_id, version)
        if existing is not None and existing.manifest.strategy_kind == StrategyKind.BUILTIN:
            raise ResearchError(
                "built-in strategy is immutable; save a new version", "STRATEGY_VERSION_CONFLICT"
            )
        parameters = dict(payload.get("parameters", {}))
        resolved, _ = resolve_parameters(dsl, parameters)
        manifest = self.registry._manifest(dsl, resolved, StrategyStatus.DRAFT)
        self.registry.save(StrategyDraft(manifest, dsl, resolved))
        errors, warnings, _, _ = validate_strategy_dsl(dsl, parameters)
        self._event("strategy.draft_saved", result=f"{strategy_id}@{version}")
        return {
            "strategy": self.get_strategy(strategy_id, version),
            "validation": to_dict(
                StrategyValidationResult(
                    not errors,
                    strategy_id,
                    version,
                    StrategyStatus.DRAFT,
                    manifest.source_hash,
                    tuple(errors),
                    tuple(warnings),
                )
            ),
        }

    def validate_strategy(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = None
        dsl = payload.get("dsl")
        if isinstance(dsl, dict):
            strategy_id = str(dsl.get("strategy_id", dsl.get("id", "")))
            version = str(dsl.get("version", ""))
            parameters = dict(payload.get("parameters", {}))
        else:
            strategy_id = str(payload.get("strategy_id", ""))
            version_value: str | None = (
                None if payload.get("version") is None else str(payload["version"])
            )
            record = self._record(strategy_id, version_value)
            dsl = record.dsl
            version = record.manifest.version
            parameters = dict(payload.get("parameters", record.parameters))
        errors, warnings, resolved, warmup = validate_strategy_dsl(dsl, parameters)
        status = record.manifest.status if record else StrategyStatus.DRAFT
        if (
            not errors
            and record is not None
            and record.manifest.strategy_kind != StrategyKind.BUILTIN
            and status == StrategyStatus.DRAFT
        ):
            updated = replace(
                record.manifest,
                status=StrategyStatus.VALIDATED,
                warmup_bars=warmup,
                source_hash=source_hash(dsl, resolved),
            )
            self.registry.save(StrategyDraft(updated, dsl, resolved))
            status = StrategyStatus.VALIDATED
        result = StrategyValidationResult(
            not errors,
            strategy_id,
            str(version),
            status,
            source_hash(dsl, resolved),
            tuple(errors),
            tuple(warnings),
        )
        self._event("strategy.validated", result=f"{strategy_id}@{version}; valid={not errors}")
        return {"validation": to_dict(result), "python_runner": self.python_sandbox}

    def _bars(
        self,
        *,
        symbol: str,
        timeframe: str,
        snapshot_id: str | None = None,
        start: Any = None,
        end: Any = None,
        max_bars: int = 500,
    ) -> tuple[Any, tuple[MarketBar, ...]]:
        if max_bars < 1 or max_bars > 500:
            raise ResearchError("max_bars must be between 1 and 500", "RESOURCE_LIMIT")
        try:
            snapshot = self.provider.load_market()
        except FileNotFoundError as exc:
            raise ResearchError("market snapshot is missing", "DATA_MISSING") from exc
        if snapshot_id and snapshot.snapshot_id != snapshot_id:
            raise ResearchError("requested snapshot is not available", "SNAPSHOT_NOT_FOUND")
        if timeframe not in ALLOWED_TIMEFRAMES:
            raise ResearchError(f"unsupported timeframe {timeframe}", "TIMEFRAME_UNSUPPORTED")
        start_time = _parse_datetime(start)
        end_time = _parse_datetime(end)
        bars = tuple(
            bar
            for bar in snapshot.bars_by_symbol().get(symbol, ())
            if bar.timeframe == timeframe
            and (start_time is None or bar.timestamp >= start_time)
            and (end_time is None or bar.timestamp <= end_time)
        )
        if not bars:
            if symbol not in snapshot.bars_by_symbol():
                raise ResearchError(f"symbol not found: {symbol}", "SYMBOL_NOT_FOUND")
            raise ResearchError(
                f"timeframe {timeframe} is not available for {symbol}", "TIMEFRAME_UNSUPPORTED"
            )
        if any(
            current.timestamp <= previous.timestamp
            for previous, current in zip(bars, bars[1:], strict=False)
        ):
            raise ResearchError("bars must be strictly time ordered", "DATA_BAR_ORDER_INVALID")
        if len(bars) > max_bars:
            bars = bars[-max_bars:]
        return snapshot, bars

    def _strategy_run_inputs(
        self, payload: dict[str, Any]
    ) -> tuple[StrategyDraft, dict[str, Any], tuple[MarketBar, ...], Any]:
        strategy_id = str(payload.get("strategy_id", "moving-average-demo"))
        record = self._record(
            strategy_id, None if payload.get("version") is None else str(payload["version"])
        )
        parameters = dict(payload.get("parameters", record.parameters))
        errors, _, resolved, _ = validate_strategy_dsl(record.dsl, parameters)
        if errors:
            raise ResearchError("strategy validation failed", "STRATEGY_SCHEMA_INVALID")
        snapshot, bars = self._bars(
            symbol=str(payload.get("symbol", "AAPL")),
            timeframe=str(payload.get("timeframe", record.manifest.supported_timeframes[0])),
            snapshot_id=payload.get("snapshot_id"),
            start=payload.get("start"),
            end=payload.get("end"),
            max_bars=int(payload.get("max_bars", 500)),
        )
        return record, resolved, bars, snapshot

    def get_chart_data(
        self, payload: dict[str, Any], report: DailyReport | None = None
    ) -> dict[str, Any]:
        record, parameters, bars, snapshot = self._strategy_run_inputs(payload)
        strategy = run_strategy(record.dsl, parameters, bars)
        latest = bars[-1]
        previous = bars[-2] if len(bars) > 1 else None
        now = self.clock.now()
        age = (now - snapshot.as_of).total_seconds() if snapshot.as_of.tzinfo else None
        stale = age is not None and age > self.config.risk.max_data_age_seconds
        chart_bars = [to_dict(bar) for bar in bars]
        indicators = []
        for name in record.dsl.get("indicators", {}):
            indicators.append(
                {
                    "name": name,
                    "label": name.replace("_", " ").upper(),
                    "values": to_dict(strategy["indicators"].get(name, [])),
                }
            )
        signals = [
            {**to_dict(signal), "kind": "strategy_signal", "label": signal["direction"]}
            for signal in strategy["signals"]
        ]
        markers: list[dict[str, Any]] = []
        if report is not None:
            approved_ids = set(report.plan.risk_decision.allowed_order_ids)
            latest_time = latest.timestamp
            for order in report.plan.orders:
                if order.symbol != latest.symbol:
                    continue
                markers.append(
                    {
                        "kind": "candidate_order",
                        "label": "CANDIDATE",
                        "timestamp": latest_time,
                        "price": order.reference_price,
                        "order_id": order.order_id,
                        "status": "APPROVED" if order.order_id in approved_ids else "CANDIDATE",
                    }
                )
            if report.account:
                position = next(
                    (item for item in report.account.positions if item.symbol == latest.symbol),
                    None,
                )
                if position is not None:
                    markers.append(
                        {
                            "kind": "position_cost",
                            "label": "AVG COST",
                            "timestamp": latest_time,
                            "price": position.average_price,
                            "quantity": position.quantity,
                        }
                    )
        return {
            "schema_version": "chart-data.v2",
            "symbol": latest.symbol,
            "timeframe": latest.timeframe,
            "snapshot_id": snapshot.snapshot_id,
            "data_source": snapshot.source,
            "is_synthetic": any(bar.is_synthetic for bar in bars),
            "data_status": "STALE" if stale else "FRESH",
            "stale": stale,
            "data_as_of": snapshot.as_of,
            "supported_timeframes": sorted(
                {bar.timeframe for bar in snapshot.bars if bar.symbol == latest.symbol}
            ),
            "latest": {
                "timestamp": latest.timestamp,
                "price": latest.close,
                "open": latest.open,
                "high": latest.high,
                "low": latest.low,
                "close": latest.close,
                "volume": latest.volume,
                "change": None if previous is None else latest.close - previous.close,
                "change_percent": None
                if previous is None or previous.close == 0
                else latest.close / previous.close - Decimal("1"),
            },
            "strategy": {
                "strategy_id": record.manifest.strategy_id,
                "version": record.manifest.version,
                "source_hash": strategy["source_hash"],
                "parameters": parameters,
            },
            "bars": chart_bars,
            "indicators": indicators,
            "volume": [{"timestamp": bar.timestamp, "value": bar.volume} for bar in bars],
            "signals": signals,
            "markers": markers,
            "legend": [
                {"kind": "BUY", "label": "BUY signal", "color": "#36e0a0"},
                {"kind": "SELL", "label": "SELL signal", "color": "#ff6b8b"},
                {"kind": "CANDIDATE", "label": "Candidate order", "color": "#ffbe5c"},
                {"kind": "AVG COST", "label": "Position average cost", "color": "#a88bff"},
            ],
        }

    def run_strategy_debug(self, payload: dict[str, Any]) -> dict[str, Any]:
        record, parameters, bars, snapshot = self._strategy_run_inputs(payload)
        strategy = run_strategy(record.dsl, parameters, bars)
        run_id = str(
            payload.get("run_id")
            or f"dbg_{source_hash({**record.dsl, 'snapshot_id': snapshot.snapshot_id, 'symbol': bars[0].symbol}, parameters)[:20]}"
        )
        result = {
            "run_id": run_id,
            "strategy_id": record.manifest.strategy_id,
            "version": record.manifest.version,
            "source_hash": strategy["source_hash"],
            "snapshot_id": snapshot.snapshot_id,
            "symbol": bars[0].symbol,
            "timeframe": bars[0].timeframe,
            "status": "COMPLETED",
            "total_bars": len(strategy["trace"]),
            "signals": strategy["signals"],
            "trace": strategy["trace"],
            "warmup_bars": strategy["warmup_bars"],
            "assumptions": (
                "Fixed snapshot; deterministic DSL interpreter; no account, approval, or PaperBroker writes.",
            ),
        }
        _json_write(self.paths.debug_dir / f"{_safe_file_part(run_id)}.json", result)
        self._event("strategy.debug_completed", run_id=run_id, result=f"bars={len(bars)}")
        return to_dict(result)

    def get_debug_trace(self, run_id: str, start: int = 0, limit: int = 100) -> dict[str, Any]:
        path = self.paths.debug_dir / f"{_safe_file_part(run_id)}.json"
        if not path.exists():
            raise ResearchError(f"debug run not found: {run_id}", "DEBUG_RUN_NOT_FOUND")
        result = _json_read(path)
        trace = result.get("trace", [])
        return {
            **result,
            "trace": trace[max(0, start) : max(0, start) + min(limit, 200)],
            "trace_total": len(trace),
        }

    def run_backtest(self, payload: dict[str, Any]) -> dict[str, Any]:
        record, parameters, bars, snapshot = self._strategy_run_inputs(payload)
        request = BacktestRequest(
            strategy_id=record.manifest.strategy_id,
            version=record.manifest.version,
            symbol=bars[0].symbol,
            timeframe=bars[0].timeframe,
            snapshot_id=snapshot.snapshot_id,
            initial_cash=Decimal(str(payload.get("initial_cash", "10000"))),
            fee_bps=Decimal(str(payload.get("fee_bps", self.config.portfolio.fee_bps))),
            slippage_bps=Decimal(
                str(payload.get("slippage_bps", self.config.portfolio.slippage_bps))
            ),
            max_position_notional=None
            if payload.get("max_position_notional") in (None, "")
            else Decimal(str(payload["max_position_notional"])),
            run_id=payload.get("run_id"),
        )
        run_id = str(
            request.run_id
            or f"bt_{source_hash({**record.dsl, 'snapshot_id': snapshot.snapshot_id, 'symbol': request.symbol, 'fee_bps': str(request.fee_bps), 'slippage_bps': str(request.slippage_bps)}, parameters)[:20]}"
        )
        result = run_backtest(
            dsl=record.dsl,
            parameters=parameters,
            bars=bars,
            run_id=run_id,
            initial_cash=request.initial_cash,
            fee_bps=request.fee_bps,
            slippage_bps=request.slippage_bps,
            max_position_notional=request.max_position_notional,
        )
        payload_result = {
            **result,
            "strategy_id": record.manifest.strategy_id,
            "version": record.manifest.version,
            "snapshot_id": snapshot.snapshot_id,
            "symbol": request.symbol,
            "timeframe": request.timeframe,
            "parameters": parameters,
            "initial_cash": request.initial_cash,
            "fee_bps": request.fee_bps,
            "slippage_bps": request.slippage_bps,
        }
        _json_write(self.paths.backtests_dir / f"{_safe_file_part(run_id)}.json", payload_result)
        if record.manifest.strategy_kind != StrategyKind.BUILTIN and record.manifest.status in {
            StrategyStatus.DRAFT,
            StrategyStatus.VALIDATED,
        }:
            updated = replace(
                record.manifest, status=StrategyStatus.BACKTESTED, source_hash=result["source_hash"]
            )
            self.registry.save(StrategyDraft(updated, record.dsl, parameters))
        self._event(
            "strategy.backtest_completed", run_id=run_id, result=f"trades={len(result['trades'])}"
        )
        return to_dict(payload_result)

    def get_backtest_result(self, run_id: str) -> dict[str, Any]:
        path = self.paths.backtests_dir / f"{_safe_file_part(run_id)}.json"
        if not path.exists():
            raise ResearchError(f"backtest run not found: {run_id}", "BACKTEST_NOT_FOUND")
        return _json_read(path)

    def compare_backtests(self, run_ids: list[str]) -> dict[str, Any]:
        if not run_ids or len(run_ids) > 8:
            raise ResearchError("compare requires 1 to 8 run IDs", "RESOURCE_LIMIT")
        results = [self.get_backtest_result(run_id) for run_id in run_ids]
        return {
            "schema_version": "backtest-comparison.v1",
            "runs": [
                {
                    "run_id": result["run_id"],
                    "strategy_id": result["strategy_id"],
                    "version": result["version"],
                    "source_hash": result["source_hash"],
                    "metrics": result["metrics"],
                }
                for result in results
            ],
        }

    def promote_strategy_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = self._record(str(payload.get("strategy_id", "")), str(payload.get("version", "")))
        if record.manifest.status != StrategyStatus.BACKTESTED:
            raise ResearchError(
                "strategy must be BACKTESTED before promotion", "STRATEGY_STATE_INVALID"
            )
        backtest = self.get_backtest_result(str(payload.get("backtest_run_id", "")))
        if (
            backtest["strategy_id"] != record.manifest.strategy_id
            or backtest["version"] != record.manifest.version
        ):
            raise ResearchError(
                "backtest does not match strategy version", "STRATEGY_BINDING_INVALID"
            )
        metadata = {
            **record.manifest.metadata,
            "backtest_run_id": backtest["run_id"],
            "snapshot_id": backtest["snapshot_id"],
            "backtest_assumptions": backtest["assumptions"],
            "promoted_at": self.clock.now(),
        }
        updated = replace(record.manifest, status=StrategyStatus.PAPER_CANDIDATE, metadata=metadata)
        saved = self.registry.save(StrategyDraft(updated, record.dsl, record.parameters))
        self._event(
            "strategy.paper_candidate_created",
            result=f"{saved.manifest.strategy_id}@{saved.manifest.version}",
        )
        return {
            "strategy": self.get_strategy(saved.manifest.strategy_id, saved.manifest.version),
            "paper_enabled": False,
        }

    def enable_paper_strategy(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirm") is not True:
            raise ResearchError("explicit confirmation is required", "CONFIRMATION_REQUIRED")
        record = self._record(str(payload.get("strategy_id", "")), str(payload.get("version", "")))
        if record.manifest.status != StrategyStatus.PAPER_CANDIDATE:
            raise ResearchError(
                "strategy must be PAPER_CANDIDATE before enabling", "STRATEGY_STATE_INVALID"
            )
        errors, _, parameters, _ = validate_strategy_dsl(record.dsl, record.parameters)
        if errors:
            raise ResearchError(
                "strategy must pass validation again before enabling", "STRATEGY_SCHEMA_INVALID"
            )
        new_version = f"{record.manifest.version}-paper.1"
        if self.registry.get(record.manifest.strategy_id, new_version) is not None:
            raise ResearchError(
                "paper strategy version already exists", "STRATEGY_VERSION_CONFLICT"
            )
        metadata = {
            **record.manifest.metadata,
            "enabled_from_version": record.manifest.version,
            "enabled_at": self.clock.now(),
        }
        enabled_manifest = replace(
            record.manifest,
            version=new_version,
            status=StrategyStatus.PAPER_ENABLED,
            source_hash=source_hash(record.dsl, parameters),
            metadata=metadata,
        )
        enabled = self.registry.save(StrategyDraft(enabled_manifest, record.dsl, parameters))
        self._event(
            "strategy.paper_enabled",
            result=f"{enabled.manifest.strategy_id}@{enabled.manifest.version}",
        )
        return {
            "strategy": self.get_strategy(enabled.manifest.strategy_id, enabled.manifest.version),
            "daily_strategy_replaced": False,
        }
