from __future__ import annotations

from typing import Any

import numpy as np
import torch

from .tiny_moe import build_model


class QuantPredictor:
    """加载 best_model.pt 后对单日横截面排序。"""

    def __init__(self, checkpoint_path: str, device: str = "cpu") -> None:
        ckpt = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
        self.config: dict[str, Any] = ckpt["config"]
        self.feature_names: list[str] = list(ckpt["feature_names"])
        self.market_feature_names: list[str] = list(ckpt["market_feature_names"])
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
        symbols: list[str],
    ) -> dict[str, Any]:
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
                f"market_features 维度应为 {len(self.market_feature_names)}，"
                f"实际 {market_features.shape[1]}"
            )

        sf = torch.tensor(
            self.scaler.transform(stock_features), dtype=torch.float32, device=self.device
        )
        mf = torch.tensor(
            self.market_scaler.transform(market_features), dtype=torch.float32, device=self.device
        )

        with torch.no_grad():
            out = self.model(sf, mf, return_details=True)

        scores = out["scores"].cpu().numpy()
        ranks = np.argsort(-scores, kind="stable")

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
        result: dict[str, Any] = {"stocks": stocks}
        if gate is not None:
            g = gate.cpu().numpy().tolist()
            result["gate_weights"] = {f"expert_{i + 1}": float(g[i]) for i in range(len(g))}
        else:
            result["gate_weights"] = None
        return result
