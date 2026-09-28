"""派发路由：判断用户输入应同步对话还是异步 Job，并推荐推理档位。

启发式（可扩展为模型分类）；前端/CLI 可调用 analyze 后决定走
/chat/stream 还是 POST /jobs，以及用 low/high/max 哪档推理。
档位语义：low 省钱快响应，medium 默认，high 复杂编码/排障，
max 方案设计/全库审计等重推理任务。
"""

from __future__ import annotations

import re

# 明显适合后台跑的长任务
_ASYNC_PATTERNS = (
    re.compile(r"跑(一下|一遍|下)?\s*(测试|test|pytest|unittest)", re.I),
    re.compile(r"\b(pytest|npm test|cargo test|go test)\b", re.I),
    re.compile(r"(整理|归档|批量|扫描|全库|整个项目|所有文件)", re.I),
    re.compile(r"(写报告|生成报告|总结报告|日报|周报)", re.I),
    re.compile(r"(重构|迁移|批量改|批量替换)", re.I),
    re.compile(r"(后台|异步|派活|慢慢|不着急)", re.I),
)

# 明显适合同步的短交互
_SYNC_PATTERNS = (
    re.compile(r"^(什么是|解释一下|为什么|怎么用|帮我看(一下)?)$", re.I),
    re.compile(r"^(hi|hello|你好|在吗|谢谢|好的)[\s!！。.?？]*$", re.I),
    re.compile(r"^(是|否|对|不对|继续|好的|可以)[\s!！。.?？]*$", re.I),
)

# 推理档位启发式（顺序优先：max > high > low，兜底 medium）
_MAX_PATTERNS = (
    re.compile(r"(方案|架构|设计|审计|安全|全库|源码分析|技术选型|对比.*方案)", re.I),
    re.compile(r"(为什么.*失败|根因|深入分析|逐步推理|数学|算法证明)", re.I),
    re.compile(r"\b(design|architecture|audit|rfc|proposal)\b", re.I),
)
_HIGH_PATTERNS = (
    re.compile(r"(写代码|实现|排障|debug|报错|堆栈|重构|迁移|优化性能)", re.I),
    re.compile(r"(批量改|批量替换|测试失败|修bug|修复)", re.I),
    re.compile(r"\b(pytest|traceback|refactor|implement|fix bug)\b", re.I),
)
_LOW_PATTERNS = (
    re.compile(r"^(hi|hello|你好|在吗|谢谢|好的|列目录|查配置|改文件名|看一下).{0,20}$", re.I),
    re.compile(r"^(是|否|对|不对|继续|可以)[\s!！。.?？]*$", re.I),
)

_MIN_ASYNC_CHARS = 80


def analyze_reasoning(text: str) -> dict:
    """返回 {effort, reason, confidence}，effort 为 low/medium/high/max。"""
    t = (text or "").strip()
    if not t:
        return {"effort": "low", "reason": "空输入", "confidence": 1.0}
    if any(p.search(t) for p in _MAX_PATTERNS) or len(t) >= 200:
        return {"effort": "max", "reason": "重推理任务（方案/审计/长输入）", "confidence": 0.75}
    if any(p.search(t) for p in _HIGH_PATTERNS):
        return {"effort": "high", "reason": "复杂编码/排障任务", "confidence": 0.7}
    if len(t) <= 20 and any(p.search(t) for p in _LOW_PATTERNS):
        return {"effort": "low", "reason": "简单问候/小操作", "confidence": 0.85}
    if len(t) <= 40:
        return {"effort": "low", "reason": "短输入默认低档", "confidence": 0.6}
    return {"effort": "medium", "reason": "默认中档", "confidence": 0.5}


def analyze_dispatch(text: str, *, history_turns: int = 0) -> dict:
    """返回 {mode, reason, confidence, effort, effort_reason, effort_confidence}。"""
    t = (text or "").strip()
    reasoning = analyze_reasoning(t)
    base = {"effort": reasoning["effort"],
            "effort_reason": reasoning["reason"],
            "effort_confidence": reasoning["confidence"]}
    if not t:
        return {"mode": "sync", "reason": "空输入", "confidence": 1.0, **base}

    if len(t) <= 12 and any(p.search(t) for p in _SYNC_PATTERNS):
        return {"mode": "sync", "reason": "短问候/确认", "confidence": 0.9, **base}

    if any(p.search(t) for p in _ASYNC_PATTERNS):
        return {"mode": "async", "reason": "匹配长任务关键词", "confidence": 0.85, **base}

    if len(t) >= _MIN_ASYNC_CHARS and history_turns == 0:
        return {"mode": "async", "reason": "较长单轮任务描述", "confidence": 0.6, **base}

    if len(t) >= 200:
        return {"mode": "async", "reason": "输入较长，建议后台执行", "confidence": 0.7, **base}

    return {"mode": "sync", "reason": "默认同步对话", "confidence": 0.5, **base}
