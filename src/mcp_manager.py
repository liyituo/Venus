"""
MCP 管理器接入层 — 从 llm_server 拆出的独立模块。

- 读取 mcp_config.json（servers 段）；
- 惰性初始化 McpManager（PCAGENT_DISABLE_MCP=1 时跳过，测试环境用）；
- _is_readonly_mcp：MCP 工具只读判定（配置声明优先，未声明按写处理；
  兼容旧配置前缀/精确名规则）。

llm_server.py 通过 `from mcp_manager import *` re-export，保持既有引用兼容。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from brand import env_is_set

log = logging.getLogger("llm-backend")

_mcp_manager = None
_mcp_init_lock = threading.Lock()   # 初始化互斥：启动预热线程与 /health 并发只创建一个 Manager
MCP_START_TIMEOUT = 2.0             # 首次连接等待上限（秒）：不阻塞健康检查

# 只读 MCP 工具判定：只来自显式配置声明（mcp_config.json 的
# read_only_tools/write_tools，未声明按写处理）。不依赖任何 server 名称
# 前缀的"默认只读"后门（tavily/amap 等旧规则已移除）。
MCP_READONLY_TOOLS: frozenset[str] = frozenset()


def _load_mcp_config() -> dict:
    """读取 mcp_config.json 的 servers 段（含 token，已 gitignore；示例见 mcp_config.example.json）。"""
    try:
        p = Path(__file__).resolve().parent.parent / "mcp_config.json"
        if p.exists():
            cfg = json.loads(p.read_text(encoding="utf-8"))
            return cfg.get("servers") or {}
    except Exception as exc:
        log.warning("mcp_config.json 解析失败：%s", exc)
    return {}


def _ensure_mcp():
    """惰性初始化 MCP 管理器（首次访问工具列表时连接各 server）。

    - 初始化加锁：启动预热线程与 /health 并发访问时只创建一个 Manager；
    - PCAGENT_DISABLE_MCP=1 时跳过（测试环境用，避免真实连接外部 server）；
    - 首次连接最多等待 MCP_START_TIMEOUT 秒（不阻塞健康检查 20 秒）。
    """
    global _mcp_manager
    if _mcp_manager is None and env_is_set(("VENUS_DISABLE_MCP", "PCAGENT_DISABLE_MCP")):
        return None
    with _mcp_init_lock:
        if _mcp_manager is None:
            from mcp_client import McpManager
            _mcp_manager = McpManager(_load_mcp_config())
            _mcp_manager.start()
    return _mcp_manager


def _is_readonly_mcp(name: str) -> bool:
    """MCP 工具只读判定：只来自显式配置声明（read_only_tools/write_tools）。

    未声明一律按写处理（保守）；不依赖任何 server 名称前缀。
    """
    mcp = _ensure_mcp()
    if mcp is not None:
        try:
            r = mcp.is_readonly(name)
            if r is not None:
                return r
        except Exception:
            pass
    return False


def get_manager_state() -> list[dict]:
    """返回各 MCP server 的状态（name/connected/tool_count/error）。

    供诊断入口使用；未初始化或已禁用时返回空列表。
    """
    manager = _mcp_manager
    if manager is None:
        return []
    result = []
    for name, conn in manager.conns.items():
        result.append({
            "name": name,
            "connected": conn.session is not None,
            "tool_count": len(conn.tools),
            "error": conn.error,
        })
    return result
