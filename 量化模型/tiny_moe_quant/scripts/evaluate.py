"""evaluate.py：从已训练的实验目录重新评估测试集（生成 predictions/metrics/expert_analysis）。

用法:
    python scripts/evaluate.py --experiment outputs/A3_full --data-dir data/processed
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

from src.data.preprocessing import split_by_date
from src.inference.predictor import QuantPredictor
from src.metrics.quant_metrics import daily_ic, summarize_ic


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="用已保存 checkpoint 重新评估测试集")
    p.add_argument("--experiment", required=True, help="实验输出目录，如 outputs/A3_full")
    p.add_argument("--data-dir", default="data/processed")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    with open(f"{args.experiment}/config.yaml", "r", encoding="utf-8") as f:
        import yaml
        cfg = yaml.safe_load(f)
    horizon = int(cfg["data"]["horizon"])

    features = pd.read_csv(f"{args.data_dir}/features.csv", parse_dates=False)
    market = pd.read_csv(f"{args.data_dir}/market_features.csv", parse_dates=False)
    labels = pd.read_csv(f"{args.data_dir}/labels.csv", parse_dates=False)
    prices = pd.read_csv(f"{args.data_dir}/prices.csv", parse_dates=False)
    for df in (features, market, labels, prices):
        df["date"] = df["date"].astype(str)

    feature_names = [c for c in features.columns if c not in ("date", "symbol")]
    market_feature_names = [c for c in market.columns if c != "date"]

    test_f = split_by_date(features, "date", cfg["splits"])["test"]
    test_m = split_by_date(market, "date", cfg["splits"])["test"].set_index("date")
    test_l = split_by_date(labels, "date", cfg["splits"])["test"].set_index(["date", "symbol"])
    test_p = split_by_date(prices, "date", cfg["splits"])["test"]

    predictor = QuantPredictor(f"{args.experiment}/best_model.pt")
    rows = []
    for date, day in test_f.groupby("date", sort=True):
        mkt = test_m.loc[date]
        out = predictor.predict_daily(
            day[feature_names].to_numpy(dtype=np.float64),
            mkt[market_feature_names].to_numpy(dtype=np.float64),
            day["symbol"].tolist(),
        )
        gw = out["gate_weights"] or {}
        for stock in out["stocks"]:
            key = (date, stock["symbol"])
            rows.append({
                "date": date, "symbol": stock["symbol"],
                "label": test_l.loc[key, "label_zscore"] if key in test_l.index else np.nan,
                "future_return_5d": test_l.loc[key, f"future_return_{horizon}d"]
                if key in test_l.index else np.nan,
                "score": stock["score"], "rank": stock["rank"],
                "expert_1_score": stock["expert_scores"][0] if stock["expert_scores"] else np.nan,
                "expert_2_score": stock["expert_scores"][1] if stock["expert_scores"] else np.nan,
                "expert_3_score": stock["expert_scores"][2] if stock["expert_scores"] else np.nan,
                "gate_1": gw.get("expert_1", np.nan),
                "gate_2": gw.get("expert_2", np.nan),
                "gate_3": gw.get("expert_3", np.nan),
            })
    pred = pd.DataFrame(rows)
    pred.to_csv(f"{args.experiment}/predictions.csv", index=False)

    daily = daily_ic(pred["score"].to_numpy(), pred["future_return_5d"].to_numpy(),
                     pred["date"].to_numpy())
    summary = summarize_ic(daily)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    with open(f"{args.experiment}/metrics.json", "r", encoding="utf-8") as f:
        metrics = json.load(f)
    metrics["ic"] = summary
    with open(f"{args.experiment}/metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2, default=str)

    # 回测（默认成本档）
    from src.backtest.backtester import Backtester
    bt_cfg = cfg["backtest"]
    bt = Backtester(top_k=int(bt_cfg["top_k"]), rebalance_days=int(bt_cfg["rebalance_days"]),
                    transaction_cost_bps=float(bt_cfg["transaction_cost_bps"]))
    res = bt.run(pred, test_p)
    bt.plot(res, args.experiment)
    metrics["backtest"][str(bt_cfg["transaction_cost_bps"])] = res.metrics
    with open(f"{args.experiment}/metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2, default=str)
    print(f"回测: Sharpe={res.metrics['sharpe']:.3f} 年化={res.metrics['annual_return']:.4f}")
    print(f"评估完成: {args.experiment}")


if __name__ == "__main__":
    main()
