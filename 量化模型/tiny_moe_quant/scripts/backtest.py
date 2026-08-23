"""backtest.py：基于 predictions.csv 对已训练实验做 Top-K 回测。

用法:
    python scripts/backtest.py --experiment outputs/A3_full --data-dir data/processed
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd
import yaml

from src.backtest.backtester import Backtester
from src.data.preprocessing import split_by_date


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="对已有预测做 Top-K 回测")
    p.add_argument("--experiment", required=True)
    p.add_argument("--data-dir", default="data/processed")
    p.add_argument("--cost-bps", type=float, nargs="+", default=[0, 5, 10, 20])
    return p.parse_args()


def main() -> None:
    args = parse_args()
    with open(f"{args.experiment}/config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    pred = pd.read_csv(f"{args.experiment}/predictions.csv", parse_dates=False)
    prices = pd.read_csv(f"{args.data_dir}/prices.csv", parse_dates=False)
    prices["date"] = prices["date"].astype(str)
    test_prices = split_by_date(prices, "date", cfg["splits"])["test"]

    bt_cfg = cfg["backtest"]
    results = {}
    for bps in args.cost_bps:
        bt = Backtester(top_k=int(bt_cfg["top_k"]),
                        rebalance_days=int(bt_cfg["rebalance_days"]),
                        transaction_cost_bps=float(bps))
        res = bt.run(pred, test_prices)
        bt.plot(res, args.experiment)
        results[str(bps)] = res.metrics
        print(f"cost={bps}bps: {json.dumps(res.metrics, ensure_ascii=False)}")

    with open(f"{args.experiment}/metrics.json", "r", encoding="utf-8") as f:
        metrics = json.load(f)
    metrics["backtest"] = results
    with open(f"{args.experiment}/metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2, default=str)
    print(f"回测完成: {args.experiment}")


if __name__ == "__main__":
    main()
