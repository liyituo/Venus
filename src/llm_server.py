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
import hashlib
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
import uuid
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException
# 工具权限元数据与判定 / MCP 接入层（显式导入：import * 不会带入下划线符号；
# 以下名字是模块级 re-export，测试通过模块属性访问，必须保留）
from security_policy import (  # noqa: E402,F401
    QUERY_TOOLS, RUN_SHELL_MAX_CMD,
    _SCREEN_TOOLS, _FILE_TOOLS, _current_confirm_mode,
    _is_query_tool, _confirm_policy, _is_readonly_shell, _is_readonly_mcp,
    _needs_confirm,
)
from mcp_manager import (  # noqa: E402,F401
    _load_mcp_config,
)
# R3：Token 优化基础模块（工具结果压缩 / Provider 能力 / 提示词缓存）
from tool_result_reducer import ResultStore, reduce_tool_result  # noqa: E402
from provider_capabilities import build_payload as _build_payload_caps  # noqa: E402
from provider_capabilities import load_overrides_from_config as _load_provider_overrides  # noqa: E402
from prompt_cache import PromptCacheManager  # noqa: E402
from subagent_router import should_delegate, RISK_LOW, RISK_MEDIUM  # noqa: E402
# R4：记忆系统（L0-L3 + Skill + CodeGraph）——只在需要时 import，失败不影响主流程
import agent_memory as _agent_memory  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR.parent / "chat_config.json"
APP_VERSION = "0.8.0"       # 系统版本（health 端点返回，前端可展示）
UPSTREAM_TIMEOUT = 180  # 模型生成可能较慢（reasoner 更慢）
DAEMON_BASE = "http://127.0.0.1:8000"   # 屏幕控制 daemon（app.py）

# ---- R3：全局工具结果存储（LRU）与提示词缓存管理器 ----
_result_store = ResultStore()
_prompt_cache = PromptCacheManager()
# 结果压缩上限：工具结果回传模型的最大长度（head/tail/error 分区保留）
MAX_TOOL_RESULT_CHARS = 800

# ---- R4：记忆系统（有界队列 + 单 worker；失败静默不影响主流程）----
_memory_jobs = _agent_memory.MemoryJobQueue()
_memory_worker = _agent_memory.MemoryWorker(_memory_jobs, lambda job: None)  # handler 后置
_memory_worker.start()


def _dynamic_memory_message(last_user: str, workspace: str = "") -> str | None:
    """组装动态记忆上下文（独立 system 消息，不并入稳定前缀）。

    内容：L3 画像注入版 + 当前消息召回的 L1 top3（去重）。
    为空/失败返回 None（不注入）。
    """
    try:
        if not _memory_enabled():
            return None
        if not (last_user and last_user.strip()):
            return None
        parts: list[str] = []
        profile_text = _agent_memory.profile_inject_text()
        if profile_text:
            parts.append("用户画像：" + profile_text)
        hits = _agent_memory.recall_memories(last_user, top_k=3,
                                             workspace_id=workspace)
        if hits:
            seen: set[str] = set()
            lines = []
            for h in hits:
                c = str(h.get("content") or "").strip()
                if c and c not in seen:
                    seen.add(c)
                    lines.append(f"- {c}")
            if lines:
                parts.append("相关记忆：\n" + "\n".join(lines))
        if not parts:
            return None
        return ("（动态记忆上下文，与对话正文独立；如与本轮用户要求冲突，以本轮为准）\n"
                + "\n".join(parts))
    except Exception:
        return None


def _memory_enabled() -> bool:
    """记忆系统开关：chat_config.json 的 memory_enabled（默认 true）。"""
    try:
        return bool(load_config().get("memory_enabled", True))
    except Exception:
        return True


def _submit_memory_record(record: dict, status: str | None = None) -> None:
    """把 AgentRunRecord 提交给 MemoryWorker（非阻塞，失败静默）。

    status 非 None 时兜底覆盖（loop 异常退出未填充状态的情况）。
    """
    try:
        if not _memory_enabled():
            return
        if status is not None:
            record["status"] = status
        if not record.get("status"):
            record["status"] = "failed"
        record.setdefault("finished_at", time.time())
        _memory_jobs.put(record)
    except Exception:
        pass


def _llm_extract_memories(user_texts: list[str], api_url: str, headers: dict,
                          model: str) -> list[dict] | None:
    """LLM 兜底提取（配置 llm_memory_extract=true 时启用）：把规则候选
    精化为结构化 JSON 列表；失败返回 None（调用方回退规则结果）。

    关闭思考（off 模式）+ 校验；绝不保存隐藏推理——只提取显式陈述。
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": ("你是记忆提取器。从用户消息中提取值得长期记住的偏好/约束/决定，"
                         "输出 JSON 数组：[{\"type\":\"preference|constraint|decision|fact\","
                         "\"content\":\"原样引用用户表述\"}]。"
                         "规则：只提取用户明确陈述；不要推断人格；不要提取疑问句/假设句/"
                         "密钥/代码；没有可提取内容输出 []。只输出 JSON，不要解释。")},
            {"role": "user", "content": json.dumps(user_texts, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_tokens": 500,
    }
    _apply_reasoning(payload, "off")
    try:
        data = _call_upstream_raw(api_url, payload, headers)
        raw = (data["choices"][0]["message"].get("content") or "").strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return None
        out = []
        for item in parsed:
            if (isinstance(item, dict) and item.get("content")
                    and item.get("type") in
                    ("preference", "constraint", "decision", "fact")):
                out.append({"type": item["type"], "content": str(item["content"])[:200],
                            "confidence": 0.8, "explicit": True,
                            "retrieval_keys": _agent_memory._default_keys(
                                str(item["content"]))})
        return out or None
    except Exception:
        return None


def _memory_process_run(record: dict) -> None:
    """MemoryWorker handler：L0 归档 → 幂等检查 → L1 提取 → 画像重建。

    全部失败静默（记忆提取不能影响聊天主流程）。
    """
    import uuid as _uuid
    if not _memory_enabled():
        return
    sid = record.get("session_id")
    rid = str(record.get("request_id") or _uuid.uuid4().hex[:12])
    ws = str(record.get("workspace") or "")
    try:
        # 幂等：同一 request_id 不重复处理
        env = _agent_memory.load_l1()
        cur = (env.get("extraction_cursors") or {}).get(
            f"session-{sid}" if sid else "default") or {}
        if cur.get("last_request_id") == rid:
            return
        # L0 归档（原文永久保留）
        for m in (record.get("input_messages") or [])[-40:]:
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                _agent_memory.l0_append(session_id=sid, request_id=rid,
                                        role=m.get("role", ""),
                                        content=content[:4000], workspace=ws)
        if record.get("status") == "cancelled":
            return
        # L1 提取（保守规则）
        items = _agent_memory.extract_l1_from_run(record)
        # LLM 兜底精化（默认关闭；有规则候选且配置开启时才调用）
        try:
            if items and load_config().get("llm_memory_extract"):
                cfg = load_config()
                aurl = normalize_url(cfg.get("api_url"))
                akey = (cfg.get("api_key") or "").strip()
                if aurl and akey:
                    refined = _llm_extract_memories(
                        [str(m.get("content") or "") for m in
                         (record.get("input_messages") or [])
                         if m.get("role") == "user"][-6:],
                        aurl,
                        {"Content-Type": "application/json",
                         "Authorization": f"Bearer {akey}"},
                        cfg.get("model") or "")
                    if refined:
                        items = refined
        except Exception:
            pass    # LLM 兜底失败：用规则结果（cursor 幂等防重试）
        if items:
            now = time.time()
            for it in items:
                it.setdefault("id", _uuid.uuid4().hex[:12])
                it.setdefault("scope", "global")
                it.setdefault("workspace_id", ws)
                it.setdefault("status", "active")
                it.setdefault("pinned", False)
                it.setdefault("source_refs",
                              [{"session_id": sid, "request_id": rid}])
                it.setdefault("supersedes", [])
                it.setdefault("created_at", now)
                it.setdefault("updated_at", now)
                it.setdefault("last_accessed_at", now)
                it.setdefault("access_count", 0)
            cursor = {"version": record.get("session_version"),
                      "last_request_id": rid,
                      "last_content_hash": hashlib.sha256(
                          json.dumps(record.get("input_messages") or [],
                                     ensure_ascii=False).encode("utf-8")).hexdigest()[:16]}
            if items:
                _agent_memory.add_memories(items, session_id=sid,
                                           request_id=rid, cursor=cursor)
            else:
                # 无候选也必须记录 cursor：否则同一请求重复处理时
                # 会再次追加 L0 归档（幂等失效）
                _agent_memory.add_memories([], session_id=sid,
                                           request_id=rid, cursor=cursor)
            log.info("记忆提取：会话 %s 请求 %s → %d 条候选",
                     sid, rid, len(items))
        # L3 画像重建（有偏好/约束类记忆时）
        if any(it.get("type") in ("preference", "constraint") for it in items):
            _agent_memory.rebuild_profile()
    except Exception:
        log.debug("memory worker 处理失败（不影响主流程）", exc_info=True)


# handler 后置绑定（worker 已启动）
_memory_worker.handler = _memory_process_run

# ---- Agent 安全锁（防循环调用导致系统崩溃）----
MAX_TOOL_STEPS = 20             # 单次请求最多工具轮数
MAX_TOOL_CALLS_TOTAL = 50       # 单次请求工具调用总数上限（一轮可含多个调用）
MAX_TOOL_CALLS_PER_ROUND = 8    # 单轮 tool_calls 硬上限：模型一次返回过多调用直接整批拒绝
MAX_CONSECUTIVE_FAILURES = 4    # 连续失败熔断阈值：达到即停止整个任务
MAX_AGENT_SECONDS = 900         # 单次 agent 请求总耗时上限（含上游生成时间；50 次工具调用任务约需 15 分钟）
MAX_TEXT_LENGTH = 5000          # type_text 文本长度上限
_agent_lock = threading.Lock()  # 并发互斥：同一时刻只允许一个 agent 循环

# ---- 敏感操作确认机制 ----
CONFIRM_TIMEOUT = 120          # 等待用户确认超时（秒），超时默认拒绝（安全方向）
_confirm_table: dict = {}      # request_id -> {"event", "choice", "task_id", "source", "expires"}
_confirm_lock = threading.Lock()

# 当前 agent 任务 ID（确认请求绑定用：已取消/已完成/过期任务的确认一律拒绝）
_current_agent_task_id = ""
_agent_task_lock = threading.Lock()


def _new_confirm_id(task_id: str) -> str:
    """随机不可预测确认 ID（防猜测/防跨任务应答）。"""
    import uuid
    return f"ask-{uuid.uuid4().hex[:16]}-{task_id}"



# ---- 问询模式（五种）----
CONFIRM_MODES = ("auto", "strict", "trusted", "query", "plan")
CONFIRM_MODE_DESC = {
    "auto":    "智能：敏感写操作确认，只读命令免确认（默认）",
    "strict":  "严格：所有修改/执行类操作都需确认",
    "trusted": "信任：全部自动执行（危险命令黑名单仍拦截，工作区路径限制与调用上限仍生效）",
    "query":   "只读：仅允许查询操作，一切修改直接拒绝",
    "plan":    "计划：任务先列计划表格（步骤+所需工具），统一批准后按计划执行，计划内免确认",
}


class ConfirmModeRequest(BaseModel):
    mode: str = Field(..., description="auto / strict / trusted / query")


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
TODOS_NOTE_MAX_CHARS = 1200      # 任务清单注入 system 的字符上限（防膨胀）
TODO_STATUSES = ("pending", "in_progress", "completed", "failed", "cancelled")
REPO_MAP_TTL = 30                # repo_map 缓存秒数
REPO_MAP_MAX_ENTRIES = 300
REPO_MAP_MAX_DEPTH = 5
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
             ".pcagent", ".idea", ".vscode", ".mypy_cache", ".pytest_cache"}

# ---- Token 用量优化 ----
MAX_HISTORY_MESSAGES = 20    # 发送给上游的消息数上限（保留 system + 最近 N 条）
MAX_HISTORY_CHARS = 120_000  # 发送给上游的总字符硬上限（约 96K tokens，防长消息累积激增）

# ---- Token 用量统计（缓存命中率 / 每请求明细）----
_stats_lock = threading.Lock()
_stats = {
    "calls": 0,             # 上游调用次数
    "prompt_tokens": 0,     # 输入 token 总数
    "cached_tokens": 0,     # 缓存命中 token 总数（DeepSeek cached_tokens）
    "completion_tokens": 0, # 输出 token 总数
    "reasoning_tokens": 0,  # 其中推理 token
}
_recent_usage: deque = deque(maxlen=200)   # 每请求 usage 明细（环形，供 /api/v1/usage）


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
    """聚合一次上游调用的 usage（线程安全）。兼容 DeepSeek / OpenAI 字段。

    只记录数字与聚合值，不记录任何消息正文/密钥；明细环形保留 200 条。
    """
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
        _recent_usage.append({
            "ts": time.time(),
            "prompt_tokens": prompt, "cached_tokens": cached,
            "completion_tokens": completion, "reasoning_tokens": reasoning,
        })

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("llm-backend")


def _setup_file_logging() -> None:
    """统一运行日志：写入数据目录 server.log（1MB 轮转，保留 3 份）。"""
    try:
        from logging.handlers import RotatingFileHandler
        from data_paths import data_dir
        log_dir = data_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "server.log", maxBytes=1_000_000,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
        log.info("运行日志已写入 %s", log_dir / "server.log")
    except Exception as exc:
        log.warning("日志文件初始化失败：%s", exc)


_setup_file_logging()


# 推理强度三档：max=最高（reasoning_effort: max）/ high=高（reasoning_effort: high）/
# off=关闭思考（thinking: {"type": "disabled"}，不发 reasoning_effort）
REASONING_MODES = ("max", "high", "off")
REASONING_MODE_LABEL = {"max": "最高", "high": "高", "off": "关闭"}


def load_config() -> dict:
    default = {
        "api_url": "https://api.deepseek.com/v1/chat/completions",
        "api_key": "",
        "model": "deepseek-v4-flash",
        "context_window": 65536,   # 模型上下文窗口（token），用于容量显示与压缩阈值
        "confirm_mode": "auto",    # 问询模式：auto/strict/trusted/query
        "reasoning_mode": "max",   # 推理强度：max/high/off（DeepSeek v4 系列）
        "vision_api_url": "",      # 视觉模型（view_image 用）：OpenAI 兼容地址，如 https://dashscope.aliyuncs.com/compatible-mode/v1
        "vision_api_key": "",
        "vision_model": "",
        "tool_router": False,      # 工具路由：本地小模型先选工具子集，防 MCP 工具定义挤爆上下文
        "tool_router_url": "http://127.0.0.1:11434",   # Ollama 地址（Windows 侧，Mirrored 网络直通）
        "tool_router_model": "gemma3:1b",
    }
    if CONFIG_PATH.exists():
        try:
            cfg = {**default, **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}
            # 密钥安全存储：配置中为占位符时从 secure_store 读取真实值
            for secret_key in ("api_key", "vision_api_key"):
                if (cfg.get(secret_key) or "") == "__secure__":
                    try:
                        from secure_store import load as ss_load
                        cfg[secret_key] = ss_load(secret_key)
                    except Exception:
                        cfg[secret_key] = ""
            return cfg
        except Exception:
            log.warning("chat_config.json 解析失败，使用默认配置")
    return default


def _write_config_atomic(cfg: dict) -> None:
    """原子写配置（临时文件 + replace），权限限制为当前用户（POSIX 0600）。"""
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    if sys.platform != "win32":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    tmp.replace(CONFIG_PATH)
    if sys.platform != "win32":
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass


def _apply_reasoning(payload: dict, mode: str | None = None) -> None:
    """按推理强度档位向请求 payload 注入 DeepSeek v4 推理参数。

    max → reasoning_effort: "max"；high → reasoning_effort: "high"；
    off → thinking: {"type": "disabled"}（同时不携带 reasoning_effort）。
    仅 DeepSeek v4 系列支持；其他 OpenAI 兼容服务如报参数错误可改为 off。
    """
    mode = mode or str(load_config().get("reasoning_mode") or "max")
    if mode not in REASONING_MODES:
        mode = "max"
    if mode == "off":
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["reasoning_effort"] = mode


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


# ==========================================================================
# 工具路由（Tool Router）：本地小模型先选工具子集，防 MCP 工具定义挤爆上下文。
# 流程：规则关键词命中 → 直接用；未命中 → gemma3:1b 分类 → 宽松解析；
#       结果 ∪ 核心工具集（永远注入）；Ollama 不可达/超时 → 全量工具（降级）。
# 只影响「主模型可见的工具」，不影响权限/确认机制；子 agent 白名单优先于路由。
# ==========================================================================
ROUTER_TIMEOUT = 30.0       # 路由调用超时（秒）：1b 热推理 ~2s；8B 模型冷加载需 20s+
ROUTER_CACHE_TTL = 300      # 路由结果缓存（秒）：同类请求只路由一次
# 模型可能输出中文类别：映射回英文
ROUTER_CN_CATEGORY = {"音乐": "music", "搜索": "search", "地图": "map",
                      "代码": "code", "其他": "general", "天气": "search",
                      "查询": "search", "系统": "general", "任务": "general"}
ROUTER_PROMPT = (
    "你是工具路由器。根据用户请求，从以下类别中选择最合适的一个：\n"
    "- music: 播放/歌单/歌曲/音乐/歌手/专辑（Spotify）\n"
    "- search: 网络搜索/查询信息/新闻/资讯/天气（Tavily）\n"
    "- map: 地图/导航/路线/附近/位置/怎么走（高德）\n"
    "- code: 代码/文件/脚本/编辑/创建/删除/git/终端/命令/运行/测试（本地编程工具）\n"
    "- github: 仓库/issue/PR/CI/提交记录\n"
    "- general: 其他/寒暄/系统状态/任务管理\n"
    "只输出 JSON：{{\"category\": \"xxx\", \"reason\": \"一句话理由\"}}\n"
    "用户请求：{query}"
)

# 核心工具集：永远注入（不参与路由），保证查询/任务管理/视觉/技能始终可用。
# create_plan/stop/delegate 为功能与安全必需：plan 模式提交计划、紧急止停、子 agent 委派；
# create_file 为高频基础写操作（新建不确认，仅覆盖确认）。
ROUTER_CORE_TOOLS = {
    "list_folder", "read_file", "search_text", "glob_files", "list_symbols",
    "git_status", "git_diff", "git_log", "list_todos", "repo_map",
    "system_status", "load_skill", "view_image",
    "process_output", "list_processes", "create_todo", "update_todo",
    "create_plan", "stop", "delegate", "create_file",
    "remember", "recall_memory", "codegraph_query",
}

# 类别 → 追加工具（MCP 按前缀动态展开）
ROUTER_CATEGORY_PREFIXES = {
    "music": ("mcp_spotify_",),
    "search": ("mcp_tavily_",),
    "map": ("mcp_amap_",),
    "github": ("mcp_github_",),
    "code": (),
    "general": (),
}
ROUTER_CATEGORY_LOCAL = {  # 类别额外注入的本地工具
    "code": {"create_folder", "create_file", "replace_text", "undo",
             "run_code", "run_shell", "start_process", "stop_process", "git_commit"},
}

# 规则前置：关键词 → 类别（覆盖高频场景，零延迟零成本）
ROUTER_RULES = [
    (("music",), ("播放", "歌单", "歌曲", "音乐", "歌手", "专辑", "听歌", "spotify", "周杰伦")),
    (("map",), ("地图", "导航", "路线", "附近", "怎么走", "定位", "位置", "地址")),
    (("github",), ("github", "issue", "pull request", "pr", "仓库", "ci", "提交记录")),
    (("code",), ("代码", "文件", "脚本", "修改", "编辑", "创建", "删除", "git ",
                 "终端", "命令", "运行", "测试", "报错", "函数", "目录", "配置")),
    (("search",), ("搜索", "搜一下", "查一下", "新闻", "资讯", "了解一下", "天气")),
]

_router_cache: dict[str, tuple[float, str | None]] = {}   # key -> (时间戳, 类别/None=全量)
_router_lock = threading.Lock()


def _route_rules(query: str) -> frozenset[str] | None:
    """关键词规则路由：返回所有命中的类别（多关键词合并，如「写个播放器」→ code+music）。

    单类别互斥会让「写个播放器脚本」这类请求只命中 music、丢掉代码工具。
    """
    q = (query or "").lower()
    hits = set()
    for cats, kws in ROUTER_RULES:
        if any(kw.lower() in q for kw in kws):
            hits.add(cats[0])
    return frozenset(hits) if hits else None


def _parse_route_output(text: str) -> str | None:
    """宽松解析路由模型输出：JSON → 裸词 → 关键词（含中文类别）。失败返回 None。"""
    if not text:
        return None
    t = text.strip()
    candidates = []
    # 1) 完整 JSON（英文或中文类别值）
    try:
        cat = json.loads(t).get("category")
        if cat:
            candidates.append(cat)
    except Exception:
        pass
    # 2) 提取 JSON 片段中的 category 字段：先反转义（\" → "）再匹配，
    #    兼容模型输出的转义 JSON（\"category\": \"其他\" 形态）
    import re
    unescaped = t.replace('\\"', '"')
    for m in re.finditer(r'"category"\s*:\s*"([^"]+)"', unescaped):
        candidates.append(m.group(1))
    # 3) 裸词/文本中的类别词
    for cat in ROUTER_CATEGORY_PREFIXES:
        if re.search(rf"\b{cat}\b", t.lower()):
            candidates.append(cat)
    for cat in ROUTER_CN_CATEGORY:
        if cat in t:
            candidates.append(ROUTER_CN_CATEGORY[cat])
    for cand in candidates:
        norm = ROUTER_CN_CATEGORY.get(str(cand).strip().lower(), str(cand).strip().lower())
        if norm in ROUTER_CATEGORY_PREFIXES:
            return norm
    return None


def _call_router(query: str, cfg: dict) -> str | None:
    """调用本地路由模型（Ollama）。强制直连（防代理劫持 127.0.0.1）；失败返回 None。

    keep_alive 保活：模型常驻内存，避免每次冷加载导致路由超时。
    """
    url = (cfg.get("tool_router_url") or "http://127.0.0.1:11434").rstrip("/") + "/api/chat"
    model = cfg.get("tool_router_model") or "gemma3:1b"
    payload = {"model": model, "stream": False,
               "keep_alive": "10m",   # 保活：路由请求频繁，防冷加载超时
               "options": {"temperature": 0, "num_ctx": 512},   # 路由 prompt 短，小上下文提速
               "messages": [{"role": "user",
                             "content": ROUTER_PROMPT.format(query=query[:500])}]}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 直连
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=ROUTER_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("message", {}).get("content") or ""


def _active_mcp_servers(messages: list[dict], max_rounds: int = 2) -> set[str]:
    """多轮任务保持：最近 N 轮 assistant tool_calls 里用过的 MCP server。

    路由只按最后一条用户消息分类，多轮任务第二轮会丢掉上一轮的 server
    （如「播放周杰伦的歌」→ music，下一轮「暂停」→ general 无 spotify）。
    扫描最近几条 assistant 消息的工具调用记录，把这些 server 的工具保持注入。
    """
    servers: set[str] = set()
    rounds = 0
    for m in reversed(messages):
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        rounds += 1
        for tc in m["tool_calls"]:
            name = (tc.get("function") or {}).get("name", "")
            if name.startswith("mcp_"):
                mcp = _ensure_mcp()
                server = mcp.server_of(name) if mcp is not None else None
                if server is None:
                    # 未连接 MCP（测试环境）：回退旧式解析
                    parts = name.split("_", 2)
                    if len(parts) == 3:
                        server = parts[1]
                if server:
                    servers.add(server)
        if rounds >= max_rounds:
            break
    return servers


def _tools_for_category(cat: str, known: set[str], all_tools: list[dict]) -> list[dict]:
    """类别 → 工具定义列表（核心集 ∪ 类别工具，过滤不存在的）。"""
    names = set(ROUTER_CORE_TOOLS) | ROUTER_CATEGORY_LOCAL.get(cat, set())
    for prefix in ROUTER_CATEGORY_PREFIXES.get(cat, ()):
        names |= {n for n in known if n.startswith(prefix)}
    names &= known
    return [t for t in all_tools if t["function"]["name"] in names]


def _route_tools(messages: list[dict]) -> list[dict] | None:
    """路由入口：返回应注入的工具子集；返回 None = 降级（全量工具）。

    调用链：开关检查 → 缓存 → 规则（多类别合并）→ 模型 → 宽松解析；
    再叠加「活跃 MCP server」（多轮任务保持）。任一步失败回退全量。
    """
    cfg = load_config()
    if not cfg.get("tool_router"):
        return None
    # 取最后一条用户消息作路由依据
    query = next((m.get("content", "") for m in reversed(messages)
                  if m.get("role") == "user"), "")
    if not query:
        return None
    cache_key = query[:80]
    now = time.monotonic()
    with _router_lock:
        hit = _router_cache.get(cache_key)
        if hit and now - hit[0] < ROUTER_CACHE_TTL:
            cats = hit[1]
            if cats is not None:
                tools = _build_routed_tools(cats, messages)
                log.info("router 缓存命中: %s（%d 工具）", ",".join(sorted(cats)), len(tools))
                return tools
            return None
    # 规则前置（多类别合并）
    cats = _route_rules(query)
    src = "规则"
    if cats is None:
        # 模型路由
        try:
            out = _call_router(query, cfg)
            cat = _parse_route_output(out)
            cats = frozenset({cat}) if cat else None
            src = "模型"
        except Exception as exc:
            log.warning("router 模型调用失败（降级全量）: %s", exc)
            cats = None
    with _router_lock:
        _router_cache[cache_key] = (time.monotonic(), cats)
    if cats is None:
        log.info("router 未命中（降级全量）: %.60s", query)
        return None
    tools = _build_routed_tools(cats, messages)
    log.info("router %s命中 %s: %d 工具（%.60s）", src,
             ",".join(sorted(cats)), len(tools), query)
    return tools


def _build_routed_tools(cats: frozenset[str], messages: list[dict]) -> list[dict]:
    """类别集合 + 活跃 MCP server → 工具定义列表。"""
    all_tools = _agent_tools()
    known = {t["function"]["name"] for t in all_tools}
    names = set(ROUTER_CORE_TOOLS)
    for cat in cats:
        names |= ROUTER_CATEGORY_LOCAL.get(cat, set())
        for prefix in ROUTER_CATEGORY_PREFIXES.get(cat, ()):
            names |= {n for n in known if n.startswith(prefix)}
    # 活跃 MCP server（多轮任务保持）：上一轮用过的 server 工具继续注入
    for server in _active_mcp_servers(messages):
        prefix = f"mcp_{server}_"
        names |= {n for n in known if n.startswith(prefix)}
    names &= known
    return [t for t in all_tools if t["function"]["name"] in names]


class ChatRequest(BaseModel):
    messages: list[dict] = Field(..., description="OpenAI 格式的消息列表")
    model: str | None = Field(default=None, description="覆盖配置中的模型名")
    temperature: float = Field(default=0.7, ge=0, le=2)
    agent: bool = Field(default=False, description="启用内置 Agent 工具调用循环")
    # ---- 记忆系统会话身份（可选；提供后记忆提取可溯源到会话/工作区）----
    session_id: int | None = Field(default=None, description="当前会话 ID（记忆溯源用）")
    request_id: str | None = Field(default=None, description="本次请求唯一 ID（幂等）")
    workspace: str | None = Field(default=None, description="规范化工作区路径（记忆隔离用）")
    session_version: int | None = Field(default=None, description="会话版本（提取进度游标）")


class CompressRequest(BaseModel):
    messages: list[dict] = Field(..., description="待压缩的完整消息列表")
    keep_recent: int = Field(default=8, ge=2, le=30, description="保留最近 N 条消息不压缩")
    session_id: int | None = Field(default=None, description="会话 ID（L2 场景归属溯源）")


class ConfigUpdate(BaseModel):
    api_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    context_window: int | None = None
    reasoning_mode: str | None = None
    vision_api_url: str | None = None
    vision_api_key: str | None = None
    vision_model: str | None = None


class LlmError(Exception):
    """上游调用失败，携带可直接展示给用户的 HTTP 状态与信息。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


