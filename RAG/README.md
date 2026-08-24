# RAG Server — 本地文档知识库检索服务

独立的 RAG（检索增强生成）服务：把文档分块 + 向量化，暴露 HTTP API 供查询。
**Venus 可通过 `rag_search` 工具接入**（见文末「Agent 接入」）。

## 架构

```
┌─────────────┐    HTTP     ┌──────────────────────────────┐
│  Venus /    │ ──────────► │  RAG Server (127.0.0.1:8010) │
│  其他客户端  │             │  集合 → 文档 → 分块 → 向量索引  │
└─────────────┘             │  检索：向量优先 / 词法兜底      │
                            └──────────────┬───────────────┘
                                           │ /api/embed
                                    ┌──────▼──────┐
                                    │ Ollama       │
                                    │ nomic-embed- │
                                    │ text (768维) │
                                    └─────────────┘
```

- **Embedding**：本地 Ollama（`nomic-embed-text`，768 维）；Ollama 不可达时**自动降级词法检索**（BM25 风格），框架始终可用
- **向量检索**：numpy 余弦相似度（零外部向量库）；数据量上来后可换 FAISS/pgvector（`store.py` 的 `search_vector` 是唯一入口）
- **存储**：`data/{collection}.json` 原子落盘，重启不丢
- **分块**：中文友好（段落优先 + 定长重叠，`rag_core/chunker.py`）

## 快速开始

```bash
# 1. 安装依赖（Python 3.13）
pip install -r requirements.txt

# 2. 拉取 embedding 模型（首次）
ollama pull nomic-embed-text

# 3. 启动服务
python rag_server.py                      # 默认 127.0.0.1:8010
python rag_server.py --port 8010 --token xxx   # 启用鉴权（客户端需 X-Api-Token）
```

配置：复制 `rag_config.example.json` 为 `rag_config.json`（ollama 地址 / 模型 / 数据目录 / 端口 / token）。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 健康检查（embedding 可用性/模型/错误） |
| POST | `/api/v1/collections` | 创建集合 `{"name": "kb_demo"}`（名称限 `[A-Za-z0-9_-]`） |
| GET | `/api/v1/collections` | 集合列表（含文档/块/向量维度统计） |
| DELETE | `/api/v1/collections/{name}` | 删除集合（连同数据文件） |
| POST | `/api/v1/collections/{name}/documents` | 添加文档 `{"title","text","meta"?}` → `{id, chunks, embedding}` |
| GET | `/api/v1/collections/{name}/documents` | 文档列表 |
| DELETE | `/api/v1/collections/{name}/documents/{doc_id}` | 删除文档（同步移除其全部块） |
| GET | `/api/v1/collections/{name}/status` | 集合统计（文档/块/向量维度） |
| POST | `/api/v1/collections/{name}/search` | 检索 `{"query","top_k"?}` → hits（doc_id/title/text/score/method） |

### 检索响应示例

```json
{
  "ok": true, "query": "agent 能控制电脑吗",
  "embedding": true,            // false = 词法兜底模式
  "hits": [{
    "doc_id": "07a1519349e8", "title": "Venus 介绍",
    "text": "Venus 是把 LLM 接到电脑的桌面智能体…",
    "score": 0.6824, "method": "vector"   // vector | lexical
  }]
}
```

### 快速试用

```bash
curl -X POST http://127.0.0.1:8010/api/v1/collections -H "Content-Type: application/json" -d '{"name":"kb_demo"}'
curl -X POST http://127.0.0.1:8010/api/v1/collections/kb_demo/documents -H "Content-Type: application/json" \
  -d '{"title":"项目说明","text":"……文档内容……"}'
curl -X POST http://127.0.0.1:8010/api/v1/collections/kb_demo/search -H "Content-Type: application/json" \
  -d '{"query":"……问题……","top_k":5}'
```

## 测试

```bash
python tests/rag_test.py          # 32 断言：分块/存储/词法/向量/API（不依赖 Ollama）
python tests/rag_test.py --live   # + 真实 Ollama 端到端（37 断言）
```

## 目录结构

```
RAG/
├── rag_server.py        FastAPI 服务入口
├── rag_core/
│   ├── chunker.py       文本分块（段落优先 + 定长重叠）
│   ├── embedder.py      Ollama embedding（批量 + 缓存 + 降级探测）
│   ├── store.py         集合存储（文档/块/向量，原子落盘）
│   └── retriever.py     检索（向量优先 / BM25 词法兜底）
├── rag_config.example.json
├── requirements.txt
├── data/                集合数据（自动创建，勿入库）
└── tests/rag_test.py
```

## Agent 接入

Venus 侧新增一个 `rag_search` 工具即可接入（llm_server.py 的 `_execute_tool` 加分支）：

```python
# 工具实现（示意）：检索 → 把 top_k 片段注入上下文
if name == "rag_search":
    args = json.loads(arguments or "{}")
    # POST http://127.0.0.1:8010/api/v1/collections/{collection}/search
    # body: {"query": args["query"], "top_k": args.get("top_k", 5)}
    # 返回：命中的 title + text 片段（含 score 与来源），供模型引用
```

设计要点：
- RAG 服务与 Venus 完全解耦：只通过 HTTP 通信，Agent 挂了 RAG 不受影响
- 集合名即知识域（如 `project-docs`、`daily-notes`），Agent 按任务选集合
- 检索结果以 `method` 字段标记向量/词法，Agent 可感知精度差异
- 鉴权可选（`--token`），Agent 请求时带 `X-Api-Token`

## 扩展点（当前版本边界）

- 向量检索为 numpy 内存实现，单集合数万块内流畅；更大规模换 FAISS（`store.search_vector` 是唯一入口）
- embedding provider 目前只有 Ollama；`Embedder` 类加一个子类即可支持 OpenAI/本地模型
- 文档上传当前为纯文本；文件解析（pdf/docx/md）后续按需加 `ingest` 端点
