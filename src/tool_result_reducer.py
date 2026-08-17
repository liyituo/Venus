"""工具结果压缩（Tool Result Reducer）。

工具输出是主要 Token 消耗来源。目标：
- 完整原始结果保存在本地 ResultStore，以 result_id 引用，绝不永久丢弃；
- 发送给模型的是结构化摘要：head/tail 都保留，错误附近绝不丢失；
- 连续重复行去重（进度条/日志噪音）；
- 统计被省略行数与字符数；
- 简短输出（<= 上限）原样返回，不额外处理（总结成本 > 节省量）；
- 不压缩失败原因、行号、路径、返回码、断言差异。

模型可通过 fetch_result 工具按 result_id 取回指定区段（见 llm_server 集成）。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict

# 结果存储上限：条目数与总字符（防内存膨胀）
_STORE_MAX_ITEMS = 200
_STORE_MAX_CHARS = 20_000_000

_HEAD_RATIO = 0.35          # 头部保留比例
_TAIL_RATIO = 0.30          # 尾部保留比例（错误/堆栈常在末尾）
_ERR_RADIUS = 2             # 错误行上下文行数
_MAX_CONSECUTIVE_DUP = 1    # 连续相同行最多保留的行数
_SECTION_LINES = 150        # fetch_result 单区段最大行数

# 错误特征行（不区分大小写匹配）
_ERR_PATTERNS = (
    re.compile(r"\b(error|failed|failure|fatal|exception|traceback|assert|crash)\b", re.I),
    re.compile(r"^\s*(exit\s+code|return\s+code|rc=)"),
    re.compile(r"(✗|FAIL|FAILED|ERROR|WARN|DEPRECATED|not\s+found|no\s+such)"),
    re.compile(r"[A-Za-z_]+Error:"),       # Python 异常名
)


class ResultStore:
    """线程安全的工具结果存储（LRU）：result_id -> (meta, full_text)。"""

    def __init__(self, max_items: int = _STORE_MAX_ITEMS,
                 max_chars: int = _STORE_MAX_CHARS):
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, tuple[dict, str]]" = OrderedDict()
        self._max_items = max_items
        self._max_chars = max_chars
        self._total_chars = 0

    def put(self, result_id: str, meta: dict, full: str) -> None:
        with self._lock:
            if result_id in self._items:
                self._items.move_to_end(result_id)
                return
            self._items[result_id] = (meta, full)
            self._total_chars += len(full)
            while len(self._items) > self._max_items or self._total_chars > self._max_chars:
                old_id, (_, old) = self._items.popitem(last=False)
                self._total_chars -= len(old)

    def get(self, result_id: str) -> tuple[dict, str] | None:
        with self._lock:
            item = self._items.get(result_id)
            if item is None:
                return None
            self._items.move_to_end(result_id)
            return item

    def section(self, result_id: str, section: str,
                max_lines: int = _SECTION_LINES) -> str | None:
        """取回指定区段：head / tail / error / full（均限行数）。"""
        item = self.get(result_id)
        if item is None:
            return None
        _, full = item
        lines = full.splitlines()
        if section == "full":
            return "\n".join(lines[:max_lines]) + (
                f"\n...(共 {len(lines)} 行，仅显示前 {max_lines} 行)" if len(lines) > max_lines else "")
        if section == "head":
            return "\n".join(lines[:max_lines])
        if section == "tail":
            return "\n".join(lines[-max_lines:])
        if section == "error":
            return _extract_error_block(full, _SECTION_LINES)
        return None


def new_result_id(name: str, text: str) -> str:
    """稳定 result_id：工具名 + 内容 hash 前 10 位。"""
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:10]
    return f"{name[:18]}-{digest}"


def _dedup_lines(lines: list[str]) -> tuple[list[str], int]:
    """连续相同行去重；返回 (行列表, 被移除行数)。"""
    out: list[str] = []
    removed = 0
    for line in lines:
        if out and out[-1] == line:
            removed += 1
            continue
        out.append(line)
    return out, removed


def _extract_error_block(text: str, max_lines: int) -> str:
    """提取错误特征行附近的内容（模型诊断最需要的区段）。"""
    lines = text.splitlines()
    hits: list[int] = []
    for i, line in enumerate(lines):
        if any(p.search(line) for p in _ERR_PATTERNS):
            hits.append(i)
    if not hits:
        return "\n".join(lines[-max_lines:])
    # 合并重叠区段（错误行 ± 半径）
    merged: list[tuple[int, int]] = []
    for h in hits:
        s, e = max(0, h - _ERR_RADIUS), min(len(lines), h + _ERR_RADIUS + 1)
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    spans: list[str] = []
    for s, e in merged:
        spans.extend(lines[s:e])
        spans.append("…")
    text = "\n".join(spans)
    if len(lines) > max_lines and len(text.splitlines()) > max_lines:
        text = "\n".join(text.splitlines()[:max_lines]) + "\n…"
    return text


def _summarize_json(result: str, max_chars: int) -> str | None:
    """对工具返回的 JSON 对象做结构化摘要；非 JSON 返回 None。"""
    try:
        d = json.loads(result)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    parts: list[str] = []
    for k in ("ok", "success", "status", "exit_code", "returncode", "changed", "created", "deleted"):
        if k in d:
            parts.append(f"{k}={d[k]}")
    for k in ("changed_files", "files", "created_files"):
        v = d.get(k)
        if isinstance(v, (list, tuple)):
            parts.append(f"{k}=[{', '.join(str(x) for x in v[:4])}"
                         + (f", …共{len(v)}个" if len(v) > 4 else "") + "]")
        elif v:
            parts.append(f"{k}={v}")
    err = d.get("error") or d.get("stderr")
    if isinstance(err, str) and err.strip():
        parts.append(f"error={err[:220]}")
    out = d.get("stdout")
    if isinstance(out, str) and out.strip():
        parts.append(f"stdout={out[:220]}")
    if not parts:
        return None
    summary = "{" + ", ".join(parts) + "}"
    return summary[:max_chars]


def _fit_lines(lines: list[str], budget: int) -> list[str]:
    """按字符预算逐行裁剪（保留尽可能多的完整行）。"""
    out: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 1
        if used + cost > budget:
            break
        out.append(line)
        used += cost
    return out


def reduce_tool_result(name: str, args: dict, result: str, ok: bool,
                       max_chars: int = 1500, store: ResultStore | None = None) -> tuple[str, dict]:
    """压缩工具结果；完整原文存入 store（若提供）。

    返回 (发送给模型的文本, meta)。meta 含 result_id/original_chars/reduced_chars/
    dropped_lines/truncated。简短结果原样返回且不占存储。

    head/error/tail 分区预算：错误区段（尾部的错误、堆栈）绝不因头部过长被挤掉。
    """
    result = result or ""
    orig_len = len(result)
    if orig_len <= max_chars:
        return result, {"result_id": None, "original_chars": orig_len,
                        "reduced_chars": orig_len, "dropped_lines": 0, "truncated": False}

    lines = result.splitlines()
    n_lines = len(lines)
    # 结构化 JSON 摘要优先（保留关键值 + error/stdout 片段）
    json_summary = _summarize_json(result, max_chars)
    if json_summary is not None:
        reduced = json_summary
        dropped_lines = n_lines
    else:
        # 通用文本：head / error 块 / tail 分区预算（错误绝不丢）
        head_n = max(8, int(n_lines * _HEAD_RATIO))
        tail_n = max(8, int(n_lines * _TAIL_RATIO))
        head_budget = int(max_chars * 0.35)
        err_budget = int(max_chars * 0.35)
        tail_budget = max(24, max_chars - head_budget - err_budget)
        head, _ = _dedup_lines(lines[:head_n])
        tail, _ = _dedup_lines(lines[-tail_n:])
        err_lines = _extract_error_block(result, 120).splitlines()
        head_fit = _fit_lines(head, head_budget)
        err_fit = _fit_lines(err_lines, err_budget)
        # tail 从末尾倒序保留（最新的行价值最高，错误/结论常在末尾）
        tail_fit = list(reversed(_fit_lines(list(reversed(tail)), tail_budget)))
        err_text = "\n".join(err_fit)
        tail_text = "\n".join(tail_fit)
        head_text = "\n".join(head_fit)
        # error 块若已完整包含在 tail/head 中则跳过（避免重复）
        err_dup = (err_text and (err_text in tail_text or err_text in head_text))
        blocks: list[str] = []
        if head_text:
            blocks.append(("…前段…\n" if n_lines > len(head_fit) else "") + head_text)
        if err_text and not err_dup:
            blocks.append("…错误区段…\n" + err_text)
        if tail_text:
            blocks.append("…尾段…\n" + tail_text)
        reduced = "\n\n".join(blocks)
        dropped_lines = n_lines - (len(head_fit) + len(tail_fit))
    # 限制最终长度（极端情况保护；分区预算已保证 head/tail/error 不互挤）
    if len(reduced) > max_chars:
        reduced = reduced[:max_chars] + "\n…"

    result_id = None
    if store is not None:
        # 随机盐：result_id 不可预测（内容 hash 是确定性可计算的，
        # 直接暴露会让其他会话/任务可猜测并取回完整结果——M3）
        import uuid
        result_id = f"{new_result_id(name, result)}-{uuid.uuid4().hex[:8]}"
        store.put(result_id, {"name": name, "ok": ok, "chars": orig_len}, result)
    reduced += (f"\n\n[完整结果 {orig_len} 字符/{n_lines} 行已省略，"
                f"可用 fetch_result 按 id 查看：{result_id or '未存储'}]")
    meta = {"result_id": result_id, "original_chars": orig_len,
            "reduced_chars": len(reduced), "dropped_lines": dropped_lines,
            "truncated": True}
    return reduced, meta
