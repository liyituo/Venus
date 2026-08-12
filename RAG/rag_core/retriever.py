"""检索层：向量优先，词法（BM25 风格）兜底。

- embedder 可用：query → 向量 → 余弦 top_k
- embedder 不可用：BM25 词法打分（保证框架无 embedding 也能用）
- 词法检索分词：ASCII 词 + 中文二元组（与 PC Agent 的 history_index 一致）
"""
from __future__ import annotations

import math
import re
from collections import Counter

_ASCII_RE = re.compile(r"[A-Za-z0-9_./\\\-]{2,}")
_CJK_RE = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af]{2}")


def tokenize(text: str) -> list[str]:
    toks: list[str] = []
    for m in _ASCII_RE.finditer(text):
        toks.append(m.group().lower())
    cjk = "".join(re.findall(r"[\u2e80-\u9fff\uac00-\ud7af]", text))
    toks.extend(_CJK_RE.findall(cjk))
    return toks


class LexicalIndex:
    """BM25 风格词法索引（embedding 不可用时的兜底检索）。"""

    def __init__(self, chunks: list[dict]):
        self._n = len(chunks)
        self._lengths: list[int] = []
        self._postings: dict[str, list[tuple[int, int]]] = {}
        self._idf: dict[str, float] = {}
        self._chunks = chunks
        self._build()

    def _build(self) -> None:
        df: Counter = Counter()
        for i, c in enumerate(self._chunks):
            toks = tokenize(c["text"])
            self._lengths.append(len(toks))
            seen = set()
            for t in toks:
                if t not in seen:
                    seen.add(t)
                    df[t] += 1
                self._postings.setdefault(t, []).append((i, 1))
        n = max(1, self._n)
        for t, d in df.items():
            self._idf[t] = math.log(1 + (n - d + 0.5) / (d + 0.5))

    def search(self, query: str, top_k: int) -> list[dict]:
        q_toks = tokenize(query)
        if not q_toks or not self._n:
            return []
        scores: dict[int, float] = {}
        avg_len = sum(self._lengths) / len(self._lengths)
        for t in q_toks:
            idf = self._idf.get(t)
            if idf is None:
                continue
            for idx, freq in self._postings.get(t, []):
                tf = freq / (freq + 0.5 + 1.5 * self._lengths[idx] / max(1, avg_len))
                scores[idx] = scores.get(idx, 0.0) + idf * tf
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]
        return [{"seq": int(i), "doc_id": self._chunks[i]["doc_id"],
                 "text": self._chunks[i]["text"], "score": float(s)}
                for i, s in ranked if s > 0]