app = FastAPI(title="LLM Backend", version=APP_VERSION)


@app.on_event("startup")
async def _warmup_mcp() -> None:
    """启动时后台预连接 MCP server：首个请求不等待连接（懒加载兜底仍在）。"""
    if os.environ.get("PCAGENT_DISABLE_MCP"):
        return
    threading.Thread(target=_ensure_mcp, daemon=True, name="mcp-warmup").start()
    # 工具路由预热：把本地路由模型加载进内存（防首个路由请求冷加载超时）
    if load_config().get("tool_router"):
        def _warm_router():
            try:
                _call_router("ping", load_config())
                log.info("工具路由模型已预热")
            except Exception:
                pass
        threading.Thread(target=_warm_router, daemon=True, name="router-warmup").start()


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
        _write_config_atomic(cfg)
    except OSError as exc:
        raise HTTPException(500, f"配置写入失败：{exc}") from exc
    log.info("confirm mode -> %s", req.mode)
    return {"ok": True, "mode": req.mode, "description": CONFIRM_MODE_DESC[req.mode]}


# 可选 token 鉴权：--token 启动时启用，所有请求须带 X-Api-Token 头
AUTH_TOKEN = ""
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _token_ok(request) -> bool:
    """常量时间比较验证 token（防时序攻击）。"""
    if not AUTH_TOKEN:
        return True
    import hmac
    return hmac.compare_digest(request.headers.get("X-Api-Token", ""), AUTH_TOKEN)


def _parse_host_header(host: str) -> str | None:
    """解析 Host 头为规范 hostname；非法格式（userinfo/多余冒号/坏端口）返回 None。

    支持：127.0.0.1、localhost、[::1]、上述形式携带合法数字端口。
    """
    host = (host or "").strip()
    if not host:
        return None
    if host.startswith("["):
        # IPv6 字面量：[::1] 或 [::1]:8000
        end = host.find("]")
        if end < 0:
            return None
        inner = host[1:end]
        rest = host[end + 1:]
        if rest:
            if not rest.startswith(":") or not rest[1:].isdigit():
                return None
        return inner.lower()
    if "@" in host or host.count(":") > 1:
        return None   # userinfo 或裸 IPv6 未加括号：拒绝
    if ":" in host:
        hostname, _, port = host.partition(":")
        if not port.isdigit():
            return None
        return hostname.lower()
    return host.lower()


def _is_loopback(host: str) -> bool:
    hostname = _parse_host_header(host)
    if hostname is None:
        return False
    return hostname in _LOOPBACK_HOSTS


@app.middleware("http")
async def host_guard(request, call_next):
    """Host/Origin 限制：防 DNS rebinding（恶意域名解析到本机）与跨站请求。

    - Host 必须是本机回环地址（或 ::1）；
    - Origin 若存在必须是本机来源（浏览器跨站防护）。
    """
    # Origin 检查先于 Host（与 Host 白名单分支互不影响）
    origin = request.headers.get("origin")
    if origin:
        # URL 解析后精确比较 hostname（拒绝 http://localhost.evil.example 等欺骗）
        from urllib.parse import urlparse as _up
        try:
            o = _up(origin)
            ohost = (o.hostname or "").lower()
        except Exception:
            ohost = ""
        if ohost not in _LOOPBACK_HOSTS or o.scheme not in ("http", "https"):
            return JSONResponse(status_code=403,
                                content={"detail": "非法 Origin（仅允许本机来源）"})
    host = request.headers.get("host", "")
    hostname = _parse_host_header(host)
    if hostname is None or hostname not in _LOOPBACK_HOSTS:
        # 测试环境（TestClient 默认 testserver）白名单：不放开任意 Host
        if (os.environ.get("PCAGENT_ALLOW_TEST_HOST") == "1"
                and hostname in ("testserver", "testclient", "localhost")):
            return await call_next(request)
        return JSONResponse(status_code=403,
                            content={"detail": "非法 Host（仅允许本机回环访问）"})
    return await call_next(request)


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
    if AUTH_TOKEN and not _token_ok(request):
        return JSONResponse(
            status_code=401,
            content={"detail": "未授权：需要正确的 X-Api-Token（llm_server 已启用 token 鉴权）"},
        )
    return await call_next(request)


# 结构化压缩：schema 版本 + 稳定结构（规格第六章）
SUMMARY_SCHEMA_VERSION = 1
COMPRESS_PROMPT = (
    "你是一个上下文压缩引擎。以下是 Agent 与用户之间的早期对话历史（JSON 格式）。\n"
    "请压缩为**一个 JSON 对象**（不要其他文字、不要 markdown 围栏），字段：\n"
    '{"objective": "", "success_criteria": [], "user_constraints": [], '
    '"decisions": [], "assumptions": [], "completed_actions": [], '
    '"files_and_artifacts": [], "commands_and_results": [], '
    '"active_errors": [], "open_tasks": [], "pending_confirmations": [], '
    '"tool_state": [], "workspace": "", "retrieval_keys": []}\n'
    "规则：\n"
    "- 只记录外部可验证的任务状态，不保存或伪造模型思维过程；\n"
    "- 文件路径、函数名、错误文本、数字、版本、用户约束**尽量原样保存**；\n"
    "- retrieval_keys：从历史中提取后续可能被引用的检索键（文件路径、函数名、端口号、版本号、约束短语），"
    "供按需检索原始消息；\n"
    "- 未完成任务、失败状态、关键决定必须保留，不得遗漏；\n"
    "- 忽略寒暄和无关内容。JSON 总长控制在 400 字以内，不要解释。"
)


def _extract_evidence_keys(early: list[dict]) -> list[str]:
    """从早期对话提取证据键（路径/错误/数字），供摘要一致性校验与检索。"""
    text = " ".join(str(m.get("content") or "") for m in early)
    keys: set[str] = set()
    for m in re.finditer(r"(?:^|[\s\"'(])([A-Za-z0-9_./\\\-]+\.(?:py|js|ts|json|md|html|css|txt|bat|sh|yml|yaml|toml|ini|png|jpg|exe))", text):
        keys.add(m.group(1))
    for m in re.finditer(r"(?:Error|Exception|Traceback|Failed)\s*[:：]?\s*([^\n，。]{4,60})", text):
        keys.add(m.group(1).strip()[:40])
    for m in re.finditer(r"\b(?:port|端口|version|版本|exit\s+code)\b[\s:：]*(\d+)", text, re.I):
        keys.add(m.group(1))
    return sorted(k for k in keys if k)[:30]


def _summary_to_text(summary: dict) -> str:
    """结构化摘要 → 注入上下文的稳定文本（顺序固定，不随时间变化）。

    集合语义字段（标准/约束/决定/任务）排序后再拼接：相同逻辑 → 相同文本
    （提示词缓存与一致性要求）；有序字段（已完成/命令结果）保持原文顺序。
    """
    parts = []
    if summary.get("objective"):
        parts.append(f"目标：{summary['objective']}")
    for label, key in (("成功标准", "success_criteria"), ("用户约束", "user_constraints"),
                       ("已确认决定", "decisions"), ("未完成任务", "open_tasks"),
                       ("等待确认", "pending_confirmations")):
        vals = summary.get(key) or []
        if vals:
            parts.append(f"{label}：" + "；".join(sorted(str(v) for v in vals)))
    if summary.get("assumptions"):
        parts.append("假设：" + "；".join(sorted(str(v) for v in summary["assumptions"])))
    if summary.get("completed_actions"):
        parts.append("已完成：" + "；".join(str(v) for v in summary["completed_actions"]))
    if summary.get("files_and_artifacts"):
        parts.append("文件：" + "；".join(str(v) for v in summary["files_and_artifacts"]))
    if summary.get("active_errors"):
        parts.append("未解决错误：" + "；".join(str(v) for v in summary["active_errors"]))
    if summary.get("commands_and_results"):
        parts.append("命令与结果：" + "；".join(str(v) for v in summary["commands_and_results"]))
    if summary.get("workspace"):
        parts.append(f"工作区：{summary['workspace']}")
    return "\n".join(parts) or "（摘要为空）"


def _validate_summary(summary: dict, evidence_keys: list[str]) -> tuple[bool, str]:
    """摘要一致性校验：失败不得替换原始上下文。

    - 路径类证据键：摘要必须覆盖大部分（缺失即判定遗漏风险）；
    - 必须含 objective 或 open_tasks（否则摘要无法支撑后续任务）。
    """
    if not isinstance(summary, dict):
        return False, "摘要不是 JSON 对象"
    if not (summary.get("objective") or summary.get("open_tasks")):
        return False, "摘要缺少目标或未完成任务（一致性校验失败）"
    if evidence_keys:
        joined = " ".join(str(v) for v in summary.values())
        covered = sum(1 for k in evidence_keys if k in joined)
        if covered < max(1, int(len(evidence_keys) * 0.4)):
            return False, (f"摘要遗漏关键路径/错误（覆盖 {covered}/{len(evidence_keys)}，"
                           f"一致性校验失败）")
    return True, "ok"


@app.post("/api/v1/compress", summary="上下文压缩（结构化摘要：schema + 校验 + 检索键）")
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
    evidence_keys = _extract_evidence_keys(early)

    # 用模型生成结构化摘要（非流式，少量 token）；摘要任务关闭思考，更快更省
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": COMPRESS_PROMPT},
            {"role": "user", "content": early_json},
        ],
        "temperature": 0.3,
        "max_tokens": 800,
    }
    _apply_reasoning(payload, "off")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(None, _call_upstream_raw, api_url, payload, headers)
    except LlmError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    raw = (data["choices"][0]["message"].get("content") or "").strip()
    if not raw:
        raise HTTPException(502, "摘要生成失败（模型返回空内容）")
    # 解析 JSON（容忍 markdown 围栏）
    parsed: dict | None = None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
        if fence:
            try:
                parsed = json.loads(fence.group(1))
            except (json.JSONDecodeError, ValueError):
                parsed = None
    if parsed is None:
        raise HTTPException(502, "摘要不是合法 JSON，已放弃本次压缩（保留原始上下文）")
    ok, why = _validate_summary(parsed, evidence_keys)
    if not ok:
        log.warning("compress 校验失败：%s", why)
        raise HTTPException(502, f"摘要一致性校验失败：{why}（已保留原始上下文）")

    summary_text = _summary_to_text(parsed)
    retrieval_keys = [str(k) for k in (parsed.get("retrieval_keys") or [])][:40]
    new_msgs = system + [
        {"role": "system",
         "content": f"（较早对话的结构化摘要 v{SUMMARY_SCHEMA_VERSION}，用于替代被压缩的历史）"
                    f"{summary_text}"},
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
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "summary_hash": hashlib.sha256(early_json.encode("utf-8")).hexdigest()[:12],
        "evidence_keys": len(evidence_keys),
        "retrieval_keys": retrieval_keys,
    }
    log.info("compress: %d -> %d messages, saved %d chars, keys=%d",
             len(msgs), len(new_msgs), before_c - after_c, len(retrieval_keys))
    # R4：L2 场景旁路派生（压缩成功时写场景；失败静默，不改变压缩流程）
    try:
        if _memory_enabled():
            _agent_memory.add_scenario_from_summary(
                summary=parsed, session_id=req.session_id or 0)
    except Exception:
        pass
    return {"ok": True, "compressed": True, "messages": new_msgs,
            "summary": summary_text, "stats": stats}


@app.get("/api/v1/config", summary="查看 API 配置（Key 脱敏）")
async def get_config() -> dict:
    cfg = load_config()
    return {"ok": True, "config": {
        "api_url": cfg.get("api_url", ""),
        "api_key": "***" if cfg.get("api_key") else "",
        "model": cfg.get("model", ""),
        "context_window": cfg.get("context_window", 65536),
        "reasoning_mode": cfg.get("reasoning_mode", "max"),
    }}


