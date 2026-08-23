"""synthetic_demo.py：生成小型人工数据验证全链路（非正式实验，仅代码正确性验证）。

规模: 200 只股票 x 500 个交易日，20 个股票特征，8 个市场特征。
人工注入 3 个市场 regime，以 6 段交替排列（R0 R1 R2 R0 R1 R2），
保证训练/验证/测试区间都覆盖多个 regime，Gate 才能在训练中学到
"市场状态 -> 专家权重" 的映射并泛化到测试期:
    regime 0: feature_1（动量型，AR 强持续）更有效
    regime 1: feature_2（反转型，快速回归）更有效
    regime 2: feature_3（风险型，个股持续性）更有效，且市场波动更高

输出到 data/synthetic/（与 prepare_data.py 相同的处理后格式）:
    features.csv, market_features.csv, labels.csv, prices.csv, meta.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np
import pandas as pd

from src.data.preprocessing import build_labels

N_STOCKS = 200
N_DAYS = 500
N_FEATURES = 20
N_MARKET = 8
HORIZON = 5
BETA = 0.12          # 因子暴露 -> 日收益 的强度（regime 内信号强且干净）
IDIO_VOL = 0.012     # 个股特质波动
# 12 段交替 regime（每段 ~42 天，4 个完整周期），
# 保证 train / valid / test 都覆盖全部 3 个 regime，
# 早停选出的模型不会偏向某个 regime，Gate 也能学到完整的"市场状态 -> 专家权重"映射
REGIME_SEQ = [0, 1, 2] * 4
REGIME_DAYS = []
_block = N_DAYS // 12
for b, r in enumerate(REGIME_SEQ):
    start = b * _block
    end = N_DAYS - 1 if b == 11 else (b + 1) * _block - 1
    REGIME_DAYS.append((r, start, end))
# 各 regime 的市场日收益漂移 / 波动率（让市场特征携带强 regime 信息，Gate 才能学）
MARKET_DRIFT = [0.0030, -0.0030, 0.0005]
MARKET_VOL = [0.006, 0.006, 0.014]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="生成 Tiny-MoE 合成演示数据")
    p.add_argument("--out", default="data/synthetic")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def regime_of(day_idx: int) -> int:
    for r, start, end in REGIME_DAYS:
        if start <= day_idx <= end:
            return r
    return 2


def make_factors(rng: np.random.Generator, n_days: int, n_stocks: int) -> np.ndarray:
    """生成 f1..f20 [n_days, n_stocks, 20]。

    f1: 动量型（AR rho=0.9，强持续）
    f2: 反转型（AR rho=0.1，快速回归）
    f3: 风险型（个股基线 + 小噪声，横截面持久）
    f4..f20: 纯噪声
    """
    n = n_days * n_stocks
    eps = rng.normal(0, 1, (n_days, n_stocks))
    f1 = np.zeros((n_days, n_stocks))
    for t in range(1, n_days):
        f1[t] = 0.9 * f1[t - 1] + eps[t]
    eps2 = rng.normal(0, 1, (n_days, n_stocks))
    f2 = np.zeros((n_days, n_stocks))
    for t in range(1, n_days):
        f2[t] = 0.1 * f2[t - 1] + eps2[t]
    base3 = rng.normal(0, 1, n_stocks)  # 每只股票的风险基线（持久）
    f3 = base3[None, :] + 0.2 * rng.normal(0, 1, (n_days, n_stocks))
    f4_20 = rng.normal(0, 1, (n_days, n_stocks, N_FEATURES - 3))

    factors = np.concatenate(
        [f1[:, :, None], f2[:, :, None], f3[:, :, None], f4_20], axis=2
    )
    # 逐日横截面标准化（与真实预处理一致，保持量纲可比）
    for t in range(n_days):
        factors[t] = (factors[t] - factors[t].mean(axis=0)) / (
            factors[t].std(axis=0) + 1e-8
        )
    return factors  # [n_days, n_stocks, 20]


def main() -> None:
    args = parse_args()
    rng = np.random.default_rng(args.seed)

    dates = pd.bdate_range("2015-01-05", periods=N_DAYS).strftime("%Y-%m-%d").tolist()
    symbols = [f"SYM_{i:04d}" for i in range(N_STOCKS)]

    # ---- 市场日收益（带 regime 漂移/波动） ----
    market_ret = np.zeros(N_DAYS)
    for t in range(N_DAYS):
        r = regime_of(t)
        market_ret[t] = rng.normal(MARKET_DRIFT[r], MARKET_VOL[r])

    # ---- 股票特征 ----
    factors = make_factors(rng, N_DAYS, N_STOCKS)  # [T, N, 20]

    # ---- 股票日收益：受 regime 对应的特征驱动 ----
    stock_ret = np.zeros((N_DAYS, N_STOCKS))
    for t in range(1, N_DAYS):
        r = regime_of(t)
        active = factors[t - 1, :, r]  # 用前一日特征值（避免同期信息）
        stock_ret[t] = market_ret[t] + BETA * active + rng.normal(0, IDIO_VOL, N_STOCKS)
    stock_ret[0] = market_ret[0] + rng.normal(0, IDIO_VOL, N_STOCKS)

    # ---- 价格 ----
    close = 100.0 * np.exp(np.cumsum(stock_ret, axis=0))  # [T, N]

    # ---- 整理为长表 ----
    rows = []
    for t, date in enumerate(dates):
        for i, sym in enumerate(symbols):
            rows.append([date, sym] + [float(factors[t, i, k]) for k in range(N_FEATURES)])
    features = pd.DataFrame(
        rows, columns=["date", "symbol"] + [f"f{k + 1}" for k in range(N_FEATURES)]
    )
    prices = pd.DataFrame(
        {
            "date": np.repeat(dates, N_STOCKS),
            "symbol": np.tile(symbols, N_DAYS),
            "close": close.reshape(-1),
        }
    )

    # ---- 市场特征（从模拟横截面推导，保持真实结构） ----
    daily_ret = pd.DataFrame(
        {
            "date": np.repeat(dates, N_STOCKS),
            "symbol": np.tile(symbols, N_DAYS),
            "ret_1d": stock_ret.reshape(-1),
        }
    )
    mkt_close = pd.Series(close.mean(axis=1), index=dates)
    mkt_ret_1d = pd.Series(market_ret, index=dates)
    volume = rng.lognormal(mean=0.0, sigma=0.3, size=(N_DAYS, N_STOCKS))  # 相对量
    mkt_volume = volume.mean(axis=1)
    advance = (stock_ret > 0).mean(axis=1)
    dispersion = stock_ret.std(axis=1, ddof=0)

    market = pd.DataFrame(
        {
            "date": dates,
            "market_return_1d": mkt_ret_1d.to_numpy(),
            "market_return_5d": mkt_close.pct_change(5).to_numpy(),
            "market_return_20d": mkt_close.pct_change(20).to_numpy(),
            "market_volatility_5d": mkt_ret_1d.rolling(5).std().to_numpy(),
            "market_volatility_20d": mkt_ret_1d.rolling(20).std().to_numpy(),
            "advance_ratio": advance,
            "cross_section_dispersion": dispersion,
            "market_volume_ratio": mkt_volume
            / pd.Series(mkt_volume, index=dates).rolling(20).mean().to_numpy(),
        }
    )
    # 去掉滚动窗口不足的初始行
    market = market.iloc[20:].reset_index(drop=True)
    features = features[features["date"] >= market["date"].min()].reset_index(drop=True)
    prices = prices[prices["date"] >= market["date"].min()].reset_index(drop=True)

    # ---- 标签（复用正式预处理代码，同时验证其防泄漏逻辑） ----
    labels = build_labels(features, prices, horizon=HORIZON)

    os.makedirs(args.out, exist_ok=True)
    features.to_csv(f"{args.out}/features.csv", index=False)
    market.to_csv(f"{args.out}/market_features.csv", index=False)
    labels.to_csv(f"{args.out}/labels.csv", index=False)
    prices.to_csv(f"{args.out}/prices.csv", index=False)

    # 建议时间切分（60% / 15% / 25%），供 run_all_experiments.py 使用
    split_ends = {
        "train_start": dates[0],
        "train_end": dates[300],
        "valid_start": dates[301],
        "valid_end": dates[375],
        "test_start": dates[376],
        "test_end": dates[-1],
    }
    with open(f"{args.out}/meta.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "n_stocks": N_STOCKS, "n_days": N_DAYS,
                "n_features": N_FEATURES, "n_market_features": N_MARKET,
                "horizon": HORIZON, "seed": args.seed,
                "regime_days": {f"regime_{r}": [dates[s], dates[e]]
                                for r, s, e in REGIME_DAYS},
                "suggested_splits": split_ends,
            },
            f, ensure_ascii=False, indent=2,
        )
    print(f"合成数据已生成: {args.out}")
    print(f"  features: {features.shape}, market: {market.shape}, "
          f"labels: {labels.shape}, prices: {prices.shape}")
    print(f"  建议切分: train_end={split_ends['train_end']}, "
          f"valid_end={split_ends['valid_end']}, test_end={split_ends['test_end']}")
    print("  运行完整实验: python scripts/run_all_experiments.py --data-dir data/synthetic")


if __name__ == "__main__":
    main()
