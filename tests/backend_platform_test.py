"""后端平台 API：派发 / 记忆 / 定时任务。"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
os.environ.setdefault("PCAGENT_ALLOW_TEST_HOST", "1")

_TMP = tempfile.mkdtemp(prefix="venus_platform_")
os.environ["VENUS_DATA_DIR"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import agent_memory as M  # noqa: E402
import llm_server as L  # noqa: E402
import schedule_store as S  # noqa: E402
from dispatch_router import analyze_dispatch  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

M._memory_file = lambda name: Path(_TMP) / "memory" / name
Path(_TMP, "memory").mkdir(parents=True, exist_ok=True)

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

print("== 1. 派发路由 ==")
r = analyze_dispatch("跑一下 pytest 并写报告")
check("长任务 -> async", r["mode"] == "async", r)
r = analyze_dispatch("你好")
check("问候 -> sync", r["mode"] == "sync", r)

r = client.post("/api/v1/dispatch/analyze",
                json={"text": "整理下载文件夹", "history_turns": 0})
check("analyze API", r.status_code == 200 and r.json().get("mode") == "async", r.text)

r = client.post("/api/v1/dispatch", json={"text": "列出项目结构", "force_mode": "async"})
check("dispatch 创建 job", r.status_code == 200 and r.json().get("job", {}).get("id"), r.text)

r = client.post("/api/v1/dispatch", json={"text": "你好", "force_mode": "sync"})
check("dispatch sync 建议", r.status_code == 200 and r.json().get("mode") == "sync", r.text)

print("== 2. 记忆 API ==")
M.add_memories([{"id": "t1", "type": "preference", "content": "用户喜欢简洁回答",
                 "scope": "global", "confidence": 0.9, "status": "active",
                 "explicit": True, "pinned": False, "retrieval_keys": ["简洁"],
                 "source_refs": [], "supersedes": [],
                 "created_at": M._now(), "updated_at": M._now(),
                 "last_accessed_at": M._now(), "access_count": 0}])

r = client.get("/api/v1/memory")
check("memory list", r.status_code == 200 and len(r.json().get("memories") or []) >= 1, r.text)

r = client.get("/api/v1/memory/inject-preview?query=简洁")
check("inject preview", r.status_code == 200 and "recalled" in r.json(), r.text)

r = client.put("/api/v1/memory/t1", json={"content": "用户喜欢非常简洁的回答"})
check("memory correct", r.status_code == 200, r.text)

r = client.put("/api/v1/memory/profile",
               json={"preferences": [{"content": "主用 CLI", "confidence": 1.0}]})
check("profile update", r.status_code == 200, r.text)

print("== 3. 定时任务 ==")
r = client.post("/api/v1/schedules", json={"time": "08:00", "prompt": "每日简报"})
check("create schedule", r.status_code == 200, r.text)
sid = r.json().get("schedule", {}).get("id")

r = client.get("/api/v1/schedules")
check("list schedules", r.status_code == 200 and len(r.json().get("schedules") or []) >= 1, r.text)

r = client.patch(f"/api/v1/schedules/{sid}", json={"enabled": False})
check("patch schedule", r.status_code == 200, r.text)

r = client.delete(f"/api/v1/schedules/{sid}")
check("delete schedule", r.status_code == 200, r.text)

print("== 4. schedule_store 单元 ==")
try:
    S.add_schedule(time_hhmm="99:99", prompt="x")
    check("invalid time", False, "should raise")
except ValueError:
    check("invalid time rejected", True, "")

row = S.add_schedule(time_hhmm="09:30", prompt="测试定时")
check("store add", row.get("time") == "09:30", row)

print(f"\n{'=' * 40}\n  {passed} passed, {failed} failed\n{'=' * 40}")
sys.exit(1 if failed else 0)
