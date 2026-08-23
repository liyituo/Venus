"""FeatureBuilder：从 OHLCV 原始数据生成股票因子与市场特征。

全部使用 pandas/numpy 实现，不依赖 TA-Lib。

防泄漏保证（最高优先级）:
  - 所有 rolling / pct_change / shift 只使用当日及之前的数据；
  - rolling 一律在 symbol 分组内进行，避免跨股票窗口；
  - 未来收益（label）在 preprocessing.py 中单独计算，使用 t+1..t+horizon。
"""
from __future__ import annotations

from typing import List, Tuple

import numpy as np
import pandas as pd

EPS = 1e-8

# 基础股票因子列表（可在 build 前通过 self.feature_names 查看）
STOCK_FEATURE_NAMES = [
    "return_1d",
    "return_5d",
    "return_10d",
    "return_20d",
    "volatility_5d",
    "volatility_20d",
    "volume_ratio",
    "turnover",
    "ma5_ratio",
    "ma10_ratio",
    "ma20_ratio",
    "rsi",
    "high_low_range",
    "momentum_5d",
    "momentum_20d",
]

MARKET_FEATURE_NAMES = [
    "market_return_1d",
    "market_return_5d",
    "market_return_20d",
    "market_volatility_5d",
    "market_volatility_20d",
    "advance_ratio",
    "cross_section_dispersion",
    "market_volume_ratio",
]


