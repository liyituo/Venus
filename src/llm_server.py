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
import difflib
import fnmatch
import itertools
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR.parent / "chat_config.json"
APP_VERSION = "0.7.0"       # 系统版本（health 端点返回，前端可展示）
UPSTREAM_TIMEOUT = 180  # 模型生成可能较慢（reasoner 更慢）
DAEMON_BASE = "http://127.0.0.1:8000"   # 屏幕控制 daemon（app.py）

# ---- Agent 安全锁（防循环调用导致系统崩溃）----
MAX_TOOL_STEPS = 10             # 单次请求最多工具轮数
MAX_TOOL_CALLS_TOTAL = 30       # 单次请求工具调用总数上限（一轮可含多个调用）
MAX_CONSECUTIVE_FAILURES = 4    # 连续失败熔断阈值：达到即停止整个任务
MAX_AGENT_SECONDS = 240         # 单次 agent 请求总耗时上限（含上游生成时间）
MAX_TEXT_LENGTH = 5000          # type_text 文本长度上限
_agent_lock = threading.Lock()  # 并发互斥：同一时刻只允许一个 agent 循环

# ---- 敏感操作确认机制 ----
CONFIRM_TIMEOUT = 120          # 等待用户确认超时（秒），超时默认拒绝（安全方向）
_confirm_table: dict = {}      # request_id -> {"event": Event, "choice": str|None}
_confirm_lock = threading.Lock()
_confirm_counter = itertools.count(1)

# run_shell 只读命令白名单：命中则无需确认（其余命令默认需要确认）
READONLY_SHELL = re.compile(
    r"^\s*(ls|cat|head|tail|grep|find|echo|pwd|whoami|date|df|du|uname|ps|env|"
    r"which|type|file|stat|wc|sort|cut|awk|sed -n|history|printenv|id|hostname|"
    r"uptime|free|getconf|locale)\b"
)
# Windows（cmd）只读命令白名单：Linux 白名单 + cmd 原生只读命令
READONLY_SHELL_WIN = re.compile(
    r"^\s*(ls|cat|head|tail|grep|find|echo|pwd|whoami|date|df|du|uname|ps|env|"
    r"which|type|file|stat|wc|sort|cut|awk|sed -n|history|printenv|id|hostname|"
    r"uptime|free|getconf|locale|dir|more|findstr|where|tasklist|systeminfo|"
    r"ver|set|path|cd|cls|help|netstat|ipconfig|reg query)\b"
)

# 确认请求需要发 ask 事件的敏感工具
CONFIRM_TOOLS = {"create_file", "run_shell", "replace_text", "git_commit", "start_process", "undo"}

# ---- 问询模式（四种）----
CONFIRM_MODES = ("auto", "strict", "trusted", "query")
CONFIRM_MODE_DESC = {
    "auto":    "智能：敏感写操作确认，只读命令免确认（默认）",
    "strict":  "严格：所有修改/执行类操作都需确认",
    "trusted": "信任：全部自动执行（危险命令黑名单仍拦截）",
    "query":   "只读：仅允许查询操作，一切修改直接拒绝",
}
QUERY_TOOLS = {  # 查询类工具（任何模式都放行）
    "list_folder", "read_file", "get_screen_size",
    "search_text", "glob_files", "list_symbols",
    "git_status", "git_diff", "git_log",
    "process_output", "list_processes", "list_todos", "repo_map",
}


def _current_confirm_mode() -> str:
    return str(load_config().get("confirm_mode", "auto"))


def _confirm_policy(name: str, args: dict) -> str:
    """按当前问询模式决定工具处理方式。
    返回：allow（直接执行）/ ask（需用户确认）/ deny（直接拒绝）
    """
    mode = _current_confirm_mode()
    if mode == "trusted":
        return "allow"
    is_query = name in QUERY_TOOLS or (
        name == "run_shell" and _is_readonly_shell((args.get("command") or "").strip()))
    if mode == "query":
        return "allow" if is_query else "deny"
    if mode == "strict":
        return "allow" if is_query else "ask"
    # auto
    if name.startswith("mcp_"):
        return "ask"    # MCP 外部工具保守处理：一律确认（trusted 模式已放行）
    return "ask" if _needs_confirm(name, args) else "allow"


class ConfirmModeRequest(BaseModel):
    mode: str = Field(..., description="auto / strict / trusted / query")


# 隔离模式（--isolated）：禁用屏幕操作工具，只保留文件/系统类工具。
# WSL 隔离测试环境使用，代码层面保证 agent 无法操作任何屏幕。
_SCREEN_TOOLS = {"get_screen_size", "click", "type_text", "press_key"}
_FILE_TOOLS = {
    "create_folder", "list_folder", "create_file", "read_file", "run_code", "run_shell",
    "search_text", "glob_files", "list_symbols", "replace_text", "undo",
    "git_status", "git_diff", "git_log", "git_commit",
    "start_process", "process_output", "stop_process", "list_processes",
    "create_todo", "update_todo", "list_todos", "repo_map",
}
ISOLATED = False

# ---- 文件/系统工具安全限制 ----
MAX_FILE_SIZE = 200_000      # read_file 单文件上限（字节）
MAX_FILE_CHARS = 4000        # read_file 返回字符数上限（防爆 token）
MAX_WRITE_CHARS = 100_000    # create_file 内容上限
MAX_LIST_ENTRIES = 50        # list_folder 单目录条目上限
RUN_CODE_TIMEOUT = 30        # run_code 执行超时（秒），超时即 kill
RUN_CODE_OUTPUT_LIMIT = 2000 # run_code 输出截断（字符）
RUN_SHELL_TIMEOUT = 30       # run_shell 执行超时（秒），超时即 kill
RUN_SHELL_OUTPUT_LIMIT = 3000
RUN_SHELL_MAX_CMD = 2000     # 命令长度上限

# run_shell 危险命令黑名单（破坏性操作，正则匹配即拦截）
DANGEROUS_PATTERNS = [
    r"\brm\s+(-[a-zA-Z]*\s+)*/",            # rm -rf / 或 rm /xxx（根路径）
    r"\brm\s+(-[a-zA-Z]*\s+)*~",            # rm -rf ~
    r"\bmkfs\b",
    r"\bdd\s+if=/dev/zero",
    r"\bdd\s+of=/dev/",
    r"\bshutdown\b", r"\breboot\b", r"\bpoweroff\b", r"\bhalt\b",
    r"\binit\s+[06]\b",
    r"\bmv\s+/\s+\S",                        # mv / xxx
    r":\(\)\s*\{",                           # fork bomb
    r"\bchmod\s+-R\s+777\s+/",               # 全盘权限
    r">+\s*/dev/sd",                         # 写块设备
    r"\bsudo\s+rm\b",
    r"\bgit\s+push\s+(-f|--force)",          # 强制推送（防误覆盖远程）
    # ---- Windows 破坏性命令 ----
    r"\bformat\s+[a-zA-Z]:",                 # format C:（格式化盘符）
    r"\bdiskpart\b",                         # 磁盘分区工具
    r"\b(rd|rmdir|deltree)\s+(/\w+\s+)*[a-zA-Z]:",  # rd /s /q C:\（递归删盘）
    r"\bdel\s+/\w*\s*[a-zA-Z]:\\",           # del /f /s /q C:\*
    r"\breg\s+(add|delete)\b",               # 注册表写操作
    r"\bcipher\s+/\w*[w]",                   # cipher /w（擦除磁盘）
]

# ---- 编程工具安全限制（检索 / 编辑 / git / 进程 / todo / repo 索引）----
SEARCH_MAX_RESULTS = 30          # search_text 单次最多匹配数
SEARCH_CONTEXT_LINES = 2         # 匹配行上下文行数（默认）
SEARCH_MAX_FILE_SIZE = 1_000_000 # 搜索跳过 >1MB 文件
SEARCH_MAX_LINE_LEN = 300        # 匹配行内容截断长度
SEARCH_OUTPUT_LIMIT = 6000       # 搜索结果回传模型的总字符上限
MAX_SYMBOLS_PER_FILE = 50        # list_symbols / repo_map 单文件符号上限
MAX_SYMBOLS_FILE_SIZE = 1_000_000
REPLACE_OLD_MAX = 8000           # replace_text old 串长度上限
REPLACE_DIFF_CHARS = 2000        # replace_text 确认 diff 回传上限
MAX_PROCESSES = 8                # start_process 并发上限
PROCESS_OUTPUT_LINES = 2000      # 进程输出环形缓冲行数
PROCESS_OUTPUT_LIMIT = 4000      # process_output 单次返回字符上限
PROCESS_WAIT_MAX = 60            # process_output wait_seconds 上限
MAX_TODOS = 30                   # 任务清单上限
TODO_STATUSES = ("pending", "in_progress", "completed", "failed", "cancelled")
REPO_MAP_TTL = 30                # repo_map 缓存秒数
REPO_MAP_MAX_ENTRIES = 300
REPO_MAP_MAX_DEPTH = 5
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
             ".pcagent", ".idea", ".vscode", ".mypy_cache", ".pytest_cache"}

