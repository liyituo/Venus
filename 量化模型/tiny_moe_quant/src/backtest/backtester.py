"""轻量 Top-K 等权调仓回测器。

规则（第一阶段）:
  - 每 rebalance_days 个交易日调仓一次
  - 每次按模型 score 排序取 Top-K，等权持仓
  - score 在交易日 t 收盘后可得，持仓从 t+1 日收益开始生效
  - 交易成本按换手率 * bps 在该次调仓生效首日扣除一次（不重复扣）
  - benchmark = 全股票池等权日收益
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

import matplotlib

matplotlib.use("Agg")  # 无显示环境（服务器）下绘图
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ANNUAL_DAYS = 252


@dataclass
class BacktestResult:
    """回测结果。"""

    metrics: Dict[str, float] = field(default_factory=dict)
    daily_returns: pd.Series = field(default_factory=pd.Series)  # 策略日收益
    benchmark_returns: pd.Series = field(default_factory=pd.Series)  # 基准日收益
    equity: pd.Series = field(default_factory=pd.Series)
    benchmark_equity: pd.Series = field(default_factory=pd.Series)
    drawdown: pd.Series = field(default_factory=pd.Series)
    turnover_per_rebalance: List[float] = field(default_factory=list)


class Backtester:
    """Top-K 等权回测器。

    输入:
        scores_df: [date, symbol, score]（score 在 date 收盘后可知）
        prices_df: [date, symbol, close]
    """

    def __init__(
        self,
        top_k: int = 20,
        rebalance_days: int = 5,
        transaction_cost_bps: float = 10.0,
    ) -> None:
        self.top_k = top_k
        self.rebalance_days = rebalance_days
        self.transaction_cost_bps = transaction_cost_bps

    def run(self, scores_df: pd.DataFrame, prices_df: pd.DataFrame) -> BacktestResult:
        # 1. 逐股日收益（当日收盘相对前一日收盘）
        px = prices_df[["date", "symbol", "close"]].sort_values(["symbol", "date"]).copy()
        px["ret_1d"] = px.groupby("symbol", sort=False)["close"].pct_change()
        ret = px.dropna(subset=["ret_1d"])[["date", "symbol", "ret_1d"]]

        # 日期轴来自价格表（pct_change 会丢掉每只股票的首日，不能用作日期轴）
        dates = sorted(prices_df["date"].astype(str).unique().tolist())
        n = len(dates)
        if n == 0:
            raise ValueError("回测区间内没有可用的价格数据")

        # 按日期分组，避免逐日对全表做索引查找
        ret_by_date: Dict[str, pd.Series] = {
            d: g.set_index("symbol")["ret_1d"] for d, g in ret.groupby("date", sort=True)
        }
        score_by_date: Dict[str, pd.Series] = {
            d: g.set_index("symbol")["score"]
            for d, g in scores_df.groupby("date", sort=True)
        }

        daily_returns = np.zeros(n)
        benchmark_returns = np.zeros(n)
        turnover_list: List[float] = []
        holdings: List[str] = []

        for k in range(1, n):  # 第 0 天没有历史持仓，收益记为 0
            date = dates[k]
            day_ret = ret_by_date.get(date, pd.Series(dtype=float))
            benchmark_returns[k] = float(day_ret.mean()) if len(day_ret) else 0.0

            # 最近一次调仓日（严格早于当前交易日）
            r_idx = ((k - 1) // self.rebalance_days) * self.rebalance_days

            if (k - 1) % self.rebalance_days == 0:
                # 新调仓：按 r_date 的 score 取 Top-K
                new_holdings = self._select_top_k(score_by_date, dates[r_idx])
                if holdings:
                    turnover = 1.0 - len(set(holdings) & set(new_holdings)) / max(
                        len(holdings), 1
                    )
                else:
                    turnover = 1.0 if new_holdings else 0.0
                turnover_list.append(turnover)
                holdings = new_holdings

            if holdings:
                held_ret = [day_ret[sym] for sym in holdings if sym in day_ret.index]
                port_ret = float(np.mean(held_ret)) if held_ret else 0.0
            else:
                port_ret = 0.0
            # 交易成本只在该次调仓生效首日扣除一次
            if (k - 1) % self.rebalance_days == 0 and turnover_list:
                port_ret -= turnover_list[-1] * self.transaction_cost_bps / 10000.0
            daily_returns[k] = port_ret

        return self._build_result(
            daily_returns, benchmark_returns, dates, turnover_list
        )

    # ------------------------------------------------------------------ #
    def _select_top_k(self, score_by_date: Dict[str, pd.Series], date: str) -> List[str]:
        """取某日 score 最高的 Top-K 股票（score 在 date 收盘后已知）。"""
        day_scores = score_by_date.get(date)
        if day_scores is None:
            return []
        day_scores = day_scores.dropna().sort_values(ascending=False)
        return day_scores.index[: self.top_k].tolist()

    def _build_result(
        self,
        daily_returns: np.ndarray,
        benchmark_returns: np.ndarray,
        dates: List[str],
        turnover_list: List[float],
    ) -> BacktestResult:
        result = BacktestResult()
        result.daily_returns = pd.Series(daily_returns, index=dates)
        result.benchmark_returns = pd.Series(benchmark_returns, index=dates)
        result.equity = pd.Series(np.cumprod(1.0 + daily_returns), index=dates)
        result.benchmark_equity = pd.Series(np.cumprod(1.0 + benchmark_returns), index=dates)
        eq = result.equity.to_numpy()
        result.drawdown = pd.Series(eq / np.maximum.accumulate(eq) - 1.0, index=dates)
        result.turnover_per_rebalance = turnover_list

        n_days = len(daily_returns)
        total = float(np.prod(1.0 + daily_returns) - 1.0)
        ann_return = float((1.0 + total) ** (ANNUAL_DAYS / n_days) - 1.0) if n_days else 0.0
        std = float(np.std(daily_returns, ddof=1))
        ann_vol = std * np.sqrt(ANNUAL_DAYS)
        sharpe = float(np.mean(daily_returns) / (std + 1e-12) * np.sqrt(ANNUAL_DAYS))
        max_dd = float(result.drawdown.min())
        calmar = ann_return / abs(max_dd) if max_dd < 0 else 0.0

        bench_total = float(np.prod(1.0 + benchmark_returns) - 1.0)
        bench_ann = float((1.0 + bench_total) ** (ANNUAL_DAYS / n_days) - 1.0) if n_days else 0.0

        result.metrics = {
            "cum_return": total,
            "annual_return": ann_return,
            "annual_volatility": ann_vol,
            "sharpe": sharpe,
            "max_drawdown": max_dd,
            "calmar": calmar,
            "turnover": float(np.mean(turnover_list)) if turnover_list else 0.0,
            "n_rebalances": len(turnover_list),
            "benchmark_cum_return": bench_total,
            "benchmark_annual_return": bench_ann,
            "n_days": n_days,
            "top_k": self.top_k,
            "rebalance_days": self.rebalance_days,
            "transaction_cost_bps": self.transaction_cost_bps,
        }
        return result

    # ------------------------------------------------------------------ #
    def plot(self, result: BacktestResult, output_dir: str) -> None:
        """保存策略/基准累计收益曲线与回撤曲线 PNG。"""
        fig, ax = plt.subplots(2, 1, figsize=(10, 9))
        ax[0].plot(result.equity.index, result.equity.values, label="Strategy")
        ax[0].plot(
            result.benchmark_equity.index,
            result.benchmark_equity.values,
            label="Benchmark (equal-weight)",
            alpha=0.8,
        )
        ax[0].set_title(f"Equity Curve (Top-{self.top_k}, cost={self.transaction_cost_bps}bps)")
        ax[0].set_ylabel("Cumulative Return")
        ax[0].legend()
        ax[0].grid(alpha=0.3)

        ax[1].fill_between(
            result.drawdown.index, result.drawdown.values, 0, color="red", alpha=0.4
        )
        ax[1].set_title("Drawdown")
        ax[1].set_ylabel("Drawdown")
        ax[1].grid(alpha=0.3)

        fig.tight_layout()
        fig.savefig(f"{output_dir}/equity_curve.png", dpi=120)
        plt.close(fig)

        fig2, ax2 = plt.subplots(figsize=(10, 4))
        ax2.fill_between(result.drawdown.index, result.drawdown.values, 0, color="red", alpha=0.4)
        ax2.set_title("Drawdown Curve")
        ax2.grid(alpha=0.3)
        fig2.tight_layout()
        fig2.savefig(f"{output_dir}/drawdown.png", dpi=120)
        plt.close(fig2)
