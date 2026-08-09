"""工具路由测试：规则前置 / 宽松解析 / 类别映射 / 缓存 / 降级 / 主循环集成。
import os
os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")

路由模型调用全部打桩，不依赖 Ollama，CI 可跑。
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import llm_server as L  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="pcagent_router_"))
L._get_workspace = lambda: _TMP
L._todos = []
L._todos_loaded = True
# CI/无配置环境：注入假配置
L.load_config = lambda: {"api_url": "http://127.0.0.1:9", "api_key": "test",
                         "model": "test-model", "context_window": 65536,
                         "confirm_mode": "auto", "tool_router": True,
                         "tool_router_url": "http://127.0.0.1:11434",
                         "tool_router_model": "gemma3:1b"}

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}  {detail}")


# ============ 1. 规则前置 ============
print("== 1. 规则路由 ==")
cases = [
    ("播放周杰伦的歌", "music"), ("把这首歌加入歌单", "music"),
    ("导航去机场", "map"), ("附近有什么咖啡厅", "map"),
    ("看看 github issue", "github"), ("帮我写个脚本", "code"),
    ("搜索一下 python 新特性", "search"), ("你好", None),
]
for query, expect in cases:
    got = L._route_rules(query)
    check(f"规则[{query[:10]}...] → {got}", got == expect, f"期望 {expect}")

# ============ 2. 宽松解析 ============
print("== 2. 宽松解析 ==")
check("完整 JSON", L._parse_route_output('{"category": "music", "reason": "x"}') == "music", "")
check("代码块包裹 JSON", L._parse_route_output('```json\n{"category": "map"}\n```') == "map", "")
check("裸词", L._parse_route_output("search") == "search", "")
check("带解释的裸词", L._parse_route_output("search - 用户需要搜索") == "search", "")
check("垃圾输出", L._parse_route_output("我不知道你在说什么") is None, "")
check("空输出", L._parse_route_output("") is None, "")
check("非法类别", L._parse_route_output('{"category": "hack"}') is None, "")
check("中文类别", L._parse_route_output('{"category": "音乐", "reason": "x"}') == "music", "")
check("中文裸词", L._parse_route_output("我认为是代码") == "code", "")
check("转义 JSON（嵌套输出形态）",
      L._parse_route_output('{"output": "{\\"type\\": \\"音乐\\", \\"category\\": \\"其他\\"}"}') == "general", "")

# ============ 3. 类别 → 工具集映射 ============
print("== 3. 类别映射 ==")
# 打桩 _agent_tools：模拟含 MCP 与本地写工具的完整工具集
_fake_tools = (
    [{"type": "function", "function": {"name": n, "description": "", "parameters": {}}}
     for n in L.ROUTER_CORE_TOOLS | {"replace_text", "run_shell", "git_commit", "create_file"}]
    + [{"type": "function", "function": {"name": f"mcp_spotify_{i}", "description": "", "parameters": {}}}
       for i in range(3)]
    + [{"type": "function", "function": {"name": f"mcp_tavily_{i}", "description": "", "parameters": {}}}
       for i in range(2)]
)
_orig_agent_tools = L._agent_tools
L._agent_tools = lambda: _fake_tools
try:
    music_tools = {t["function"]["name"] for t in L._tools_for_category("music")}
    check("核心工具恒在", L.ROUTER_CORE_TOOLS <= music_tools, str(len(music_tools)))
    check("music 含 spotify", any(n.startswith("mcp_spotify_") for n in music_tools), "")
    check("music 不含 tavily", not any(n.startswith("mcp_tavily_") for n in music_tools), "")
    code_tools = {t["function"]["name"] for t in L._tools_for_category("code")}
    check("code 含写工具", "replace_text" in code_tools and "run_shell" in code_tools, "")
    general_tools = {t["function"]["name"] for t in L._tools_for_category("general")}
    check("general = 核心集", general_tools == set(L.ROUTER_CORE_TOOLS), "")
finally:
    L._agent_tools = _orig_agent_tools

# ============ 4. 缓存与降级 ============
print("== 4. 缓存与降级 ==")
L._router_cache.clear()
# 恢复 fake 工具集（含 tavily），验证 search 类别的 MCP 展开
L._agent_tools = lambda: _fake_tools
calls = {"n": 0}
_orig_call = L._call_router
L._call_router = lambda q, c: (calls.__setitem__("n", calls["n"] + 1) or
                               '{"category": "search", "reason": "t"}')
try:
    # 用规则不命中的 query，确保走模型路由
    r1 = L._route_tools([{"role": "user", "content": "量子纠缠和量子计算的本质区别"}])
    r2 = L._route_tools([{"role": "user", "content": "量子纠缠和量子计算的本质区别"}])
    check("模型路由命中（search 含 tavily）",
          r1 is not None and any(t["function"]["name"].startswith("mcp_tavily_")
                                 for t in r1), "")
    check("同类请求缓存命中（模型只调一次）", calls["n"] == 1, f"调用 {calls['n']} 次")
finally:
    L._call_router = _orig_call
    L._agent_tools = _orig_agent_tools

# 降级：模型调用异常 → 全量（None）
L._router_cache.clear()
L._call_router = lambda q, c: (_ for _ in ()).throw(RuntimeError("Ollama down"))
try:
    r = L._route_tools([{"role": "user", "content": "一个模糊的请求"}])
    check("模型失败降级全量", r is None, str(r))
finally:
    L._call_router = _orig_call
    L._router_cache.clear()

# 开关关闭 → 全量
_orig_cfg = L.load_config
L.load_config = lambda: {**_orig_cfg(), "tool_router": False}
try:
    r = L._route_tools([{"role": "user", "content": "播放音乐"}])
    check("开关关闭走全量", r is None, "")
finally:
    L.load_config = _orig_cfg

# ============ 5. 主循环集成（真实链路：规则路由 music） ============
print("== 5. 主循环集成 ==")
seen = {"tools": None}


def fake_upstream(api_url, payload, headers):
    seen["tools"] = [t["function"]["name"] for t in payload.get("tools", [])]
    return {"choices": [{"message": {"role": "assistant", "content": "好的，已了解"}}],
            "usage": {}}


L._call_upstream_raw = fake_upstream
client = TestClient(L.app)


def collect_events(body):
    out = []
    done = threading.Event()

    def consume():
        try:
            with client.stream("POST", "/api/v1/chat/stream", json=body) as r:
                ev = ""
                for line in r.iter_lines():
                    if not line:
                        continue
                    if line.startswith("event:"):
                        ev = line[6:].strip()
                        continue
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if ev:
                            out.append((ev, payload))
                            ev = ""
        except Exception as exc:
            out.append(("error", str(exc)))
        finally:
            done.set()

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    t.join(timeout=30)
    return out


collect_events({"agent": True, "messages": [{"role": "user", "content": "播放周杰伦的歌"}]})
tools = seen["tools"] or []
check("主循环 payload 工具被路由过滤", len(tools) < len(_fake_tools) + 10, f"{len(tools)} 个")
check("核心集在主循环可见", "read_file" in tools and "system_status" in tools, "")
check("music 类别无多余 MCP", not any(n.startswith("mcp_tavily_") for n in tools), "")

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
