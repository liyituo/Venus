"""Tiny-MoE Quant Ranker 主模型（V1 + V2）。

V1（A3 保持不动）:
    score = Σ_k g_k * E_k(h)            # g = softmax(Gate(market_embedding))，市场级 Gate

V2（A4，全部由 config 控制）:
    base_i = BaseHead(h_i)                                    # 稳定全局 Alpha
    moe_i  = Σ_k g_k * E_k(h_i)                               # 市场状态相关 residual
    score_i = base_i + moe_scale * moe_i                      # moe_scale 默认 0.1 可学习

V2 Gate 输入 = market_embedding(16) + cross_section_state(128) = 144:
    cs_mean = h.mean(dim=0), cs_std = h.std(dim=0, unbiased=False)
    （只使用 t 日可见的 stock embeddings，不含任何未来信息）

所有开关:
    use_moe / use_market_gate          （V1 已有，A1/A2 消融）
    use_base_head / use_cross_section_gate / learnable_moe_scale
    moe_scale_init / gate_temperature
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import torch
import torch.nn.functional as F
from torch import Tensor, nn

from .base_head import BaseHead
from .experts import ExpertEnsemble
from .factor_encoder import FactorEncoder
from .market_encoder import MarketEncoder


class TinyMoE(nn.Module):
    """Tiny-MoE Quant Ranker。

    forward(stock_features: [N, F], market_features: [M] 或 [1, M], return_details: bool)

    return_details=True 时返回:
        {"scores": [N], "gate_weights": [K], "expert_scores": [N, K],
         "stock_embeddings": [N, D],
         "base_scores": [N] 或 None, "moe_scores": [N],
         "moe_scale": 标量或 None}
    """

    def __init__(
        self,
        num_features: int,
        num_market_features: int,
        factor_hidden_dim: int = 128,
        embedding_dim: int = 64,
        market_hidden_dim: int = 32,
        market_embedding_dim: int = 16,
        expert_hidden_dim: int = 32,
        num_experts: int = 3,
        dropout: float = 0.1,
        use_moe: bool = True,
        use_market_gate: bool = True,
        use_base_head: bool = False,
        use_cross_section_gate: bool = False,
        learnable_moe_scale: bool = True,
        moe_scale_init: float = 0.1,
        gate_temperature: float = 1.0,
    ) -> None:
        super().__init__()
        self.use_moe = use_moe
        self.use_market_gate = use_market_gate
        self.use_base_head = use_base_head
        self.use_cross_section_gate = use_cross_section_gate
        self.num_experts = num_experts
        self.embedding_dim = embedding_dim
        self.market_embedding_dim = market_embedding_dim
        self.gate_temperature = gate_temperature

        # 股票侧: 所有变体共享 FactorEncoder
        self.factor_encoder = FactorEncoder(num_features, factor_hidden_dim, embedding_dim, dropout)

        if use_moe:
            # 市场侧
            self.market_encoder = MarketEncoder(
                num_market_features, market_hidden_dim, market_embedding_dim
            )
            self.experts = ExpertEnsemble(num_experts, embedding_dim, expert_hidden_dim, dropout)
            if use_market_gate:
                if use_cross_section_gate:
                    # V2 Gate: market_embedding(16) + cross_section_state(2*64=128) -> 144
                    gate_in_dim = market_embedding_dim + 2 * embedding_dim
                    self.gate_net = nn.Sequential(
                        nn.Linear(gate_in_dim, 64),      # [1, 144] -> [1, 64]
                        nn.GELU(),
                        nn.Dropout(0.05),
                        nn.Linear(64, num_experts),      # [1, 64] -> [1, K]
                    )
                else:
                    # V1 Gate（A3 原结构，仅去掉 Softmax 层，forward 统一 softmax(logits/T)，T=1 时完全等价）
                    self.gate_net = nn.Sequential(
                        nn.Linear(market_embedding_dim, market_embedding_dim),  # [1, 16] -> [1, 16]
                        nn.GELU(),
                        nn.Linear(market_embedding_dim, num_experts),           # [1, 16] -> [1, K]
                    )
            else:
                # 无市场 Gate: 固定可学习权重 softmax([w1, w2, w3])（A2）
                self.gate_logits = nn.Parameter(torch.zeros(num_experts))
        else:
            # MLP Ranker 输出头（A1）
            self.head = nn.Linear(embedding_dim, 1)

        if use_base_head:
            self.base_head = BaseHead(embedding_dim)
            if learnable_moe_scale:
                # 可学习标量，初始化 0.1：避免训练初期 MoE residual 破坏 Base Head
                self.moe_scale = nn.Parameter(torch.tensor(float(moe_scale_init)))
            else:
                self.register_buffer("moe_scale", torch.tensor(float(moe_scale_init)))

    def _gate(self, z: Tensor, h: Tensor) -> Tensor:
        """市场级 Gate：同一天所有股票共享 [1, K]（绝不生成 [N, K]）。"""
        if self.use_cross_section_gate:
            # 当日横截面 embedding 统计量（只用 t 日可见数据）
            cs_mean = h.mean(dim=0, keepdim=True)                            # [1, D]
            cs_std = h.std(dim=0, keepdim=True, unbiased=False)              # [1, D]
            cs_state = torch.cat([cs_mean, cs_std], dim=1)                   # [1, 2D]
            gate_in = torch.cat([z, cs_state], dim=1)                        # [1, 16+2D]
        else:
            gate_in = z                                                      # [1, 16]
        logits = self.gate_net(gate_in)                                      # [1, K]
        return torch.softmax(logits / self.gate_temperature, dim=-1)         # [1, K]

    def forward(
        self,
        stock_features: Tensor,
        market_features: Optional[Tensor] = None,
        return_details: bool = False,
    ) -> Tensor | Dict[str, Any]:
        h = self.factor_encoder(stock_features)  # [N, F] -> [N, D]

        if not self.use_moe:
            # MLP Ranker（A1）: FactorEncoder -> Linear -> score
            scores = self.head(h).squeeze(-1)  # [N]
            if return_details:
                return {
                    "scores": scores,
                    "gate_weights": None,
                    "expert_scores": None,
                    "stock_embeddings": h,
                    "base_scores": None,
                    "moe_scores": None,
                    "moe_scale": None,
                }
            return scores

        if market_features is None:
            raise ValueError("use_moe=True 时必须提供 market_features")
        if market_features.dim() == 1:
            market_features = market_features.unsqueeze(0)  # [1, M]
        z = self.market_encoder(market_features)  # [1, 16]

        if self.use_market_gate:
            g = self._gate(z, h)  # [1, K]
        else:
            g = F.softmax(self.gate_logits / self.gate_temperature, dim=0).unsqueeze(0)  # [1, K]

        e = self.experts(h)          # [N, K]
        moe_scores = (e * g).sum(dim=1)  # [N]（广播 [N,K] * [1,K]）

        if self.use_base_head:
            base_scores = self.base_head(h).squeeze(-1)  # [N]
            scores = base_scores + self.moe_scale * moe_scores  # score = base + α * residual
        else:
            base_scores = None
            scores = moe_scores

        if return_details:
            return {
                "scores": scores,
                "gate_weights": g.squeeze(0),  # [K]
                "expert_scores": e,            # [N, K]
                "stock_embeddings": h,         # [N, D]
                "base_scores": base_scores,    # [N] 或 None
                "moe_scores": moe_scores,      # [N]
                "moe_scale": getattr(self, "moe_scale", None),
            }
        return scores


def build_model(config: Dict[str, Any], num_features: int, num_market_features: int) -> nn.Module:
    """根据配置构建模型（用于训练 / 推理的统一入口）。"""
    m = config["model"]
    return TinyMoE(
        num_features=num_features,
        num_market_features=num_market_features,
        factor_hidden_dim=m["factor_hidden_dim"],
        embedding_dim=m["embedding_dim"],
        market_hidden_dim=m["market_hidden_dim"],
        market_embedding_dim=m["market_embedding_dim"],
        expert_hidden_dim=m["expert_hidden_dim"],
        num_experts=m["num_experts"],
        dropout=m["dropout"],
        use_moe=m["use_moe"],
        use_market_gate=m["use_market_gate"],
        use_base_head=bool(m.get("use_base_head", False)),
        use_cross_section_gate=bool(m.get("use_cross_section_gate", False)),
        learnable_moe_scale=bool(m.get("learnable_moe_scale", True)),
        moe_scale_init=float(m.get("moe_scale_init", 0.1)),
        gate_temperature=float(m.get("gate_temperature", 1.0)),
    )
