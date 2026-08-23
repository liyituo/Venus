"""端到端集成冒烟测试：Dataset -> Trainer -> checkpoint 全链路（CPU，秒级）。"""
import os

import numpy as np
import pandas as pd
import torch

from src.data.dataset import DailyCrossSectionDataset
from src.data.preprocessing import fit_scaler, split_by_date
from src.models.tiny_moe import build_model
from src.training.trainer import Trainer

N_STOCKS, N_DAYS = 30, 60


def make_small_data(tmp_path) -> str:
    """构建小型 processed 格式数据（特征/市场/标签/价格），返回数据目录。"""
    rng = np.random.default_rng(0)
    dates = pd.bdate_range("2015-01-05", periods=N_DAYS).strftime("%Y-%m-%d").tolist()
    symbols = [f"S{i}" for i in range(N_STOCKS)]

    # 特征：f1 与未来收益相关，f2 噪声
    f1 = rng.normal(0, 1, (N_DAYS, N_STOCKS))
    f2 = rng.normal(0, 1, (N_DAYS, N_STOCKS))
    rows = [
        [d, s, float(f1[t, i]), float(f2[t, i])]
        for t, d in enumerate(dates) for i, s in enumerate(symbols)
    ]
    features = pd.DataFrame(rows, columns=["date", "symbol", "f1", "f2"])

    # 价格：未来 5 日收益与 f1 正相关（同一日期的 f1 用于未来收益，保证可学习）
    daily_ret = 0.02 * f1 + rng.normal(0, 0.01, (N_DAYS, N_STOCKS))
    close = 100.0 * np.exp(np.cumsum(daily_ret, axis=0))
    prices = pd.DataFrame(
        {
            "date": np.repeat(dates, N_STOCKS),
            "symbol": np.tile(symbols, N_DAYS),
            "close": close.reshape(-1),
        }
    )
    market = pd.DataFrame(
        {
            "date": dates,
            "m1": rng.normal(0, 1, N_DAYS),
            "m2": rng.normal(0, 1, N_DAYS),
        }
    )

    from src.data.preprocessing import build_labels

    labels = build_labels(features, prices, horizon=5)

    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    features.to_csv(f"{data_dir}/features.csv", index=False)
    market.to_csv(f"{data_dir}/market_features.csv", index=False)
    labels.to_csv(f"{data_dir}/labels.csv", index=False)
    prices.to_csv(f"{data_dir}/prices.csv", index=False)
    return data_dir


def test_trainer_end_to_end(tmp_path):
    """2 个 epoch 全链路：切分 -> scaler -> dataset -> 训练 -> checkpoint -> 日志。"""
    torch.manual_seed(0)
    data_dir = make_small_data(tmp_path)
    features = pd.read_csv(data_dir + "/features.csv", parse_dates=False)
    market = pd.read_csv(data_dir + "/market_features.csv", parse_dates=False)
    labels = pd.read_csv(data_dir + "/labels.csv", parse_dates=False)
    prices = pd.read_csv(data_dir + "/prices.csv", parse_dates=False)

    dates = sorted(features["date"].unique())
    splits = {
        "train": {"start": dates[0], "end": dates[int(N_DAYS * 0.6) - 1]},
        "valid": {"start": dates[int(N_DAYS * 0.6)], "end": dates[int(N_DAYS * 0.75) - 1]},
        "test": {"start": dates[int(N_DAYS * 0.75)], "end": dates[-1]},
    }
    split_f = split_by_date(features, "date", splits)
    split_m = split_by_date(market, "date", splits)
    split_l = split_by_date(labels, "date", splits)

    feature_names = ["f1", "f2"]
    market_names = ["m1", "m2"]
    scaler = fit_scaler(split_f["train"], feature_names)
    mkt_scaler = fit_scaler(split_m["train"], market_names)

    train_ds = DailyCrossSectionDataset(
        split_f["train"], split_l["train"], split_m["train"],
        feature_names, market_names, scaler, mkt_scaler, horizon=5,
    )
    valid_ds = DailyCrossSectionDataset(
        split_f["valid"], split_l["valid"], split_m["valid"],
        feature_names, market_names, scaler, mkt_scaler, horizon=5,
    )

    cfg = {
        "seed": 0,
        "model": {
            "factor_hidden_dim": 16, "embedding_dim": 8, "market_hidden_dim": 8,
            "market_embedding_dim": 4, "expert_hidden_dim": 8, "num_experts": 3,
            "dropout": 0.0, "use_moe": True, "use_market_gate": True,
            "use_balance_loss": True,
        },
        "training": {
            "epochs": 2, "learning_rate": 0.01, "weight_decay": 0.0,
            "lambda_rank": 1.0, "lambda_mse": 0.2, "lambda_balance": 0.01,
            "pair_margin": 0.1, "max_pairs_per_day": 512,
            "early_stopping_patience": 10,
        },
    }
    model = build_model(cfg, num_features=2, num_market_features=2)
    out_dir = str(tmp_path / "out")
    os.makedirs(out_dir, exist_ok=True)

    trainer = Trainer(model, train_ds, valid_ds, cfg, torch.device("cpu"), out_dir,
                      feature_names, market_names, scaler, mkt_scaler)
    result = trainer.train()

    assert "best_valid_rank_ic" in result
    assert os.path.exists(f"{out_dir}/best_model.pt")
    assert os.path.exists(f"{out_dir}/training_log.csv")
    log = pd.read_csv(f"{out_dir}/training_log.csv")
    assert len(log) >= 1
    # gate 统计列存在且值域合法
    assert {"gate_1_mean", "gate_2_mean", "gate_3_mean",
            "gate_1_std", "gate_2_std", "gate_3_std", "gate_entropy"} <= set(log.columns)
    g = log["gate_1_mean"].dropna()
    assert ((g >= 0) & (g <= 1)).all()
    # checkpoint 可加载且含推理所需字段
    ckpt = torch.load(f"{out_dir}/best_model.pt", map_location="cpu", weights_only=False)
    for key in ("model_state", "config", "feature_names", "market_feature_names",
                "scaler", "market_scaler", "architecture"):
        assert key in ckpt


