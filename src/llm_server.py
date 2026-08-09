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
APP_VERSION = "0.7.1"       # 系统版本（health 端点返回，前端可展示）
UPSTREAM_TIMEOUT = 180  # 模型生成可能较慢（reasoner 更慢）
DAEMON_BASE = "http://127.0.0.1:8000"   # 屏幕控制 daemon（app.py）

# ---- Agent 安全锁（防循环调用导致系统崩溃）----
MAX_TOOL_STEPS = 10             # 单次请求最多工具轮数
MAX_TOOL_CALLS_TOTAL = 30       # 单次请求工具调用总数上限（一轮可含多个调用）
MAX_CONSECUTIVE_FAILURES = 4    # 连续失败熔断阈值：达到即停止整个任务
MAX_AGENT_SECONDS = 420         # 单次 agent 请求总耗时上限（含上游生成时间；max 推理思考较久，放宽至 7 分钟）
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

# ---- 问询模式（五种）----
CONFIRM_MODES = ("auto", "strict", "trusted", "query", "plan")
CONFIRM_MODE_DESC = {
    "auto":    "智能：敏感写操作确认，只读命令免确认（默认）",
    "strict":  "严格：所有修改/执行类操作都需确认",
    "trusted": "信任：全部自动执行（危险命令黑名单仍拦截）",
    "query":   "只读：仅允许查询操作，一切修改直接拒绝",
    "plan":    "计划：任务先列计划表格（步骤+所需工具），统一批准后按计划执行，计划内免确认",
}
QUERY_TOOLS = {  # 查询类工具（任何模式都放行）
    "list_folder", "read_file", "get_screen_size",
    "search_text", "glob_files", "list_symbols",
    "git_status", "git_diff", "git_log",
    "process_output", "list_processes", "list_todos", "repo_map",
    "load_skill", "system_status", "view_image",
}


def _current_confirm_mode() -> str:
    return str(load_config().get("confirm_mode", "auto"))


# 只读 MCP server 前缀：其全部工具视为查询操作（auto 免确认 / plan 免规划）
# 当前：tavily（网络搜索/提取）、amap（地图查询，脚本 scripts/mcp_servers/amap_server.py）
MCP_READONLY_PREFIXES = ("mcp_tavily_", "mcp_amap_")

# 混合型 server（如 spotify：搜索只读、播放写）按工具名精确标记只读
MCP_READONLY_TOOLS = {
    "mcp_spotify_search_tracks", "mcp_spotify_search_artists", "mcp_spotify_search_albums",
    "mcp_spotify_get_album_tracks", "mcp_spotify_get_my_playlists",
    "mcp_spotify_get_my_top_artists", "mcp_spotify_get_my_top_tracks",
    "mcp_spotify_get_now_playing", "mcp_spotify_get_playlist_tracks",
    "mcp_spotify_get_saved_tracks", "mcp_spotify_get_server_version",
}


def _is_readonly_mcp(name: str) -> bool:
    return any(name.startswith(p) for p in MCP_READONLY_PREFIXES) or name in MCP_READONLY_TOOLS


def _confirm_policy(name: str, args: dict) -> str:
    """按当前问询模式决定工具处理方式。
    返回：allow（直接执行）/ ask（需用户确认）/ deny（直接拒绝）
    """
    mode = _current_confirm_mode()
    if mode == "trusted":
        return "allow"
    is_query = name in QUERY_TOOLS or _is_readonly_mcp(name) or (
        name == "run_shell" and _is_readonly_shell((args.get("command") or "").strip()))
    if mode == "query":
        return "allow" if is_query else "deny"
    if mode == "strict":
        return "allow" if is_query else "ask"
    # auto
    if name.startswith("mcp_"):
        if _is_readonly_mcp(name):
            return "allow"    # 只读 MCP（如 tavily 搜索）：免确认
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
    "load_skill", "system_status", "view_image", "delegate",
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
            return {**default, **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}
        except Exception:
            log.warning("chat_config.json 解析失败，使用默认配置")
    return default


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
ROUTER_TIMEOUT = 5.0        # 路由调用超时（秒）：gemma3:1b 热推理 ~2.2s，留余量
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


