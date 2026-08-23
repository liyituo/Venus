from __future__ import annotations

import torch.nn as nn
from torch import Tensor


class FactorEncoder(nn.Module):
    def __init__(
        self,
        num_features: int,
        hidden_dim: int = 128,
        embedding_dim: int = 64,
        dropout: float = 0.1,
    ) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(num_features, hidden_dim),
            nn.LayerNorm(hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, embedding_dim),
            nn.GELU(),
        )

    def forward(self, stock_features: Tensor) -> Tensor:
        return self.net(stock_features)
