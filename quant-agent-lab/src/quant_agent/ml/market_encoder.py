from __future__ import annotations

import torch.nn as nn
from torch import Tensor


class MarketEncoder(nn.Module):
    def __init__(
        self,
        num_market_features: int,
        hidden_dim: int = 32,
        embedding_dim: int = 16,
    ) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(num_market_features, hidden_dim),
            nn.GELU(),
            nn.Linear(hidden_dim, embedding_dim),
            nn.GELU(),
        )

    def forward(self, market_features: Tensor) -> Tensor:
        return self.net(market_features)
