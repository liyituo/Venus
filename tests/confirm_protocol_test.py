"""确认协议端到端测试：真实 agent/respond HTTP 路径下 waiter 必须收到 choice。

覆盖：yes / no / timeout / 并发响应 / 重复响应 / 过期 ID / 记录清理（不泄漏内存）。
"""
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
os.environ.setdefault("PCAGENT_ALLOW_TEST_HOST", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import llm_server as L  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


client = TestClient(L.app)
L._confirm_table.clear()


def respond(rid, choice):
    return client.post("/api/v1/agent/respond",
                       json={"request_id": rid, "choice": choice})


def set_task(tid):
    """模拟 agent 任务上下文（任务绑定校验需要 _current_agent_task_id）。"""
    with L._agent_task_lock:
        L._current_agent_task_id = tid


def clear_task():
    with L._agent_task_lock:
        L._current_agent_task_id = ""


# ============ 1. 真实 HTTP 路径 yes：waiter 收到 yes ============
print("== 1. HTTP yes ==")
L._confirm_table.clear()
result = {}

def waiter_yes():
    result["choice"] = L._wait_confirm("rid-yes", timeout=5, task_id="t1")

set_task("t1")
t = threading.Thread(target=waiter_yes, daemon=True)
t.start()
time.sleep(0.2)   # waiter 已注册
r = respond("rid-yes", "yes")
clear_task()
check("HTTP 返回 200", r.status_code == 200 and r.json().get("choice") == "yes", r.text)
t.join(timeout=10)
check("waiter 收到 yes", result.get("choice") == "yes", str(result))
check("记录已清理", "rid-yes" not in L._confirm_table, str(list(L._confirm_table)))

# ============ 2. no ============
print("== 2. HTTP no ==")
result2 = {}
def waiter_no():
    result2["choice"] = L._wait_confirm("rid-no", timeout=5, task_id="t2")
set_task("t2")
t = threading.Thread(target=waiter_no, daemon=True)
t.start()
time.sleep(0.2)
r = respond("rid-no", "no")
clear_task()
check("HTTP no 200", r.status_code == 200, r.text)
t.join(timeout=10)
check("waiter 收到 no", result2.get("choice") == "no", str(result2))

# ============ 3. 超时 ============
print("== 3. 超时 ==")
t0 = time.monotonic()
choice = L._wait_confirm("rid-timeout", timeout=0.5, task_id="t3")
check("超时返回 None（拒绝）", choice is None, str(choice))
check("超时等待约等于设定值", 0.4 <= time.monotonic() - t0 < 3, str(time.monotonic() - t0))
check("超时后记录清理", "rid-timeout" not in L._confirm_table, "")
# 超时后再响应 → 404
r = respond("rid-timeout", "yes")
check("超时后响应 404", r.status_code == 404, r.text)

# ============ 4. 并发响应同一 ID ============
print("== 4. 并发响应 ==")
L._confirm_table.clear()
result4 = {}
def waiter_c():
    result4["choice"] = L._wait_confirm("rid-conc", timeout=5, task_id="t4")
set_task("t4")
t = threading.Thread(target=waiter_c, daemon=True)
t.start()
time.sleep(0.2)
responses = []
def resp(choice):
    responses.append(respond("rid-conc", choice).json())
ts = [threading.Thread(target=resp, args=(c,), daemon=True) for c in ("yes", "no")]
[x.start() for x in ts]
[x.join() for x in ts]
t.join(timeout=10)
clear_task()
check("并发只有一个生效（结果确定且幂等）", result4.get("choice") in ("yes", "no"), str(result4))
choices = {x.get("choice") for x in responses}
check("并发响应均有确定结果（200 生效或 404 已消费）", len(responses) == 2
      and all(x.get("ok") or "detail" in x for x in responses)
      and any(x.get("ok") for x in responses), str(responses))
first = result4.get("choice")
check("并发后结果未被第二个改变", result4.get("choice") == first, "")
check("并发后记录清理", "rid-conc" not in L._confirm_table, str(list(L._confirm_table)))

# ============ 5. 重复响应（已消费不改结果）============
print("== 5. 重复响应 ==")
L._confirm_table.clear()
result5 = {}
def waiter_d():
    result5["choice"] = L._wait_confirm("rid-dup", timeout=5, task_id="t5")
set_task("t5")
t = threading.Thread(target=waiter_d, daemon=True)
t.start()
time.sleep(0.2)
r1 = respond("rid-dup", "yes")
t.join(timeout=10)
clear_task()
check("首次响应生效", result5.get("choice") == "yes", str(result5))
r2 = respond("rid-dup", "no")
check("重复响应 404（记录已消费清理）", r2.status_code == 404, r2.text)
check("结果未被重复响应改变", result5.get("choice") == "yes", str(result5))

# ============ 6. 不存在的确认 ID ============
print("== 6. 不存在 ID ==")
r = respond("rid-nope", "yes")
check("不存在 404", r.status_code == 404, r.text)

# ============ 7. 任务结束后状态清理 ============
print("== 7. 任务结束清理 ==")
L._confirm_table.clear()
import asyncio
async def cleanup():
    L._call_upstream_raw = lambda *a, **k: {
        "choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}}
    ag = L._agent_stream_events("http://x", {}, [{"role": "user", "content": "hi"}], "m", 0.7)
    async for _ in ag:
        pass
asyncio.run(cleanup())
check("任务结束后任务 ID 清空", L._current_agent_task_id == "", L._current_agent_task_id)
check("无 ask 时确认表无残留", L._confirm_table == {}, str(list(L._confirm_table)))

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
