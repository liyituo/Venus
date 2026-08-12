from __future__ import annotations

from decimal import Decimal

from quant_agent.research.dsl import validate_strategy_dsl


def _custom_dsl() -> dict[str, object]:
    return {
        "schema_version": "strategy-dsl.v1",
        "strategy_id": "test-cross",
        "version": "0.1.0",
        "display_name": "Test Cross",
        "timeframe": "1d",
        "parameters": {
            "fast": {"type": "integer", "default": 3, "minimum": 2, "maximum": 10},
            "slow": {"type": "integer", "default": 8, "minimum": 3, "maximum": 30},
        },
        "indicators": {
            "fast_ma": {"type": "sma", "source": "close", "window": {"param": "fast"}},
            "slow_ma": {"type": "ema", "source": "close", "window": {"param": "slow"}},
            "rsi": {"type": "rsi", "source": "close", "window": 5},
        },
        "rules": {
            "buy": {"crossover": ["fast_ma", "slow_ma"]},
            "sell": {"crossunder": ["fast_ma", "slow_ma"]},
        },
        "outputs": {
            "buy": {"direction": "BUY", "reason_code": "TEST_CROSS_UP"},
            "sell": {"direction": "SELL", "reason_code": "TEST_CROSS_DOWN"},
        },
    }


def test_chart_contract_contains_decimal_strings_and_synthetic_metadata(service):
    chart = service.get_chart_data({"symbol": "AAPL", "timeframe": "1d", "max_bars": 40})

    assert chart["schema_version"] == "chart-data.v2"
    assert chart["is_synthetic"] is True
    assert chart["supported_timeframes"] == ["1d"]
    assert len(chart["bars"]) == 40
    assert isinstance(chart["bars"][0]["close"], str)
    assert chart["bars"][0]["timestamp"].endswith("Z")
    assert {item["name"] for item in chart["indicators"]} == {"fast_ma", "slow_ma"}


def test_dsl_is_explicit_and_rejects_forbidden_tokens_and_unknown_refs():
    dsl = _custom_dsl()
    dsl["rules"] = {"buy": {"op": "gt", "left": {"ref": "secret_series"}, "right": {"value": 1}}}
    dsl["description"] = "do not eval this"

    errors, _, _, _ = validate_strategy_dsl(dsl)

    codes = {error["code"] for error in errors}
    assert "DSL_FORBIDDEN_TOKEN" in codes
    assert "RULE_REFERENCE" in codes


def test_debug_and_backtest_are_deterministic_and_use_next_bar(service):
    payload = {
        "dsl": _custom_dsl(),
        "parameters": {"fast": 3, "slow": 8},
        "request_id": "research-save",
    }
    service.save_strategy_draft(payload)
    first = service.run_strategy_debug(
        {"strategy_id": "test-cross", "version": "0.1.0", "symbol": "AAPL", "timeframe": "1d"}
    )
    second = service.run_strategy_debug(
        {"strategy_id": "test-cross", "version": "0.1.0", "symbol": "AAPL", "timeframe": "1d"}
    )
    assert first["run_id"] == second["run_id"]
    assert first["trace"] == second["trace"]

    result = service.run_backtest(
        {
            "strategy_id": "test-cross",
            "version": "0.1.0",
            "symbol": "AAPL",
            "timeframe": "1d",
            "initial_cash": "10000",
            "fee_bps": "5",
            "slippage_bps": "10",
        }
    )
    assert result["status"] == "COMPLETED"
    assert result["assumptions"]
    assert result["formulas"]["total_return"]
    for trade in result["trades"]:
        assert any(item["bar_index"] == trade["entry_index"] - 1 for item in result["signals"])


def test_strategy_promotion_requires_backtest_and_creates_new_paper_version(service):
    service.save_strategy_draft({"dsl": _custom_dsl(), "request_id": "draft"})
    service.validate_strategy({"strategy_id": "test-cross", "version": "0.1.0"})
    backtest = service.run_backtest(
        {"strategy_id": "test-cross", "version": "0.1.0", "symbol": "AAPL", "timeframe": "1d"}
    )
    candidate = service.promote_strategy_candidate(
        {"strategy_id": "test-cross", "version": "0.1.0", "backtest_run_id": backtest["run_id"]}
    )
    assert candidate["strategy"]["manifest"]["status"] == "PAPER_CANDIDATE"
    enabled = service.enable_paper_strategy(
        {"strategy_id": "test-cross", "version": "0.1.0", "confirm": True}
    )
    assert enabled["strategy"]["manifest"]["status"] == "PAPER_ENABLED"
    assert enabled["strategy"]["manifest"]["version"] == "0.1.0-paper.1"
    assert enabled["daily_strategy_replaced"] is False
    assert service.research.python_sandbox["available"] is False


def test_parameter_change_changes_run_source_hash(service):
    baseline = service.run_strategy_debug(
        {"strategy_id": "moving-average-demo", "symbol": "AAPL", "timeframe": "1d"}
    )
    changed = service.run_strategy_debug(
        {
            "strategy_id": "moving-average-demo",
            "parameters": {"fast": 4, "slow": 9, "minimum_strength": Decimal("0.001")},
            "symbol": "AAPL",
            "timeframe": "1d",
        }
    )
    assert baseline["source_hash"] != changed["source_hash"]
