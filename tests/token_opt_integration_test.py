"""R3 Token 优化集成测试：agent 循环去重 / 跨轮只读缓存 / fetch_result /
view_image 同图复用 / 结构化压缩校验 / usage 端点。

全部使用 mock 上游与临时目录，不访问真实 API、不污染真实 .pcagent。
"""
import os
import sys
import tempfile
import threading
import time
import json
import queue
from pathlib import Path

os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import llm_server as L  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="pcagent_tokopt_")
L.CONFIG_PATH = Path(_TMP) / "chat_config.json"
L.CONFIG_PATH.write_text(json.dumps({
    "api_url": "https://api.deepseek.com/v1/chat/completions",
    "api_key": "sk-test", "model": "deepseek-v4-flash",
    "workspace": str(Path(_TMP) / "ws"),
}), encoding="utf-8")
_WS = Path(_TMP) / "ws"
_WS.mkdir(exist_ok=True)
L._workspace_path = _WS.resolve()

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


# ============ 1. 同轮重复工具调用去重 ============
print("== 1. 同轮重复调用去重 ==")
_ws_file = _WS / "a.txt"
_ws_file.write_text("hello world\n", encoding="utf-8")
upstream_calls = {"n": 0, "tools": None}


def fake_dup_upstream(api_url, payload, headers):
    upstream_calls["n"] += 1
    if upstream_calls["n"] == 1:
        # 一轮返回两个完全相同的 read_file 调用
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})}},
            {"id": "t2", "type": "function",
             "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})}},
        ]}}], "usage": {"prompt_tokens": 100, "completion_tokens": 5}}
    return {"choices": [{"message": {"role": "assistant",
                                     "content": "完成"}}], "usage": {}}


L._call_upstream_raw = fake_dup_upstream
q = queue.Queue()
events = []
def collector():
    while True:
        try:
            events.append(q.get(timeout=5))
        except queue.Empty:
            return
t = threading.Thread(target=collector, daemon=True)
t.start()
reply = L._agent_loop(L.normalize_url("https://api.deepseek.com"), {}, [
    {"role": "system", "content": "test"},
    {"role": "user", "content": "读文件"}], "m", 0.7, q, threading.Event())
t.join(timeout=6)
results = [e for e in events if e[0] == "tool_result"]
check("两个调用都有结果", len(results) == 2, str(len(results)))
check("第二个结果标记复用", "已复用" in (results[1][1].get("result") or ""),
      str(results[1][1])[:200])
check("上游只调用 2 次（无重复执行）", upstream_calls["n"] == 2, str(upstream_calls["n"]))
check("回复正常返回", "完成" in reply, reply)

# ============ 2. 跨轮只读缓存（文件变化失效）============
print("== 2. 跨轮只读缓存 ==")
cache = {}
L._read_cache_put(cache, "read_file", {"path": "a.txt"}, True, "R1")
hit = L._read_cache_hit(cache, "read_file", {"path": "a.txt"})
check("文件未变 → 命中", hit is not None and hit[1] == "R1", str(hit))
_ws_file.write_text("changed content\n", encoding="utf-8")
hit2 = L._read_cache_hit(cache, "read_file", {"path": "a.txt"})
check("文件变化 → 缓存失效", hit2 is None, str(hit2))
L._read_cache_put(cache, "read_file", {"path": "a.txt"}, True, "R2")
_ws_file.write_text("changed again\n", encoding="utf-8")
# 动态工具不缓存
L._read_cache_put(cache, "get_screen_size", {}, True, "dyn")
check("无路径参数不缓存", L._read_cache_hit(cache, "get_screen_size", {}) is None)
# TTL 过期
L._read_cache_put(cache, "read_file", {"path": "a.txt"}, True, "R3")
entry = cache[L._read_cache_key("read_file", {"path": "a.txt"})]
cache[L._read_cache_key("read_file", {"path": "a.txt"})] = (time.monotonic() - 999, *entry[1:])
check("TTL 过期 → 失效", L._read_cache_hit(cache, "read_file", {"path": "a.txt"}) is None)

# ============ 3. fetch_result ============
print("== 3. fetch_result ==")
rid = L._result_store.put.__self__.put  # noqa: 占位（避免误用）
from tool_result_reducer import new_result_id
rid = new_result_id("run_shell", "FULL DATA " * 300)
L._result_store.put(rid, {"name": "run_shell"}, "FULL DATA " * 300)
ok, out = L._execute_tool("fetch_result", json.dumps({"result_id": rid, "section": "head"}))
check("fetch_result head 可取", ok and "FULL DATA" in out, out[:80])
ok2, out2 = L._execute_tool("fetch_result", json.dumps({"result_id": rid, "section": "bogus"}))
check("非法区段拒绝", not ok2, out2)
ok3, out3 = L._execute_tool("fetch_result", json.dumps({"result_id": "nope-123"}))
check("未知 id 明确报错", not ok3 and "找不到" in out3, out3)
check("fetch_result 在工具列表", any(t["function"]["name"] == "fetch_result" for t in L._agent_tools()))

# ============ 4. view_image 同图复用 ============
print("== 4. view_image 同图缓存 ==")
img = _WS / "shot.png"
img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 400)
vurl = "https://dashscope.aliyuncs.com/compatible-mode/v1"
L.load_config = lambda: {"api_url": "https://api.deepseek.com/v1/chat/completions",
                         "api_key": "sk", "model": "m",
                         "vision_api_url": vurl, "vision_api_key": "vk",
                         "vision_model": "qwen-vl"}
