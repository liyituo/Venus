"""
LLM API 后端 — 把 Chat 前端的聊天请求转发到任意 OpenAI 兼容接口

兼容性设计
----------
- URL 智能归一化：接受 base_url / 带 /v1 / 完整 /chat/completions 任意填法
  （DeepSeek: https://api.deepseek.com ；OpenAI: https://api.openai.com/v1 ；
   Ollama: http://localhost:11434/v1 ；均可直接填）
- 上游错误透出：把真实 API 的错误信息（如 "Model Not Exist"）展示给用户，
  而不是只给 HTTP 状态码
- reasoning_content 兼容：DeepSeek reasoner 等模型可能 content 为空，
  给出明确提示而不是返回空回复
- 实时读取 chat_config.json（API URL / Key / Model），Settings 保存后立即生效
- 网络调用放在线程池执行，不阻塞 FastAPI 事件循环
- 由 chat.py 自动拉起（默认端口 8001），也可手动运行

运行：python llm_server.py [--host 127.0.0.1] [--port 8001]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "chat_config.json"
UPSTREAM_TIMEOUT = 180  # 模型生成可能较慢（reasoner 更慢）
DAEMON_BASE = "http://127.0.0.1:8000"   # 屏幕控制 daemon（app.py）

# ---- Agent 安全锁（防循环调用导致系统崩溃）----
MAX_TOOL_STEPS = 10             # 单次请求最多工具轮数
MAX_TOOL_CALLS_TOTAL = 30       # 单次请求工具调用总数上限（一轮可含多个调用）
MAX_CONSECUTIVE_FAILURES = 4    # 连续失败熔断阈值：达到即停止整个任务
MAX_AGENT_SECONDS = 240         # 单次 agent 请求总耗时上限（含上游生成时间）
MAX_TEXT_LENGTH = 5000          # type_text 文本长度上限
_agent_lock = threading.Lock()  # 并发互斥：同一时刻只允许一个 agent 循环

# 隔离模式（--isolated）：禁用屏幕操作工具，只保留文件类工具。
# WSL 隔离测试环境使用，代码层面保证 agent 无法操作任何屏幕。
_SCREEN_TOOLS = {"get_screen_size", "click", "type_text", "press_key"}
_FILE_TOOLS = {"create_folder", "list_folder", "create_file", "read_file", "run_code"}
ISOLATED = False

# ---- 文件工具安全限制 ----
MAX_FILE_SIZE = 200_000      # read_file 单文件上限（字节）
MAX_FILE_CHARS = 4000        # read_file 返回字符数上限（防爆 token）
MAX_WRITE_CHARS = 100_000    # create_file 内容上限
MAX_LIST_ENTRIES = 50        # list_folder 单目录条目上限
RUN_CODE_TIMEOUT = 30        # run_code 执行超时（秒），超时即 kill
RUN_CODE_OUTPUT_LIMIT = 2000 # run_code 输出截断（字符）

# ---- Token 用量优化 ----
MAX_HISTORY_MESSAGES = 20    # 发送给上游的消息数上限（保留 system + 最近 N 条）
MAX_TOOL_RESULT_CHARS = 1500 # 工具结果回传模型的最大长度（超出截断并提示）

# ---- Token 用量统计（缓存命中率）----
_stats_lock = threading.Lock()
_stats = {
    "calls": 0,             # 上游调用次数
    "prompt_tokens": 0,     # 输入 token 总数
    "cached_tokens": 0,     # 缓存命中 token 总数（DeepSeek cached_tokens）
    "completion_tokens": 0, # 输出 token 总数
    "reasoning_tokens": 0,  # 其中推理 token
}


def _trim_messages(messages: list[dict]) -> list[dict]:
    """控制上下文规模：保留 system + 最近 N 条消息，裁剪早期对话。

    注意：仅用于入口请求；agent 工具循环内部的 messages 不能裁剪
    （tool 消息必须紧跟对应的 assistant tool_calls）。
    """
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    keep = MAX_HISTORY_MESSAGES - len(system) - 1   # 预留一条"已省略"提示
    trimmed = rest[-keep:] if keep > 0 else []
    result = system + trimmed
    if len(rest) > len(trimmed):
        result.insert(len(system),
                      {"role": "system",
                       "content": "（较早的对话历史因过长已被省略，请基于当前上下文继续）"})
    return result


def _record_usage(usage) -> None:
    """聚合一次上游调用的 usage（线程安全）。兼容 DeepSeek / OpenAI 字段。"""
    if not isinstance(usage, dict):
        return
    prompt = usage.get("prompt_tokens") or 0
    cached = 0
    details = usage.get("prompt_tokens_details") or {}
    if isinstance(details, dict):
        cached = details.get("cached_tokens") or details.get("cache_read_input_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    reasoning = 0
    cd = usage.get("completion_tokens_details") or {}
    if isinstance(cd, dict):
        reasoning = cd.get("reasoning_tokens") or 0
    with _stats_lock:
        _stats["calls"] += 1
        _stats["prompt_tokens"] += prompt
        _stats["cached_tokens"] += cached
        _stats["completion_tokens"] += completion
        _stats["reasoning_tokens"] += reasoning

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("llm-backend")


def load_config() -> dict:
    default = {
        "api_url": "https://api.deepseek.com/v1/chat/completions",
        "api_key": "",
        "model": "deepseek-v4-flash",
        "context_window": 65536,   # 模型上下文窗口（token），用于容量显示与压缩阈值
    }
    if CONFIG_PATH.exists():
        try:
            return {**default, **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}
        except Exception:
            log.warning("chat_config.json 解析失败，使用默认配置")
    return default


def normalize_url(raw: str) -> str:
    """把用户填写的地址归一化为完整的 /v1/chat/completions 端点。

    统一的 OpenAI 兼容路径是 /v1/chat/completions：
    OpenAI / DeepSeek / Ollama / 各家兼容服务均支持。
    支持的填法（全部等价）：
      https://api.deepseek.com
      https://api.deepseek.com/v1
      https://api.deepseek.com/chat/completions
      https://api.deepseek.com/v1/chat/completions
      http://localhost:11434/v1
    """
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    # 兜底：域名 / base_url 统一补 /v1（OpenAI 官方仅支持带 /v1 的路径）
    return url + "/v1/chat/completions"


class ChatRequest(BaseModel):
    messages: list[dict] = Field(..., description="OpenAI 格式的消息列表")
    model: str | None = Field(default=None, description="覆盖配置中的模型名")
    temperature: float = Field(default=0.7, ge=0, le=2)
    agent: bool = Field(default=False, description="启用内置 Agent 工具调用循环")


class CompressRequest(BaseModel):
    messages: list[dict] = Field(..., description="待压缩的完整消息列表")
    keep_recent: int = Field(default=8, ge=2, le=30, description="保留最近 N 条消息不压缩")


class ConfigUpdate(BaseModel):
    api_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    context_window: int | None = None


class LlmError(Exception):
    """上游调用失败，携带可直接展示给用户的 HTTP 状态与信息。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


