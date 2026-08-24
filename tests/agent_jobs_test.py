"""异步任务台 API 与存储测试。"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
os.environ.setdefault("PCAGENT_ALLOW_TEST_HOST", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_TMP = tempfile.mkdtemp(prefix="venus_jobs_")
os.environ["VENUS_DATA_DIR"] = _TMP

import agent_jobs as J  # noqa: E402
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


def _noop_handler(job_id: str) -> None:
    J.update_job(job_id, status="completed", finished_at=time.time(),
                 result_summary="测试完成")


print("== 1. 存储 CRUD ==")
job = J.create_job(messages=[{"role": "user", "content": "跑一下单元测试并总结"}],
                   session_id=1, workspace="/tmp/ws")
check("创建任务", job.get("status") == "queued" and job.get("id", "").startswith("job_"), job)
check("标题自动截取", "单元测试" in job.get("title", ""), job.get("title"))

loaded = J.get_job(job["id"])
check("读取任务", loaded and loaded["id"] == job["id"], str(loaded))

J.update_job(job["id"], status="running", started_at=time.time())
check("更新状态", J.get_job(job["id"])["status"] == "running", "")

rows = J.list_jobs()
check("列表含任务", any(r["id"] == job["id"] for r in rows), str(rows))

ok, msg = J.cancel_job(job["id"])
check("取消运行中任务（无 cancel event）", not ok or "已发送" in msg, msg)

queued = J.create_job(messages=[{"role": "user", "content": "排队任务"}])
ok2, _ = J.cancel_job(queued["id"])
check("取消排队任务", ok2 and J.get_job(queued["id"])["status"] == "cancelled", "")

print("== 2. Worker + handler ==")
J.set_job_handler(_noop_handler)
J.start_job_worker()
done_id = J.create_job(messages=[{"role": "user", "content": "后台执行"}])["id"]
J.enqueue_job(done_id)
time.sleep(1.5)
final = J.get_job(done_id)
check("worker 执行完成", final and final.get("status") == "completed", str(final))

print("== 3. HTTP API ==")
L._agent_jobs.set_job_handler(_noop_handler)
client = TestClient(L.app)

r = client.post("/api/v1/jobs", json={
    "messages": [{"role": "user", "content": "整理下载文件夹"}],
    "session_id": 2,
})
check("POST /jobs", r.status_code == 200 and r.json().get("job", {}).get("id"), r.text)
api_job_id = r.json()["job"]["id"]

time.sleep(1.5)
r = client.get(f"/api/v1/jobs/{api_job_id}")
check("GET /jobs/{id}", r.status_code == 200, r.text)

r = client.get("/api/v1/jobs")
check("GET /jobs 列表", r.status_code == 200 and "jobs" in r.json(), r.text)

r = client.get("/api/v1/health")
check("health 含 jobs 统计", "jobs" in r.json(), str(r.json().get("jobs")))

r = client.post("/api/v1/jobs", json={"messages": []})
check("空 messages 422", r.status_code == 422, r.text)

print("== 4. 取消排队任务 ==")
queued2 = J.create_job(messages=[{"role": "user", "content": "待取消"}])["id"]
J.enqueue_job(queued2)
ok3, _ = J.cancel_job(queued2)
check("取消后不再执行", ok3 and J.get_job(queued2)["status"] == "cancelled", "")
time.sleep(1.0)
check("取消任务保持 cancelled", J.get_job(queued2)["status"] == "cancelled", "")

print(f"\n{'=' * 40}\n  {passed} passed, {failed} failed\n{'=' * 40}")
sys.exit(1 if failed else 0)
