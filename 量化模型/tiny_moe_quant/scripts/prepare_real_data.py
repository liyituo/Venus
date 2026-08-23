"""prepare_real_data.py：用 QlibAdapter 构建真实数据（CSI300 + Alpha158）。

用法:
    python scripts/prepare_real_data.py --config configs/real_csi300.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import yaml

from src.data.qlib_adapter import QlibAdapter


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="构建真实 CSI300 数据")
    p.add_argument("--config", default="configs/real_csi300.yaml")
    p.add_argument("--out", default=None, help="输出目录（默认 config data.out_dir）")
    p.add_argument("--source", default=None, choices=["qlib", "csv"])
    p.add_argument("--csv-path", default=None, help="csv 模式的数据文件")
    p.add_argument("--benchmark-csv", default=None, help="csv 模式的指数收盘文件 (date, close)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    d = cfg["data"]
    out_dir = args.out or os.path.join(ROOT, d["out_dir"])

    adapter = QlibAdapter(
        source=args.source or d.get("source", "qlib"),
        instruments=d.get("instruments", "csi300"),
        benchmark_symbol=d.get("benchmark_symbol", "SH000300"),
        start_time=d.get("warmup_start", "2014-01-01"),
        end_time=d.get("raw_end", "2024-12-31"),
        horizon=int(d.get("horizon", 5)),
        csv_path=args.csv_path,
        benchmark_csv=args.benchmark_csv,
    )
    meta = adapter.build(out_dir)
    print("\n=== 数据构建完成 ===")
    for k, v in meta.items():
        if k in ("feature_names", "market_feature_names"):
            print(f"  {k}: {len(v)} 个（前 5: {v[:5]}...）")
        else:
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
