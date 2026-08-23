"""run_all_experiments.py：依次运行第一阶段全部实验并输出对比表。

    A0 Momentum                (score = 过去20日收益，无参数)
    A1 MLP Ranker              (use_moe=False)
    A2 Tiny-MoE w/o Market Gate(use_market_gate=False)
    A3 Full Tiny-MoE

相同数据 / 相同切分 / 相同标签 / 相同回测规则。

用法:
    python scripts/run_all_experiments.py --data-dir data/synthetic [--epochs 30]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="运行 A0-A3 全部实验")
    p.add_argument("--data-dir", default="data/synthetic")
    p.add_argument("--epochs", type=int, default=None)
    p.add_argument("--config", default="configs/default.yaml")
    p.add_argument("--device", default="auto")
    p.add_argument("--cost-bps", type=float, default=10.0, help="对比表使用的成本档")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # 从 meta.json 读取建议切分
    meta_path = f"{args.data_dir}/meta.json"
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        s = meta["suggested_splits"]
        split_args = [
            "--train-end", s["train_end"],
            "--valid-start", s["valid_start"],
            "--valid-end", s["valid_end"],
            "--test-start", s["test_start"],
            "--test-end", s["test_end"],
        ]
    else:
        split_args = []

    base = [
        sys.executable, f"{ROOT}/scripts/train.py",
        "--config", f"{ROOT}/{args.config}",
        "--data-dir", f"{ROOT}/{args.data_dir}",
        "--device", args.device,
    ] + split_args
    if args.epochs:
        base += ["--epochs", str(args.epochs)]

    experiments = [
        ("A0_momentum", ["--baseline", "momentum"]),
        ("A1_mlp", ["--no-moe"]),
        ("A2_no_market_gate", ["--no-market-gate"]),
        ("A3_full_tiny_moe", []),
    ]

    out_root = f"{ROOT}/outputs"
    rows = []
    for name, extra in experiments:
        print(f"\n{'=' * 60}\n>>> 运行实验 {name}\n{'=' * 60}")
        subprocess.run(base + ["--name", name] + extra, check=True)
        with open(f"{out_root}/{name}/metrics.json", "r", encoding="utf-8") as f:
            m = json.load(f)
        ic = m["ic"]
        bt = m["backtest"][str(int(args.cost_bps))]
        rows.append({
            "Model": name,
            "IC": round(ic["mean_ic"], 4),
            "RankIC": round(ic["mean_rank_ic"], 4),
            "RankICIR": round(ic["rank_icir"], 4),
            "Annual Return": round(bt["annual_return"], 4),
            "Sharpe": round(bt["sharpe"], 3),
            "Max Drawdown": round(bt["max_drawdown"], 4),
            "Turnover": round(bt["turnover"], 3),
            "Parameters": m["params"]["total"],
        })

    table = pd.DataFrame(rows)
    print("\n" + "=" * 90)
    print(f"第一阶段实验结果对比（回测成本 {args.cost_bps:.0f}bps）")
    print("=" * 90)
    print(table.to_string(index=False))
    table.to_csv(f"{out_root}/comparison_A0_A3.csv", index=False)
    print(f"\n对比表已保存: {out_root}/comparison_A0_A3.csv")


if __name__ == "__main__":
    main()
