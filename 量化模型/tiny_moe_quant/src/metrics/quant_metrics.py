"""量化指标：逐日 IC / RankIC。

禁止把所有日期的股票混在一起计算一个总相关系数；
必须逐日计算 IC_t / RankIC_t，再汇总 mean/std/ICIR。
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd
from scipy.stats import pearsonr, spearmanr

MIN_DAILY_SAMPLES = 3


def daily_ic(scores: np.ndarray, labels: np.ndarray, dates: np.ndarray) -> pd.DataFrame:
    """按日期分组逐日计算 Pearson IC 与 Spearman RankIC。

    参数:
        scores: 模型对每只股票的分数 [M]
        labels: 未来收益（future_return，非 z-score，Pearson 对单调变换敏感）
        dates: 与 scores 对齐的日期数组 [M]

    返回:
        DataFrame[date, ic, rank_ic]，一天一行；
        样本过少或方差为 0 的日期会被跳过。
    """
    df = pd.DataFrame({"date": dates, "score": scores, "label": labels}).dropna()
    rows = []
    for date, g in df.groupby("date", sort=True):
        if len(g) < MIN_DAILY_SAMPLES:
            continue
        if g["score"].nunique() < 2 or g["label"].nunique() < 2:
            continue
        ic, _ = pearsonr(g["score"], g["label"])
        rank_ic, _ = spearmanr(g["score"], g["label"])
        rows.append({"date": date, "ic": ic, "rank_ic": rank_ic})
    return pd.DataFrame(rows, columns=["date", "ic", "rank_ic"])


def summarize_ic(daily: pd.DataFrame) -> Dict[str, float]:
    """汇总逐日 IC / RankIC: mean / std / ICIR（IR = mean / std of 日度序列）。"""
    out: Dict[str, float] = {"n_days": int(len(daily))}
    for col, prefix in (("ic", "ic"), ("rank_ic", "rank_ic")):
        s = daily[col].dropna().astype(float)
        mean = float(s.mean()) if len(s) else float("nan")
        std = float(s.std(ddof=1)) if len(s) > 1 else float("nan")
        out[f"mean_{prefix}"] = mean
        out[f"std_{prefix}"] = std
        out[f"{prefix}ir"] = mean / (std + 1e-12) if len(s) > 1 else float("nan")
    return out
