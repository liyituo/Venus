"""analyze_experts.py：分析 Gate 是否与市场环境相关、Expert 是否自发分工。

输出:
    - expert_analysis.csv（每个交易日 gate + 市场状态 + 各 Expert RankIC）
    - gate_weights.png
    - Gate 权重与市场特征的相关性汇总（控制台 + gate_market_corr.json）

用法:
    python scripts/analyze_experts.py --experiment outputs/A3_full --data-dir data/processed
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
import yaml
from scipy.stats import spearmanr

from src.data.preprocessing import split_by_date
from src.metrics.quant_metrics import daily_ic, summarize_ic


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Expert 分工与 Gate-市场相关性分析")
    p.add_argument("--experiment", required=True)
    p.add_argument("--data-dir", default="data/processed")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    with open(f"{args.experiment}/config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    pred = pd.read_csv(f"{args.experiment}/predictions.csv", parse_dates=False)
    market = pd.read_csv(f"{args.data_dir}/market_features.csv", parse_dates=False)
    market["date"] = market["date"].astype(str)
    test_market = split_by_date(market, "date", cfg["splits"])["test"]

    # 逐日 gate（从 predictions 的 gate 列恢复，若存在）
    gate_cols = [c for c in ("gate_1", "gate_2", "gate_3") if c in pred.columns]
    if not gate_cols:
        print("predictions.csv 中没有 gate 列，无法做 Gate 分析（可能是非 MoE 模型）")
        gate_daily = None
    else:
        gate_daily = pred.groupby("date", sort=True)[gate_cols].first().reset_index()
        gate_daily = gate_daily.merge(test_market, on="date", how="left")

        # Gate 与市场特征相关性（Spearman，逐日序列）
        corr_rows = {}
        market_cols = [c for c in test_market.columns if c != "date"]
        for g in gate_cols:
            corr_rows[g] = {}
            for mcol in market_cols:
                pair = gate_daily[[g, mcol]].dropna()
                if len(pair) >= 10:
                    rho, pval = spearmanr(pair[g], pair[mcol])
                    corr_rows[g][mcol] = {"spearman": float(rho), "p_value": float(pval)}
        print(json.dumps(corr_rows, ensure_ascii=False, indent=2))
        with open(f"{args.experiment}/gate_market_corr.json", "w", encoding="utf-8") as f:
            json.dump(corr_rows, f, ensure_ascii=False, indent=2)

        # Gate 权重图
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(12, 4))
        for i, g in enumerate(gate_cols):
            ax.plot(gate_daily["date"], gate_daily[g], label=g, linewidth=0.8)
        ax.set_title(f"Gate Weights (test, {args.experiment})")
        ax.legend()
        ax.tick_params(axis="x", rotation=45)
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(f"{args.experiment}/gate_weights.png", dpi=120)
        plt.close(fig)

    # 各 Expert 的 RankIC 汇总
    expert_cols = [c for c in ("expert_1_score", "expert_2_score", "expert_3_score")
                   if c in pred.columns and pred[c].notna().any()]
    summary_rows = []
    for col in expert_cols:
        daily = daily_ic(pred[col].to_numpy(), pred["future_return_5d"].to_numpy(),
                         pred["date"].to_numpy())
        s = summarize_ic(daily)
        summary_rows.append({"expert": col.replace("_score", ""), **s})
    daily_final = daily_ic(pred["score"].to_numpy(), pred["future_return_5d"].to_numpy(),
                           pred["date"].to_numpy())
    s = summarize_ic(daily_final)
    summary_rows.append({"expert": "final_model", **s})
    expert_ic = pd.DataFrame(summary_rows)
    print(expert_ic.to_string(index=False))
    expert_ic.to_csv(f"{args.experiment}/expert_rank_ic.csv", index=False)

    # expert_analysis.csv（每个交易日一行）
    if gate_daily is not None:
        out = gate_daily[["date"] + gate_cols]
        for mcol in ("market_return_20d", "market_volatility_20d", "advance_ratio"):
            if mcol in test_market.columns:
                out = out.merge(test_market[["date", mcol]], on="date", how="left")
        for col in expert_cols:
            daily = daily_ic(pred[col].to_numpy(), pred["future_return_5d"].to_numpy(),
                             pred["date"].to_numpy())
            out = out.merge(daily.set_index("date")["rank_ic"].rename(f"rank_ic_{col.replace('_score', '')}"),
                            on="date", how="left")
        out = out.merge(daily_final.set_index("date")["rank_ic"].rename("rank_ic_final"),
                        on="date", how="left")
        out.to_csv(f"{args.experiment}/expert_analysis.csv", index=False)
        print(f"expert_analysis.csv 已保存（{len(out)} 个交易日）")


if __name__ == "__main__":
    main()
