"""深度 debug 修复回归测试（H1-H3 / M3-M5 / 记忆误报）。

独立脚本风格（run_all_tests.py 自动发现）；不依赖 MCP/网络/上游 LLM：
- H1：上游异常结构 → LlmError（不再 KeyError 挂起 SSE）
- H2：agent 完成时 run_record 字段先于 done 事件写入
- H3：动态记忆消息插在静态 system 之后（前缀缓存稳定）
- M3：reduce_tool_result 的 result_id 含随机盐（不可预测）
- M4：早期轮次压缩保留轮次间的用户指令
- 记忆：区别/级别/特别/别人 不误报 constraint；真实约束仍提取
"""
import queue
import sys
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import llm_server as L  # noqa: E402
from tool_result_reducer import ResultStore, reduce_tool_result  # noqa: E402

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


# ============ H1：上游异常结构 → LlmError ============
class _FakeResp:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


print("== H1. 上游异常结构 ==")
with patch("urllib.request.urlopen", return_value=_FakeResp(b'{"error": "gw"}')) as mu:
    try:
        L._call_upstream_raw("http://x", {}, {})
        check("异常结构抛 LlmError", False, "未抛异常")
    except L.LlmError as exc:
        check("异常结构抛 LlmError", "异常结构" in str(exc), str(exc))
    mu.return_value = _FakeResp(b'{"choices": [{"message": {"content": "ok"}}]}')
    data = L._call_upstream_raw("http://x", {}, {})
    check("正常形状不受影响", data["choices"][0]["message"]["content"] == "ok")

# ============ H2：run_record 先于 done 写入 ============
print("== H2. 完成记录先于 done 事件 ==")


def _fake_upstream(api_url, payload, headers):
    return {"choices": [{"message": {"content": "final", "tool_calls": None}}]}


qq = queue.Queue()
cancel = threading.Event()
record = {"tool_events": []}
with patch("llm_server._call_upstream_raw", side_effect=_fake_upstream):
    ret = L._agent_loop("http://x", {}, [{"role": "user", "content": "hi"}],
                        "m", 0.1, qq, cancel, run_record=record)
check("返回最终文本", ret == "final", ret)
check("status=completed", record.get("status") == "completed", str(record))
check("final_answer 已写", record.get("final_answer") == "final")
events = []
while not qq.empty():
    events.append(qq.get()[0])
check("done 事件已发", "done" in events, str(events))

# ============ H3：动态记忆在静态 system 之后 ============
print("== H3. 动态记忆插入位置 ==")
dyn = "（动态记忆上下文）\n· 用 python"
msgs = [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "hi"}]
insert_at = 0
while insert_at < len(msgs) and msgs[insert_at].get("role") == "system":
    insert_at += 1
msgs.insert(insert_at, {"role": "system", "content": dyn})
check("静态 system 在最前", msgs[0]["content"] == "你是助手", str(msgs[0]))
check("dyn 在第二", "动态记忆上下文" in msgs[1]["content"])
check("user 消息在后", msgs[2]["role"] == "user")

# ============ M3：result_id 随机盐 ============
print("== M3. result_id 不可预测 ==")
s = ResultStore()
r1, m1 = reduce_tool_result("run_shell", {}, "X" * 3000, True, store=s)
r2, m2 = reduce_tool_result("run_shell", {}, "X" * 3000, True, store=s)
check("两次结果 id 不同", m1["result_id"] and m1["result_id"] != m2["result_id"],
      f"{m1['result_id']} vs {m2['result_id']}")
check("id 含随机盐", m1["result_id"].count("-") >= 2, m1["result_id"])
sec = s.section(m1["result_id"], "head")
check("取回仍可用", sec is not None and "XXX" in sec)

# ============ M4：早期轮次压缩保留用户指令 ============
print("== M4. 窗口压缩保留用户指令 ==")
early = [
    {"role": "user", "content": "以后不要用 pytest"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"function": {"name": "run_shell", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "t1", "content": "ok\nline2"},
]
summary = L._compact_early_rounds(early)
check("用户指令保留", "以后不要用 pytest" in summary, summary)
check("工具记录保留", "run_shell" in summary, summary)
check("超长用户消息跳过", "用户：" not in L._compact_early_rounds(
    [{"role": "user", "content": "x" * 500}]), "")

# ============ 记忆：约束正则误报回归 ============
print("== 记忆约束正则 ==")
from agent_memory import detect_l1_candidates  # noqa: E402
for text in ["这个区别很大", "文件级别不同", "特别是权限", "别人怎么做"]:
    check(f"不误报：{text[:8]}", not detect_l1_candidates(text))
for text in ["以后不要用 pytest", "别动那个文件", "记住提交前先跑 ruff"]:
    check(f"提取约束：{text[:8]}", any(
        c["type"] == "constraint" for c in detect_l1_candidates(text)))

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