def test_trainer_mlp_variant(tmp_path):
    """MLP 变体（无 MoE）也能走通训练链路。"""
    torch.manual_seed(0)
    data_dir = make_small_data(tmp_path)
    features = pd.read_csv(data_dir + "/features.csv", parse_dates=False)
    market = pd.read_csv(data_dir + "/market_features.csv", parse_dates=False)
    labels = pd.read_csv(data_dir + "/labels.csv", parse_dates=False)

    dates = sorted(features["date"].unique())
    splits = {
        "train": {"start": dates[0], "end": dates[35]},
        "valid": {"start": dates[36], "end": dates[45]},
        "test": {"start": dates[46], "end": dates[-1]},
    }
    split_f = split_by_date(features, "date", splits)
    split_m = split_by_date(market, "date", splits)
    split_l = split_by_date(labels, "date", splits)
    scaler = fit_scaler(split_f["train"], ["f1", "f2"])
    mkt_scaler = fit_scaler(split_m["train"], ["m1", "m2"])

    cfg = {
        "seed": 0,
        "model": {
            "factor_hidden_dim": 16, "embedding_dim": 8, "market_hidden_dim": 8,
            "market_embedding_dim": 4, "expert_hidden_dim": 8, "num_experts": 3,
            "dropout": 0.0, "use_moe": False, "use_market_gate": True,
            "use_balance_loss": False,
        },
        "training": {
            "epochs": 1, "learning_rate": 0.01, "weight_decay": 0.0,
            "lambda_rank": 1.0, "lambda_mse": 0.2, "lambda_balance": 0.01,
            "pair_margin": 0.1, "max_pairs_per_day": 512,
            "early_stopping_patience": 10,
        },
    }
    train_ds = DailyCrossSectionDataset(
        split_f["train"], split_l["train"], split_m["train"],
        ["f1", "f2"], ["m1", "m2"], scaler, mkt_scaler, horizon=5,
    )
    valid_ds = DailyCrossSectionDataset(
        split_f["valid"], split_l["valid"], split_m["valid"],
        ["f1", "f2"], ["m1", "m2"], scaler, mkt_scaler, horizon=5,
    )
    model = build_model(cfg, num_features=2, num_market_features=2)
    out_dir = str(tmp_path / "out_mlp")
    os.makedirs(out_dir, exist_ok=True)
    trainer = Trainer(model, train_ds, valid_ds, cfg, torch.device("cpu"), out_dir,
                      ["f1", "f2"], ["m1", "m2"], scaler, mkt_scaler)
    result = trainer.train()
    assert "best_valid_rank_ic" in result
    assert os.path.exists(f"{out_dir}/best_model.pt")
