"""集合存储：文档 + 分块 + 向量（numpy 余弦检索，零外部向量库）。

小规模（≤ 数万块）足够；数据量上来后可替换为 FAISS/pgvector（README 扩展点）。
持久化：data/{collection}.json（文档/块/向量全部落盘，原子写）。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

import numpy as np

from rag_core.chunker import chunk_text

# 集合名白名单：字母/数字/下划线/连字符（防路径穿越）
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.fullmatch(name or ""))


def _atomic_write(path: Path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


class Collection:
    """一个集合 = 文档集 + 分块向量索引。线程安全。"""

    def __init__(self, name: str, data_dir: Path):
        if not valid_name(name):
            raise ValueError(
                f"非法集合名：{name!r}（仅允许字母/数字/下划线/连字符）")
        self.name = name
        self.path = data_dir / f"{name}.json"
        self._lock = threading.Lock()
        self.docs: dict[str, dict] = {}          # doc_id -> {title, meta, ts}
        self.chunks: list[dict] = []             # {doc_id, text, vec, seq}
        self._matrix: np.ndarray | None = None   # 惰性重建的向量矩阵
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.docs = data.get("docs", {})
            self.chunks = data.get("chunks", [])
            self._matrix = None          # 向量矩阵惰性重建
        except (OSError, json.JSONDecodeError):
            # 损坏文件不静默清空：改名 .corrupt 保留现场，避免误删数据
            try:
                corrupt = self.path.with_suffix(".corrupt")
                self.path.replace(corrupt)
            except OSError:
                pass
            self.docs, self.chunks = {}, []

    def save(self) -> None:
        with self._lock:
            _atomic_write(self.path, {"docs": self.docs, "chunks": self.chunks})

    def delete(self) -> None:
        with self._lock:
            self.docs.clear()
            self.chunks.clear()
            self._matrix = None
        try:
            self.path.unlink()
        except OSError:
            pass

    # ---- 文档 ----
    def add_document(self, title: str, text: str, meta: dict | None,
                     vectors: list[list[float]] | None,
                     chunks: list[str] | None = None) -> dict:
        """分块并入库。vectors 为 None 表示无向量（词法模式）。
        chunks 可传入预计算块（服务端 embedding 时已分块，避免重复计算）。"""
        doc_id = uuid.uuid4().hex[:12]
        with self._lock:
            self.docs[doc_id] = {"title": title, "meta": meta or {},
                                 "ts": time.time()}
            chunks = chunks if chunks is not None else chunk_text(text)
            if vectors is None:
                new_chunks = [{"doc_id": doc_id, "text": c, "vec": None,
                               "seq": i} for i, c in enumerate(chunks)]
            else:
                if len(vectors) != len(chunks):
                    raise ValueError(
                        f"向量数 {len(vectors)} 与块数 {len(chunks)} 不匹配")
                new_chunks = [{"doc_id": doc_id, "text": c, "vec": list(v),
                               "seq": i} for i, (c, v) in enumerate(zip(chunks, vectors))]
            self.chunks.extend(new_chunks)
            self._matrix = None          # 向量矩阵失效，惰性重建
        self.save()
        return {"id": doc_id, "chunks": len(chunks)}

    def delete_document(self, doc_id: str) -> bool:
        with self._lock:
            if doc_id not in self.docs:
                return False
            del self.docs[doc_id]
            self.chunks = [c for c in self.chunks if c["doc_id"] != doc_id]
            self._matrix = None
        self.save()
        return True

    def list_documents(self) -> list[dict]:
        with self._lock:
            return [{"id": did, "title": d["title"], "chunks": sum(
                1 for c in self.chunks if c["doc_id"] == did),
                "ts": d["ts"]} for did, d in self.docs.items()]

    def stats(self) -> dict:
        with self._lock:
            vec_dims = len(self.chunks[0]["vec"]) if (
                self.chunks and self.chunks[0].get("vec")) else 0
            return {"collection": self.name, "documents": len(self.docs),
                    "chunks": len(self.chunks), "vector_dim": vec_dims,
                    "indexed": bool(vec_dims)}

    # ---- 向量检索 ----
    def _matrix_or_rebuild(self) -> np.ndarray | None:
        if self._matrix is not None:
            return self._matrix
        vecs = [c["vec"] for c in self.chunks if c.get("vec")]
        if not vecs:
            self._matrix = None
            return None
        self._matrix = np.asarray(vecs, dtype=np.float32)
        return self._matrix

    def search_vector(self, query_vec: list[float], top_k: int) -> list[dict]:
        """余弦相似度检索；返回 [{seq, doc_id, text, score}]。"""
        mat = self._matrix_or_rebuild()
        if mat is None or mat.shape[0] == 0:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        qn = np.linalg.norm(q)
        if qn == 0:
            return []
        norms = np.linalg.norm(mat, axis=1)
        denom = norms * qn
        with np.errstate(divide="ignore", invalid="ignore"):
            scores = (mat @ q) / denom
            scores = np.nan_to_num(scores, nan=0.0)
        order = np.argsort(-scores)[:top_k]
        return [{"seq": int(i), "doc_id": self.chunks[i]["doc_id"],
                 "text": self.chunks[i]["text"], "score": float(scores[i])}
                for i in order if scores[i] > 0]

    def chunks_of(self, doc_id: str) -> list[str]:
        with self._lock:
            return [c["text"] for c in self.chunks if c["doc_id"] == doc_id]
