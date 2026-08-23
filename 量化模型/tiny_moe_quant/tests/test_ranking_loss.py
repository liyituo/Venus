"""Pairwise Ranking Loss 与 Balance Loss 测试。"""
import math

import torch

from src.losses.ranking_loss import (
    compute_balance_loss,
    compute_rank_loss,
    hybrid_loss,
    sample_valid_pairs,
)


def _seed_generator(seed: int = 0) -> torch.Generator:
    return torch.Generator().manual_seed(seed)


def test_rank_loss_lower_when_scores_agree_with_labels():
    """分数与标签顺序一致时 loss 应显著更低。"""
    labels = torch.tensor([3.0, 2.0, 1.0, 0.0, -1.0, -2.0])
    good = compute_rank_loss(labels.clone(), labels, 0.5, 4096, _seed_generator(0))[0]
    bad = compute_rank_loss(-labels.clone(), labels, 0.5, 4096, _seed_generator(0))[0]
    n_good = compute_rank_loss(labels.clone(), labels, 0.5, 4096, _seed_generator(0))[1]
    assert n_good > 0  # 确保确实采样到了 pair
    assert good < bad
    assert good < 0.5  # 顺序一致时 softplus(-diff) 应较小
    assert bad > 1.0   # 顺序完全颠倒时 loss 应明显更大


def test_pairs_respect_margin():
    """|label_i - label_j| <= pair_margin 的 pair 不应被采样。"""
    labels = torch.tensor([0.0, 0.04, -0.04])  # 任意两对之差都 < 0.1
    loss, n = compute_rank_loss(torch.randn(3), labels, 0.1, 100, _seed_generator(1))
    assert n == 0
    assert loss.item() == 0.0


def test_all_sampled_pairs_have_strictly_higher_label():
    """采样出的每个 pair，第一项的 label 必须严格大于第二项 + margin。"""
    labels = torch.arange(40, dtype=torch.float32)
    margin = 0.5
    pairs = sample_valid_pairs(labels, margin, 4096, _seed_generator(0))
    sorted_labels, _ = torch.sort(labels, descending=True)
    diffs = sorted_labels[pairs[:, 0]] - sorted_labels[pairs[:, 1]]
    assert (diffs > margin).all()
    assert pairs.shape[0] <= 4096


def test_pairs_never_mixed_across_days():
    """sample_valid_pairs 只接收单日向量 —— 接口层面杜绝跨日 pair。"""
    labels = torch.tensor([1.0, 2.0, 3.0, 4.0])
    pairs = sample_valid_pairs(labels, 0.1, 4096, _seed_generator(0))
    assert pairs is not None
    # 返回的索引都在 [0, 4) 内
    assert pairs.min() >= 0 and pairs.max() < 4


def test_balance_loss_uniform_gate_is_zero():
    """均匀 Gate [1/3,1/3,1/3] -> entropy = log(3) -> loss = 0。"""
    g = torch.tensor([1 / 3, 1 / 3, 1 / 3])
    assert torch.allclose(compute_balance_loss(g, 3), torch.tensor(0.0), atol=1e-5)


def test_balance_loss_extreme_gate_equals_log3():
    """极端 Gate [1,0,0] -> entropy = 0 -> loss = log(3)。"""
    g = torch.tensor([1.0, 0.0, 0.0])
    assert torch.allclose(compute_balance_loss(g, 3), torch.tensor(math.log(3)), atol=1e-5)


def test_balance_loss_2d_input():
    """Gate 为 [1, K] 时与 [K] 结果一致。"""
    g1 = torch.tensor([0.5, 0.3, 0.2])
    g2 = g1.unsqueeze(0)
    assert torch.allclose(compute_balance_loss(g1, 3), compute_balance_loss(g2, 3), atol=1e-6)


def test_hybrid_loss_weights():
    """混合损失 = lambda 加权和，且组件可单独关闭。"""
    scores, labels = torch.randn(12), torch.randn(12)
    gate = torch.tensor([0.5, 0.3, 0.2])
    total, parts = hybrid_loss(
        scores, labels, gate,
        lambda_rank=1.0, lambda_mse=0.2, lambda_balance=0.01,
        pair_margin=0.1, max_pairs_per_day=100, use_balance_loss=True,
        generator=_seed_generator(0),
    )
    expected = (
        1.0 * parts["rank_loss"] + 0.2 * parts["mse_loss"] + 0.01 * parts["balance_loss"]
    )
    assert torch.allclose(total, expected, atol=1e-6)
    assert parts["balance_loss"].item() >= 0

    # 关闭 balance loss
    total2, parts2 = hybrid_loss(
        scores, labels, gate,
        lambda_rank=1.0, lambda_mse=0.2, lambda_balance=0.01,
        pair_margin=0.1, max_pairs_per_day=100, use_balance_loss=False,
        generator=_seed_generator(0),
    )
    assert torch.allclose(total2, 1.0 * parts2["rank_loss"] + 0.2 * parts2["mse_loss"], atol=1e-6)
    assert parts2["balance_loss"].item() == 0.0


def test_hybrid_loss_reproducible_with_same_seed():
    scores, labels = torch.randn(20), torch.randn(20)
    gate = torch.tensor([0.4, 0.4, 0.2])
    l1, _ = hybrid_loss(scores, labels, gate, pair_margin=0.2, max_pairs_per_day=200,
                        generator=_seed_generator(7))
    l2, _ = hybrid_loss(scores, labels, gate, pair_margin=0.2, max_pairs_per_day=200,
                        generator=_seed_generator(7))
    assert torch.allclose(l1, l2, atol=1e-8)


def test_rank_loss_backward():
    scores = torch.randn(10, requires_grad=True)
    labels = torch.randn(10)
    loss, n = compute_rank_loss(scores, labels, 0.1, 100, _seed_generator(0))
    if n > 0:
        loss.backward()
        assert scores.grad is not None
        assert torch.isfinite(scores.grad).all()
