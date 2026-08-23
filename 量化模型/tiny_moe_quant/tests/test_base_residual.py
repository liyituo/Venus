"""Base Head + MoE Residual 组合测试（V2 核心结构）。

关键测试（Spec 三十三）:
    moe_scale = 0 时 final_score == base_score
    use_cross_section_gate = false 时 A3 逻辑不变
"""
import torch

from src.models.tiny_moe import TinyMoE

F, M, N = 20, 8, 30


def make_model(**kw) -> TinyMoE:
    defaults = dict(num_features=F, num_market_features=M)
    defaults.update(kw)
    return TinyMoE(**defaults)


def test_moe_scale_zero_final_equals_base():
    """moe_scale=0: final_score == base_score（Base Head 独立工作，residual 完全关闭）。"""
    model = make_model(use_base_head=True, use_cross_section_gate=True)
    model.eval()
    sf, mf = torch.randn(N, F), torch.randn(M)
    with torch.no_grad():
        model.moe_scale.fill_(0.0)
        out = model(sf, mf, return_details=True)
    assert torch.allclose(out["scores"], out["base_scores"], atol=1e-6)
    # 而 moe_scores 仍保留（供分析）
    assert out["moe_scores"].shape == (N,)


def test_moe_scale_positive_blends_base_and_moe():
    """moe_scale=0.5: score = base + 0.5 * moe。"""
    model = make_model(use_base_head=True, use_cross_section_gate=True)
    model.eval()
    sf, mf = torch.randn(N, F), torch.randn(M)
    with torch.no_grad():
        model.moe_scale.fill_(0.5)
        out = model(sf, mf, return_details=True)
    expected = out["base_scores"] + 0.5 * out["moe_scores"]
    assert torch.allclose(out["scores"], expected, atol=1e-6)


def test_no_base_head_v1_behavior():
    """use_base_head=False: score == moe_scores（A3/V1 行为不变）。"""
    model = make_model(use_base_head=False, use_cross_section_gate=False)
    model.eval()
    sf, mf = torch.randn(N, F), torch.randn(M)
    out = model(sf, mf, return_details=True)
    assert out["base_scores"] is None
    assert out["moe_scale"] is None
    assert torch.allclose(out["scores"], out["moe_scores"], atol=1e-8)


def test_base_head_shapes():
    """BaseHead 结构: 64 -> 32 -> GELU -> Dropout(0.05) -> 1。"""
    from src.models.base_head import BaseHead

    head = BaseHead(embedding_dim=64, hidden_dim=32, dropout=0.05)
    h = torch.randn(10, 64)
    assert head(h).shape == (10, 1)
    n_layers = len(head.net)
    assert n_layers == 4  # Linear, GELU, Dropout, Linear


def test_no_cross_section_gate_keeps_v1_gate():
    """use_cross_section_gate=False 时 gate_net 结构 == V1（16->16->GELU->16->3）。"""
    model = make_model(use_base_head=False, use_cross_section_gate=False)
    layers = list(model.gate_net)
    assert len(layers) == 3  # Linear, GELU, Linear（无 Softmax 模块，forward 统一 softmax(logits/T)）
    assert layers[0].in_features == 16
    assert layers[0].out_features == 16
    assert layers[2].out_features == 3


def test_cross_section_gate_net_shape():
    """V2 gate_net: Linear(144, 64) -> GELU -> Dropout -> Linear(64, 3)。"""
    model = make_model(use_base_head=True, use_cross_section_gate=True)
    layers = list(model.gate_net)
    assert layers[0].in_features == 16 + 2 * 64  # 144
    assert layers[0].out_features == 64
    assert layers[3].out_features == 3


def test_mlp_variant_unchanged():
    """use_moe=False（A1）不受 V2 开关影响。"""
    model = make_model(use_moe=False, use_base_head=True, use_cross_section_gate=True)
    model.eval()
    sf = torch.randn(N, F)
    out = model(sf, None, return_details=True)
    assert out["scores"].shape == (N,)
    assert out["base_scores"] is None
    assert out["gate_weights"] is None
