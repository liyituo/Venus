"""数据预处理：标签构建、严格时间切分、Scaler（只 fit train）、Winsorize、Sanity Check。

防泄漏关键点:
  - label 只允许使用 t+1 .. t+horizon 的数据（future_return 方向正确）;
  - 标签横截面标准化只使用当天数据;
  - 时间切分不做随机打乱，train < valid < test;
  - Scaler / Winsorize 阈值只能在 train 上 fit，再 transform train/valid/test。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler

EPS = 1e-8


def compute_future_returns(price_df: pd.DataFrame, horizon: int = 5) -> pd.DataFrame:
    """逐股票计算未来 horizon 日收益: future_return = close[t+h] / close[t] - 1。

    输入 price_df: [date, symbol, close]（无需排序，内部处理）
    返回: [date, symbol, future_return_5d]，最后 horizon 天为 NaN 并剔除。
    """
    df = price_df[["date", "symbol", "close"]].sort_values(["symbol", "date"]).copy()
    grp_close = df.groupby("symbol", sort=False)["close"]
    # shift(-horizon)：把 t+horizon 的 close 移到 t 行 —— 只使用未来数据，方向正确
    df["future_return"] = grp_close.shift(-horizon) / df["close"] - 1.0
    out = df[["date", "symbol", "future_return"]].dropna()
    out = out.rename(columns={"future_return": f"future_return_{horizon}d"})
    return out.reset_index(drop=True)


def build_labels(
    feature_df: pd.DataFrame,
    price_df: pd.DataFrame,
    horizon: int = 5,
    benchmark_close: pd.Series | None = None,
    benchmark_name: str = "equal_weight_pool",
) -> pd.DataFrame:
    """构建训练标签: excess_return_5d 与逐日横截面 z-score。

    基准 benchmark_return 优先使用外部指数（benchmark_close，按日期对齐），
    否则回退为当日股票池等权未来收益。
    label_zscore 只使用当天横截面 mean/std，不掺入任何其他日期信息。

    返回: [date, symbol, future_return_5d, benchmark_return, excess_return, label_zscore]
    """
    future_col = f"future_return_{horizon}d"
    labels = compute_future_returns(price_df, horizon)
    out = feature_df[["date", "symbol"]].merge(labels, on=["date", "symbol"], how="left")
    if benchmark_close is not None:
        # 指数基准: benchmark_return(t) = index_close[t+h] / index_close[t] - 1
        bench = benchmark_close.sort_index()
        bench_ret = (bench.shift(-horizon) / bench - 1.0).rename("benchmark_return")
        out = out.merge(bench_ret.reset_index().rename(columns={"index": "date"}),
                        on="date", how="left")
    else:
        # 当日股票池等权基准（只统计当日有 future return 的股票）
        out["benchmark_return"] = out.groupby("date")[future_col].transform("mean")
    out["excess_return"] = out[future_col] - out["benchmark_return"]
    # 当日横截面 z-score（ddof=0，人口标准差）
    out["label_zscore"] = out.groupby("date")["excess_return"].transform(
        lambda s: (s - s.mean()) / (s.std(ddof=0) + EPS)
    )
    return out.dropna(subset=["label_zscore"]).reset_index(drop=True)


def split_by_date(
    df: pd.DataFrame,
    date_col: str,
    splits: Dict[str, Dict[str, str]],
) -> Dict[str, pd.DataFrame]:
    """严格时间切分，不做任何随机打乱。

    splits 形如 {"train": {"start": "2015-01-01", "end": "2021-12-31"}, ...}
    """
    out: Dict[str, pd.DataFrame] = {}
    for name, bounds in splits.items():
        mask = (df[date_col] >= str(bounds["start"])) & (df[date_col] <= str(bounds["end"]))
        out[name] = df.loc[mask].copy().reset_index(drop=True)
    return out


def fit_scaler(train_df: pd.DataFrame, feature_cols: List[str]) -> StandardScaler:
    """StandardScaler 只在训练集上 fit（严禁在完整数据上 fit）。"""
    scaler = StandardScaler()
    scaler.fit(train_df[feature_cols].to_numpy(dtype=np.float64))
    return scaler


def transform_features(
    df: pd.DataFrame, feature_cols: List[str], scaler: StandardScaler
) -> pd.DataFrame:
    """用已 fit 的 scaler 做 transform（不重新 fit）。"""
    out = df.copy()
    out[feature_cols] = scaler.transform(df[feature_cols].to_numpy(dtype=np.float64))
    return out


def fit_winsorize(train_df: pd.DataFrame, feature_cols: List[str],
                  lower_quantile: float = 0.01, upper_quantile: float = 0.99) -> Dict[str, Tuple[float, float]]:
    """Winsorize 阈值只能从 train 估计（禁止在完整数据上估计）。

    返回 {col: (lower_bound, upper_bound)}。
    """
    bounds: Dict[str, Tuple[float, float]] = {}
    for col in feature_cols:
        s = train_df[col].dropna()
        lo = float(s.quantile(lower_quantile))
        hi = float(s.quantile(upper_quantile))
        bounds[col] = (lo, hi)
    return bounds


def apply_winsorize(
    df: pd.DataFrame, feature_cols: List[str], bounds: Dict[str, Tuple[float, float]]
) -> pd.DataFrame:
    """按 train 估计的阈值裁剪（只裁剪，不重新估计）。"""
    out = df.copy()
    for col in feature_cols:
        lo, hi = bounds[col]
        out[col] = out[col].clip(lower=lo, upper=hi)
    return out


def sanity_check_data(
    feature_df: pd.DataFrame,
    label_df: pd.DataFrame,
    market_df: pd.DataFrame,
    min_stocks_per_day: int = 50,
) -> Dict[str, object]:
    """真实数据运行前的 Data Sanity Check（Spec 十七）。

    输出: date range / 股票数 / 交易日数 / 特征数 / missing ratio /
          label mean+std / 每日股票数统计。
    检查: NaN、inf、重复 date-symbol。
    返回报告 dict（供日志使用）。
    """
    report: Dict[str, object] = {}
    dates = pd.to_datetime(feature_df["date"])
    report["date_range"] = [str(dates.min().date()), str(dates.max().date())]
    report["n_symbols"] = int(feature_df["symbol"].nunique())
    report["n_days"] = int(feature_df["date"].nunique())
    feature_cols = [c for c in feature_df.columns if c not in ("date", "symbol")]
    report["n_features"] = len(feature_cols)

    missing = feature_df[feature_cols].isna().mean()
    report["missing_ratio"] = float(missing.mean())
    report["max_feature_missing_ratio"] = float(missing.max())
    report["has_inf"] = bool(np.isinf(feature_df[feature_cols].to_numpy()).any())

    dup = feature_df.duplicated(subset=["date", "symbol"]).sum()
    report["duplicate_date_symbol"] = int(dup)

    if "label_zscore" in label_df.columns and len(label_df):
        report["label_mean"] = float(label_df["label_zscore"].mean())
        report["label_std"] = float(label_df["label_zscore"].std(ddof=0))

    stocks_per_day = feature_df.groupby("date").size()
    report["stocks_per_day"] = {
        "mean": float(stocks_per_day.mean()),
        "std": float(stocks_per_day.std(ddof=1)),
        "min": int(stocks_per_day.min()),
        "max": int(stocks_per_day.max()),
    }
    report["min_stocks_per_day"] = int(min_stocks_per_day)
    return report


def drop_sparse_days(
    feature_df: pd.DataFrame,
    label_df: pd.DataFrame,
    market_df: pd.DataFrame,
    prices_df: pd.DataFrame,
    min_stocks_per_day: int = 50,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, List[str]]:
    """剔除股票数过少的交易日（当天横截面太小，IC/pair 不稳定）。"""
    counts = feature_df.groupby("date").size()
    keep_days = counts[counts >= min_stocks_per_day].index.tolist()
    keep_days = set(keep_days)
    out = (
        feature_df[feature_df["date"].isin(keep_days)].reset_index(drop=True),
        label_df[label_df["date"].isin(keep_days)].reset_index(drop=True),
        market_df[market_df["date"].isin(keep_days)].reset_index(drop=True),
        prices_df[prices_df["date"].isin(keep_days)].reset_index(drop=True),
    )
    dropped = [d for d in counts.index if d not in keep_days]
    return (*out, dropped)


def auto_adjust_splits(
    splits: Dict[str, Dict[str, str]], first_date: str, last_date: str
) -> Dict[str, Dict[str, str]]:
    """真实数据时间范围不足时自动缩短切分（Spec 十四）。

    规则:
      1) 先裁剪超出数据范围的 end；
      2) 若裁剪后仍有切分为空，则收缩为: test = 最近 2 年，valid = 之前 1 年，
         train = 更早（保证 train < valid < test，且 test 不参与模型选择）。
    返回调整后的 splits（新 dict，不改原配置）。
    """
    from pandas import Timestamp

    first, last = Timestamp(first_date), Timestamp(last_date)
    out = {k: dict(v) for k, v in splits.items()}
    t_start, t_end = Timestamp(out["test"]["start"]), Timestamp(out["test"]["end"])
    v_start, v_end = Timestamp(out["valid"]["start"]), Timestamp(out["valid"]["end"])
    tr_start, tr_end = Timestamp(out["train"]["start"]), Timestamp(out["train"]["end"])

    # 1) 裁剪超出范围的部分
    if t_end > last:
        out["test"]["end"] = str(last.date())
        t_end = last
    if v_end >= t_start:
        out["valid"]["end"] = str((t_start - pd.Timedelta(days=1)).date())
        v_end = Timestamp(out["valid"]["end"])
    if tr_end >= v_start:
        out["train"]["end"] = str((v_start - pd.Timedelta(days=1)).date())
        tr_end = Timestamp(out["train"]["end"])

    # 2) 若仍有空切分（例如数据在 test_start 之前就结束了），整体收缩
    def empty(name: str) -> bool:
        s, e = Timestamp(out[name]["start"]), Timestamp(out[name]["end"])
        return s > e or e < first or s > last

    if any(empty(name) for name in ("train", "valid", "test")):
        test_end = last
        test_start = max(first, test_end - pd.DateOffset(years=2) + pd.Timedelta(days=1))
        valid_end = test_start - pd.Timedelta(days=1)
        valid_start = max(first, valid_end - pd.DateOffset(years=1) + pd.Timedelta(days=1))
        train_end = valid_start - pd.Timedelta(days=1)
        out = {
            "train": {"start": str(max(first, tr_start).date()), "end": str(train_end.date())},
            "valid": {"start": str(valid_start.date()), "end": str(valid_end.date())},
            "test": {"start": str(test_start.date()), "end": str(test_end.date())},
        }
    return out
