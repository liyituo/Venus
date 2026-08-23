from __future__ import annotations

import torch.nn as nn
from torch import Tensor


class BaseHead(nn.Module):
    def __init__(
        self,
        embedding_dim: int = 64,
        hidden_dim: int = 32,
        dropout: float = 0.05,
    ) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(embedding_dim, hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, 1),
        )

    def forward(self, h: Tensor) -> Tensor:
        return self.net(h)
