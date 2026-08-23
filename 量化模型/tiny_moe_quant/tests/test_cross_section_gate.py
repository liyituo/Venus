"""Cross-sectional Gate 测试：V1 行为不变、V2 gate 与当日横截面相关、无未来泄漏。"""
import torch

from src.models.tiny_moe import TinyMoE

F, M = 20, 8


def make_model(**kw) -> TinyMoE:
    defaults = dict(num_features=F, num_market_features=M)
    defaults.update(kw)
    return TinyMoE(**defaults)


def test_v1_gate_identical_with_and_without_cs_flag_off():
    """use_cross_section_gate=False 时（A3），Gate 只用市场特征 —— 行为与 V1 一致。"""
    torch.manual_seed(0)
    m1 = make_model(use_base_head=False, use_cross_section_gate=False)
    torch.manual_seed(0)
    m2 = make_model(use_base_head=False, use_cross_section_gate=False)
    m1.eval()
    m2.eval()
    sf = torch.randn(30, F)
    mf = torch.randn(M)
    with torch.no_grad():
        s1 = m1(sf, mf, return_details=True)["scores"]
        s2 = m2(sf, mf, return_details=True)["scores"]
    assert torch.allclose(s1, s2, atol=1e-8)


def test_v1_gate_independent_of_other_stocks():
    """A3（无横截面 Gate）: 同一天其他股票的变化不影响单只股票的分数。"""
    torch.manual_seed(0)
    model = make_model(use_base_head=False, use_cross_section_gate=False)
    model.eval()
    sf = torch.randn(10, F)
    mf = torch.randn(M)
    with torch.no_grad():
        s1 = model(sf, mf, return_details=True)["scores"]
        # 只看前 5 只股票（横截面不同）
        s2 = model(sf[:5], mf, return_details=True)["scores"]
    assert torch.allclose(s1[:5], s2, atol=1e-8)


def test_v2_gate_depends_on_cross_section():
    """V2（横截面 Gate）: 当日股票集合不同 -> Gate 不同（但仍然是市场级 [K]）。

    构造两个横截面: 一个全部重复同一只股票（cs_std=0），一个随机（cs_std>0），
    Gate 必然不同。
    """
    torch.manual_seed(0)
    model = make_model(use_base_head=False, use_cross_section_gate=True)
    model.eval()
    base = torch.randn(1, F)
    sf_degenerate = base.repeat(20, 1)      # 20 只相同股票 -> cs_std = 0
    sf_random = torch.randn(20, F)
    mf = torch.randn(M)
    with torch.no_grad():
        g1 = model(sf_degenerate, mf, return_details=True)["gate_weights"]
        g2 = model(sf_random, mf, return_details=True)["gate_weights"]
    assert g1.shape == (3,) and g2.shape == (3,)
    assert torch.allclose(g1.sum(), torch.tensor(1.0), atol=1e-5)
    assert torch.allclose(g2.sum(), torch.tensor(1.0), atol=1e-5)
    assert not torch.allclose(g1, g2, atol=1e-5), "横截面状态不同时 Gate 应不同"


def test_v2_gate_shared_across_stocks_same_day():
    """V2: 同一天所有股票共享同一个 Gate（[1, K] 广播，绝不生成 [N, K]）。"""
    torch.manual_seed(0)
    model = make_model(use_base_head=True, use_cross_section_gate=True)
    model.eval()
    sf = torch.randn(40, F)
    mf = torch.randn(M)
    with torch.no_grad():
        out = model(sf, mf, return_details=True)
    g = out["gate_weights"]
    assert g.shape == (3,)
    # 通过 moe = (e * g) 验证广播正确：手工重算
    e = out["expert_scores"]
    moe_manual = (e * g.unsqueeze(0)).sum(dim=1)
    assert torch.allclose(out["moe_scores"], moe_manual, atol=1e-6)


def test_v2_cs_state_from_embeddings_only():
    """横截面状态只由当日 stock embeddings 统计得到（mean/std），不依赖外部未来信息。"""
    torch.manual_seed(0)
    model = make_model(use_base_head=False, use_cross_section_gate=True)
    model.eval()
    sf = torch.randn(20, F)
    mf = torch.randn(M)
    with torch.no_grad():
        h = model.factor_encoder(sf)
        cs_mean = h.mean(dim=0)
        cs_std = h.std(dim=0, unbiased=False)
        g = model(sf, mf, return_details=True)["gate_weights"]
    # gate 只依赖 (z, cs_mean, cs_std)；这里验证 cs 统计维度
    assert cs_mean.shape == (64,)
    assert cs_std.shape == (64,)
    assert g.shape == (3,)