app = FastAPI(title="LLM Backend", version="0.3.0")

# 可选 token 鉴权：--token 启动时启用，所有请求须带 X-Api-Token 头
AUTH_TOKEN = ""


@app.middleware("http")
async def auth_middleware(request, call_next):
    if AUTH_TOKEN and request.headers.get("X-Api-Token") != AUTH_TOKEN:
        return JSONResponse(
            status_code=401,
            content={"detail": "未授权：需要正确的 X-Api-Token（llm_server 已启用 token 鉴权）"},
        )
    return await call_next(request)


COMPRESS_PROMPT = (
    "你是一个上下文压缩引擎。以下是 Agent 与用户之间的早期对话历史（JSON 格式）。\n"
    "请将其压缩为一份简洁的中文摘要，**保留对后续任务有用的信息**：\n"
    "- 用户的核心需求与偏好\n"
    "- 已完成的任务与结果\n"
    "- 进行中的任务与状态\n"
    "- 创建/修改/浏览过的文件路径\n"
    "- 重要的决定、约束与错误教训\n"
    "忽略寒暄和无关内容。只输出摘要正文，200 字以内，不要解释。"
)


@app.post("/api/v1/compress", summary="上下文压缩（模型摘要早期对话）")
async def compress(req: CompressRequest) -> dict:
    cfg = load_config()
    api_url = normalize_url(cfg.get("api_url"))
    api_key = (cfg.get("api_key") or "").strip()
    model = cfg.get("model") or ""
    _validate_config(api_url, api_key, req.messages)

    msgs = req.messages
    system = [m for m in msgs if m.get("role") == "system"]
    others = [m for m in msgs if m.get("role") != "system"]
    if len(others) <= req.keep_recent:
        return {"ok": True, "compressed": False, "messages": msgs,
                "stats": {"reason": "历史不足，无需压缩"}}

    keep = others[-req.keep_recent:]
    early = others[:-req.keep_recent]
    early_json = json.dumps(early, ensure_ascii=False)

    # 用模型生成早期对话摘要（非流式，少量 token）
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": COMPRESS_PROMPT},
            {"role": "user", "content": early_json},
        ],
        "temperature": 0.3,
        "max_tokens": 500,
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(None, _call_upstream_raw, api_url, payload, headers)
    except LlmError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    summary = (data["choices"][0]["message"].get("content") or "").strip()
    if not summary:
        raise HTTPException(502, "摘要生成失败（模型返回空内容）")

    new_msgs = system + [
        {"role": "system", "content": f"（较早对话的摘要，用于替代被压缩的历史）{summary}"},
    ] + keep

    def _chars(ms):
        return sum(len(m.get("content") or "") for m in ms)

    before_c, after_c = _chars(msgs), _chars(new_msgs)
    stats = {
        "before_messages": len(msgs),
        "after_messages": len(new_msgs),
        "dropped_messages": len(early),
        "before_chars": before_c,
        "after_chars": after_c,
        "saved_chars": before_c - after_c,
    }
    log.info("compress: %d -> %d messages, saved %d chars", len(msgs), len(new_msgs),
             before_c - after_c)
    return {"ok": True, "compressed": True, "messages": new_msgs, "summary": summary, "stats": stats}


