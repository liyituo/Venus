"""RAG 服务：独立的文档知识库检索服务（FastAPI，零数据库依赖）。

设计：
- 集合（collection）隔离知识域；文档分块 + 向量索引（numpy 余弦）
- Embedding 来自本地 Ollama（nomic-embed-text）；Ollama 不可达自动降级词法检索
- 数据落盘 data/{collection}.json（原子写），重启不丢
- 暴露 HTTP API，供 PC Agent / 其他客户端接入（见 README「Agent 接入」）

运行：python rag_server.py [--port 8010] [--token xxx]
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from rag_core.chunker import chunk_text
from rag_core.embedder import Embedder
from rag_core.retriever import LexicalIndex
from rag_core.store import Collection, valid_name

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "rag_config.json"
APP_VERSION = "0.1.0"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("rag-server")


def load_config() -> dict:
    default = {
        "ollama_url": "http://127.0.0.1:11434",
        "embed_model": "nomic-embed-text",
        "data_dir": str(BASE_DIR / "data"),
        "port": 8010,
        "token": "",
    }
    if CONFIG_PATH.exists():
        try:
            return {**default, **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}
        except Exception:
            log.warning("rag_config.json 解析失败，使用默认配置")
    return default


cfg = load_config()
app = FastAPI(title="RAG Server", version=APP_VERSION)
_data_dir = Path(cfg["data_dir"])
_data_dir.mkdir(parents=True, exist_ok=True)
embedder = Embedder(ollama_url=cfg.get("ollama_url", ""),
                    model=cfg.get("embed_model", ""))

_lock = threading.Lock()
_collections: dict[str, Collection] = {}


def _get_collection(name: str) -> Collection:
    # 路径参数进来的集合名必须与创建时同规则校验（防路径穿越）
    if not valid_name(name):
        raise HTTPException(400, f"非法集合名：{name!r}（仅允许字母/数字/下划线/连字符）")
    with _lock:
        c = _collections.get(name)
        if c is None:
            c = Collection(name, _data_dir)
            _collections[name] = c
        return c


# ---------- 鉴权 ----------
@app.middleware("http")
async def _auth(request, call_next):
    token = str(cfg.get("token") or "")
    if token:
        if request.headers.get("X-Api-Token") != token:
            return JSONResponse({"detail": "未授权：需要正确的 X-Api-Token"}, status_code=401)
    return await call_next(request)


# ---------- 请求模型 ----------
class CollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")


class DocAdd(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1)
    meta: dict | None = None


class IngestReq(BaseModel):
    """财报/文档摄入：文本直传 或 本地文件路径（txt/md/纯文本）。"""
    title: str = Field(min_length=1, max_length=200)
    text: str | None = None
    file_path: str | None = None
    meta: dict | None = None

    def content(self) -> str:
        """解析请求内容：文本优先；file_path 读本地纯文本文件（限大小）。"""
        if self.text and self.text.strip():
            return self.text
        if self.file_path:
            p = Path(self.file_path).expanduser()
            if not p.is_file():
                raise HTTPException(400, f"文件不存在：{self.file_path}")
            if p.stat().st_size > 5_000_000:
                raise HTTPException(400, "文件过大（≤5MB）")
            try:
                return p.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise HTTPException(400, f"读取文件失败：{exc}") from exc
        raise HTTPException(422, "需要 text 或 file_path 之一")


class SearchReq(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)
    symbol: str | None = Field(default=None, description="按股票代码过滤（财报检索）")


# ---------- 端点 ----------
@app.get("/api/v1/health")
async def health() -> dict:
    return {"ok": True, "version": APP_VERSION,
            "embedding": embedder.available(),
            "embed_model": embedder.model,
            "embedding_error": embedder.error() or "",
            "collections": len(_collections)}


@app.post("/api/v1/collections")
async def create_collection(req: CollectionCreate) -> dict:
    c = _get_collection(req.name)
    return {"ok": True, "collection": c.name, "stats": c.stats()}


@app.get("/api/v1/collections")
async def list_collections() -> dict:
    names = sorted(p.stem for p in _data_dir.glob("*.json"))
    out = []
    for n in names:
        c = _get_collection(n)
        out.append(c.stats())
    return {"ok": True, "collections": out}


@app.delete("/api/v1/collections/{name}")
async def delete_collection(name: str) -> dict:
    c = _get_collection(name)
    c.delete()
    with _lock:
        _collections.pop(name, None)
    return {"ok": True, "deleted": name}


@app.post("/api/v1/collections/{name}/documents")
async def add_document(name: str, req: DocAdd) -> dict:
    c = _get_collection(name)
    try:
        if embedder.available():
            chunks = chunk_text(req.text)
            vectors = embedder.embed(chunks)
        else:
            chunks, vectors = None, None
        doc = c.add_document(req.title, req.text, req.meta, vectors, chunks)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    log.info("collection %s +doc %s（%d 块）", name, req.title, doc["chunks"])
    return {"ok": True, **doc, "embedding": vectors is not None}


@app.post("/api/v1/collections/{name}/ingest")
async def ingest(name: str, req: IngestReq) -> dict:
    """财报/文档摄入：文本直传或本地 txt/md 文件；meta 存 symbol/report_date。"""
    c = _get_collection(name)
    content = req.content()
    if not content.strip():
        raise HTTPException(422, "摄入内容为空")
    meta = dict(req.meta or {})
    # symbol 归一化（美股/A股代码统一大写；含市场后缀时保留）
    if meta.get("symbol"):
        meta["symbol"] = str(meta["symbol"]).strip().upper()
    try:
        if embedder.available():
            chunks = chunk_text(content)
            vectors = embedder.embed(chunks)
        else:
            chunks, vectors = None, None
        doc = c.add_document(req.title, content, meta, vectors, chunks)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    log.info("ingest %s +%s（%d 块，meta=%s）", name, req.title, doc["chunks"],
             {k: v for k, v in meta.items() if k in ("symbol", "report_date", "market")})
    return {"ok": True, **doc, "embedding": vectors is not None}


@app.get("/api/v1/collections/{name}/documents")
async def list_documents(name: str) -> dict:
    c = _get_collection(name)
    return {"ok": True, "documents": c.list_documents()}


@app.delete("/api/v1/collections/{name}/documents/{doc_id}")
async def delete_document(name: str, doc_id: str) -> dict:
    c = _get_collection(name)
    if not c.delete_document(doc_id):
        raise HTTPException(404, f"文档不存在：{doc_id}")
    return {"ok": True, "deleted": doc_id}


@app.get("/api/v1/collections/{name}/status")
async def collection_status(name: str) -> dict:
    c = _get_collection(name)
    return {"ok": True, **c.stats()}


@app.post("/api/v1/collections/{name}/search")
async def search(name: str, req: SearchReq) -> dict:
    c = _get_collection(name)
    hits = _search(c, req.query, req.top_k)
    if req.symbol:
        sym = req.symbol.strip().upper()
        hits = [h for h in hits
                if str(((c.docs.get(h["doc_id"]) or {}).get("meta") or {}).get("symbol") or "").strip().upper() == sym]
    return {"ok": True, "query": req.query, "symbol": req.symbol,
            "embedding": embedder.available(), "hits": hits}


def _search(c: Collection, query: str, top_k: int) -> list[dict]:
    """向量优先，词法兜底。"""
    if embedder.available():
        qvec = embedder.embed([query])
        if qvec:
            hits = c.search_vector(qvec[0], top_k)
            if hits:
                return _attach_meta(c, hits, "vector")
    # 词法兜底（embedding 不可用或向量无结果）
    lex = LexicalIndex([{"doc_id": ch["doc_id"], "text": ch["text"]}
                        for ch in c.chunks])
    hits = lex.search(query, top_k)
    return _attach_meta(c, hits, "lexical")


def _attach_meta(c: Collection, hits: list[dict], method: str) -> list[dict]:
    out = []
    for h in hits:
        doc = c.docs.get(h["doc_id"]) or {}
        meta = doc.get("meta") or {}
        out.append({"doc_id": h["doc_id"], "title": doc.get("title", ""),
                    "text": h["text"], "score": round(h["score"], 4),
                    "method": method,
                    "meta": {k: meta.get(k) for k in ("symbol", "report_date", "market")
                             if meta.get(k)}})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(cfg.get("port") or 8010))
    ap.add_argument("--token", default=str(cfg.get("token") or ""),
                    help="启用鉴权：客户端需携带 X-Api-Token")
    args = ap.parse_args()
    if args.token:
        cfg["token"] = args.token
    log.info("RAG server v%s 启动（%s:%s，embedding=%s）",
             APP_VERSION, args.host, args.port, embedder.model)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
