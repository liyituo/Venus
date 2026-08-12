"""后端取消与确认绑定测试：随机确认 ID / 过期与任务绑定校验 / 客户端断开后全局锁释放。"""
import os
import sys
import threading
import tempfile
import time
from pathlib import Path

os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
os.environ.setdefault("PCAGENT_DATA_DIR", str(Path(tempfile.mkdtemp(prefix="pcagent_td_"))))
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

# ============ 1. 确认 ID 随机不可预测 ============
print("== 1. 确认 ID 随机性 ==")
a = L._new_confirm_id("task-1")
b = L._new_confirm_id("task-1")
check("两次生成不同", a != b, f"{a} vs {b}")
check("ID 含任务标识", "task-1" in a, a)

# ============ 2. 过期确认拒绝 ============
print("== 2. 过期 / 任务不匹配确认拒绝 ==")
L._confirm_table.clear()
expired_id = "ask-expired-1"
with L._confirm_lock:
    L._confirm_table[expired_id] = {"event": threading.Event(), "choice": None,
                                    "task_id": "task-x", "source": "http",
                                    "expires": time.monotonic() - 5}   # 已过期
r = client.post("/api/v1/agent/respond", json={"request_id": expired_id, "choice": "yes"})
check("过期确认被拒绝 404", r.status_code == 404 and "过期" in r.json().get("detail", ""), r.text)
# 生命周期：waiter 是唯一清理者；responder 只写 choice + set event，不删除记录
# （删除会让 waiter 醒来拿不到 choice）。无 waiter 场景条目保留由 waiter 超时清理。

# 任务不匹配：task_id 不等于当前 agent 任务
L._confirm_table.clear()
stale_id = "ask-stale-1"
with L._confirm_lock:
    L._confirm_table[stale_id] = {"event": threading.Event(), "choice": None,
                                  "task_id": "old-task", "source": "http",
                                  "expires": time.monotonic() + 60}
with L._agent_task_lock:
    L._current_agent_task_id = "new-task"
r = client.post("/api/v1/agent/respond", json={"request_id": stale_id, "choice": "yes"})
check("任务不匹配确认被拒绝 404", r.status_code == 404 and "已结束" in r.json().get("detail", ""), r.text)

# 匹配任务 + 未过期 → 正常应答
ok_id = "ask-ok-1"
ev = threading.Event()
with L._confirm_lock:
    L._confirm_table[ok_id] = {"event": ev, "choice": None,
                               "task_id": "new-task", "source": "http",
                               "expires": time.monotonic() + 60}
r = client.post("/api/v1/agent/respond", json={"request_id": ok_id, "choice": "yes"})
check("合法确认应答成功", r.status_code == 200 and r.json().get("choice") == "yes", r.text)
check("应答后事件被触发", ev.is_set(), "")
# 有 waiter 时：responder 写 choice + set event → waiter 醒来读取并清理
ev2 = threading.Event()
with L._confirm_lock:
    L._confirm_table["ok-waiter"] = {"event": ev2, "choice": None,
                                     "task_id": "new-task", "source": "http",
                                     "expires": time.monotonic() + 60}

def consume_waiter():
    with L._confirm_lock:
        entry = L._confirm_table.pop("ok-waiter", None)
        if entry is not None and entry["choice"] is not None:
            entry["event"].set()
t2 = threading.Thread(target=consume_waiter, daemon=True)
t2.start()
t2.join(timeout=5)
check("waiter 消费后条目清除", "ok-waiter" not in L._confirm_table,
      str(list(L._confirm_table)))
with L._agent_task_lock:
    L._current_agent_task_id = ""

# 不存在的确认 404
r = client.post("/api/v1/agent/respond", json={"request_id": "ask-nope", "choice": "yes"})
check("不存在确认 404", r.status_code == 404, r.text)

# ============ 3. 全局锁：任务进行中拒绝新任务 ============
print("== 3. 全局 agent 锁 ==")
L._confirm_table.clear()
ok = L._agent_lock.acquire(blocking=False)
check("锁可获取", ok, "")

async def _first_event():
    ag = L._agent_stream_events("http://127.0.0.1:9", {}, [], "m", 0.7)
    try:
        return await ag.__anext__()
    finally:
        await ag.aclose()

import asyncio
ev = asyncio.run(_first_event())
check("锁占用时新任务被拒", "已有另一个 Agent" in ev, ev[:120])
L._agent_lock.release()

# ============ 4. 客户端断开后锁释放（asyncio 直连模拟断开）============
print("== 4. 客户端断开后全局锁释放 ==")
L._confirm_table.clear()
seen = {"n": 0}

def fake_upstream(api_url, payload, headers):
    n = seen["n"]
    seen["n"] = n + 1
    if n == 0:
        time.sleep(0.8)   # 模拟慢上游：客户端断开时 worker 还在跑
        return {"choices": [{"message": {"role": "assistant", "content": "第一个回复"}}], "usage": {}}
    return {"choices": [{"message": {"role": "assistant", "content": "第二个回复"}}], "usage": {}}

L._call_upstream_raw = fake_upstream

async def disconnect_and_check():
    """启动 agent 流 → 读第一条事件 → 断开（aclose）→ 返回断开前锁是否被持有。"""
    ag = L._agent_stream_events("http://127.0.0.1:9", {}, [{"role": "user", "content": "hi"}],
                                "m", 0.7)
    await ag.__anext__()             # 首事件（worker 仍在跑）
    busy_before = not L._agent_lock.acquire(blocking=False)
    if not busy_before:
        L._agent_lock.release()
    await ag.aclose()                # 模拟客户端断开：cancel + 等 worker 结束 → 释放锁
    return busy_before

busy_before = asyncio.run(disconnect_and_check())
check("断开前锁被持有（worker 未结束）", busy_before, "")

async def lock_free_after():
    return L._agent_lock.acquire(blocking=False)

free = asyncio.run(lock_free_after())
if free:
    L._agent_lock.release()
check("断开后 worker 结束，锁已释放", free, "")

# 锁释放后：第二个任务可以启动（独立回复）
L._confirm_table.clear()

async def second_task():
    ag = L._agent_stream_events("http://127.0.0.1:9", {}, [{"role": "user", "content": "hi2"}],
                                "m", 0.7)
    got = ""
    async for ev in ag:
        got += ev
    return got

got2 = asyncio.run(second_task())
check("断开后新任务可启动且获得独立回复", "第二个" in got2 and "复" in got2, got2[:200])
check("新任务调用独立计数", seen["n"] >= 2, str(seen["n"]))

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
