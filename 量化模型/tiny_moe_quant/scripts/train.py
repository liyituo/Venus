"""train.py：训练 / 评估 / 回测 / 专家分析 一体化实验入口（V1 + V2）。

用法示例:
    # V1（A3 行为不变）
    python scripts/train.py --name A3_tiny_moe_v1 --version v1
    # V2（A4: Base Head + Cross-sectional Gate + Top-heavy Loss）
    python scripts/train.py --name A4_tiny_moe_v2 --version v2
    # 其他消融
    python scripts/train.py --name A1_mlp --version v1 --no-moe
    python scripts/train.py --name A2_static_moe --version v1 --no-market-gate
    python scripts/train.py --name A0_momentum --baseline momentum

输出 outputs/<name>/:
    config.yaml, best_model.pt, training_log.csv, metrics.json,
    predictions.csv, expert_analysis.csv,
    equity_curve.png, drawdown.png, gate_weights.png,
    quantile_returns.csv/png, topk_sensitivity.csv, cost_sensitivity.csv,
    model_state.pt, scaler.pkl, feature_names.json, metadata.json
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import matplotlib

matplotlib.use("Agg")
import numpy as np
import pandas as pd
import torch
import yaml

from src.backtest.backtester import Backtester
from src.data.dataset import DailyCrossSectionDataset
from src.data.preprocessing import (
    apply_winsorize,
    auto_adjust_splits,
    drop_sparse_days,
    fit_scaler,
    fit_winsorize,
    sanity_check_data,
    split_by_date,
)
from src.inference.predictor import QuantPredictor
from src.metrics.quant_metrics import daily_ic, summarize_ic
from src.models.tiny_moe import build_model
from src.training.trainer import Trainer, count_parameters

# V1 / V2 配置包（--version 一键切换，粒度开关仍可单独覆盖）
V1_BUNDLE = {
    "version": "v1",
    "use_base_head": False,
    "use_cross_section_gate": False,
    "ranking_type": "normal",
    "top_weight_mode": "continuous",
    "lambda_mse": 0.2,
    "lambda_balance": 0.01,
    "use_scheduler": False,
}
V2_BUNDLE = {
    "version": "v2",
    "use_base_head": True,
    "use_cross_section_gate": True,
    "ranking_type": "top_heavy",
    "top_weight_mode": "continuous",
    "lambda_mse": 0.1,
    "lambda_balance": 0.005,
    "use_scheduler": True,
}

LABEL_DEFINITION_DEFAULT = "close_t_to_close_t+5"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Tiny-MoE Quant Ranker 训练/评估")
    p.add_argument("--config", default="configs/default.yaml")
    p.add_argument("--data-dir", default="data/processed")
    p.add_argument("--name", required=True, help="实验名 -> <out_root>/<name>/")
    p.add_argument("--out-root", default=None, help="输出根目录（默认 config.output_dir）")
    p.add_argument("--seed", type=int, default=None)
    # 版本包与消融开关
    p.add_argument("--version", choices=["v1", "v2"], default="v1")
    p.add_argument("--no-moe", action="store_true", help="use_moe=False -> MLP Ranker")
    p.add_argument("--no-market-gate", action="store_true", help="use_market_gate=False")
    p.add_argument("--no-balance-loss", action="store_true", help="use_balance_loss=False")
    p.add_argument("--no-base-head", action="store_true", help="use_base_head=False")
    p.add_argument("--no-cross-section-gate", action="store_true", help="use_cross_section_gate=False")
    p.add_argument("--baseline", choices=["momentum"], default=None,
                   help="Baseline A0: score = 过去20日收益（由价格计算），不训练模型")
    # 粒度开关（在版本包基础上再覆盖，用于 B1/B2/B3 消融）
    p.add_argument("--base-head", action="store_true", help="use_base_head=True")
    p.add_argument("--cross-section-gate", action="store_true", help="use_cross_section_gate=True")
    p.add_argument("--top-heavy", action="store_true", help="ranking_type=top_heavy")
    # 训练覆盖
    p.add_argument("--epochs", type=int, default=None)
    p.add_argument("--lr", type=float, default=None)
    p.add_argument("--device", default="auto", help="auto / cpu / cuda")
    # 时间切分覆盖（默认使用 config 中 splits，超范围自动收缩）
    p.add_argument("--train-end", default=None)
    p.add_argument("--valid-start", default=None)
    p.add_argument("--valid-end", default=None)
    p.add_argument("--test-start", default=None)
    p.add_argument("--test-end", default=None)
    return p.parse_args()


def set_seed(seed: int) -> None:
    """固定随机种子，保证实验可复现。"""
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def load_processed(data_dir: str):
    """加载统一格式数据（prepare_data / synthetic_demo / qlib_adapter 输出）。"""
    features = pd.read_csv(f"{data_dir}/features.csv", parse_dates=False)
    market = pd.read_csv(f"{data_dir}/market_features.csv", parse_dates=False)
    labels = pd.read_csv(f"{data_dir}/labels.csv", parse_dates=False)
    prices = pd.read_csv(f"{data_dir}/prices.csv", parse_dates=False)
    for df in (features, market, labels, prices):
        df["date"] = df["date"].astype(str)
    return features, market, labels, prices


def load_data_meta(data_dir: str) -> dict:
    """读取数据 meta（benchmark_type / label_definition / feature_source），不存在则用默认。"""
    meta_path = f"{data_dir}/meta.json"
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def clean_features(features: pd.DataFrame) -> pd.DataFrame:
    """剔除含 NaN / inf 特征的行（Alpha158 滚动窗口初期有 NaN）。"""
    feature_cols = [c for c in features.columns if c not in ("date", "symbol")]
    n0 = len(features)
    features = features[np.isfinite(features[feature_cols].to_numpy(dtype=np.float64)).all(axis=1)]
    n1 = len(features)
    if n1 < n0:
        print(f"特征清洗: 剔除 {n0 - n1} 行含 NaN/inf 的行（剩余 {n1}）")
    return features.reset_index(drop=True)


def main() -> None:
    args = parse_args()
    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # ---- 版本包 / 覆盖配置 ----
    # 版本包决定整套开关（--version v1 = A3 行为，--version v2 = A4 行为），
    # 粒度开关（--base-head / --no-moe 等）在版本包基础上再覆盖。
    bundle = V2_BUNDLE if args.version == "v2" else V1_BUNDLE
    if args.seed is not None:
        cfg["seed"] = args.seed
    m = cfg["model"]
    m["version"] = bundle["version"]
    m["use_base_head"] = bundle["use_base_head"]
    m["use_cross_section_gate"] = bundle["use_cross_section_gate"]
    cfg.setdefault("loss", {})
    cfg["loss"]["ranking_type"] = bundle["ranking_type"]
    cfg["loss"].setdefault("top_weight_mode", bundle["top_weight_mode"])
    t = cfg["training"]
    t["lambda_mse"] = bundle["lambda_mse"]
    t["lambda_balance"] = bundle["lambda_balance"]
    t.setdefault("max_grad_norm", 1.0)
    t["use_scheduler"] = bundle["use_scheduler"]
    # 粒度开关
    if args.base_head:
        m["use_base_head"] = True
    if args.no_base_head:
        m["use_base_head"] = False
    if args.cross_section_gate:
        m["use_cross_section_gate"] = True
    if args.no_cross_section_gate:
        m["use_cross_section_gate"] = False
    if args.top_heavy:
        cfg["loss"]["ranking_type"] = "top_heavy"
    if args.no_moe:
        m["use_moe"] = False
    if args.no_market_gate:
        m["use_market_gate"] = False
    if args.no_balance_loss:
        m["use_balance_loss"] = False
    if args.epochs is not None:
        t["epochs"] = args.epochs
    if args.lr is not None:
        t["learning_rate"] = args.lr

    horizon = int(cfg["data"]["horizon"])
    set_seed(int(cfg["seed"]))

    out_root = args.out_root or cfg["output_dir"]
    out_dir = os.path.join(out_root, args.name)
    os.makedirs(out_dir, exist_ok=True)
    with open(f"{out_dir}/config.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    device = torch.device(device)
    data_meta = load_data_meta(args.data_dir)
    benchmark_type = data_meta.get("benchmark_type", "equal_weight_pool")
    label_definition = data_meta.get("label_definition", LABEL_DEFINITION_DEFAULT)
    print(f"==> 实验: {args.name} | version={bundle['version']} | device={device} | seed={cfg['seed']}")
    print(f"    label_definition={label_definition} | benchmark_type={benchmark_type}")

    # ---- 数据 ----
    features, market, labels, prices = load_processed(args.data_dir)
    features = clean_features(features)
    feature_names = [c for c in features.columns if c not in ("date", "symbol")]
    market_feature_names = [c for c in market.columns if c not in ("date",)]
    print(f"数据: {len(features)} 行, {len(features['date'].unique())} 个交易日, "
          f"{len(features['symbol'].unique())} 只股票, F={len(feature_names)}, M={len(market_feature_names)}")

    # Data Sanity Check + 剔除稀疏日（Spec 十七）
    min_stocks = int(cfg["data"].get("min_stocks_per_day", 50))
    sanity = sanity_check_data(features, labels, market, min_stocks)
    print("== Data Sanity Check ==")
    for k, v in sanity.items():
        print(f"  {k}: {v}")
    features, labels, market, prices, dropped_days = drop_sparse_days(
        features, labels, market, prices, min_stocks
    )
    if dropped_days:
        print(f"  剔除股票数 < {min_stocks} 的交易日: {len(dropped_days)} 天 "
              f"({dropped_days[:5]}{'...' if len(dropped_days) > 5 else ''})")

    # ---- 时间切分（超范围自动收缩，保证 train < valid < test）----
    splits = cfg["splits"]
    if args.train_end:
        splits["train"]["end"] = args.train_end
    if args.valid_start:
        splits["valid"]["start"] = args.valid_start
    if args.valid_end:
        splits["valid"]["end"] = args.valid_end
    if args.test_start:
        splits["test"]["start"] = args.test_start
    if args.test_end:
        splits["test"]["end"] = args.test_end
    all_dates = sorted(features["date"].unique())
    splits = auto_adjust_splits(splits, all_dates[0], all_dates[-1])
    if splits != cfg["splits"]:
        print("切分超出数据范围，已自动收缩:")
        for k, v in splits.items():
            print(f"  [{k}] {v['start']} ~ {v['end']}")

    split_dfs = split_by_date(features, "date", splits)
    split_markets = split_by_date(market, "date", splits)
    split_labels = split_by_date(labels, "date", splits)
    split_prices = split_by_date(prices, "date", splits)
    for name, df in split_dfs.items():
        print(f"  [{name}] 交易日数: {len(df['date'].unique())}, 行数: {len(df)}")
    for name in ("train", "valid", "test"):
        if len(split_dfs[name]) == 0:
            raise SystemExit(f"切分 [{name}] 为空，请检查数据范围或 splits 配置")

    # ---- 预处理：winsorize（阈值只从 train 估计）-> scaler（只 fit train）----
    pre_cfg = cfg.get("preprocessing", {})
    train_features, train_market = split_dfs["train"], split_markets["train"]
    if pre_cfg.get("winsorize", False):
        lower_q = float(pre_cfg.get("lower_quantile", 0.01))
        upper_q = float(pre_cfg.get("upper_quantile", 0.99))
        bounds = fit_winsorize(train_features, feature_names, lower_q, upper_q)
        mkt_bounds = fit_winsorize(train_market, market_feature_names, lower_q, upper_q)
        for df in (train_features, split_dfs["valid"], split_dfs["test"]):
            apply_winsorize(df, feature_names, bounds)
        for df in (train_market, split_markets["valid"], split_markets["test"]):
            apply_winsorize(df, market_feature_names, mkt_bounds)
        print(f"Winsorize 已应用（阈值由 train 估计: [{lower_q}, {upper_q}]）")
    scaler = fit_scaler(train_features, feature_names)
    market_scaler = fit_scaler(train_market, market_feature_names)
    print("Scaler 已在训练集上 fit（valid/test 仅 transform）")

    def make_ds(name: str):
        return DailyCrossSectionDataset(
            split_dfs[name], split_labels[name], split_markets[name],
            feature_names, market_feature_names, scaler, market_scaler, horizon=horizon,
        )

    train_ds, valid_ds, test_ds = make_ds("train"), make_ds("valid"), make_ds("test")

    # ---- 模型 / Baseline ----
    train_result = None
    model_name = "MomentumBaseline"
    if args.baseline == "momentum":
        print("==> Baseline A0: Momentum (score = 过去20日收益，由价格计算)，不训练模型")
        predictor = None
        params = {"total": 0, "trainable": 0, "size_mb": 0.0, "model": model_name}
    else:
        model = build_model(cfg, num_features=len(feature_names),
                            num_market_features=len(market_feature_names))
        model_name = model.__class__.__name__
        params = count_parameters(model)
        print(f"==> 模型: {model_name} (version={m['version']})")
        print(f"    Total parameters:     {params['total']:,}")
        print(f"    Trainable parameters: {params['trainable']:,}")
        print(f"    Model size:           {params['size_mb']:.3f} MB")
        if params["total"] >= 1_000_000:
            print("    [WARNING] 参数量超过 1M，超出第一阶段目标！")
        model.to(device)

        trainer = Trainer(model, train_ds, valid_ds, cfg, device, out_dir,
                          feature_names, market_feature_names, scaler, market_scaler)
        train_result = trainer.train()
        print(f"训练完成: best_valid_RankIC={train_result['best_valid_rank_ic']:.4f} "
              f"(epoch {train_result['best_epoch']})")
        predictor = QuantPredictor(f"{out_dir}/best_model.pt", device=str(device))

    # ---- 测试集预测 ----
    test_df = split_dfs["test"]
    test_labels = split_labels["test"].set_index(["date", "symbol"])
    test_market = split_markets["test"].set_index("date")
    predictions_rows = []
    gate_rows = []

    if predictor is not None:
        for date, day in test_df.groupby("date", sort=True):
            mkt_row = test_market.loc[date]
            out = predictor.predict_daily(
                day[feature_names].to_numpy(dtype=np.float64),
                mkt_row[market_feature_names].to_numpy(dtype=np.float64),
                day["symbol"].tolist(),
            )
            gw = out["gate_weights"] or {}
            for stock in out["stocks"]:
                key = (date, stock["symbol"])
                predictions_rows.append({
                    "date": date,
                    "symbol": stock["symbol"],
                    "label": test_labels.loc[key, "label_zscore"] if key in test_labels.index else np.nan,
                    "future_return_5d": test_labels.loc[key, f"future_return_{horizon}d"]
                    if key in test_labels.index else np.nan,
                    "excess_return_5d": test_labels.loc[key, "excess_return"]
                    if key in test_labels.index else np.nan,
                    "score": stock["score"],
                    "rank": stock["rank"],
                    "expert_1_score": stock["expert_scores"][0] if stock["expert_scores"] else np.nan,
                    "expert_2_score": stock["expert_scores"][1] if stock["expert_scores"] else np.nan,
                    "expert_3_score": stock["expert_scores"][2] if stock["expert_scores"] else np.nan,
                    "gate_1": gw.get("expert_1", np.nan),
                    "gate_2": gw.get("expert_2", np.nan),
                    "gate_3": gw.get("expert_3", np.nan),
                })
            if out["gate_weights"] is not None:
                gate_rows.append({"date": date, **out["gate_weights"]})
    else:
        # A0 Momentum：score = 过去 20 日收益（由价格计算，与特征无关）
        px = prices[["date", "symbol", "close"]].sort_values(["symbol", "date"]).copy()
        px["mom20"] = px.groupby("symbol", sort=False)["close"].pct_change(20)
        mom_idx = px.set_index(["date", "symbol"])["mom20"]
        for date, day in test_df.groupby("date", sort=True):
            day_scores = mom_idx.xs(date, level="date").reindex(day["symbol"].tolist())
            day_scores = day_scores.dropna().sort_values(ascending=False)
            for pos, sym in enumerate(day_scores.index):
                key = (date, sym)
                predictions_rows.append({
                    "date": date, "symbol": sym,
                    "label": test_labels.loc[key, "label_zscore"] if key in test_labels.index else np.nan,
                    "future_return_5d": test_labels.loc[key, f"future_return_{horizon}d"]
                    if key in test_labels.index else np.nan,
                    "excess_return_5d": test_labels.loc[key, "excess_return"]
                    if key in test_labels.index else np.nan,
                    "score": float(day_scores[sym]),
                    "rank": int(pos + 1),
                    "expert_1_score": np.nan, "expert_2_score": np.nan, "expert_3_score": np.nan,
                    "gate_1": np.nan, "gate_2": np.nan, "gate_3": np.nan,
                })

    pred_df = pd.DataFrame(predictions_rows)
    pred_df.to_csv(f"{out_dir}/predictions.csv", index=False)
    print(f"预测已保存: {out_dir}/predictions.csv ({len(pred_df)} 行)")

    # ---- IC / RankIC（逐日）+ Top 5%/10%/Top-20 超额收益 ----
    daily = daily_ic(
        pred_df["score"].to_numpy(),
        pred_df["future_return_5d"].to_numpy(),
        pred_df["date"].to_numpy(),
    )
    ic_summary = summarize_ic(daily)
    top_excess = compute_top_excess(pred_df)
    print(f"测试集 IC: mean={ic_summary['mean_ic']:.4f} std={ic_summary['std_ic']:.4f} "
          f"ICIR={ic_summary['icir']:.4f}")
    print(f"测试集 RankIC: mean={ic_summary['mean_rank_ic']:.4f} std={ic_summary['std_rank_ic']:.4f} "
          f"RankICIR={ic_summary['rank_icir']:.4f}")
    print(f"Top-5% 平均超额收益: {top_excess['top_5pct_excess']:.4f} | "
          f"Top-10%: {top_excess['top_10pct_excess']:.4f} | "
          f"Top-20: {top_excess['top_20_excess']:.4f}")

    # ---- 回测（0/5/10/20 bps）----
    bt_config = cfg["backtest"]
    backtest_metrics = {}
    cost_rows = []
    for bps in (0, 5, 10, 20):
        bt = Backtester(top_k=int(bt_config["top_k"]),
                        rebalance_days=int(bt_config["rebalance_days"]),
                        transaction_cost_bps=float(bps))
        res = bt.run(pred_df, split_prices["test"])
        if bps == int(bt_config["transaction_cost_bps"]):
            bt.plot(res, out_dir)  # 默认成本档位画图
        backtest_metrics[str(bps)] = res.metrics
        cost_rows.append({"cost_bps": bps, **res.metrics})
        m_ = res.metrics
        print(f"  回测 cost={bps}bps: 累计={m_['cum_return']:.4f} 年化={m_['annual_return']:.4f} "
              f"Sharpe={m_['sharpe']:.3f} MaxDD={m_['max_drawdown']:.4f} Turnover={m_['turnover']:.3f}")
    pd.DataFrame(cost_rows).to_csv(f"{out_dir}/cost_sensitivity.csv", index=False)

    # ---- Top-K Sensitivity（同一份预测，不重训）----
    topk_rows = []
    for k in (5, 10, 20, 30, 50):
        bt = Backtester(top_k=k, rebalance_days=int(bt_config["rebalance_days"]),
                        transaction_cost_bps=float(bt_config["transaction_cost_bps"]))
        res = bt.run(pred_df, split_prices["test"])
        topk_rows.append({"K": k, **res.metrics})
        print(f"  Top-{k}: 累计={res.metrics['cum_return']:.4f} 年化={res.metrics['annual_return']:.4f} "
              f"Sharpe={res.metrics['sharpe']:.3f} MaxDD={res.metrics['max_drawdown']:.4f} "
              f"Turnover={res.metrics['turnover']:.3f}")
    pd.DataFrame(topk_rows).to_csv(f"{out_dir}/topk_sensitivity.csv", index=False)

    # ---- Quantile Analysis（Q1..Q10 未来收益，检查是否真正把高收益股票推向顶部）----
    quantile_returns = run_quantile_analysis(pred_df, out_dir)

    # ---- Expert 分析 ----
    expert_analysis = build_expert_analysis(gate_rows, test_market, daily, pred_df)
    expert_analysis.to_csv(f"{out_dir}/expert_analysis.csv", index=False)
    if gate_rows:
        plot_gate_weights(gate_rows, out_dir)

    # ---- 模型保存（Spec 二十八）----
    save_model_artifacts(out_dir, cfg, feature_names, market_feature_names, scaler,
                         market_scaler, params, model_name, train_result, splits,
                         label_definition, benchmark_type, device)

    # ---- metrics.json ----
    metrics = {
        "experiment": args.name,
        "version": bundle["version"],
        "model": model_name,
        "params": params,
        "ic": ic_summary,
        "top_excess_return": top_excess,
        "backtest": backtest_metrics,
        "best_valid_rank_ic": train_result["best_valid_rank_ic"] if train_result else None,
        "best_epoch": train_result["best_epoch"] if train_result else None,
        "label_definition": label_definition,
        "benchmark_type": benchmark_type,
        "splits": splits,
        "quantile_returns": quantile_returns,
    }
    with open(f"{out_dir}/metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2, default=str)
    print(f"==> 实验完成: {out_dir}")


# ---------------------------------------------------------------------- #
# 工具函数
# ---------------------------------------------------------------------- #
def compute_top_excess(pred_df: pd.DataFrame) -> dict:
    """Top 5% / Top 10% / Top-20 平均未来 5 日超额收益（每日取分最高组，再按日平均）。"""
    df = pred_df.dropna(subset=["excess_return_5d"])
    out = {"top_5pct_excess": float("nan"), "top_10pct_excess": float("nan"),
           "top_20_excess": float("nan")}
    if df.empty:
        return out
    for key, frac in (("top_5pct_excess", 0.05), ("top_10pct_excess", 0.10)):
        vals = []
        for _, g in df.groupby("date"):
            k = max(1, int(round(len(g) * frac)))
            vals.append(g.nlargest(k, "score")["excess_return_5d"].mean())
        out[key] = float(np.mean(vals))
    vals20 = []
    for _, g in df.groupby("date"):
        if len(g) >= 20:
            vals20.append(g.nlargest(20, "score")["excess_return_5d"].mean())
    out["top_20_excess"] = float(np.mean(vals20)) if vals20 else float("nan")
    return out


def run_quantile_analysis(pred_df: pd.DataFrame, out_dir: str) -> dict:
    """每日按 score 分 10 组（Q1 最低 .. Q10 最高），统计各组未来收益，保存 csv/png。"""
    import matplotlib.pyplot as plt

    df = pred_df.dropna(subset=["future_return_5d"]).copy()
    rows = []
    for date, g in df.groupby("date"):
        g = g.sort_values("score", ascending=False).reset_index(drop=True)
        g["q"] = pd.qcut(g["score"].rank(method="first"), 10, labels=False) + 1  # 1..10
        for q, sub in g.groupby("q"):
            rows.append({"date": date, "quantile": int(q),
                         "future_return": sub["future_return_5d"].mean()})
    qdf = pd.DataFrame(rows)
    summary = qdf.groupby("quantile")["future_return"].agg(["mean", "std", "count"]).reset_index()
    summary.to_csv(f"{out_dir}/quantile_returns.csv", index=False)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar(summary["quantile"], summary["mean"], color="steelblue")
    ax.set_xlabel("Score Quantile (Q1=lowest .. Q10=highest)")
    ax.set_ylabel("Mean Future 5d Return")
    ax.set_title("Quantile Analysis: 未来收益 vs 模型分数分组")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(f"{out_dir}/quantile_returns.png", dpi=120)
    plt.close(fig)
    return {f"Q{q}": float(r) for q, r in zip(summary["quantile"], summary["mean"])}


def save_model_artifacts(out_dir, cfg, feature_names, market_feature_names, scaler,
                         market_scaler, params, model_name, train_result, splits,
                         label_definition, benchmark_type, device) -> None:
    """按 Spec 二十八 保存模型产物。"""
    import torch as _torch

    if train_result is not None:
        ckpt = _torch.load(f"{out_dir}/best_model.pt", map_location="cpu", weights_only=False)
        _torch.save(ckpt["model_state"], f"{out_dir}/model_state.pt")
    with open(f"{out_dir}/scaler.pkl", "wb") as f:
        pickle.dump({"scaler": scaler, "market_scaler": market_scaler}, f)
    with open(f"{out_dir}/feature_names.json", "w", encoding="utf-8") as f:
        json.dump({"feature_names": feature_names,
                   "market_feature_names": market_feature_names}, f, ensure_ascii=False, indent=2)
    metadata = {
        "model_version": cfg["model"].get("version", "v1"),
        "model_name": model_name,
        "train_start": splits["train"]["start"],
        "train_end": splits["train"]["end"],
        "valid_start": splits["valid"]["start"],
        "valid_end": splits["valid"]["end"],
        "test_start": splits["test"]["start"],
        "test_end": splits["test"]["end"],
        "feature_count": len(feature_names),
        "label_definition": label_definition,
        "benchmark_type": benchmark_type,
        "best_epoch": train_result["best_epoch"] if train_result else None,
        "best_valid_rankic": train_result["best_valid_rank_ic"] if train_result else None,
        "params": params,
        "torch_version": _torch.__version__,
        "cuda_available": _torch.cuda.is_available(),
        "device_name": str(device),
    }
    with open(f"{out_dir}/metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)


def build_expert_analysis(gate_rows, test_market, daily, pred_df) -> pd.DataFrame:
    """每个交易日一行: gate 权重 + 市场状态 + 各 Expert 与最终模型的 RankIC。"""
    if not gate_rows:
        gate = pd.DataFrame(columns=["date", "expert_1", "expert_2", "expert_3"])
    else:
        gate = pd.DataFrame(gate_rows).rename(
            columns={"expert_1": "gate_1", "expert_2": "gate_2", "expert_3": "gate_3"}
        )
    market_cols = ["market_return_20d", "market_volatility_20d", "advance_ratio"]
    mkt = test_market.reset_index()[["date"] + [c for c in market_cols if c in test_market.columns]]
    out = gate.merge(mkt, on="date", how="outer").sort_values("date")

    # 每个 Expert 的逐日 RankIC 与最终模型 RankIC
    pred = pred_df.copy()
    daily_rank = daily.set_index("date")["rank_ic"].rename("rank_ic_final")
    out = out.merge(daily_rank, on="date", how="left")
    for i in range(1, 4):
        col = f"expert_{i}_score"
        if col in pred.columns and pred[col].notna().any():
            d = daily_ic(
                pred[col].to_numpy(), pred["future_return_5d"].to_numpy(), pred["date"].to_numpy()
            )
            out = out.merge(d.set_index("date")["rank_ic"].rename(f"rank_ic_expert_{i}"),
                            on="date", how="left")
    return out


def plot_gate_weights(gate_rows, out_dir: str) -> None:
    """Gate 权重随时间的走势图。"""
    import matplotlib.pyplot as plt

    gate = pd.DataFrame(gate_rows).sort_values("date")
    fig, ax = plt.subplots(1, 2, figsize=(14, 4))
    for i in range(1, 4):
        ax[0].plot(gate["date"], gate[f"expert_{i}"], label=f"gate_{i}", linewidth=0.8)
    ax[0].set_title("Gate Weights over Time (test)")
    ax[0].legend()
    ax[0].tick_params(axis="x", rotation=45)
    ax[0].grid(alpha=0.3)
    eps = 1e-8
    g = gate[[f"expert_{i}" for i in range(1, 4)]].to_numpy()
    entropy = -(g * np.log(g + eps)).sum(axis=1)
    ax[1].plot(gate["date"], entropy, color="green", linewidth=0.8)
    ax[1].set_title("Gate Entropy (max=log3)")
    ax[1].tick_params(axis="x", rotation=45)
    ax[1].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(f"{out_dir}/gate_weights.png", dpi=120)
    plt.close(fig)


if __name__ == "__main__":
    main()
