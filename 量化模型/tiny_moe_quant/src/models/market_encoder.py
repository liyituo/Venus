"""市场编码器：将市场状态特征编码为市场 embedding（Gate 的输入）。"""
from __future__ import annotations

import torch.nn as nn
from torch import Tensor


class MarketEncoder(nn.Module):
    """市场状态编码器。

    结构:
        M -> Linear(M, 32) -> GELU -> Linear(32, 16) -> GELU -> z (16)

    输入 market_features: [1, M]（同一交易日所有股票共享同一市场状态）
    输出 market_embedding: [1, market_embedding_dim]
    """

    def __init__(
        self,
        num_market_features: int,
        hidden_dim: int = 32,
        embedding_dim: int = 16,
    ) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(num_market_features, hidden_dim),  # [1, M] -> [1, 32]
            nn.GELU(),
            nn.Linear(hidden_dim, embedding_dim),        # [1, 32] -> [1, 16]
            nn.GELU(),
        )

    def forward(self, market_features: Tensor) -> Tensor:
        # [1, M] -> [1, market_embedding_dim]
        return self.net(market_features)
