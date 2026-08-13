"""RagClient：调 RAG 服务（127.0.0.1:8010）检索财报等文档。

RAG 不可达 → 返回空列表（策略层降级为「仅行情 prompt」，明确标注），
绝不因 RAG 故障阻塞信号生成。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class RagClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8010",
        collection: str = "financial-reports",
        timeout: float = 8,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.collection = collection
        self.timeout = timeout
        self.last_error = ""

    def search(self, query: str, *, top_k: int = 5, symbol: str | None = None) -> list[dict]:
        """检索返回 [{doc_id,title,text,score,meta}]；不可达返回 []。"""
        payload = {"query": query, "top_k": top_k}
        if symbol:
            payload["symbol"] = symbol
        req = urllib.request.Request(
            f"{self.base_url}/api/v1/collections/{self.collection}/search",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self.last_error = ""
            return data.get("hits") or []
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            self.last_error = f"RAG 不可达：{type(exc).__name__}"
            return []

    def available(self) -> bool:
        return not self.last_error
