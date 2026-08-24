"""派发路由：判断用户输入应同步对话还是异步 Job。

启发式（可扩展为模型分类）；前端/CLI 可调用 analyze 后决定走
/chat/stream 还是 POST /jobs。
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

_MIN_ASYNC_CHARS = 80


def analyze_dispatch(text: str, *, history_turns: int = 0) -> dict:
    """返回 {mode, reason, confidence}，mode 为 sync 或 async。"""
    t = (text or "").strip()
    if not t:
        return {"mode": "sync", "reason": "空输入", "confidence": 1.0}

    if len(t) <= 12 and any(p.search(t) for p in _SYNC_PATTERNS):
        return {"mode": "sync", "reason": "短问候/确认", "confidence": 0.9}

    if any(p.search(t) for p in _ASYNC_PATTERNS):
        return {"mode": "async", "reason": "匹配长任务关键词", "confidence": 0.85}

    if len(t) >= _MIN_ASYNC_CHARS and history_turns == 0:
        return {"mode": "async", "reason": "较长单轮任务描述", "confidence": 0.6}

    if len(t) >= 200:
        return {"mode": "async", "reason": "输入较长，建议后台执行", "confidence": 0.7}

    return {"mode": "sync", "reason": "默认同步对话", "confidence": 0.5}