@app.get("/api/v1/config", summary="查看 API 配置（Key 脱敏）")
async def get_config() -> dict:
    cfg = load_config()
    return {"ok": True, "config": {
        "api_url": cfg.get("api_url", ""),
        "api_key": "***" if cfg.get("api_key") else "",
        "model": cfg.get("model", ""),
        "context_window": cfg.get("context_window", 65536),
    }}


@app.post("/api/v1/config", summary="更新 API 配置（写入 chat_config.json，实时生效）")
async def update_config(req: ConfigUpdate) -> dict:
    cfg = load_config()
    updates = {}
    if req.api_url is not None:
        cfg["api_url"] = req.api_url.strip()
        updates["api_url"] = True
    if req.api_key is not None:
        cfg["api_key"] = req.api_key.strip()
        updates["api_key"] = True
    if req.model is not None:
        cfg["model"] = req.model.strip()
        updates["model"] = True
    if req.context_window is not None:
        if req.context_window <= 0:
            raise HTTPException(422, "context_window 必须是正整数")
        cfg["context_window"] = req.context_window
        updates["context_window"] = True
    if not updates:
        raise HTTPException(422, "没有可更新的字段（支持 api_url/api_key/model/context_window）")
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        raise HTTPException(500, f"配置写入失败：{exc}") from exc
    log.info("config updated: %s", updates)
    return {"ok": True, "updated": list(updates.keys()), "config": {
        "api_url": cfg.get("api_url", ""),
        "api_key": "***" if cfg.get("api_key") else "",
        "model": cfg.get("model", ""),
        "context_window": cfg.get("context_window", 65536),
    }}


@app.get("/api/v1/stats", summary="Token 用量统计（缓存命中率）")
async def stats() -> dict:
    with _stats_lock:
        s = dict(_stats)
    rate = s["cached_tokens"] / s["prompt_tokens"] if s["prompt_tokens"] else 0.0
    return {
        "ok": True,
        **s,
        "cache_hit_rate": round(rate, 4),
        "cache_hit_rate_pct": round(rate * 100, 1),
        "note": "cached_tokens 来自上游 prompt_tokens_details（DeepSeek 上下文硬盘缓存）",
    }


@app.get("/api/v1/health", summary="LLM 后端健康检查")
async def health() -> dict:
    cfg = load_config()
    return {
        "ok": True,
        "configured": bool(cfg.get("api_url") and cfg.get("api_key")),
        "api_url": normalize_url(cfg.get("api_url")),
        "model": cfg.get("model") or "",
        "context_window": cfg.get("context_window") or 65536,
        "isolated": ISOLATED,
        "tools": sorted(t["function"]["name"] for t in _agent_tools()),
    }


@app.post("/api/v1/test", summary="连接测试（发送最小请求验证配置，不进入会话）")
async def test_connection() -> dict:
    cfg = load_config()
    api_url = normalize_url(cfg.get("api_url"))
    api_key = (cfg.get("api_key") or "").strip()
    model = cfg.get("model") or ""
    _validate_config(api_url, api_key)

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "temperature": 0,
        "max_tokens": 200,  # 足够大：v4-flash 等模型先输出推理再输出正式回答
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _call_llm, api_url, payload, headers)
    except LlmError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return {"ok": True, "model": model, "reply": result["reply"][:80]}


@app.post("/api/v1/chat", summary="发送聊天消息（转发到 OpenAI 兼容接口）")
async def chat(req: ChatRequest) -> dict:
    cfg = load_config()
    api_url = normalize_url(cfg.get("api_url"))
    api_key = (cfg.get("api_key") or "").strip()
    model = req.model or cfg.get("model") or ""
    _validate_config(api_url, api_key, req.messages)

    payload = {"model": model, "messages": req.messages, "temperature": req.temperature}
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _call_llm, api_url, payload, headers)
    except LlmError as exc:
        log.warning("upstream error: %s", exc.message)
        raise HTTPException(exc.status, exc.message) from exc
    log.info("chat ok: model=%s messages=%d", payload["model"], len(req.messages))
    return result