def _route_tools(messages: list[dict]) -> list[dict] | None:
    """路由入口：返回应注入的工具子集；返回 None = 降级（全量工具）。

    调用链：开关检查 → 缓存 → 规则 → 模型 → 宽松解析；任一步失败回退全量。
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
            cat = hit[1]
            if cat is not None:
                tools = _tools_for_category(cat)
                log.info("router 缓存命中: %s（%d 工具）", cat, len(tools))
                return tools
            return None
    # 规则前置
    cat = _route_rules(query)
    src = "规则"
    if cat is None:
        # 模型路由
        try:
            out = _call_router(query, cfg)
            cat = _parse_route_output(out)
            src = "模型"
        except Exception as exc:
            log.warning("router 模型调用失败（降级全量）: %s", exc)
            cat = None
    with _router_lock:
        _router_cache[cache_key] = (time.monotonic(), cat)
    if cat is None:
        log.info("router 未命中（降级全量）: %.60s", query)
        return None
    tools = _tools_for_category(cat)
    log.info("router %s命中 %s: %d 工具（%.60s）", src, cat, len(tools), query)
    return tools


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
                servers.add(name.split("_", 2)[1])
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


class CompressRequest(BaseModel):
    messages: list[dict] = Field(..., description="待压缩的完整消息列表")
    keep_recent: int = Field(default=8, ge=2, le=30, description="保留最近 N 条消息不压缩")


class ConfigUpdate(BaseModel):
    api_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    context_window: int | None = None
    reasoning_mode: str | None = None


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

    # 用模型生成早期对话摘要（非流式，少量 token）；摘要任务关闭思考，更快更省
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": COMPRESS_PROMPT},
            {"role": "user", "content": early_json},
        ],
        "temperature": 0.3,
        "max_tokens": 500,
    }
    _apply_reasoning(payload, "off")
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
    if req.reasoning_mode is not None:
        if req.reasoning_mode not in REASONING_MODES:
            raise HTTPException(422, f"无效推理强度，可选：{' / '.join(REASONING_MODES)}")
        cfg["reasoning_mode"] = req.reasoning_mode
        updates["reasoning_mode"] = True
    if not updates:
        raise HTTPException(
            422, "没有可更新的字段（支持 api_url/api_key/model/context_window/reasoning_mode）")
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
        "reasoning_mode": cfg.get("reasoning_mode", "max"),
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
        "name": "create_plan",
        "description": "（计划审批模式）任务开始前提交执行计划：列出每个步骤、每步需要的工具和原因。"
                       "用户批准后按计划执行，计划内声明的工具不再逐个确认；计划外的操作仍需确认。"
                       "只声明计划，不执行任何实际动作。",
        "parameters": {"type": "object",
                       "properties": {
                           "steps": {"type": "array",
                                     "items": {"type": "object",
                                               "properties": {
                                                   "step": {"type": "string", "description": "步骤描述"},
                                                   "tools": {"type": "array", "items": {"type": "string"},
                                                             "description": "本步骤需要的工具名（如 create_file、run_shell）"},
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
    """可用技能清单（只注入清单不注入全文，模型按需 load_skill 加载）。"""
    skills = _scan_skills()
    if not skills:
        return ""
    lines = ["可用技能包（任务匹配某技能时，用 load_skill 加载该技能全文再执行）："]
    for s in skills:
        lines.append(f"- {s['name']}：{s['description'] or '无描述'}")
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
    note = "\n".join(lines)
    # 防膨胀：todo 多/描述长时截断（系统注入的预算有限）
    if len(note) > TODOS_NOTE_MAX_CHARS:
        note = note[:TODOS_NOTE_MAX_CHARS] + "\n...（任务清单过长已截断）"
    return note


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
    import base64
    try:
        b64 = base64.b64encode(target.read_bytes()).decode()
    except OSError as exc:
        return False, f"读取图片失败：{exc}"
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
    return True, content


def _exec_delegate(args: dict, api_url: str | None, headers: dict | None,
                   model: str | None, temperature: float,
                   q: queue.Queue | None, cancel: threading.Event | None,
                   depth: int) -> tuple[bool, str]:
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
                        tools_filter=allowed)
    summary = (reply or "（子 agent 无回复）").strip()[:SUBAGENT_REPLY_CHARS]
    return True, f"子 agent {agent_name} 执行完毕，最终回复：\n{summary}"


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


def _execute_tool(name: str, arguments: str,
                  api_url: str | None = None, headers: dict | None = None,
                  model: str | None = None, temperature: float = 0.7,
                  q: queue.Queue | None = None, cancel: threading.Event | None = None,
                  depth: int = 0) -> tuple[bool, str]:
    """执行工具，返回 (ok, 结果文本)。在工作线程中调用。

    delegate 需要上游上下文（api_url/headers/model/q/cancel/depth），
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
        if name == "delegate":
            return _exec_delegate(args, api_url, headers, model, temperature, q, cancel, depth)
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


