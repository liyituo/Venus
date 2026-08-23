"""防未来信息泄漏测试（Spec 十二，最高优先级）。

验证:
  1. 改变 t 日之后的价格/成交量，t 日的 features 不应变化；
  2. t 日的 label 使用未来数据，未来数据变化时 label 必须变化；
  3. t 日的 label 不受更早历史数据变化的影响；
  4. 时间切分严格顺序、互不重叠；
  5. Scaler 只在 train 上 fit，全数据 fit 会得到不同结果。
"""
import numpy as np
import pandas as pd
import pytest

from src.data.feature_builder import FeatureBuilder
from src.data.preprocessing import (
    build_labels,
    compute_future_returns,
    fit_scaler,
    split_by_date,
    transform_features,
)


def make_ohlcv(n_symbols: int = 2, n_days: int = 90, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2015-01-05", periods=n_days).strftime("%Y-%m-%d").tolist()
    rows = []
    for s in range(n_symbols):
        rets = rng.normal(0.0002, 0.02, n_days)
        close = 100.0 * np.exp(np.cumsum(rets))
        volume = rng.uniform(1e6, 5e6, n_days)
        for k, d in enumerate(dates):
            rows.append(
                {
                    "date": d,
                    "symbol": f"S{s}",
                    "open": float(close[k] * 0.99),
                    "high": float(close[k] * 1.02),
                    "low": float(close[k] * 0.98),
                    "close": float(close[k]),
                    "volume": float(volume[k]),
                    "shares_outstanding": 1e9,
                }
            )
    return pd.DataFrame(rows)


def test_features_do_not_change_when_future_changes():
    """核心防泄漏：t 日之后的行情变化不能改变 t 日的任何特征。"""
    fb = FeatureBuilder(lookback=20)
    df = make_ohlcv()
    feats_a, mkt_a, fnames, mnames = fb.build(df)

    t = feats_a["date"].unique().tolist()[40]  # 中间某天

    df_future_changed = df.copy()
    mask = df_future_changed["date"] > t
    df_future_changed.loc[mask, "close"] *= 1.5
    df_future_changed.loc[mask, "volume"] *= 3.0
    df_future_changed.loc[mask, "high"] *= 1.5
    df_future_changed.loc[mask, "low"] *= 1.5
    feats_b, mkt_b, _, _ = fb.build(df_future_changed)

    def at(df_, date_):
        out = df_[df_["date"] == date_]
        if "symbol" in out.columns:
            out = out.sort_values("symbol")
        return out.reset_index(drop=True)

    pd.testing.assert_frame_equal(at(feats_a, t), at(feats_b, t))
    pd.testing.assert_frame_equal(
        at(mkt_a, t)[mnames].reset_index(drop=True),
        at(mkt_b, t)[mnames].reset_index(drop=True),
    )
    # 同时确认：特征确实不是全 0（测试本身有效）
    assert not np.allclose(at(feats_a, t)[fnames].to_numpy(), 0.0)


def test_features_use_own_symbol_history_only():
    """某股票的特征不应受另一只股票数据的影响（分组正确）。"""
    fb = FeatureBuilder(lookback=20)
    df = make_ohlcv(n_symbols=2)
    feats_a, _, _, _ = fb.build(df)

    df2 = df.copy()
    # 只改 S1 的价格
    df2.loc[df2["symbol"] == "S1", "close"] *= 1.2
    feats_b, _, _, _ = fb.build(df2)

    t = feats_a["date"].unique().tolist()[40]
    a0 = feats_a[(feats_a["date"] == t) & (feats_a["symbol"] == "S0")]
    b0 = feats_b[(feats_b["date"] == t) & (feats_b["symbol"] == "S0")]
    pd.testing.assert_frame_equal(a0.reset_index(drop=True), b0.reset_index(drop=True))


def test_label_changes_when_future_changes():
    """label 使用 t+1..t+h 数据：未来价格变化 -> 当日 label 必须变化。"""
    df = make_ohlcv()
    horizon = 5
    lab_a = compute_future_returns(df[["date", "symbol", "close"]], horizon)

    df2 = df.copy()
    df2.loc[df2["date"] > "2015-03-01", "close"] *= 1.5
    lab_b = compute_future_returns(df2[["date", "symbol", "close"]], horizon)

    t = "2015-02-25"  # 该日期的 future 窗口跨越 2015-03-01
    a = lab_a[lab_a["date"] == t].sort_values("symbol").reset_index(drop=True)
    b = lab_b[lab_b["date"] == t].sort_values("symbol").reset_index(drop=True)
    assert not np.allclose(a["future_return_5d"].to_numpy(),
                           b["future_return_5d"].to_numpy())


def test_label_unchanged_when_past_changes():
    """label 不应受更早历史（t 之前）数据影响。"""
    df = make_ohlcv()
    horizon = 5
    lab_a = compute_future_returns(df[["date", "symbol", "close"]], horizon)

    df2 = df.copy()
    df2.loc[df2["date"] < "2015-01-20", "close"] *= 0.5  # 只改早期历史
    lab_b = compute_future_returns(df2[["date", "symbol", "close"]], horizon)

    t = "2015-02-25"
    a = lab_a[lab_a["date"] == t].sort_values("symbol").reset_index(drop=True)
    b = lab_b[lab_b["date"] == t].sort_values("symbol").reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)


