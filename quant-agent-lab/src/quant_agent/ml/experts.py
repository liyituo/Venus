from __future__ import annotations

import torch
import torch.nn as nn
from torch import Tensor


class Expert(nn.Module):
    def __init__(
        self,
        embedding_dim: int = 64,
        hidden_dim: int = 32,
        dropout: float = 0.1,
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


class ExpertEnsemble(nn.Module):
    def __init__(
        self,
        num_experts: int = 3,
        embedding_dim: int = 64,
        hidden_dim: int = 32,
        dropout: float = 0.1,
    ) -> None:
        super().__init__()
        self.experts = nn.ModuleList(
            [Expert(embedding_dim, hidden_dim, dropout) for _ in range(num_experts)]
        )

    def forward(self, h: Tensor) -> Tensor:
        return torch.cat([expert(h) for expert in self.experts], dim=1)