@app.post("/api/v1/config", summary="更新 API 配置（写入 chat_config.json，实时生效）")
async def update_config(req: ConfigUpdate) -> dict:
    cfg = load_config()
    updates = {}
    if req.api_url is not None:
        cfg["api_url"] = req.api_url.strip()
        updates["api_url"] = True
    if req.api_key is not None:
        key = req.api_key.strip()
        if key:
            # 密钥安全存储：真实 Key 进 DPAPI/受限文件，配置只保留占位符
            try:
                from secure_store import store as ss_store
                ss_store("api_key", key)
            except Exception as exc:
                raise HTTPException(500, f"密钥安全存储失败：{exc}（未保存 Key）") from exc
            cfg["api_key"] = "__secure__"
        else:
            # 清空 Key：同步删除 secure store 旧值（不留残留凭据）
            cfg["api_key"] = ""
            try:
                from secure_store import delete as ss_delete
                ss_delete("api_key")
            except Exception:
                pass
        updates["api_key"] = True
    if req.vision_api_url is not None:
        cfg["vision_api_url"] = req.vision_api_url.strip()
        updates["vision_api_url"] = True
    if req.vision_api_key is not None:
        vkey = req.vision_api_key.strip()
        if vkey:
            try:
                from secure_store import store as ss_store
                ss_store("vision_api_key", vkey)
            except Exception as exc:
                raise HTTPException(500, f"视觉密钥安全存储失败：{exc}（未保存 Key）") from exc
            cfg["vision_api_key"] = "__secure__"
        else:
            cfg["vision_api_key"] = ""
            try:
                from secure_store import delete as ss_delete
                ss_delete("vision_api_key")
            except Exception:
                pass
        updates["vision_api_key"] = True
    if req.vision_model is not None:
        cfg["vision_model"] = req.vision_model.strip()
        updates["vision_model"] = True
    if req.model is not None:
        cfg["model"] = req.model.strip()
        updates["model"] = True
    if req.context_window is not None:
        if req.context_window <= 0:
            raise HTTPException(422, "context_window 必须是正整数")
        cfg["context_window"] = req.context_window
        updates["context_window"] = True
    if req.reasoning_mode is not None:
        if req.reasoning_mode not in REASONING_MODES:
            raise HTTPException(422, f"无效推理强度，可选：{' / '.join(REASONING_MODES)}")
        cfg["reasoning_mode"] = req.reasoning_mode
        updates["reasoning_mode"] = True
    if not updates:
        raise HTTPException(
            422, "没有可更新的字段（支持 api_url/api_key/model/context_window/"
                 "reasoning_mode/vision_api_url/vision_api_key/vision_model）")
    # 写盘前脱敏：内存中的真实密钥（secure store 读出的明文）绝不落盘，
    # 未在本请求中更新的密钥字段替换回占位符
    try:
        from secure_store import load as ss_has
        for secret_key in ("api_key", "vision_api_key"):
            if cfg.get(secret_key) and cfg.get(secret_key) != "__secure__":
                if ss_has(secret_key):
                    cfg[secret_key] = "__secure__"
                else:
                    cfg[secret_key] = ""
    except Exception:
        pass
    try:
        _write_config_atomic(cfg)
    except OSError as exc:
        raise HTTPException(500, f"配置写入失败：{exc}") from exc
    log.info("config updated: %s", updates)
    return {"ok": True, "updated": list(updates.keys()), "config": {
        "api_url": cfg.get("api_url", ""),
        "api_key": "***" if cfg.get("api_key") else "",
        "model": cfg.get("model", ""),
        "context_window": cfg.get("context_window", 65536),
        "reasoning_mode": cfg.get("reasoning_mode", "max"),
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


@app.get("/api/v1/agents", summary="可用子 agent 列表（供前端 /agents 展示）")
async def list_agents() -> dict:
    return {"ok": True, "agents": [
        {k: v for k, v in a.items() if k != "system_prompt"} for a in _scan_agents()]}


@app.get("/api/v1/workspace", summary="查看当前工作区")
async def get_workspace() -> dict:
    ws = _get_workspace()
    return {"ok": True, "workspace": str(ws)}


class WorkspaceUpdate(BaseModel):
    path: str = Field(..., description="工作区目录（绝对路径，须存在）")


@app.post("/api/v1/workspace", summary="切换工作区（持久化；旧任务检测到切换后自动中止）")
async def set_workspace(req: WorkspaceUpdate) -> dict:
    global _workspace_path, _workspace_epoch
    p = _resolve_workspace(req.path)
    if p is None:
        raise HTTPException(422, f"工作区目录无效或不存在：{req.path}")
    with _workspace_lock:
        _workspace_path = p
        _workspace_epoch += 1
    # 持久化到配置（resolve 后保存）
    cfg = load_config()
    cfg["workspace"] = str(p)
    try:
        _write_config_atomic(cfg)
    except OSError as exc:
        log.warning("工作区配置保存失败：%s", exc)
    log.info("workspace -> %s（epoch %d）", p, _workspace_epoch)
    return {"ok": True, "workspace": str(p), "epoch": _workspace_epoch}


@app.get("/api/v1/health", summary="LLM 后端健康检查")
async def health() -> dict:
    cfg = load_config()
    # 工具枚举可能触发 MCP 连接等待：放到线程池，不阻塞 FastAPI 事件循环
    loop = asyncio.get_running_loop()
    tools = await loop.run_in_executor(
        None, lambda: sorted(t["function"]["name"] for t in _agent_tools()))
    return {
        "ok": True,
        "version": APP_VERSION,
        "configured": bool(cfg.get("api_url") and cfg.get("api_key")),
        "api_url": normalize_url(cfg.get("api_url")),
        "model": cfg.get("model") or "",
        "context_window": cfg.get("context_window") or 65536,
        "reasoning_mode": cfg.get("reasoning_mode", "max"),
        "isolated": ISOLATED,
        "workspace": str(_get_workspace()),
        "tools": tools,
        "usage": _usage_summary(),
        "memory_stats": _memory_stats(),
    }


def _memory_stats() -> dict:
    """记忆系统统计（health 下发；失败返回空，不影响 health）。"""
    try:
        env = _agent_memory.load_l1()
        profile = _agent_memory.load_profile()
        return {
            "enabled": _memory_enabled(),
            "l1_memories": len([e for e in env.get("items", [])
                                if e.get("status") == "active"]),
            "l2_scenarios": len(_agent_memory.list_scenario_paths()),
            "profile_preferences": len(profile.get("preferences") or []),
            "profile_updated": profile.get("updated") or 0,
            "dynamic_skills": len(_agent_memory.load_skills_dynamic()),
        }
    except Exception:
        return {"enabled": False}


@app.get("/api/v1/usage", summary="Token 用量统计（聚合 + 最近明细，无正文）")
async def usage_stats() -> dict:
    """Token 可观测性：聚合统计 + 缓存命中率 + 最近 N 次请求明细。

    只含数字指标，不含任何消息正文、密钥或用户数据。统计失败不影响聊天主流程。
    """
    return _usage_summary(recent=True)


def _usage_summary(recent: bool = False) -> dict:
    """读取聚合用量（线程安全）。recent=True 时附加最近请求明细。"""
    with _stats_lock:
        stats = dict(_stats)
        recent_list = list(_recent_usage) if recent else []
    total_prompt = stats.get("prompt_tokens", 0)
    cached = stats.get("cached_tokens", 0)
    out = {
        "calls": stats.get("calls", 0),
        "prompt_tokens": total_prompt,
        "cached_tokens": cached,
        "uncached_tokens": max(0, total_prompt - cached),
        "completion_tokens": stats.get("completion_tokens", 0),
        "reasoning_tokens": stats.get("reasoning_tokens", 0),
        "cache_hit_rate": (cached / total_prompt) if total_prompt else 0.0,
        "total_tokens": total_prompt + stats.get("completion_tokens", 0),
    }
    if recent:
        out["recent"] = recent_list
    # 提示词缓存观测（本地构造层；不伪造供应商缓存命中）
    try:
        out["prefix_cache"] = _prompt_cache.metrics()
    except Exception:
        out["prefix_cache"] = {}
    return out


@app.get("/api/v1/diagnostics", summary="可见诊断入口（健康状态 + 脱敏导出）")
async def diagnostics(redact: int = 1) -> dict:
    """展示 Daemon/LLM/MCP/secure store 健康状态；默认脱敏（redact=1）。

    绝不包含 api_key/token/Authorization 原文与用户消息正文；诊断失败
    不影响聊天主流程（各分项独立容错）。
    """
    cfg = load_config()
    daemon = {"status": "unknown", "detail": ""}
    try:
        code, data, _ = _call_daemon("GET", "/api/v1/status", timeout=5)
        if code == 200 and data:
            daemon = {"status": "ok", "detail": {
                "screen": data.get("screen_size"),
                "busy": data.get("busy"),
                "queued": data.get("queued"),
            }}
        else:
            daemon = {"status": "error", "detail": f"HTTP {code}"}
    except Exception as exc:
        daemon = {"status": "error", "detail": str(exc)[:200]}

    mcp = []
    try:
        from mcp_manager import get_manager_state
        for server in get_manager_state():
            mcp.append({
                "name": server.get("name"),
                "connected": bool(server.get("connected")),
                "tool_count": int(server.get("tool_count") or 0),
                "error": (server.get("error") or "")[:200],
            })
    except Exception as exc:
        mcp = [{"name": "(manager 不可用)", "connected": False,
                "tool_count": 0, "error": str(exc)[:200]}]

    secure = {"status": "unknown"}
    try:
        from secure_store import _secrets_path
        p = _secrets_path()
        secure = {
            "status": "ok" if p.exists() else "empty",
            "path": str(p) if redact == 0 else "…/secrets.json",
            "keys": [],
        }
    except Exception as exc:
        secure = {"status": "error", "detail": str(exc)[:200]}

    # 会话状态（不含正文）
    _ensure_sessions()
    session_summary = {
        "count": len(_sessions),
        "unbound": sum(1 for s in _sessions.values() if not s.get("workspace")),
    }
    return {
        "ok": True,
        "version": APP_VERSION,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "llm": {
            "configured": bool(cfg.get("api_url") and cfg.get("api_key")),
            "model": cfg.get("model") or "",
            "api_url": normalize_url(cfg.get("api_url")) if redact == 0 else (
                "…/" + (normalize_url(cfg.get("api_url") or "").rsplit("/", 1)[-1])
                if cfg.get("api_url") else ""),
            "api_key": "***" if cfg.get("api_key") else "",
            "vision_configured": bool(cfg.get("vision_api_url")
                                      and cfg.get("vision_api_key")),
        },
        "daemon": daemon,
        "mcp": mcp,
        "secure_store": secure,
        "telegram": {"note": "Telegram bot 为独立进程，健康状态由 bot 自身日志记录"},
        "sessions": session_summary,
        "workspace": str(_get_workspace()),
        "isolated": ISOLATED,
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
    _apply_reasoning(payload, "off")  # 连通性测试关闭思考，快速返回
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
    _apply_reasoning(payload)
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
        "description": "读取工作区内文件的内容（UTF-8 文本）。可用 start_line/end_line 读取指定行范围"
                       "（1 起始），返回总行数与实际范围；大文件分段读取用这两个参数。",
        "parameters": {"type": "object",
                       "properties": {
                           "path": {"type": "string", "description": "相对工作区的文件路径"},
                           "start_line": {"type": "integer", "description": "可选：起始行（1 起始）"},
                           "end_line": {"type": "integer", "description": "可选：结束行（含）"}},
                       "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "delete_file",
        "description": "删除工作区内的文件。删除前自动备份，可用 undo 恢复。需用户确认。",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string", "description": "相对工作区的文件路径"}},
                       "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "delete_folder",
        "description": "删除工作区内的空目录（非空目录拒绝，需先删除其中文件）。可用 undo 恢复目录。需用户确认。",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string", "description": "相对工作区的目录路径"}},
                       "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "move_file",
        "description": "移动或重命名工作区内的文件（源与目标均为相对路径）。移动前自动备份，可用 undo 撤销。"
                       "需用户确认。",
        "parameters": {"type": "object",
                       "properties": {
                           "src": {"type": "string", "description": "源文件路径（相对工作区）"},
                           "dst": {"type": "string", "description": "目标路径（相对工作区，目录不存在会自动创建）"}},
                       "required": ["src", "dst"]},
    }},
    {"type": "function", "function": {
        "name": "rename_file",
        "description": "重命名工作区内的文件（move_file 的别名，src→dst）。需用户确认。",
        "parameters": {"type": "object",
                       "properties": {
                           "src": {"type": "string", "description": "原文件名（相对工作区）"},
                           "dst": {"type": "string", "description": "新文件名（相对工作区）"}},
                       "required": ["src", "dst"]},
    }},
    {"type": "function", "function": {
        "name": "copy_file",
        "description": "复制工作区内的文件（源→目标）。目标已存在时先备份再覆盖，可用 undo 恢复。需用户确认。",
        "parameters": {"type": "object",
                       "properties": {
                           "src": {"type": "string", "description": "源文件路径（相对工作区）"},
                           "dst": {"type": "string", "description": "目标路径（相对工作区）"}},
                       "required": ["src", "dst"]},
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
        "description": "在系统中执行 shell 命令（ls/cat/grep/find/pip/apt/systemctl 等任意命令）。"
                       "默认在工作区目录执行，cwd 仅接受工作区内的相对路径（拒绝绝对路径与 ..）。"
                       "执行超时 30 秒，输出最多 3000 字符。"
                       "破坏性命令（rm -rf /、mkfs、shutdown、dd 写磁盘、fork bomb 等）会被拦截。"
                       "注意：sudo 命令需要交互密码，非交互环境会失败。",
        "parameters": {"type": "object",
                       "properties": {
                           "command": {"type": "string", "description": "要执行的 shell 命令"},
                           "cwd": {"type": "string",
                                   "description": "可选：执行目录（工作区内相对路径，默认工作区）"}},
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
        "name": "create_plan",
        "description": "（计划审批模式）任务开始前提交执行计划：列出每个步骤、每步需要的工具和原因。"
                       "文件类工具（replace_text/create_file/undo）必须声明 files（文件或目录范围，"
                       "目录以 / 结尾表示前缀匹配）；run_shell/start_process 必须声明 commands（允许的"
                       "命令，可用前缀）。用户批准后按计划执行，声明范围内的操作免确认；范围外的参数、"
                       "文件被外部修改、计划外工具都会重新确认。只声明计划，不执行任何实际动作。",
        "parameters": {"type": "object",
                       "properties": {
                           "steps": {"type": "array",
                                     "items": {"type": "object",
                                               "properties": {
                                                   "step": {"type": "string", "description": "步骤描述"},
                                                   "tools": {"type": "array", "items": {"type": "string"},
                                                             "description": "本步骤需要的工具名（如 create_file、run_shell）"},
                                                   "files": {"type": "array", "items": {"type": "string"},
                                                             "description": "可选：本步骤操作的文件/目录范围（相对工作区，目录以 / 结尾）"},
                                                   "commands": {"type": "array", "items": {"type": "string"},
                                                                "description": "可选：本步骤允许执行的 shell 命令（完整或前缀）"},
                                                   "reason": {"type": "string", "description": "为什么需要这个权限"}},
                                               "required": ["step", "tools"]},
                                     "description": "计划步骤列表"}},
                       "required": ["steps"]},
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
        "description": "提交 Git 变更。默认只提交 Agent 本轮实际修改过的文件（replace_text/create_file/undo"
                       "等记录的文件），也可用 files 显式指定（相对工作区的路径列表）。"
                       "绝不使用 git add -A（不会误提交无关用户改动）。"
                       "提交前展示完整文件列表与改动统计，经用户确认后执行；"
                       "确认后工作树发生变化会要求重新确认。",
        "parameters": {"type": "object",
                       "properties": {
                           "message": {"type": "string", "description": "提交信息（说明改了什么、为什么）"},
                           "path": {"type": "string", "description": "可选：仓库所在目录（相对工作区）"},
                           "files": {"type": "array", "items": {"type": "string"},
                                     "description": "可选：要提交的文件列表（相对工作区；省略则提交本轮修改的文件）"}},
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
    {"type": "function", "function": {
        "name": "load_skill",
        "description": "加载用户导入的技能包全文（skills/<名称>/SKILL.md，含步骤与注意事项）。"
                       "任务与某个技能匹配时调用；清单见系统提示中的可用技能包。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "技能名（目录名或 frontmatter 的 name）"},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "system_status",
        "description": "查询系统资源状态（只读）：磁盘使用率、内存、CPU 负载、运行时间。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "delegate",
        "description": "把子任务委派给专业子 agent（独立上下文 + 工具白名单执行，结果摘要返回）。"
                       "适合视觉分析、专项审查等需要独立上下文的子任务；子 agent 清单见系统提示。"
                       "子 agent 不能再委派。",
        "parameters": {"type": "object", "properties": {
            "agent": {"type": "string", "description": "子 agent 名称（系统提示中的可用子 agent 列表）"},
            "task": {"type": "string", "description": "要委派的任务描述（尽量具体）"},
            "max_steps": {"type": "integer", "default": 10, "description": "子 agent 工具轮数上限（默认 10，最大 12）"},
        }, "required": ["agent", "task"]},
    }},
    {"type": "function", "function": {
        "name": "view_image",
        "description": "视觉分析：把图片（工作区内路径）发给配置的视觉模型，返回内容描述。"
                       "适合分析截图、界面、图表、照片。需要 chat_config.json 配置视觉模型"
                       "（vision_api_url/vision_api_key/vision_model）。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "图片路径（相对工作区）"},
            "question": {"type": "string", "description": "可选：针对图片的具体问题（默认描述内容）"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "fetch_result",
        "description": "取回被压缩的完整工具结果（按 result_id 与区段 head/tail/error/full）。"
                       "当工具结果提示「完整结果已省略」并给出 id 时，用本工具查看被省略的部分"
                       "（错误/堆栈/长输出在 error 或 tail 区段）。",
        "parameters": {"type": "object", "properties": {
            "result_id": {"type": "string", "description": "完整结果的 id（工具结果尾部提示中给出）"},
            "section": {"type": "string", "enum": ["head", "tail", "error", "full"],
                        "default": "tail", "description": "要取回的区段"},
        }, "required": ["result_id"]},
    }},
    {"type": "function", "function": {
        "name": "remember",
        "description": "主动写入一条长期记忆（用户明确表达且值得跨会话记住的偏好/约束/事实/决定）。"
                       "仅用于用户明确要求记住的内容；对话中自动提取的记忆无需调用本工具。",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string", "description": "记忆内容（原样保留用户表述）"},
            "type": {"type": "string", "enum": ["fact", "preference", "constraint", "decision"],
                     "default": "preference", "description": "记忆类型"},
        }, "required": ["content"]},
    }},
    {"type": "function", "function": {
        "name": "recall_memory",
        "description": "检索长期记忆：查询用户之前明确表达的偏好/约束/事实/决定，"
                       "或按场景路径加载被压缩的会话场景正文（scope=scenario 时）。"
                       "用于需要参考历史上下文时；只读操作。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索关键词（如「端口」「编码风格」）"},
            "scope": {"type": "string", "enum": ["memory", "scenario", "profile"],
                      "default": "memory", "description": "检索范围：记忆/场景正文/用户画像"},
            "scenario_id": {"type": "string", "description": "scope=scenario 时：场景 id（来自 recall_memory 的路径清单）"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "codegraph_query",
        "description": "代码图谱查询：查符号定义位置、谁调用它（callers），或对文件做影响分析。"
                       "修改代码前用它评估影响范围；首次调用会自动构建索引（缓存）。",
        "parameters": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["symbol", "impact", "build"],
                     "default": "symbol", "description": "symbol=查符号调用关系；impact=文件影响分析；build=重建索引"},
            "symbol": {"type": "string", "description": "mode=symbol：符号名（函数/类名）"},
            "file": {"type": "string", "description": "mode=impact：工作区相对路径"},
        }, "required": ["mode"]},
    }},
]

# ---- Skill 包（用户导入的技能包：skills/<名称>/SKILL.md）----
SKILLS_DIR = BASE_DIR.parent / "skills"
SKILL_MAX_CHARS = 8000       # load_skill 返回全文上限（防爆 token）


def _parse_skill_frontmatter(text: str) -> tuple[str, str]:
    """解析 SKILL.md 头部 frontmatter（--- 包裹的 name/description）。"""
    name = desc = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            for line in text[3:end].splitlines():
                k, _, v = line.partition(":")
                k, v = k.strip().lower(), v.strip().strip('"').strip("'")
                if k == "name":
                    name = v
                elif k == "description":
                    desc = v
    return name, desc


def _scan_skills() -> list[dict]:
    """扫描 skills/ 下的技能包（每个子目录一份 SKILL.md），按目录名排序。"""
    out = []
    if not SKILLS_DIR.is_dir():
        return out
    for d in sorted(SKILLS_DIR.iterdir()):
        f = d / "SKILL.md"
        if d.is_dir() and f.is_file():
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name, desc = _parse_skill_frontmatter(text)
            out.append({"name": name or d.name, "description": desc,
                        "dir": d.name, "text": text})
    return out


def _skill_catalog_text() -> str:
    """可用技能清单（只注入清单不注入全文，模型按需 load_skill 加载）。

    静态 skills/ + 动态记忆 Skill（active 状态；命名空间 dynamic: 不覆盖静态同名）。
    """
    skills = _scan_skills()
    lines = []
    if skills:
        lines.append("可用技能包（任务匹配某技能时，用 load_skill 加载该技能全文再执行）：")
        for s in skills:
            lines.append(f"- {s['name']}：{s['description'] or '无描述'}")
    # R4：动态 Skill（active）清单
    try:
        dyn = [s for s in _agent_memory.load_skills_dynamic()
               if s.get("status") == "active"]
        if dyn:
            lines.append("可用动态技能（从成功任务提炼，按触发条件自动适配）：")
            for s in dyn[:10]:
                lines.append(f"- dynamic:{s['name']}：{s.get('trigger') or '无触发条件'}")
    except Exception:
        pass
    return "\n".join(lines)


def _system_status_text() -> str:
    """系统资源快照（只读，纯标准库）：磁盘 / 内存 / CPU 负载 / 运行时间。

    Linux 读 /proc；Windows 只取磁盘（其余项优雅降级）。
    """
    import shutil
    parts = []
    for p in ("/", str(Path.home())):
        try:
            u = shutil.disk_usage(p)
            pct = u.used * 100 // u.total
            parts.append(f"磁盘 {p}: 总 {u.total // 2**30}G 已用 {u.used // 2**30}G "
                         f"可用 {u.free // 2**30}G（{pct}%）")
        except OSError:
            pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            mem = {}
            for line in f:
                k, _, v = line.partition(":")
                if k in ("MemTotal", "MemAvailable"):
                    mem[k] = int(v.strip().split()[0])
        if mem.get("MemTotal"):
            total, avail = mem["MemTotal"], mem.get("MemAvailable", 0)
            used = total - avail
            parts.append(f"内存: 总 {total // 2**20}G 已用 {used // 2**20}G "
                         f"可用 {avail // 2**20}G（{used * 100 // total}%）")
    except OSError:
        pass
    try:
        with open("/proc/loadavg", encoding="utf-8") as f:
            la = f.read().split()
        parts.append(f"CPU 负载(1/5/15 分钟): {la[0]} {la[1]} {la[2]}（核数 {os.cpu_count()}）")
    except OSError:
        pass
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            secs = float(f.read().split()[0])
        parts.append(f"系统已运行: {int(secs // 86400)}天{int(secs % 86400 // 3600)}小时")
    except OSError:
        pass
    return "\n".join(parts) or "无法获取系统状态"


# ---- 子 Agent（agents/<名称>.json：专业子代理，delegate 委派执行）----
AGENTS_DIR = BASE_DIR.parent / "agents"
MAX_AGENT_DEPTH = 2          # 委派深度上限：主 agent(0) → 子 agent(1)，子 agent 不能再委派
SUBAGENT_MAX_STEPS = 10      # 子 agent 单次任务的工具轮数上限（与主循环一致；深度任务如代码审查需多轮）
SUBAGENT_REPLY_CHARS = 2000  # 子 agent 最终回复回传主循环的长度上限
MAX_IMAGE_BYTES = 10_000_000 # view_image 图片大小上限（10MB）
VISION_SYSTEM_NOTE = "（视觉分析：view_image 需在配置中提供 vision_api_url/vision_api_key/vision_model）"
# R3：view_image 同图复用缓存（60s；图片 mtime/size 变化即失效）
_VIMAGE_TTL = 60.0
_vimage_lock = threading.Lock()
_vimage_cache: dict = {}


def _scan_agents() -> list[dict]:
    """扫描 agents/ 下的子 agent 定义（每个 <名称>.json）。"""
    out = []
    if not AGENTS_DIR.is_dir():
        return out
    for f in sorted(AGENTS_DIR.glob("*.json")):
        try:
            spec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(spec, dict) or not spec.get("name"):
            continue
        out.append(spec)
    return out


def _agent_catalog_text() -> str:
    """可用子 agent 清单（注入 system，模型按需 delegate 委派）。"""
    agents = _scan_agents()
    if not agents:
        return ""
    lines = ["可用子 agent（任务专业性强/需要专注上下文时用 delegate 委派，子 agent 独立上下文执行，"
             "结果摘要返回；子 agent 不能再委派）："]
    for a in agents:
        lines.append(f"- {a['name']}：{a.get('description') or '无描述'}"
                     f"{'（模型 ' + a['model'] + '）' if a.get('model') else ''}")
    return "\n".join(lines)


AGENT_SYSTEM_SUFFIX = (
    "\n\n你是 PC Agent，可以控制用户电脑的智能体（工具操作、编写和修改代码）。\n"
    "编程工作流：\n"
    "1. 先 repo_map / search_text / glob_files / list_symbols 定位相关代码，"
    "再用 replace_text 小步修改（系统展示 diff 请用户确认），新文件用 create_file。\n"
    "2. 多步长任务先 create_todo 列计划并逐步 update_todo；后台服务用 start_process，"
    "输出用 process_output；测试/一次性命令用 run_code / run_shell，改完代码主动运行相关测试验证。\n"
    "3. 完成一个阶段用 git_status / git_diff 自查，需要时 git_commit 提交（均需用户确认）。\n"
    "安全与确认：\n"
    "4. 只执行用户明确要求的动作；覆盖文件、修改代码、git 提交、系统级写操作会弹出确认，"
    "请尊重用户选择；关键抉择（删除内容、安装软件、修改配置、二选一路径）"
    "先列选项等用户答复，再行动。\n"
    "5. 可能造成损害时调用 stop 并告知用户；回答先给结论，再补充必要证据/风险/下一步；"
    "简单问题直接简短回答；任务完成用简短中文总结；"
    "寒暄/状态询问（「你还在吗」「你好」）直接简短回答，不调用任何工具、不执行命令。"
)


def _call_upstream_raw(api_url: str, payload: dict, headers: dict) -> dict:
    """非流式调用上游，返回完整响应 JSON（供工具循环解析 tool_calls）。

    Provider 能力过滤：按 api_url 识别 provider，移除不支持的参数
    （如 Ollama 的 reasoning_effort），不因服务忽略未知参数就假装生效。
    """
    payload, caps = _build_payload_caps(
        api_url, str(payload.get("model") or ""), payload,
        _load_provider_overrides(load_config()))
    req = urllib.request.Request(
        api_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _record_usage(data.get("usage"))
        # 结构校验：200 但形状异常（代理网关的错误包装/空 choices）必须在此
        # 转为 LlmError，否则调用方 KeyError 会让 SSE 永久挂起（H1）
        choices = data.get("choices")
        msg = (choices or [{}])[0].get("message") if isinstance(choices, list) and choices else None
        if not isinstance(msg, dict):
            raise LlmError(502, "上游返回了异常结构（缺少 choices/message）")
        return data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        upstream = _extract_upstream_error(body)
        raise LlmError(502, f"上游 API 返回 HTTP {e.code}：{upstream}")
    except urllib.error.URLError as e:
        raise LlmError(502, f"无法连接 API 服务（{getattr(e, 'reason', e)}）")
    except json.JSONDecodeError:
        raise LlmError(502, "上游返回了非 JSON 内容")


def _daemon_base_url() -> str:
    """daemon 地址：配置 daemon_base 优先（不再硬编码 127.0.0.1:8000）。"""
    cfg = load_config()
    return str(cfg.get("daemon_base") or DAEMON_BASE).strip().rstrip("/") or DAEMON_BASE


def _call_daemon(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    """调用本地屏幕控制 daemon。daemon 启用 token 时携带 X-Api-Token。"""
    headers = {"Content-Type": "application/json"}
    dt = str(load_config().get("daemon_token") or "").strip()
    if dt == "__secure__":
        try:
            from secure_store import load as ss_load
            dt = ss_load("daemon_token")
        except Exception:
            dt = ""
    if dt:
        headers["X-Api-Token"] = dt
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        _daemon_base_url() + path, data=data, method=method, headers=headers)
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


# ---- 工作区（可配置，不再硬编码 %USERPROFILE%/agent_workspace）----
_workspace_lock = threading.Lock()
_workspace_path: Path | None = None      # resolve 后的当前工作区（首次访问时从配置加载）
_workspace_epoch = 0                     # 切换工作区时递增：旧任务检测到变化即中止（隔离）


def _resolve_workspace(raw: str) -> Path | None:
    """解析并校验工作区路径：必须存在且为目录，resolve 后返回。"""
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError:
        return None
    if not p.is_dir():
        return None
    return p


def _get_workspace() -> Path:
    """当前工作区：配置优先（chat_config.json 的 workspace 字段），默认主目录下 agent_workspace。

    首次访问时从配置加载并 resolve 保存；切换（POST /api/v1/workspace）后全局生效。
    """
    global _workspace_path
    with _workspace_lock:
        if _workspace_path is None:
            cfg_ws = str(load_config().get("workspace") or "").strip()
            p = _resolve_workspace(cfg_ws) if cfg_ws else None
            if p is None:
                p = Path.home() / "agent_workspace"
                p.mkdir(parents=True, exist_ok=True)
                p = p.resolve()
            _workspace_path = p
        return _workspace_path


def _workspace_changed(epoch: int) -> bool:
    """工作区是否在任务创建后被切换（用于隔离旧任务）。"""
    with _workspace_lock:
        return epoch != _workspace_epoch


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
    yield (绝对路径, 相对工作区的 posix 路径)。

    root 先 resolve 再遍历：Windows 8.3 短路径（RUNNER~1 等）/ junction 场景下
    os.walk 返回的 dirpath 可能与 workspace.resolve() 前缀不一致，直接 relative_to
    会抛 ValueError（CI runner 用户名 >8 字符时触发）。resolve 后重试，仍失败则跳过。
    """
    root = (root or workspace).resolve()
    ws_res = workspace.resolve()
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # 剪枝：隐藏目录与已知构建目录不进子目录
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            abs_p = Path(dirpath) / fn
            try:
                rel = abs_p.relative_to(ws_res).as_posix()
            except ValueError:
                try:
                    rel = abs_p.resolve().relative_to(ws_res).as_posix()
                except ValueError:
                    continue   # 路径无法映射到工作区：跳过（不崩溃）
            yield abs_p, rel
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


# ---- Agent 本轮修改的文件追踪（git_commit 默认提交范围，绝不用 git add -A）----
_modified_lock = threading.Lock()
_agent_modified_files: set[str] = set()
_pending_git_snapshot = ""      # git_commit 确认时记录的工作树快照（执行前校验）


def _record_modified(rel: str) -> None:
    """记录 Agent 本轮修改的文件（相对工作区），git_commit 默认提交范围。"""
    with _modified_lock:
        _agent_modified_files.add(rel)


def _commit_targets(root: Path, workspace: Path, args: dict) -> list[str]:
    """计算 git_commit 将要提交的文件（相对仓库根）：显式 files 或本轮修改文件。"""
    files_arg = args.get("files")
    rels: list[str] = []
    if isinstance(files_arg, list) and files_arg:
        for f in files_arg:
            if isinstance(f, str):
                t = _safe_join(workspace, f)
                if t is not None:
                    try:
                        rels.append(t.relative_to(root).as_posix())
                    except ValueError:
                        pass
    else:
        with _modified_lock:
            rels = sorted(_agent_modified_files)
        out = []
        for r in rels:
            t = _safe_join(workspace, r)
            if t is not None:
                try:
                    out.append(t.relative_to(root).as_posix())
                except ValueError:
                    pass
        return out
    return sorted(set(rels))


def _git_worktree_snapshot(root: Path) -> str:
    """工作树状态快照（porcelain + diff），确认后变化检测用。

    porcelain 只反映文件增删改状态，不反映内容变化；叠加 diff 才能
    捕获「外部修改了已跟踪文件内容」的情况。
    """
    ok, out = _run_git(root, "status", "--porcelain")
    ok2, diff = _run_git(root, "diff")
    return (out or "") + "\n<<DIFF>>\n" + (diff or "")


# ---- 后台进程管理 ----
_process_lock = threading.Lock()
_processes: dict[int, dict] = {}   # pid -> {"proc", "cmd", "started", "lines", "stopped", "stop_failed", "exit_code"}
PROCESS_HISTORY_MAX = 20           # 已结束进程条目保留上限（供 process_output/list_processes 查询）


def _process_reader(proc, lines: deque) -> None:
    """后台线程：逐行缓冲进程输出（环形，防内存膨胀）。bytes 解码容错，兼容任意编码输出。"""
    try:
        for raw in proc.stdout:
            lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
    except (ValueError, OSError, AttributeError):
        pass   # 管道已关闭（stop 时）：读取线程自然退出


def _cleanup_dead_processes() -> None:
    """移除进程表中超出历史上限的已结束条目；运行中条目不受影响。

    刚停止的进程保留完整状态（stopped/stop_failed/exit_code）供查询，
    不会因清理而立即变成「未知 PID」。
    """
    dead = [pid for pid, e in _processes.items() if e["proc"].poll() is not None]
    overflow = len(dead) - PROCESS_HISTORY_MAX
    if overflow > 0:
        for pid in sorted(dead, key=lambda p: _processes[p].get("started_ts", 0))[:overflow]:
            entry = _processes.pop(pid, None)
            if entry is not None and entry.get("job"):
                _close_windows_job(entry["job"])


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
    job = _create_windows_job()
    if job is not None and not _assign_windows_job(job, proc.pid):
        _close_windows_job(job)
        job = None
    lines: deque = deque(maxlen=PROCESS_OUTPUT_LINES)
    threading.Thread(target=_process_reader, args=(proc, lines), daemon=True).start()
    with _process_lock:
        _cleanup_dead_processes()
        running = sum(1 for e in _processes.values() if e["proc"].poll() is None)
        if running >= MAX_PROCESSES:
            _kill_process_tree(proc)
            if job is not None:
                _close_windows_job(job)
            return False, f"后台进程数已达上限 {MAX_PROCESSES}，请先 stop_process 清理"
        _processes[proc.pid] = {"proc": proc, "cmd": command[:200],
                                "started": time.strftime("%H:%M:%S"),
                                "started_ts": time.time(), "lines": lines,
                                "stopped": False, "stop_failed": False, "exit_code": None,
                                "job": job}
    return True, json.dumps({"pid": proc.pid, "command": command[:200],
                             "started": _processes[proc.pid]["started"]}, ensure_ascii=False)


def _create_windows_job():
    """创建带 KILL_ON_JOB_CLOSE 的 Windows Job Object；不可用返回 None。

    Job 句柄关闭时整棵进程树被强制终止——作为超时/停止路径的最后兜底，
    保证 shell 子进程/孙进程不会成为孤儿。调用方必须保存句柄直到进程结束。
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", wintypes.ULONGLONG),
                ("WriteOperationCount", wintypes.ULONGLONG),
                ("OtherOperationCount", wintypes.ULONGLONG),
                ("ReadTransferCount", wintypes.ULONGLONG),
                ("WriteTransferCount", wintypes.ULONGLONG),
                ("OtherTransferCount", wintypes.ULONGLONG),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _assign_windows_job(job, pid: int) -> bool:
    """把进程归属到 Job Object；父进程已在其他 Job 时可能失败，返回 False。"""
    if not job or sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.AssignProcessToJobObject(
            job, ctypes.c_void_p(int(pid))))
    except Exception:
        return False


def _close_windows_job(job) -> None:
    """关闭 Job 句柄；若进程仍在运行，KILL_ON_JOB_CLOSE 会终止整树（兜底）。"""
    if not job or sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(job)
    except Exception:
        pass


def _kill_process_tree(proc) -> None:
    """杀整个进程组/控制台组（shell 启动的子进程一并结束），防止残留。"""
    if sys.platform == "win32":
        # errors="replace"：taskkill 输出可能为 GBK（中文 Windows），防解码崩溃
        r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            # taskkill 失败（权限/已退出/无子进程等）：直接 kill 后备，
            # 不提前报告成功；Job Object 句柄关闭时还会再兜底一次。
            try:
                proc.kill()
            except Exception:
                pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except Exception:
                pass


def _controlled_env(extra: dict | None = None) -> dict:
    """受控环境变量：run_code 执行用，只保留运行必需的系统变量 + 显式编码设置。

    不继承完整父环境（隔离掉代理凭据、用户自定义变量等），降低代码执行副作用。
    """
    allow = {"PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP",
             "SYSTEMROOT", "SystemRoot", "WINDIR", "APPDATA", "LOCALAPPDATA",
             "USERNAME", "USER", "LANG", "LC_ALL", "COMSPEC", "PATHEXT",
             "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS", "COMPUTERNAME"}
    env = {k: v for k, v in os.environ.items() if k in allow}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    if extra:
        env.update(extra)
    return env


def _run_subprocess(cmd, cwd, timeout: float, shell: bool = True,
                    env: dict | None = None) -> tuple[int, str, str, bool]:
    """统一子进程执行（run_shell / run_code 共用）。

    超时处理：杀整个进程组（防子进程残留），并带回超时前的完整部分输出——
    让模型能判断命令是「真慢（有进度）」还是「卡死（无输出）」。
    env=None 继承父进程环境（run_shell）；显式传 env 时使用受控环境（run_code）。

    返回 (returncode, stdout, stderr, timed_out)；输出统一 bytes 解码容错。
    """
    kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  cwd=str(cwd), shell=shell)
    if env is not None:
        kwargs["env"] = env
    if sys.platform != "win32":
        kwargs["executable"] = "/bin/bash" if shell else None
        kwargs["start_new_session"] = True          # 独立进程组，超时可整组击杀
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(cmd, **kwargs)
    job = _create_windows_job()
    if job is not None and not _assign_windows_job(job, proc.pid):
        _close_windows_job(job)
        job = None
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        out, err = proc.communicate()               # 杀完后再取剩余输出
        return proc.returncode, out, err, True
    finally:
        # 关闭 Job 句柄：进程若仍残留（未杀干净），KILL_ON_JOB_CLOSE 兜底整树终止
        if job is not None:
            _close_windows_job(job)