def _agent_loop(api_url: str, headers: dict, messages: list[dict],
                model: str, temperature: float, q: queue.Queue,
                cancel: threading.Event, depth: int = 0,
                max_steps: int | None = None,
                tools_filter: set[str] | None = None) -> str:
    """后台线程：工具调用循环，事件经 queue 发送给 SSE 生成器。

    安全锁：轮数上限 / 总调用数上限 / 连续失败熔断 / 总时长上限 / 取消事件。
    事件: ("tool_call", {...}) / ("tool_result", {...}) /
          ("delta", text) / ("done", None) / ("error", msg)
    返回最终回复文本（主循环忽略；delegate 用它取子 agent 结果）。
    depth > 0 时（子 agent 循环）：工具轮数用 max_steps、tools 用白名单、
    不套用计划审批模式（授权已在主层完成）。
    """
    start = time.monotonic()
    tool_calls_total = 0
    consecutive_failures = 0
    step_limit = max_steps or MAX_TOOL_STEPS
    # ---- 计划审批模式状态（plan）：任务先列计划，统一批准后按计划执行 ----
    # 仅主循环（depth=0）生效；子 agent 的授权由主层批准委托
    plan_mode = _current_confirm_mode() == "plan" and depth == 0
    plan_submitted = not plan_mode
    approved_tools: set[str] = set()
    PLAN_CONFIRM_TIMEOUT = 300   # 计划审批等待更宽松（看表格+思考需要时间）

    for step in range(1, step_limit + 1):
        # 安全锁检查
        if cancel.is_set():
            if depth == 0:
                q.put(("error", "已由用户中止"))
            return "已由用户中止"
        if time.monotonic() - start > MAX_AGENT_SECONDS:
            if depth == 0:
                q.put(("error", f"任务超过总时长上限 {MAX_AGENT_SECONDS}s，已自动中止"))
            return f"任务超过总时长上限 {MAX_AGENT_SECONDS}s，已自动中止"
        if tool_calls_total >= MAX_TOOL_CALLS_TOTAL:
            if depth == 0:
                q.put(("error", f"工具调用总数超过上限 {MAX_TOOL_CALLS_TOTAL}，已自动中止"))
            return f"工具调用总数超过上限 {MAX_TOOL_CALLS_TOTAL}，已自动中止"

        payload = {"model": model, "messages": messages, "temperature": temperature,
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
        tool_calls_total += len(tool_calls)
        names = ",".join((tc.get("function") or {}).get("name", "?") for tc in tool_calls)
        log.info("agent step %d: [%s] (total %d)", step, names, tool_calls_total)
        messages.append({"role": "assistant", "content": msg.get("content") or None,
                         "tool_calls": tool_calls})
        for tc in tool_calls:
            if cancel.is_set():
                if depth == 0:
                    q.put(("error", "已由用户中止"))
                return "已由用户中止"
            fn = tc["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            q.put(("tool_call", {"id": tc["id"], "name": fn["name"],
                                 "arguments": fn["arguments"],
                                 "step": step, "max_steps": MAX_TOOL_STEPS}))
            # ---- 计划审批模式：先规划，批准后按计划执行 ----
            if plan_mode and not plan_submitted:
                if fn["name"] == "create_plan":
                    plan_steps = args.get("steps") or []
                    if not isinstance(plan_steps, list) or not plan_steps:
                        result = "计划无效：steps 必须是非空数组（每个步骤含 step/tools）"
                        ok = False
                    else:
                        ask_id = f"ask-{next(_confirm_counter)}"
                        q.put(("ask", {"id": ask_id, "name": "create_plan",
                                       "arguments": fn["arguments"],
                                       "question": "任务计划需要你的批准：批准后按计划执行，"
                                                   "计划内声明的操作不再逐个确认；计划外操作仍会确认。",
                                       "options": ["yes", "no"],
                                       "plan": plan_steps}))
                        choice = _wait_confirm(ask_id, timeout=PLAN_CONFIRM_TIMEOUT)
                        if choice == "yes":
                            approved_tools = {t for s in plan_steps
                                              for t in (s.get("tools") or [])}
                            plan_submitted = True
                            result = (f"计划已批准（{len(plan_steps)} 步，"
                                      f"授权工具：{', '.join(sorted(approved_tools)) or '无'}），"
                                      f"开始按计划执行")
                            ok = True
                        else:
                            result = "计划被用户拒绝，请停止执行并询问用户如何调整"
                            ok = False
                elif (fn["name"] in QUERY_TOOLS or _is_readonly_mcp(fn["name"]) or
                      (fn["name"] == "run_shell" and
                       _is_readonly_shell((args.get("command") or "").strip()))):
                    # 只读操作（查询类工具/只读 shell/只读 MCP）天然无害：免规划直接执行
                    ok, result = _timed_execute(fn["name"], fn["arguments"], api_url, headers, model, temperature, q, cancel, depth)
                else:
                    result = ("计划审批模式下，写操作执行前必须先用 create_plan 提交计划"
                              "（列出步骤与所需工具，用户批准后才可执行）")
                    ok = False
            # ---- 常规执行（或计划已批准）----
            elif plan_mode and fn["name"] == "create_plan":
                # 计划已批准后重复规划：提示而非误导
                result = "计划已提交并批准，直接按计划执行即可，无需重复规划；如需调整请询问用户"
                ok = False
            elif plan_mode and fn["name"] in approved_tools:
                # 计划内声明的工具：免确认直接执行
                ok, result = _timed_execute(fn["name"], fn["arguments"], api_url, headers, model, temperature, q, cancel, depth)
            else:
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
                        ok, result = _timed_execute(fn["name"], fn["arguments"], api_url, headers, model, temperature, q, cancel, depth)
                else:
                    ok, result = _timed_execute(fn["name"], fn["arguments"], api_url, headers, model, temperature, q, cancel, depth)
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
                return f"连续 {MAX_CONSECUTIVE_FAILURES} 次工具调用失败，熔断器已触发"
    q.put(("error", f"工具调用超过 {step_limit} 轮上限，已自动中止"))
    return f"工具调用超过 {step_limit} 轮上限，已自动中止"


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
                # 注入可用技能包清单（只注入清单，全文按需 load_skill）
                m["content"] += _skill_catalog_text()
                # 注入可用子 agent 清单（delegate 委派）
                m["content"] += _agent_catalog_text()
                # 计划审批模式：提示先规划
                if _current_confirm_mode() == "plan":
                    m["content"] += ("\n\n（当前为计划审批模式：收到任务后先用 create_plan 提交计划，"
                                     "列出步骤与所需工具；用户批准前不要执行任何工具。"
                                     "批准后按计划执行，计划内操作免确认。）")
                break
        else:
            messages.insert(0, {"role": "system",
                                "content": AGENT_SYSTEM_SUFFIX.lstrip() + _todos_system_note()
                                + _skill_catalog_text() + _agent_catalog_text()})
        return StreamingResponse(
            _agent_stream_events(api_url, headers, messages, model, req.temperature),
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
        # 客户端断开：无法强停上游读取线程（urllib 阻塞），记录后正常退出
        log.info("chat stream 客户端断开，上游读取线程将自然结束")
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
    AUTH_TOKEN = args.token   # 模块级赋值即修改全局
    ISOLATED = args.isolated
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
