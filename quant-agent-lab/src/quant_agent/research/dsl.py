from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from quant_agent.domain.models import MarketBar, canonical_hash

ALLOWED_TIMEFRAMES = ("1m", "5m", "15m", "1h", "1d")
MARKET_FIELDS = ("open", "high", "low", "close", "volume")
INDICATOR_TYPES = {
    "sma",
    "ema",
    "rsi",
    "macd",
    "bollinger",
    "rolling_high",
    "rolling_low",
    "returns",
}
RULE_OPERATORS = {"gt", "gte", "lt", "lte", "eq", "crossover", "crossunder", "and", "or", "not"}
FORBIDDEN_TOKENS = ("eval", "exec", "__import__", "open(", "os.", "subprocess", "import ")


class DSLValidationError(ValueError):
    def __init__(self, errors: Sequence[dict[str, Any]]) -> None:
        self.errors = tuple(errors)
        super().__init__("strategy DSL validation failed")


def _error(code: str, message: str, path: str) -> dict[str, str]:
    return {"code": code, "message": message, "path": path}


def _decimal(value: Any, path: str, errors: list[dict[str, Any]]) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        errors.append(_error("PARAMETER_TYPE", "expected a decimal-compatible value", path))
        return None


def _resolve_parameter(value: Any, parameters: Mapping[str, Any]) -> Any:
    if isinstance(value, Mapping) and set(value) == {"param"}:
        return parameters.get(str(value["param"]))
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return parameters.get(value[2:-1])
    return value


