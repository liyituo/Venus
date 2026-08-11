"""分层提示词缓存管理（PromptCacheManager）。

目标：让稳定且重复的提示词前缀保持字节级稳定，配合供应商原生隐式缓存
（DeepSeek/OpenAI 的 cached_tokens），并避免"prefix churn"。

分层（P0 最稳定，P5 最动态）：
  P0 长期系统规则/安全规则   P1 工具定义   P2 Agent/项目规则
  P3 会话压缩摘要            P4 检索内容/工具结果   P5 用户输入

原则：
- 稳定前缀中禁止时间戳/随机 ID/请求 ID/密钥/动态内容；
- 工具按稳定标识排序，JSON 确定性序列化；
- 版本变化只失效对应层级；用户新消息不得使系统层失效；
- 本地构造缓存命中 ≠ 模型 Token 节省（只有供应商 cached_tokens 才算）；
- 显式缓存写入成本高于收益时禁止（net_saving 决策函数）；
- 缓存内容绑定 provider+model+locale（隔离域），提供 clear() 接口。
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass

from provider_capabilities import json_dumps_compact

# ---- 层级版本（内容变化时由集成方调用 invalidate 递增）----
_SYSTEM_VERSION = 1      # P0：系统/安全规则
_TOOL_VERSION = 1        # P1：工具 schema
_AGENT_VERSION = 1       # P2：Agent 定义/项目规则
_PROJECT_VERSION = 1     # P2：工作区项目规则

# 观测窗口
_OBSERVE_MAX = 500


@dataclass
class CacheDecision:
    """显式缓存决策（规格 §6 的 net_saving 模型）。"""

    use_explicit: bool
    stable_prefix_tokens: int
    expected_reuse: float
    cache_ttl: float
    write_cost: float
    read_cost: float
    miss_probability: float
    invalidation_probability: float
    expected_net_saving: float

    def to_dict(self) -> dict:
        return {f: getattr(self, f) for f in (
            "use_explicit", "stable_prefix_tokens", "expected_reuse", "cache_ttl",
            "write_cost", "read_cost", "miss_probability", "invalidation_probability",
            "expected_net_saving")}


def stable_hash(*parts: str) -> str:
    """稳定前缀哈希（SHA-256 前 16 位）。"""
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", "replace"))
    return h.hexdigest()[:16]


def canonical_tools(tools: list[dict]) -> list[dict]:
    """工具按稳定标识排序 + 确定性序列化（消除顺序/空格导致的缓存失效）。"""
    def _key(t: dict) -> str:
        fn = t.get("function") or {}
        return fn.get("name") or json_dumps_compact(t)
    return sorted(tools, key=_key)


class PromptCacheManager:
    """本地构造缓存 + 分层版本 + 观测指标（线程安全，仅内存）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._versions = {"system": _SYSTEM_VERSION, "tool": _TOOL_VERSION,
                          "agent": _AGENT_VERSION, "project": _PROJECT_VERSION}
        self._prefix_cache: dict[str, tuple[str, str]] = {}   # 版本签名 -> (文本, hash)
        self._observed: list[dict] = []                        # 观测记录（环形）
        self._churn: list[tuple[float, str]] = []              # (时间, 前缀hash) 变化记录

    # ---- 版本 ----
    def version_of(self, level: str) -> int:
        with self._lock:
            return self._versions.get(level, 1)

    def invalidate(self, level: str) -> None:
        with self._lock:
            if level in self._versions:
                self._versions[level] += 1
            self._prefix_cache.clear()          # 任一层变化 → 前缀重建

    def versions_snapshot(self) -> dict:
        with self._lock:
            return dict(self._versions)

    # ---- 稳定前缀构造（字节级稳定）----
    def build_prefix(self, *, system_parts: list[str], tools: list[dict] | None,
                     agent_parts: list[str], project_parts: list[str],
                     locale: str = "zh-CN", provider_id: str = "generic",
                     model_id: str = "") -> tuple[str, str]:
        """组装稳定前缀（P0+P1+P2）。返回 (文本, hash)。相同输入 → 相同输出。"""
        with self._lock:
            sig = json_dumps_compact({
                "v": self._versions, "locale": locale, "provider": provider_id,
                "model": model_id,
                "sys": [s for s in system_parts if s],
                "tools": canonical_tools(tools) if tools else [],
                "agent": [a for a in agent_parts if a],
                "project": [p for p in project_parts if p],
            })
            cached = self._prefix_cache.get(sig)
            if cached is not None:
                return cached
        # 文本组装（不含时间/ID/密钥）；hash 覆盖完整签名（provider/model/版本隔离）
        text_parts: list[str] = []
        for s in system_parts:
            if s:
                text_parts.append(s)
        if tools:
            text_parts.append("工具说明：" + json_dumps_compact(canonical_tools(tools)))
        for a in agent_parts:
            if a:
                text_parts.append(a)
        for p in project_parts:
            if p:
                text_parts.append(p)
        text = "\n\n".join(text_parts)
        h = stable_hash(sig, text)
        with self._lock:
            self._prefix_cache[sig] = (text, h)
            self._churn.append((time.monotonic(), h))
            if len(self._churn) > 200:
                self._churn = self._churn[-200:]
        return text, h

    # ---- 观测 ----
    def observe(self, *, prefix_hash: str, hit: bool, miss_reason: str = "",
                provider_usage: dict | None = None) -> None:
        """记录一次请求的缓存观测（provider_usage 为供应商真实 usage）。"""
        entry = {
            "ts": time.time(), "prefix_hash": prefix_hash, "hit": hit,
            "miss_reason": miss_reason,
            "cached_input_tokens": 0, "uncached_input_tokens": 0,
        }
        if isinstance(provider_usage, dict):
            entry["cached_input_tokens"] = (
                (provider_usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
            entry["uncached_input_tokens"] = max(0, (
                provider_usage.get("prompt_tokens") or 0) - entry["cached_input_tokens"])
        with self._lock:
            self._observed.append(entry)
            if len(self._observed) > _OBSERVE_MAX:
                self._observed = self._observed[-_OBSERVE_MAX:]

    def metrics(self) -> dict:
        """缓存观测指标（不伪造节省：仅统计供应商真实 cached tokens）。"""
        with self._lock:
            observed = list(self._observed)
            churn = list(self._churn)
        total = len(observed)
        hits = sum(1 for e in observed if e["hit"])
        cached = sum(e["cached_input_tokens"] for e in observed)
        uncached = sum(e["uncached_input_tokens"] for e in observed)
        # prefix churn：哈希变化次数 / 记录数
        distinct = len({h for _, h in churn})
        churn_rate = (len(churn) - distinct) / max(1, len(churn))
        return {
            "requests": total, "prefix_hits": hits, "prefix_miss": total - hits,
            "cached_input_tokens": cached, "uncached_input_tokens": uncached,
            "cache_hit_rate": (hits / total) if total else 0.0,
            "prefix_churn_rate": churn_rate,
            "last_miss_reasons": [e["miss_reason"] for e in observed[-5:] if e["miss_reason"]],
        }

    # ---- 显式缓存决策（规格 §6：net_saving 必须 > 0 才允许）----
    @staticmethod
    def decide_explicit_cache(*, stable_prefix_tokens: int, expected_reuse: float,
                              cache_ttl: float, write_cost_per_token: float,
                              read_cost_per_token: float, uncached_cost_per_token: float,
                              miss_probability: float = 0.1,
                              invalidation_probability: float = 0.2,
                              min_prefix_tokens: int = 2000,
                              min_reuse: float = 2.0) -> CacheDecision:
        """net_saving = 无缓存基线 - 写缓存 - 读缓存 - 失效成本。<=0 或收益过低 → 不用。"""
        reuse_eff = expected_reuse * (1 - miss_probability) * (1 - invalidation_probability)
        uncached = stable_prefix_tokens * expected_reuse * uncached_cost_per_token
        write = stable_prefix_tokens * write_cost_per_token
        read = stable_prefix_tokens * reuse_eff * read_cost_per_token
        invalidation = stable_prefix_tokens * invalidation_probability * uncached_cost_per_token
        net = uncached - write - read - invalidation
        use = (stable_prefix_tokens >= min_prefix_tokens and expected_reuse >= min_reuse
               and net > 0 and cache_ttl > 0)
        return CacheDecision(
            use_explicit=use, stable_prefix_tokens=stable_prefix_tokens,
            expected_reuse=expected_reuse, cache_ttl=cache_ttl,
            write_cost=write, read_cost=read, miss_probability=miss_probability,
            invalidation_probability=invalidation_probability,
            expected_net_saving=net if use else 0.0)

    def clear(self) -> None:
        """清除全部缓存与观测（会话/敏感数据删除时调用）。"""
        with self._lock:
            self._prefix_cache.clear()
            self._observed.clear()
            self._churn.clear()
