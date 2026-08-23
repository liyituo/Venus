"""模型 shape / 前向 / 反向 / 参数量 / 可复现性测试。"""
import pytest
import torch

from src.models.tiny_moe import TinyMoE, build_model
from src.training.trainer import count_parameters

F, M, N = 20, 8, 50


def make_model(**kwargs) -> TinyMoE:
    defaults = dict(num_features=F, num_market_features=M)
    defaults.update(kwargs)
    return TinyMoE(**defaults)


def test_scores_shape_and_gate_details():
    """默认（Full Tiny-MoE）: scores [N], gate 和为 1, expert [N,3], embedding [N,64]。"""
    model = make_model()
    sf = torch.randn(N, F)
    mf = torch.randn(M)
    out = model(sf, mf, return_details=True)

    assert out["scores"].shape == (N,)
    assert out["gate_weights"].shape == (3,)
    assert torch.allclose(out["gate_weights"].sum(), torch.tensor(1.0), atol=1e-5)
    assert torch.all(out["gate_weights"] >= 0)
    assert out["expert_scores"].shape == (N, 3)
    assert out["stock_embeddings"].shape == (N, 64)


def test_market_features_1d_and_2d_equivalent():
    """market_features 传 [M] 与 [1, M] 应得到相同结果。"""
    model = make_model()
    model.eval()
    sf = torch.randn(10, F)
    mf = torch.randn(M)
    with torch.no_grad():
        s1 = model(sf, mf, return_details=True)["scores"]
        s2 = model(sf, mf.unsqueeze(0), return_details=True)["scores"]
    assert torch.allclose(s1, s2, atol=1e-6)


def test_no_moe_is_mlp_ranker():
    """use_moe=False: MLP Ranker，无 gate / expert，且不需要市场特征。"""
    model = make_model(use_moe=False)
    sf = torch.randn(N, F)
    out = model(sf, None, return_details=True)  # MLP 忽略市场特征
    assert out["scores"].shape == (N,)
    assert out["gate_weights"] is None
    assert out["expert_scores"] is None
    assert out["stock_embeddings"].shape == (N, 64)


def test_no_market_gate_is_learnable_param():
    """use_market_gate=False: Gate 与市场输入无关（固定可学习参数）。"""
    model = make_model(use_market_gate=False)
    model.eval()  # 关闭 dropout，保证两次前向可比较
    sf = torch.randn(N, F)
    out1 = model(sf, torch.randn(M), return_details=True)
    out2 = model(sf, torch.randn(M), return_details=True)
    assert torch.allclose(out1["gate_weights"], out2["gate_weights"], atol=1e-8)
    assert torch.allclose(out1["scores"], out2["scores"], atol=1e-8)
    assert torch.allclose(out1["gate_weights"].sum(), torch.tensor(1.0), atol=1e-5)


def test_backward_pass_produces_gradients():
    model = make_model()
    sf, mf = torch.randn(N, F), torch.randn(M)
    loss = model(sf, mf).pow(2).mean()
    loss.backward()
    for p in model.parameters():
        assert p.grad is not None, f"参数 {p.shape} 缺少梯度"
        assert torch.isfinite(p.grad).all()


def test_param_count_below_1m():
    """第一阶段目标：参数量 < 1,000,000。"""
    model = make_model()
    counts = count_parameters(model)
    assert counts["total"] < 1_000_000
    assert counts["trainable"] == counts["total"]


def test_deterministic_forward():
    """固定种子后前向结果可复现（eval 模式，无 dropout 噪声）。"""
    torch.manual_seed(42)
    model = make_model()
    model.eval()
    sf, mf = torch.randn(N, F), torch.randn(M)
    with torch.no_grad():
        s1 = model(sf, mf).clone()
        s2 = model(sf, mf).clone()
    assert torch.allclose(s1, s2, atol=1e-9)


def test_build_model_from_config():
    """build_model 工厂：从 config dict 构建，字段完整。"""
    config = {
        "model": {
            "factor_hidden_dim": 128, "embedding_dim": 64, "market_hidden_dim": 32,
            "market_embedding_dim": 16, "expert_hidden_dim": 32, "num_experts": 3,
            "dropout": 0.1, "use_moe": True, "use_market_gate": True,
        }
    }
    model = build_model(config, num_features=F, num_market_features=M)
    assert isinstance(model, TinyMoE)
    sf, mf = torch.randn(8, F), torch.randn(M)
    assert model(sf, mf).shape == (8,)


def test_expert_outputs_differ():
    """三个 Expert 参数独立，训练前输出应互不相同（随机初始化）。"""
    model = make_model()
    sf, mf = torch.randn(N, F), torch.randn(M)
    out = model(sf, mf, return_details=True)
    e = out["expert_scores"]
    assert not torch.allclose(e[:, 0], e[:, 1], atol=1e-8)
    assert not torch.allclose(e[:, 1], e[:, 2], atol=1e-8)


def test_invalid_market_features_raises():
    model = make_model()
    with pytest.raises(ValueError):
        model(torch.randn(N, F), None, return_details=True)