def _decode_out(data) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data or ""


def _stop_process_impl(pid: int) -> tuple[bool, str]:
    """停止后台进程：杀进程树后显式 wait 验证，绝不谎报已停止。

    - taskkill / killpg 后调用 proc.wait(timeout=8) 确认真正退出；
    - 再次 proc.poll() 验证；
    - 超时/仍在运行 → 返回 stopped=False + stop_failed=True（不谎报）；
    - 停止后保留条目状态（stopped/exit_code）供查询，关闭输出管道让读取线程退出。
    """
    with _process_lock:
        entry = _processes.get(pid)
        if entry is None:
            return False, f"进程不存在：{pid}（可用 list_processes 查看）"
        proc = entry["proc"]
    if proc.poll() is None:
        _kill_process_tree(proc)
        try:
            proc.wait(timeout=8)            # taskkill 后显式等待完整进程树退出
        except subprocess.TimeoutExpired:
            with _process_lock:
                e = _processes.get(pid)
                if e is not None:
                    e["stop_failed"] = True
            return False, json.dumps({"pid": pid, "stopped": False, "stop_failed": True,
                                      "detail": "进程未能及时终止（超时 8s），可能仍有残留"},
                                     ensure_ascii=False)
    if proc.poll() is None:
        # 再次验证：仍在运行 → 失败
        with _process_lock:
            e = _processes.get(pid)
            if e is not None:
                e["stop_failed"] = True
        return False, json.dumps({"pid": pid, "stopped": False, "stop_failed": True,
                                  "detail": "进程仍在运行，停止失败"}, ensure_ascii=False)
    # 关闭输出管道：读取线程随之退出（EOF）
    try:
        if proc.stdout is not None:
            proc.stdout.close()
    except (ValueError, OSError):
        pass
    with _process_lock:
        e = _processes.get(pid)
        if e is not None:
            e["stopped"] = True
            e["exit_code"] = proc.returncode
            job = e.get("job")
            e["job"] = None
    if job is not None:
        _close_windows_job(job)
    return True, json.dumps({"pid": pid, "stopped": True,
                             "exit_code": proc.returncode,
                             "stop_failed": False}, ensure_ascii=False)


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