# ---- Token 用量优化 ----
MAX_HISTORY_MESSAGES = 20    # 发送给上游的消息数上限（保留 system + 最近 N 条）
MAX_HISTORY_CHARS = 120_000  # 发送给上游的总字符硬上限（约 96K tokens，防长消息累积激增）
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

    双重上限，保证每轮发送给模型的总量有硬边界（防 tokens 激增）：
    1. 条数上限 MAX_HISTORY_MESSAGES（20 条）
    2. 总字符上限 MAX_HISTORY_CHARS（12 万字符，从最早消息开始丢）

    注意：仅用于入口请求；agent 工具循环内部的 messages 不能裁剪
    （tool 消息必须紧跟对应的 assistant tool_calls）。
    """
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    if len(rest) <= MAX_HISTORY_MESSAGES - 1:
        trimmed = rest
    else:
        keep = MAX_HISTORY_MESSAGES - len(system) - 1   # 预留一条"已省略"提示
        trimmed = rest[-keep:] if keep > 0 else []
    # 字符硬上限：从最早的对话开始丢，直到总字符达标
    total = sum(len(m.get("content") or "") for m in system + trimmed)
    while trimmed and total > MAX_HISTORY_CHARS:
        dropped = trimmed.pop(0)
        total -= len(dropped.get("content") or "")
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


def _setup_file_logging() -> None:
    """统一运行日志：写入项目根 .pcagent/server.log（1MB 轮转，保留 3 份）。"""
    try:
        from logging.handlers import RotatingFileHandler
        log_dir = BASE_DIR.parent / ".pcagent"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "server.log", maxBytes=1_000_000,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
        log.info("运行日志已写入 %s", log_dir / "server.log")
    except Exception as exc:
        log.warning("日志文件初始化失败：%s", exc)


_setup_file_logging()


def load_config() -> dict:
    default = {
        "api_url": "https://api.deepseek.com/v1/chat/completions",
        "api_key": "",
        "model": "deepseek-v4-flash",
        "context_window": 65536,   # 模型上下文窗口（token），用于容量显示与压缩阈值
        "confirm_mode": "auto",    # 问询模式：auto/strict/trusted/query
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


app = FastAPI(title="LLM Backend", version=APP_VERSION)


@app.get("/api/v1/confirm-mode", summary="查看当前问询模式")
async def get_confirm_mode() -> dict:
    return {"ok": True, "mode": _current_confirm_mode(),
            "modes": list(CONFIRM_MODES), "descriptions": CONFIRM_MODE_DESC}


@app.post("/api/v1/confirm-mode", summary="切换问询模式（实时生效，持久化保存）")
async def set_confirm_mode(req: ConfirmModeRequest) -> dict:
    if req.mode not in CONFIRM_MODES:
        raise HTTPException(422, f"无效模式，可选：{' / '.join(CONFIRM_MODES)}")
    cfg = load_config()
    cfg["confirm_mode"] = req.mode
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        raise HTTPException(500, f"配置写入失败：{exc}") from exc
    log.info("confirm mode -> %s", req.mode)
    return {"ok": True, "mode": req.mode, "description": CONFIRM_MODE_DESC[req.mode]}


# 可选 token 鉴权：--token 启动时启用，所有请求须带 X-Api-Token 头
AUTH_TOKEN = ""


@app.middleware("http")
async def log_requests(request, call_next):
    """请求日志：method/path/状态/耗时，写入统一运行日志。"""
    start = time.monotonic()
    response = await call_next(request)
    log.info("%s %s -> %d (%.2fs)", request.method, request.url.path,
             response.status_code, time.monotonic() - start)
    return response


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
        "version": APP_VERSION,
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
        "name": "run_shell",
        "description": "在 Linux 系统中执行 shell 命令（ls/cat/grep/find/pip/apt/systemctl 等任意命令，"
                       "支持管道 |、重定向 >、&& 等 shell 语法）。默认在工作区目录执行，可用 cwd 指定目录。"
                       "执行超时 30 秒，输出最多 3000 字符。"
                       "破坏性命令（rm -rf /、mkfs、shutdown、dd 写磁盘、fork bomb 等）会被拦截。"
                       "注意：sudo 命令需要交互密码，非交互环境会失败。",
        "parameters": {"type": "object",
                       "properties": {
                           "command": {"type": "string", "description": "要执行的 shell 命令"},
                           "cwd": {"type": "string",
                                   "description": "可选：执行目录（默认工作区 ~/agent_workspace，可用绝对路径如 /tmp）"}},
                       "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "stop",
        "description": "紧急止停：立即停止所有后续操作并拒绝新指令。当用户要求停止或发现操作可能造成损害时调用。",
        "parameters": {"type": "object", "properties": {}},
    }},
    # ---- 编程工具：检索 ----
    {"type": "function", "function": {
        "name": "search_text",
        "description": "在项目中搜索文本/正则，返回 文件:行号:内容（可带上下文行）。"
                       "自动跳过 .venv/.git/node_modules 等目录。定位代码优先用它，不要逐个 read_file 翻。",
        "parameters": {"type": "object",
                       "properties": {
                           "pattern": {"type": "string", "description": "正则表达式或关键字，如 r'def \\w+' 或 'fib'"},
                           "path": {"type": "string", "description": "可选：限定搜索目录（相对工作区，省略搜整个工作区）"},
                           "file_pattern": {"type": "string", "description": "可选：按文件名过滤，如 *.py、*.md"},
                           "max_results": {"type": "integer", "default": 20, "description": "最多返回匹配数（1-30）"},
                           "context_lines": {"type": "integer", "default": 2, "description": "匹配行前后各带几行上下文（0-5）"}},
                       "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "glob_files",
        "description": "按文件名模式列出工作区文件，如 *.py、test_*.py。返回相对路径 + 大小 + 行数。"
                       "想找「哪些文件」时用它，想找「内容关键字」用 search_text。",
        "parameters": {"type": "object",
                       "properties": {
                           "pattern": {"type": "string", "description": "文件名模式（支持 * 通配），如 *.py、test_*.py"},
                           "max_results": {"type": "integer", "default": 50, "description": "最多返回条数"}},
                       "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "list_symbols",
        "description": "列出单个文件的函数/类定义符号表（行号 + 签名），快速了解文件结构，无需读全文。",
        "parameters": {"type": "object",
                       "properties": {"file": {"type": "string", "description": "相对工作区的文件路径"}},
                       "required": ["file"]},
    }},
    # ---- 编程工具：精确编辑 ----
    {"type": "function", "function": {
        "name": "replace_text",
        "description": "精确修改工作区内的文件：把 old 文本替换为 new 文本（小步修改，不要重写整个文件）。"
                       "执行前会生成 diff 并经用户确认。old 必须是文件中的唯一片段（注意缩进/换行），"
                       "若不唯一可用 occurrence 指定第几次出现，或把 old 写长一些。"
                       "修改前会自动备份，改错了可用 undo 恢复。",
        "parameters": {"type": "object",
                       "properties": {
                           "file": {"type": "string", "description": "相对工作区的文件路径"},
                           "old": {"type": "string", "description": "要被替换的原文（需与文件内容逐字一致）"},
                           "new": {"type": "string", "description": "替换成的新文本"},
                           "occurrence": {"type": "integer", "default": 1, "description": "old 出现多次时指定第几次"}},
                       "required": ["file", "old", "new"]},
    }},
    {"type": "function", "function": {
        "name": "undo",
        "description": "撤销最近一次文件修改（replace_text / create_file 覆盖前会自动备份）。"
                       "file 参数可指定只撤销某个文件；省略则撤销全局最近一次修改。"
                       "执行前会展示前后 diff 并经用户确认。",
        "parameters": {"type": "object",
                       "properties": {"file": {"type": "string",
                                               "description": "可选：要撤销的文件（相对工作区）"}},
                       "required": []},
    }},
    # ---- 编程工具：Git ----
    {"type": "function", "function": {
        "name": "git_status",
        "description": "查看工作区内 Git 仓库的状态：当前分支 + 变更文件列表（相当于 git status --short）。"
                       "如果还没有仓库，先用 run_shell 执行 git init。",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string", "description": "可选：仓库所在目录（相对工作区）"}},
                       "required": []},
    }},
    {"type": "function", "function": {
        "name": "git_diff",
        "description": "查看工作区 Git 仓库的改动内容（unified diff）或统计摘要。改完代码用它自查改动。",
        "parameters": {"type": "object",
                       "properties": {
                           "path": {"type": "string", "description": "可选：仓库所在目录（相对工作区）"},
                           "staged": {"type": "boolean", "default": False, "description": "查看已暂存（git add 后）的改动"},
                           "stat": {"type": "boolean", "default": False, "description": "只看统计摘要（改动文件/行数）"}},
                       "required": []},
    }},
    {"type": "function", "function": {
        "name": "git_log",
        "description": "查看工作区 Git 仓库的最近提交历史（git log --oneline）。",
        "parameters": {"type": "object",
                       "properties": {
                           "path": {"type": "string", "description": "可选：仓库所在目录（相对工作区）"},
                           "n": {"type": "integer", "default": 10, "description": "显示最近几条（1-30）"}},
                       "required": []},
    }},
    {"type": "function", "function": {
        "name": "git_commit",
        "description": "提交工作区 Git 仓库的所有改动（内部执行 git add -A + git commit）。"
                       "提交前会展示改动统计并经用户确认。",
        "parameters": {"type": "object",
                       "properties": {
                           "message": {"type": "string", "description": "提交信息（说明改了什么、为什么）"},
                           "path": {"type": "string", "description": "可选：仓库所在目录（相对工作区）"}},
                       "required": ["message"]},
    }},
    # ---- 编程工具：后台进程 ----
    {"type": "function", "function": {
        "name": "start_process",
        "description": "在后台启动一个长时间运行的进程（如 dev server、监听脚本），不阻塞当前任务。"
                       "启动后立即返回 pid，用 process_output 查看输出、stop_process 停止。"
                       "适合跑服务/守护类命令；一次性命令用 run_shell 即可。",
        "parameters": {"type": "object",
                       "properties": {
                           "command": {"type": "string", "description": "要启动的命令（支持 shell 语法）"},
                           "cwd": {"type": "string", "description": "可选：工作目录（相对工作区）"}},
                       "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "process_output",
        "description": "查看后台进程的输出（最近 N 行）与运行状态。wait_seconds 可等待一段时间再取输出。",
        "parameters": {"type": "object",
                       "properties": {
                           "pid": {"type": "integer", "description": "start_process 返回的进程号"},
                           "tail": {"type": "integer", "default": 2000, "description": "返回最近多少行"},
                           "wait_seconds": {"type": "number", "default": 0, "description": "可选：先等待几秒再取输出（如服务启动日志）"}},
                       "required": ["pid"]},
    }},
    {"type": "function", "function": {
        "name": "stop_process",
        "description": "停止后台进程（连同其子进程一并结束）。",
        "parameters": {"type": "object",
                       "properties": {"pid": {"type": "integer", "description": "start_process 返回的进程号"}},
                       "required": ["pid"]},
    }},
    {"type": "function", "function": {
        "name": "list_processes",
        "description": "列出所有正在运行/已结束的后台进程（pid、命令、状态）。",
        "parameters": {"type": "object", "properties": {}},
    }},
    # ---- 编程工具：任务规划 ----
    {"type": "function", "function": {
        "name": "create_todo",
        "description": "为当前任务添加一个待办项（任务清单会展示给用户并在重启后保留）。"
                       "多步骤任务先建 todo 列表再逐步执行。",
        "parameters": {"type": "object",
                       "properties": {
                           "title": {"type": "string", "description": "待办标题（一句话）"},
                           "description": {"type": "string", "description": "可选：补充说明"}},
                       "required": ["title"]},
    }},
    {"type": "function", "function": {
        "name": "update_todo",
        "description": "更新待办项状态：pending / in_progress / completed / failed / cancelled。"
                       "任务每完成一个步骤就更新一次。",
        "parameters": {"type": "object",
                       "properties": {
                           "id": {"type": "integer", "description": "待办项编号（create_todo 返回的 id）"},
                           "status": {"type": "string",
                                      "enum": ["pending", "in_progress", "completed", "failed", "cancelled"]}},
                       "required": ["id", "status"]},
    }},
    {"type": "function", "function": {
        "name": "list_todos",
        "description": "查看当前任务清单。",
        "parameters": {"type": "object", "properties": {}},
    }},
    # ---- 编程工具：项目索引 ----
    {"type": "function", "function": {
        "name": "repo_map",
        "description": "生成项目结构摘要：目录树 + 每个文件的行数 + 顶层函数/类符号。"
                       "接触新项目时第一步调用它了解整体结构，再配合 search_text / list_symbols 深入。",
        "parameters": {"type": "object",
                       "properties": {
                           "path": {"type": "string", "description": "可选：子目录（相对工作区，省略为整个工作区）"},
                           "depth": {"type": "integer", "default": 3, "description": "目录深度（1-5）"},
                           "max_entries": {"type": "integer", "default": 200, "description": "最多返回条目数"}},
                       "required": []},
    }},
]

AGENT_SYSTEM_SUFFIX = (
    "\n\n你是 PC Agent，一个可以控制用户电脑的智能体。你可以通过工具操作电脑、编写和修改代码。\n"
    "编程工作流：\n"
    "1. 先 repo_map 了解项目结构，search_text / glob_files 定位相关代码，list_symbols 查看文件内部结构。\n"
    "2. 修改用 replace_text 小步替换（系统会展示 diff 请用户确认）；新文件用 create_file。\n"
    "3. 用 git_status / git_diff 自查改动，完成一个阶段后用 git_commit 提交（需用户确认）。\n"
    "4. 多步骤长任务用 create_todo 先列计划，每完成一步用 update_todo 更新状态。\n"
    "5. 后台服务（dev server 等）用 start_process 启动，process_output 看输出。\n"
    "6. 运行测试/一次性命令用 run_code 或 run_shell；修改代码后主动运行相关测试验证。\n"
    "7. 操作要谨慎，只执行用户明确要求的动作；不确定时先询问用户。\n"
    "8. 覆盖文件、修改代码、提交 git、执行系统级写操作等敏感动作系统会弹出确认，请尊重用户的选择。\n"
    "9. 遇到关键抉择（如删除内容、安装软件、修改配置、二选一路径）时，"
    "先用文字列出选项让用户选择，等待用户答复后再行动。\n"
    "10. 完成任务后，用简短的中文总结你做了什么。\n"
    "11. 用户要求停止或动作可能造成损害时，调用 stop 工具并告知用户。\n"
    "12. 用户发来寒暄或状态询问（如「你还在吗」「在吗」「你好」）时，直接简短回答，"
    "不要调用任何工具，不要执行命令。"
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


def _iter_workspace_files(workspace: Path, root: Path | None = None, max_files: int = 5000):
    """惰性遍历工作区文件（os.walk 逐目录，不整树载入内存）：跳过 SKIP_DIRS 与隐藏目录。
    yield (绝对路径, 相对工作区的 posix 路径)。"""
    root = root or workspace
    ws_res = workspace.resolve()
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # 剪枝：隐藏目录与已知构建目录不进子目录
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            abs_p = Path(dirpath) / fn
            yield abs_p, abs_p.relative_to(ws_res).as_posix()
            count += 1
            if count >= max_files:
                return


_SYMBOL_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)(.*)$")


def _extract_symbols(text: str, limit: int = MAX_SYMBOLS_PER_FILE) -> list[str]:
    """提取文本中的 def/class 定义，返回 ["行号: 名字 签名", ...]（只扫前 2000 行）。"""
    syms = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if lineno > 2000:
            break
        m = _SYMBOL_RE.match(line)
        if m:
            tail = (m.group(2) or "").strip()
            sig = tail.split("#")[0].strip()[:80] if tail else ""
            syms.append(f"{lineno}: {m.group(1)}{' ' + sig if sig else ''}")
        if len(syms) >= limit:
            break
    return syms


def _extract_symbols_file(abs_p: Path) -> list[str]:
    """提取单文件的符号表；二进制 / 超限 / 不可读文件返回空。"""
    try:
        if abs_p.stat().st_size > MAX_SYMBOLS_FILE_SIZE:
            return []
        text = abs_p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if "\x00" in text[:4096]:
        return []
    return _extract_symbols(text)


def _find_git_root(workspace: Path, rel: str = "") -> Path | None:
    """从工作区（或 rel 子目录）向上找 Git 仓库根；仓库根必须仍位于工作区内。"""
    start = workspace if not rel else _safe_join(workspace, rel)
    if start is None:
        return None
    ws_res = workspace.resolve()
    cur = start if start.is_dir() else start.parent
    for p in [cur, *cur.parents]:
        if (p / ".git").exists():
            try:
                p.resolve().relative_to(ws_res)
                return p
            except ValueError:
                return None    # 仓库根在工作区外：拒绝操作
    return None


def _run_git(root: Path, *args: str, timeout: int = 20) -> tuple[bool, str]:
    """执行 git 命令（参数数组形式，无 shell 注入）。返回 (ok, 输出/错误)。"""
    try:
        proc = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=timeout, shell=False)
    except FileNotFoundError:
        return False, "未找到 git 命令（请先安装 git）"
    except subprocess.TimeoutExpired:
        return False, f"git 执行超时（>{timeout}s）"
    except Exception as exc:
        return False, f"git 执行失败：{exc}"
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return False, (proc.stderr or out).strip()[:500] or f"git 退出码 {proc.returncode}"
    return True, out


# ---- 后台进程管理 ----
_process_lock = threading.Lock()
_processes: dict[int, dict] = {}   # pid -> {"proc", "cmd", "started", "lines"}


def _process_reader(proc, lines: deque) -> None:
    """后台线程：逐行缓冲进程输出（环形，防内存膨胀）。bytes 解码容错，兼容任意编码输出。"""
    for raw in proc.stdout:
        lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))


def _cleanup_dead_processes() -> None:
    """移除已退出的进程条目。调用方须持有 _process_lock。"""
    dead = [pid for pid, e in _processes.items() if e["proc"].poll() is not None]
    for pid in dead:
        _processes.pop(pid, None)


def _start_process_impl(command: str, cwd: Path) -> tuple[bool, str]:
    """后台启动进程。POSIX 独立进程组 / Windows 独立控制台组，停止时整组击杀。"""
    kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                  shell=True, cwd=str(cwd))
    if sys.platform != "win32":
        kwargs["executable"] = "/bin/bash"
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(command, **kwargs)
    lines: deque = deque(maxlen=PROCESS_OUTPUT_LINES)
    threading.Thread(target=_process_reader, args=(proc, lines), daemon=True).start()
    with _process_lock:
        _cleanup_dead_processes()
        if len(_processes) >= MAX_PROCESSES:
            proc.kill()
            return False, f"后台进程数已达上限 {MAX_PROCESSES}，请先 stop_process 清理"
        _processes[proc.pid] = {"proc": proc, "cmd": command[:200],
                                "started": time.strftime("%H:%M:%S"), "lines": lines}
    return True, json.dumps({"pid": proc.pid, "command": command[:200],
                             "started": _processes[proc.pid]["started"]}, ensure_ascii=False)


def _kill_process_tree(proc) -> None:
    """杀整个进程组/控制台组（shell 启动的子进程一并结束），防止残留。"""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, text=True)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except Exception:
                pass


def _run_subprocess(cmd, cwd, timeout: float, shell: bool = True) -> tuple[int, str, str, bool]:
    """统一子进程执行（run_shell / run_code 共用）。

    超时处理：杀整个进程组（防子进程残留），并带回超时前的完整部分输出——
    让模型能判断命令是「真慢（有进度）」还是「卡死（无输出）」。

    返回 (returncode, stdout, stderr, timed_out)；输出统一 bytes 解码容错。
    """
    kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  cwd=str(cwd), shell=shell)
    if sys.platform != "win32":
        kwargs["executable"] = "/bin/bash" if shell else None
        kwargs["start_new_session"] = True          # 独立进程组，超时可整组击杀
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        out, err = proc.communicate()               # 杀完后再取剩余输出
        return proc.returncode, out, err, True


def _decode_out(data) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data or ""


def _stop_process_impl(pid: int) -> tuple[bool, str]:
    with _process_lock:
        entry = _processes.get(pid)
        if entry is None:
            return False, f"进程不存在：{pid}（可用 list_processes 查看）"
        proc = entry["proc"]
    if proc.poll() is None:
        _kill_process_tree(proc)
    return True, json.dumps({"pid": pid, "stopped": True}, ensure_ascii=False)


def _process_output_impl(pid: int, tail: int, wait_seconds: float) -> tuple[bool, str]:
    with _process_lock:
        entry = _processes.get(pid)
        if entry is None:
            return False, f"进程不存在：{pid}（可用 list_processes 查看）"
        proc = entry["proc"]
        lines = list(entry["lines"])
    if wait_seconds > 0 and proc.poll() is None:
        time.sleep(wait_seconds)
        with _process_lock:
            entry = _processes.get(pid)
            if entry is not None:
                lines = list(entry["lines"])
    running = proc.poll() is None
    out = "\n".join(lines[-tail:])
    if len(out) > PROCESS_OUTPUT_LIMIT:
        out = out[-PROCESS_OUTPUT_LIMIT:]
    return True, json.dumps({"pid": pid, "running": running,
                             "exit_code": None if running else proc.returncode,
                             "output": out}, ensure_ascii=False)


# ---- 任务清单（todo）----
_todo_lock = threading.Lock()
_todos: list[dict] = []
_todo_counter = itertools.count(1)
_todos_loaded = False


def _todo_file() -> Path:
    return _get_workspace() / ".pcagent" / "todos.json"


def _load_todos() -> None:
    global _todos, _todo_counter, _todos_loaded
    _todos_loaded = True
    try:
        p = _todo_file()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            _todos = [dict(t) for t in data.get("todos", [])]
            if _todos:
                _todo_counter = itertools.count(max(int(t.get("id", 0)) for t in _todos) + 1)
    except Exception:
        _todos = []


def _save_todos() -> None:
    try:
        p = _todo_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"todos": _todos}, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    except OSError as exc:
        log.warning("todo 持久化失败：%s", exc)


def _todos_snapshot() -> dict:
    if not _todos_loaded:
        _load_todos()
    with _todo_lock:
        return {"todos": [dict(t) for t in _todos]}


def _todos_system_note() -> str:
    """把当前任务清单追加进 system 提示，实现中断后任务的半恢复。"""
    todos = _todos_snapshot().get("todos", [])
    if not todos:
        return ""
    lines = ["\n\n当前任务清单（用 update_todo 更新状态，新任务用 create_todo 添加）："]
    for t in todos[-MAX_TODOS:]:
        desc = f" — {t.get('description')}" if t.get("description") else ""
        lines.append(f"- [#{t['id']}] {t['status']}: {t['title']}{desc}")
    return "\n".join(lines)


# ---- 会话持久化（权威存储在项目目录 .pcagent/，跟随程序 / U 盘移动，已 gitignore）----
SESSION_MAX = 50               # 会话数上限
SESSION_MAX_MESSAGES = 200     # 单会话消息数上限（超出丢弃最早，防无限膨胀）
SESSION_TITLE_CHARS = 30       # 自动标题长度（取首条用户消息）
_session_lock = threading.Lock()
_sessions: dict[int, dict] = {}  # id -> {"id", "title", "messages": [...], "updated"}
_session_id_counter = itertools.count(1)
_sessions_loaded = False


def _session_file() -> Path:
    """会话文件位置：项目根目录 .pcagent/sessions.json（程序数据，跟着代码/U 盘走）。"""
    return BASE_DIR.parent / ".pcagent" / "sessions.json"


def _load_sessions() -> None:
    global _sessions, _session_id_counter, _sessions_loaded
    _sessions_loaded = True
    try:
        p = _session_file()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            _sessions = {int(k): v for k, v in (data.get("sessions") or {}).items()}
            if _sessions:
                _session_id_counter = itertools.count(max(_sessions) + 1)
    except Exception:
        _sessions = {}


def _save_sessions() -> None:
    """原子写：先写临时文件再 rename，防止写一半崩溃损坏数据。"""
    try:
        p = _session_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sessions": _sessions}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        log.warning("会话持久化失败：%s", exc)


def _ensure_sessions() -> None:
    if not _sessions_loaded:
        _load_sessions()


# ---- 修改回滚（undo）：replace_text / create_file 覆盖前自动备份 ----
BACKUP_MAX = 50            # 备份条目上限（超出丢最老）
_backup_lock = threading.Lock()


def _backup_dir() -> Path:
    return BASE_DIR.parent / ".pcagent" / "backups"


def _backup_index() -> list[dict]:
    """[{id, file, time, backup}] 按时间旧→新。"""
    try:
        p = _backup_dir() / "index.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_backup_index(idx: list) -> None:
    try:
        p = _backup_dir()
        p.mkdir(parents=True, exist_ok=True)
        (p / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    except OSError as exc:
        log.warning("备份清单写入失败：%s", exc)


def _take_backup(workspace: Path, target: Path) -> bool:
    """修改文件前备份原内容（供 undo 恢复）。失败不阻塞修改。"""
    try:
        rel = target.relative_to(workspace).as_posix()
        content = target.read_text(encoding="utf-8", errors="replace")
        with _backup_lock:
            idx = _backup_index()
            bid = (idx[-1]["id"] + 1) if idx else 1
            bdir = _backup_dir() / str(bid)
            bdir.mkdir(parents=True, exist_ok=True)
            (bdir / "content").write_text(content, encoding="utf-8")
            idx.append({"id": bid, "file": rel,
                        "time": time.strftime("%m-%d %H:%M:%S"), "backup": str(bid)})
            if len(idx) > BACKUP_MAX:          # 上限：丢最老
                old = idx.pop(0)
                shutil.rmtree(_backup_dir() / str(old["backup"]), ignore_errors=True)
            _save_backup_index(idx)
        return True
    except Exception:
        return False


def _find_undo_entry(target_file: str = "") -> dict | None:
    """找要撤销的备份条目：指定文件取该文件最近一次，否则全局最近一次。"""
    idx = _backup_index()
    if not idx:
        return None
    if target_file:
        for e in reversed(idx):
            if e["file"] == target_file:
                return e
        return None
    return idx[-1]


# ---- repo 索引缓存 ----
_repo_cache = {"key": "", "time": 0.0, "text": ""}


def _count_lines(abs_p: Path) -> int:
    try:
        with abs_p.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _file_summary(abs_p: Path, max_lines: int = 2000) -> tuple[int, list[str]]:
    """一次读取返回 (总行数, 符号表)：只收集前 max_lines 行做符号提取，避免全文件驻留。"""
    total = 0
    head: list[str] = []
    try:
        if abs_p.stat().st_size > MAX_SYMBOLS_FILE_SIZE:
            return 0, []
        with abs_p.open("r", encoding="utf-8", errors="replace") as f:
            if "\x00" in f.read(4096):     # 二进制文件跳过
                return 0, []
            f.seek(0)
            for line in f:
                total += 1
                if len(head) < max_lines:
                    head.append(line)
    except OSError:
        return 0, []
    return total, _extract_symbols("".join(head))


def _repo_map_text(workspace: Path, rel: str, depth: int, max_entries: int) -> str:
    """生成项目结构摘要（目录树 + 文件行数 + 顶层符号），30 秒 TTL 缓存。"""
    key = f"{rel}|{depth}|{max_entries}"
    now = time.monotonic()
    if _repo_cache["key"] == key and now - _repo_cache["time"] < REPO_MAP_TTL:
        return _repo_cache["text"]
    root = workspace if not rel else _safe_join(workspace, rel)
    if root is None or not root.is_dir():
        return f"目录不存在：{rel or '.'}"
    out = []
    count = 0
    for abs_p, rel_p in _iter_workspace_files(workspace, root=root):
        level = len(rel_p.split("/"))
        if level > depth:
            continue
        indent = "  " * (level - 1)
        n, syms = _file_summary(abs_p)
        suffix = f"  [{', '.join(syms[:6])}]" if syms else ""
        out.append(f"{indent}{abs_p.name}  {n}行{suffix}")
        count += 1
        if count >= max_entries:
            out.append("...(条目过多已截断，可缩小范围)")
            break
    text = "\n".join(out) if out else f"（空目录：{rel or '.'}）"
    _repo_cache.update(key=key, time=now, text=text)
    return text


def _make_replace_diff(args: dict) -> str | None:
    """为 replace_text 生成确认用的 unified diff；文件缺失/无匹配时返回 None。"""
    workspace = _get_workspace()
    target = _safe_join(workspace, args.get("file", ""))
    old = args.get("old") or ""
    if target is None or not target.is_file() or not old:
        return None
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    count = text.count(old)
    if count == 0:
        return None
    occ = max(1, int(args.get("occurrence") or 1))
    new = args.get("new") or ""
    parts = text.split(old)
    if occ > len(parts) - 1:
        return None
    new_text = old.join(parts[:occ]) + new + old.join(parts[occ:])
    diff = "".join(difflib.unified_diff(
        text.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile=f"a/{args['file']}", tofile=f"b/{args['file']}", n=2))
    return diff[:REPLACE_DIFF_CHARS] or None


def _safe_int(value, default: int, lo: int, hi: int) -> int:
    """工具参数容错：模型偶发传错类型/非法值时回退默认，而不是抛异常。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(v, lo), hi)


