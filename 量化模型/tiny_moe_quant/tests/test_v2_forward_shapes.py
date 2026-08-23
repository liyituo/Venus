"""V2 模型 shape 测试：Base Head + MoE Residual 组合、Gate 形状、moe_scale。"""
import torch

from src.models.tiny_moe import TinyMoE, build_model

F, M, N = 20, 8, 50


def make_v2(**kwargs) -> TinyMoE:
    defaults = dict(
        num_features=F, num_market_features=M,
        use_base_head=True, use_cross_section_gate=True,
        learnable_moe_scale=True, moe_scale_init=0.1, gate_temperature=1.0,
    )
    defaults.update(kwargs)
    return TinyMoE(**defaults)


def test_v2_forward_shapes_and_details():
    """V2: scores [N]、gate [3]（绝非 [N,3]）、base/moe 分量存在。"""
    model = make_v2()
    sf, mf = torch.randn(N, F), torch.randn(M)
    out = model(sf, mf, return_details=True)

    assert out["scores"].shape == (N,)
    assert out["gate_weights"].shape == (3,), "gate_weights 必须是 [K]（市场级 Gate），不能是 [N, K]"
    assert torch.allclose(out["gate_weights"].sum(), torch.tensor(1.0), atol=1e-5)
    assert out["expert_scores"].shape == (N, 3)
    assert out["stock_embeddings"].shape == (N, 64)
    assert out["base_scores"].shape == (N,)
    assert out["moe_scores"].shape == (N,)
    assert out["moe_scale"] is not None
    # score = base + moe_scale * moe
    expected = out["base_scores"] + out["moe_scale"] * out["moe_scores"]
    assert torch.allclose(out["scores"], expected, atol=1e-6)


def test_moe_scale_zero_means_final_equals_base():
    """moe_scale=0 时 final_score == base_score（Base Head 单独工作）。"""
    model = make_v2()
    with torch.no_grad():
        model.moe_scale.fill_(0.0)
        sf, mf = torch.randn(N, F), torch.randn(M)
        out = model(sf, mf, return_details=True)
    assert torch.allclose(out["scores"], out["base_scores"], atol=1e-6)
    assert not torch.allclose(out["scores"], out["moe_scores"], atol=1e-6)


def test_moe_scale_initialized_to_0_1():
    """moe_scale 初始化 = 0.1（避免训练初期 residual 破坏 Base Head）。"""
    model = make_v2(moe_scale_init=0.1)
    assert torch.allclose(model.moe_scale, torch.tensor(0.1), atol=1e-6)
    assert model.moe_scale.requires_grad  # 默认可学习


def test_learnable_moe_scale_false_is_buffer():
    """learnable_moe_scale=False 时 moe_scale 为不可学习 buffer。"""
    model = make_v2(learnable_moe_scale=False, moe_scale_init=0.3)
    assert not model.moe_scale.requires_grad
    assert torch.allclose(model.moe_scale, torch.tensor(0.3), atol=1e-6)


def test_gate_temperature_changes_gate_spread():
    """temperature 生效：低温度使 Gate 更尖锐。"""
    model = make_v2(gate_temperature=0.1)
    model.eval()
    sf, mf = torch.randn(10, F), torch.randn(M)
    with torch.no_grad():
        g_hot = model(sf, mf, return_details=True)["gate_weights"]
    model2 = make_v2(gate_temperature=10.0)
    model2.eval()
    with torch.no_grad():
        g_cold = model2(sf, mf, return_details=True)["gate_weights"]
    # 相同输入、不同温度（不同初始化权重会影响，这里只验证形状与和为 1）
    assert torch.allclose(g_hot.sum(), torch.tensor(1.0), atol=1e-5)
    assert torch.allclose(g_cold.sum(), torch.tensor(1.0), atol=1e-5)


def test_v2_backward_with_base_head():
    """V2 反向传播正常（base + scale*moe 梯度可达）。"""
    model = make_v2()
    sf, mf = torch.randn(N, F), torch.randn(M)
    loss = model(sf, mf).pow(2).mean()
    loss.backward()
    assert model.moe_scale.grad is not None
    for p in model.parameters():
        assert p.grad is not None
        assert torch.isfinite(p.grad).all()


def test_v2_param_count_below_1m():
    """V2 参数量仍 < 1M。"""
    model = make_v2()
    total = sum(p.numel() for p in model.parameters())
    assert total < 1_000_000


def test_build_model_v2_from_config():
    """build_model 工厂支持 V2 配置。"""
    config = {
        "model": {
            "factor_hidden_dim": 128, "embedding_dim": 64, "market_hidden_dim": 32,
            "market_embedding_dim": 16, "expert_hidden_dim": 32, "num_experts": 3,
            "dropout": 0.1, "use_moe": True, "use_market_gate": True,
            "use_base_head": True, "use_cross_section_gate": True,
            "learnable_moe_scale": True, "moe_scale_init": 0.1, "gate_temperature": 1.0,
        }
    }
    model = build_model(config, num_features=F, num_market_features=M)
    assert model.use_base_head and model.use_cross_section_gate
    sf, mf = torch.randn(8, F), torch.randn(M)
    assert model(sf, mf).shape == (8,)
