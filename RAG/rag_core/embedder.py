"""Embedding 提供层：Ollama HTTP 接口（可扩展其他 provider）。

- POST {ollama_url}/api/embed {"model": ..., "input": [texts...]} → embeddings
- 文本 hash 缓存（内存），相同文本不重复请求
- 批量请求；Ollama 不可达时 available() 返回 False，调用方降级词法检索
"""
from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
_BATCH = 32            # 单次请求最大文本数
_TIMEOUT = 60          # 嵌入请求超时（模型冷加载可能较慢）


class Embedder:
    """Embedding 客户端（线程安全，带内存缓存）。"""

    def __init__(self, ollama_url: str = DEFAULT_OLLAMA_URL,
                 model: str = DEFAULT_EMBED_MODEL, timeout: float = _TIMEOUT):
        self.url = (ollama_url or DEFAULT_OLLAMA_URL).rstrip("/")
        self.model = model or DEFAULT_EMBED_MODEL
        self.timeout = timeout
        self._lock = threading.Lock()
        self._cache: dict[str, list[float]] = {}
        self._err: str = ""

    # ---- 可用性 ----
    def available(self) -> bool:
        """探测 Ollama 是否可达且模型已安装（结果缓存 30s）。"""
        with self._lock:
            last = getattr(self, "_avail_ts", 0.0)
            if self._err and __import__("time").time() - last < 30:
                return False
        try:
            req = urllib.request.Request(self.url + "/api/tags")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("name", "") for m in data.get("models", [])]
            ok = any(m == self.model or m.startswith(self.model + ":")
                     for m in models)
            with self._lock:
                self._err = "" if ok else f"模型 {self.model} 未安装"
                self._avail_ts = __import__("time").time()
            return ok
        except Exception as exc:
            with self._lock:
                self._err = f"Ollama 不可达：{exc}"
                self._avail_ts = __import__("time").time()
            return False

    def error(self) -> str:
        with self._lock:
            return self._err

    # ---- 嵌入 ----
    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """批量嵌入；失败返回 None（调用方降级）。"""
        if not texts:
            return []
        with self._lock:
            cached = [self._cache.get(_hash(t)) for t in texts]
        # 全部命中缓存
        if all(c is not None for c in cached):
            return [list(c) for c in cached]
        todo = [(i, t) for i, t in enumerate(texts)
                if self._cache.get(_hash(t)) is None]
        results: list[list[float] | None] = [None] * len(texts)
        for i, t in todo:
            hit = self._cache.get(_hash(t))
            if hit is not None:
                results[i] = hit
        # 分批请求
        for start in range(0, len(todo), _BATCH):
            batch = todo[start:start + _BATCH]
            vecs = self._request([t for _, t in batch])
            if vecs is None:
                return None
            for (i, t), v in zip(batch, vecs):
                results[i] = v
                with self._lock:
                    self._cache[_hash(t)] = v
        return [list(v) for v in results]

    def _request(self, texts: list[str]) -> list[list[float]] | None:
        payload = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        req = urllib.request.Request(self.url + "/api/embed", data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            embs = data.get("embeddings") or []
            vecs = [list(map(float, e)) for e in embs]
            # 长度不匹配（上游部分失败/截断）：拒绝整批，避免静默错位
            if len(vecs) != len(texts):
                with self._lock:
                    self._err = (f"嵌入返回数量不匹配（请求 {len(texts)}，"
                                 f"收到 {len(vecs)}）")
                return None
            return vecs
        except Exception as exc:
            with self._lock:
                self._err = f"嵌入请求失败：{exc}"
            return None

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
