"""Tiny-MoE 推理模块（vendored，供 tiny-moe-ranker 策略离线加载 checkpoint）。"""

from .predictor import QuantPredictor

__all__ = ["QuantPredictor"]
