"""训练器：逐日训练、Gate 统计、逐日 IC/RankIC、Early Stopping、Checkpoint。

训练单位：一个交易日 = 一个 batch（DataLoader batch_size=1），
按日期顺序遍历，不做任何随机打乱。

V2 新增（均由 config 控制）:
    - gradient clipping（max_grad_norm，默认 1.0）
    - ReduceLROnPlateau（监控 validation RankIC，可选）
    - V2 debug 统计（base/moe score 的 mean/std、moe_scale、各 expert score std）
    - collapse 自动报警（gate entropy / expert std / moe_scale 异常时打印 WARNING，不停止训练）
"""
from __future__ import annotations

import copy
import json
import time
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import torch
from sklearn.preprocessing import StandardScaler
from torch import nn
from torch.utils.data import DataLoader

from ..losses.ranking_loss import hybrid_loss
from ..metrics.quant_metrics import daily_ic, summarize_ic

GATE_COLLAPSE_ENTROPY = 0.1      # gate entropy 连续低于该值的 epoch 数超过阈值则报警
EXPERT_COLLAPSE_STD = 1e-5       # expert score std 低于该值视为塌缩
MOE_SCALE_ALARM = 10.0           # |moe_scale| 超过该值报警


def count_parameters(model: nn.Module) -> Dict[str, float]:
    """统计模型参数量（目标 < 1,000,000）。"""
    total = sum(p.numel() for p in model.parameters())
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    return {"total": int(total), "trainable": int(trainable), "size_mb": float(total * 4.0 / 1e6)}


