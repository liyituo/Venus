"""Token 感知的上下文预算管理（Context Budget Manager）。

不再用"字符数 × 固定系数"作为唯一判断：
- 分层统计 system / tool schema / conversation / image / output / reasoning headroom；
- 阈值根据模型上下文窗口、任务类型与预计输出动态计算；
- 优先使用模型 tokenizer / provider 计数接口（外部注入），缺失时保守估算 + 安全余量；
- 达到阈值时报告"应压缩"，由调用方决定压缩策略（本模块不修改消息）。

只读、无副作用、线程安全（纯函数 + 缓存），可被 chat.py / cli / llm_server 复用。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# 估算系数（保守：估算值 >= 真实值的概率高，避免撑爆上下文）
_CJK_RE = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")
_CJK_WEIGHT = 1.05        # 中日韩字符 → token
_ASCII_WEIGHT = 0.32      # ASCII 字符 → token（约 3.1 字符/token）
_TOKEN_FUDGE = 1.08       # 总安全系数：估算上浮 8%

# 分层阈值（动态）：可用窗口的占比，随任务类型调整
_THRESHOLDS = {
    "simple": 0.55,       # 简单问答：少量历史就压缩（历史价值低）
    "default": 0.68,
    "coding": 0.75,       # 复杂编码/多工具任务：保留更多上下文，延迟压缩
    "analysis": 0.75,
}
_DEFAULT_TASK = "default"


@dataclass
class BudgetReport:
    """一次请求的预算报告（发送前生成）。"""

    context_window: int = 0
    system_tokens: int = 0
    tool_tokens: int = 0
    conversation_tokens: int = 0
    image_tokens: int = 0
    retrieved_tokens: int = 0      # 检索注入的旧历史
    output_budget: int = 0
    reasoning_headroom: int = 0    # 推理强度 max 时预留
    safety_margin: int = 0
    used: int = 0                  # 输入侧已用（不含输出预算）
    available: int = 0             # 输入侧可用上限
    status: str = "ok"             # ok / compress / over
    task: str = _DEFAULT_TASK
    estimated: bool = True         # True = 估算值（provider 未提供计数接口）

    def to_dict(self) -> dict:
        return {f: getattr(self, f) for f in (
            "context_window", "system_tokens", "tool_tokens", "conversation_tokens",
            "image_tokens", "retrieved_tokens", "output_budget", "reasoning_headroom",
            "safety_margin", "used", "available", "status", "task", "estimated")}

    @property
    def input_tokens(self) -> int:
        return (self.system_tokens + self.tool_tokens + self.conversation_tokens
                + self.image_tokens + self.retrieved_tokens)

    @property
    def usage_ratio(self) -> float:
        return self.input_tokens / max(1, self.available)


def estimate_tokens(text: str) -> int:
    """保守估算文本 token 数（中英混合）。估算值偏大，留安全余量。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    rest = max(0, len(text) - cjk)
    raw = cjk * _CJK_WEIGHT + rest * _ASCII_WEIGHT
    return max(1, int(raw * _TOKEN_FUDGE) + 1)


def estimate_messages_tokens(messages: list[dict]) -> int:
    """估算消息列表 token 数（含 role 开销）。"""
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, list):          # 多模态 content 数组
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        total += estimate_tokens(part.get("text") or "")
                    elif part.get("type") == "image_url":
                        total += estimate_image_tokens(part)
                else:
                    total += estimate_tokens(str(part))
        else:
            total += estimate_tokens(str(content))
        total += 4                              # role/结构开销
    return total


def estimate_image_tokens(part: dict, detail: str = "auto") -> int:
    """图片 token 估算（OpenAI 计费规则的保守近似）。

    high detail 按瓦片估算（765 + 170/瓦片，上限 4 瓦片）；
    low/auto 按固定保守值。坐标敏感任务必须保持 high/original。
    """
    img = part.get("image_url") or {}
    url = img.get("url") or ""
    if detail == "high" or "detail=high" in url:
        return 765 + 170 * 4
    if detail == "low" or "detail=low" in url:
        return 85
    return 360                                     # auto：保守取中


def plan_budget(*, context_window: int, system_text: str = "", tools: list | None = None,
                messages: list | None = None, task: str = _DEFAULT_TASK,
                output_budget: int = 0, reasoning_mode: str = "max",
                image_parts: list | None = None,
                retrieved_text: str = "", provider_token_count=None) -> BudgetReport:
    """生成发送前预算报告。

    provider_token_count: 可选的外部精确计数回调（tokenizer / provider 接口），
    签名 (text) -> int；提供时优先使用，缺失时用保守估算（报告标记 estimated）。
    """
    est = provider_token_count or estimate_tokens

    def _count(text: str) -> int:
        try:
            return int(est(text))
        except Exception:
            return estimate_tokens(text)

    system_tokens = _count(system_text)
    tool_tokens = estimate_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
    conversation_tokens = estimate_messages_tokens(messages) if messages else 0
    image_tokens = sum(estimate_image_tokens(p) for p in (image_parts or []))
    retrieved_tokens = _count(retrieved_text)

    # 输出预算：调用方传入；缺省按任务类型
    if output_budget <= 0:
        output_budget = 2000 if task in ("coding", "analysis") else 800
    # 推理余量：max/high 时预留窗口的一部分（输出可能被推理先占）
    reasoning_headroom = 0
    if reasoning_mode in ("max", "high"):
        reasoning_headroom = int(context_window * 0.08)
    safety_margin = int(context_window * 0.05)

    used = system_tokens + tool_tokens + conversation_tokens + image_tokens + retrieved_tokens
    available = max(0, context_window - output_budget - reasoning_headroom - safety_margin)
    ratio = _THRESHOLDS.get(task, _THRESHOLDS[_DEFAULT_TASK])
    threshold = int(available * ratio)
    if used > available:
        status = "over"
    elif used > threshold:
        status = "compress"
    else:
        status = "ok"
    return BudgetReport(
        context_window=context_window, system_tokens=system_tokens,
        tool_tokens=tool_tokens, conversation_tokens=conversation_tokens,
        image_tokens=image_tokens, retrieved_tokens=retrieved_tokens,
        output_budget=output_budget, reasoning_headroom=reasoning_headroom,
        safety_margin=safety_margin, used=used, available=available,
        status=status, task=task,
        estimated=provider_token_count is None)