def _safe_float(value, default: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(v, lo), hi)


def _agent_tools() -> list[dict]:
    """按运行模式返回可用工具：隔离模式只保留文件类工具。
    MCP 外部工具（如 GitHub）与屏幕无关，隔离模式同样保留。"""
    if ISOLATED:
        tools = [t for t in AGENT_TOOLS if t["function"]["name"] in _FILE_TOOLS]
    else:
        tools = list(AGENT_TOOLS)
    mcp = _ensure_mcp()
    if mcp is not None:
        tools.extend(mcp.all_tools())     # MCP server 工具动态并入（mcp_<server>_<tool>）
    return tools


# ---- MCP 客户端（外部工具接入，mcp_config.json 配置）----
_mcp_manager = None


def _load_mcp_config() -> dict:
    """读取 mcp_config.json 的 servers 段（含 token，已 gitignore；示例见 mcp_config.example.json）。"""
    try:
        p = BASE_DIR.parent / "mcp_config.json"
        if p.exists():
            cfg = json.loads(p.read_text(encoding="utf-8"))
            return cfg.get("servers") or {}
    except Exception as exc:
        log.warning("mcp_config.json 解析失败：%s", exc)
    return {}


def _ensure_mcp():
    """惰性初始化 MCP 管理器（首次访问工具列表时连接各 server）。
    PCAGENT_DISABLE_MCP=1 时跳过（测试环境用，避免真实连接外部 server）。"""
    global _mcp_manager
    if _mcp_manager is None and os.environ.get("PCAGENT_DISABLE_MCP"):
        return None
    if _mcp_manager is None:
        from mcp_client import McpManager
        _mcp_manager = McpManager(_load_mcp_config())
        _mcp_manager.start()
    return _mcp_manager