def _secure_atomic_write(path: Path, text: str) -> None:
    """原子写 + 权限限制为当前用户（POSIX 0600；Windows 无 POSIX 权限概念）。

    保存顺序：同目录唯一临时文件 → flush → fsync（条件允许）→ 原子 replace → 保留 .bak。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    if sys.platform != "win32":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    tmp.replace(path)
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _save_todos() -> None:
    try:
        _secure_atomic_write(_todo_file(),
                             json.dumps({"todos": _todos}, ensure_ascii=False, indent=1))
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
    note = "\n".join(lines)
    # 防膨胀：todo 多/描述长时截断（系统注入的预算有限）
    if len(note) > TODOS_NOTE_MAX_CHARS:
        note = note[:TODOS_NOTE_MAX_CHARS] + "\n...（任务清单过长已截断）"
    return note


# ---- 会话持久化（权威存储在项目目录 .pcagent/，跟随程序 / U 盘移动，已 gitignore）----
SESSION_MAX = 50               # 会话数上限
SESSION_MAX_MESSAGES = 200     # 单会话消息数上限（超出丢弃最早，防无限膨胀）
SESSION_MSG_MAX_CHARS = 20_000      # 单条消息字符上限（超限拒绝）
SESSION_TOTAL_MAX_CHARS = 500_000   # 单会话消息总字符上限
SESSION_APPEND_MAX_CHARS = 200_000  # 单次 append 请求体总字符上限
SESSION_TITLE_CHARS = 30       # 自动标题长度（取首条用户消息）
_session_lock = threading.Lock()
_sessions: dict[int, dict] = {}  # id -> {"id", "title", "messages": [...], "updated", "version"}
_session_id_counter = itertools.count(1)
_sessions_loaded = False
_session_load_warning = ""     # 损坏恢复提示（向用户报告）


def _session_file() -> Path:
    """会话文件位置：数据目录 sessions.json（PCAGENT_DATA_DIR 可重定向）。"""
    from data_paths import data_file
    return data_file("sessions.json")


def _normalize_loaded_session(entry: dict) -> dict:
    """兼容旧会话数据：无 workspace 字段的旧会话标记为 unbound（None）。"""
    if "workspace" not in entry:
        entry["workspace"] = None
    return entry


def _load_sessions() -> None:
    """加载会话；主文件缺失或损坏时不静默清空：自动尝试 .bak 恢复。"""
    global _sessions, _session_id_counter, _sessions_loaded, _session_load_warning
    _sessions_loaded = True
    p = _session_file()
    if not p.exists():
        # 主文件缺失：尝试 .bak 恢复
        bak = p.with_suffix(".json.bak")
        if bak.exists():
            try:
                data = json.loads(bak.read_text(encoding="utf-8"))
                _sessions = {int(k): _normalize_loaded_session(v)
                             for k, v in (data.get("sessions") or {}).items()}
                if _sessions:
                    _session_id_counter = itertools.count(max(_sessions) + 1)
                _session_load_warning = "主文件缺失，已从 .bak 恢复"
                log.warning(_session_load_warning)
                return
            except Exception:
                pass
        return
    raw = None
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
        _sessions = {int(k): _normalize_loaded_session(v)
                     for k, v in (data.get("sessions") or {}).items()}
        if _sessions:
            _session_id_counter = itertools.count(max(_sessions) + 1)
        return
    except Exception as exc:
        _session_load_warning = f"sessions.json 损坏（{exc}），尝试从备份恢复"
        log.warning(_session_load_warning)
    # 损坏：备份原文件 + 尝试 .bak 恢复
    try:
        import time as _t
        p.rename(p.with_name(f"sessions.json.corrupt-{int(_t.time())}"))
    except OSError:
        pass
    bak = p.with_suffix(".json.bak")
    if bak.exists():
        try:
            data = json.loads(bak.read_text(encoding="utf-8"))
            _sessions = {int(k): v for k, v in (data.get("sessions") or {}).items()}
            if _sessions:
                _session_id_counter = itertools.count(max(_sessions) + 1)
            _session_load_warning += "；已从 .bak 恢复"
            log.warning("已从 .bak 恢复会话")
            return
        except Exception:
            pass
    _sessions = {}
    _session_load_warning += "；备份不可用，会话已清空（原文件已保留为 .corrupt-*）"


def _save_sessions() -> None:
    """原子写会话文件，主文件永不被先移走。

    顺序：同目录唯一临时文件 → flush/fsync → os.replace 原子替换主文件；
    主文件安全落盘后再更新 .bak（失败不影响主文件）。
    落盘失败抛出 OSError（调用方负责回滚），禁止吞掉错误。
    """
    p = _session_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"sessions": _sessions}, ensure_ascii=False, indent=1))
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        tmp.replace(p)
    finally:
        # 新文件失败时清理残留临时文件；replace 成功后 tmp 已不存在
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    # 主文件已安全落盘：更新 .bak 用于损坏恢复（失败不影响主文件原子性）
    try:
        import shutil
        shutil.copy2(p, p.with_suffix(".json.bak"))
    except OSError:
        pass


def _persist_sessions_or_rollback(rollback) -> None:
    """落盘失败 → 回滚内存 mutation → 抛 500；绝不吞错、绝不保留未落盘修改。"""
    try:
        _save_sessions()
    except OSError as exc:
        try:
            rollback()
        except Exception:
            pass
        log.error("会话持久化失败，已回滚内存修改：%s", exc)
        raise HTTPException(500, f"会话保存失败，修改已回滚：{exc}") from exc


def _ensure_sessions() -> None:
    if not _sessions_loaded:
        _load_sessions()


def _rollback_session_state(s: dict, snapshot: dict) -> None:
    """恢复单会话字段到快照（append/clear 落盘失败时回滚）。

    调用方必须已持有 _session_lock（mutation 接口在锁内调用）。
    """
    s.clear()
    s.update(snapshot)


def _rollback_session_restore(sid: int, saved: dict | None) -> None:
    """删除会话落盘失败时恢复条目。调用方必须已持有 _session_lock。"""
    if saved is not None:
        _sessions[sid] = saved


# ---- 修改回滚（undo）：replace_text / create_file 覆盖前自动备份 ----
BACKUP_MAX = 50            # 备份条目上限（超出丢最老）
_backup_lock = threading.Lock()


def _backup_dir() -> Path:
    """备份目录绑定当前工作区（切换工作区后备份跟随，旧工作区数据不动）。"""
    return _get_workspace() / ".pcagent" / "backups"


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
        _secure_atomic_write(_backup_dir() / "index.json",
                             json.dumps(idx, ensure_ascii=False, indent=1))
    except OSError as exc:
        log.warning("备份清单写入失败：%s", exc)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """写文件：先写同目录唯一临时文件，flush 后原子 replace（同卷）。"""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
    tmp.replace(path)


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _take_backup(workspace: Path, target: Path, op: str = "overwrite",
                 src: str | None = None, dst: str | None = None) -> bool:
    """修改文件前备份原内容（供 undo 恢复）。

    op 记录操作类型：overwrite（覆盖）/ create（新建）/ delete（删除）/
    move（移动，记录 src/dst）/ mkdir（新建目录）。
    备份失败返回 False：调用方必须中止高风险修改（不得继续）。
    """
    try:
        rel = target.relative_to(workspace).as_posix()
        with _backup_lock:
            idx = _backup_index()
            bid = (idx[-1]["id"] + 1) if idx else 1
            bdir = _backup_dir() / str(bid)
            bdir.mkdir(parents=True, exist_ok=True)
            if op not in ("create", "mkdir") and target.is_file():
                # delete/move/overwrite：按 bytes 保存源内容（二进制无损）
                _atomic_write_bytes(bdir / "content", target.read_bytes())
            entry = {"id": bid, "file": rel,
                     "time": time.strftime("%m-%d %H:%M:%S"), "backup": str(bid), "op": op}
            if src:
                entry["src"] = src
            if dst:
                entry["dst"] = dst
            idx.append(entry)
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


# ---- MCP 客户端（外部工具接入，mcp_config.json 配置）----
# 单一生命周期入口：初始化委托 mcp_manager（锁 + 短超时统一），
# 结果绑定本模块引用（测试可 monkeypatch L._mcp_manager）。
_mcp_manager = None


def _ensure_mcp():
    """惰性初始化 MCP 管理器（首次访问工具列表时连接各 server）。
    委托 mcp_manager._ensure_mcp（初始化加锁，PCAGENT_DISABLE_MCP 跳过，2s 短超时）。"""
    global _mcp_manager
    if _mcp_manager is None:
        from mcp_manager import _ensure_mcp as _shared_ensure
        _mcp_manager = _shared_ensure()
    return _mcp_manager


def _agent_tools() -> list[dict]:
    """按运行模式返回可用工具：隔离模式只保留文件类工具。
    MCP 外部工具（如 GitHub）与屏幕无关，隔离模式同样保留。
    工具按名称确定性排序（稳定前缀：防止顺序抖动破坏提示词缓存）。"""
    if ISOLATED:
        tools = [t for t in AGENT_TOOLS if t["function"]["name"] in _FILE_TOOLS]
    else:
        tools = list(AGENT_TOOLS)
    mcp = _ensure_mcp()
    if mcp is not None:
        tools.extend(mcp.all_tools())     # MCP server 工具动态并入（mcp_<server>_<tool>）
    tools.sort(key=lambda t: t["function"]["name"])
    return tools




def _exec_view_image(args: dict) -> tuple[bool, str]:
    """把工作区图片发给配置的视觉模型，返回内容描述（无本地副作用）。"""
    path = (args.get("path") or "").strip()
    question = ((args.get("question") or "").strip()
                or "描述这张图片的内容，重点说明可见的文字、界面元素、布局。")
    if not path:
        return False, "view_image 需要 path 参数"
    workspace = _get_workspace()
    target = _safe_join(workspace, path)
    if target is None or not target.is_file():
        return False, f"图片不存在：{path}"
    size = target.stat().st_size
    if size > MAX_IMAGE_BYTES:
        return False, f"图片过大（{size // 2**20}MB > 10MB 上限）"
    cfg = load_config()
    vurl = normalize_url(cfg.get("vision_api_url") or "")
    vkey = (cfg.get("vision_api_key") or "").strip()
    vmodel = (cfg.get("vision_model") or "").strip()
    if not (vurl and vkey and vmodel):
        return False, ("未配置视觉模型：请在 chat_config.json 设置 "
                       "vision_api_url / vision_api_key / vision_model")
    # 同一图片（内容 hash 未变）+ 同一问题：复用上次分析结果（60s），
    # 避免坐标敏感任务反复重发同一张 base64 图片。
    # 用内容 hash 而非 mtime/size：快速连续写入时 mtime 可能不更新
    # （文件系统时间戳粒度/缓存），内容变化必然反映在 hash 上（规格 §13）。
    import base64
    try:
        img_bytes = target.read_bytes()
    except OSError as exc:
        return False, f"读取图片失败：{exc}"
    _vkey = (str(target), hashlib.sha256(img_bytes).hexdigest()[:16], question[:200])
    with _vimage_lock:
        hit = _vimage_cache.get(_vkey)
    if hit is not None and time.monotonic() - hit[0] <= _VIMAGE_TTL:
        return True, hit[1] + "\n（同一图片与问题，已复用上次分析结果）"
    b64 = base64.b64encode(img_bytes).decode()
    payload = {
        "model": vmodel,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
        "max_tokens": 800,
    }
    # 视觉模型不走推理参数（thinking/reasoning_effort 不适用于视觉接口）
    try:
        data = _call_upstream_raw(vurl, payload, {
            "Content-Type": "application/json", "Authorization": f"Bearer {vkey}"})
    except LlmError as exc:
        return False, f"视觉模型调用失败：{exc.message}"
    content = ((data.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
    if not content:
        return False, "视觉模型返回空内容"
    if _vkey is not None:
        with _vimage_lock:
            _vimage_cache[_vkey] = (time.monotonic(), content)
            if len(_vimage_cache) > 64:
                _vimage_cache.clear()
    return True, content


def _exec_delegate(args: dict, api_url: str | None, headers: dict | None,
                   model: str | None, temperature: float,
                   q: queue.Queue | None, cancel: threading.Event | None,
                   depth: int, task_id: str = "") -> tuple[bool, str]:
    """委派子任务给子 agent：独立上下文 + 工具白名单循环执行，结果摘要返回。"""
    agent_name = (args.get("agent") or "").strip()
    task = (args.get("task") or "").strip()
    agents = _scan_agents()
    spec = next((a for a in agents if a["name"] == agent_name), None)
    if spec is None:
        avail = "、".join(a["name"] for a in agents) or "无"
        return False, f"子 agent 不存在：{agent_name}。可用：{avail}"
    if not task:
        return False, "delegate 需要 task 参数（要委派的任务描述）"
    if depth + 1 >= MAX_AGENT_DEPTH:
        return False, f"委派深度已达上限（{MAX_AGENT_DEPTH} 层），子 agent 不能再委派"
    if api_url is None or q is None:
        return False, "delegate 仅在 agent 循环内可用"
    if cancel is not None and cancel.is_set():
        return False, "任务已由用户中止"
    # ---- R3：子 Agent 智能路由决策（默认单 Agent；无拆分收益 → 拒绝并说明）----
    # 只读子 agent（视觉/审查类，工具全只读）：独立上下文收益，允许委派
    spec_tools = set(spec.get("tools") or [])
    readonly_subagent = bool(spec_tools) and spec_tools <= set(QUERY_TOOLS)
    decision = should_delegate(
        task=task, purpose=task, model=str(model or ""),
        messages=None, complexity=3,
        risk=RISK_LOW if readonly_subagent else RISK_MEDIUM,
        independent_subtasks=1,
        shared_context_tokens=0, user_wants_multi=False,
        agent_def=spec, readonly_subagent=readonly_subagent)
    if not decision.allow:
        log.info("delegate 路由拒绝：%s（%s）", agent_name, decision.reason)
        return (False,
                f"路由决策：不建议委派给子 agent（{decision.reason}）。"
                f"请主 agent 直接完成任务；如确有独立子任务可并行，"
                f"或用户明确要求多 Agent/独立复核，再使用 delegate。")
    # 工具白名单：声明的工具 + 只读工具兜底；不存在/未知的忽略（_agent_loop 过滤）
    known = {t["function"]["name"] for t in _agent_tools()}
    allowed = {t for t in (set(spec.get("tools") or []) | QUERY_TOOLS) if t in known}
    if not allowed:
        return False, f"子 agent {agent_name} 没有可用的工具（tools 白名单为空或全部无效）"
    sub_msgs = [{"role": "system",
                 "content": (spec.get("system_prompt") or "").strip() + AGENT_SYSTEM_SUFFIX
                 + _skill_catalog_text()}]
    sub_msgs.append({"role": "user", "content": task})
    sub_model = spec.get("model") or model
    log.info("delegate -> %s（depth %d，%d 工具）：%s",
             agent_name, depth + 1, len(allowed), task[:80])
    reply = _agent_loop(api_url, headers, sub_msgs, sub_model, temperature,
                        q, cancel, depth=depth + 1,
                        max_steps=_safe_int(args.get("max_steps"), SUBAGENT_MAX_STEPS, 1, 12),
                        tools_filter=allowed, task_id=task_id)
    summary = (reply or "（子 agent 无回复）").strip()[:SUBAGENT_REPLY_CHARS]
    return True, f"子 agent {agent_name} 执行完毕，最终回复：\n{summary}"


# ---- 计划审批（plan）参数级授权 ----
# 批准的计划步骤必须声明精确范围：文件类工具声明 files、shell/进程类声明 commands。
# 未声明范围的高风险工具不授予「免确认」，执行时逐次确认；
# 声明范围后，参数变化 / 路径变化 / 命令变化 / 文件被外部修改 都会触发重新确认。
_PLAN_RANGE_REQUIRED = frozenset({"replace_text", "create_file", "undo",
                                  "run_shell", "start_process", "run_code", "git_commit",
                                  "delete_file", "delete_folder", "move_file",
                                  "rename_file", "copy_file"})
_PLAN_FILE_TOOLS = frozenset({"replace_text", "create_file", "undo",
                              "delete_file", "delete_folder", "move_file",
                              "rename_file", "copy_file"})


def _tool_file_args(name: str, args: dict) -> list[str]:
    """取工具调用中涉及的文件路径参数（相对工作区）。"""
    if name in ("replace_text", "undo"):
        v = args.get("file")
        return [v] if isinstance(v, str) and v else []
    if name in ("create_file", "delete_file", "delete_folder"):
        v = args.get("path")
        return [v] if isinstance(v, str) and v else []
    if name in ("move_file", "rename_file", "copy_file"):
        out = []
        for k in ("src", "dst"):
            v = args.get(k)
            if isinstance(v, str) and v:
                out.append(v)
        return out
    if name == "run_code":
        v = args.get("file")
        return [v] if isinstance(v, str) and v else []
    if name == "git_commit":
        files = args.get("files")
        if isinstance(files, list):
            return [f for f in files if isinstance(f, str)]
        return []
    return []


def _norm_cmd(command: str) -> str:
    """shell 命令规范化：strip + 合并连续空白（用于授权精确比较，禁止 startswith 授权）。"""
    import shlex
    try:
        parts = shlex.split(command or "")
        return " ".join(parts)
    except ValueError:
        # 引号不闭合等异常：退回空白合并（仍为保守比较）
        return " ".join((command or "").split())


def _path_in_scope(target: str, scope: str, workspace=None) -> bool:
    """target 是否在 scope 范围内（scope 可为文件路径或目录前缀，如 src/ 覆盖 src/a.py）。

    workspace 提供时额外做解析后的绝对路径校验：target 的 resolve 结果必须仍位于
    scope 的 resolve 结果内（防符号链接/大小写路径绕过）。
    """
    t = (target or "").strip().replace("\\", "/").strip("/")
    s = (scope or "").strip().replace("\\", "/").strip("/")
    if not t or not s:
        return False
    if t == s or t.startswith(s.rstrip("/") + "/"):
        # 字符串范围命中后，再做绝对路径校验（双保险）
        if workspace is not None:
            try:
                from pathlib import Path as _P
                t_abs = _P(workspace).joinpath(*t.split("/")).resolve()
                s_abs = _P(workspace).joinpath(*s.split("/")).resolve()
                if t_abs == s_abs or (s_abs.is_dir() and s_abs in t_abs.parents):
                    return True
                return False
            except (OSError, ValueError):
                return False
        return True
    return False


def _plan_build_specs(plan_steps: list) -> list[dict]:
    """把批准的计划步骤转为授权规格列表。每个步骤记录：
    tools（工具名集合）、files（文件/目录范围，None=未声明）、
    commands（命令范围，None=未声明，存储规范化形式）。"""
    specs = []
    for s in plan_steps:
        if not isinstance(s, dict):
            continue
        tools = {t for t in (s.get("tools") or []) if isinstance(t, str)}
        files_raw = s.get("files") or []
        commands_raw = s.get("commands") or []
        specs.append({
            "tools": tools,
            "files": frozenset(f for f in files_raw if isinstance(f, str)) or None,
            "commands": frozenset(_norm_cmd(c) for c in commands_raw if isinstance(c, str)) or None,
        })
    return specs


def _plan_authorized(specs: list[dict], name: str, args: dict,
                     workspace=None) -> bool:
    """计划内工具且本次参数在授权范围内 → True（可免确认执行）。

    - 高风险范围类工具（_PLAN_RANGE_REQUIRED）：必须命中步骤声明的 files / commands
      范围，未声明或超范围返回 False（重新确认）；
    - run_code：file 参数必须在声明文件范围；纯 code 字符串不授予免确认（绑定内容）；
    - git_commit：提交文件集合必须 ⊆ 声明文件范围；
    - 命令比较使用规范化精确匹配（禁止 startswith，防止 git status & rm 拼接绕过）；
    - 文件路径命中后额外做 resolve 绝对路径校验（防符号链接/路径穿越）。
    """
    for spec in specs:
        if name not in spec["tools"]:
            continue
        if name in _PLAN_RANGE_REQUIRED:
            if name in ("run_shell", "start_process"):
                commands = spec["commands"]
                if not commands:
                    continue   # 该步骤未声明范围：尝试下一个步骤
                cmd = _norm_cmd(args.get("command") or "")
                if not cmd:
                    return False
                # 规范化后精确匹配（拒绝 startswith：git status 不得授权 git status & rm）
                if cmd not in commands:
                    continue   # 该步骤范围不匹配：尝试下一个步骤
            elif name == "run_code":
                # 绑定代码文件范围；纯 code 字符串无法预绑定 → 不授予免确认
                files = spec["files"]
                if not files:
                    continue
                file_args = _tool_file_args(name, args)
                if not file_args:
                    return False
                if not all(any(_path_in_scope(p, f, workspace) for f in files)
                           for p in file_args):
                    continue
            elif name == "git_commit":
                # 提交范围必须 ⊆ 声明文件范围
                files = spec["files"]
                if not files:
                    continue
                targets = _commit_targets(workspace or _get_workspace(),
                                          workspace or _get_workspace(), args)
                if not targets:
                    return False
                if not all(any(_path_in_scope(rel, f, workspace) for f in files)
                           for rel in targets):
                    continue
            else:  # 文件类工具
                files = spec["files"]
                if not files:
                    continue
                if not all(any(_path_in_scope(p, f, workspace) for f in files)
                           for p in _tool_file_args(name, args)):
                    continue
        return True
    return False


def _plan_tool_names(specs: list[dict]) -> set[str]:
    """计划内声明的全部工具名。"""
    return {t for spec in specs for t in spec["tools"]}


def _file_sha1(p: Path) -> str:
    import hashlib
    h = hashlib.sha1()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_plan_files(plan_steps: list) -> dict:
    """批准时对计划声明的、当前存在的文件做快照 {rel: (size, mtime, sha1)}。"""
    ws = _get_workspace()
    snap: dict[str, tuple] = {}
    for s in plan_steps:
        if not isinstance(s, dict):
            continue
        for f in (s.get("files") or []):
            if not isinstance(f, str):
                continue
            t = _safe_join(ws, f)
            if t is None or not t.is_file():
                continue
            try:
                st = t.stat()
                snap[f] = (st.st_size, st.st_mtime, _file_sha1(t))
            except OSError:
                pass
    return snap


def _plan_files_changed(snapshot: dict) -> list[str]:
    """返回批准后已被外部修改的文件列表（size/mtime/sha1 任一变化）。"""
    ws = _get_workspace()
    changed = []
    for rel, (size, mtime, sha) in snapshot.items():
        t = _safe_join(ws, rel)
        if t is None or not t.is_file():
            changed.append(rel)
            continue
        try:
            st = t.stat()
            if st.st_size != size or st.st_mtime != mtime or _file_sha1(t) != sha:
                changed.append(rel)
        except OSError:
            changed.append(rel)
    return changed


def _timed_execute(fn_name: str, arguments: str, *ctx) -> tuple[bool, str]:
    """执行工具并记录耗时（>=1s 记日志：MCP/网络类工具挂起时可定位）。"""
    t0 = time.monotonic()
    ok, result = _execute_tool(fn_name, arguments, *ctx)
    dt = time.monotonic() - t0
    if dt >= 1.0:
        log.info("tool %s 执行 %.1fs（ok=%s）", fn_name, dt, ok)
    return ok, result


def _confirm_question(name: str, args: dict) -> tuple[str, str | None]:
    """生成 (确认问题, diff 文本)。diff 为空时前端不展示 diff 区域。"""
    if name == "create_file":
        return f"要覆盖已存在的文件 `{args.get('path')}` 吗？", None
    if name == "delete_file":
        return f"要删除文件 `{args.get('path')}` 吗？（删除前自动备份，可用 undo 恢复）", None
    if name == "delete_folder":
        return f"要删除目录 `{args.get('path')}` 吗？（仅空目录，可用 undo 恢复）", None
    if name in ("move_file", "rename_file"):
        return (f"要移动/重命名 `{args.get('src')}` → `{args.get('dst')}` 吗？"
                f"（自动备份，可用 undo 撤销）", None)
    if name == "copy_file":
        dst = args.get("dst")
        existed = False
        ws = _get_workspace()
        t = _safe_join(ws, dst) if dst else None
        if t is not None:
            existed = t.exists()
        return (f"要复制 `{args.get('src')}` → `{dst}` 吗？"
                + ("（目标已存在，将先备份再覆盖）" if existed else ""), None)
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
            # 完整文件列表（staged + unstaged + untracked），未跟踪文件必须在预览中可见
            targets = _commit_targets(root, workspace, args)
            ok2, status = _run_git(root, "status", "--short")
            msg = (args.get("message") or "")[:60]
            files_line = "\n".join(f"  - {r}" for r in targets) or "  （无）"
            status_line = (status or "")[:REPLACE_DIFF_CHARS]
            return (f"要提交 Git 变更吗？提交信息：`{msg}`\n"
                    f"将要提交的文件（{len(targets)} 个）：\n{files_line}\n\n"
                    f"当前状态：\n{status_line or '（无改动）'}", diff)
        return "工作区内未找到 Git 仓库", None
    if name == "start_process":
        return f"要在后台启动进程 `{(args.get('command') or '')[:80]}` 吗？", None
    return "确认执行该操作吗？", None


def _wait_confirm(request_id: str, timeout: float = CONFIRM_TIMEOUT,
                  task_id: str = "") -> str | None:
    """等待用户对确认请求的响应；超时返回 None（视为拒绝）。

    条目绑定 task_id 与过期时间：agent_respond 校验，取消/完成/过期任务的确认被拒绝。
    """
    ev = threading.Event()
    with _confirm_lock:
        _confirm_table[request_id] = {"event": ev, "choice": None,
                                      "task_id": task_id, "source": "http",
                                      "expires": time.monotonic() + timeout}
    ev.wait(timeout)
    with _confirm_lock:
        entry = _confirm_table.pop(request_id, None)
        return entry["choice"] if entry else None


class AskResponse(BaseModel):
    request_id: str = Field(..., description="确认请求 ID")
    choice: str = Field(..., description="用户选择：yes / no 或选项文本")


@app.post("/api/v1/agent/respond", summary="响应 Agent 的确认请求")
async def agent_respond(req: AskResponse) -> dict:
    """响应确认请求。

    生命周期：waiter（_wait_confirm）是确认记录的唯一所有者——它创建、读取并清理记录；
    responder 只能原子地写入 choice 并触发 Event，绝不提前删除记录（否则 waiter 醒来
    拿不到 choice，HTTP 返回 200 但 Agent 仍按拒绝/超时处理）。
    重复响应幂等：已消费的确认不再改变结果。
    """
    with _confirm_lock:
        entry = _confirm_table.get(req.request_id)
        if entry is None:
            raise HTTPException(404, "确认请求不存在或已超时（默认按拒绝处理）")
        if entry["expires"] < time.monotonic():
            raise HTTPException(404, "确认请求已过期（默认按拒绝处理）")
        with _agent_task_lock:
            current_task = _current_agent_task_id
        if entry["task_id"] and entry["task_id"] != current_task:
            raise HTTPException(404, "该确认请求所属任务已结束，无法应答")
        if entry["choice"] is not None:
            # 已消费（重复点击/并发响应）：幂等返回，不改变最终结果
            return {"ok": True, "choice": entry["choice"], "already_processed": True}
        entry["choice"] = req.choice
        entry["event"].set()
    log.info("confirm %s -> %s", req.request_id, req.choice)
    return {"ok": True, "choice": req.choice}


class SessionAppend(BaseModel):
    messages: list[dict] = Field(..., description="新增消息（增量追加，如 [user, assistant]）")
    title: str | None = Field(default=None, description="可选：自定义标题（省略则自动取首条用户消息）")
    request_id: str | None = Field(default=None, description="可选：幂等键（重复请求不重复追加）")
    expected_version: int | None = Field(default=None, description="可选：乐观锁（不匹配返回 409）")


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
                # 拷贝而非活引用：锁外序列化时其他请求可能在 append（L1）
                item["messages"] = list(s.get("messages", []))
            items.append(item)
    return {"ok": True, "sessions": sorted(items, key=lambda s: s["id"])}


@app.post("/api/v1/sessions", summary="创建空会话")
async def session_create() -> dict:
    _ensure_sessions()
    with _session_lock:
        if len(_sessions) >= SESSION_MAX:
            raise HTTPException(409, f"会话数已达上限 {SESSION_MAX}，请先删除旧会话")
        sid = next(_session_id_counter)
        # 记录 canonical workspace：会话与工作区绑定（防旧会话跨目录静默执行）
        try:
            bound_ws = str(_get_workspace().resolve())
        except Exception:
            bound_ws = None
        _sessions[sid] = {"id": sid, "title": "", "messages": [],
                          "updated": time.strftime("%m-%d %H:%M"),
                          "workspace": bound_ws}
        _save_sessions()
    log.info("session created: #%d", sid)
    return {"ok": True, "id": sid, "workspace": bound_ws}


@app.get("/api/v1/sessions/{sid}", summary="读取单个会话（分页：limit/offset 取最近消息）")
async def session_get(sid: int, limit: int = 0, offset: int = 0) -> dict:
    """读取会话。limit>0 时返回最近 limit 条（offset 从最新往前数），并含总数。"""
    _ensure_sessions()
    with _session_lock:
        s = _sessions.get(sid)
        if s is None:
            raise HTTPException(404, f"会话不存在：{sid}")
        if limit > 0:
            msgs = s.get("messages", [])
            start = max(0, len(msgs) - offset - limit)
            chunk = msgs[start:start + limit] if offset > 0 else msgs[-limit:]
            return {"ok": True, "session": {**s, "messages": list(chunk),
                                            "total_messages": len(msgs)}}
        # 拷贝而非活引用：锁外序列化时 session_append 可能正在 extend（L1）
        return {"ok": True, "session": {**s, "messages": list(s.get("messages", []))}}


@app.post("/api/v1/sessions/{sid}/messages", summary="向会话追加消息（增量；返回摘要而非完整历史）")
async def session_append(sid: int, req: SessionAppend) -> dict:
    _ensure_sessions()
    if not req.messages:
        raise HTTPException(422, "messages 不能为空")
    total_req = sum(len(m.get("content") or "") for m in req.messages)
    if total_req > SESSION_APPEND_MAX_CHARS:
        raise HTTPException(413, f"单次追加内容过大（{total_req} 字符 > {SESSION_APPEND_MAX_CHARS}）")
    with _session_lock:
        s = _sessions.get(sid)
        if s is None:
            raise HTTPException(404, f"会话不存在：{sid}")
        # 乐观并发控制：expected_version 不匹配 → 拒绝（多前端并发防覆盖）
        ver = s.get("version", 1)
        if req.expected_version is not None and req.expected_version != ver:
            raise HTTPException(409, f"会话已被其他前端更新（版本 {ver} ≠ {req.expected_version}），请刷新后重试")
        # 幂等：相同 request_id 已处理过 → 直接返回（不重复追加）
        seen_rids = s.setdefault("request_ids", [])
        if req.request_id and req.request_id in seen_rids:
            return {"ok": True, "idempotent": True,
                    "session": {"id": sid, "message_count": len(s["messages"]),
                                "version": s.get("version", 1)}}
        # 事务化：先完整校验全部消息（任一超限 → 整体拒绝，不留部分消息）
        validated = []
        for m in req.messages:
            if m.get("role") in ("user", "assistant") and m.get("content"):
                content = str(m["content"])
                if len(content) > SESSION_MSG_MAX_CHARS:
                    raise HTTPException(413, f"单条消息过长（{len(content)} 字符 > {SESSION_MSG_MAX_CHARS}）")
                validated.append({"role": m["role"], "content": content})
        # 校验通过后一次性修改
        if req.request_id:
            seen_rids.append(req.request_id)
            if len(seen_rids) > 100:
                del seen_rids[:-100]
        msgs = s.setdefault("messages", [])
        msgs.extend(validated)
        # 单条上限（超出丢弃最早）+ 总字符上限
        if len(msgs) > SESSION_MAX_MESSAGES:
            del msgs[:len(msgs) - SESSION_MAX_MESSAGES]
        total_chars = sum(len(m.get("content") or "") for m in msgs)
        while msgs and total_chars > SESSION_TOTAL_MAX_CHARS:
            dropped = msgs.pop(0)
            total_chars -= len(dropped.get("content") or "")
        # 自动标题：首条用户消息前 N 字
        if not s.get("title"):
            for m in msgs:
                if m.get("role") == "user":
                    s["title"] = (m.get("content") or "").strip().replace("\n", " ")[:SESSION_TITLE_CHARS]
                    break
        if req.title:
            s["title"] = req.title[:60]
        s["updated"] = time.strftime("%m-%d %H:%M")
        s["version"] = s.get("version", 1) + 1
        import copy as _copy
        snapshot = _copy.deepcopy(s)
        _persist_sessions_or_rollback(lambda: _rollback_session_state(s, snapshot))
        # 只返回摘要，不返回完整历史（省传输；完整内容按需 GET）
        return {"ok": True, "session": {"id": sid, "message_count": len(msgs),
                                        "title": s.get("title", ""),
                                        "version": s["version"]}}


@app.delete("/api/v1/sessions/{sid}", summary="删除会话")
async def session_delete(sid: int) -> dict:
    _ensure_sessions()
    with _session_lock:
        if sid not in _sessions:
            raise HTTPException(404, f"会话不存在：{sid}")
        saved = _sessions.get(sid)
        del _sessions[sid]
        _persist_sessions_or_rollback(lambda: _rollback_session_restore(sid, saved))
    # R4：删除会话后清除其来源记忆（pinned 显式记忆保留；失败静默）
    try:
        if _memory_enabled():
            _agent_memory.forget_session_memories(sid)
    except Exception:
        pass
    log.info("session deleted: #%d", sid)
    return {"ok": True, "deleted": sid}


@app.delete("/api/v1/sessions/{sid}/messages", summary="清空会话消息（保留会话，用于 /clear）")
async def session_clear(sid: int) -> dict:
    _ensure_sessions()
    with _session_lock:
        s = _sessions.get(sid)
        if s is None:
            raise HTTPException(404, f"会话不存在：{sid}")
        import copy as _copy
        snapshot = _copy.deepcopy(s)
        s["messages"] = []
        s["title"] = ""
        s["updated"] = time.strftime("%m-%d %H:%M")
        _persist_sessions_or_rollback(lambda: _rollback_session_state(s, snapshot))
    return {"ok": True, "cleared": sid}


def _execute_tool(name: str, arguments: str,
                  api_url: str | None = None, headers: dict | None = None,
                  model: str | None = None, temperature: float = 0.7,
                  q: queue.Queue | None = None, cancel: threading.Event | None = None,
                  depth: int = 0, task_id: str = "") -> tuple[bool, str]:
    """执行工具，返回 (ok, 结果文本)。在工作线程中调用。

    delegate 需要上游上下文（api_url/headers/model/q/cancel/depth/task_id），
    由 agent 循环传入；纯函数测试/其他调用可省略。
    """
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
            if target.exists():
                return False, f"目录已存在：{args.get('path')}"
            target.mkdir(parents=True, exist_ok=True)
            _take_backup(workspace, target, op="mkdir")   # 登记（undo 可删除新建目录）
            rel = target.relative_to(workspace).as_posix()
            return True, json.dumps({"created": rel, "absolute": str(target)},
                                    ensure_ascii=False)
        if name == "delete_file":
            workspace = _get_workspace()
            target = _safe_join(workspace, args.get("path", ""))
            if target is None:
                return False, "非法路径：必须是工作区内的相对路径"
            if not target.is_file():
                return False, f"文件不存在：{args.get('path')}"
            if not _take_backup(workspace, target, op="delete"):
                return False, "备份失败，已中止删除（可用性保护）"
            target.unlink()
            _record_modified(target.relative_to(workspace).as_posix())
            return True, json.dumps({"deleted": args.get("path"), "backup": True},
                                    ensure_ascii=False)
        if name == "delete_folder":
            workspace = _get_workspace()
            target = _safe_join(workspace, args.get("path", ""))
            if target is None:
                return False, "非法路径：必须是工作区内的相对路径"
            if not target.is_dir():
                return False, f"目录不存在：{args.get('path')}"
            try:
                if any(target.iterdir()):
                    return False, f"目录非空：{args.get('path')}（请先删除其中的文件）"
            except OSError as exc:
                return False, f"无法读取目录：{exc}"
            if not _take_backup(workspace, target, op="delete_dir"):
                return False, "备份登记失败，已中止删除"
            target.rmdir()
            return True, json.dumps({"deleted": args.get("path"), "backup": True},
                                    ensure_ascii=False)
        if name in ("move_file", "rename_file"):
            workspace = _get_workspace()
            src = _safe_join(workspace, args.get("src", ""))
            dst = _safe_join(workspace, args.get("dst", ""))
            if src is None or dst is None:
                return False, "非法路径：src/dst 必须是工作区内的相对路径"
            if not src.is_file():
                return False, f"源文件不存在：{args.get('src')}"
            if src == dst:
                return False, "源与目标相同，无需移动"
            if dst.exists():
                # 目标被覆盖：先备份目标原内容（undo 恢复双边状态）
                if not _take_backup(workspace, dst, op="overwrite"):
                    return False, "目标备份失败，已中止移动"
            # 源备份最后登记：全局 undo 取到的是 move 条目（双边备份作为整体撤销）
            if not _take_backup(workspace, src, op="move",
                                src=args.get("src"), dst=args.get("dst")):
                return False, "备份失败，已中止移动"
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dst)
            _record_modified(dst.relative_to(workspace).as_posix())
            return True, json.dumps({"moved": [args.get("src"), args.get("dst")],
                                     "backup": True}, ensure_ascii=False)
        if name == "copy_file":
            workspace = _get_workspace()
            src = _safe_join(workspace, args.get("src", ""))
            dst = _safe_join(workspace, args.get("dst", ""))
            if src is None or dst is None:
                return False, "非法路径：src/dst 必须是工作区内的相对路径"
            if not src.is_file():
                return False, f"源文件不存在：{args.get('src')}"
            if src == dst:
                return False, "源与目标相同，无需复制"
            existed = dst.exists()          # 复制前记录（backup 字段必须反映复制前状态）
            if existed:
                if not _take_backup(workspace, dst, op="overwrite"):
                    return False, "目标备份失败，已中止复制"
            dst.parent.mkdir(parents=True, exist_ok=True)
            import shutil as _sh
            _sh.copy2(src, dst)
            _record_modified(dst.relative_to(workspace).as_posix())
            return True, json.dumps({"copied": [args.get("src"), args.get("dst")],
                                     "backup": existed}, ensure_ascii=False)
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
            existed = target.exists()
            if existed:
                if not _take_backup(workspace, target, op="overwrite"):
                    return False, "备份失败，已中止覆盖写入（请检查 .pcagent/backups 权限）"
            else:
                # 新建也记录（undo 可删除新建文件）；无内容可备份，仅登记
                if not _take_backup(workspace, target, op="create"):
                    return False, "备份登记失败，已中止创建"
            _atomic_write_text(target, content)
            _record_modified(target.relative_to(workspace).as_posix())
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
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            total = len(lines)
            start = _safe_int(args.get("start_line"), 1, 1, max(total, 1))
            end = _safe_int(args.get("end_line"), total, start, max(total, start))
            if args.get("start_line") is None and args.get("end_line") is None:
                # 兼容旧行为：返回全文（超长截断）
                text = "\n".join(lines)
                if len(text) > MAX_FILE_CHARS:
                    return True, text[:MAX_FILE_CHARS] + f"\n...(截断，共 {len(text)} 字符)"
                return True, text
            # 分页读取：返回总行数与实际范围
            chunk = "\n".join(lines[start - 1:end])
            if len(chunk) > MAX_FILE_CHARS:
                chunk = chunk[:MAX_FILE_CHARS] + "\n...(截断)"
            return True, (f"（文件 {args['path']} 共 {total} 行，返回第 {start}-{end} 行）\n"
                          + chunk)
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
                    [sys.executable, "-c", code], workspace, RUN_CODE_TIMEOUT,
                    shell=False, env=_controlled_env())
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
            # cwd 安全：默认当前工作区；只允许工作区内的相对路径（拒绝绝对路径与 ..）
            workspace = _get_workspace()
            cwd = str(workspace)
            rel = (args.get("cwd") or "").strip()
            if rel and rel != ".":
                target = _safe_join(workspace, rel)
                if target is None:
                    return False, ("非法 cwd：必须是工作区内的相对路径"
                                   "（拒绝绝对路径、空路径、.. 与符号链接越界）")
                if not target.is_dir():
                    return False, f"目录不存在：{rel}"
                cwd = str(target)
            try:
                rc, out, err, timed_out = _run_subprocess(
                    command, cwd, RUN_SHELL_TIMEOUT, shell=True)
            except FileNotFoundError:
                return False, "shell 执行环境不可用（命令或解释器不存在）"
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
            if not _take_backup(workspace, target):   # 备份失败：中止修改（安全方向）
                return False, "备份失败，已中止修改（请检查 .pcagent/backups 权限）"
            _atomic_write_text(target, new_text)
            _record_modified(target.relative_to(workspace).as_posix())
            return True, json.dumps({"file": args.get("file"), "occurrence": occ,
                                     "replacements": count, "diff": diff,
                                     "backup": True}, ensure_ascii=False)
        if name == "undo":
            workspace = _get_workspace()
            entry = _find_undo_entry((args.get("file") or "").strip())
            if entry is None:
                return False, "没有可撤销的修改（写操作前会自动备份）"
            bdir = _backup_dir() / str(entry["backup"])
            content_path = bdir / "content"
            op = entry.get("op", "overwrite")   # 兼容旧条目（无 op = overwrite）
            target = _safe_join(workspace, entry["file"])
            if target is None:
                return False, "非法路径：备份记录中的路径超出工作区"
            if op == "create":
                # 撤销新建：删除文件（目标不存在视为已删除）
                if target.exists():
                    if not _take_backup(workspace, target, op="create"):
                        return False, "备份失败，已中止撤销（可逆性保护）"
                    if target.is_dir():
                        try:
                            target.rmdir()
                        except OSError as exc:
                            return False, f"目录非空，无法撤销：{exc}"
                    else:
                        target.unlink()
                result_note = f"已删除新建的 {entry['file']}"
            elif op == "delete":
                # 恢复被删除的文件（bytes 无损）
                if not content_path.exists():
                    return False, "备份文件缺失，无法撤销"
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_bytes(target, content_path.read_bytes())
                result_note = f"已恢复被删除的 {entry['file']}"
            elif op == "move":
                # 撤销移动/重命名：移回 src（先备份 dst 当前内容，保证可再撤销），
                # 若移动时覆盖了目标，则从备份恢复目标原内容（完整双边状态）
                dst_rel = entry.get("dst") or entry["file"]
                dst = _safe_join(workspace, dst_rel)
                if dst is None:
                    return False, "非法路径：备份记录中的目标超出工作区"
                extra_pop = None
                if dst.exists():
                    if not _take_backup(workspace, dst, op="overwrite"):
                        return False, "备份失败，已中止撤销"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.replace(target)   # 移回原位置（覆盖前已备份）
                # 目标被覆盖（move 前存在）：恢复其原内容（找到最近的 overwrite 备份）
                with _backup_lock:
                    idx = _backup_index()
                    # 只恢复「move 操作之前」的目标备份（id 小于 move 条目），
                    # 排除 undo 自身刚创建的可逆备份
                    prev = [e for e in idx if e.get("file") == dst_rel
                            and e.get("op") == "overwrite"
                            and int(e.get("id", 0)) < int(entry.get("id", 0))]
                    if prev:
                        last = prev[-1]
                        last_bdir = _backup_dir() / str(last["backup"])
                        last_content = last_bdir / "content"
                        if last_content.exists():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            _atomic_write_bytes(dst, last_content.read_bytes())
                            extra_pop = last
                if extra_pop is not None:
                    with _backup_lock:
                        idx2 = _backup_index()
                        if extra_pop in idx2:
                            idx2.remove(extra_pop)
                            _save_backup_index(idx2)
                    shutil.rmtree(_backup_dir() / str(extra_pop["backup"]), ignore_errors=True)
                result_note = f"已撤销移动 {dst_rel} → {entry['file']}"
            elif op == "mkdir":
                # 撤销新建目录：删除空目录
                if target.is_dir():
                    try:
                        target.rmdir()
                    except OSError as exc:
                        return False, f"目录非空，无法撤销：{exc}"
                result_note = f"已删除新建目录 {entry['file']}"
            elif op == "delete_dir":
                # 恢复被删除的空目录
                target.mkdir(parents=True, exist_ok=True)
                result_note = f"已恢复目录 {entry['file']}"
            else:   # overwrite：恢复覆盖前的备份内容（bytes 无损）
                if not content_path.exists():
                    return False, "备份文件缺失，无法撤销"
                if not _take_backup(workspace, target):   # 恢复前备份当前状态（撤销可逆）
                    return False, "备份失败，已中止撤销"
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_bytes(target, content_path.read_bytes())
                result_note = f"已恢复 {entry['file']}"
            # 移除该备份条目
            with _backup_lock:
                idx = _backup_index()
                if entry in idx:
                    idx.remove(entry)
                    _save_backup_index(idx)
            shutil.rmtree(bdir, ignore_errors=True)
            _record_modified(entry["file"])
            return True, json.dumps({"restored": entry["file"], "time": entry["time"],
                                     "op": op, "note": result_note}, ensure_ascii=False)
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
            global _pending_git_snapshot
            # 工作树快照校验：确认后工作树变化 → 拒绝（要求重新确认）
            snap = _git_worktree_snapshot(root)
            if _pending_git_snapshot and snap != _pending_git_snapshot:
                _pending_git_snapshot = ""
                return False, ("工作树在确认后发生了变化（有其他文件被修改），"
                               "为安全起见本次提交被拒绝，请重新确认后提交")
            # 提交范围：显式 files 或本轮 Agent 修改的文件（绝不用 git add -A）
            targets = _commit_targets(root, workspace, args)
            if not targets:
                return False, ("没有可提交的文件（本轮未修改任何文件，"
                               "或 files 指定的文件均不在仓库内）")
            for rel in targets:
                ok1, err1 = _run_git(root, "add", "--", rel)
                if not ok1:
                    return False, f"git add 失败（{rel}）：{err1}"
            ok2, out2 = _run_git(root, "commit", "-m", message)
            if not ok2:
                return False, f"git commit 失败：{out2}"
            # 提交成功后清空本轮修改记录
            with _modified_lock:
                _agent_modified_files.clear()
            _pending_git_snapshot = ""
            return True, json.dumps({"repo": rel_root, "committed": True,
                                     "message": message, "files": targets,
                                     "result": out2}, ensure_ascii=False)
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
                          "stopped": e.get("stopped", False),
                          "stop_failed": e.get("stop_failed", False),
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
        if name == "load_skill":
            sname = (args.get("name") or "").strip()
            skills = _scan_skills()
            hit = next((s for s in skills if s["name"] == sname), None)
            if hit is None:
                avail = "、".join(s["name"] for s in skills) or "无"
                return False, f"技能不存在：{sname}。可用技能：{avail}"
            text = hit["text"]
            if len(text) > SKILL_MAX_CHARS:
                text = text[:SKILL_MAX_CHARS] + "\n...(技能过长已截断)"
            return True, text
        if name == "system_status":
            return True, _system_status_text()
        if name == "view_image":
            return _exec_view_image(args)
        if name == "fetch_result":
            rid = (args.get("result_id") or "").strip()
            section = (args.get("section") or "tail").strip()
            if not rid:
                return False, "fetch_result 需要 result_id 参数"
            if section not in ("head", "tail", "error", "full"):
                return False, f"非法区段：{section}（可选 head/tail/error/full）"
            text = _result_store.section(rid, section)
            if text is None:
                return False, f"找不到结果 {rid}（可能已过期或被清理，请基于现有摘要继续）"
            return True, text
        if name == "remember":
            content = (args.get("content") or "").strip()
            mtype = args.get("type") or "preference"
            if not content:
                return False, "remember 需要 content 参数"
            if len(content) > 300:
                return False, "记忆内容过长（≤300 字符）"
            if mtype not in ("fact", "preference", "constraint", "decision"):
                return False, f"非法记忆类型：{mtype}"
            # 密钥/注入防御：与自动提取同一套排除规则
            from agent_memory import _is_excluded
            if _is_excluded(content):
                return False, "该内容疑似包含密钥或提示注入，已拒绝写入记忆"
            now = time.time()
            n = _agent_memory.add_memories([{
                "id": uuid.uuid4().hex[:12], "type": mtype, "content": content,
                "scope": "global", "workspace_id": "", "confidence": 1.0,
                "status": "active", "explicit": True, "pinned": False,
                "retrieval_keys": _agent_memory._default_keys(content),
                "source_refs": [], "supersedes": [],
                "created_at": now, "updated_at": now,
                "last_accessed_at": now, "access_count": 0}])
            if n:
                _agent_memory.rebuild_profile()
                return True, f"已记住（{mtype}）：{content[:80]}"
            return True, "该内容已在记忆中（未重复写入）"
        if name == "recall_memory":
            query = (args.get("query") or "").strip()
            scope = args.get("scope") or "memory"
            if not query:
                return False, "recall_memory 需要 query 参数"
            if scope == "scenario":
                sid = (args.get("scenario_id") or "").strip()
                if sid:
                    body = _agent_memory.get_scenario_body(sid)
                    if body is None:
                        return False, f"场景不存在：{sid}"
                    return True, body
                # 场景路径清单（注入只给路径，正文按需加载）
                paths = _agent_memory.list_scenario_paths()
                if not paths:
                    return True, json.dumps({"scenarios": []}, ensure_ascii=False)
                return True, json.dumps({"scenarios": paths[:20]}, ensure_ascii=False)
            if scope == "profile":
                return True, (_agent_memory.profile_inject_text(limit=1000)
                              or "（暂无画像）")
            ws_id = str(_get_workspace())
            hits = _agent_memory.recall_memories(query, top_k=5, workspace_id=ws_id)
            if not hits:
                return True, json.dumps({"hits": []}, ensure_ascii=False)
            return True, json.dumps({"hits": hits}, ensure_ascii=False)
        if name == "codegraph_query":
            mode = args.get("mode") or "symbol"
            ws = _get_workspace()
            try:
                files_iter = _iter_workspace_files(ws)
                if mode == "build":
                    res = _agent_memory.build_codegraph(ws, files_iter)
                    return True, json.dumps({"built": True, "files": len(res.get("files", {})),
                                             "truncated": res.get("truncated", False)},
                                            ensure_ascii=False)
                # 先尝试直接查询；索引不存在时构建
                if mode == "symbol":
                    sym = (args.get("symbol") or "").strip()
                    if not sym:
                        return False, "codegraph_query(symbol) 需要 symbol 参数"
                    q = _agent_memory.codegraph_query(ws, sym)
                    if q.get("ok"):
                        return True, json.dumps(q, ensure_ascii=False)
                elif mode == "impact":
                    f = (args.get("file") or "").strip()
                    if not f:
                        return False, "codegraph_query(impact) 需要 file 参数"
                    imp = _agent_memory.codegraph_impact(ws, f)
                    if imp.get("ok"):
                        return True, json.dumps(imp, ensure_ascii=False)
                else:
                    return False, f"非法模式：{mode}"
                # 索引缺失 → 构建后重试一次
                _agent_memory.build_codegraph(ws, files_iter)
                if mode == "symbol":
                    return True, json.dumps(_agent_memory.codegraph_query(ws, sym),
                                            ensure_ascii=False)
                return True, json.dumps(_agent_memory.codegraph_impact(ws, f),
                                        ensure_ascii=False)
            except Exception as exc:
                return False, f"代码图谱失败：{exc}"
        if name == "delegate":
            return _exec_delegate(args, api_url, headers, model, temperature,
                                  q, cancel, depth, task_id)
        if name.startswith("mcp_"):
            # MCP 外部工具转发（mcp_<server>_<tool>）
            mcp = _ensure_mcp()
            if mcp is None or not mcp.conns:
                return False, "MCP 未配置（mcp_config.json 为空）"
            ok, result = mcp.call(name, arguments)
            if not ok:
                log.warning("MCP 工具 %s 调用失败：%s", name, result[:200])
            return ok, result
        if name == "create_plan":
            return False, "create_plan 仅在计划审批模式（/confirm-mode plan）下使用，由 agent 循环处理"
        return False, f"未知工具：{name}"
    except Exception as exc:
        return False, f"工具执行异常：{exc}"


# ---- R3：跨轮只读结果缓存（防重复读取同一文件；文件变化即失效）----
_READ_CACHE_TTL = 30.0            # 缓存有效期（秒）
_READ_CACHE_PATH_ARGS = ("path", "file", "src", "dst", "folder", "directory")

# ---- R3：agent 循环历史窗口（防上下文线性膨胀）----
_HISTORY_WINDOW_ROUNDS = 8        # 发送给上游保留的最近轮数；更早轮次压缩为摘要
_HISTORY_SUMMARY_CHARS = 700      # 早期轮次摘要长度上限


def _compact_early_rounds(early: list[dict]) -> str:
    """把早期已完成轮次压缩为紧凑摘要（工具名 + 结果首行）。

    只记录外部可验证的事实（做了什么/结果如何），不保留原始大输出——
    模型在后续轮次通常只需知道「已完成 X，结果 Y」，细节可通过 fetch_result 取回。
    """
    parts: list[str] = []
    for m in early:
        role = m.get("role")
        if role == "user":
            # 轮次间的用户指令/约束不能静默丢弃（M4）：压缩进摘要
            content = str(m.get("content") or "").strip()
            if content and len(content) < 200:
                parts.append(f"用户：{content[:120]}")
        elif role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                name = fn.get("name") or "?"
                args_txt = ""
                try:
                    a = json.loads(fn.get("arguments") or "{}")
                    if isinstance(a, dict):
                        p = a.get("path") or a.get("file")
                        if isinstance(p, str) and p:
                            args_txt = f" {p}"
                except (json.JSONDecodeError, ValueError):
                    pass
                parts.append(f"{name}{args_txt}")
        elif m.get("role") == "tool":
            content = str(m.get("content") or "")
            first = content.splitlines()[0][:50] if content else ""
            if first:
                parts.append(f"→{first}")
    text = "；".join(parts)
    return text[:_HISTORY_SUMMARY_CHARS]


def _window_messages(messages: list[dict]) -> list[dict]:
    """发送给上游的消息窗口：system + 用户目标 + 最近 N 轮完整消息；
    更早的已完成轮次压缩为一条紧凑摘要（assistant/tool 配对保持完整）。

    只读操作，不修改传入列表；轮数未超限时原样返回（零开销）。
    """
    sys_end = 0
    while sys_end < len(messages) and messages[sys_end].get("role") == "system":
        sys_end += 1
    rest = messages[sys_end:]
    # 用户目标消息（首个非 system 段，通常 1-2 条）必须保留
    user_end = 0
    while user_end < len(rest) and rest[user_end].get("role") == "user":
        user_end += 1
    body = rest[user_end:]
    # 按 assistant 消息分轮（每轮: assistant(tool_calls) + 其后的 tool 结果）
    round_starts: list[int] = []
    for i, m in enumerate(body):
        if m.get("role") == "assistant":
            round_starts.append(i)
    if len(round_starts) <= _HISTORY_WINDOW_ROUNDS:
        return messages
    keep_start = round_starts[-_HISTORY_WINDOW_ROUNDS]
    early = body[:keep_start]
    keep = body[keep_start:]
    summary = _compact_early_rounds(early)
    if not summary:
        return messages
    log.debug("history window: %d 轮 → 摘要（保留最近 %d 轮）",
              len(round_starts), _HISTORY_WINDOW_ROUNDS)
    return messages[:sys_end] + rest[:user_end] + [
        {"role": "system",
         "content": f"（早期执行记录，用于替代已完成轮次的原始消息）{summary}"},
    ] + keep


def _read_cache_key(name: str, args: dict) -> tuple[str, str]:
    return (name, json.dumps(args, sort_keys=True, ensure_ascii=False))


def _read_cache_paths(args: dict) -> dict:
    """从参数提取工作区文件路径 → (mtime, size) 状态。无路径参数返回空（不缓存）。"""
    out: dict[str, tuple[float, int]] = {}
    ws = _get_workspace()
    for key in _READ_CACHE_PATH_ARGS:
        val = args.get(key)
        if isinstance(val, str) and val:
            target = _safe_join(ws, val)
            if target is not None and target.is_file():
                try:
                    st = target.stat()
                    out[val] = (st.st_mtime, st.st_size)
                except OSError:
                    pass
    return out


def _read_cache_hit(cache: dict, name: str, args: dict) -> tuple[bool, str] | None:
    """只读缓存命中：文件参数未变化且未超 TTL 才复用（动态工具不缓存）。"""
    if not _is_query_tool(name, args):
        return None
    entry = cache.get(_read_cache_key(name, args))
    if entry is None:
        return None
    ts, ok, result, paths = entry
    if time.monotonic() - ts > _READ_CACHE_TTL:
        return None
    for rel, (mtime, size) in paths.items():
        target = _safe_join(_get_workspace(), rel)
        if target is None or not target.is_file():
            return None
        try:
            st = target.stat()
        except OSError:
            return None
        if st.st_mtime != mtime or st.st_size != size:
            return None   # 文件已变化：缓存失效
    return (ok, result)


def _read_cache_put(cache: dict, name: str, args: dict, ok: bool, result: str) -> None:
    """登记只读缓存：只缓存带文件路径参数的查询工具，避免缓存动态状态。"""
    paths = _read_cache_paths(args)
    if paths and _is_query_tool(name, args):
        cache[_read_cache_key(name, args)] = (time.monotonic(), ok, result, paths)


def _is_verification_result(tool_name: str, result: str) -> bool:
    """工具结果是否构成「验证通过」证据（Skill 候选/lesson 三要素用）。"""
    if tool_name not in ("run_code", "run_shell"):
        return False
    r = (result or "")
    return any(k in r for k in ("PASS", "通过", "passed", "✓", "ok=true",
                                "全部通过", "0 failed", "0 失败"))


def _agent_loop(api_url: str, headers: dict, messages: list[dict],
                model: str, temperature: float, q: queue.Queue,
                cancel: threading.Event, depth: int = 0,
                max_steps: int | None = None,
                tools_filter: set[str] | None = None,
                task_id: str = "",
                run_record: dict | None = None) -> str:
    """后台线程：工具调用循环，事件经 queue 发送给 SSE 生成器。

    安全锁：轮数上限 / 总调用数上限 / 单轮上限 / 连续失败熔断 / 总时长上限 / 取消事件。
    事件: ("tool_call", {...}) / ("tool_result", {...}) /
          ("delta", text) / ("done", None) / ("error", msg)
    返回最终回复文本（主循环忽略；delegate 用它取子 agent 结果）。
    depth > 0 时（子 agent 循环）：工具轮数用 max_steps、tools 用白名单、
    不套用计划审批模式（授权已在主层完成）。task_id 绑定确认请求（主循环传入）。
    run_record（可选，主循环传入）：AgentRunRecord 收集器，填充
    started_at/tool_events/final_answer/status/finished_at（记忆系统用）。
    """
    global _current_agent_task_id
    global _pending_git_snapshot
    if run_record is not None:
        run_record.setdefault("started_at", time.time())
        run_record.setdefault("tool_events", [])
        run_record.setdefault("status", "")
    if depth == 0 and task_id:
        with _agent_task_lock:
            _current_agent_task_id = task_id
    # 任务级隔离（M1/M2）：残留的 git 快照与本轮修改记录不得污染下一个任务。
    # 入口清空快照（上一任务可能确认后中止留下过期快照）；修改文件记录
    # 保存基线、退出时还原——子任务 A 未提交的修改不会被任务 B 误提交。
    _modified_baseline: set[str] = set()
    if depth == 0:
        _pending_git_snapshot = ""
        with _modified_lock:
            _modified_baseline = set(_agent_modified_files)
    # 工作区 epoch：切换工作区后旧任务在下一检查点中止（不允许旧任务操作新工作区）
    ws_epoch = _workspace_epoch
    start = time.monotonic()
    tool_calls_total = 0
    consecutive_failures = 0
    _lim_steps, _lim_secs = _agent_limits()
    step_limit = max_steps or _lim_steps
    _MAX_SECS = _lim_secs
    # ---- 计划审批模式状态（plan）：任务先列计划，统一批准后按计划执行 ----
    # 仅主循环（depth=0）生效；子 agent 的授权由主层批准委托
    # 授权是参数级的：步骤声明 files/commands 范围，范围内免确认；
    # 范围外参数、文件被外部修改、计划外工具都会重新确认/拒绝。
    plan_mode = _current_confirm_mode() == "plan" and depth == 0
    plan_submitted = not plan_mode
    approved_specs: list[dict] = []
    approved_plan_steps: list = []
    plan_file_snapshot: dict = {}
    PLAN_CONFIRM_TIMEOUT = 300   # 计划审批等待更宽松（看表格+思考需要时间）
    # 跨轮只读结果缓存：{key: (ts, ok, result, {path: (mtime, size)})}
    read_cache: dict = {}

    try:
        for step in range(1, step_limit + 1):
            # 安全锁检查
            if cancel.is_set():
                if depth == 0:
                    q.put(("error", "已由用户中止"))
                return "已由用户中止"
            if _workspace_changed(ws_epoch):
                if depth == 0:
                    q.put(("error", "工作区已切换，旧任务已中止（不允许操作新工作区）"))
                return "工作区已切换，任务中止"
            if time.monotonic() - start > _MAX_SECS:
                if depth == 0:
                    q.put(("error", f"任务超过总时长上限 {_MAX_SECS}s，已自动中止"))
                return f"任务超过总时长上限 {_MAX_SECS}s，已自动中止"
            if tool_calls_total >= MAX_TOOL_CALLS_TOTAL:
                if depth == 0:
                    q.put(("error", f"工具调用总数超过上限 {MAX_TOOL_CALLS_TOTAL}，已自动中止"))
                return f"工具调用总数超过上限 {MAX_TOOL_CALLS_TOTAL}，已自动中止"

            payload = {"model": model, "messages": _window_messages(messages),
                       "temperature": temperature,
                       "tools": _agent_tools()}
            if tools_filter is not None:
                # 子 agent：工具白名单（只保留声明的工具），优先于路由
                payload["tools"] = [t for t in payload["tools"]
                                    if t["function"]["name"] in tools_filter]
            elif depth == 0 and tools_filter is None and not (plan_mode and plan_submitted):
                # 主循环：工具路由过滤（开关关/降级时 _route_tools 返回 None = 全量）
                # plan 模式批准后跳过路由：计划内工具可能被路由过滤导致计划执行失败
                routed = _route_tools(messages)
                if routed is not None:
                    payload["tools"] = routed
            _apply_reasoning(payload)
            try:
                data = _call_upstream_raw(api_url, payload, headers)
            except LlmError as exc:
                if depth == 0:
                    q.put(("error", exc.message))
                return f"上游错误：{exc.message}"
            msg = data["choices"][0]["message"]
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                # 无工具调用：最终回复，模拟分块流式（前端打字机效果）
                content = msg.get("content") or ""
                # 先写 run_record 再发 done：SSE 侧收到 done 立即提交记忆记录，
                # 若字段后写，提取线程会看到 status="" 被记为 failed，已完成任务的
                # 偏好/约束提取与 lesson 全部丢失（H2 竞态）
                if run_record is not None:
                    run_record["final_answer"] = content
                    run_record["status"] = "completed"
                    run_record["finished_at"] = time.time()
                    run_record.setdefault("verification_ok", any(
                        e.get("verification") for e in run_record.get("tool_events", [])))
                for i in range(0, len(content), 4):
                    q.put(("delta", content[i:i + 4]))
                if depth == 0:
                    q.put(("done", None))
                log.info("agent done after %d step(s)", step)
                return content or "（模型未返回内容）"

            # 模型要求调用工具
            if cancel.is_set():
                if depth == 0:
                    q.put(("error", "已由用户中止"))
                return "已由用户中止"
            # ---- 批次校验与限额（硬边界，绝不突破 MAX_TOOL_CALLS_TOTAL）----
            # 1) 单轮上限：模型一次返回过多调用直接整批拒绝
            if len(tool_calls) > MAX_TOOL_CALLS_PER_ROUND:
                q.put(("error", f"模型单轮返回 {len(tool_calls)} 个工具调用，超过单轮上限 "
                                f"{MAX_TOOL_CALLS_PER_ROUND}，任务已中止"))
                return f"模型单轮工具调用超过上限 {MAX_TOOL_CALLS_PER_ROUND}"
            # 2) 结构校验：缺少 id / function / name 或重复 id 的调用安全拒绝（跳过）
            valid: list[dict] = []
            seen_ids: set[str] = set()
            for tc in tool_calls:
                tc_id = tc.get("id")
                fn = tc.get("function")
                if (not isinstance(tc_id, str) or not tc_id
                        or not isinstance(fn, dict) or not isinstance(fn.get("name"), str)
                        or not fn["name"]):
                    continue
                if tc_id in seen_ids:
                    continue
                seen_ids.add(tc_id)
                valid.append(tc)
            if not valid:
                q.put(("error", "模型返回的 tool_calls 全部无效（缺少 id/function 或重复），任务已中止"))
                return "模型返回的 tool_calls 全部无效"
            # 3) 剩余额度：只执行允许的数量，绝不突破总量上限
            remaining = MAX_TOOL_CALLS_TOTAL - tool_calls_total
            if len(valid) > remaining:
                log.warning("tool_calls 请求 %d 个，剩余额度 %d，截断执行", len(valid), remaining)
                valid = valid[:remaining]
            tool_calls = valid
            tool_calls_total += len(tool_calls)
            names = ",".join((tc.get("function") or {}).get("name", "?") for tc in tool_calls)
            log.info("agent step %d: [%s] (total %d)", step, names, tool_calls_total)
            messages.append({"role": "assistant", "content": msg.get("content") or None,
                             "tool_calls": tool_calls})
            # 同轮重复检测：相同 (工具, 参数) 的调用直接复用本轮结果（依赖状态必然相同）
            round_cache: dict[tuple[str, str], tuple[bool, str]] = {}
            for tc in tool_calls:
                if cancel.is_set():
                    if depth == 0:
                        q.put(("error", "已由用户中止"))
                    return "已由用户中止"
                fn = tc["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments 不是 JSON 对象")
                except (json.JSONDecodeError, ValueError):
                    args = None
                if args is None:
                    # 非法参数：安全拒绝，不执行工具
                    log.warning("tool %s 参数非法已拒绝：%.120s", fn.get("name"), fn.get("arguments") or "")
                    result = "工具参数不是合法 JSON，已安全拒绝（不执行）"
                    ok = False
                    q.put(("tool_result", {"id": tc["id"], "ok": ok, "result": result}))
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        q.put(("error", f"连续 {MAX_CONSECUTIVE_FAILURES} 次工具调用失败，"
                                        f"熔断器已触发，任务自动中止"))
                        return f"连续 {MAX_CONSECUTIVE_FAILURES} 次工具调用失败，熔断器已触发"
                    continue
                # 同轮重复调用：相同 (工具, 参数) 直接复用本轮结果（依赖状态必然相同，
                # 防止模型重复调用同一只读工具 / 反复执行同一失败操作）
                fp = (fn["name"], fn.get("arguments") or "")
                if fp in round_cache:
                    prev_ok, prev_result = round_cache[fp]
                    result = prev_result + "\n（同轮重复调用同一工具与参数，已复用本次结果）"
                    ok = prev_ok
                    q.put(("tool_call", {"id": tc["id"], "name": fn["name"],
                                         "arguments": fn["arguments"],
                                         "step": step, "max_steps": MAX_TOOL_STEPS}))
                    q.put(("tool_result", {"id": tc["id"], "ok": ok, "result": result,
                                           "reused": True}))
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                    continue
                # 跨轮只读结果缓存：文件参数未变化时避免重复读取同一文件（30s TTL）
                cache_hit = (None if fp in round_cache
                             else _read_cache_hit(read_cache, fn["name"], args))
                if cache_hit is not None:
                    prev_ok, prev_result = cache_hit
                    result = prev_result + "\n（文件未变化，已复用上次只读结果）"
                    ok = prev_ok
                    q.put(("tool_call", {"id": tc["id"], "name": fn["name"],
                                         "arguments": fn["arguments"],
                                         "step": step, "max_steps": MAX_TOOL_STEPS}))
                    q.put(("tool_result", {"id": tc["id"], "ok": ok, "result": result,
                                           "reused": True}))
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                    continue
                q.put(("tool_call", {"id": tc["id"], "name": fn["name"],
                                     "arguments": fn["arguments"],
                                     "step": step, "max_steps": MAX_TOOL_STEPS}))
                # 工具执行前检查总时长（覆盖上游等待/确认等待/MCP 调用累计耗时）
                if time.monotonic() - start > _MAX_SECS:
                    if depth == 0:
                        q.put(("error", f"任务超过总时长上限 {_MAX_SECS}s，已自动中止"))
                    return f"任务超过总时长上限 {_MAX_SECS}s"
                # ---- 计划审批模式：先规划，批准后按计划执行 ----
                if plan_mode and not plan_submitted:
                    if fn["name"] == "create_plan":
                        plan_steps = args.get("steps") or []
                        if not isinstance(plan_steps, list) or not plan_steps:
                            result = "计划无效：steps 必须是非空数组（每个步骤含 step/tools）"
                            ok = False
                        else:
                            ask_id = _new_confirm_id(task_id)
                            q.put(("ask", {"id": ask_id, "name": "create_plan",
                                           "arguments": fn["arguments"],
                                           "question": "任务计划需要你的批准：批准后按计划执行，"
                                                       "计划内声明的范围（files/commands）内操作免确认；"
                                                       "范围外参数、文件被外部修改会重新确认。",
                                           "options": ["yes", "no"],
                                           "plan": plan_steps}))
                            choice = _wait_confirm(ask_id, timeout=PLAN_CONFIRM_TIMEOUT,
                                                   task_id=task_id)
                            if time.monotonic() - start > _MAX_SECS:
                                # 确认等待也计入任务总时长
                                q.put(("error", f"任务超过总时长上限 {_MAX_SECS}s（含确认等待），已自动中止"))
                                return f"任务超过总时长上限 {_MAX_SECS}s"
                            if choice == "yes":
                                approved_specs = _plan_build_specs(plan_steps)
                                approved_plan_steps = plan_steps
                                plan_file_snapshot = _snapshot_plan_files(plan_steps)
                                plan_submitted = True
                                names = _plan_tool_names(approved_specs)
                                result = (f"计划已批准（{len(plan_steps)} 步，"
                                          f"授权工具：{', '.join(sorted(names)) or '无'}），"
                                          f"开始按计划执行（范围外参数将重新确认）")
                                ok = True
                            else:
                                result = "计划被用户拒绝，请停止执行并询问用户如何调整"
                                ok = False
                    elif _is_query_tool(fn["name"], args):
                        # 只读操作（查询类工具/简单只读 shell/只读 MCP）天然无害：免规划直接执行
                        ok, result = _timed_execute(fn["name"], fn["arguments"], api_url, headers, model, temperature, q, cancel, depth, task_id)
                    else:
                        result = ("计划审批模式下，写操作执行前必须先用 create_plan 提交计划"
                                  "（列出步骤与所需工具，用户批准后才可执行）")
                        ok = False
                # ---- 常规执行（或计划已批准）----
                elif plan_mode and fn["name"] == "create_plan":
                    # 计划已批准后重复规划：提示而非误导
                    result = "计划已提交并批准，直接按计划执行即可，无需重复规划；如需调整请询问用户"
                    ok = False
                elif plan_mode and fn["name"] in _plan_tool_names(approved_specs):
                    # 计划内工具：参数必须命中批准范围，且目标文件未被外部修改；
                    # 不满足 → 重新确认（显示实际参数与原因），用户拒绝则不执行。
                    authorized = _plan_authorized(approved_specs, fn["name"], args, workspace=_get_workspace())
                    changed = _plan_files_changed(plan_file_snapshot) if authorized else []
                    if authorized and not changed:
                        ok, result = _timed_execute(fn["name"], fn["arguments"],
                                                    api_url, headers, model, temperature, q, cancel, depth)
                        if ok:
                            # 免确认执行成功后刷新快照（agent 自己的修改不算外部变化）
                            plan_file_snapshot = _snapshot_plan_files(approved_plan_steps)
                    else:
                        hint = (f"（文件已被外部修改：{', '.join(changed[:3])}）" if changed
                                else "（参数超出计划批准的范围）")
                        ask_id = _new_confirm_id(task_id)
                        question = f"工具 `{fn['name']}` 需要重新确认{hint}"
                        q.put(("ask", {"id": ask_id, "name": fn["name"],
                                       "arguments": fn["arguments"],
                                       "question": question,
                                       "options": ["yes", "no"],
                                       "diff": _confirm_question(fn["name"], args)[1]}))
                        choice = _wait_confirm(ask_id, task_id=task_id)
                        if time.monotonic() - start > _MAX_SECS:
                            q.put(("error", f"任务超过总时长上限 {_MAX_SECS}s（含确认等待），已自动中止"))
                            return f"任务超过总时长上限 {_MAX_SECS}s"
                        if choice == "yes":
                            ok, result = _timed_execute(fn["name"], fn["arguments"],
                                                        api_url, headers, model, temperature, q, cancel, depth)
                            if ok:
                                plan_file_snapshot = _snapshot_plan_files(approved_plan_steps)
                        else:
                            log.info("plan tool %s re-confirm rejected by user", fn["name"])
                            result = "用户拒绝了该操作，请勿执行；可询问用户或改用其他方式"
                            ok = False
                else:
                    # ---- 按问询模式决定处理方式：allow 直接执行 / ask 确认 / deny 拒绝 ----
                    policy = _confirm_policy(fn["name"], args)
                    if policy == "deny":
                        ok = False
                        result = (f"当前为只读模式（query），操作 {fn['name']} 已被拒绝；"
                                  f"如需执行请先切换到其他问询模式")
                    elif policy == "ask":
                        ask_id = _new_confirm_id(task_id)
                        question, diff = _confirm_question(fn["name"], args)
                        q.put(("ask", {"id": ask_id, "name": fn["name"],
                                       "arguments": fn["arguments"],
                                       "question": question,
                                       "options": ["yes", "no"], "diff": diff}))
                        choice = _wait_confirm(ask_id, task_id=task_id)
                        if time.monotonic() - start > _MAX_SECS:
                            q.put(("error", f"任务超过总时长上限 {_MAX_SECS}s（含确认等待），已自动中止"))
                            return f"任务超过总时长上限 {_MAX_SECS}s"
                        if choice == "yes":
                            if fn["name"] == "git_commit":
                                # 批准时记录工作树快照：执行前变化则要求重新确认
                                ws_root = _find_git_root(_get_workspace(), args.get("path", ""))
                                _pending_git_snapshot = (_git_worktree_snapshot(ws_root)
                                                         if ws_root is not None else "")
                            ok, result = _timed_execute(fn["name"], fn["arguments"],
                                                        api_url, headers, model, temperature, q, cancel, depth, task_id)
                        else:
                            log.info("tool %s rejected by user (%s)", fn["name"], choice)
                            result = "用户拒绝了该操作，请勿执行；可询问用户或改用其他方式"
                            ok = False
                    else:
                        ok, result = _timed_execute(fn["name"], fn["arguments"], api_url, headers, model, temperature, q, cancel, depth, task_id)
                # 工具结果压缩：完整原文存本地（result_id 引用，模型可 fetch_result 取回），
                # 发送给模型的是 head/error/tail 分区保留的摘要（错误绝不丢）
                if len(result) > MAX_TOOL_RESULT_CHARS:
                    result, _rmeta = reduce_tool_result(
                        fn["name"], args, result, ok,
                        max_chars=MAX_TOOL_RESULT_CHARS, store=_result_store)
                    log.info("tool %s 结果 %d 字符 → 压缩为 %d（可 fetch_result 取回 %s）",
                             fn["name"], _rmeta["original_chars"], _rmeta["reduced_chars"],
                             _rmeta["result_id"])
                else:
                    _rmeta = None
                round_cache[fp] = (ok, result)
                _read_cache_put(read_cache, fn["name"], args, ok, result)
                if run_record is not None:
                    run_record["tool_events"].append({
                        "name": fn["name"],
                        "args_preview": (fn.get("arguments") or "")[:200],
                        "ok": ok,
                        "verification": _is_verification_result(fn["name"], result),
                    })
                q.put(("tool_result", {"id": tc["id"], "ok": ok, "result": result,
                                       **({"reduced": True, "result_id": _rmeta["result_id"]}
                                          if _rmeta and _rmeta.get("result_id") else {})}))
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                # 任务清单变化：推送 todo_update 事件（前端刷新任务面板）
                if fn["name"] in ("create_todo", "update_todo"):
                    q.put(("todo_update", _todos_snapshot()))
                # 连续失败熔断
                consecutive_failures = 0 if ok else consecutive_failures + 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    q.put(("error", f"连续 {MAX_CONSECUTIVE_FAILURES} 次工具调用失败，"
                                    f"熔断器已触发，任务自动中止"))
                    return f"连续 {MAX_CONSECUTIVE_FAILURES} 次工具调用失败，熔断器已触发"
        q.put(("error", f"工具调用超过 {step_limit} 轮上限，已自动中止"))
        return f"工具调用超过 {step_limit} 轮上限，已自动中止"


    except Exception as exc:
        # H1 兜底：循环内任何未捕获异常都必须转成 error 事件，
        # 否则 SSE 生成器在 q.get 上永久阻塞、全局 agent 锁泄漏
        log.exception("agent 循环未捕获异常，任务已安全终止")
        if depth == 0:
            q.put(("error", f"任务内部错误已自动中止：{type(exc).__name__}"))
        if run_record is not None and not run_record.get("status"):
            run_record["status"] = "failed"
            run_record["finished_at"] = time.time()
        return f"任务内部错误：{type(exc).__name__}"
    finally:
        # 任务级隔离（M1/M2）：退出时清空快照、还原修改记录基线
        if depth == 0:
            _pending_git_snapshot = ""
            with _modified_lock:
                _agent_modified_files.clear()
                _agent_modified_files.update(_modified_baseline)

def _agent_limits() -> tuple[int, int]:
    """工具轮数/任务时长上限：优先配置（设置界面保存后实时生效），否则默认常量。"""
    cfg = load_config()
    steps = _safe_int(cfg.get("max_tool_steps"), MAX_TOOL_STEPS, 1, 50)
    secs = _safe_int(cfg.get("max_agent_seconds"), MAX_AGENT_SECONDS, 10, 3600)
    return steps, secs


def _new_task_id() -> str:
    """agent 任务唯一 ID（确认请求绑定 / 旧任务隔离）。"""
    import uuid
    return uuid.uuid4().hex[:12]


async def _release_lock_when_done(fut) -> None:
    """后台 watcher：worker 最终结束后释放全局 agent 锁（防锁永久泄漏，幂等）。"""
    try:
        await asyncio.shield(fut)
    except Exception:
        pass
    finally:
        with _agent_task_lock:
            _current_agent_task_id = ""
        try:
            _agent_lock.release()
        except RuntimeError:
            pass   # 已释放（幂等）
        log.info("agent worker 已结束，全局锁已释放")


async def _agent_stream_events(api_url: str, headers: dict, messages: list[dict],
                               model: str, temperature: float,
                               session_id: int | None = None,
                               request_id: str | None = None,
                               workspace: str | None = None,
                               session_version: int | None = None):
    """把 agent 循环的事件流转发为 SSE；客户端断开时通知循环线程停止。

    并发互斥：持有 _agent_lock 期间不允许第二条 Agent 任务启动；
    客户端断开后必须等 worker 真正结束才释放锁（防止旧任务工具与
    新任务并发执行文件/Shell/MCP/屏幕操作）。worker 无法及时取消时
    保持全局 busy，由 watcher 在 worker 结束后释放。

    会话身份（可选）→ AgentRunRecord → MemoryWorker（记忆提取，失败静默）。
    """
    if not _agent_lock.acquire(blocking=False):
        yield f"event: error\ndata: {json.dumps({'detail': '已有另一个 Agent 任务正在执行（或旧任务仍在终止），请等待完成'}, ensure_ascii=False)}\n\n"
        return
    cancel = threading.Event()
    q: queue.Queue = queue.Queue()
    task_id = _new_task_id()
    loop = asyncio.get_running_loop()
    run_record = {"session_id": session_id, "request_id": request_id or task_id,
                  "workspace": workspace or "",
                  "session_version": session_version,
                  "input_messages": [dict(m) for m in messages],
                  "status": "", "final_answer": "", "tool_events": [],
                  "started_at": 0, "finished_at": 0}
    fut = loop.run_in_executor(None, _agent_loop, api_url, headers, messages, model,
                               temperature, q, cancel, 0, None, None, task_id,
                               run_record)
    released = False

    def _release_lock() -> None:
        nonlocal released
        if not released:
            _agent_lock.release()
            released = True

    async def _agent_cleanup() -> None:
        """任务结束清理：清任务 ID + 残留确认条目 + 释放锁（幂等，可多次调用）。"""
        global _current_agent_task_id
        with _agent_task_lock:
            _current_agent_task_id = ""
        with _confirm_lock:
            stale = [rid for rid, e in _confirm_table.items()
                     if e.get("task_id") == task_id]
            for rid in stale:
                entry = _confirm_table.pop(rid, None)
                if entry is not None and entry["event"] is not None:
                    entry["event"].set()   # 唤醒等待方（醒来 pop 不到记录 → 按拒绝）
        if fut.done():
            _release_lock()

    try:
        while True:
            # 超时轮询：worker 异常退出（未发事件）时 fut 完成，据此终止而不是
            # 在 q.get 上永久阻塞（否则客户端挂起、全局 agent 锁无法释放）
            try:
                kind, data = await loop.run_in_executor(
                    None, lambda: q.get(timeout=5))
            except queue.Empty:
                if fut.done():
                    exc = fut.exception()
                    log.warning("agent worker 异常退出（%r），SSE 终止",
                                exc if exc is not None else "无异常信息")
                    await _agent_cleanup()
                    _submit_memory_record(run_record,
                                          status="cancelled" if cancel.is_set() else "failed")
                    detail = (f"任务执行异常终止：{type(exc).__name__}"
                              if exc is not None else "任务已终止（worker 未返回）")
                    yield f"event: error\ndata: {json.dumps({'detail': detail}, ensure_ascii=False)}\n\n"
                    break
                continue
            if kind == "done":
                await _agent_cleanup()   # 显式清理：async generator 的 finally 依赖 aclose
                _submit_memory_record(run_record, status=None)  # 状态由 loop 填充
                break
            if kind == "error":
                await _agent_cleanup()
                _submit_memory_record(run_record, status="cancelled" if cancel.is_set() else "failed")
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
        # async generator 的 finally 仅在 aclose（框架关闭流）时执行；
        # done/error 分支已显式清理，此处为 aclose/异常路径兜底（幂等）。
        await _agent_cleanup()
        if fut.done():
            _release_lock()
            return
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout=15)
            _release_lock()
        except (asyncio.TimeoutError, asyncio.CancelledError):
            log.warning("agent worker 未能及时退出，保持全局 busy（旧任务仍在终止）")
            loop.create_task(_release_lock_when_done(fut))


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
        # R4：动态记忆上下文作为独立 system 消息插入（不拼进稳定前缀，
        # 避免破坏提示词缓存；L3 注入版 + 去重后的 L1 召回）
        dyn = _dynamic_memory_message(
            next((str(m.get("content") or "") for m in reversed(messages)
                  if m.get("role") == "user"), ""),
            workspace=str(req.workspace or ""))
        if dyn:
            # 插到静态 system 之后、用户消息之前：静态前缀（system+工具定义）
            # 保持字节级稳定才能命中 provider 前缀缓存；动态记忆放最前会让
            # 每次请求的第一个 token 就不同 → 整个前缀缓存全部失效（H3）
            insert_at = 0
            while insert_at < len(messages) and messages[insert_at].get("role") == "system":
                insert_at += 1
            messages.insert(insert_at, {"role": "system", "content": dyn})
        appended = False
        for m in messages:
            if m.get("role") != "system":
                continue
            if "（动态记忆上下文" in (m.get("content") or ""):
                continue    # 动态记忆消息保持独立：SUFFIX 加到原 system
            m["content"] = (m.get("content") or "") + AGENT_SYSTEM_SUFFIX
            # 注入当前任务清单：中断后模型能感知进度（半恢复）
            todo_note = _todos_system_note()
            if todo_note:
                m["content"] += todo_note
            # 注入可用技能包清单（只注入清单，全文按需 load_skill）
            m["content"] += _skill_catalog_text()
            # 注入可用子 agent 清单（delegate 委派）
            m["content"] += _agent_catalog_text()
            # 计划审批模式：提示先规划
            if _current_confirm_mode() == "plan":
                m["content"] += ("\n\n（当前为计划审批模式：收到任务后先用 create_plan 提交计划，"
                                 "列出步骤与所需工具；用户批准前不要执行任何工具。"
                                 "批准后按计划执行，计划内操作免确认。）")
            appended = True
            break
        if not appended:
            messages.insert(0, {"role": "system",
                                "content": AGENT_SYSTEM_SUFFIX.lstrip() + _todos_system_note()
                                + _skill_catalog_text() + _agent_catalog_text()})
        return StreamingResponse(
            _agent_stream_events(api_url, headers, messages, model, req.temperature,
                                 session_id=req.session_id, request_id=req.request_id,
                                 workspace=req.workspace,
                                 session_version=req.session_version),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    payload = {"model": model, "messages": _trim_messages(req.messages),
               "temperature": req.temperature, "stream": True}
    _apply_reasoning(payload)
    return StreamingResponse(
        _stream_events(api_url, payload, headers),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _upstream_reader(api_url: str, payload: dict, headers: dict, q: queue.Queue,
                     holder: dict | None = None) -> None:
    """后台线程：读取上游 SSE 流，逐行放入 queue（保持 data: 前缀原样）。
    holder 用于向事件循环暴露响应对象（客户端断开时 close 以停止读取，防 token 浪费）。"""
    req = urllib.request.Request(
        api_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            if holder is not None:
                holder["resp"] = resp
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
    holder: dict = {}
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _upstream_reader, api_url, payload, headers, q, holder)
    try:
        while True:
            # q.get 是阻塞调用，放在线程池执行，不阻塞事件循环
            kind, data = await loop.run_in_executor(None, q.get)
            if kind == "done":
                break
            if kind == "error":
                yield f"event: error\ndata: {json.dumps({'detail': data})}\n\n"
                break
            yield data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    except asyncio.CancelledError:
        # 客户端断开：关闭上游响应，停止读取（避免继续消耗 Token）
        resp = holder.get("resp")
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        log.info("chat stream 客户端断开，上游读取已停止")
        raise


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
    # 安全要求：绑定非回环地址时必须提供 token（否则拒绝启动）
    if not _is_loopback(args.host) and not args.token:
        print(f"错误：绑定非回环地址（{args.host}）时必须提供 --token 鉴权，"
              f"否则任何局域网内主机都能控制本机。\n"
              f"请加 --token <随机字符串> 后重试，或改回 127.0.0.1。")
        sys.exit(1)
    AUTH_TOKEN = args.token   # 模块级赋值即修改全局
    ISOLATED = args.isolated
    # ---- 密钥迁移：旧明文 api_key → 安全存储（配置文件只保留占位符）----
    try:
        from secure_store import PLACEHOLDER as _SS_PH, store as _ss_store
        from secure_store import platform_warning as _ss_warn
        cfg0 = load_config()
        plain = (cfg0.get("api_key") or "").strip()
        if plain and plain != _SS_PH:
            _ss_store("api_key", plain)
            cfg0["api_key"] = _SS_PH
            _write_config_atomic(cfg0)
            log.info("旧明文 api_key 已迁移到安全存储")
        warn = _ss_warn()
        if warn:
            log.warning(warn)
    except Exception as exc:
        log.warning("密钥安全存储初始化失败：%s", exc)
    # 日志级别：设置界面保存的 log_level 生效（重启后）
    try:
        lvl = str(load_config().get("log_level") or "info").upper()
        logging.getLogger().setLevel(getattr(logging, lvl, logging.INFO))
    except Exception:
        pass
    if args.isolated:
        log.warning("隔离模式：屏幕操作工具已禁用，仅保留 %s", sorted(_FILE_TOOLS))
    if args.token:
        log.warning("token 鉴权已启用，客户端需携带 X-Api-Token 头")
    # Windows（含 Mirrored 共享网络栈的 WSL）上 TIME_WAIT 连接会阻止 bind：
    # 预绑定 socket 并设 SO_REUSEADDR，避免旧进程退出后端口短暂不可用
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    sock.listen(2048)
    uvicorn.Server(uvicorn.Config(app, log_level="info")).run(sockets=[sock])
