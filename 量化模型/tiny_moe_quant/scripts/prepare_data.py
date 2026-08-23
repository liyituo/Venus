"""prepare_data.py：原始 OHLCV CSV/parquet -> 特征/标签/价格 处理文件。

输入格式至少包含列: date, symbol, close
可选: open, high, low, volume, shares_outstanding

输出到 data/processed/:
    features.csv         [date, symbol, stock features...]
    market_features.csv  [date, market features...]
    labels.csv           [date, symbol, future_return_5d, benchmark_return, excess_return, label_zscore]
    prices.csv           [date, symbol, close]
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd
import yaml

from src.data.feature_builder import FeatureBuilder
from src.data.preprocessing import build_labels


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="从 OHLCV 原始数据构建特征与标签")
    p.add_argument("--input", required=True, help="原始数据文件 (csv/parquet)")
    p.add_argument("--config", default="configs/default.yaml")
    p.add_argument("--out", default="data/processed", help="输出目录")
    p.add_argument("--lookback", type=int, default=None, help="覆盖 config 的 lookback")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    lookback = args.lookback if args.lookback is not None else int(cfg["data"]["lookback"])
    horizon = int(cfg["data"]["horizon"])

    if args.input.endswith(".parquet"):
        raw = pd.read_parquet(args.input)
    else:
        raw = pd.read_csv(args.input, parse_dates=False)
    raw["date"] = raw["date"].astype(str)
    print(f"原始数据: {raw.shape}, 列: {list(raw.columns)}")

    os.makedirs(args.out, exist_ok=True)

    builder = FeatureBuilder(lookback=lookback)
    stock_feat, market_feat, feature_names, market_feature_names = builder.build(raw)
    print(f"股票特征: {stock_feat.shape}, 特征: {feature_names}")
    print(f"市场特征: {market_feat.shape}, 特征: {market_feature_names}")

    labels = build_labels(stock_feat, raw, horizon=horizon)
    print(f"标签: {labels.shape}")

    prices = raw[["date", "symbol", "close"]].copy()
    stock_feat.to_csv(f"{args.out}/features.csv", index=False)
    market_feat.to_csv(f"{args.out}/market_features.csv", index=False)
    labels.to_csv(f"{args.out}/labels.csv", index=False)
    prices.to_csv(f"{args.out}/prices.csv", index=False)

    with open(f"{args.out}/meta.json", "w", encoding="utf-8") as f:
        import json

        json.dump(
            {"feature_names": feature_names, "market_feature_names": market_feature_names,
             "horizon": horizon, "lookback": lookback},
            f, ensure_ascii=False, indent=2,
        )
    print(f"处理完成，输出目录: {args.out}")


if __name__ == "__main__":
    main()