def _is_readonly_shell(command: str) -> bool:
    """判断 shell 命令是否只读（免确认）。重定向到真实文件视为写操作。
    平台相关：Windows(cmd) 用 Windows 白名单（dir/more/findstr 等）。"""
    if re.search(r">\s*(?!/?dev/null\b)\S", command):   # > file 或 >> file（排除 >/dev/null、2>/dev/null）
        return False
    if sys.platform == "win32":
        return bool(READONLY_SHELL_WIN.match(command))
    return bool(READONLY_SHELL.match(command))


def _needs_confirm(name: str, args: dict) -> bool:
    """判断工具调用是否需要用户确认。"""
    if name not in CONFIRM_TOOLS:
        return False
    if name == "create_file":
        # 覆盖已存在的文件才需要确认（新建文件不需要）
        workspace = _get_workspace()
        target = _safe_join(workspace, args.get("path", ""))
        return target is not None and target.exists()
    if name in ("replace_text", "git_commit", "start_process", "undo"):
        # 修改文件 / 提交 git / 启动后台进程 / 撤销修改：一律确认
        return True
    if name == "run_shell":
        command = (args.get("command") or "").strip()
        return bool(command) and not _is_readonly_shell(command)
    return False


def _confirm_question(name: str, args: dict) -> tuple[str, str | None]:
    """生成 (确认问题, diff 文本)。diff 为空时前端不展示 diff 区域。"""
    if name == "create_file":
        return f"要覆盖已存在的文件 `{args.get('path')}` 吗？", None
    if name == "run_shell":
        return f"要执行系统命令 `{(args.get('command') or '')[:80]}` 吗？", None
    if name == "replace_text":
        diff = _make_replace_diff(args)
        file = args.get("file")
        return f"要修改文件 `{file}`（把指定内容替换为新的内容）吗？", diff
    if name == "undo":
        entry = _find_undo_entry((args.get("file") or "").strip())
        if entry is None:
            return "没有可撤销的修改记录", None
        # diff：备份内容 vs 当前内容
        workspace = _get_workspace()
        cur = _safe_join(workspace, entry["file"])
        diff = None
        if cur is not None and cur.is_file():
            try:
                old_text = (_backup_dir() / str(entry["backup"]) / "content").read_text(
                    encoding="utf-8", errors="replace")
                new_text = cur.read_text(encoding="utf-8", errors="replace")
                diff = "".join(difflib.unified_diff(
                    new_text.splitlines(keepends=True), old_text.splitlines(keepends=True),
                    fromfile=f"a/{entry['file']}", tofile=f"b/{entry['file']}（{entry['time']} 备份）",
                    n=2))[:REPLACE_DIFF_CHARS]
            except OSError:
                pass
        return (f"要撤销对 `{entry['file']}` 的修改（恢复到 {entry['time']} 的备份）吗？", diff)
    if name == "git_commit":
        workspace = _get_workspace()
        root = _find_git_root(workspace, args.get("path", ""))
        diff = None
        if root is not None:
            ok, out = _run_git(root, "diff", "--stat")
            if ok and out:
                diff = out[:REPLACE_DIFF_CHARS]
        msg = (args.get("message") or "")[:60]
        return f"要提交 Git 变更吗？提交信息：`{msg}`", diff
    if name == "start_process":
        return f"要在后台启动进程 `{(args.get('command') or '')[:80]}` 吗？", None
    return "确认执行该操作吗？", None


