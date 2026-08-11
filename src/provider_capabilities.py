"""Provider 能力层：声明/探测不同 OpenAI 兼容服务的能力差异。

不要假设所有兼容服务都支持相同功能。能力来自：
1. 显式 provider 声明（按 api_url 的 host 匹配）；
2. 配置覆盖（chat_config.json 的 "provider_overrides" 段，可覆盖任何字段）；
3. 探测结果（可选：一次性的参数试探，失败即降级声明）。

规则：
- 不支持的参数不得发送（filter_payload 负责移除）。
- 不得因为服务忽略未知参数就假装功能已生效。
- 保持 Chat Completions 兼容路径；不支持缓存/Responses 时自动回退。
- 每次请求记录实际启用的优化能力（debug 日志，不含密钥或用户正文）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from urllib.parse import urlparse

log = logging.getLogger("llm-backend")

# 能力字段清单（新增能力在此登记，filter 与覆盖逻辑自动生效）
_CAPABILITY_FIELDS = (
    "supports_usage",             # 非流式响应带 usage
    "supports_stream_usage",      # 流式响应末块带 usage
    "supports_prompt_cache",      # 原生提示词缓存（cached_tokens）
    "supports_cached_token_details",  # prompt_tokens_details.cached_tokens
    "supports_reasoning_effort",  # reasoning_effort 参数
    "supports_thinking_disabled",  # thinking: {"type": "disabled"}
    "supports_previous_response_id",  # 连续响应复用
    "supports_persisted_reasoning",   # 持久化推理状态
    "supports_image_detail",      # image_url detail 参数（high/low/auto）
    "supports_max_output_tokens",  # max_tokens / max_output_tokens 上限
    "supports_parallel_tool_calls",  # 单轮多个 tool_calls
)

# payload 参数 → 所需能力（filter_payload 依据）
_PAYLOAD_PARAM_CAPS = {
    "reasoning_effort": "supports_reasoning_effort",
    "thinking": "supports_thinking_disabled",
    "previous_response_id": "supports_previous_response_id",
    "max_tokens": "supports_max_output_tokens",
    "max_output_tokens": "supports_max_output_tokens",
    "parallel_tool_calls": "supports_parallel_tool_calls",
}


@dataclass(frozen=True)
class ProviderCapabilities:
    """某 provider 的能力集合。所有字段默认 False = 未知时不发送。"""

    provider_id: str
    hosts: tuple = ()            # 匹配的 host（小写，不含端口）
    supports_usage: bool = False
    supports_stream_usage: bool = False
    supports_prompt_cache: bool = False
    supports_cached_token_details: bool = False
    supports_reasoning_effort: bool = False
    supports_thinking_disabled: bool = False
    supports_previous_response_id: bool = False
    supports_persisted_reasoning: bool = False
    supports_image_detail: bool = False
    supports_max_output_tokens: bool = False
    supports_parallel_tool_calls: bool = False

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return {f: getattr(self, f) for f in _CAPABILITY_FIELDS}

    def enabled_summary(self) -> str:
        """实际启用的能力摘要（日志用，不含敏感信息）。"""
        enabled = [f.replace("supports_", "") for f in _CAPABILITY_FIELDS
                   if getattr(self, f)]
        return ",".join(enabled) or "none"

    # ---- payload 过滤：移除当前 provider 不支持的参数 ----
    def filter_payload(self, payload: dict) -> dict:
        """返回移除不支持参数后的 payload 副本（不修改原对象）。"""
        out = dict(payload)
        for param, cap in _PAYLOAD_PARAM_CAPS.items():
            if param in out and not getattr(self, cap):
                log.debug("provider %s 不支持 %s，已移除", self.provider_id, param)
                out.pop(param, None)
        # 支持 max_tokens 但模型需要输出余量时由调用方决定；这里只做能力过滤
        return out


# ---- 显式 provider 声明（按 host）----
_DECLARATIONS = (
    ProviderCapabilities(
        provider_id="deepseek", hosts=("api.deepseek.com",),
        supports_usage=True, supports_stream_usage=True,
        supports_prompt_cache=True, supports_cached_token_details=True,
        supports_reasoning_effort=True, supports_thinking_disabled=True,
        supports_max_output_tokens=True, supports_parallel_tool_calls=True),
    ProviderCapabilities(
        provider_id="openai", hosts=("api.openai.com", "api.azure.com"),
        supports_usage=True, supports_stream_usage=True,
        supports_prompt_cache=True, supports_cached_token_details=True,
        supports_reasoning_effort=True, supports_thinking_disabled=True,
        supports_image_detail=True, supports_max_output_tokens=True,
        supports_parallel_tool_calls=True),
    ProviderCapabilities(
        provider_id="ollama", hosts=("localhost", "127.0.0.1"),
        supports_usage=True, supports_stream_usage=True,
        supports_max_output_tokens=True),
    ProviderCapabilities(
        provider_id="generic",
        supports_usage=True, supports_stream_usage=True,
        supports_max_output_tokens=True),
)


def _host_of(api_url: str) -> str:
    try:
        return (urlparse(api_url).hostname or "").lower()
    except ValueError:
        return ""


def detect_capabilities(api_url: str, model: str = "",
                        overrides: dict | None = None) -> ProviderCapabilities:
    """按 api_url 解析 provider 能力；配置覆盖字段级结果。

    overrides: chat_config.json 的 "provider_overrides" 段
    （{"deepseek": {"supports_prompt_cache": false}, ...}），
    或按模型名的 "model_overrides" 段。
    """
    host = _host_of(api_url)
    caps = _DECLARATIONS[-1]                     # 默认 generic
    for declared in _DECLARATIONS:
        if host and host in declared.hosts:
            caps = declared
            break
    # 字段级覆盖（provider_id 或 host 均可作为键）
    overrides = overrides or {}
    section = overrides.get(caps.provider_id) or (overrides.get(host) if host else None)
    if isinstance(section, dict):
        kwargs = {k: v for k, v in section.items()
                  if k in _CAPABILITY_FIELDS and isinstance(v, bool)}
        if kwargs:
            caps = replace(caps, **kwargs)
    return caps


def build_payload(api_url: str, model: str, base_payload: dict,
                  overrides: dict | None = None) -> tuple[dict, ProviderCapabilities]:
    """组装并过滤请求 payload：能力过滤 + 记录启用能力（供观测）。

    返回 (过滤后的 payload, 能力对象)。调用方把能力记录到 usage 统计。
    """
    caps = detect_capabilities(api_url, model, overrides)
    payload = caps.filter_payload(base_payload)
    return payload, caps


def load_overrides_from_config(cfg: dict) -> dict | None:
    """从配置读取 provider_overrides（仅能力字段，忽略其他内容）。"""
    ov = cfg.get("provider_overrides")
    if isinstance(ov, dict):
        return ov
    # 兼容旧的 provider 段（仅提取能力字段）
    prov = cfg.get("provider")
    if isinstance(prov, dict):
        return {k: v for k, v in prov.items()
                if isinstance(v, (dict, bool))}
    return None


# ---- 能力矩阵文本（诊断/报告用）----
def capability_matrix_text() -> str:
    rows = ["provider_id," + ",".join(f.replace("supports_", "") for f in _CAPABILITY_FIELDS)]
    for caps in _DECLARATIONS:
        rows.append(caps.provider_id + "," + ",".join(
            "1" if getattr(caps, f) else "0" for f in _CAPABILITY_FIELDS))
    return "\n".join(rows)


def json_dumps_compact(obj) -> str:
    """确定性 JSON 序列化（稳定前缀缓存用）：键排序、固定格式。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)
