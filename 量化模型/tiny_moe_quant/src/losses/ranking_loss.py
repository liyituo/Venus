"""同日内 Pairwise Ranking Loss 与 MoE Balance Loss。

重要约束：Ranking Loss 只允许在同一个交易日的横截面内部构造 pair，
绝不对不同日期的股票互相排序。本模块只接收"单日"的 scores/labels。
"""
from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

import torch
import torch.nn.functional as F
from torch import Tensor


def sample_valid_pairs(
    labels: Tensor,
    pair_margin: float = 0.1,
    max_pairs: int = 4096,
    generator: Optional[torch.Generator] = None,
) -> Optional[Tensor]:
    """在单日横截面内采样 |label_i - label_j| > pair_margin 的 (i, j) 对。

    参数:
        labels: [N]，当日所有股票的标签
        pair_margin: 只保留标签差大于该值的 pair，避免噪声对
        max_pairs: 最大采样对数（不构造全部 N^2 对）
        generator: torch.Generator，用于可复现采样

    返回:
        pairs: [P, 2] 或 None（没有有效 pair）。
        第一列 i 的 label 严格更高（label_i > label_j + margin）。
        返回的索引基于"按 label 降序排序后"的位置，配合 sorted scores 使用。
    """
    n = labels.numel()
    if n < 2:
        return None

    sorted_labels, _ = torch.sort(labels, descending=True)  # 降序
    # 升序副本，配合 searchsorted 统计"严格小于阈值"的元素个数
    asc = sorted_labels.flip(0)
    thresholds = sorted_labels - pair_margin
    # side='left': 返回升序数组中第一个 >= threshold 的位置 = 严格小于 threshold 的个数
    left_counts = torch.searchsorted(asc, thresholds, side="left")

    # 有至少一个有效 j 的候选 i（在降序位置中）
    valid_i = torch.nonzero(left_counts > 0, as_tuple=False).squeeze(-1)
    if valid_i.numel() == 0:
        return None

    num_pairs = min(max_pairs, valid_i.numel())
    # 在 CPU 上用固定种子生成随机索引，再移到 labels 所在设备（GPU 上不依赖 CUDA generator）
    rand_i = torch.randint(
        0, valid_i.numel(), (num_pairs,), generator=generator, device="cpu"
    ).to(labels.device)
    i_pos = valid_i[rand_i]
    counts = left_counts[i_pos].to(torch.float32)
    # 每个 i 在 [0, count_i) 内均匀采样 j 的升序位置
    j_asc = (
        torch.rand(num_pairs, generator=generator, device="cpu").to(labels.device) * counts
    ).long()
    # 升序位置 -> 降序位置
    j_pos = (n - 1) - j_asc
    return torch.stack([i_pos, j_pos], dim=1)


def compute_rank_loss(
    scores: Tensor,
    labels: Tensor,
    pair_margin: float = 0.1,
    max_pairs: int = 4096,
    generator: Optional[torch.Generator] = None,
    ranking_type: str = "normal",
    top_weight_mode: str = "continuous",
    top_weight_strength: float = 2.0,
    top_weight_power: float = 3.0,
) -> Tuple[Tensor, int]:
    """Pairwise Ranking Loss（单日）。

    L_rank = mean(pair_weight * softplus(-(score_i - score_j)))，其中 label_i > label_j。

    ranking_type:
        - "normal": 所有 pair 等权（V1 行为）
        - "top_heavy": 根据更优股票 i 的当日 label 横截面 percentile 加权，
          更关注真实收益排名靠前的股票

    top_weight_mode（top_heavy 时生效）:
        - "discrete":   Top 10% -> 3.0；Top 10%-30% -> 1.5；其他 -> 1.0
        - "continuous": weight = 1 + top_weight_strength * p ** top_weight_power

    返回 (loss, num_pairs)。
    """
    device = scores.device
    pairs = sample_valid_pairs(labels, pair_margin, max_pairs, generator)
    if pairs is None:
        return torch.zeros((), device=device), 0

    # 按 label 降序排序 scores，使 pair 索引直接可用
    sorted_labels, order = torch.sort(labels, descending=True)
    sorted_scores = scores[order]  # [N]
    i = pairs[:, 0]
    j = pairs[:, 1]
    diff = sorted_scores[i] - sorted_scores[j]  # [P]
    loss_ij = F.softplus(-diff)                 # [P]

    if ranking_type == "normal":
        return loss_ij.mean(), pairs.shape[0]

    if ranking_type == "top_heavy":
        n = labels.numel()
        # 更优股票 i 的横截面 percentile p ∈ (0, 1]，p 越接近 1 代表未来收益越高
        p = (n - i).to(torch.float32) / n  # i 为降序位置: 第 0 位(最高 label) -> p=1
        if top_weight_mode == "discrete":
            w = torch.ones_like(p)
            w[p > 0.9] = 3.0              # Top 10%
            w[(p > 0.7) & (p <= 0.9)] = 1.5  # Top 10%-30%
        elif top_weight_mode == "continuous":
            w = 1.0 + top_weight_strength * torch.pow(p, top_weight_power)
        else:
            raise ValueError(f"未知 top_weight_mode: {top_weight_mode}")
        return (w * loss_ij).mean(), pairs.shape[0]

    raise ValueError(f"未知 ranking_type: {ranking_type}")