v_calls = {"n": 0}


def fake_vision(api_url, payload, headers):
    v_calls["n"] += 1
    return {"choices": [{"message": {"role": "assistant",
                                     "content": "画面显示蓝色按钮"}}], "usage": {}}


L._call_upstream_raw = fake_vision
L._vimage_cache.clear()
ok4, out4 = L._exec_view_image({"path": "shot.png", "question": "描述界面"})
check("首次调用成功", ok4 and "蓝色按钮" in out4, out4)
ok5, out5 = L._exec_view_image({"path": "shot.png", "question": "描述界面"})
check("同图同问复用缓存（不调上游）", ok5 and v_calls["n"] == 1 and "复用" in out5,
      f"calls={v_calls['n']} {out5[:60]}")
img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 400)   # 图片变化
ok6, out6 = L._exec_view_image({"path": "shot.png", "question": "描述界面"})
check("图片变化 → 重新分析", ok6 and v_calls["n"] == 2, f"calls={v_calls['n']}")
ok7, out7 = L._exec_view_image({"path": "shot.png", "question": "另一个问题"})
check("问题变化 → 重新分析", ok7 and v_calls["n"] == 3, f"calls={v_calls['n']}")

# ============ 5. 结构化压缩校验 ============
print("== 5. 结构化压缩一致性校验 ==")
keys = L._extract_evidence_keys([
    {"role": "user", "content": "修改 src/main.py 后运行测试报错：ValueError 端口 8080"},
    {"role": "assistant", "content": "已在 src/main.py:42 修复"}])
check("证据键提取路径/数字", any("main.py" in k for k in keys) and "8080" in keys, str(keys))
ok_v, why = L._validate_summary({"objective": "修复 bug",
                                 "open_tasks": ["跑测试"], "files_and_artifacts": ["src/main.py"]},
                                ["src/main.py", "8080"])
check("摘要覆盖关键路径 → 通过", ok_v, why)
ok_v2, why2 = L._validate_summary({"objective": "修复 bug"},
                                  ["src/main.py", "8080", "config.json"])
check("摘要遗漏路径 → 拒绝替换", not ok_v2 and "遗漏" in why2, why2)
ok_v3, why3 = L._validate_summary({}, [])
check("空摘要（无目标无任务）→ 拒绝", not ok_v3, why3)
# 结构化文本稳定
text1 = L._summary_to_text({"objective": "目标", "open_tasks": ["a", "b"]})
text2 = L._summary_to_text({"open_tasks": ["b", "a"], "objective": "目标"})
check("结构化文本与字段顺序无关", text1 == text2, repr(text1))

# ============ 6. usage 端点 ============
print("== 6. usage 端点 ==")
c = TestClient(L.app, base_url="http://127.0.0.1:8001")
r = c.get("/api/v1/usage")
check("usage 端点 200", r.status_code == 200, r.text[:200])
data = r.json()
check("聚合字段齐全", all(k in data for k in (
    "calls", "prompt_tokens", "cached_tokens", "completion_tokens",
    "reasoning_tokens", "cache_hit_rate", "total_tokens")), str(data.keys()))
check("最近明细存在", "recent" in data and isinstance(data["recent"], list))
check("prefix 缓存指标存在", "prefix_cache" in data)
r2 = c.get("/api/v1/health")
check("health 含 usage 摘要", "usage" in r2.json(), str(r2.json().keys())[:120])

# ============ 7. agent 循环历史窗口（防上下文线性膨胀）============
print("== 7. 历史窗口裁剪 ==")


def _make_history(rounds: int) -> list:
    msgs = [{"role": "system", "content": "安全规则"}]
    msgs.append({"role": "user", "content": "写一个 todo 项目"})
    for i in range(1, rounds + 1):
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"t{i}", "type": "function",
             "function": {"name": "create_file",
                          "arguments": json.dumps({"path": f"f{i}.py"})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"t{i}",
                     "content": f"文件已创建 f{i}.py"})
    return msgs


big = _make_history(12)
out = L._window_messages(big)
check("超限后消息数下降", len(out) < len(big), f"{len(big)}→{len(out)}")
roles = [m["role"] for m in out]
check("system 全部保留", roles.count("system") >= 2)
check("用户目标保留", "user" in roles)
check("早期摘要注入", any("早期执行记录" in (m.get("content") or "") for m in out))
check("摘要含工具名", any("create_file" in (m.get("content") or "") for m in out))
# assistant/tool 配对完整（OpenAI API 硬性要求）
pair_ok = True
for i, m in enumerate(out):
    if m.get("role") == "assistant" and m.get("tool_calls"):
        n = len(m["tool_calls"])
        for j in range(1, n + 1):
            if i + j >= len(out) or out[i + j].get("role") != "tool":
                pair_ok = False
check("assistant/tool 配对完整", pair_ok)
check("最近 8 轮工具结果保留", len([m for m in out if m["role"] == "tool"]) == 8)
small = _make_history(5)
check("轮数未超限原样返回", L._window_messages(small) is small)
# 当前轮（最后一轮 assistant+tool）绝不被压缩
last_round = [m for m in out if m.get("tool_call_id") == "t12"]
check("最新轮消息完整保留", len(last_round) == 1 and last_round[0]["tool_call_id"] == "t12"
      and "f12" in last_round[0]["content"])
# MAX_TOOL_RESULT_CHARS 已收紧到 800
check("工具结果上限收紧到 800", L.MAX_TOOL_RESULT_CHARS <= 800)

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