def parameter_specs(dsl: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = dsl.get("parameters", {})
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for name, spec in raw.items():
        if isinstance(spec, Mapping):
            result[str(name)] = dict(spec)
        else:
            result[str(name)] = {"type": "number", "default": spec}
    return result


def resolve_parameters(
    dsl: Mapping[str, Any], overrides: Mapping[str, Any] | None = None
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    errors: list[dict[str, Any]] = []
    overrides = overrides or {}
    resolved: dict[str, Any] = {}
    for name, spec in parameter_specs(dsl).items():
        value = overrides.get(name, spec.get("default"))
        if value is None:
            errors.append(
                _error("PARAMETER_MISSING", f"parameter {name} has no value", f"parameters.{name}")
            )
            continue
        value_type = str(spec.get("type", "number"))
        if value_type in {"integer", "int"}:
            try:
                integer = int(value)
            except (TypeError, ValueError):
                errors.append(
                    _error(
                        "PARAMETER_TYPE",
                        f"parameter {name} must be an integer",
                        f"parameters.{name}",
                    )
                )
                continue
            if str(value) not in {str(integer), f"{integer}.0"} and not isinstance(value, int):
                errors.append(
                    _error(
                        "PARAMETER_TYPE",
                        f"parameter {name} must be an integer",
                        f"parameters.{name}",
                    )
                )
                continue
            value = integer
        elif value_type in {"number", "decimal", "float"}:
            decimal = _decimal(value, f"parameters.{name}", errors)
            if decimal is None:
                continue
            value = decimal
        elif value_type == "boolean":
            if not isinstance(value, bool):
                errors.append(
                    _error(
                        "PARAMETER_TYPE", f"parameter {name} must be boolean", f"parameters.{name}"
                    )
                )
                continue
        else:
            errors.append(
                _error(
                    "PARAMETER_TYPE_UNSUPPORTED",
                    f"unsupported parameter type {value_type}",
                    f"parameters.{name}",
                )
            )
            continue
        minimum = spec.get("minimum")
        maximum = spec.get("maximum")
        if minimum is not None and Decimal(str(value)) < Decimal(str(minimum)):
            errors.append(
                _error(
                    "PARAMETER_RANGE", f"parameter {name} is below minimum", f"parameters.{name}"
                )
            )
        if maximum is not None and Decimal(str(value)) > Decimal(str(maximum)):
            errors.append(
                _error(
                    "PARAMETER_RANGE", f"parameter {name} is above maximum", f"parameters.{name}"
                )
            )
        resolved[name] = value
    unknown = sorted(set(overrides) - set(parameter_specs(dsl)))
    for name in unknown:
        errors.append(
            _error("PARAMETER_UNKNOWN", f"unknown parameter {name}", f"parameters.{name}")
        )
    return resolved, tuple(errors)


def _walk_strings(value: Any, path: str = "$") -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, str):
        lowered = value.lower()
        for token in FORBIDDEN_TOKENS:
            if token in lowered:
                found.append(_error("DSL_FORBIDDEN_TOKEN", f"forbidden token {token!r}", path))
                break
    elif isinstance(value, Mapping):
        for key, item in value.items():
            found.extend(_walk_strings(item, f"{path}.{key}"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            found.extend(_walk_strings(item, f"{path}[{index}]"))
    return found


def _window(
    value: Any, parameters: Mapping[str, Any], path: str, errors: list[dict[str, Any]]
) -> int | None:
    resolved = _resolve_parameter(value, parameters)
    try:
        result = int(resolved)
    except (TypeError, ValueError):
        errors.append(_error("INDICATOR_WINDOW", "window must resolve to an integer", path))
        return None
    if result < 1 or result > 500:
        errors.append(_error("INDICATOR_WINDOW", "window must be between 1 and 500", path))
        return None
    return result


def _rule_refs(rule: Any, path: str = "rules") -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    if isinstance(rule, Mapping):
        if "ref" in rule and isinstance(rule["ref"], str):
            refs.append((rule["ref"], path))
        for key, value in rule.items():
            if key in {"ref", "op", "direction", "reason_code"}:
                continue
            refs.extend(_rule_refs(value, f"{path}.{key}"))
    elif isinstance(rule, Sequence) and not isinstance(rule, (str, bytes)):
        for index, value in enumerate(rule):
            if isinstance(value, str):
                refs.append((value, f"{path}[{index}]"))
            else:
                refs.extend(_rule_refs(value, f"{path}[{index}]"))
    elif isinstance(rule, str):
        refs.append((rule, path))
    return refs


def validate_strategy_dsl(
    dsl: Mapping[str, Any], overrides: Mapping[str, Any] | None = None
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], dict[str, Any], int]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not isinstance(dsl, Mapping):
        return (_error("SCHEMA_INVALID", "strategy must be a JSON object", "$"),), (), {}, 0
    errors.extend(_walk_strings(dsl))
    strategy_id = dsl.get("strategy_id", dsl.get("id"))
    version = dsl.get("version")
    if (
        not isinstance(strategy_id, str)
        or not strategy_id
        or any(char.isspace() for char in strategy_id)
    ):
        errors.append(_error("SCHEMA_REQUIRED", "strategy_id is required", "strategy_id"))
    if not isinstance(version, str) or not version:
        errors.append(_error("SCHEMA_REQUIRED", "version is required", "version"))
    timeframe = dsl.get("timeframe", "1d")
    if timeframe not in ALLOWED_TIMEFRAMES:
        errors.append(
            _error("TIMEFRAME_UNSUPPORTED", f"unsupported timeframe {timeframe}", "timeframe")
        )
    parameters, parameter_errors = resolve_parameters(dsl, overrides)
    errors.extend(parameter_errors)
    indicators = dsl.get("indicators", {})
    if not isinstance(indicators, Mapping):
        errors.append(_error("SCHEMA_TYPE", "indicators must be an object", "indicators"))
        indicators = {}
    if len(indicators) > 12:
        errors.append(_error("RESOURCE_LIMIT", "at most 12 indicators are allowed", "indicators"))
    warmup = 0
    for name, spec in indicators.items():
        path = f"indicators.{name}"
        if not isinstance(name, str) or not name or not isinstance(spec, Mapping):
            errors.append(_error("SCHEMA_TYPE", "indicator must be an object with a name", path))
            continue
        indicator_type = str(spec.get("type", ""))
        if indicator_type not in INDICATOR_TYPES:
            errors.append(
                _error(
                    "INDICATOR_UNSUPPORTED",
                    f"unsupported indicator {indicator_type}",
                    f"{path}.type",
                )
            )
            continue
        source = spec.get("source", "close")
        if source not in MARKET_FIELDS:
            errors.append(
                _error(
                    "MARKET_FIELD_MISSING", f"unsupported source field {source}", f"{path}.source"
                )
            )
        if indicator_type == "returns":
            warmup = max(warmup, 1)
            continue
        window_value = spec.get("window", spec.get("period"))
        window_values: tuple[Any, ...]
        if indicator_type == "macd":
            window_values = (spec.get("fast", 12), spec.get("slow", 26), spec.get("signal", 9))
        else:
            window_values = (window_value,)
        for index, candidate in enumerate(window_values):
            resolved_window = _window(candidate, parameters, f"{path}.window[{index}]", errors)
            if resolved_window:
                warmup = max(warmup, resolved_window)
        if indicator_type == "bollinger":
            stddev = _resolve_parameter(spec.get("stddev", 2), parameters)
            if _decimal(stddev, f"{path}.stddev", errors) is None:
                continue
    rules = dsl.get("rules", {})
    if not isinstance(rules, Mapping) or not rules:
        errors.append(_error("SCHEMA_REQUIRED", "at least one rule is required", "rules"))
        rules = {}
    if len(rules) > 8:
        errors.append(_error("RESOURCE_LIMIT", "at most 8 rules are allowed", "rules"))
    valid_refs = set(indicators) | set(MARKET_FIELDS)
    for rule_name, rule in rules.items():
        if not isinstance(rule_name, str) or not rule_name:
            errors.append(_error("SCHEMA_TYPE", "rule name must be a non-empty string", "rules"))
        for reference, path in _rule_refs(rule, f"rules.{rule_name}"):
            if reference not in valid_refs and not reference.startswith("${"):
                errors.append(
                    _error("RULE_REFERENCE", f"unknown series reference {reference}", path)
                )
        if isinstance(rule, Mapping):
            operators = set(rule) | ({str(rule.get("op"))} if rule.get("op") else set())
            unsupported = sorted(
                operator
                for operator in operators
                if operator in {"add", "mul", "div", "call", "function"}
            )
            for operator in unsupported:
                errors.append(
                    _error(
                        "RULE_OPERATOR_UNSUPPORTED",
                        f"operator {operator} is not allowed",
                        f"rules.{rule_name}",
                    )
                )
    outputs = dsl.get("outputs", {})
    if not isinstance(outputs, Mapping):
        errors.append(_error("SCHEMA_TYPE", "outputs must be an object", "outputs"))
        outputs = {}
    for rule_name, output in outputs.items():
        if rule_name not in rules:
            errors.append(
                _error(
                    "OUTPUT_RULE_MISSING",
                    f"output references unknown rule {rule_name}",
                    f"outputs.{rule_name}",
                )
            )
        if not isinstance(output, Mapping) or output.get("direction") not in {
            "BUY",
            "SELL",
            "HOLD",
        }:
            errors.append(
                _error(
                    "OUTPUT_INVALID",
                    "output direction must be BUY, SELL, or HOLD",
                    f"outputs.{rule_name}",
                )
            )
    if "outputs" not in dsl:
        warnings.append(
            {
                "code": "OUTPUT_DEFAULTED",
                "message": "missing outputs default to HOLD",
                "path": "outputs",
            }
        )
    return tuple(errors), tuple(warnings), parameters, warmup


def source_hash(dsl: Mapping[str, Any], parameters: Mapping[str, Any]) -> str:
    return canonical_hash({"dsl": dsl, "parameters": parameters})


def _source_values(bars: Sequence[MarketBar], field: str) -> list[Decimal]:
    return [getattr(bar, field) for bar in bars]


def _rolling_sma(values: Sequence[Decimal], window: int) -> list[Decimal | None]:
    result: list[Decimal | None] = []
    for index in range(len(values)):
        if index + 1 < window:
            result.append(None)
        else:
            result.append(
                sum(values[index + 1 - window : index + 1], Decimal("0")) / Decimal(window)
            )
    return result


def _rolling_ema(values: Sequence[Decimal], window: int) -> list[Decimal | None]:
    result: list[Decimal | None] = [None] * len(values)
    if len(values) < window:
        return result
    current = sum(values[:window], Decimal("0")) / Decimal(window)
    result[window - 1] = current
    alpha = Decimal("2") / Decimal(window + 1)
    for index in range(window, len(values)):
        current = (values[index] - current) * alpha + current
        result[index] = current
    return result


def _rolling_high(values: Sequence[Decimal], window: int) -> list[Decimal | None]:
    return [
        None if index + 1 < window else max(values[index + 1 - window : index + 1])
        for index in range(len(values))
    ]


def _rolling_low(values: Sequence[Decimal], window: int) -> list[Decimal | None]:
    return [
        None if index + 1 < window else min(values[index + 1 - window : index + 1])
        for index in range(len(values))
    ]


def _rsi(values: Sequence[Decimal], window: int) -> list[Decimal | None]:
    result: list[Decimal | None] = [None] * len(values)
    if len(values) <= window:
        return result
    gains = [
        max(values[index] - values[index - 1], Decimal("0")) for index in range(1, len(values))
    ]
    losses = [
        max(values[index - 1] - values[index], Decimal("0")) for index in range(1, len(values))
    ]
    average_gain = sum(gains[:window], Decimal("0")) / Decimal(window)
    average_loss = sum(losses[:window], Decimal("0")) / Decimal(window)
    result[window] = (
        Decimal("100")
        if average_loss == 0
        else Decimal("100") - Decimal("100") / (Decimal("1") + average_gain / average_loss)
    )
    for index in range(window + 1, len(values)):
        average_gain = (average_gain * Decimal(window - 1) + gains[index - 1]) / Decimal(window)
        average_loss = (average_loss * Decimal(window - 1) + losses[index - 1]) / Decimal(window)
        result[index] = (
            Decimal("100")
            if average_loss == 0
            else Decimal("100") - Decimal("100") / (Decimal("1") + average_gain / average_loss)
        )
    return result


def build_indicators(
    dsl: Mapping[str, Any], parameters: Mapping[str, Any], bars: Sequence[MarketBar]
) -> tuple[dict[str, list[Decimal | None]], int]:
    series: dict[str, list[Decimal | None]] = {
        field: list(_source_values(bars, field)) for field in MARKET_FIELDS
    }
    warmup = 0
    for name, spec in dsl.get("indicators", {}).items():
        indicator_type = str(spec["type"])
        source = str(spec.get("source", "close"))
        values = [Decimal(str(item)) for item in series[source]]
        if indicator_type == "returns":
            result: list[Decimal | None] = [None]
            result.extend(
                None if values[index - 1] == 0 else values[index] / values[index - 1] - Decimal("1")
                for index in range(1, len(values))
            )
            series[name] = result
            warmup = max(warmup, 1)
        elif indicator_type == "sma":
            window = int(_resolve_parameter(spec["window"], parameters))
            series[name] = _rolling_sma(values, window)
            warmup = max(warmup, window)
        elif indicator_type == "ema":
            window = int(_resolve_parameter(spec["window"], parameters))
            series[name] = _rolling_ema(values, window)
            warmup = max(warmup, window)
        elif indicator_type == "rsi":
            window = int(_resolve_parameter(spec.get("window", spec.get("period", 14)), parameters))
            series[name] = _rsi(values, window)
            warmup = max(warmup, window)
        elif indicator_type == "rolling_high":
            window = int(_resolve_parameter(spec["window"], parameters))
            series[name] = _rolling_high(values, window)
            warmup = max(warmup, window)
        elif indicator_type == "rolling_low":
            window = int(_resolve_parameter(spec["window"], parameters))
            series[name] = _rolling_low(values, window)
            warmup = max(warmup, window)
        elif indicator_type == "macd":
            fast = int(_resolve_parameter(spec.get("fast", 12), parameters))
            slow = int(_resolve_parameter(spec.get("slow", 26), parameters))
            signal_window = int(_resolve_parameter(spec.get("signal", 9), parameters))
            fast_values = _rolling_ema(values, fast)
            slow_values = _rolling_ema(values, slow)
            macd_values: list[Decimal | None] = []
            for index in range(len(values)):
                fast_value = fast_values[index]
                slow_value = slow_values[index]
                macd_values.append(
                    None if fast_value is None or slow_value is None else fast_value - slow_value
                )
            compact = [item for item in macd_values if item is not None]
            compact_signal = _rolling_ema(compact, signal_window)
            signal_values: list[Decimal | None] = [None] * len(values)
            cursor = 0
            for index, item in enumerate(macd_values):
                if item is not None:
                    signal_values[index] = compact_signal[cursor]
                    cursor += 1
            series[name] = macd_values
            series[f"{name}.signal"] = signal_values
            histogram: list[Decimal | None] = []
            for index in range(len(values)):
                macd_value = macd_values[index]
                signal_value = signal_values[index]
                histogram.append(
                    None
                    if macd_value is None or signal_value is None
                    else macd_value - signal_value
                )
            series[f"{name}.histogram"] = histogram
            warmup = max(warmup, slow + signal_window)
        elif indicator_type == "bollinger":
            window = int(_resolve_parameter(spec["window"], parameters))
            deviation = Decimal(str(_resolve_parameter(spec.get("stddev", 2), parameters)))
            middle = _rolling_sma(values, window)
            upper: list[Decimal | None] = []
            lower: list[Decimal | None] = []
            for index, average in enumerate(middle):
                if average is None:
                    upper.append(None)
                    lower.append(None)
                    continue
                sample = values[index + 1 - window : index + 1]
                variance = sum((value - average) ** 2 for value in sample) / Decimal(window)
                deviation_value = variance.sqrt() * deviation
                upper.append(average + deviation_value)
                lower.append(average - deviation_value)
            series[name] = middle
            series[f"{name}.upper"] = upper
            series[f"{name}.lower"] = lower
            warmup = max(warmup, window)
    return series, warmup


def _value(
    expr: Any, series: Mapping[str, Sequence[Decimal | None]], bars: Sequence[MarketBar], index: int
) -> Decimal | None:
    if isinstance(expr, Mapping):
        if "ref" in expr:
            return _value(expr["ref"], series, bars, index)
        if "field" in expr:
            return _value(expr["field"], series, bars, index)
        if "value" in expr:
            try:
                return Decimal(str(expr["value"]))
            except InvalidOperation:
                return None
    if isinstance(expr, str):
        values = series.get(expr)
        if values is not None and 0 <= index < len(values):
            return values[index]
        return None
    try:
        return Decimal(str(expr))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _detail(value: Any, **extra: Any) -> dict[str, Any]:
    result = {"value": value}
    result.update(extra)
    return result


def evaluate_rule(
    expression: Any,
    series: Mapping[str, Sequence[Decimal | None]],
    bars: Sequence[MarketBar],
    index: int,
) -> tuple[bool, dict[str, Any]]:
    if isinstance(expression, Mapping) and "crossover" in expression:
        pair = expression["crossover"]
        if not isinstance(pair, Sequence) or len(pair) != 2 or index == 0:
            return False, _detail(False, operator="crossover")
        previous_left = _value(pair[0], series, bars, index - 1)
        previous_right = _value(pair[1], series, bars, index - 1)
        current_left = _value(pair[0], series, bars, index)
        current_right = _value(pair[1], series, bars, index)
        if any(
            item is None for item in (previous_left, previous_right, current_left, current_right)
        ):
            return False, _detail(False, operator="crossover")
        assert (
            previous_left is not None
            and previous_right is not None
            and current_left is not None
            and current_right is not None
        )
        result = previous_left <= previous_right and current_left > current_right
        return bool(result), _detail(bool(result), operator="crossover")
    if isinstance(expression, Mapping) and "crossunder" in expression:
        pair = expression["crossunder"]
        if not isinstance(pair, Sequence) or len(pair) != 2 or index == 0:
            return False, _detail(False, operator="crossunder")
        previous_left = _value(pair[0], series, bars, index - 1)
        previous_right = _value(pair[1], series, bars, index - 1)
        current_left = _value(pair[0], series, bars, index)
        current_right = _value(pair[1], series, bars, index)
        if any(
            item is None for item in (previous_left, previous_right, current_left, current_right)
        ):
            return False, _detail(False, operator="crossunder")
        assert (
            previous_left is not None
            and previous_right is not None
            and current_left is not None
            and current_right is not None
        )
        result = previous_left >= previous_right and current_left < current_right
        return bool(result), _detail(bool(result), operator="crossunder")
    if isinstance(expression, Mapping):
        operator = str(expression.get("op", ""))
        if operator in {"and", "or"}:
            args = expression.get("args", [])
            values = (
                [evaluate_rule(item, series, bars, index)[0] for item in args]
                if isinstance(args, Sequence)
                else []
            )
            result = all(values) if operator == "and" else any(values)
            return result, _detail(result, operator=operator, operands=values)
        if operator == "not":
            value, _ = evaluate_rule(expression.get("arg"), series, bars, index)
            return not value, _detail(not value, operator="not", operand=value)
        if operator in {"gt", "gte", "lt", "lte", "eq"}:
            left = _value(expression.get("left"), series, bars, index)
            right = _value(expression.get("right"), series, bars, index)
            if left is None or right is None:
                return False, _detail(False, operator=operator, left=None, right=None)
            result = {
                "gt": left > right,
                "gte": left >= right,
                "lt": left < right,
                "lte": left <= right,
                "eq": left == right,
            }[operator]
            return result, _detail(result, operator=operator, left=left, right=right)
        for shorthand, operator in (("greater_than", "gt"), ("less_than", "lt"), ("equals", "eq")):
            if shorthand in expression:
                pair = expression[shorthand]
                if isinstance(pair, Sequence) and len(pair) == 2:
                    return evaluate_rule(
                        {"op": operator, "left": pair[0], "right": pair[1]}, series, bars, index
                    )
    return False, _detail(False, operator="invalid")


def run_strategy(
    dsl: Mapping[str, Any],
    parameters: Mapping[str, Any] | None,
    bars: Sequence[MarketBar],
) -> dict[str, Any]:
    errors, warnings, resolved, warmup = validate_strategy_dsl(dsl, parameters)
    if errors:
        raise DSLValidationError(errors)
    series, calculated_warmup = build_indicators(dsl, resolved, bars)
    warmup = max(warmup, calculated_warmup)
    rules = dsl.get("rules", {})
    outputs = dsl.get("outputs", {})
    traces: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    for index, bar in enumerate(bars):
        rule_results: dict[str, bool] = {}
        rule_details: dict[str, dict[str, Any]] = {}
        for name in sorted(rules):
            result, detail = evaluate_rule(rules[name], series, bars, index)
            rule_results[name] = result
            rule_details[name] = detail
        signal = "HOLD"
        reason_code = "WARMUP" if index < warmup else "NO_SIGNAL"
        for name in sorted(rules):
            if rule_results[name]:
                output = outputs.get(name, {})
                signal = str(output.get("direction", "HOLD"))
                reason_code = str(output.get("reason_code", f"RULE_{name.upper()}"))
                break
        indicator_values = {name: series[name][index] for name in dsl.get("indicators", {})}
        warmup_active = index < warmup
        if warmup_active:
            signal = "HOLD"
        signal_row = {
            "bar_index": index,
            "symbol": bar.symbol,
            "timestamp": bar.timestamp,
            "direction": signal,
            "strength": Decimal("0"),
            "reason_code": reason_code,
            "price": bar.close,
        }
        if signal in {"BUY", "SELL"} and not warmup_active:
            signals.append(signal_row)
        traces.append(
            {
                "bar_index": index,
                "timestamp": bar.timestamp,
                "ohlcv": {
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                },
                "indicators": indicator_values,
                "rules": rule_results,
                "rule_details": rule_details,
                "signal": signal_row,
                "warmup": warmup_active,
                "candidate_trade": signal in {"BUY", "SELL"} and not warmup_active,
                "ignored_reason": "warmup"
                if warmup_active
                else (None if signal != "HOLD" else "no_rule_true"),
            }
        )
    return {
        "parameters": resolved,
        "warmup_bars": warmup,
        "warnings": warnings,
        "indicators": series,
        "signals": signals,
        "trace": traces,
        "source_hash": source_hash(dsl, resolved),
    }
