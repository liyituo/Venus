"""MCP 客户端集成测试：连接测试 server、拉工具列表、转发调用。

另验证与 llm_server 的集成（动态工具合并 + _execute_tool 转发 + 确认策略）。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import llm_server as L  # noqa: E402
from mcp_client import McpManager  # noqa: E402

_PY = sys.executable
_ECHO = str(Path(__file__).resolve().parent / "mcp_echo_server.py")
# my_echo：server 名含下划线，验证工具名反查不受分隔符歧义影响
CONFIG = {"echo": {"command": _PY, "args": [_ECHO]},
          "my_echo": {"command": _PY, "args": [_ECHO]}}

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


def wait_conn(mgr, name, timeout=20):
    """轮询等待 server 连接就绪（start 短超时后连接在后台继续）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        c = mgr.conns.get(name)
        if c is not None and c.session is not None:
            return True
        time.sleep(0.1)
    return False


# ============ 1. 连接与工具列表 ============
print("== 1. 连接 / 工具列表 ==")
mgr = McpManager(CONFIG)
mgr.start()
check("echo server 连接", wait_conn(mgr, "echo") and mgr.conns["echo"].session is not None,
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
check("未知 server 拒绝", not ok, res)
ok, res = mgr.call("not_mcp_tool", "{}")
check("非 MCP 名拒绝", not ok, res)
# server 名含下划线：前缀反查（旧 split 实现会误拆为 server="my"）
ok, res = mgr.call("mcp_my_echo_echo", json.dumps({"text": "嵌套下划线"}))
check("server 名含下划线可调用", ok and "嵌套下划线" in res, res)

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
# trusted 模式放行
_orig_mode = L._current_confirm_mode
L._current_confirm_mode = lambda: "trusted"
policy = L._confirm_policy("mcp_echo_add", {})
L._current_confirm_mode = _orig_mode
check("trusted 模式放行", policy == "allow", policy)

# 3.5 只读 MCP 判定：无 server 名称前缀后门——未显式声明的工具一律按写处理
check("tavily 未声明不视为只读",
      not L._is_readonly_mcp("mcp_tavily_tavily-search") and
      not L._is_readonly_mcp("mcp_github_create_issue"), "")
policy = L._confirm_policy("mcp_tavily_tavily-search", {})
check("auto 模式未声明 MCP 需确认", policy == "ask", policy)
policy = L._confirm_policy("mcp_github_create_issue", {})
check("auto 模式其他 MCP 仍确认", policy == "ask", policy)
L._current_confirm_mode = lambda: "query"
policy = L._confirm_policy("mcp_tavily_tavily-search", {})
check("query 模式未声明 MCP 拒绝", policy == "deny", policy)
policy = L._confirm_policy("mcp_github_create_issue", {})
L._current_confirm_mode = _orig_mode
check("query 模式其他 MCP 拒绝", policy == "deny", policy)

# 3.6 工具只读只来自显式配置声明（无配置时 spotify 全按写处理）
check("spotify 未声明不视为只读",
      not L._is_readonly_mcp("mcp_spotify_search_tracks") and
      L._confirm_policy("mcp_spotify_search_tracks", {}) == "ask", "")
check("spotify 歌单查询未声明需确认",
      L._confirm_policy("mcp_spotify_get_my_playlists", {}) == "ask", "")
check("spotify 播放保持确认",
      not L._is_readonly_mcp("mcp_spotify_play_track") and
      L._confirm_policy("mcp_spotify_play_track", {}) == "ask", "")
check("spotify 建歌单保持确认",
      L._confirm_policy("mcp_spotify_create_playlist", {}) == "ask", "")

# 3.7 高德 MCP：无 server 前缀后门，未声明按写处理
check("amap 未声明不视为只读",
      not L._is_readonly_mcp("mcp_amap_poi_search") and
      L._confirm_policy("mcp_amap_direction", {}) == "ask", "")

# ============ 5. 高德 MCP server 连接 ============
print("== 5. 高德 MCP server ==")
_AMAP = str(Path(__file__).resolve().parent.parent / "scripts" / "mcp_servers" / "amap_server.py")
# 空 key：只测连接与工具注册；「未配置」分支不依赖网络/真实 key
mgr3 = McpManager({"amap": {"command": _PY, "args": [_AMAP],
                            "env": {"AMAP_MAPS_API_KEY": ""}}})
mgr3.start()
conn3 = mgr3.conns["amap"]
check("amap server 连接", wait_conn(mgr3, "amap"), conn3.error[:80])
names3 = [t["name"] for t in conn3.tools]
check("工具注册", "poi_search" in names3 and "direction" in names3
      and "regeocode" in names3, str(names3))
# 无 key 调用返回明确提示（不依赖网络/真实 key；server 正常返回提示文本）
ok3, res3 = mgr3.call("mcp_amap_poi_search", json.dumps({"keyword": "加油站"}))
check("无 key 调用有明确提示", ok3 and "AMAP_MAPS_API_KEY" in res3, res3[:80])
mgr3.stop()

# ============ 4. 断连重连（指数退避循环存活） ============
print("== 4. 断连重连 ==")
_BAD = str(Path(__file__).resolve().parent / "mcp_bad_server.py")
mgr2 = McpManager({"bad": {"command": _PY, "args": [_BAD]}})
mgr2.start()
conn = mgr2.conns["bad"]
time.sleep(2)
check("崩溃 server 记录错误", bool(conn.error), conn.error[:80])
check("session 为 None（未连上）", conn.session is None, "")
time.sleep(6)   # 跨过 RECONNECT_BASE(5s) 重试窗口
check("重连循环存活（协程未退出）", "bad" in mgr2._tasks and not mgr2._tasks["bad"].done(),
      str(mgr2._tasks.get("bad")))
task2 = mgr2._tasks.get("bad")
mgr2.stop()
check("stop 后退出重连", task2 is not None and task2.done(), str(task2))

mgr.stop()

# ============ 6. read_only_tools/write_tools 声明 ============
print("== 6. 只读声明 ==")
mgr4 = McpManager({"echo": {"command": _PY, "args": [_ECHO],
                            "read_only_tools": ["echo"],
                            "write_tools": ["add"]}})
mgr4.start()
wait_conn(mgr4, "echo")
check("声明只读生效", mgr4.is_readonly("mcp_echo_echo") is True, "")
check("声明写生效", mgr4.is_readonly("mcp_echo_add") is False, "")
check("未声明工具按写处理", mgr4.is_readonly("mcp_echo_unknown_tool") is False, "")
check("未匹配 server 返回 None", mgr4.is_readonly("mcp_nonexistent_foo") is None, "")
mgr4.stop()

# 未声明配置：全部按写处理（保守）
mgr5 = McpManager({"echo": {"command": _PY, "args": [_ECHO]}})
mgr5.start()
wait_conn(mgr5, "echo")
check("无声明时只读工具也按写", mgr5.is_readonly("mcp_echo_echo") is False, "")
mgr5.stop()

# ============ 7. 断线后工具清空（不暴露陈旧工具）============
print("== 7. 断线工具清空 ==")
mgr6 = McpManager({"echo": {"command": _PY, "args": [_ECHO]}})
mgr6.start()
conn6 = mgr6.conns["echo"]
wait_conn(mgr6, "echo")
check("连接后工具有效", len(conn6.tools) >= 2, str(len(conn6.tools)))
# 模拟连接断开：直接终止子进程（server 崩溃）
if conn6.session is not None:
    # 停止 server 进程：通过 stop_event 模拟断开 → _serve 退出 → tools 清空
    mgr6._loop.call_soon_threadsafe(conn6.stop_event.set)
    deadline = time.time() + 5
    while time.time() < deadline:
        if conn6.session is None:
            break
        time.sleep(0.1)
check("断开后 session 清空", conn6.session is None, "")
check("断开后工具清空（不暴露陈旧工具）", conn6.tools == [], str(conn6.tools))
mgr6.stop()

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