class TopHeavyPairwiseRankingLoss:
    """Top-heavy Pairwise Ranking Loss（面向 Top-K 选股）。

    关注真实收益排名靠前的股票：pair 权重基于更优股票 i 的当日 label percentile。
    与普通 pairwise loss 一样，只在同一天内部构造 pair，
    保留 pair_margin 与 max_pairs_per_day 限制（不构造 N^2 对）。
    """

    def __init__(
        self,
        top_weight_mode: str = "continuous",
        top_weight_strength: float = 2.0,
        top_weight_power: float = 3.0,
        pair_margin: float = 0.1,
        max_pairs_per_day: int = 4096,
        generator: Optional[torch.Generator] = None,
    ) -> None:
        self.top_weight_mode = top_weight_mode
        self.top_weight_strength = top_weight_strength
        self.top_weight_power = top_weight_power
        self.pair_margin = pair_margin
        self.max_pairs_per_day = max_pairs_per_day
        self.generator = generator

    def __call__(self, scores: Tensor, labels: Tensor) -> Tuple[Tensor, int]:
        """返回 (loss, num_pairs)。"""
        return compute_rank_loss(
            scores, labels,
            pair_margin=self.pair_margin,
            max_pairs=self.max_pairs_per_day,
            generator=self.generator,
            ranking_type="top_heavy",
            top_weight_mode=self.top_weight_mode,
            top_weight_strength=self.top_weight_strength,
            top_weight_power=self.top_weight_power,
        )


def compute_balance_loss(
    gate_weights: Tensor,
    num_experts: int = 3,
    eps: float = 1e-8,
) -> Tensor:
    """MoE Balance Loss（entropy regularization）。

    L_balance = log(K) - entropy(g) = log(K) + sum(g * log(g + eps))

    均匀 Gate [1/3, 1/3, 1/3] 时 L_balance = 0；
    极端 Gate [1, 0, 0] 时 L_balance = log(K)。
    该正则只能很弱，作用是防止训练早期 Expert 塌缩，
    绝不强制 Gate 长期保持均匀。
    """
    g = gate_weights.squeeze() if gate_weights.dim() == 2 else gate_weights
    entropy = -(g * (g + eps).log()).sum()
    return torch.tensor(math.log(num_experts), device=g.device) - entropy


def hybrid_loss(
    scores: Tensor,
    labels: Tensor,
    gate_weights: Optional[Tensor],
    lambda_rank: float = 1.0,
    lambda_mse: float = 0.2,
    lambda_balance: float = 0.01,
    pair_margin: float = 0.1,
    max_pairs_per_day: int = 4096,
    use_balance_loss: bool = True,
    num_experts: int = 3,
    generator: Optional[torch.Generator] = None,
    ranking_type: str = "normal",
    top_weight_mode: str = "continuous",
    top_weight_strength: float = 2.0,
    top_weight_power: float = 3.0,
) -> Tuple[Tensor, Dict[str, Tensor]]:
    """混合损失（单日）:

    L = lambda_rank * L_rank + lambda_mse * L_mse + lambda_balance * L_balance

    L_rank 支持 normal / top_heavy（见 compute_rank_loss）。

    返回 (total_loss, {"rank_loss", "mse_loss", "balance_loss", "num_pairs"})。
    """
    rank_loss, num_pairs = compute_rank_loss(
        scores, labels, pair_margin, max_pairs_per_day, generator,
        ranking_type=ranking_type,
        top_weight_mode=top_weight_mode,
        top_weight_strength=top_weight_strength,
        top_weight_power=top_weight_power,
    )
    mse_loss = F.mse_loss(scores, labels)
    if use_balance_loss and gate_weights is not None:
        balance_loss = compute_balance_loss(gate_weights, num_experts)
    else:
        balance_loss = torch.zeros((), device=scores.device)

    total = lambda_rank * rank_loss + lambda_mse * mse_loss + lambda_balance * balance_loss
    return total, {
        "rank_loss": rank_loss,
        "mse_loss": mse_loss,
        "balance_loss": balance_loss,
        "num_pairs": torch.tensor(float(num_pairs), device=scores.device),
    }