def _validate_config(api_url: str, api_key: str, messages: list | None = None) -> None:
    if not api_url:
        raise HTTPException(422, "尚未配置 API URL，请先在 Chat 的 Settings 中设置")
    if not api_key:
        raise HTTPException(422, "尚未配置 API Key，请先在 Chat 的 Settings 中设置")
    if messages is not None and not messages:
        raise HTTPException(422, "messages 不能为空")


# ==========================================================================
# Agent 工具调用循环
# ==========================================================================
AGENT_TOOLS = [
    {"type": "function", "function": {
        "name": "get_screen_size",
        "description": "获取电脑屏幕的分辨率（宽x高），用于计算点击坐标。执行任何点击前建议先调用它。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "click",
        "description": "在电脑屏幕上指定坐标点击。x/y 为屏幕绝对坐标（0 到屏幕分辨率范围内）。",
        "parameters": {"type": "object",
                       "properties": {
                           "x": {"type": "integer", "description": "横坐标"},
                           "y": {"type": "integer", "description": "纵坐标"},
                           "clicks": {"type": "integer", "enum": [1, 2], "default": 1},
                           "button": {"type": "string", "enum": ["left", "right", "middle"],
                                      "default": "left"}},
                       "required": ["x", "y"]},
    }},
    {"type": "function", "function": {
        "name": "type_text",
        "description": "在当前聚焦的输入框中输入文字（中文自动处理，换行按 Enter）。",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string"}},
                       "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "press_key",
        "description": "按下按键或组合键。示例：enter、esc、tab、space、ctrl+c、alt+tab。",
        "parameters": {"type": "object",
                       "properties": {"key": {"type": "string"}},
                       "required": ["key"]},
    }},
    {"type": "function", "function": {
        "name": "create_folder",
        "description": "在工作区（主目录下的 agent_workspace）内创建文件夹，支持嵌套路径如 projects/demo。"
                       "路径必须是相对路径，不能是绝对路径或包含 ..",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string",
                                               "description": "相对工作区的文件夹路径，如 projects/demo"}},
                       "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "list_folder",
        "description": "浏览工作区内的文件夹内容，返回文件和子目录列表（含大小）。"
                       "path 省略时列出工作区根目录。",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string",
                                               "description": "相对工作区的文件夹路径，如 projects 或留空"}},
                       "required": []},
    }},
    {"type": "function", "function": {
        "name": "create_file",
        "description": "在工作区内创建或覆盖写入一个文件（用于编写代码/文档）。"
                       "父目录不存在会自动创建。内容为 UTF-8 文本。",
        "parameters": {"type": "object",
                       "properties": {
                           "path": {"type": "string", "description": "相对工作区的文件路径，如 projects/fib.py"},
                           "content": {"type": "string", "description": "文件内容（代码）"}},
                       "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "读取工作区内文件的内容（UTF-8 文本）。超过 10000 字符会被截断。",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string", "description": "相对工作区的文件路径"}},
                       "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "run_code",
        "description": "在工作区中执行 Python 代码。可指定 file（工作区内的 .py 文件）或直接传 code 字符串。"
                       "执行超时 30 秒，输出最多返回 3000 字符。"
                       "编写代码流程：先用 create_file 保存，再用 run_code 运行。",
        "parameters": {"type": "object",
                       "properties": {
                           "file": {"type": "string", "description": "相对工作区的 Python 文件路径（与 code 二选一）"},
                           "code": {"type": "string", "description": "要执行的 Python 代码（与 file 二选一）"}},
                       "required": []},
    }},
    {"type": "function", "function": {
        "name": "stop",
        "description": "紧急止停：立即停止所有后续操作并拒绝新指令。当用户要求停止或发现操作可能造成损害时调用。",
        "parameters": {"type": "object", "properties": {}},
    }},
]

AGENT_SYSTEM_SUFFIX = (
    "\n\n你是 PC Agent，一个可以控制用户电脑的智能体。你可以通过工具操作电脑。\n"
    "使用规则：\n"
    "1. 执行点击前，先调用 get_screen_size 获取屏幕分辨率，坐标必须在该范围内。\n"
    "2. 操作要谨慎，只执行用户明确要求的动作；不确定时先询问用户。\n"
    "3. 完成任务后，用简短的中文总结你做了什么。\n"
    "4. 用户要求停止或动作可能造成损害时，调用 stop 工具并告知用户。"
)


