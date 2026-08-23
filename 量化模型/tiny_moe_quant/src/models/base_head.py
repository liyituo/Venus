"""Base Head：学习稳定的全局 Alpha，MoE 只负责市场状态相关的 residual 修正。"""
from __future__ import annotations

import torch.nn as nn
from torch import Tensor


class BaseHead(nn.Module):
    """V2 基础打分头。

    结构:
        64 -> Linear(64, 32) -> GELU -> Dropout(0.05) -> Linear(32, 1)

    输入 stock_embedding h: [N, 64]
    输出 base_scores: [N, 1]（squeeze 后 [N]）
    """

    def __init__(
        self,
        embedding_dim: int = 64,
        hidden_dim: int = 32,
        dropout: float = 0.05,
    ) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(embedding_dim, hidden_dim),  # [N, 64] -> [N, 32]
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, 1),              # [N, 32] -> [N, 1]
        )

    def forward(self, h: Tensor) -> Tensor:
        # [N, 64] -> [N, 1]
        return self.net(h)
