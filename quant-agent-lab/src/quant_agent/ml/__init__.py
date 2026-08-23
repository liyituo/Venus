"""Tiny-MoE 推理模块（vendored，供 tiny-moe-ranker 策略离线加载 checkpoint）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .predictor import QuantPredictor

__all__ = ["QuantPredictor"]


def __getattr__(name: str) -> object:
    if name == "QuantPredictor":
        from .predictor import QuantPredictor

        return QuantPredictor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
