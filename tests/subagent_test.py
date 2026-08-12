"""子 agent 模块测试：delegate 委派链路 / 事件透传 / 深度限制 / 工具白名单 / view_image。

AGENTS_DIR 重定向到临时目录；上游响应打桩（TestClient 单事件循环，
确认请求通过直接操作 _confirm_table 自动应答）。
"""
import os
os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
os.environ.setdefault("PCAGENT_DATA_DIR", str(__import__("tempfile").mkdtemp(prefix="pcagent_td_")))
os.environ.setdefault("PCAGENT_ALLOW_TEST_HOST", "1")
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import llm_server as L  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="pcagent_subagent_"))
# CI/无配置环境：注入假配置（测试不走真实上游，仅过 _validate_config）
L.load_config = lambda: {"api_url": "http://127.0.0.1:9", "api_key": "test",
                         "model": "test-model", "context_window": 65536,
                         "confirm_mode": "auto", "reasoning_mode": "max"}
L.AGENTS_DIR = _TMP / "agents"
L.AGENTS_DIR.mkdir()
# 造一个 vision 子 agent（与真实示例同构）
(L.AGENTS_DIR / "vision.json").write_text(json.dumps({
    "name": "vision",
    "description": "视觉分析专家",
    "system_prompt": "你是视觉分析专家。用 view_image 查看图片。",
    "tools": ["view_image", "read_file"],
}, ensure_ascii=False), encoding="utf-8")
L._get_workspace = lambda: _TMP
L._todos = []
L._todos_loaded = True

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}  {detail}")


def collect_events(client, body):
    """消费 SSE 流（后台线程），返回 [(event_type, payload)]。"""
    out = []
    done = threading.Event()

    def consume():
        try:
            with client.stream("POST", "/api/v1/chat/stream", json=body) as r:
                if r.status_code != 200:
                    # 422 等校验错误：打印响应体 detail（定位 CI 失败的关键）
                    out.append(("HTTP_ERROR",
                                f"{r.status_code}: {r.read().decode('utf-8', 'replace')[:800]}"))
                    return
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
                            try:
                                out.append((ev, json.loads(payload)))
                            except Exception:
                                out.append((ev, payload))
                            ev = ""
                        elif payload == "[DONE]":
                            out.append(("done", None))
        except Exception as exc:
            out.append(("error", str(exc)))
        finally:
            done.set()

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    t.join(timeout=90)
    if t.is_alive():
        out.append(("TIMEOUT", f"collect_events 90s 超时（已收集 {len(out)} 条事件）"))
    return out


def auto_respond():
    """后台线程：自动应答所有确认请求（yes）；30 秒无新请求后自动退出。"""
    def loop():
        idle = 0
        while idle < 1500:   # 30 秒无确认请求则退出，避免 CI 上线程残留
            with L._confirm_lock:
                ids = [k for k, v in L._confirm_table.items() if v["choice"] is None]
            if not ids:
                time.sleep(0.02)
                idle += 1
                continue
            idle = 0
            with L._confirm_lock:
                for rid in ids:
                    entry = L._confirm_table.get(rid)
                    if entry is not None:
                        entry["choice"] = "yes"
                        entry["event"].set()
    threading.Thread(target=loop, daemon=True).start()


client = TestClient(L.app)

# ============ 1. 扫描与清单 ============
print("== 1. agents 扫描与清单 ==")
agents = L._scan_agents()
check("扫描到 vision", len(agents) == 1 and agents[0]["name"] == "vision", str(agents))
cat = L._agent_catalog_text()
check("清单含 vision 与描述", "vision" in cat and "视觉分析" in cat, cat[:120])
all_names = [t["function"]["name"] for t in L.AGENT_TOOLS]
check("delegate/view_image 已注册", "delegate" in all_names and "view_image" in all_names, "")

# ============ 2. delegate 全链路（打桩上游） ============
print("== 2. delegate 委派链路 ==")
seen = {"n": 0, "sub_tools": None}

