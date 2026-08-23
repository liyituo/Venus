# Tiny-MoE Quant Ranker

轻量级量化股票排序模型。第一版（V1）验证纯模型全链路；V2 增加 Base Head + Cross-sectional Gate + Top-heavy Ranking Loss，目标是在真实股票数据上提高 Top-K 选股能力与 Sharpe。

**目标**：每个交易日对股票横截面输出 Alpha Score 并排序，使排名靠前的股票在未来 5 个交易日具有更高的超额收益。

## 核心设计

### V1（A3，保持不变）

```
Stock Features → FactorEncoder → h_i (64)
                                      │
              ┌───────────────────────┼──────────────┐
              ▼                       ▼              ▼
          Expert 1                 Expert 2      Expert 3
              │                       │              │
              └───────────────────────┼──────────────┘
                                      ▼
                        Weighted Fusion (逐日共享一个市场级 Gate)
                                      ▼
                                    Alpha Score
```

### V2（A4，全部由 config 开关控制）

```
                         ┌──── Base Head ───────────────┐
                         │                              │
Stock Features → Factor Encoder → h_i                  │
                         │                              │
                         ├→ Expert 1 ─┐                 │
                         ├→ Expert 2 ─┼→ MoE Residual ─┼→ Final Score
                         └→ Expert 3 ─┘                 │
                                      ▲                │
Market Features ───────┐              │                │
                       ├→ Dynamic Gate ┘                │
Cross-sectional State ─┘          (h 的 mean/std)       │
                                                       │
Final Score = Base Score + α * MoE Residual ───────────┘
```

- **Base Head**（64→32→GELU→Dropout→1）：学习稳定的全局 Alpha；
- **Cross-sectional Gate**（144→64→GELU→Dropout→5→3→Softmax）：Gate 输入 = market_embedding(16) + 当日横截面 h 的 mean/std(128)，只用 t 日可见数据；
- **moe_scale**：可学习标量，初始 0.1（`score = base + moe_scale * moe`），避免训练初期 residual 破坏 Base Head；
- **Top-heavy Pairwise Loss**：pair 权重基于更优股票的当日 label percentile（discrete 3/1.5/1 或 continuous `1 + 2*p^3`），更关注真实收益排名靠前的股票。

### 关键约束（两版通用）

- 训练单位：一个交易日的完整横截面（batch_size = 1 date），Ranking Loss 只在同一天内部构造 pair
- 标签：`excess_return_5d = future_return_5d - benchmark`，再逐日横截面 z-score（只用当天数据）
- benchmark 优先 CSI300 指数（`benchmark_type: csi300_index`），不可用时回退股票池等权
- Scaler / Winsorize 阈值只 fit(train)；时间切分严格 train < valid < test，超出数据范围自动收缩
- 逐日计算 IC/RankIC，不做跨日期混合；`min_stocks_per_day` 以下的交易日跳过
- 参数量：V1 ≈ 1.9 万，V2 ≈ 3.0 万（目标 < 1M）
- 训练稳定性：gradient clipping（max_grad_norm=1.0）、可选 ReduceLROnPlateau、Gate/Expert collapse 自动报警

## 项目结构

```
tiny_moe_quant/
├── configs/
│   ├── default.yaml           # V1 默认（A3 行为）
│   └── real_csi300.yaml       # 真实数据 V2 配置（A4）
├── data/
│   ├── raw/                   # 原始 OHLCV CSV
│   ├── processed/             # 统一格式数据
│   ├── csi300_raw/            # akshare 抓取的 CSI300 OHLCV + 指数基准
│   └── csi300/                # 真实数据统一格式
├── src/
│   ├── data/
│   │   ├── feature_builder.py     # OHLCV → 14 个股票因子 + 7 个市场特征
│   │   ├── preprocessing.py       # 标签/时间切分/Scaler/Winsorize/Sanity Check/自动缩切分
│   │   ├── dataset.py             # 每日横截面 Dataset
│   │   └── qlib_adapter.py        # Qlib/本地 CSV → 统一 DataFrame（模型不感知数据来源）
│   ├── models/
│   │   ├── factor_encoder.py      # F → 128 → LN → GELU → 64 → h
│   │   ├── market_encoder.py      # M → 32 → GELU → 16 → z
│   │   ├── experts.py             # 3 个独立 Expert: 64 → 32 → 1
│   │   ├── base_head.py           # V2: 稳定全局 Alpha 头
│   │   ├── tiny_moe.py            # TinyMoE 主模型（V1+V2 开关）+ build_model
│   │   └── baselines.py           # MomentumBaseline
│   ├── losses/ranking_loss.py     # 同日 pair 采样 / normal + top_heavy loss / balance / hybrid
│   ├── metrics/quant_metrics.py   # 逐日 IC / RankIC / ICIR
│   ├── training/trainer.py        # 逐日训练 / grad clip / scheduler / V2 debug / collapse 报警
│   ├── backtest/backtester.py     # Top-K 等权、每 5 日调仓、交易成本、绘图
│   └── inference/predictor.py     # QuantPredictor（后续给 Agent 用）
├── scripts/
│   ├── prepare_data.py            # 原始 CSV → 处理后数据
│   ├── fetch_csi300_akshare.py    # akshare 抓取 CSI300 日线（真实数据）
│   ├── prepare_real_data.py       # QlibAdapter 构建真实数据
│   ├── train.py                   # 训练 + 评估 + 回测 + 灵敏度分析（实验入口，--version v1/v2）
│   ├── run_experiments.py         # A0-A4 统一入口 + experiment_summary.csv
│   ├── evaluate.py / backtest.py / analyze_experts.py
│   ├── synthetic_demo.py / run_all_experiments.py   # 合成数据链路
├── tests/                         # 65 个 pytest（shape/loss/防泄漏/回测/V2/横截面 Gate/Base+Residual）
└── outputs* / outputs_real/       # 实验输出目录
```