def _wait_confirm(request_id: str, timeout: float = CONFIRM_TIMEOUT) -> str | None:
    """等待用户对确认请求的响应；超时返回 None（视为拒绝）。"""
    ev = threading.Event()
    with _confirm_lock:
        _confirm_table[request_id] = {"event": ev, "choice": None}
    ev.wait(timeout)
    with _confirm_lock:
        entry = _confirm_table.pop(request_id, None)
        return entry["choice"] if entry else None


class AskResponse(BaseModel):
    request_id: str = Field(..., description="确认请求 ID")
    choice: str = Field(..., description="用户选择：yes / no 或选项文本")


@app.post("/api/v1/agent/respond", summary="响应 Agent 的确认请求")
async def agent_respond(req: AskResponse) -> dict:
    with _confirm_lock:
        entry = _confirm_table.get(req.request_id)
        if entry is None:
            raise HTTPException(404, "确认请求不存在或已超时（默认按拒绝处理）")
        entry["choice"] = req.choice
        entry["event"].set()
    log.info("confirm %s -> %s", req.request_id, req.choice)
    return {"ok": True, "choice": req.choice}


class SessionAppend(BaseModel):
    messages: list[dict] = Field(..., description="新增消息（增量追加，如 [user, assistant]）")
    title: str | None = Field(default=None, description="可选：自定义标题（省略则自动取首条用户消息）")


