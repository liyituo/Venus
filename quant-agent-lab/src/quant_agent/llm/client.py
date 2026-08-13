"""LlmClient：OpenAI 兼容接口的最小标准库客户端（零新依赖）。

- key 从环境变量读取（配置 api_key_env 指定），绝不进配置文件/日志/审计
- 非流式 JSON 调用；失败抛 LlmUnavailable（策略层降级 HOLD）
- 输出解析容错（markdown 围栏剥离）由策略层负责
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass


class LlmUnavailable(Exception):
    """LLM 不可用（网络/认证/上游错误），策略层据此降级为 HOLD。"""


@dataclass(frozen=True)
class LlmConfig:
    api_url: str = ""            # 空 = 未配置（策略层直接降级）
    model: str = ""
    api_key_env: str = "QUANT_AGENT_LLM_API_KEY"
    timeout: int = 60

    @property
    def enabled(self) -> bool:
        return bool(self.api_url and self.model)


class LlmClient:
    def __init__(self, config: LlmConfig):
        self.config = config
        self._key = (os.environ.get(config.api_key_env) or "").strip()

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        return headers

    def complete(self, system: str, user: str, *, max_tokens: int = 800,
                 temperature: float = 0.2) -> str:
        """非流式调用，返回 content 文本；失败抛 LlmUnavailable（不含 key 信息）。"""
        if not self.config.enabled:
            raise LlmUnavailable("LLM 未配置（api_url/model 为空）")
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        req = urllib.request.Request(
            self.config.api_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 不读取/不回传 body（可能含敏感信息），只留状态码
            raise LlmUnavailable(f"上游 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise LlmUnavailable(f"LLM 调用失败：{type(exc).__name__}") from exc
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return str(content).strip()

    def has_key(self) -> bool:
        return bool(self._key)
