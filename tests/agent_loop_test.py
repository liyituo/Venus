"""agent 循环端到端测试：打桩上游响应，验证 ask(diff) / todo_update / system 注入。

SSE 流在后台线程消费；确认请求通过直接操作 _confirm_table 自动应答（TestClient
单事件循环，流式期间不能并发 POST respond）。
"""
import os
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import llm_server as L  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# CI/无配置环境：注入假配置（测试不走真实上游，仅过 _validate_config）
L.load_config = lambda: {"api_url": "http://127.0.0.1:9", "api_key": "test",
                         "model": "test-model", "context_window": 65536,
                         "confirm_mode": "auto"}

_TMP = tempfile.mkdtemp(prefix="pcagent_loop_")
WS = Path(_TMP)
L._get_workspace = lambda: WS
L._todos = []
L._todos_loaded = True

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


def collect_events(client, body) -> list:
    """后台线程消费 SSE 流，返回 (kind, payload) 列表；自动应答确认请求。"""
    out = []
    done = threading.Event()

    def auto_respond():
        # 确认表一旦出现条目就自动应答 yes
        while not done.is_set():
            with L._confirm_lock:
                ids = list(L._confirm_table.keys())
            if ids:
                with L._confirm_lock:
                    L._confirm_table[ids[0]]["choice"] = "yes"
                    L._confirm_table[ids[0]]["event"].set()
                return
            time.sleep(0.02)

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
                            try:
                                out.append((ev, json.loads(payload)))
                            except Exception:
                                out.append((ev, payload))
                            ev = ""
                        elif payload == "[DONE]":
                            out.append(("done", None))
                        else:
                            try:
                                d = json.loads(payload)
                                content = (d.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
                                if content:
                                    out.append(("delta", content))
                            except Exception:
                                pass
        except Exception as exc:
            out.append(("error", str(exc)))
        finally:
            done.set()

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    auto_respond()
    t.join(timeout=30)
    return out


client = TestClient(L.app)

# ============ 1. replace_text → ask 事件带 diff → 自动确认 → tool_result ============
print("== 1. replace_text 确认链路（ask + diff）==")
(WS / "a.txt").write_text("hello world\n", encoding="utf-8")
seen_payloads = {}

def fake_upstream_1(api_url, payload, headers):
    seen_payloads["messages"] = payload.get("messages", [])
    if not seen_payloads.get("called"):
        seen_payloads["called"] = True
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "replace_text",
                          "arguments": json.dumps({"file": "a.txt", "old": "hello", "new": "hi"})}}]}}],
            "usage": {}}
    return {"choices": [{"message": {"role": "assistant", "content": "已改好"}}], "usage": {}}

L._call_upstream_raw = fake_upstream_1
L._todos = []
L._todos_loaded = True
L._confirm_table.clear()

events = collect_events(client, {"agent": True, "messages": [
    {"role": "system", "content": "你是测试助手"}, {"role": "user", "content": "改一下 a.txt"}]})

kinds = [e[0] for e in events]
check("tool_call 事件", "tool_call" in kinds, str(kinds))
check("ask 事件", "ask" in kinds, str(kinds))

ask = next((p for k, p in events if k == "ask"), None)
check("ask 带 diff 字段", ask is not None and ask.get("diff") is not None and "+hi" in ask.get("diff", ""),
      str(ask)[:200])
check("ask 带 question", ask is not None and "a.txt" in ask.get("question", ""), str(ask)[:120])

tr = next((p for k, p in events if k == "tool_result"), None)
check("tool_result ok", tr is not None and tr.get("ok") is True, str(tr)[:150])
check("文件已修改", (WS / "a.txt").read_text(encoding="utf-8") == "hi world\n", "")
check("最终回复", any(k == "delta" and "已改好" in p for k, p in events), str(kinds))
check("流正常结束（无 error）", not any(k == "error" for k, p in events), str(kinds))

# ============ 2. create_todo → todo_update 事件 + system 注入 ============
print("== 2. todo 事件与 system 注入 ==")
L._todos = []
L._todos_loaded = True

def fake_upstream_2(api_url, payload, headers):
    n = seen_payloads.get("n2", 0) + 1
    seen_payloads["n2"] = n
    seen_payloads[f"msgs{n}"] = payload.get("messages", [])
    if n == 1:
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c2", "type": "function",
             "function": {"name": "create_todo",
                          "arguments": json.dumps({"title": "写单元测试"})}}]}}],
            "usage": {}}
    return {"choices": [{"message": {"role": "assistant", "content": "已建任务"}}], "usage": {}}

L._call_upstream_raw = fake_upstream_2
L._confirm_table.clear()
events = collect_events(client, {"agent": True, "messages": [
    {"role": "user", "content": "列个任务"}]})

kinds = [e[0] for e in events]
check("todo_update 事件", "todo_update" in kinds, str(kinds))
tu = next((p for k, p in events if k == "todo_update"), None)
check("todo_update 内容", tu is not None and any(t.get("title") == "写单元测试" for t in tu.get("todos", [])),
      str(tu)[:150])
check("todo 持久化", (WS / ".pcagent" / "todos.json").exists(), "")

# 下一次全新请求：system 应注入任务清单（半恢复）
events2 = collect_events(client, {"agent": True, "messages": [
    {"role": "user", "content": "继续"}]})
sys_msgs = seen_payloads.get("msgs3") or []
sys_content = " ".join(m.get("content", "") for m in sys_msgs if m.get("role") == "system")
check("system 注入任务清单", "当前任务清单" in sys_content and "写单元测试" in sys_content,
      sys_content[-200:])

# ============ 3. 上下文硬上界（防 tokens 激增）============
print("== 3. _trim_messages 双重上限 ==")
# 3.1 条数上限：30 条 → 裁到 20 条内
msgs = [{"role": "system", "content": "s"}] + \
       [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(30)]
trimmed = L._trim_messages(msgs)
check("条数裁剪到 20 条内", len(trimmed) <= 20, str(len(trimmed)))
check("保留省略提示", any("省略" in (m.get("content") or "") for m in trimmed), "")
check("保留最近消息", any("m29" in (m.get("content") or "") for m in trimmed), "")

# 3.2 字符硬上限：巨长消息超 12 万字符 → 丢最早
big = "x" * 150_000
msgs2 = [{"role": "system", "content": "s"},
         {"role": "user", "content": big},
         {"role": "assistant", "content": "recent-ok"}]
trimmed2 = L._trim_messages(msgs2)
total = sum(len(m.get("content") or "") for m in trimmed2)
check("字符硬上限生效", total <= L.MAX_HISTORY_CHARS, f"total={total}")
check("保留最近消息", any("recent-ok" in (m.get("content") or "") for m in trimmed2), "")
check("丢弃最早的巨长消息", not any(m.get("content") == big for m in trimmed2), "")

# 3.3 小上下文不受影响
small = [{"role": "user", "content": "hi"}]
check("小上下文原样", L._trim_messages(small) == small, "")

# ============ 汇总 ============
print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