@app.get("/api/v1/sessions", summary="会话列表（默认仅摘要；full=1 时含完整消息）")
async def sessions_list(full: int = 0) -> dict:
    """默认只返回摘要（id/标题/消息数），避免大量会话时全量传输。
    需要完整消息用 full=1 或逐个 GET /sessions/{id}（按需加载）。"""
    _ensure_sessions()
    with _session_lock:
        items = []
        for s in _sessions.values():
            item = {"id": s["id"], "title": s.get("title", ""),
                    "message_count": len(s.get("messages", [])),
                    "updated": s.get("updated", "")}
            if full:
                item["messages"] = s.get("messages", [])
            items.append(item)
    return {"ok": True, "sessions": sorted(items, key=lambda s: s["id"])}


@app.post("/api/v1/sessions", summary="创建空会话")
async def session_create() -> dict:
    _ensure_sessions()
    with _session_lock:
        if len(_sessions) >= SESSION_MAX:
            raise HTTPException(409, f"会话数已达上限 {SESSION_MAX}，请先删除旧会话")
        sid = next(_session_id_counter)
        _sessions[sid] = {"id": sid, "title": "", "messages": [],
                          "updated": time.strftime("%m-%d %H:%M")}
        _save_sessions()
    log.info("session created: #%d", sid)
    return {"ok": True, "id": sid}


@app.get("/api/v1/sessions/{sid}", summary="读取单个会话")
async def session_get(sid: int) -> dict:
    _ensure_sessions()
    with _session_lock:
        s = _sessions.get(sid)
        if s is None:
            raise HTTPException(404, f"会话不存在：{sid}")
        return {"ok": True, "session": s}


@app.post("/api/v1/sessions/{sid}/messages", summary="向会话追加消息（增量）")
async def session_append(sid: int, req: SessionAppend) -> dict:
    _ensure_sessions()
    if not req.messages:
        raise HTTPException(422, "messages 不能为空")
    with _session_lock:
        s = _sessions.get(sid)
        if s is None:
            raise HTTPException(404, f"会话不存在：{sid}")
        msgs = s.setdefault("messages", [])
        for m in req.messages:
            if m.get("role") in ("user", "assistant") and m.get("content"):
                msgs.append({"role": m["role"], "content": m["content"]})
        # 上限：超出丢弃最早消息
        if len(msgs) > SESSION_MAX_MESSAGES:
            del msgs[:len(msgs) - SESSION_MAX_MESSAGES]
        # 自动标题：首条用户消息前 N 字
        if not s.get("title"):
            for m in msgs:
                if m.get("role") == "user":
                    s["title"] = (m.get("content") or "").strip().replace("\n", " ")[:SESSION_TITLE_CHARS]
                    break
        if req.title:
            s["title"] = req.title[:60]
        s["updated"] = time.strftime("%m-%d %H:%M")
        _save_sessions()
        return {"ok": True, "session": s}


@app.delete("/api/v1/sessions/{sid}", summary="删除会话")
async def session_delete(sid: int) -> dict:
    _ensure_sessions()
    with _session_lock:
        if sid not in _sessions:
            raise HTTPException(404, f"会话不存在：{sid}")
        del _sessions[sid]
        _save_sessions()
    log.info("session deleted: #%d", sid)
    return {"ok": True, "deleted": sid}


