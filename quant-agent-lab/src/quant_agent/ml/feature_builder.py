"""从 MarketSnapshot OHLCV 构建 Tiny-MoE 训练同款特征（只用 t 日及之前数据）。"""

from __future__ import annotations

import pandas as pd

EPS = 1e-8

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
    def __init__(self, lookback: int = 60) -> None:
        self.lookback = lookback

    def build_stock_features(self, df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
        df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
        has_high_low = {"high", "low"}.issubset(df.columns)
        has_volume = "volume" in df.columns
        has_shares = "shares_outstanding" in df.columns

        close = df["close"]
        ret_1d = df.groupby("symbol", sort=False)["close"].pct_change(1)
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
                "momentum_20d": self._grp_shift(df, close, 1) / self._grp_shift(df, close, 21)
                - 1.0,
            }
        )
        if has_volume:
            volume = df["volume"].astype(float)
            feat["volume_ratio"] = volume / self._grp_rolling(df, volume, 20, "mean")
        if has_volume and has_shares:
            feat["turnover"] = df["volume"].astype(float) / df["shares_outstanding"].astype(float)
        if has_high_low:
            feat["high_low_range"] = (df["high"] - df["low"]) / (close + EPS)

        available = [c for c in STOCK_FEATURE_NAMES if c in feat.columns]
        feat = feat.dropna(subset=available)

        if self.lookback and self.lookback > 1:
            day_in_history = df.groupby("symbol", sort=False).cumcount()
            keep = day_in_history >= (self.lookback - 1)
            feat = feat.loc[keep.reindex(feat.index).fillna(False).to_numpy()]
        feat = feat.reset_index(drop=True)

        feature_names = [c for c in STOCK_FEATURE_NAMES if c in feat.columns]
        return feat, feature_names

    def build_market_features(self, df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
        df = df.sort_values(["date", "symbol"]).reset_index(drop=True)
        ret_1d = df.groupby("symbol", sort=False)["close"].pct_change(1)
        mkt_close = df.groupby("date", sort=True)["close"].mean()
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

    def build(self, df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, list[str], list[str]]:
        stock_feat, feature_names = self.build_stock_features(df)
        market_feat, market_feature_names = self.build_market_features(df)
        valid_dates = set(stock_feat["date"].unique())
        market_feat = market_feat[market_feat["date"].isin(valid_dates)].reset_index(drop=True)
        return stock_feat, market_feat, feature_names, market_feature_names

    @staticmethod
    def _grp_rolling(df: pd.DataFrame, series: pd.Series, window: int, agg: str) -> pd.Series:
        frame = pd.DataFrame({"val": series.to_numpy(), "sym": df["symbol"].to_numpy()})
        rolled = frame.groupby("sym", sort=False)["val"].rolling(window).agg(agg)
        rolled.index = rolled.index.droplevel(0)
        return rolled.reindex(df.index)

    @staticmethod
    def _grp_shift(df: pd.DataFrame, series: pd.Series, periods: int) -> pd.Series:
        frame = pd.DataFrame({"val": series.to_numpy(), "sym": df["symbol"].to_numpy()})
        shifted = frame.groupby("sym", sort=False)["val"].shift(periods)
        shifted.index = df.index
        return shifted

    @staticmethod
    def _rsi(df: pd.DataFrame, close: pd.Series, window: int = 14) -> pd.Series:
        frame = pd.DataFrame({"close": close.to_numpy(), "sym": df["symbol"].to_numpy()})
        delta = frame.groupby("sym", sort=False)["close"].diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)
        rolled_gain = gain.groupby(frame["sym"]).rolling(window).mean()
        rolled_gain.index = rolled_gain.index.droplevel(0)
        rolled_loss = loss.groupby(frame["sym"]).rolling(window).mean()
        rolled_loss.index = rolled_loss.index.droplevel(0)
        rs = rolled_gain.reindex(df.index) / (rolled_loss.reindex(df.index) + EPS)
        return 100.0 - 100.0 / (1.0 + rs)
