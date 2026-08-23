"""每日横截面 Dataset。

训练单位 = 一个交易日的完整股票横截面（batch_size=1 即"一天一个 batch"）。
Ranking Loss 只允许在同一天内部计算 —— 本 Dataset 天然保证不会跨日期构造 pair。
"""
from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd
import torch
from sklearn.preprocessing import StandardScaler
from torch.utils.data import Dataset

from .preprocessing import transform_features


class DailyCrossSectionDataset(Dataset):
    """按交易日组织样本。

    __getitem__ 返回:
        {
            "date": str,
            "stock_features": Tensor[N, F],
            "market_features": Tensor[M],
            "labels": Tensor[N],          # label_zscore（训练标签）
            "future_returns": Tensor[N],  # 原始未来收益（用于 IC / RankIC）
            "symbols": List[str],
        }
    """

    def __init__(
        self,
        feature_df: pd.DataFrame,
        label_df: pd.DataFrame,
        market_df: pd.DataFrame,
        feature_cols: List[str],
        market_cols: List[str],
        scaler: StandardScaler,
        market_scaler: StandardScaler,
        horizon: int = 5,
    ) -> None:
        label_cols = ["date", "symbol", "label_zscore", f"future_return_{horizon}d"]
        df = feature_df.merge(label_df[label_cols], on=["date", "symbol"], how="inner")
        df = df.sort_values(["date", "symbol"]).reset_index(drop=True)
        # 特征缩放（使用训练集 fit 的 scaler）
        scaled = transform_features(df, feature_cols, scaler)

        mkt = market_df.sort_values("date").reset_index(drop=True)
        mkt_scaled = transform_features(mkt, market_cols, market_scaler)

        self.dates: List[str] = sorted(df["date"].unique().tolist())
        self._samples: List[Dict] = []
        for date in self.dates:
            day = scaled[scaled["date"] == date]
            m = mkt_scaled[mkt_scaled["date"] == date]
            if m.empty:
                raise ValueError(f"日期 {date} 缺少市场特征")
            self._samples.append(
                {
                    "date": date,
                    "stock_features": torch.tensor(
                        day[feature_cols].to_numpy(dtype=np.float32)
                    ),  # [N, F]
                    "market_features": torch.tensor(
                        m[market_cols].to_numpy(dtype=np.float32)[0]
                    ),  # [M]
                    "labels": torch.tensor(
                        day["label_zscore"].to_numpy(dtype=np.float32)
                    ),  # [N]
                    "future_returns": torch.tensor(
                        day[f"future_return_{horizon}d"].to_numpy(dtype=np.float32)
                    ),  # [N]
                    "symbols": day["symbol"].tolist(),
                }
            )

    def __len__(self) -> int:
        return len(self._samples)

    def __getitem__(self, idx: int) -> Dict:
        return self._samples[idx]
