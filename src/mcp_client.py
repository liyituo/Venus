"""
MCP 客户端层 — 把外部 MCP server 的工具动态接入 agent。

- 独立 asyncio 事件循环线程管理连接（stdio / streamable-http）
- 每个连接由常驻协程持有（MCP 协议要求 context 进入/退出在同一任务）
- 工具调用通过 run_coroutine_threadsafe 从工作线程转发

配置（mcp_config.json，含 token 已 gitignore；示例见 mcp_config.example.json）：
  {
    "servers": {
      "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                 "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "..."}},
      "memory": {"command": "/path/python", "args": ["tests/mcp_echo_server.py"]},
      "web":    {"transport": "http", "url": "http://127.0.0.1:8888/mcp"}
    }
  }
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading

log = logging.getLogger("mcp-client")


class McpConnection:
    """单个 MCP server 连接：常驻协程持有 stdio/http context，工具调用跨任务转发。"""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.session = None
        self.tools: list[dict] = []      # [{name, description, inputSchema}]
        self.error = ""
        self.ready: asyncio.Event | None = None
        self.stop_event: asyncio.Event | None = None

    async def _run(self) -> None:
        """常驻连接协程（在 manager 事件循环中运行，context 进出同一任务）。"""
        try:
            if self.config.get("transport", "stdio") == "stdio":
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client
                params = StdioServerParameters(
                    command=self.config["command"],
                    args=self.config.get("args") or [],
                    env=self.config.get("env") or None,
                )
                async with stdio_client(params) as (read, write):
                    await self._serve(ClientSession(read, write))
            else:
                from mcp import ClientSession
                from mcp.client.streamable_http import streamablehttp_client
                async with streamablehttp_client(
                    self.config["url"], headers=self.config.get("headers") or {}
                ) as (read, write):
                    await self._serve(ClientSession(read, write))
        except Exception as exc:
            self.error = str(exc)[:300]
            log.warning("MCP server %s 异常：%s", self.name, self.error)
        finally:
            if self.ready is not None:
                self.ready.set()          # 唤醒等待方（session 可能为 None = 失败）
            self.session = None

    async def _serve(self, session) -> None:
        async with session:
            await session.initialize()
            self.session = session
            res = await session.list_tools()
            self.tools = [{
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.inputSchema or {"type": "object", "properties": {}},
            } for t in res.tools]
            log.info("MCP server %s 已连接：%d 个工具", self.name, len(self.tools))
            if self.ready is not None:
                self.ready.set()
            await self.stop_event.wait()  # 保持连接直到显式停止

    async def call(self, tool: str, arguments: dict) -> tuple[bool, str]:
        result = await self.session.call_tool(tool, arguments or {})
        parts = []
        for c in result.content or []:
            if getattr(c, "type", "") == "text":
                parts.append(c.text or "")
            elif getattr(c, "type", "") == "image":
                parts.append(f"[图片 {getattr(c, 'mimeType', '?')} {len(getattr(c, 'data', b'') or b'')}B]")
            elif getattr(c, "type", "") == "resource":
                parts.append(f"[资源 {getattr(getattr(c, 'resource', None), 'uri', '?')}]")
        ok = not bool(getattr(result, "isError", False))
        return ok, "\n".join(parts) or "(无输出)"


class McpManager:
    """多 server 管理：独立事件循环线程 + 同步调用桥接。"""

    def __init__(self, servers_config: dict):
        self.servers = servers_config or {}
        self.conns: dict[str, McpConnection] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    # ---- 生命周期（llm_server 启动时调用）----
    def start(self) -> None:
        if not self.servers or self._thread is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mcp-loop")
        self._thread.start()
        for name, cfg in self.servers.items():
            conn = McpConnection(name, cfg)
            conn.ready = asyncio.Event()
            conn.stop_event = asyncio.Event()
            self.conns[name] = conn
            conn.task = self._loop.create_task(conn._run())
            # 等待连接结果（成功或失败，20 秒超时）
            try:
                asyncio.run_coroutine_threadsafe(conn.ready.wait(), self._loop).result(timeout=20)
            except Exception:
                pass
        ok = sum(1 for c in self.conns.values() if c.session is not None)
        log.info("MCP: %d/%d server 连接成功", ok, len(self.servers))

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # ---- 工具定义（合并进 AGENT_TOOLS）----
    def all_tools(self) -> list[dict]:
        """返回 AGENT_TOOLS 格式的工具定义，mcp_<server>_<tool> 前缀防重名。"""
        tools = []
        for name, conn in self.conns.items():
            for t in conn.tools:
                tools.append({"type": "function", "function": {
                    "name": f"mcp_{name}_{t['name']}",
                    "description": f"[MCP:{name}] {t['description']}",
                    "parameters": t["inputSchema"] or {"type": "object", "properties": {}},
                }})
        return tools

    # ---- 工具调用（工作线程同步调用）----
    def call(self, tool_name: str, arguments: str) -> tuple[bool, str]:
        parts = tool_name.split("_", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            return False, "无效的 MCP 工具名"
        _, server, tool = parts
        conn = self.conns.get(server)
        if conn is None or conn.session is None:
            return False, f"MCP server '{server}' 未连接：{conn.error if conn else '未配置'}"
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return False, "MCP 工具参数不是合法 JSON"
        fut = asyncio.run_coroutine_threadsafe(conn.call(tool, args), self._loop)
        try:
            return fut.result(timeout=120)
        except asyncio.TimeoutError:
            return False, "MCP 工具调用超时（>120s）"
        except Exception as exc:
            return False, f"MCP 调用失败：{exc}"

    def stop(self) -> None:
        """停止所有连接（常驻协程在同一任务内退出 context，符合 MCP 协议要求）。"""
        if self._loop is None:
            return
        for conn in self.conns.values():
            if conn.stop_event is not None:
                try:
                    asyncio.run_coroutine_threadsafe(
                        conn.stop_event.set(), self._loop).result(timeout=5)
                except Exception:
                    pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop = None
