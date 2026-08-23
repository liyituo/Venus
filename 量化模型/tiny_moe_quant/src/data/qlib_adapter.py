"""QlibAdapter: Qlib / 本地 CSV → 统一 DataFrame（模型层不感知数据来源）。

数据流:
    Qlib cn_data (CSI300) 或 本地 CSV OHLCV
        ↓ QlibAdapter.build()
    统一格式（与 prepare_data.py 输出一致，训练管线完全复用）:
        features.csv         [date, symbol, feature_1..feature_F]
        market_features.csv  [date, market_feature_1..M]
        labels.csv           [date, symbol, future_return_5d, benchmark_return,
                              excess_return, label_zscore]
        prices.csv           [date, symbol, close]
        meta.json            数据来源 / 特征来源 / benchmark_type /
                             label_definition / feature_names / 日期范围

约定（Spec 十六）:
    - t 日 feature 只使用 <= t 的信息
    - label = close_t → close_{t+5}（future_return_5d = close[t+5]/close[t] - 1）
    - benchmark 优先 CSI300 指数（SH000300），不可用时回退当日股票池等权
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .feature_builder import FeatureBuilder
from .preprocessing import build_labels

LABEL_DEFINITION = "close_t_to_close_t+5"  # label = future_return_5d = close[t+5]/close[t] - 1


class QlibAdapter:
    """把 Qlib 数据（或本地 CSV）转换为统一训练格式。"""

    def __init__(
        self,
        source: str = "qlib",
        qlib_dir: Optional[str] = None,
        instruments: str = "csi300",
        benchmark_symbol: str = "SH000300",
        start_time: str = "2014-01-01",
        end_time: str = "2024-12-31",
        horizon: int = 5,
        csv_path: Optional[str] = None,
        benchmark_csv: Optional[str] = None,
    ) -> None:
        self.source = source
        self.qlib_dir = qlib_dir or os.path.expanduser("~/.qlib/qlib_data/cn_data")
        self.instruments = instruments
        self.benchmark_symbol = benchmark_symbol
        self.start_time = start_time
        self.end_time = end_time
        self.horizon = horizon
        self.csv_path = csv_path
        self.benchmark_csv = benchmark_csv
        self._feature_input: Optional[pd.DataFrame] = None
        self._prices_full: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------ #
    # 统一入口
    # ------------------------------------------------------------------ #
    def build(self, out_dir: str) -> Dict[str, object]:
        """构建统一数据文件到 out_dir，返回 meta 信息。"""
        os.makedirs(out_dir, exist_ok=True)
        if self.source == "qlib":
            price_df, benchmark_close, feature_source = self._load_from_qlib()
        elif self.source == "csv":
            price_df, benchmark_close, feature_source = self._load_from_csv()
        else:
            raise ValueError(f"未知数据源: {self.source}")

        # 特征（qlib 路径直接用 Alpha158；csv 路径用 FeatureBuilder，输入完整 OHLCV）
        if feature_source == "alpha158":
            feature_df, feature_names = self._load_alpha158_features()
            features_out = feature_df  # 已含 date/symbol + 158 列
        else:
            builder = FeatureBuilder(lookback=0)
            features_out, feature_names = builder.build_stock_features(self._feature_input)

        # 市场特征（等权横截面，只使用当日及之前数据；用完整 OHLCV 计算，避免丢 volume 类因子）
        market_input = self._feature_input if self._feature_input is not None else price_df
        market_df, market_names = FeatureBuilder(lookback=0).build_market_features(market_input)

        # 标签（benchmark 优先指数，回退等权池）
        benchmark_type = self._benchmark_type(benchmark_close)
        label_df = build_labels(
            features_out, price_df, horizon=self.horizon,
            benchmark_close=benchmark_close, benchmark_name=benchmark_type,
        )

        # 统一保存
        features_out.to_csv(f"{out_dir}/features.csv", index=False)
        market_df.to_csv(f"{out_dir}/market_features.csv", index=False)
        label_df.to_csv(f"{out_dir}/labels.csv", index=False)
        price_df[["date", "symbol", "close"]].to_csv(f"{out_dir}/prices.csv", index=False)

        meta = {
            "source": self.source,
            "feature_source": feature_source,
            "benchmark_type": benchmark_type,
            "label_definition": LABEL_DEFINITION,
            "horizon": self.horizon,
            "feature_names": feature_names,
            "market_feature_names": market_names,
            "n_features": len(feature_names),
            "n_market_features": len(market_names),
            "date_range": [str(price_df["date"].min()), str(price_df["date"].max())],
            "n_symbols": int(price_df["symbol"].nunique()),
            "instruments": self.instruments,
        }
        with open(f"{out_dir}/meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2, default=str)
        return meta

    # ------------------------------------------------------------------ #
    # Qlib 路径
    # ------------------------------------------------------------------ #
    def _load_from_qlib(self) -> Tuple[pd.DataFrame, Optional[pd.Series], str]:
        """从 qlib cn_data 读取 CSI300 行情（open/high/low/close/volume/amount/vwap）。"""
        import qlib
        from qlib.config import REG_CN
        from qlib.data import D

        qlib.init(provider_uri=self.qlib_dir, region=REG_CN, mount_path="/tmp/qlib_mount")
        # 校准时间范围：以实际日历为准
        calendar = D.calendar(freq="day")
        cal = pd.Series(calendar)
        cal_start, cal_end = str(cal.min()), str(cal.max())
        start = max(self.start_time, cal_start)
        end = min(self.end_time, cal_end)
        print(f"[QlibAdapter] 日历范围: {cal_start} ~ {cal_end}，使用: {start} ~ {end}")

        fields = ["$open", "$high", "$low", "$close", "$volume", "$amount", "$vwap"]
        df = D.features([self.instruments], fields, start_time=start, end_time=end, freq="day")
        # df: MultiIndex (datetime, instrument) x fields
        df = df.reset_index()
        df = df.rename(columns={"datetime": "date", "instrument": "symbol"})
        df["date"] = df["date"].astype(str)
        # 价格只要求 close 存在（volume/amount 缺失不影响价格与标签）
        df = df[df["$close"].notna()].reset_index(drop=True)

        # benchmark: CSI300 指数收盘
        benchmark_close: Optional[pd.Series] = None
        try:
            bench = D.features([self.benchmark_symbol], ["$close"],
                               start_time=start, end_time=end, freq="day")
            bench = bench.reset_index().rename(columns={"datetime": "date", "instrument": "symbol"})
            bench["date"] = bench["date"].astype(str)
            benchmark_close = bench.set_index("date")["$close"]
            print(f"[QlibAdapter] benchmark 指数 {self.benchmark_symbol} 读取成功 "
                  f"({len(benchmark_close)} 天)")
        except Exception as exc:  # noqa: BLE001
            print(f"[QlibAdapter] benchmark 指数读取失败（回退等权池）: {exc}")

        price_df = df[["date", "symbol", "close"]].copy()
        self._prices_full = df  # 供 Alpha158 使用
        return price_df, benchmark_close, "alpha158"

    def _load_alpha158_features(self) -> Tuple[pd.DataFrame, List[str]]:
        """使用 qlib 内置 Alpha158 handler 计算 158 个特征（原始值，不做缩放）。"""
        from qlib.contrib.data.handler import Alpha158
        from qlib.data.dataset.handler import DataHandlerLP

        start = str(self._prices_full["date"].min())
        end = str(self._prices_full["date"].max())
        handler = Alpha158(
            instruments=self.instruments,
            start_time=start,
            end_time=end,
            freq="day",
            infer_processors=[],  # 原始特征由本项目管线在 train 上统一缩放
            learn_processors=[],
            process_type=DataHandlerLP.PTYPE_A,
        )
        feat = handler.fetch(col_set="feature", data_key=DataHandlerLP.DK_I)
        # feat: MultiIndex (datetime, instrument) x 158 列
        feat = feat.reset_index().rename(columns={"datetime": "date", "instrument": "symbol"})
        feat["date"] = feat["date"].astype(str)
        feature_names = [c for c in feat.columns if c not in ("date", "symbol")]
        print(f"[QlibAdapter] Alpha158 特征: {feat.shape}, F={len(feature_names)}")
        return feat, feature_names

    # ------------------------------------------------------------------ #
    # 本地 CSV 路径（fallback）
    # ------------------------------------------------------------------ #
    def _load_from_csv(self) -> Tuple[pd.DataFrame, Optional[pd.Series], str]:
        """本地 CSV: date, symbol, open, high, low, close, volume（可选 amount）。

        benchmark_csv（可选）: date, close —— CSI300 指数日收盘，用于指数基准。
        """
        if not self.csv_path or not os.path.exists(self.csv_path):
            raise FileNotFoundError(f"本地数据不存在: {self.csv_path}")
        df = pd.read_csv(self.csv_path, parse_dates=False)
        df["date"] = df["date"].astype(str)
        df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
        price_df = df[["date", "symbol", "close"]].copy()
        self._feature_input = df  # 完整 OHLCV 供 FeatureBuilder 使用
        benchmark_close: Optional[pd.Series] = None
        if self.benchmark_csv and os.path.exists(self.benchmark_csv):
            bench = pd.read_csv(self.benchmark_csv, parse_dates=False)
            bench["date"] = bench["date"].astype(str)
            benchmark_close = bench.set_index("date")["close"].sort_index()
            print(f"[QlibAdapter] benchmark 指数读取成功 ({len(benchmark_close)} 天)")
        print(f"[QlibAdapter] 本地 CSV: {df.shape}, 股票 {price_df['symbol'].nunique()} 只")
        return price_df, benchmark_close, "feature_builder"

    # ------------------------------------------------------------------ #
    @staticmethod
    def _benchmark_type(benchmark_close: Optional[pd.Series]) -> str:
        return "csi300_index" if benchmark_close is not None else "equal_weight_pool"
