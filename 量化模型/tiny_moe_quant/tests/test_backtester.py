"""回测器测试：Top-K 选择、日收益计算、benchmark、交易成本、调仓频率。"""
import numpy as np
import pandas as pd
import pytest

from src.backtest.backtester import Backtester

# A: 每天 +1%，B: 每天 -1%，C: 每天 0%
RETS = {"A": 0.01, "B": -0.01, "C": 0.0}
N_DAYS = 12


def make_data(top_rank: str = "A") -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = pd.bdate_range("2015-01-05", periods=N_DAYS).strftime("%Y-%m-%d").tolist()
    # 价格（从第 0 天收盘开始）
    rows = []
    for sym, r in RETS.items():
        price = 100.0
        for d in dates:
            rows.append({"date": d, "symbol": sym, "close": price})
            price *= 1.0 + r
    prices = pd.DataFrame(rows)

    # score: top_rank 股票始终最高（且每天分数稳定），其余随机但低于它
    rng = np.random.default_rng(0)
    score_rows = []
    for d in dates:
        order = ["A", "B", "C"]
        base = {s: float(rng.uniform(-1, 0)) for s in order if s != top_rank}
        base[top_rank] = 5.0
        for sym in order:
            score_rows.append({"date": d, "symbol": sym, "score": base[sym]})
    scores = pd.DataFrame(score_rows)
    return scores, prices


def test_topk_selection_and_daily_returns():
    """Top-1 永远选中 A：除第一天外每日收益应恰好为 +1%。"""
    scores, prices = make_data(top_rank="A")
    bt = Backtester(top_k=1, rebalance_days=1, transaction_cost_bps=0)
    res = bt.run(scores, prices)
    assert np.allclose(res.daily_returns.to_numpy()[1:], 0.01, atol=1e-9)
    # 第一日无历史持仓
    assert res.daily_returns.to_numpy()[0] == 0.0
    expected_cum = (1.01 ** (N_DAYS - 1)) - 1
    assert np.isclose(res.metrics["cum_return"], expected_cum, atol=1e-9)


def test_benchmark_is_equal_weight_pool():
    """benchmark = 全股票池等权日收益。"""
    scores, prices = make_data()
    bt = Backtester(top_k=1, rebalance_days=1, transaction_cost_bps=0)
    res = bt.run(scores, prices)
    expected_bench = (1.01 + 0.99 + 1.0) / 3.0 - 1.0  # 0
    assert np.allclose(res.benchmark_returns.to_numpy()[1:], expected_bench, atol=1e-9)


def test_transaction_cost_deducted_once():
    """成本 = 换手率 * bps，只在调仓生效首日扣一次。

    构造：rebalance_days=2，Top-1 在 dates[2] 处从 A 切换到 B（换手率 = 1.0），
    cost=100bps：调仓生效日扣 1%，非调仓日不重复扣。
    """
    dates = pd.bdate_range("2015-01-05", periods=8).strftime("%Y-%m-%d").tolist()
    prices = pd.DataFrame(
        [
            # A 每天 +1%，B 价格不变（收益 0%）
            {"date": d, "symbol": s,
             "close": 100.0 * (1.01 ** k) if s == "A" else 100.0}
            for k, d in enumerate(dates)
            for s in ("A", "B")
        ]
    )
    # 分数：dates[0..1] 选 A，dates[2..] 选 B
    score_rows = []
    for k, d in enumerate(dates):
        top = "A" if k < 2 else "B"
        score_rows.append({"date": d, "symbol": top, "score": 1.0})
        other = "B" if top == "A" else "A"
        score_rows.append({"date": d, "symbol": other, "score": 0.0})
    scores = pd.DataFrame(score_rows)

    bt = Backtester(top_k=1, rebalance_days=2, transaction_cost_bps=100.0)
    res = bt.run(scores, prices)
    # k=1: 调仓（取 dates[0] 的 Top-1=A），A 当日收益 +1%，扣成本 1% -> 0
    assert np.isclose(res.daily_returns.iloc[1], 0.0, atol=1e-9)
    # k=2: 非调仓日，不扣成本，持仓 A 收益 +1%
    assert np.isclose(res.daily_returns.iloc[2], 0.01, atol=1e-9)
    # k=3: 调仓（取 dates[2] 的 Top-1=B），B 收益 0%，扣成本 1% -> -1%
    assert np.isclose(res.daily_returns.iloc[3], -0.01, atol=1e-9)
    # k=4: 非调仓日，无成本，持仓 B 收益 0%
    assert np.isclose(res.daily_returns.iloc[4], 0.0, atol=1e-9)


def test_rebalance_every_5_days():
    """score 只在第 0 天给出：持仓保持 A 到下一次调仓（每 5 天），
    第 6 天调仓时无新 score -> 清仓。"""
    dates = pd.bdate_range("2015-01-05", periods=12).strftime("%Y-%m-%d").tolist()
    prices = pd.DataFrame(
        [
            {"date": d, "symbol": s, "close": 100.0 * (1 + RETS[s]) ** k}
            for k, d in enumerate(dates)
            for s in RETS
        ]
    )
    scores = pd.DataFrame(
        [
            {"date": dates[0], "symbol": "A", "score": 1.0},
            {"date": dates[0], "symbol": "B", "score": 0.0},
            {"date": dates[0], "symbol": "C", "score": 0.0},
        ]
    )
    bt = Backtester(top_k=1, rebalance_days=5, transaction_cost_bps=0)
    res = bt.run(scores, prices)
    # 第 1-5 天持仓 A（+1%）；第 6 天调仓时无 score -> 清仓（0%）
    assert np.allclose(res.daily_returns.to_numpy()[1:6], 0.01, atol=1e-9)
    assert np.allclose(res.daily_returns.to_numpy()[6:], 0.0, atol=1e-9)


def test_missing_price_symbol_skipped():
    """Top-K 中的股票某天无价格时，该天收益按剩余持仓计算，不应报错。"""
    dates = pd.bdate_range("2015-01-05", periods=6).strftime("%Y-%m-%d").tolist()
    prices = pd.DataFrame(
        [
            {"date": d, "symbol": "A", "close": 100.0 * 1.01 ** k}
            for k, d in enumerate(dates)
        ]
        + [
            {"date": d, "symbol": "B", "close": 100.0}
            for d in dates[:3]  # B 只有前 3 天价格
        ]
    )
    scores = pd.DataFrame(
        [
            {"date": d, "symbol": "A", "score": 1.0}
            for d in dates
        ]
        + [
            {"date": d, "symbol": "B", "score": 0.9}
            for d in dates
        ]
    )
    bt = Backtester(top_k=2, rebalance_days=1, transaction_cost_bps=0)
    res = bt.run(scores, prices)
    assert np.isfinite(res.metrics["cum_return"])
    # 第 4 天起只有 A 有价格：等权 2 只 -> 只剩 A 时收益 = A 的收益
    assert np.isclose(res.daily_returns.iloc[4], 0.01, atol=1e-9)


def test_metrics_fields_present():
    """输出指标齐全（Spec 十六）。"""
    scores, prices = make_data()
    bt = Backtester(top_k=1, rebalance_days=5, transaction_cost_bps=10)
    res = bt.run(scores, prices)
    for key in ("cum_return", "annual_return", "annual_volatility", "sharpe",
                "max_drawdown", "calmar", "turnover"):
        assert key in res.metrics
        assert np.isfinite(res.metrics[key])