@app.delete("/api/v1/sessions/{sid}/messages", summary="清空会话消息（保留会话，用于 /clear）")
async def session_clear(sid: int) -> dict:
    _ensure_sessions()
    with _session_lock:
        s = _sessions.get(sid)
        if s is None:
            raise HTTPException(404, f"会话不存在：{sid}")
        s["messages"] = []
        s["title"] = ""
        s["updated"] = time.strftime("%m-%d %H:%M")
        _save_sessions()
    return {"ok": True, "cleared": sid}


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
            if target.exists():
                _take_backup(workspace, target)      # 覆盖前自动备份（供 undo 恢复）
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
                rc, out, err, timed_out = _run_subprocess(
                    [sys.executable, "-c", code], workspace, RUN_CODE_TIMEOUT, shell=False)
            except FileNotFoundError:
                return False, "Python 解释器不存在"
            except Exception as exc:
                return False, f"执行失败：{exc}"
            stdout = _decode_out(out)[:RUN_CODE_OUTPUT_LIMIT]
            stderr = _decode_out(err)[:RUN_CODE_OUTPUT_LIMIT]
            if timed_out:
                # 超时即 debug：带回超时前的部分输出，让模型判断是慢还是卡死
                return False, json.dumps({
                    "error": f"执行超时（>{RUN_CODE_TIMEOUT}s），进程组已强制终止",
                    "exit_code": rc, "partial_stdout": stdout, "partial_stderr": stderr,
                    "hint": "以上是超时前产生的输出。有持续输出=代码还在跑（如循环/下载），"
                            "可缩短任务或改用 start_process 后台执行；无输出=可能卡死（如阻塞等待输入）。",
                }, ensure_ascii=False)
            if rc != 0:
                return False, json.dumps({"exit_code": rc, "stderr": stderr}, ensure_ascii=False)
            return True, json.dumps({"exit_code": 0, "stdout": stdout}, ensure_ascii=False)
        if name == "run_shell":
            command = (args.get("command") or "").strip()
            if not command:
                return False, "没有提供命令"
            if len(command) > RUN_SHELL_MAX_CMD:
                return False, f"命令过长（>{RUN_SHELL_MAX_CMD} 字符）"
            # 危险命令黑名单
            for pat in DANGEROUS_PATTERNS:
                if re.search(pat, command):
                    return False, f"危险命令已被拦截：{command[:80]}（破坏性操作禁止执行）"
            cwd = (args.get("cwd") or "").strip() or str(_get_workspace())
            try:
                rc, out, err, timed_out = _run_subprocess(
                    command, cwd, RUN_SHELL_TIMEOUT, shell=True)
            except FileNotFoundError:
                return False, f"目录不存在：{cwd}"
            except Exception as exc:
                return False, f"执行失败：{exc}"
            stdout = _decode_out(out)[:RUN_SHELL_OUTPUT_LIMIT]
            stderr = _decode_out(err)[:RUN_SHELL_OUTPUT_LIMIT]
            if timed_out:
                # 超时即 debug：部分输出 + 处置建议
                return False, json.dumps({
                    "error": f"执行超时（>{RUN_SHELL_TIMEOUT}s），进程组已强制终止",
                    "exit_code": rc, "partial_stdout": stdout, "partial_stderr": stderr,
                    "hint": "以上是超时前产生的输出。有持续输出=命令在正常推进（编译/下载/安装），"
                            "可拆分步骤执行或改用 start_process 后台运行（process_output 分次查看）；"
                            "无输出且无进度=命令卡死（如等待交互输入），请检查命令是否挂起。",
                }, ensure_ascii=False)
            if rc != 0:
                return False, json.dumps({"exit_code": rc,
                                          "stderr": stderr or stdout}, ensure_ascii=False)
            return True, json.dumps({"exit_code": 0, "stdout": stdout}, ensure_ascii=False)
        # ================= 编程工具：检索 =================
        if name == "search_text":
            workspace = _get_workspace()
            pattern = args.get("pattern") or ""
            if not pattern:
                return False, "没有提供搜索 pattern"
            try:
                rx = re.compile(pattern)
            except re.error:
                rx = re.compile(re.escape(pattern))
            rel = (args.get("path") or "").strip()
            root = workspace if not rel else _safe_join(workspace, rel)
            if root is None:
                return False, "非法路径：必须是工作区内的相对路径"
            if not root.exists():
                return False, f"路径不存在：{rel or '.'}"
            file_pat = (args.get("file_pattern") or "").strip()
            max_results = _safe_int(args.get("max_results"), 20, 1, SEARCH_MAX_RESULTS)
            ctx = _safe_int(args.get("context_lines"), SEARCH_CONTEXT_LINES, 0, 5)
            hits = []   # (rel, lineno, text, before, after)
            for abs_p, rel_p in _iter_workspace_files(workspace, root=root):
                if file_pat and not fnmatch.fnmatch(abs_p.name, file_pat):
                    continue
                try:
                    if abs_p.stat().st_size > SEARCH_MAX_FILE_SIZE:
                        continue
                    with abs_p.open("r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                except OSError:
                    continue
                if "\x00" in "".join(lines[:10]):   # 二进制文件跳过（只查头部，省拼接）
                    continue
                for i, line in enumerate(lines):
                    if rx.search(line):
                        before = [l.rstrip("\r\n") for l in lines[max(0, i - ctx):i]]
                        after = [l.rstrip("\r\n") for l in lines[i + 1:i + 1 + ctx]]
                        hits.append((rel_p, i + 1, line.rstrip("\r\n")[:SEARCH_MAX_LINE_LEN],
                                     before, after))
                        if len(hits) >= max_results:
                            break
                if len(hits) >= max_results:
                    break
            if not hits:
                return True, json.dumps({"matches": 0, "pattern": pattern[:100]},
                                        ensure_ascii=False)
            out = []
            for rel_p, lineno, text, before, after in hits:
                out.append(f"{rel_p}:{lineno}: {text}")
                out.extend(f"  {b}" for b in before)
                out.extend(f"  {a}" for a in after)
            text_out = "\n".join(out)
            if len(text_out) > SEARCH_OUTPUT_LIMIT:
                text_out = text_out[:SEARCH_OUTPUT_LIMIT] + "\n...(结果过多已截断)"
            return True, json.dumps({"matches": len(hits), "pattern": pattern[:100],
                                     "results": text_out}, ensure_ascii=False)
        if name == "glob_files":
            workspace = _get_workspace()
            pattern = (args.get("pattern") or "").strip()
            if not pattern:
                return False, "没有提供文件名模式（如 *.py）"
            max_results = _safe_int(args.get("max_results"), 50, 1, 200)
            files = []
            for abs_p, rel_p in _iter_workspace_files(workspace):
                if not (fnmatch.fnmatch(abs_p.name, pattern) or fnmatch.fnmatch(rel_p, pattern)):
                    continue
                try:
                    size = abs_p.stat().st_size
                    lines = _count_lines(abs_p)
                except OSError:
                    size, lines = 0, 0
                files.append({"path": rel_p, "size": size, "lines": lines})
                if len(files) >= max_results:
                    break
            return True, json.dumps({"matches": len(files), "files": files},
                                    ensure_ascii=False)
        if name == "list_symbols":
            workspace = _get_workspace()
            target = _safe_join(workspace, args.get("file", ""))
            if target is None:
                return False, "非法路径：必须是工作区内的相对路径"
            if not target.is_file():
                return False, f"文件不存在：{args.get('file')}"
            syms = _extract_symbols_file(target)
            return True, json.dumps({"file": args.get("file"), "symbols": syms},
                                    ensure_ascii=False)
        # ================= 编程工具：精确编辑 =================
        if name == "replace_text":
            workspace = _get_workspace()
            target = _safe_join(workspace, args.get("file", ""))
            if target is None:
                return False, "非法路径：必须是工作区内的相对路径"
            if not target.is_file():
                return False, f"文件不存在：{args.get('file')}"
            if target.stat().st_size > MAX_FILE_SIZE:
                return False, f"文件过大（>{MAX_FILE_SIZE} 字节）"
            old = args.get("old") or ""
            if not old:
                return False, "old 不能为空"
            if len(old) > REPLACE_OLD_MAX:
                return False, f"old 过长（{len(old)} 字符 > 上限 {REPLACE_OLD_MAX}）"
            text = target.read_text(encoding="utf-8", errors="replace")
            count = text.count(old)
            if count == 0:
                return False, ("未找到要替换的内容：old 与文件内容不一致（注意空格/缩进/换行，"
                               "或先用 read_file 查看实际内容）")
            occ = _safe_int(args.get("occurrence"), 1, 1, 10 ** 9)
            if occ > count:
                return False, (f"occurrence={occ} 超出出现次数 {count}；"
                               f"可省略 occurrence 或把 old 写长一点保证唯一")
            new = args.get("new") or ""
            parts = text.split(old)
            new_text = old.join(parts[:occ]) + new + old.join(parts[occ:])
            diff = "".join(difflib.unified_diff(
                text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                fromfile=f"a/{args['file']}", tofile=f"b/{args['file']}", n=1))
            if len(diff) > REPLACE_DIFF_CHARS:
                diff = diff[:REPLACE_DIFF_CHARS] + "\n...(diff 过长已截断)"
            _take_backup(workspace, target)          # 修改前自动备份（供 undo 恢复）
            target.write_text(new_text, encoding="utf-8")
            return True, json.dumps({"file": args.get("file"), "occurrence": occ,
                                     "replacements": count, "diff": diff,
                                     "backup": True}, ensure_ascii=False)
        if name == "undo":
            workspace = _get_workspace()
            entry = _find_undo_entry((args.get("file") or "").strip())
            if entry is None:
                return False, "没有可撤销的修改（replace_text / create_file 覆盖前会自动备份）"
            bdir = _backup_dir() / str(entry["backup"])
            content_path = bdir / "content"
            if not content_path.exists():
                return False, "备份文件缺失，无法撤销"
            target = _safe_join(workspace, entry["file"])
            if target is None:
                return False, "非法路径：备份记录中的路径超出工作区"
            content = content_path.read_text(encoding="utf-8", errors="replace")
            _take_backup(workspace, target)          # 恢复前备份当前状态（撤销可逆）
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            # 移除该备份条目
            with _backup_lock:
                idx = _backup_index()
                if entry in idx:
                    idx.remove(entry)
                    _save_backup_index(idx)
            shutil.rmtree(bdir, ignore_errors=True)
            return True, json.dumps({"restored": entry["file"], "time": entry["time"],
                                     "chars": len(content)}, ensure_ascii=False)
        # ================= 编程工具：Git =================
        if name in ("git_status", "git_diff", "git_log", "git_commit"):
            workspace = _get_workspace()
            root = _find_git_root(workspace, args.get("path", ""))
            if root is None:
                return False, ("工作区内未找到 Git 仓库（仓库根必须位于工作区内）。"
                               "可先用 run_shell 执行 git init 创建")
            rel_root = root.relative_to(workspace).as_posix()
            if name == "git_status":
                ok, out = _run_git(root, "status", "--short", "--branch")
                if not ok:
                    return False, f"git status 失败：{out}"
                return True, json.dumps({"repo": rel_root, "status": out},
                                        ensure_ascii=False)
            if name == "git_diff":
                cmd = ["diff"]
                if args.get("staged"):
                    cmd.append("--cached")
                if args.get("stat"):
                    cmd.append("--stat")
                ok, out = _run_git(root, *cmd)
                if not ok:
                    return False, f"git diff 失败：{out}"
                if not out:
                    return True, json.dumps({"repo": rel_root, "diff": "（无改动）"},
                                            ensure_ascii=False)
                if len(out) > REPLACE_DIFF_CHARS * 2:
                    out = out[:REPLACE_DIFF_CHARS * 2] + "\n...(diff 过长已截断)"
                return True, json.dumps({"repo": rel_root, "diff": out}, ensure_ascii=False)
            if name == "git_log":
                n = _safe_int(args.get("n"), 10, 1, 30)
                ok, out = _run_git(root, "log", "--oneline", f"-{n}")
                if not ok:
                    return False, f"git log 失败：{out}"
                return True, json.dumps({"repo": rel_root, "log": out or "（暂无提交）"},
                                        ensure_ascii=False)
            # git_commit（确认后由 _agent_loop 调用到此处）
            message = (args.get("message") or "").strip()
            if not message:
                return False, "提交信息不能为空"
            if len(message) > 200:
                return False, "提交信息过长（≤200 字符）"
            ok1, err1 = _run_git(root, "add", "-A")
            if not ok1:
                return False, f"git add 失败：{err1}"
            ok2, out2 = _run_git(root, "commit", "-m", message)
            if not ok2:
                return False, f"git commit 失败：{out2}"
            return True, json.dumps({"repo": rel_root, "committed": True,
                                     "message": message, "result": out2},
                                    ensure_ascii=False)
        # ================= 编程工具：后台进程 =================
        if name == "start_process":
            command = (args.get("command") or "").strip()
            if not command:
                return False, "没有提供命令"
            if len(command) > RUN_SHELL_MAX_CMD:
                return False, f"命令过长（>{RUN_SHELL_MAX_CMD} 字符）"
            for pat in DANGEROUS_PATTERNS:
                if re.search(pat, command):
                    return False, f"危险命令已被拦截：{command[:80]}"
            workspace = _get_workspace()
            cwd = workspace
            rel = (args.get("cwd") or "").strip()
            if rel and rel != ".":
                cwd = _safe_join(workspace, rel)
                if cwd is None:
                    return False, "非法路径：必须是工作区内的相对路径"
                if not cwd.is_dir():
                    return False, f"目录不存在：{rel}"
            return _start_process_impl(command, cwd)
        if name == "process_output":
            try:
                pid = int(args.get("pid") or 0)
            except (TypeError, ValueError):
                return False, "pid 必须是整数"
            if pid <= 0:
                return False, "无效 pid"
            tail = _safe_int(args.get("tail"), 2000, 1, 5000)
            wait = _safe_float(args.get("wait_seconds"), 0, 0, PROCESS_WAIT_MAX)
            return _process_output_impl(pid, tail, wait)
        if name == "stop_process":
            try:
                pid = int(args.get("pid") or 0)
            except (TypeError, ValueError):
                return False, "pid 必须是整数"
            return _stop_process_impl(pid)
        if name == "list_processes":
            with _process_lock:
                _cleanup_dead_processes()
                procs = [{"pid": pid, "command": e["cmd"], "started": e["started"],
                          "running": e["proc"].poll() is None,
                          "exit_code": None if e["proc"].poll() is None else e["proc"].returncode}
                         for pid, e in _processes.items()]
            return True, json.dumps({"processes": procs}, ensure_ascii=False)
        # ================= 编程工具：任务规划 =================
        if name == "create_todo":
            title = (args.get("title") or "").strip()
            if not title:
                return False, "title 不能为空"
            if len(title) > 200:
                return False, "title 过长（≤200 字符）"
            _todos_snapshot()   # 确保已从磁盘加载
            with _todo_lock:
                if len(_todos) >= MAX_TODOS:
                    return False, (f"任务清单已满（{MAX_TODOS} 条），"
                                   f"先 update_todo 把旧任务标记 completed/failed")
                item = {"id": next(_todo_counter), "title": title,
                        "description": (args.get("description") or "")[:500],
                        "status": "pending", "created": time.strftime("%H:%M:%S")}
                _todos.append(item)
                _save_todos()
                snapshot = [dict(t) for t in _todos]
            return True, json.dumps({"created": item, "todos": snapshot},
                                    ensure_ascii=False)
        if name == "update_todo":
            try:
                tid = int(args.get("id") or -1)
            except (TypeError, ValueError):
                return False, "id 必须是整数"
            status = (args.get("status") or "").strip()
            if status not in TODO_STATUSES:
                return False, f"无效状态：{status}（可选：{' / '.join(TODO_STATUSES)}）"
            _todos_snapshot()
            with _todo_lock:
                for it in _todos:
                    if it["id"] == tid:
                        it["status"] = status
                        _save_todos()
                        snapshot = [dict(t) for t in _todos]
                        return True, json.dumps({"updated": it, "todos": snapshot},
                                                ensure_ascii=False)
            return False, f"todo 不存在：{tid}"
        if name == "list_todos":
            return True, json.dumps(_todos_snapshot(), ensure_ascii=False)
        # ================= 编程工具：项目索引 =================
        if name == "repo_map":
            workspace = _get_workspace()
            rel = (args.get("path") or "").strip()
            depth = _safe_int(args.get("depth"), 3, 1, REPO_MAP_MAX_DEPTH)
            max_entries = _safe_int(args.get("max_entries"), 200, 10, REPO_MAP_MAX_ENTRIES)
            return True, _repo_map_text(workspace, rel, depth, max_entries)
        if name in ("click", "type_text", "press_key"):
            code, data = _call_daemon("POST", "/api/v1/execute", {"action": name, **args})
            if code == 200:
                return True, json.dumps({"ok": True}, ensure_ascii=False)
            return False, f"执行失败：{data.get('detail', code)}"
        if name.startswith("mcp_"):
            # MCP 外部工具转发（mcp_<server>_<tool>）
            mcp = _ensure_mcp()
            if mcp is None or not mcp.conns:
                return False, "MCP 未配置（mcp_config.json 为空）"
            return mcp.call(name, arguments)
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
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            q.put(("tool_call", {"id": tc["id"], "name": fn["name"],
                                 "arguments": fn["arguments"],
                                 "step": step, "max_steps": MAX_TOOL_STEPS}))
            # ---- 按问询模式决定处理方式：allow 直接执行 / ask 确认 / deny 拒绝 ----
            policy = _confirm_policy(fn["name"], args)
            if policy == "deny":
                ok = False
                result = (f"当前为只读模式（query），操作 {fn['name']} 已被拒绝；"
                          f"如需执行请先切换到其他问询模式")
            elif policy == "ask":
                ask_id = f"ask-{next(_confirm_counter)}"
                question, diff = _confirm_question(fn["name"], args)
                q.put(("ask", {"id": ask_id, "name": fn["name"],
                               "arguments": fn["arguments"],
                               "question": question,
                               "options": ["yes", "no"], "diff": diff}))
                choice = _wait_confirm(ask_id)
                if choice != "yes":
                    log.info("tool %s rejected by user (%s)", fn["name"], choice)
                    result = "用户拒绝了该操作，请勿执行；可询问用户或改用其他方式"
                    ok = False
                else:
                    ok, result = _execute_tool(fn["name"], fn["arguments"])
            else:
                ok, result = _execute_tool(fn["name"], fn["arguments"])
            # 工具结果截断：避免大结果（read_file 等）撑爆上下文
            if len(result) > MAX_TOOL_RESULT_CHARS:
                result = result[:MAX_TOOL_RESULT_CHARS] + "\n...(结果过长已截断)"
            q.put(("tool_result", {"id": tc["id"], "ok": ok, "result": result}))
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            # 任务清单变化：推送 todo_update 事件（前端刷新任务面板）
            if fn["name"] in ("create_todo", "update_todo"):
                q.put(("todo_update", _todos_snapshot()))
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
            elif kind == "ask":
                yield f"event: ask\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            elif kind == "todo_update":
                yield f"event: todo_update\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
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
                # 注入当前任务清单：中断后模型能感知进度（半恢复）
                todo_note = _todos_system_note()
                if todo_note:
                    m["content"] += todo_note
                break
        else:
            messages.insert(0, {"role": "system",
                                "content": AGENT_SYSTEM_SUFFIX.lstrip() + _todos_system_note()})
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
