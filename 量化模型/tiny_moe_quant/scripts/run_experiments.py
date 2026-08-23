"""run_experiments.py：统一实验入口，自动运行 A0-A4 并生成对比表。

用法:
    python scripts/run_experiments.py --config configs/real_csi300.yaml

自动运行（相同数据 / 相同切分 / 相同标签 / 相同回测规则）:
    A0 Momentum                (score = 过去20日收益，无参数)
    A1 MLP Ranker              (use_moe=False)
    A2 Static MoE              (use_market_gate=False)
    A3 Tiny-MoE V1             (version=v1)
    A4 Tiny-MoE V2             (version=v2)

输出:
    outputs_real/<name>/      每个实验的完整产物
    outputs_real/experiment_summary.csv   最终对比表（Spec 二十九）
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
    p = argparse.ArgumentParser(description="运行 A0-A4 全部实验")
    p.add_argument("--config", default="configs/real_csi300.yaml")
    p.add_argument("--data-dir", default=None, help="默认读 config data.out_dir")
    p.add_argument("--epochs", type=int, default=None)
    p.add_argument("--device", default="auto")
    p.add_argument("--only", default=None, help="只运行部分实验，逗号分隔，如 A3,A4")
    p.add_argument("--cost-bps", type=float, default=None,
                   help="对比表使用的成本档（默认取 config backtest.transaction_cost_bps）")
    p.add_argument("--summary-only", action="store_true",
                   help="只从已有 outputs 生成对比表，不重新训练")
    return p.parse_args()


def build_summary_table(out_root: str, cost_bps: float, names) -> pd.DataFrame:
    """从已有实验的 metrics.json 汇总对比表。"""
    rows = []
    for name in names:
        metrics_path = f"{out_root}/{name}/metrics.json"
        if not os.path.exists(metrics_path):
            print(f"[跳过] 缺少 {metrics_path}")
            continue
        with open(metrics_path, "r", encoding="utf-8") as f:
            m = json.load(f)
        ic = m["ic"]
        bt = m["backtest"][str(int(cost_bps))]
        top = m.get("top_excess_return", {})
        rows.append({
            "Model": name,
            "Params": m["params"]["total"],
            "IC": round(ic["mean_ic"], 4),
            "ICIR": round(ic["icir"], 4),
            "RankIC": round(ic["mean_rank_ic"], 4),
            "RankICIR": round(ic["rank_icir"], 4),
            "Top20ExcessReturn": round(top.get("top_20_excess", float("nan")), 5),
            "CumulativeReturn": round(bt["cum_return"], 4),
            "AnnualReturn": round(bt["annual_return"], 4),
            "Sharpe": round(bt["sharpe"], 3),
            "MaxDD": round(bt["max_drawdown"], 4),
            "Turnover": round(bt["turnover"], 3),
        })
    return pd.DataFrame(rows)


def main() -> None:
    args = parse_args()
    import yaml

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    data_dir = args.data_dir or os.path.join(ROOT, cfg["data"]["out_dir"])
    out_root = os.path.join(ROOT, cfg["output_dir"])
    cost_bps = args.cost_bps if args.cost_bps is not None else int(
        cfg["backtest"]["transaction_cost_bps"]
    )

    experiments = [
        ("A0_momentum", ["--baseline", "momentum", "--version", "v1"]),
        ("A1_mlp", ["--version", "v1", "--no-moe"]),
        ("A2_static_moe", ["--version", "v1", "--no-market-gate"]),
        ("A3_tiny_moe_v1", ["--version", "v1"]),
        ("A4_tiny_moe_v2", ["--version", "v2"]),
    ]
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        experiments = [e for e in experiments if e[0].split("_")[0] in wanted]
    names = [e[0] for e in experiments]

    if args.summary_only:
        table = build_summary_table(out_root, cost_bps, names)
        _print_and_save(table, out_root, cost_bps)
        return

    base = [
        sys.executable, f"{ROOT}/scripts/train.py",
        "--config", f"{ROOT}/{args.config}",
        "--data-dir", data_dir,
        "--device", args.device,
    ]
    if args.epochs:
        base += ["--epochs", str(args.epochs)]

    for name, extra in experiments:
        print(f"\n{'=' * 60}\n>>> 运行实验 {name}\n{'=' * 60}")
        subprocess.run(base + ["--name", name] + extra, check=True)

    table = build_summary_table(out_root, cost_bps, names)
    _print_and_save(table, out_root, cost_bps)


def _print_and_save(table: pd.DataFrame, out_root: str, cost_bps: float) -> None:
    """打印 Markdown 表格并保存 experiment_summary.csv（Spec 二十九）。"""
    summary_path = f"{out_root}/experiment_summary.csv"
    table.to_csv(summary_path, index=False)
    print("\n" + "=" * 100)
    print(f"A0-A4 实验结果对比（回测成本 {cost_bps}bps，同一测试集）")
    print("=" * 100)
    print(table.to_string(index=False))
    print(f"\n对比表已保存: {summary_path}")


if __name__ == "__main__":
    main()