def test_label_zscore_uses_only_same_day():
    """label_zscore 的标准化只用当天横截面。

    未来价格变化（逐股不同幅度，破坏同日内仿射不变性）
    -> 当天 excess_return 与 z-score 必须变化；
    而无论未来怎么变，当天 z-score 均值为 0、标准差为 1（只用了当天数据）。
    """
    df = make_ohlcv(n_symbols=5, n_days=60)
    fb = FeatureBuilder(lookback=20)
    feats, _, _, _ = fb.build(df)
    horizon = 5

    lab_a = build_labels(feats, df[["date", "symbol", "close"]], horizon)
    df2 = df.copy()
    mask = df2["date"] > "2015-03-01"
    # 逐股不同倍率，避免同日内整体仿射变换（那不会改变 z-score）
    multipliers = {"S0": 2.0, "S1": 1.5, "S2": 1.2, "S3": 0.8, "S4": 0.6}
    for sym, mult in multipliers.items():
        df2.loc[mask & (df2["symbol"] == sym), "close"] *= mult
    lab_b = build_labels(feats, df2[["date", "symbol", "close"]], horizon)

    t = "2015-02-27"
    za = lab_a[lab_a["date"] == t].sort_values("symbol")["label_zscore"].to_numpy()
    zb = lab_b[lab_b["date"] == t].sort_values("symbol")["label_zscore"].to_numpy()
    # 未来数据变化 -> 当天 label（含 z-score）必须变化
    assert not np.allclose(za, zb)
    # 关键：当天内部标准化只使用当天横截面 —— 无论未来怎么变，当天均值为 0、标准差为 1
    for lab in (lab_a, lab_b):
        g = lab[lab["date"] == t]["label_zscore"]
        assert np.allclose(g.mean(), 0.0, atol=1e-8)
        assert np.allclose(g.std(ddof=0), 1.0, atol=1e-6)


def test_split_is_chronological_and_non_overlapping():
    """时间切分: train < valid < test，顺序严格且互不重叠。"""
    date_list = pd.bdate_range("2015-01-05", periods=60).strftime("%Y-%m-%d").tolist()
    df = pd.DataFrame({"date": date_list, "value": np.arange(60)})
    splits = {
        "train": {"start": date_list[0], "end": date_list[40]},
        "valid": {"start": date_list[41], "end": date_list[50]},
        "test": {"start": date_list[51], "end": date_list[59]},
    }
    out = split_by_date(df, "date", splits)
    dates = {k: sorted(v["date"].tolist()) for k, v in out.items()}
    assert dates["train"][-1] < dates["valid"][0]
    assert dates["valid"][-1] < dates["test"][0]
    assert len(set(dates["train"]) & set(dates["valid"])) == 0
    assert len(set(dates["valid"]) & set(dates["test"])) == 0
    assert len(dates["train"]) + len(dates["valid"]) + len(dates["test"]) == 60