def fake_upstream(api_url, payload, headers):
    seen["n"] += 1
    n = seen["n"]
    if n == 1:
        # 主循环：委派给 vision
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "d1", "type": "function",
             "function": {"name": "delegate", "arguments": json.dumps(
                 {"agent": "vision", "task": "分析 test.png 的内容"})}}]}}], "usage": {}}
    if n == 2:
        # 子循环第一轮：调用 view_image（白名单内）
        seen["sub_tools"] = [t["function"]["name"] for t in payload.get("tools", [])]
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "d2", "type": "function",
             "function": {"name": "view_image", "arguments": json.dumps(
                 {"path": "test.png"})}}]}}], "usage": {}}
    # 子循环第二轮：最终回复
    return {"choices": [{"message": {"role": "assistant",
                                     "content": "图片显示：蓝色按钮，标题『开始』"}}], "usage": {}}

L._call_upstream_raw = fake_upstream
auto_respond()
events = collect_events(client, {"agent": True, "messages": [
    {"role": "user", "content": "分析图片"}]})

kinds = [e[0] for e in events]
check("delegate 工具被调用", any(k == "tool_call" and p.get("name") == "delegate"
                                for k, p in events if isinstance(p, dict)), str(kinds))
check("子循环事件透传（子 view_image 调用出现在流中）",
      any(k == "tool_call" and p.get("name") == "view_image"
          for k, p in events if isinstance(p, dict)), str(kinds))
tr = next((p for k, p in events if k == "tool_result" and "子 agent vision" in p.get("result", "")),
          None)
check("delegate 返回子 agent 摘要", tr is not None, str(events)[:300])
check("摘要含子 agent 最终回复", tr is not None and "蓝色按钮" in tr["result"], str(tr)[:200])
# 子循环工具白名单：只有 vision 声明 + 只读兜底
allowed = {"view_image", "read_file"} | L.QUERY_TOOLS
check("子循环工具白名单生效",
      seen["sub_tools"] is not None and all(t in allowed for t in seen["sub_tools"]),
      str(seen["sub_tools"])[:150])
check("白名单不含危险写工具", "run_shell" not in (seen["sub_tools"] or []), "")

# ============ 3. 深度限制 ============
print("== 3. 深度限制（子 agent 不能再委派）==")
seen2 = {"n": 0}

def fake_deep_upstream(api_url, payload, headers):
    seen2["n"] += 1
    n = seen2["n"]
    if n == 1:
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "e1", "type": "function",
             "function": {"name": "delegate", "arguments": json.dumps(
                 {"agent": "vision", "task": "分析"})}}]}}], "usage": {}}
    if n == 2:
        # 子循环尝试再次委派 → 应被拒绝
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "e2", "type": "function",
             "function": {"name": "delegate", "arguments": json.dumps(
                 {"agent": "vision", "task": "再委派"})}}]}}], "usage": {}}
    return {"choices": [{"message": {"role": "assistant", "content": "子层结束"}}], "usage": {}}

L._call_upstream_raw = fake_deep_upstream
L._confirm_table.clear()
events = collect_events(client, {"agent": True, "messages": [
    {"role": "user", "content": "委派"}]})
tr = next((p for k, p in events if k == "tool_result"
           and "深度已达上限" in p.get("result", "")), None)
check("子 agent 再委派被拒绝", tr is not None, str(events)[:250])

# ============ 4. view_image ============
print("== 4. view_image ==")
# 4.1 未配置视觉模型 → 明确报错
img = _TMP / "test.png"
img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
ok, res = L._execute_tool("view_image", json.dumps({"path": "test.png"}))
check("未配置视觉模型报错明确", not ok and "vision" in res, res)
# 4.2 配置后 + 打桩视觉上游 → 成功返回描述
ok, res = L._execute_tool("view_image", json.dumps({"path": "不存在的.png"}))
check("图片不存在报错", not ok and "不存在" in res, res)
_orig_cfg = L.load_config
L.load_config = lambda: {**_orig_cfg(), "vision_api_url": "https://vision.example.com/v1",
                         "vision_api_key": "sk-v", "vision_model": "qwen-vl-test"}

def fake_vision(api_url, payload, headers):
    assert "image_url" in str(payload["messages"][0]["content"])
    return {"choices": [{"message": {"role": "assistant",
                                     "content": "图片显示一个蓝色按钮"}}], "usage": {}}

L._call_upstream_raw = fake_vision
ok, res = L._execute_tool("view_image", json.dumps({"path": "test.png"}))
check("视觉调用成功返回描述", ok and "蓝色按钮" in res, res)
L.load_config = _orig_cfg

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
