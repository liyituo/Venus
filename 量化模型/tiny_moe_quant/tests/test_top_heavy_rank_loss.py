"""Top-heavy Pairwise Ranking Loss 测试。"""
import math

import torch

from src.losses.ranking_loss import (
    TopHeavyPairwiseRankingLoss,
    compute_rank_loss,
    hybrid_loss,
)


def _g(seed: int = 0) -> torch.Generator:
    return torch.Generator().manual_seed(seed)


def test_normal_mode_unchanged():
    """normal 模式与 V1 等权 loss 一致（top-heavy 参数不影响）。"""
    scores, labels = torch.randn(20), torch.randn(20)
    l1 = compute_rank_loss(scores, labels, 0.1, 512, _g(0), ranking_type="normal")[0]
    l2 = compute_rank_loss(scores, labels, 0.1, 512, _g(0), ranking_type="normal",
                           top_weight_mode="continuous", top_weight_strength=2.0,
                           top_weight_power=3.0)[0]
    assert torch.allclose(l1, l2, atol=1e-8)


def test_top_heavy_weights_better_stock():
    """top_heavy: 更优股票 label 越高，pair 权重越大。"""
    n = 50
    # 实现中: p = (n - i) / n，i 为 label 降序位置（i=0 最高 -> p=1）
    p = (n - torch.arange(n, dtype=torch.float32)) / n
    w_discrete = torch.ones(n)
    w_discrete[p > 0.9] = 3.0
    w_discrete[(p > 0.7) & (p <= 0.9)] = 1.5
    # 边界检查
    assert w_discrete[0] == 3.0    # p=1.00 -> Top 10%
    assert w_discrete[4] == 3.0    # p=0.92 -> Top 10%
    assert w_discrete[5] == 1.5    # p=0.90 -> 严格 >0.9 不成立，落入 10-30%
    assert w_discrete[6] == 1.5    # p=0.88
    assert w_discrete[14] == 1.5   # p=0.72
    assert w_discrete[15] == 1.0   # p=0.70 -> 严格 >0.7 不成立
    assert w_discrete[20] == 1.0   # p=0.60
    # continuous 模式：label 越高（i 越小 -> p 越大）权重越大，单调递减（沿 i 方向）
    w_cont = 1.0 + 2.0 * torch.pow(p, 3.0)
    assert (w_cont.diff() <= 0).all()
    assert torch.allclose(w_cont[0], torch.tensor(3.0), atol=1e-6)   # p=1 -> 3.0
    assert torch.allclose(w_cont[-1], torch.tensor(1.0), atol=1e-3)  # p=0.02 -> ~1.0


def test_top_heavy_loss_emphasizes_top_pairs():
    """top_heavy loss 值 >= normal loss（高权重对总体贡献更大）。"""
    labels = torch.arange(30, dtype=torch.float32)
    # 分数略有噪声
    torch.manual_seed(0)
    scores = labels + 0.1 * torch.randn(30)
    l_normal = compute_rank_loss(scores, labels, 0.1, 2048, _g(1), ranking_type="normal")[0]
    l_top = compute_rank_loss(scores, labels, 0.1, 2048, _g(1), ranking_type="top_heavy",
                              top_weight_mode="continuous")[0]
    assert l_top >= l_normal  # 高权重 pair 加权后 loss 不应变小
    assert l_top < 1.0  # 合理量级


def test_top_heavy_respects_margin_and_same_day():
    """top_heavy 保留 pair_margin 与同日约束。"""
    labels = torch.tensor([0.0, 0.04, -0.04])  # 任意两对差 < 0.1
    loss, n = compute_rank_loss(torch.randn(3), labels, 0.1, 100, _g(0),
                                ranking_type="top_heavy")
    assert n == 0 and loss.item() == 0.0


def test_class_api():
    """TopHeavyPairwiseRankingLoss 类接口。"""
    loss_fn = TopHeavyPairwiseRankingLoss(
        top_weight_mode="continuous", top_weight_strength=2.0, top_weight_power=3.0,
        pair_margin=0.1, max_pairs_per_day=512, generator=_g(0),
    )
    scores = torch.randn(20, requires_grad=True)
    labels = torch.randn(20)
    loss, n = loss_fn(scores, labels)
    assert n > 0
    loss.backward()
    assert scores.grad is not None


def test_hybrid_loss_with_top_heavy():
    """hybrid_loss 支持 ranking_type=top_heavy。"""
    scores, labels = torch.randn(15), torch.randn(15)
    gate = torch.tensor([0.5, 0.3, 0.2])
    total, parts = hybrid_loss(
        scores, labels, gate,
        lambda_rank=1.0, lambda_mse=0.1, lambda_balance=0.005,
        pair_margin=0.1, max_pairs_per_day=256, use_balance_loss=True,
        generator=_g(0), ranking_type="top_heavy", top_weight_mode="continuous",
    )
    expected = 1.0 * parts["rank_loss"] + 0.1 * parts["mse_loss"] + 0.005 * parts["balance_loss"]
    assert torch.allclose(total, expected, atol=1e-6)


def test_reproducible():
    """top_heavy 同种子可复现。"""
    scores, labels = torch.randn(20), torch.randn(20)
    l1 = compute_rank_loss(scores, labels, 0.1, 512, _g(7), ranking_type="top_heavy")[0]
    l2 = compute_rank_loss(scores, labels, 0.1, 512, _g(7), ranking_type="top_heavy")[0]
    assert torch.allclose(l1, l2, atol=1e-8)