class FeatureBuilder:
    """从 OHLCV 长表构建股票因子与市场特征。

    输入格式（data/raw 下 CSV/parquet）至少包含:
        date, symbol, close
    可选: open, high, low, volume, shares_outstanding
    """

    def __init__(self, lookback: int = 60) -> None:
        self.lookback = lookback

    # ------------------------------------------------------------------ #
    # 股票因子
    # ------------------------------------------------------------------ #
    def build_stock_features(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
        """构建逐股因子。

        返回 (长表 [date, symbol, feature...], 实际生成的特征名列表)。
        上市不足 lookback 个交易日的股票行会被剔除。
        """
        df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
        has_high_low = {"high", "low"}.issubset(df.columns)
        has_volume = "volume" in df.columns
        has_shares = "shares_outstanding" in df.columns

        close = df["close"]
        # 分组内 pct_change / shift：只使用本股票自己的历史
        ret_1d = df.groupby("symbol", sort=False)["close"].pct_change(1)  # close_t/close_{t-1}-1
        ret_5d = df.groupby("symbol", sort=False)["close"].pct_change(5)
        ret_10d = df.groupby("symbol", sort=False)["close"].pct_change(10)
        ret_20d = df.groupby("symbol", sort=False)["close"].pct_change(20)

        feat = pd.DataFrame(
            {
                "date": df["date"],
                "symbol": df["symbol"],
                "return_1d": ret_1d,
                "return_5d": ret_5d,
                "return_10d": ret_10d,
                "return_20d": ret_20d,
                "volatility_5d": self._grp_rolling(df, ret_1d, 5, "std"),
                "volatility_20d": self._grp_rolling(df, ret_1d, 20, "std"),
                "ma5_ratio": close / self._grp_rolling(df, close, 5, "mean") - 1.0,
                "ma10_ratio": close / self._grp_rolling(df, close, 10, "mean") - 1.0,
                "ma20_ratio": close / self._grp_rolling(df, close, 20, "mean") - 1.0,
                "rsi": self._rsi(df, close, window=14),
                "momentum_5d": self._grp_shift(df, close, 1) / self._grp_shift(df, close, 6) - 1.0,
                "momentum_20d": self._grp_shift(df, close, 1) / self._grp_shift(df, close, 21) - 1.0,
            }
        )
        # 可选的量价因子
        if has_volume:
            volume = df["volume"].astype(float)
            feat["volume_ratio"] = volume / self._grp_rolling(df, volume, 20, "mean")
        if has_volume and has_shares:
            # 换手率 = 成交量 / 流通股本
            feat["turnover"] = df["volume"].astype(float) / df["shares_outstanding"].astype(float)
        if has_high_low:
            # 日内振幅（相对收盘价）
            feat["high_low_range"] = (df["high"] - df["low"]) / (close + EPS)

        # 剔除 rolling 窗口不足产生的 NaN 行（保留原始行号索引，供 lookback 过滤对齐）
        available = [c for c in STOCK_FEATURE_NAMES if c in feat.columns]
        feat = feat.dropna(subset=available)

        # 上市初期过滤：每只股票至少 lookback 个交易日（掩码按原始行号对齐）
        if self.lookback and self.lookback > 1:
            day_in_history = df.groupby("symbol", sort=False).cumcount()
            keep = (day_in_history >= (self.lookback - 1))
            feat = feat.loc[keep.reindex(feat.index).fillna(False).to_numpy()]
        feat = feat.reset_index(drop=True)

        feature_names = [c for c in STOCK_FEATURE_NAMES if c in feat.columns]
        return feat, feature_names

    # ------------------------------------------------------------------ #
    # 市场特征
    # ------------------------------------------------------------------ #
    def build_market_features(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
        """构建每日市场状态特征（全市场等权视角，只使用当日及之前数据）。"""
        df = df.sort_values(["date", "symbol"]).reset_index(drop=True)

        # 逐股日收益（横截面）
        ret_1d = df.groupby("symbol", sort=False)["close"].pct_change(1)
        # 市场等权收盘指数（用于 5/20 日收益）
        mkt_close = df.groupby("date", sort=True)["close"].mean()
        # 逐日横截面平均日收益（ret_1d 已按 symbol 分组计算）
        mkt_ret_1d = ret_1d.groupby(df["date"]).mean()

        mkt = pd.DataFrame({"date": mkt_close.index.astype(str)})
        mkt = mkt.set_index("date")
        mkt["market_return_1d"] = mkt_ret_1d
        mkt["market_return_5d"] = mkt_close.pct_change(5).to_numpy()
        mkt["market_return_20d"] = mkt_close.pct_change(20).to_numpy()
        mkt["market_volatility_5d"] = mkt_ret_1d.rolling(5).std().to_numpy()
        mkt["market_volatility_20d"] = mkt_ret_1d.rolling(20).std().to_numpy()
        mkt["advance_ratio"] = (ret_1d > 0).groupby(df["date"]).mean().to_numpy()
        mkt["cross_section_dispersion"] = ret_1d.groupby(df["date"]).std().to_numpy()
        if "volume" in df.columns:
            mkt_volume = df.groupby("date", sort=True)["volume"].mean()
            mkt["market_volume_ratio"] = (mkt_volume / mkt_volume.rolling(20).mean()).to_numpy()
        mkt = mkt.reset_index()

        feature_names = [c for c in MARKET_FEATURE_NAMES if c in mkt.columns]
        mkt = mkt.dropna(subset=feature_names).reset_index(drop=True)
        return mkt, feature_names

    # ------------------------------------------------------------------ #
    # 统一入口
    # ------------------------------------------------------------------ #
    def build(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, List[str], List[str]]:
        """构建全部特征。

        返回:
            stock_feat_df: [date, symbol, feature...]
            market_feat_df: [date, market_feature...]
            feature_names, market_feature_names
        """
        stock_feat, feature_names = self.build_stock_features(df)
        market_feat, market_feature_names = self.build_market_features(df)
        # 市场特征只保留股票特征存在的日期，保证对齐
        valid_dates = set(stock_feat["date"].unique())
        market_feat = market_feat[market_feat["date"].isin(valid_dates)].reset_index(drop=True)
        return stock_feat, market_feat, feature_names, market_feature_names

    # ------------------------------------------------------------------ #
    # 内部工具（全部在 symbol 分组内 rolling，防止跨股票泄漏）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _grp_rolling(df: pd.DataFrame, series: pd.Series, window: int, agg: str) -> pd.Series:
        """在 symbol 分组内做 rolling 聚合，返回与 df 行对齐的 Series。"""
        key = "symbol"
        frame = pd.DataFrame({"val": series.to_numpy(), "sym": df[key].to_numpy()})
        rolled = frame.groupby("sym", sort=False)["val"].rolling(window).agg(agg)
        rolled.index = rolled.index.droplevel(0)  # 恢复原行号
        return rolled.reindex(df.index)

    @staticmethod
    def _grp_shift(df: pd.DataFrame, series: pd.Series, periods: int) -> pd.Series:
        """在 symbol 分组内 shift，返回与 df 行对齐的 Series。"""
        frame = pd.DataFrame({"val": series.to_numpy(), "sym": df["symbol"].to_numpy()})
        shifted = frame.groupby("sym", sort=False)["val"].shift(periods)
        shifted.index = df.index
        return shifted

    @staticmethod
    def _rsi(df: pd.DataFrame, close: pd.Series, window: int = 14) -> pd.Series:
        """RSI(14)：简单平均增益/损失版，只用当日及之前数据。"""
        frame = pd.DataFrame({"close": close.to_numpy(), "sym": df["symbol"].to_numpy()})
        delta = frame.groupby("sym", sort=False)["close"].diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)
        # 分组内 rolling 平均（index 恢复为原始行号）
        rolled_gain = gain.groupby(frame["sym"]).rolling(window).mean()
        rolled_gain.index = rolled_gain.index.droplevel(0)
        rolled_loss = loss.groupby(frame["sym"]).rolling(window).mean()
        rolled_loss.index = rolled_loss.index.droplevel(0)
        rs = rolled_gain.reindex(df.index) / (rolled_loss.reindex(df.index) + EPS)
        return 100.0 - 100.0 / (1.0 + rs)
