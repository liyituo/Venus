"""Baseline 模型。

Baseline A0 (Momentum) 不需要神经网络：score = 过去 20 日收益。
Baseline A1 (MLP Ranker) 与 A2 (MoE w/o market gate) 是 TinyMoE 的配置消融
（use_moe=False / use_market_gate=False），见 tiny_moe.py 与默认配置。
"""
from __future__ import annotations

from typing import List

import numpy as np


class MomentumBaseline:
    """Baseline A0: score = 过去 20 日收益（动量因子，无参数、无训练）。

    真实数据（FeatureBuilder 输出）默认使用 return_20d；
    合成演示数据（f1..f20）自动退化为 f1（即构造中的动量型因子）。
    """

    def __init__(self, feature_names: List[str], score_feature: str | None = None) -> None:
        if score_feature is None:
            score_feature = "return_20d" if "return_20d" in feature_names else "f1"
        self.score_source = score_feature
        if self.score_source not in feature_names:
            raise ValueError(
                f"MomentumBaseline 需要特征 '{self.score_source}'，"
                f"实际特征列表: {feature_names}"
            )
        self._idx = feature_names.index(self.score_source)

    def predict(self, features: np.ndarray) -> np.ndarray:
        """输入原始（未缩放）特征矩阵 [N, F]，返回动量分数 [N]。"""
        return features[:, self._idx].astype(np.float64)

    @property
    def num_parameters(self) -> int:
        return 0
