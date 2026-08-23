"""推理 API：QuantPredictor。

第一阶段只提供 Python API（不需要 FastAPI），后续可以直接封装给 Agent 调用。
"""
from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
import torch

from ..models.tiny_moe import build_model


class QuantPredictor:
    """加载 best_model.pt checkpoint 后进行单日排序预测。

    predict_daily 输出:
        {
            "gate_weights": {"expert_1": ..., "expert_2": ..., "expert_3": ...},
            "stocks": [
                {"symbol": ..., "score": ..., "rank": ..., "expert_scores": [...]},
                ...
            ]
        }
    """

    def __init__(self, checkpoint_path: str, device: str = "cpu") -> None:
        ckpt = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
        self.config: Dict[str, Any] = ckpt["config"]
        self.feature_names: List[str] = ckpt["feature_names"]
        self.market_feature_names: List[str] = ckpt["market_feature_names"]
        self.scaler = ckpt["scaler"]
        self.market_scaler = ckpt["market_scaler"]

        self.model = build_model(
            self.config,
            num_features=len(self.feature_names),
            num_market_features=len(self.market_feature_names),
        )
        self.model.load_state_dict(ckpt["model_state"])
        self.model.eval()
        self.device = torch.device(device)
        self.model.to(self.device)

    def predict_daily(
        self,
        stock_features: np.ndarray,
        market_features: np.ndarray,
        symbols: List[str],
    ) -> Dict[str, Any]:
        """对单个交易日的股票横截面排序打分。

        参数:
            stock_features: [N, F] 原始（未缩放）特征
            market_features: [M] 或 [1, M] 原始市场特征
            symbols: [N] 股票代码
        """
        stock_features = np.asarray(stock_features, dtype=np.float64)
        market_features = np.asarray(market_features, dtype=np.float64)
        if stock_features.ndim != 2 or stock_features.shape[1] != len(self.feature_names):
            raise ValueError(
                f"stock_features 应为 [N, {len(self.feature_names)}]，实际 {stock_features.shape}"
            )
        if market_features.ndim == 1:
            market_features = market_features.reshape(1, -1)
        if market_features.shape[1] != len(self.market_feature_names):
            raise ValueError(
                f"market_features 维度应为 {len(self.market_feature_names)}，实际 {market_features.shape[1]}"
            )

        # 使用训练时 fit 的 scaler 做标准化
        sf = torch.tensor(
            self.scaler.transform(stock_features), dtype=torch.float32, device=self.device
        )
        mf = torch.tensor(
            self.market_scaler.transform(market_features), dtype=torch.float32, device=self.device
        )

        with torch.no_grad():
            out = self.model(sf, mf, return_details=True)

        scores = out["scores"].cpu().numpy()
        ranks = np.argsort(-scores, kind="stable")  # 分数越高 rank 越小

        stocks = []
        for pos, idx in enumerate(ranks):
            stocks.append(
                {
                    "symbol": symbols[idx],
                    "score": float(scores[idx]),
                    "rank": int(pos + 1),
                    "expert_scores": (
                        out["expert_scores"][idx].cpu().numpy().tolist()
                        if out["expert_scores"] is not None
                        else None
                    ),
                }
            )

        gate = out["gate_weights"]
        result: Dict[str, Any] = {"stocks": stocks}
        if gate is not None:
            g = gate.cpu().numpy().tolist()
            result["gate_weights"] = {
                f"expert_{i + 1}": float(g[i]) for i in range(len(g))
            }
        else:
            result["gate_weights"] = None
        return result