def test_scaler_fit_on_train_only():
    """Scaler 必须只 fit(train)：全数据 fit 会得到不同的统计量。"""
    rng = np.random.default_rng(0)
    train_df = pd.DataFrame({"f1": rng.normal(0, 1, 200), "f2": rng.normal(5, 2, 200)})
    test_df = pd.DataFrame({"f1": rng.normal(0, 1, 50), "f2": rng.normal(5, 2, 50)})
    all_df = pd.concat([train_df, test_df], ignore_index=True)

    scaler_train = fit_scaler(train_df, ["f1", "f2"])
    scaler_all = fit_scaler(all_df, ["f1", "f2"])
    # 训练集与全数据统计量不同（测试集均值不同）
    assert not np.allclose(scaler_train.mean_, scaler_all.mean_, atol=1e-6)

    # transform 必须使用 fit 好的 scaler（不重新 fit）
    transformed = transform_features(test_df, ["f1", "f2"], scaler_train)
    manual = (test_df[["f1", "f2"]].to_numpy() - scaler_train.mean_) / (
        scaler_train.scale_ + 1e-12
    )
    assert np.allclose(transformed[["f1", "f2"]].to_numpy(), manual, atol=1e-8)


def test_winsorize_thresholds_from_train_only():
    """Winsorize 阈值只从 train 估计（Spec 十八），全数据估计会不同。"""
    from src.data.preprocessing import apply_winsorize, fit_winsorize

    rng = np.random.default_rng(0)
    train_df = pd.DataFrame({"f1": rng.normal(0, 1, 300)})
    # 测试集带离群值（应被裁剪到 train 分位数）
    test_df = pd.DataFrame({"f1": np.concatenate([rng.normal(0, 1, 80), [50.0, -60.0]])})
    all_df = pd.concat([train_df, test_df], ignore_index=True)

    bounds_train = fit_winsorize(train_df, ["f1"], 0.01, 0.99)
    bounds_all = fit_winsorize(all_df, ["f1"], 0.01, 0.99)
    assert not np.allclose(bounds_train["f1"][1], bounds_all["f1"][1], atol=1e-6)

    out = apply_winsorize(test_df, ["f1"], bounds_train)
    lo, hi = bounds_train["f1"]
    assert out["f1"].max() <= hi
    assert out["f1"].min() >= lo
    # 非离群值不被破坏
    mid = test_df["f1"].iloc[10]
    assert out["f1"].iloc[10] == mid


def test_auto_adjust_splits_shrinks_when_out_of_range():
    """数据范围不足时自动收缩切分，且保持 train < valid < test（Spec 十四）。"""
    from src.data.preprocessing import auto_adjust_splits

    splits = {
        "train": {"start": "2015-01-01", "end": "2021-12-31"},
        "valid": {"start": "2022-01-01", "end": "2022-12-31"},
        "test": {"start": "2023-01-01", "end": "2024-12-31"},
    }
    # 数据只到 2022-08-31
    out = auto_adjust_splits(splits, "2015-01-05", "2022-08-31")
    assert out["test"]["end"] == "2022-08-31"
    assert out["test"]["start"] < out["test"]["end"]
    assert out["valid"]["end"] < out["test"]["start"]
    assert out["train"]["end"] < out["valid"]["start"]

    # 数据足够时不做任何修改
    out2 = auto_adjust_splits(splits, "2014-01-01", "2024-12-31")
    assert out2 == splits


def test_feature_builder_requires_no_future():
    """冒烟：FeatureBuilder 在小型数据上能正常生成全部特征且无 NaN。"""
    fb = FeatureBuilder(lookback=20)
    df = make_ohlcv()
    feats, mkt, fnames, mnames = fb.build(df)
    assert len(fnames) == 15  # 全部 15 个因子（含 volume/turnover/high_low）
    assert len(mnames) == 8
    assert feats[["date", "symbol"]].duplicated().sum() == 0
    assert not feats[fnames].isna().any().any()
    assert not mkt[mnames].isna().any().any()