## 快速开始

```bash
pip install -r requirements.txt

# 1) 合成演示数据 + 全部实验
python scripts/synthetic_demo.py --out data/synthetic
python scripts/run_all_experiments.py --data-dir data/synthetic

# 2) 真实数据（CSI300，akshare 抓取 → adapter 构建 → A0-A4）
python scripts/fetch_csi300_akshare.py --out data/csi300_raw
python scripts/prepare_real_data.py --source csv \
    --csv-path data/csi300_raw/ohlcv.csv --benchmark-csv data/csi300_raw/benchmark.csv
python scripts/run_experiments.py --config configs/real_csi300.yaml
```

单实验与消融：

```bash
python scripts/train.py --config configs/real_csi300.yaml --name A3_tiny_moe_v1 --version v1
python scripts/train.py --config configs/real_csi300.yaml --name A4_tiny_moe_v2 --version v2
python scripts/train.py --name B1_v1_base --version v1 --base-head          # V1 + Base Head
python scripts/train.py --name B2_v1_csgate --version v1 --cross-section-gate  # V1 + CS Gate
python scripts/train.py --name B3_v1_topheavy --version v1 --top-heavy     # V1 + Top-heavy Loss
```

## 实验输出（outputs_real/<name>/）

| 文件 | 内容 |
| --- | --- |
| config.yaml | 本次实验生效配置 |
| best_model.pt / model_state.pt | 模型权重（标准 PyTorch，可跨设备部署） |
| scaler.pkl / feature_names.json / metadata.json | 推理所需 + 训练元信息 |
| training_log.csv | 每 epoch loss/IC/RankIC/gate 统计 + V2 debug（base/moe score、moe_scale、expert std） |
| metrics.json | 测试集 IC/RankIC/ICIR + Top 5%/10%/Top-20 超额 + 0/5/10/20bps 回测 |
| predictions.csv / expert_analysis.csv | 逐股预测 / 逐日 gate + 市场状态 |
| equity_curve.png / drawdown.png / gate_weights.png / quantile_returns.png | 可视化 |
| topk_sensitivity.csv / cost_sensitivity.csv / quantile_returns.csv | 灵敏度与分位数分析 |

## 推理 API

```python
from src.inference.predictor import QuantPredictor

predictor = QuantPredictor("outputs_real/A4_tiny_moe_v2/best_model.pt")
result = predictor.predict_daily(stock_features, market_features, symbols)
# -> {"gate_weights": {"expert_1": ..., "expert_2": ..., "expert_3": ...},
#     "stocks": [{"symbol": ..., "score": ..., "rank": ..., "expert_scores": [...]}, ...]}
```

## 测试与验收

```bash
pytest tests/ -v
```

覆盖：模型 shape/前向/反向/参数量 <1M、V2 forward shapes、`moe_scale=0 → final==base`、
Cross-sectional Gate（A3 开关关闭时行为不变）、Top-heavy loss（权重边界/单调性/可复现）、
**未来信息泄漏**（改未来数据特征不变、label 变；时间切分；Scaler/Winsorize 只 fit train）、
回测收益/成本/调仓逻辑、Dataset→Trainer→checkpoint 端到端冒烟。

## 已知限制（第一阶段）

- 真实数据目前来自 akshare（当前 CSI300 成分，存在幸存者偏差；前复权价格），
  特征为 FeatureBuilder 生成的 14 个基础因子（非 Alpha158）；QlibAdapter 的 Alpha158 路径
  已实现，等待 qlib cn_data 完整下载后可直接切换（data.source: qlib）
- 不做涨跌分类，只做排序；Gate 为每日市场级（非逐股）
- 无 FastAPI（仅 Python API）


- 未接入真实数据源（接口为通用 CSV/parquet）
- 不做涨跌分类，只做排序
- Gate 为每日市场级（非逐股），三个 Expert 自由学习，不做人为分工
- 无 FastAPI（仅 Python API）
