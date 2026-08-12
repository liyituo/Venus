from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_DOWN, Decimal
from typing import Any

from quant_agent.domain.models import MarketBar
from quant_agent.research.dsl import run_strategy


def _money(value: Decimal) -> str:
    return format(value, "f")


def _drawdown(equity: Decimal, peak: Decimal) -> Decimal:
    if peak <= 0:
        return Decimal("0")
    return equity / peak - Decimal("1")


def run_backtest(
    *,
    dsl: Mapping[str, Any],
    parameters: Mapping[str, Any] | None,
    bars: Sequence[MarketBar],
    run_id: str,
    initial_cash: Decimal,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    max_position_notional: Decimal | None = None,
) -> dict[str, Any]:
    if initial_cash <= 0:
        raise ValueError("initial_cash must be positive")
    if fee_bps < 0 or slippage_bps < 0:
        raise ValueError("fee_bps and slippage_bps must be non-negative")
    if len(bars) > 500:
        raise ValueError("BACKTEST_DATA_LIMIT: at most 500 bars are allowed")
    strategy = run_strategy(dsl, parameters, bars)
    signal_by_index = {int(signal["bar_index"]): signal for signal in strategy["signals"]}
    cash = initial_cash
    quantity = Decimal("0")
    total_fees = Decimal("0")
    total_slippage = Decimal("0")
    turnover = Decimal("0")
    peak_equity = initial_cash
    equity_curve: list[dict[str, Any]] = []
    drawdown_curve: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    open_trade: dict[str, Any] | None = None
    fee_rate = fee_bps / Decimal("10000")
    slippage_rate = slippage_bps / Decimal("10000")

    for index, bar in enumerate(bars):
        if index > 0 and index - 1 in signal_by_index:
            signal = signal_by_index[index - 1]
            direction = signal["direction"]
            if direction == "BUY" and quantity == 0:
                execution_price = bar.open * (Decimal("1") + slippage_rate)
                available_notional = cash / (Decimal("1") + fee_rate)
                desired_notional = (
                    available_notional
                    if max_position_notional is None
                    else min(available_notional, max_position_notional)
                )
                notional = min(desired_notional, available_notional)
                bought = (notional / execution_price).quantize(Decimal("1"), rounding=ROUND_DOWN)
                if bought > 0:
                    gross = bought * execution_price
                    fee = gross * fee_rate
                    cash -= gross + fee
                    quantity = bought
                    total_fees += fee
                    slippage_cost = bought * (execution_price - bar.open)
                    total_slippage += slippage_cost
                    turnover += gross
                    open_trade = {
                        "entry_index": index,
                        "entry_timestamp": bar.timestamp,
                        "entry_price": execution_price,
                        "quantity": bought,
                        "entry_fee": fee,
                        "signal_reason_code": signal["reason_code"],
                    }
            elif direction == "SELL" and quantity > 0:
                execution_price = bar.open * (Decimal("1") - slippage_rate)
                gross = quantity * execution_price
                fee = gross * fee_rate
                cash += gross - fee
                total_fees += fee
                slippage_cost = quantity * (bar.open - execution_price)
                total_slippage += slippage_cost
                turnover += gross
                if open_trade is not None:
                    entry_gross = open_trade["quantity"] * open_trade["entry_price"]
                    net_pnl = gross - fee - entry_gross - open_trade["entry_fee"]
                    trades.append(
                        {
                            **open_trade,
                            "exit_index": index,
                            "exit_timestamp": bar.timestamp,
                            "exit_price": execution_price,
                            "exit_fee": fee,
                            "gross_pnl": gross - entry_gross,
                            "net_pnl": net_pnl,
                        }
                    )
                quantity = Decimal("0")
                open_trade = None
        equity = cash + quantity * bar.close
        peak_equity = max(peak_equity, equity)
        drawdown = _drawdown(equity, peak_equity)
        equity_curve.append(
            {
                "timestamp": bar.timestamp,
                "equity": equity,
                "cash": cash,
                "quantity": quantity,
                "price": bar.close,
            }
        )
        drawdown_curve.append({"timestamp": bar.timestamp, "drawdown": drawdown})

    if quantity > 0 and open_trade is not None:
        final_bar = bars[-1]
        mark_value = quantity * final_bar.close
        trades.append(
            {
                **open_trade,
                "exit_index": None,
                "exit_timestamp": None,
                "exit_price": final_bar.close,
                "exit_fee": Decimal("0"),
                "gross_pnl": mark_value - open_trade["quantity"] * open_trade["entry_price"],
                "net_pnl": mark_value
                - open_trade["quantity"] * open_trade["entry_price"]
                - open_trade["entry_fee"],
                "status": "OPEN_AT_END",
            }
        )
    final_equity = equity_curve[-1]["equity"] if equity_curve else initial_cash
    first_close = bars[0].close if bars else Decimal("0")
    last_close = bars[-1].close if bars else Decimal("0")
    benchmark_return = Decimal("0") if first_close <= 0 else last_close / first_close - Decimal("1")
    total_return = final_equity / initial_cash - Decimal("1")
    max_drawdown = min((item["drawdown"] for item in drawdown_curve), default=Decimal("0"))
    returns: list[Decimal] = []
    for previous, current in zip(equity_curve, equity_curve[1:], strict=False):
        if previous["equity"] > 0:
            returns.append(current["equity"] / previous["equity"] - Decimal("1"))
    volatility: str | Decimal
    sharpe: str | Decimal
    if len(returns) < 2:
        volatility = "N/A"
        sharpe = "N/A"
    else:
        average = sum(returns, Decimal("0")) / Decimal(len(returns))
        variance = sum((item - average) ** 2 for item in returns) / Decimal(len(returns) - 1)
        volatility = variance.sqrt()
        sharpe = "N/A" if volatility == 0 else average / volatility * Decimal(len(returns)).sqrt()
    winning = [trade for trade in trades if trade["net_pnl"] > 0]
    losing = [trade for trade in trades if trade["net_pnl"] < 0]
    gross_profit = sum((trade["net_pnl"] for trade in winning), Decimal("0"))
    gross_loss = abs(sum((trade["net_pnl"] for trade in losing), Decimal("0")))
    win_rate: str | Decimal = "N/A" if not trades else Decimal(len(winning)) / Decimal(len(trades))
    profit_factor: str | Decimal = "N/A" if gross_loss == 0 else gross_profit / gross_loss
    metrics = {
        "total_return": total_return,
        "benchmark_return": benchmark_return,
        "max_drawdown": max_drawdown,
        "volatility": volatility,
        "sharpe_ratio": sharpe,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "trade_count": len(trades),
        "turnover": turnover,
        "fees": total_fees,
        "slippage_cost": total_slippage,
        "final_cash": cash,
        "final_position_quantity": quantity,
        "final_equity": final_equity,
    }
    return {
        "run_id": run_id,
        "status": "COMPLETED",
        "metrics": metrics,
        "equity_curve": tuple(equity_curve),
        "drawdown_curve": tuple(drawdown_curve),
        "trades": tuple(trades),
        "signals": tuple(strategy["signals"]),
        "trace": tuple(strategy["trace"]),
        "warmup_bars": strategy["warmup_bars"],
        "source_hash": strategy["source_hash"],
        "assumptions": (
            "Signal is evaluated after the current bar closes.",
            "Execution occurs at the next bar open.",
            "Long-only; no leverage, margin, or naked shorting.",
            "Fees and slippage are applied deterministically in basis points.",
            "Insufficient samples return N/A instead of annualized estimates.",
        ),
        "formulas": {
            "total_return": "final_equity / initial_cash - 1",
            "benchmark_return": "last_close / first_close - 1",
            "max_drawdown": "minimum(equity / running_peak - 1)",
            "sharpe_ratio": "mean(period_returns) / sample_std(period_returns) * sqrt(n)",
            "win_rate": "winning_trades / closed_or_marked_trades",
            "profit_factor": "gross_profit / abs(gross_loss)",
        },
    }
