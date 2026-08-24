"""历史消息按需检索（HistoryIndex）。

模型默认只获得与当前任务相关的旧信息，而不是整个历史：
- 完整会话原文保存在本地（.venus/sessions.json 等），本模块只做内存索引；
- 关键词/二元组倒排索引：低成本、本地、确定性（不引入 embeddings）；
- 检索结果包含消息 id / role / 时间 / 命中片段；
- 与结构化摘要协作：压缩时记录 retrieval_keys（路径/函数名/数字/约束），
  新用户消息命中这些键时自动检索对应原文插入当前请求；
- 如果摘要与原文冲突，以原始消息为准（检索原文优先）。

会话删除或重建时调用 clear() 丢弃索引，绝不持有用户数据副本之外的持久状态。
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict

_MAX_INDEX_MESSAGES = 2000      # 索引上限（超长会话只索引最近部分）
_MAX_HITS = 6                   # 单次检索最大命中数
_SNIPPET_RADIUS = 80            # 命中片段半径（字符）
_MAX_SNIPPET_TOTAL = 1600       # 全部片段合计上限

_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_./\\\-]{2,}")
_CJK_CHAR_RE = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af]")
_CJK_TERM_RE = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af]{2}")   # CJK 二元组


def tokenize(text: str) -> set[str]:
    """确定性分词：ASCII 词（含路径片段）+ CJK 二元组。"""
    tokens: set[str] = set()
    for m in _ASCII_TOKEN_RE.finditer(text):
        tok = m.group()
        if tok not in ("ok", "no", "is", "in", "to", "of", "on", "at", "do", "if"):
            tokens.add(tok.lower())
    cjk_span = "".join(_CJK_CHAR_RE.findall(text))
    for m in _CJK_TERM_RE.finditer(cjk_span):
        tokens.add(m.group())
    return tokens


def find_keys(text: str, keys: list[str]) -> list[str]:
    """检测文本中出现的检索键（键可能包含路径/符号，做子串匹配）。"""
    lowered = text.lower()
    hits = []
    for key in keys:
        k = str(key).lower().strip()
        if len(k) >= 2 and k in lowered:
            hits.append(k)
    return hits


class HistoryIndex:
    """消息倒排索引（线程安全，仅内存）。"""

    def __init__(self, max_messages: int = _MAX_INDEX_MESSAGES):
        self._lock = threading.Lock()
        self._max_messages = max_messages
        self._messages: list[dict] = []            # 顺序消息（含索引元数据）
        self._by_term: dict[str, list[int]] = defaultdict(list)  # term -> msg index
        self._msg_by_id: dict[str, int] = {}       # message id -> index

    def clear(self) -> None:
        with self._lock:
            self._messages.clear()
            self._by_term.clear()
            self._msg_by_id.clear()

    def add_messages(self, messages: list[dict]) -> int:
        """增量索引消息（返回新增条数）。重复消息 id 跳过。"""
        added = 0
        with self._lock:
            for m in messages:
                mid = m.get("id") or m.get("message_id")
                if mid is not None and mid in self._msg_by_id:
                    continue
                content = m.get("content") or ""
                if isinstance(content, list):
                    content = " ".join(str(p.get("text", "")) if isinstance(p, dict) else str(p)
                                       for p in content)
                tokens = tokenize(str(content))
                idx = len(self._messages)
                self._messages.append({
                    "id": mid, "role": m.get("role"),
                    "content": str(content), "ts": m.get("ts") or time.time(),
                    "tokens": tokens,
                })
                for t in tokens:
                    self._by_term[t].append(idx)
                if mid is not None:
                    self._msg_by_id[mid] = idx
                added += 1
            # 超限裁剪：只保留最近的消息
            overflow = len(self._messages) - self._max_messages
            if overflow > 0:
                keep = self._messages[overflow:]
                self._messages = keep
                self._by_term = defaultdict(list)
                self._msg_by_id = {}
                for i, m in enumerate(keep):
                    for t in m["tokens"]:
                        self._by_term[t].append(i)
                    if m["id"] is not None:
                        self._msg_by_id[m["id"]] = i
        return added

    def search(self, query: str, top_k: int = _MAX_HITS,
               exclude_ids: set | None = None) -> list[dict]:
        """关键词检索；返回按相关度排序的命中（含片段）。"""
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        with self._lock:
            exclude_ids = exclude_ids or set()
            scores: dict[int, int] = defaultdict(int)
            for t in q_tokens:
                for idx in self._by_term.get(t, ()):
                    scores[idx] += 1
            # 长键（>=4 字符）命中加权：精确子串价值更高。
            # 用消息在 self._messages 中的位置索引加权：消息可能没有 id
            # （msg_by_id 缺失时 get 回退 0 会把所有加权错加到索引 0）。
            for idx, m in enumerate(self._messages):
                content = m["content"].lower()
                for t in q_tokens:
                    if len(t) >= 4 and t in content:
                        scores[idx] += 2
            ranked = sorted(scores.items(), key=lambda kv: (-kv[1], -kv[0]))
        hits = []
        total_chars = 0
        for idx, score in ranked[:top_k]:
            msg = self._messages[idx]
            if msg["id"] in exclude_ids:
                continue
            content = msg["content"]
            snippet = _make_snippet(content, query, _SNIPPET_RADIUS)
            total_chars += len(snippet)
            if total_chars > _MAX_SNIPPET_TOTAL and hits:
                break
            hits.append({
                "message_id": msg["id"], "role": msg["role"], "score": score,
                "ts": msg["ts"], "snippet": snippet,
            })
        return hits


def _make_snippet(content: str, query: str, radius: int) -> str:
    """命中位置附近片段（找不到命中点则取开头）。"""
    pos = content.lower().find(query[:40].lower())
    if pos < 0:
        # 用第一个查询词定位
        for word in _ASCII_TOKEN_RE.findall(query) + list(_CJK_TERM_RE.findall(query)):
            p = content.lower().find(word.lower())
            if p >= 0:
                pos = p
                break
    if pos < 0:
        return content[: radius * 2] + ("…" if len(content) > radius * 2 else "")
    start = max(0, pos - radius)
    end = min(len(content), pos + radius)
    return ("…" if start > 0 else "") + content[start:end] + ("…" if end < len(content) else "")