def _call_upstream_raw(api_url: str, payload: dict, headers: dict) -> dict:
    """非流式调用上游，返回完整响应 JSON（供工具循环解析 tool_calls）。"""
    req = urllib.request.Request(
        api_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _record_usage(data.get("usage"))
        return data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        upstream = _extract_upstream_error(body)
        raise LlmError(502, f"上游 API 返回 HTTP {e.code}：{upstream}")
    except urllib.error.URLError as e:
        raise LlmError(502, f"无法连接 API 服务（{getattr(e, 'reason', e)}）")
    except json.JSONDecodeError:
        raise LlmError(502, "上游返回了非 JSON 内容")


def _call_daemon(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    """调用本地屏幕控制 daemon（127.0.0.1:8000）。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        DAEMON_BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"detail": f"HTTP {e.code}"}
    except Exception as exc:
        return 0, {"detail": f"无法连接屏幕控制 daemon（{exc}）——请确认 app.py 已运行"}


def _get_workspace() -> Path:
    """Agent 文件工作区：主目录下的 agent_workspace（跟随运行系统：
    Windows 上是 C:\\Users\\xxx\\agent_workspace，WSL 上是 /home/xxx/agent_workspace）。"""
    p = Path.home() / "agent_workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_join(workspace: Path, rel: str) -> Path | None:
    """路径安全校验：拒绝绝对路径、盘符、..、空路径、符号链接越界。返回工作区内的绝对路径。"""
    raw = (rel or "").strip()
    if not raw:
        return None
    # 绝对路径 / Windows 盘符直接拒绝（而不是规范化为相对路径）
    if raw.startswith(("/", "\\")) or ":" in raw.split("/")[0].split("\\")[0]:
        return None
    parts = raw.replace("\\", "/").strip("/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    target = workspace.joinpath(*parts)
    try:
        # 解析符号链接后必须仍在工作区内（防 symlink 越界）
        target.resolve().relative_to(workspace.resolve())
    except (ValueError, OSError):
        return None
    return target


def _agent_tools() -> list[dict]:
    """按运行模式返回可用工具：隔离模式只保留文件类工具。"""
    if ISOLATED:
        return [t for t in AGENT_TOOLS if t["function"]["name"] in _FILE_TOOLS]
    return AGENT_TOOLS


def _execute_tool(name: str, arguments: str) -> tuple[bool, str]:
    """执行工具，返回 (ok, 结果文本)。在工作线程中调用。"""
    if ISOLATED and name in _SCREEN_TOOLS:
        return False, "隔离模式：屏幕操作工具已禁用（llm_server 以 --isolated 运行）"
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return False, "工具参数不是合法 JSON"
    try:
        # ---- 参数安全校验 ----
        if name == "type_text":
            text = args.get("text") or ""
            if len(text) > MAX_TEXT_LENGTH:
                return False, f"文本过长（{len(text)} 字符 > 上限 {MAX_TEXT_LENGTH}）"
        if name == "click":
            x, y = args.get("x"), args.get("y")
            if not isinstance(x, int) or not isinstance(y, int):
                return False, "click 需要整数坐标 x/y"
            if x < 0 or y < 0:
                return False, f"坐标不能为负（{x},{y}）"
            # 屏幕范围兜底校验（daemon 侧也有校验）
            code, data = _call_daemon("GET", "/api/v1/status")
            if code == 200:
                w, h = data["screen_size"]["width"], data["screen_size"]["height"]
                if w and h and (x >= w or y >= h):
                    return False, f"坐标越界（{x},{y}）超出屏幕 {w}x{h}"
            else:
                return False, f"无法获取屏幕尺寸：{data.get('detail')}"
        if name == "press_key":
            key = args.get("key") or ""
            if len(key) > 50:
                return False, "按键名称过长"
        if name == "get_screen_size":
            code, data = _call_daemon("GET", "/api/v1/status")
            if code != 200:
                return False, f"获取屏幕信息失败：{data.get('detail')}"
            return True, json.dumps({"width": data["screen_size"]["width"],
                                     "height": data["screen_size"]["height"]},
                                    ensure_ascii=False)
        if name == "stop":
            code, data = _call_daemon("POST", "/api/v1/stop")
            return code == 200, json.dumps({"stopped": True, **data}, ensure_ascii=False)
        if name == "create_folder":
            workspace = _get_workspace()
            target = _safe_join(workspace, args.get("path", ""))
            if target is None:
                return False, "非法路径：必须是工作区内的相对路径（不能是绝对路径、空路径或包含 ..）"
            target.mkdir(parents=True, exist_ok=True)
            rel = target.relative_to(workspace).as_posix()
            return True, json.dumps({"created": rel, "absolute": str(target)},
                                    ensure_ascii=False)
        if name == "list_folder":
            workspace = _get_workspace()
            rel = (args.get("path") or "").strip() or "."
            target = workspace if rel == "." else _safe_join(workspace, rel)
            if target is None:
                return False, "非法路径：必须是工作区内的相对路径"
            if not target.is_dir():
                return False, f"不是文件夹：{rel}"
            entries = []
            try:
                for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                    if len(entries) >= MAX_LIST_ENTRIES:
                        entries.append({"name": "...", "type": "truncated"})
                        break
                    if p.is_dir():
                        entries.append({"name": p.name + "/", "type": "dir"})
                    else:
                        try:
                            size = p.stat().st_size
                        except OSError:
                            size = 0
                        entries.append({"name": p.name, "type": "file", "size": size})
            except PermissionError:
                return False, "无权限访问该目录"
            return True, json.dumps({"path": rel, "entries": entries}, ensure_ascii=False)
        if name == "create_file":
            workspace = _get_workspace()
            target = _safe_join(workspace, args.get("path", ""))
            if target is None:
                return False, "非法路径：必须是工作区内的相对路径"
            content = args.get("content") or ""
            if len(content) > MAX_WRITE_CHARS:
                return False, f"内容过长（{len(content)} 字符 > 上限 {MAX_WRITE_CHARS}）"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return True, json.dumps(
                {"created": target.relative_to(workspace).as_posix(), "chars": len(content)},
                ensure_ascii=False)
        if name == "read_file":
            workspace = _get_workspace()
            target = _safe_join(workspace, args.get("path", ""))
            if target is None:
                return False, "非法路径：必须是工作区内的相对路径"
            if not target.is_file():
                return False, f"文件不存在：{args.get('path')}"
            size = target.stat().st_size
            if size > MAX_FILE_SIZE:
                return False, f"文件过大（{size} 字节 > {MAX_FILE_SIZE}）"
            text = target.read_text(encoding="utf-8", errors="replace")
            if len(text) > MAX_FILE_CHARS:
                return True, text[:MAX_FILE_CHARS] + f"\n...(截断，共 {len(text)} 字符)"
            return True, text
        if name == "run_code":
            workspace = _get_workspace()
            code = args.get("code") or ""
            if args.get("file"):
                target = _safe_join(workspace, args["file"])
                if target is None:
                    return False, "非法路径：必须是工作区内的相对路径"
                if not target.is_file():
                    return False, f"文件不存在：{args['file']}"
                code = target.read_text(encoding="utf-8", errors="replace")
            if not code.strip():
                return False, "没有可执行的代码（请提供 file 或 code 参数）"
            try:
                proc = subprocess.run(
                    [sys.executable, "-c", code],
                    cwd=str(workspace), capture_output=True, text=True,
                    timeout=RUN_CODE_TIMEOUT, shell=False,
                )
            except subprocess.TimeoutExpired:
                return False, f"执行超时（>{RUN_CODE_TIMEOUT}s），已强制终止"
            except Exception as exc:
                return False, f"执行失败：{exc}"
            stdout = (proc.stdout or "")[:RUN_CODE_OUTPUT_LIMIT]
            stderr = (proc.stderr or "")[:RUN_CODE_OUTPUT_LIMIT]
            if proc.returncode != 0:
                return False, json.dumps({"exit_code": proc.returncode, "stderr": stderr},
                                         ensure_ascii=False)
            return True, json.dumps({"exit_code": 0, "stdout": stdout}, ensure_ascii=False)
        if name in ("click", "type_text", "press_key"):
            code, data = _call_daemon("POST", "/api/v1/execute", {"action": name, **args})
            if code == 200:
                return True, json.dumps({"ok": True}, ensure_ascii=False)
            return False, f"执行失败：{data.get('detail', code)}"
        return False, f"未知工具：{name}"
    except Exception as exc:
        return False, f"工具执行异常：{exc}"


def _agent_loop(api_url: str, headers: dict, messages: list[dict],
                model: str, temperature: float, q: queue.Queue,
                cancel: threading.Event) -> None:
    """后台线程：工具调用循环，事件经 queue 发送给 SSE 生成器。

    安全锁：轮数上限 / 总调用数上限 / 连续失败熔断 / 总时长上限 / 取消事件。
    事件: ("tool_call", {...}) / ("tool_result", {...}) /
          ("delta", text) / ("done", None) / ("error", msg)
    """
    start = time.monotonic()
    tool_calls_total = 0
    consecutive_failures = 0

    for step in range(1, MAX_TOOL_STEPS + 1):
        # 安全锁检查
        if cancel.is_set():
            q.put(("error", "已由用户中止"))
            return
        if time.monotonic() - start > MAX_AGENT_SECONDS:
            q.put(("error", f"任务超过总时长上限 {MAX_AGENT_SECONDS}s，已自动中止"))
            return
        if tool_calls_total >= MAX_TOOL_CALLS_TOTAL:
            q.put(("error", f"工具调用总数超过上限 {MAX_TOOL_CALLS_TOTAL}，已自动中止"))
            return

        payload = {"model": model, "messages": messages, "temperature": temperature,
                   "tools": _agent_tools()}
        try:
            data = _call_upstream_raw(api_url, payload, headers)
        except LlmError as exc:
            q.put(("error", exc.message))
            return
        msg = data["choices"][0]["message"]
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            # 无工具调用：最终回复，模拟分块流式（前端打字机效果）
            content = msg.get("content") or ""
            for i in range(0, len(content), 4):
                q.put(("delta", content[i:i + 4]))
            q.put(("done", None))
            log.info("agent done after %d step(s)", step)
            return

        # 模型要求调用工具
        if cancel.is_set():
            q.put(("error", "已由用户中止"))
            return
        tool_calls_total += len(tool_calls)
        log.info("agent step %d: %d tool call(s) (total %d)", step,
                 len(tool_calls), tool_calls_total)
        messages.append({"role": "assistant", "content": msg.get("content") or None,
                         "tool_calls": tool_calls})
        for tc in tool_calls:
            if cancel.is_set():
                q.put(("error", "已由用户中止"))
                return
            fn = tc["function"]
            q.put(("tool_call", {"id": tc["id"], "name": fn["name"],
                                 "arguments": fn["arguments"],
                                 "step": step, "max_steps": MAX_TOOL_STEPS}))
            ok, result = _execute_tool(fn["name"], fn["arguments"])
            # 工具结果截断：避免大结果（read_file 等）撑爆上下文
            if len(result) > MAX_TOOL_RESULT_CHARS:
                result = result[:MAX_TOOL_RESULT_CHARS] + "\n...(结果过长已截断)"
            q.put(("tool_result", {"id": tc["id"], "ok": ok, "result": result}))
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            # 连续失败熔断
            consecutive_failures = 0 if ok else consecutive_failures + 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                q.put(("error", f"连续 {MAX_CONSECUTIVE_FAILURES} 次工具调用失败，"
                                f"熔断器已触发，任务自动中止"))
                return
    q.put(("error", f"工具调用超过 {MAX_TOOL_STEPS} 轮上限，已自动中止"))


async def _agent_stream_events(api_url: str, headers: dict, messages: list[dict],
                               model: str, temperature: float):
    """把 agent 循环的事件流转发为 SSE；客户端断开时通知循环线程停止。"""
    # 并发互斥：同一时刻只允许一个 agent 循环（防止多循环同时操作屏幕）
    if not _agent_lock.acquire(blocking=False):
        yield f"event: error\ndata: {json.dumps({'detail': '已有另一个 Agent 任务正在执行，请等待完成'}, ensure_ascii=False)}\n\n"
        return
    cancel = threading.Event()
    q: queue.Queue = queue.Queue()
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _agent_loop, api_url, headers, messages, model,
                         temperature, q, cancel)
    try:
        while True:
            kind, data = await loop.run_in_executor(None, q.get)
            if kind == "done":
                break
            if kind == "error":
                yield f"event: error\ndata: {json.dumps({'detail': data}, ensure_ascii=False)}\n\n"
                break
            if kind == "tool_call":
                yield f"event: tool_call\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            elif kind == "tool_result":
                yield f"event: tool_result\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            elif kind == "delta":
                yield f"data: {json.dumps({'choices': [{'delta': {'content': data}}]}, ensure_ascii=False)}\n\n"
    except asyncio.CancelledError:
        # 客户端断开：让循环线程在下一个检查点退出
        cancel.set()
        raise
    finally:
        _agent_lock.release()


# --------------------------------------------------------------------------
# 流式聊天（SSE）：上游逐块转发，读取在后台线程，事件循环不阻塞
# --------------------------------------------------------------------------
@app.post("/api/v1/chat/stream", summary="流式聊天（SSE 逐块返回；agent=true 启用工具调用循环）")
async def chat_stream(req: ChatRequest):
    cfg = load_config()
    api_url = normalize_url(cfg.get("api_url"))
    api_key = (cfg.get("api_key") or "").strip()
    model = req.model or cfg.get("model") or ""
    _validate_config(api_url, api_key, req.messages)

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    if req.agent:
        # Agent 模式：附加工具说明到 system 消息，走工具调用循环
        messages = [dict(m) for m in _trim_messages(req.messages)]
        for m in messages:
            if m.get("role") == "system":
                m["content"] = (m.get("content") or "") + AGENT_SYSTEM_SUFFIX
                break
        else:
            messages.insert(0, {"role": "system", "content": AGENT_SYSTEM_SUFFIX.lstrip()})
        return StreamingResponse(
            _agent_stream_events(api_url, headers, messages, model, req.temperature),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    payload = {"model": model, "messages": _trim_messages(req.messages),
               "temperature": req.temperature, "stream": True}
    return StreamingResponse(
        _stream_events(api_url, payload, headers),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _upstream_reader(api_url: str, payload: dict, headers: dict, q: queue.Queue) -> None:
    """后台线程：读取上游 SSE 流，逐行放入 queue（保持 data: 前缀原样）。"""
    req = urllib.request.Request(
        api_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            for raw in resp:                      # 逐行迭代 SSE
                line = raw.decode("utf-8", "replace")
                if '"usage"' in line:             # 流式末尾的用量块
                    try:
                        _record_usage(json.loads(line.split("data:", 1)[-1].strip()).get("usage"))
                    except Exception:
                        pass
                q.put(("data", line))
    except urllib.error.HTTPError as e:
        q.put(("error", f"上游 HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"))
    except Exception as e:
        q.put(("error", str(e)))
    q.put(("done", None))


async def _stream_events(api_url: str, payload: dict, headers: dict):
    """把上游 SSE 流逐块转发给客户端。"""
    q: queue.Queue = queue.Queue()
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _upstream_reader, api_url, payload, headers, q)
    while True:
        # q.get 是阻塞调用，放在线程池执行，不阻塞事件循环
        kind, data = await loop.run_in_executor(None, q.get)
        if kind == "done":
            break
        if kind == "error":
            yield f"event: error\ndata: {json.dumps({'detail': data})}\n\n"
            break
        yield data.decode("utf-8", "replace") if isinstance(data, bytes) else data


def _call_llm(api_url: str, payload: dict, headers: dict) -> dict:
    """后台线程执行：转发请求到上游并解析回复。"""
    req = urllib.request.Request(
        api_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _record_usage(data.get("usage"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        upstream = _extract_upstream_error(body)
        if e.code in (401, 403):
            raise LlmError(502, f"API Key 无效或被拒绝（HTTP {e.code}）：{upstream}请检查 Settings 中的 Key")
        if e.code == 404:
            raise LlmError(502, f"API 地址或路径不正确（HTTP 404）：{api_url} —— 请确认填的是完整地址或域名，如 https://api.deepseek.com 或 https://api.openai.com/v1")
        if e.code == 429:
            raise LlmError(502, f"请求过于频繁或额度不足（HTTP 429）：{upstream}")
        raise LlmError(502, f"上游 API 返回 HTTP {e.code}：{upstream}")
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        raise LlmError(502, f"无法连接 API 服务（{reason}）—— 检查网络；OpenAI 等境外接口需要代理")
    except json.JSONDecodeError:
        raise LlmError(502, "上游返回了非 JSON 内容（该地址可能不是 OpenAI 兼容接口）")
    except Exception as e:
        raise LlmError(502, f"请求失败：{e}")

    try:
        reply = _extract_reply(data)
    except LlmError:
        raise
    msg = data["choices"][0]["message"]
    return {
        "reply": reply,
        "reasoning": msg.get("reasoning_content") or "",
        "model": payload["model"],
        "usage": data.get("usage"),
    }


def _extract_upstream_error(body: str) -> str:
    """提取上游错误 body 中的 error.message（如 DeepSeek 的 "Model Not Exist"）。"""
    try:
        data = json.loads(body)
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or json.dumps(err, ensure_ascii=False)
        else:
            msg = str(err) if err else json.dumps(data, ensure_ascii=False)
        return f"{msg}。" if msg and not msg.endswith(("。", ".", "！")) else f"{msg}"
    except Exception:
        return body[:200]


def _extract_reply(data: dict) -> str:
    """解析回复内容；兼容 DeepSeek reasoner 等空 content 场景。"""
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise LlmError(502, f"上游响应格式异常（非标准 OpenAI 结构）：{json.dumps(data, ensure_ascii=False)[:300]}")
    content = msg.get("content") or ""
    if content.strip():
        return content
    # content 为空但存在推理内容：常见于 max_tokens 太小被推理占满，
    # 或纯 reasoner 模型尚未输出正式回答
    reasoning = msg.get("reasoning_content") or ""
    if reasoning:
        raise LlmError(502, "模型只返回了推理内容、没有正式回答（reasoning_content 非空）——"
                            "若为连接测试请重试；若使用纯 reasoner 模型（如 deepseek-reasoner）请改用对话模型")
    raise LlmError(502, f"上游返回了空内容：{json.dumps(msg, ensure_ascii=False)[:200]}")


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="LLM Backend")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址；Linux 虚拟机等远程访问时用 0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--token", default="",
                        help="启用 token 鉴权（远程访问时强烈建议设置）")
    parser.add_argument("--isolated", action="store_true",
                        help="隔离模式：禁用屏幕操作工具，只保留文件类工具（WSL 隔离测试用）")
    args = parser.parse_args()
    AUTH_TOKEN = args.token   # 模块级赋值即修改全局
    ISOLATED = args.isolated
    if args.isolated:
        log.warning("隔离模式：屏幕操作工具已禁用，仅保留 %s", sorted(_FILE_TOOLS))
    if args.token:
        log.warning("token 鉴权已启用，客户端需携带 X-Api-Token 头")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
