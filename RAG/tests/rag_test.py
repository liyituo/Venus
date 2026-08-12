"""RAG 核心与 API 测试：分块 / 存储 / 检索 / 接口（向量用确定性 mock，不依赖 Ollama）。

真实 Ollama 端到端：python tests/rag_test.py --live
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ap = argparse.ArgumentParser()
ap.add_argument("--live", action="store_true")
args = ap.parse_args()

import numpy as np  # noqa: E402
from rag_core import chunker, embedder as emb_mod  # noqa: E402
from rag_core.retriever import LexicalIndex, tokenize  # noqa: E402
from rag_core.store import Collection  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


# 确定性 mock 向量：字符袋模型（相同字符 → 相似向量，能表达词面相关性）
def mock_vec(text: str) -> list[float]:
    v = np.zeros(256, dtype=np.float32)
    for ch in text:
        v[ord(ch) % 256] += 1
    norm = np.linalg.norm(v)
    return (v / norm).tolist() if norm else v.tolist()


# ============ 1. 分块 ============
print("== 1. chunker ==")
c = chunker.chunk_text(
    "第一段落内容完整，这里足够长不会被合并，确实已经超过最短块长度了，还要继续加长到六十个字符以上。\n\n"
    "第二段落内容完整，这里也足够长不会被合并，确实已经超过最短块长度了，还要继续加长到六十个字符以上。",
    chunk_size=100)
check("段落成块", len(c) >= 2 and "第一段落" in c[0] and "第二段落" in c[-1], str(c))
long_text = "这是一个很长的句子。" * 100
c2 = chunker.chunk_text(long_text, chunk_size=200, overlap=20)
check("超长文本切成多块", len(c2) >= 3, str(len(c2)))
check("块大小不超上限+余量", all(len(x) <= 250 for x in c2), str([len(x) for x in c2]))
check("内容不丢（拼接后包含原文）", "很长的句子" in "".join(c2))
c3 = chunker.chunk_text("   \n  ", chunk_size=100)
check("空白文本返回空", c3 == [], str(c3))
c4 = chunker.chunk_text("单段内容" * 200, chunk_size=300)
check("无空行文本也可切", len(c4) >= 3, str(len(c4)))

# ============ 2. 词法检索 ============
print("== 2. retriever（词法）==")
chunks = [
    {"doc_id": "a", "text": "FastAPI 框架用于构建 RAG 服务的 API 接口"},
    {"doc_id": "b", "text": "Ollama 提供本地 embedding 模型 nomic-embed-text"},
    {"doc_id": "c", "text": "numpy 实现余弦相似度向量检索"},
]
lex = LexicalIndex(chunks)
hits = lex.search("FastAPI API", 2)
check("词法命中相关块", hits and hits[0]["doc_id"] == "a", str(hits))
hits2 = lex.search("embedding 模型", 2)
check("中文词法命中", hits2 and hits2[0]["doc_id"] == "b", str(hits2))
check("无关查询无结果", lex.search("zzzzqqqq", 2) == [])
check("tokenize 中英混合", "fastapi" in tokenize("FastAPI 接口") and "接口" in tokenize("FastAPI 接口"))

# ============ 3. 存储 ============
print("== 3. store ==")
tmp = Path(tempfile.mkdtemp(prefix="rag_test_"))
col = Collection("kb1", tmp)
long_body = "第一段内容。" + "中间段落。" * 150 + "结尾段落。"
doc = col.add_document("测试文档", long_body,
                       {"src": "test"}, None)
check("添加文档返回 id 与块数", doc["id"] and doc["chunks"] >= 2, str(doc))
check("文档列表", len(col.list_documents()) == 1)
check("chunks 可查", len(col.chunks_of(doc["id"])) == doc["chunks"])
# 持久化
col2 = Collection("kb1", tmp)
check("重启后文档恢复", len(col2.list_documents()) == 1, str(col2.list_documents()))
# 删除
check("删除文档", col2.delete_document(doc["id"]) and col2.list_documents() == [])
check("删除不存在返回 False", not col2.delete_document("nope"))
col2.delete()
check("集合删除后文件消失", not col2.path.exists())

# ============ 4. 向量检索 ============
print("== 4. 向量检索 ==")
vcol = Collection("vec", tmp)
d1_text = "如何部署 FastAPI 服务。" * 40 + "uvicorn 是 ASGI 服务器。"
d1_chunks = chunker.chunk_text(d1_text)
r = vcol.add_document("d1", d1_text, None, [mock_vec(c) for c in d1_chunks])
d1_id = r["id"]
check("向量入库", r["chunks"] == len(d1_chunks) and vcol.stats()["indexed"], str(r))
d2_text = "股票行情分析。"
r2 = vcol.add_document("d2", d2_text, None, [mock_vec(d2_text)])
check("第二文档入库", r2["chunks"] == 1)
q = mock_vec("FastAPI 服务部署 uvicorn")
hits = vcol.search_vector(q, 2)
check("向量检索相关优先", hits and hits[0]["doc_id"] == d1_id, str(hits)[:120])
check("无关文档不命中", all(h["doc_id"] != r2["id"] for h in hits), str(hits)[:120])

# ============ 5. API ============
print("== 5. API ==")
import rag_server as RS  # noqa: E402
RS._data_dir = tmp
RS._collections.clear()
RS.embedder = emb_mod.Embedder(ollama_url="http://127.0.0.1:9", model="x")  # 不可达 → 词法模式
c = TestClient(RS.app, base_url="http://127.0.0.1:8010")
r = c.get("/api/v1/health")
check("health 200", r.status_code == 200 and r.json()["embedding"] is False, r.text[:100])
r = c.post("/api/v1/collections", json={"name": "demo"})
check("创建集合", r.status_code == 200, r.text[:100])
r = c.post("/api/v1/collections/demo/documents",
           json={"title": "部署指南", "text": "FastAPI 部署需要 uvicorn。配置 proxy 后启动。"})
check("添加文档（词法模式）", r.status_code == 200 and r.json()["embedding"] is False, r.text[:150])
r = c.post("/api/v1/collections/demo/search", json={"query": "怎么部署 FastAPI", "top_k": 3})
check("搜索返回", r.status_code == 200 and r.json()["hits"], r.text[:200])
check("搜索用词法模式", r.json()["hits"][0]["method"] == "lexical", r.text[:150])
r = c.get("/api/v1/collections/demo/status")
check("状态含统计", r.status_code == 200 and r.json()["chunks"] >= 1, r.text[:100])
r = c.delete("/api/v1/collections/demo/documents/nope")
check("删除不存在 404", r.status_code == 404)
r = c.post("/api/v1/collections", json={"name": "bad name!"})
check("非法集合名 422", r.status_code == 422)
r = c.post("/api/v1/collections/demo/documents", json={"title": "", "text": "x"})
check("空标题 422", r.status_code == 422)
# token 鉴权
RS.cfg = dict(RS.cfg, token="rag-secret")
r = c.get("/api/v1/health")
check("token 拒绝无头请求", r.status_code == 401)
r = c.get("/api/v1/health", headers={"X-Api-Token": "rag-secret"})
check("token 放行", r.status_code == 200)
RS.cfg = dict(RS.cfg, token="")

# ============ 6. 真实 Ollama 端到端（--live）============
print("== 6. 真实 Ollama 端到端 ==")
if args.live:
    emb = emb_mod.Embedder()
    if emb.available():
        check("Ollama embedding 可用", True)
        vecs = emb.embed(["测试", "检索", "fastapi"])
        check("嵌入维度正确", vecs and len(vecs) == 3 and len(vecs[0]) > 0,
              str(len(vecs[0]) if vecs else 0))
        cache_hit = emb.embed(["测试"])
        check("嵌入缓存命中", cache_hit is not None)
        # 端到端：真实集合 + 检索
        RS.embedder = emb
        r = c.post("/api/v1/collections/real", json={"name": "real"})
        r = c.post("/api/v1/collections/real/documents",
                   json={"title": "RAG 介绍", "text": "RAG 检索增强生成，把文档知识注入大模型回答。"})
        check("真实文档入库（向量模式）", r.status_code == 200 and r.json()["embedding"], r.text[:150])
        r = c.post("/api/v1/collections/real/search", json={"query": "检索增强生成是什么", "top_k": 2})
        check("真实向量检索命中", r.status_code == 200 and r.json()["hits"]
              and r.json()["hits"][0]["method"] == "vector", r.text[:200])
    else:
        print("  SKIP  live：Ollama embedding 不可用（", emb.error(), "）")
else:
    print("  SKIP  live（加 --live 跑真实 Ollama）")

# ============ 7. 深度 debug 边界（安全/健壮性）============
print("== 7. 边界与安全 ==")
from rag_core.store import valid_name  # noqa: E402
check("合法集合名", valid_name("kb_demo") and valid_name("A_1-b"))
check("非法集合名拒绝", not valid_name("../escape") and not valid_name("a/b")
      and not valid_name("a b") and not valid_name(""))
try:
    Collection("../../escape", tmp)
    check("store 构造拒绝穿越名", False)
except ValueError:
    check("store 构造拒绝穿越名", True)
r = c.post("/api/v1/collections/..%2F..%2Fescape", json={"name": "x"})
check("API 创建拒绝非法名", r.status_code in (400, 404, 422), str(r.status_code))
r = c.delete("/api/v1/collections/..%2F..%2Fescape")
check("API 删除拒绝非法名", r.status_code in (400, 404), str(r.status_code))
check("逃逸文件未被创建", not (tmp.parent / "escape.json").exists())

# 损坏文件恢复：不静默清空，改名 .corrupt 保留现场
bad = tmp / "broken.json"
bad.write_text("{not valid json", encoding="utf-8")
bc = Collection("broken", tmp)
check("损坏文件备份为 .corrupt", (tmp / "broken.corrupt").exists())
check("损坏后集合为空但不崩", bc.stats()["documents"] == 0)

# chunker：overlap >= chunk_size 不死循环
c5 = chunker.chunk_text("很长的句子" * 500, chunk_size=100, overlap=200)
check("overlap>=size 不死循环", len(c5) >= 1 and len(c5) < 100, str(len(c5)))

# embedder：上游返回数量不匹配 → 拒绝整批
class _FakeResp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b'{"embeddings": [[0.1]]}'
class _FakeURL:
    def __init__(self, u): self._u = u
    def open(self, req, timeout=0): return _FakeResp()
orig_urlopen = emb_mod.urllib.request.urlopen
emb_mod.urllib.request.urlopen = _FakeURL(None).open
try:
    e2 = emb_mod.Embedder(ollama_url="http://x", model="m")
    out = e2.embed(["a", "b", "c"])
    check("数量不匹配拒绝整批", out is None and "不匹配" in e2.error(), e2.error())
finally:
    emb_mod.urllib.request.urlopen = orig_urlopen

# 空集合搜索不崩
ec = Collection("empty_kb", tmp)
hits = ec.search_vector([0.1] * 8, 3)
check("空集合向量搜索返回空", hits == [])
lex2 = LexicalIndex([])
check("空集合词法搜索返回空", lex2.search("x", 2) == [])

# 并发添加文档（线程安全）
import threading as _th  # noqa: E402
cc = Collection("conc", tmp)
errs = []
def _adder(i):
    try:
        cc.add_document(f"d{i}", f"并发文档 {i} 的内容。" * 30, None, None)
    except Exception as exc:
        errs.append(str(exc))
ts = [_th.Thread(target=_adder, args=(i,)) for i in range(8)]
[t.start() for t in ts]
[t.join() for t in ts]
check("并发添加无异常", not errs, str(errs)[:150])
check("并发后文档齐全", len(cc.list_documents()) == 8, str(len(cc.list_documents())))
check("并发后持久化一致", Collection("conc", tmp).stats()["documents"] == 8)

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
