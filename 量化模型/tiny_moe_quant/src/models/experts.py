"""MoE Expert：三个参数独立、结构相同的轻量打分器。"""
from __future__ import annotations

import torch
import torch.nn as nn
from torch import Tensor


class Expert(nn.Module):
    """单个 Expert。

    结构:
        64 -> Linear(64, 32) -> GELU -> Dropout(0.1) -> Linear(32, 1)

    输入 stock_embedding h: [N, 64]
    输出 expert score: [N, 1]

    第一阶段不人为规定 expert 分工（trend/reversal/risk），让它们自由学习。
    """

    def __init__(
        self,
        embedding_dim: int = 64,
        hidden_dim: int = 32,
        dropout: float = 0.1,
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


class ExpertEnsemble(nn.Module):
    """num_experts 个独立 Expert 的集合，输出每个 Expert 的单独打分。"""

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
        """返回 expert_scores: [N, num_experts]，每列对应一个 Expert 的独立输出。"""
        return torch.cat([expert(h) for expert in self.experts], dim=1)  # [N, num_experts]
