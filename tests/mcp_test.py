"""MCP 客户端集成测试：连接测试 server、拉工具列表、转发调用。

另验证与 llm_server 的集成（动态工具合并 + _execute_tool 转发 + 确认策略）。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import llm_server as L  # noqa: E402
from mcp_client import McpManager  # noqa: E402

_PY = sys.executable
_ECHO = str(Path(__file__).resolve().parent / "mcp_echo_server.py")
CONFIG = {"echo": {"command": _PY, "args": [_ECHO]}}

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


# ============ 1. 连接与工具列表 ============
print("== 1. 连接 / 工具列表 ==")
mgr = McpManager(CONFIG)
mgr.start()
check("echo server 连接", mgr.conns.get("echo") is not None and mgr.conns["echo"].session is not None,
      str({k: v.error for k, v in mgr.conns.items()}))
tools = mgr.all_tools()
names = [t["function"]["name"] for t in tools]
check("工具带 mcp_ 前缀", "mcp_echo_echo" in names and "mcp_echo_add" in names, str(names))
echo_tool = next(t for t in tools if t["function"]["name"] == "mcp_echo_echo")
check("schema 映射", "text" in echo_tool["function"]["parameters"]["properties"], str(echo_tool))
check("描述带来源", "[MCP:echo]" in echo_tool["function"]["description"], "")

# ============ 2. 调用转发 ============
print("== 2. 工具调用转发 ==")
ok, res = mgr.call("mcp_echo_echo", json.dumps({"text": "你好"}))
check("echo 调用", ok and res == "echo: 你好", res)
ok, res = mgr.call("mcp_echo_add", json.dumps({"a": 3, "b": 4}))
check("add 调用", ok and res == "7", res)
ok, res = mgr.call("mcp_echo_add", json.dumps({"a": "x"}))
check("参数错误回传", not ok, res[:80])
ok, res = mgr.call("mcp_nonexist_tool", "{}")
check("未连接 server", not ok, res)
ok, res = mgr.call("not_mcp_tool", "{}")
check("非 MCP 名拒绝", not ok, res)

# ============ 3. llm_server 集成 ============
print("== 3. 与 llm_server 集成 ==")
L._mcp_manager = mgr

# 3.1 动态工具合并（非隔离）
L.ISOLATED = False
all_tools = L._agent_tools()
names_all = [t["function"]["name"] for t in all_tools]
check("MCP 工具并入工具集", "mcp_echo_echo" in names_all and len(names_all) > 28, str(len(names_all)))

# 3.2 隔离模式保留 MCP 工具（外部工具与屏幕无关，GitHub 等 API 类在隔离环境可用）
L.ISOLATED = True
iso_names = [t["function"]["name"] for t in L._agent_tools()]
L.ISOLATED = False
check("隔离模式保留 MCP 工具", "mcp_echo_echo" in iso_names, "")

# 3.3 _execute_tool 转发
ok, res = L._execute_tool("mcp_echo_add", json.dumps({"a": 10, "b": 32}))
check("_execute_tool 转发 MCP", ok and "42" in res, res)

# 3.4 确认策略：MCP 工具一律 ask（auto 模式）
policy = L._confirm_policy("mcp_echo_add", {"a": 1, "b": 2})
check("MCP 工具默认确认", policy == "ask", policy)
policy = L._confirm_policy("mcp_echo_echo", {})
check("只读 MCP 也确认（保守）", policy == "ask", policy)
L.CONFIRM_MODES and None
# trusted 模式放行
_orig_mode = L._current_confirm_mode
L._current_confirm_mode = lambda: "trusted"
policy = L._confirm_policy("mcp_echo_add", {})
L._current_confirm_mode = _orig_mode
check("trusted 模式放行", policy == "allow", policy)

mgr.stop()
print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
