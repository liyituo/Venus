from __future__ import annotations

from typing import Any

import torch
import torch.nn.functional as F
from torch import Tensor, nn

from .base_head import BaseHead
from .experts import ExpertEnsemble
from .factor_encoder import FactorEncoder
from .market_encoder import MarketEncoder


class TinyMoE(nn.Module):
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

        self.factor_encoder = FactorEncoder(num_features, factor_hidden_dim, embedding_dim, dropout)

        if use_moe:
            self.market_encoder = MarketEncoder(
                num_market_features, market_hidden_dim, market_embedding_dim
            )
            self.experts = ExpertEnsemble(num_experts, embedding_dim, expert_hidden_dim, dropout)
            if use_market_gate:
                if use_cross_section_gate:
                    gate_in_dim = market_embedding_dim + 2 * embedding_dim
                    self.gate_net = nn.Sequential(
                        nn.Linear(gate_in_dim, 64),
                        nn.GELU(),
                        nn.Dropout(0.05),
                        nn.Linear(64, num_experts),
                    )
                else:
                    self.gate_net = nn.Sequential(
                        nn.Linear(market_embedding_dim, market_embedding_dim),
                        nn.GELU(),
                        nn.Linear(market_embedding_dim, num_experts),
                    )
            else:
                self.gate_logits = nn.Parameter(torch.zeros(num_experts))
        else:
            self.head = nn.Linear(embedding_dim, 1)

        if use_base_head:
            self.base_head = BaseHead(embedding_dim)
            if learnable_moe_scale:
                self.moe_scale = nn.Parameter(torch.tensor(float(moe_scale_init)))
            else:
                self.register_buffer("moe_scale", torch.tensor(float(moe_scale_init)))

    def _gate(self, z: Tensor, h: Tensor) -> Tensor:
        if self.use_cross_section_gate:
            cs_mean = h.mean(dim=0, keepdim=True)
            cs_std = h.std(dim=0, keepdim=True, unbiased=False)
            cs_state = torch.cat([cs_mean, cs_std], dim=1)
            gate_in = torch.cat([z, cs_state], dim=1)
        else:
            gate_in = z
        logits = self.gate_net(gate_in)
        return torch.softmax(logits / self.gate_temperature, dim=-1)

    def forward(
        self,
        stock_features: Tensor,
        market_features: Tensor | None = None,
        return_details: bool = False,
    ) -> Tensor | dict[str, Any]:
        h = self.factor_encoder(stock_features)

        if not self.use_moe:
            scores = self.head(h).squeeze(-1)
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
            market_features = market_features.unsqueeze(0)
        z = self.market_encoder(market_features)

        if self.use_market_gate:
            g = self._gate(z, h)
        else:
            g = F.softmax(self.gate_logits / self.gate_temperature, dim=0).unsqueeze(0)

        e = self.experts(h)
        moe_scores = (e * g).sum(dim=1)

        if self.use_base_head:
            base_scores = self.base_head(h).squeeze(-1)
            scores = base_scores + self.moe_scale * moe_scores
        else:
            base_scores = None
            scores = moe_scores

        if return_details:
            return {
                "scores": scores,
                "gate_weights": g.squeeze(0),
                "expert_scores": e,
                "stock_embeddings": h,
                "base_scores": base_scores,
                "moe_scores": moe_scores,
                "moe_scale": getattr(self, "moe_scale", None),
            }
        return scores


def build_model(config: dict[str, Any], num_features: int, num_market_features: int) -> nn.Module:
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
