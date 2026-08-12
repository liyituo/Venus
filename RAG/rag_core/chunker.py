"""文本分块：中文友好的段落优先 + 定长兜底（带重叠）。

策略：
1. 按空行切段落，段落 ≤ 上限直接成块；
2. 超长段落按字符窗口切（CHUNK_SIZE + OVERLAP），尽量在句子边界断开。
"""
from __future__ import annotations

import re

CHUNK_SIZE = 600        # 单块最大字符数（约 300-400 tokens）
OVERLAP = 60            # 相邻块重叠字符（保持上下文连贯）
_MIN_CHUNK = 40         # 低于此长度的块与相邻块合并（避免碎片噪音）

_SENT_BOUNDARY = re.compile(r"(?<=[。！？!?；;．.\n])")


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE,
               overlap: int = OVERLAP) -> list[str]:
    """把文本切成块；返回非空块列表。

    overlap 钳制到 chunk_size 的一半以内：overlap >= chunk_size 时
    滑动步长退化为 1 字符，会产生海量碎片块。
    """
    chunk_size = max(10, int(chunk_size))
    overlap = max(0, min(int(overlap), chunk_size // 2))
    text = (text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    for para in paragraphs:
        if len(para) <= chunk_size:
            chunks.append(para)
        else:
            chunks.extend(_cut_long(para, chunk_size, overlap))
    return _merge_fragments(chunks, chunk_size, _MIN_CHUNK)


def _cut_long(para: str, chunk_size: int, overlap: int) -> list[str]:
    """超长段落：按句子边界优先的滑动窗口切分。"""
    sentences = [s for s in _SENT_BOUNDARY.split(para) if s.strip()]
    out: list[str] = []
    buf = ""
    for sent in sentences:
        if len(buf) + len(sent) <= chunk_size:
            buf += sent
            continue
        if buf:
            out.append(buf.strip())
        # 单句超长：硬切（步长至少 1，防止 overlap >= chunk_size 死循环）
        if len(sent) > chunk_size:
            step = max(1, chunk_size - overlap)
            start = 0
            while start < len(sent):
                out.append(sent[start:start + chunk_size].strip())
                start += step
            buf = ""
        else:
            buf = sent
    if buf.strip():
        out.append(buf.strip())
    return [c for c in out if c]


def _merge_fragments(chunks: list[str], chunk_size: int, min_len: int) -> list[str]:
    """把过短的碎片并入前一块（保持信息不丢）。"""
    merged: list[str] = []
    for c in chunks:
        if not merged:
            merged.append(c)
            continue
        if len(c) < min_len and len(merged[-1]) + len(c) <= chunk_size + 200:
            merged[-1] = merged[-1] + c
        else:
            merged.append(c)
    return [c for c in merged if c.strip()]