class Trainer:
    """Tiny-MoE 训练器。

    - 每 epoch 遍历训练集全部交易日（按时间顺序），一天一个梯度步
    - 记录逐日 Gate 权重: mean/std of gate_1/2/3 与 gate entropy
    - V2: base/moe score 统计、moe_scale、各 expert score std
    - 以验证集 mean RankIC 为 early stopping 指标
    - 保存 best_model.pt（模型 + config + feature names + scaler）
    """

    def __init__(
        self,
        model: nn.Module,
        train_dataset,
        valid_dataset,
        config: Dict[str, Any],
        device: torch.device,
        output_dir: str,
        feature_names: List[str],
        market_feature_names: List[str],
        scaler: StandardScaler,
        market_scaler: StandardScaler,
    ) -> None:
        self.model = model
        self.train_ds = train_dataset
        self.valid_ds = valid_dataset
        self.cfg = config
        self.device = device
        self.output_dir = output_dir
        self.feature_names = feature_names
        self.market_feature_names = market_feature_names
        self.scaler = scaler
        self.market_scaler = market_scaler

        tcfg = config["training"]
        loss_cfg = config.get("loss", {})
        self.epochs = int(tcfg["epochs"])
        self.lambda_rank = float(tcfg["lambda_rank"])
        self.lambda_mse = float(tcfg["lambda_mse"])
        self.lambda_balance = float(tcfg["lambda_balance"])
        self.pair_margin = float(tcfg["pair_margin"])
        self.max_pairs = int(tcfg["max_pairs_per_day"])
        self.patience = int(tcfg["early_stopping_patience"])
        self.use_balance_loss = bool(config["model"]["use_balance_loss"])
        self.num_experts = int(config["model"]["num_experts"])
        # V2 loss 配置
        self.ranking_type = str(loss_cfg.get("ranking_type", "normal"))
        self.top_weight_mode = str(loss_cfg.get("top_weight_mode", "continuous"))
        self.top_weight_strength = float(loss_cfg.get("top_weight_strength", 2.0))
        self.top_weight_power = float(loss_cfg.get("top_weight_power", 3.0))
        # V2 训练配置
        self.max_grad_norm = float(tcfg.get("max_grad_norm", 1.0))
        self.use_scheduler = bool(tcfg.get("use_scheduler", False))

        self.optimizer = torch.optim.AdamW(
            model.parameters(),
            lr=float(tcfg["learning_rate"]),
            weight_decay=float(tcfg["weight_decay"]),
        )
        self.scheduler = None
        if self.use_scheduler:
            self.scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
                self.optimizer, mode="max", factor=0.5, patience=5, min_lr=1e-6
            )
        # 固定种子的随机数生成器（pair 采样可复现）
        self.generator = torch.Generator().manual_seed(int(config["seed"]))
        self.train_loader = DataLoader(
            train_dataset, batch_size=1, shuffle=False, num_workers=0
        )
        self._gate_collapse_epochs = 0  # gate entropy 连续过低计数

    # ------------------------------------------------------------------ #
    # 训练主循环
    # ------------------------------------------------------------------ #
    def train(self) -> Dict[str, Any]:
        """执行训练，返回汇总结果。"""
        log_rows: List[Dict[str, Any]] = []
        best_rank_ic = -np.inf
        best_state = None
        wait = 0

        for epoch in range(1, self.epochs + 1):
            t0 = time.time()
            epoch_losses: List[float] = []
            epoch_gates: List[torch.Tensor] = []
            epoch_base: List[torch.Tensor] = []
            epoch_moe: List[torch.Tensor] = []
            epoch_experts: List[torch.Tensor] = []
            epoch_moe_scale: List[float] = []

            self.model.train()
            for batch in self.train_loader:
                # DataLoader batch_size=1 会多出 batch 维，squeeze 恢复 [N, F] / [M] / [N]
                stock = batch["stock_features"].squeeze(0).to(self.device)
                market = batch["market_features"].squeeze(0).to(self.device)
                labels = batch["labels"].squeeze(0).to(self.device)

                self.optimizer.zero_grad()
                out = self.model(stock, market, return_details=True)
                loss, parts = hybrid_loss(
                    out["scores"],
                    labels,
                    out["gate_weights"],
                    lambda_rank=self.lambda_rank,
                    lambda_mse=self.lambda_mse,
                    lambda_balance=self.lambda_balance,
                    pair_margin=self.pair_margin,
                    max_pairs_per_day=self.max_pairs,
                    use_balance_loss=self.use_balance_loss,
                    num_experts=self.num_experts,
                    generator=self.generator,
                    ranking_type=self.ranking_type,
                    top_weight_mode=self.top_weight_mode,
                    top_weight_strength=self.top_weight_strength,
                    top_weight_power=self.top_weight_power,
                )
                loss.backward()
                # V2: gradient clipping（默认 max_grad_norm=1.0）
                torch.nn.utils.clip_grad_norm_(
                    self.model.parameters(), self.max_grad_norm
                )
                self.optimizer.step()

                epoch_losses.append(loss.item())
                if out["gate_weights"] is not None:
                    epoch_gates.append(out["gate_weights"].detach().cpu())
                if out["base_scores"] is not None:
                    epoch_base.append(out["base_scores"].detach().cpu())
                if out["moe_scores"] is not None:
                    epoch_moe.append(out["moe_scores"].detach().cpu())
                if out["expert_scores"] is not None:
                    epoch_experts.append(out["expert_scores"].detach().cpu())
                if out["moe_scale"] is not None:
                    epoch_moe_scale.append(float(out["moe_scale"].detach().cpu()))

            # 验证集（eval 模式）
            valid = self.evaluate(self.valid_ds)
            valid_rank_ic = valid["rank_ic"]["mean_rank_ic"]
            if self.scheduler is not None:
                self.scheduler.step(valid_rank_ic)

            # Gate / V2 debug 统计
            gate_stats = self._gate_stats(epoch_gates)
            v2_stats = self._v2_stats(epoch_base, epoch_moe, epoch_experts, epoch_moe_scale)
            row = {
                "epoch": epoch,
                "train_loss": float(np.mean(epoch_losses)),
                "valid_loss": valid["valid_loss"],
                "valid_ic": valid["ic"]["mean_ic"],
                "valid_icir": valid["ic"]["icir"],
                "valid_rank_ic": valid_rank_ic,
                "valid_rank_icir": valid["rank_ic"]["rank_icir"],
                "lr": float(self.optimizer.param_groups[0]["lr"]),
                **gate_stats,
                **v2_stats,
                "time_sec": round(time.time() - t0, 1),
            }
            log_rows.append(row)

            # V2 collapse 自动报警（不停止训练）
            self._check_collapse(row, gate_stats, v2_stats)

            # Early stopping：验证集 mean RankIC
            if valid_rank_ic > best_rank_ic:
                best_rank_ic = valid_rank_ic
                best_state = copy.deepcopy(self.model.state_dict())
                best_epoch = epoch
                wait = 0
                print(
                    f"[epoch {epoch:3d}] train_loss={row['train_loss']:.4f} "
                    f"valid_loss={row['valid_loss']:.4f} valid_IC={row['valid_ic']:.4f} "
                    f"valid_RankIC={valid_rank_ic:.4f} (best)"
                )
            else:
                wait += 1
                print(
                    f"[epoch {epoch:3d}] train_loss={row['train_loss']:.4f} "
                    f"valid_loss={row['valid_loss']:.4f} valid_IC={row['valid_ic']:.4f} "
                    f"valid_RankIC={valid_rank_ic:.4f} (wait {wait}/{self.patience})"
                )
                if wait >= self.patience:
                    print(f"Early stopping at epoch {epoch} (patience={self.patience})")
                    break

        # 恢复最优模型并保存
        if best_state is not None:
            self.model.load_state_dict(best_state)
        self._save_checkpoint()

        log_df = pd.DataFrame(log_rows)
        log_df.to_csv(f"{self.output_dir}/training_log.csv", index=False)

        results = {
            "best_valid_rank_ic": float(best_rank_ic),
            "best_epoch": int(best_epoch),
            "log": log_df.to_dict(orient="records"),
        }
        with open(f"{self.output_dir}/metrics_train.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        return results

    # ------------------------------------------------------------------ #
    # 评估
    # ------------------------------------------------------------------ #
    @torch.no_grad()
    def evaluate(self, dataset) -> Dict[str, Any]:
        """在给定数据集上计算逐日 IC/RankIC 与平均损失（eval 模式）。"""
        self.model.eval()
        loader = DataLoader(dataset, batch_size=1, shuffle=False, num_workers=0)
        all_scores: List[np.ndarray] = []
        all_labels: List[np.ndarray] = []
        all_dates: List[str] = []
        losses: List[float] = []

        for batch in loader:
            stock = batch["stock_features"].squeeze(0).to(self.device)
            market = batch["market_features"].squeeze(0).to(self.device)
            labels = batch["labels"].squeeze(0).to(self.device)
            out = self.model(stock, market, return_details=True)
            loss, _ = hybrid_loss(
                out["scores"],
                labels,
                out["gate_weights"],
                lambda_rank=self.lambda_rank,
                lambda_mse=self.lambda_mse,
                lambda_balance=self.lambda_balance,
                pair_margin=self.pair_margin,
                max_pairs_per_day=self.max_pairs,
                use_balance_loss=self.use_balance_loss,
                num_experts=self.num_experts,
                generator=self.generator,
                ranking_type=self.ranking_type,
                top_weight_mode=self.top_weight_mode,
                top_weight_strength=self.top_weight_strength,
                top_weight_power=self.top_weight_power,
            )
            losses.append(loss.item())
            all_scores.append(out["scores"].cpu().numpy())
            all_labels.append(batch["future_returns"].squeeze(0).numpy())
            all_dates.extend([batch["date"][0]] * len(out["scores"]))

        daily = daily_ic(
            np.concatenate(all_scores), np.concatenate(all_labels), np.array(all_dates)
        )
        return {
            "ic": summarize_ic(daily),
            "rank_ic": summarize_ic(daily),
            "daily": daily,
            "valid_loss": float(np.mean(losses)) if losses else float("nan"),
        }

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _gate_stats(gates: List[torch.Tensor]) -> Dict[str, float]:
        """汇总一个 epoch 的 Gate: mean/std of 每个分量 + entropy。"""
        if not gates:
            return {
                "gate_1_mean": float("nan"),
                "gate_2_mean": float("nan"),
                "gate_3_mean": float("nan"),
                "gate_1_std": float("nan"),
                "gate_2_std": float("nan"),
                "gate_3_std": float("nan"),
                "gate_entropy": float("nan"),
            }
        g = torch.stack(gates).numpy()  # [days, K]
        stats: Dict[str, float] = {}
        for k in range(g.shape[1]):
            stats[f"gate_{k + 1}_mean"] = float(g[:, k].mean())
            stats[f"gate_{k + 1}_std"] = float(g[:, k].std(ddof=1))
        eps = 1e-8
        entropies = -(g * np.log(g + eps)).sum(axis=1)
        stats["gate_entropy"] = float(entropies.mean())
        return stats

    @staticmethod
    def _v2_stats(
        base: List[torch.Tensor],
        moe: List[torch.Tensor],
        experts: List[torch.Tensor],
        moe_scales: List[float],
    ) -> Dict[str, float]:
        """V2 debug 统计：base/moe score 的 mean/std、各 expert score std、moe_scale。"""
        stats: Dict[str, float] = {}
        for name, tensors in (("base", base), ("moe", moe)):
            if tensors:
                s = torch.cat(tensors).numpy()
                stats[f"{name}_score_mean"] = float(s.mean())
                stats[f"{name}_score_std"] = float(s.std(ddof=1))
            else:
                stats[f"{name}_score_mean"] = float("nan")
                stats[f"{name}_score_std"] = float("nan")
        if experts:
            e = torch.cat(experts).numpy()  # [days*N, K]
            for k in range(e.shape[1]):
                stats[f"expert_{k + 1}_score_std"] = float(e[:, k].std(ddof=1))
        else:
            for k in range(3):
                stats[f"expert_{k + 1}_score_std"] = float("nan")
        stats["moe_scale"] = float(np.mean(moe_scales)) if moe_scales else float("nan")
        return stats

    def _check_collapse(self, row: Dict[str, Any], gate_stats: Dict[str, float],
                        v2_stats: Dict[str, float]) -> None:
        """V2 自动报警：Gate/Expert collapse、MoE residual 爆炸（只警告，不停止训练）。"""
        entropy = gate_stats.get("gate_entropy")
        if entropy is not None and not np.isnan(entropy):
            if entropy < GATE_COLLAPSE_ENTROPY:
                self._gate_collapse_epochs += 1
                if self._gate_collapse_epochs >= 3:
                    print(f"WARNING: possible gate collapse "
                          f"(gate_entropy={entropy:.4f} < {GATE_COLLAPSE_ENTROPY}, "
                          f"连续 {self._gate_collapse_epochs} 个 epoch)")
            else:
                self._gate_collapse_epochs = 0
        for k in range(1, 4):
            std = v2_stats.get(f"expert_{k}_score_std")
            if std is not None and not np.isnan(std) and std < EXPERT_COLLAPSE_STD:
                print(f"WARNING: possible expert collapse "
                      f"(expert_{k}_score_std={std:.2e} < {EXPERT_COLLAPSE_STD})")
        scale = v2_stats.get("moe_scale")
        if scale is not None and not np.isnan(scale) and abs(scale) > MOE_SCALE_ALARM:
            print(f"WARNING: moe_scale 过大 |{scale:.3f}| > {MOE_SCALE_ALARM}，"
                  f"MoE residual 可能爆炸")

    def _save_checkpoint(self) -> None:
        """保存 best_model.pt：模型权重 + config + 特征名 + scaler + 架构信息。"""
        checkpoint = {
            "model_state": self.model.state_dict(),
            "config": self.cfg,
            "feature_names": self.feature_names,
            "market_feature_names": self.market_feature_names,
            "scaler": self.scaler,
            "market_scaler": self.market_scaler,
            "architecture": str(self.model),
            "param_count": count_parameters(self.model),
            "model_class": self.model.__class__.__name__,
        }
        torch.save(checkpoint, f"{self.output_dir}/best_model.pt")
