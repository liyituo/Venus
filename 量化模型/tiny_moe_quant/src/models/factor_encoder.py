"""因子编码器：将原始股票因子映射为股票 embedding。"""
from __future__ import annotations

import torch.nn as nn
from torch import Tensor


class FactorEncoder(nn.Module):
    """股票因子编码器。

    结构:
        F -> Linear(F, 128) -> LayerNorm -> GELU -> Dropout(0.1)
          -> Linear(128, 64) -> GELU -> h (64)

    输入 stock_features: [N, F]，N 为当日股票数量
    输出 stock_embeddings: [N, embedding_dim]
    """

    def __init__(
        self,
        num_features: int,
        hidden_dim: int = 128,
        embedding_dim: int = 64,
        dropout: float = 0.1,
    ) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(num_features, hidden_dim),      # [N, F] -> [N, 128]
            nn.LayerNorm(hidden_dim),                 # 逐样本归一化，稳定训练
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, embedding_dim),     # [N, 128] -> [N, 64]
            nn.GELU(),
        )

    def forward(self, stock_features: Tensor) -> Tensor:
        # [N, F] -> [N, embedding_dim]
        return self.net(stock_features)
