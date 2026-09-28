"""异步任务台 API 与存储测试。"""
import os
import sys
import tempfile
import threading
import time
from collections import deque
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
status_event_seq = J.get_job(job["id"])["event_seq"]
once_added = J.append_event_once(job["id"], "idempotent_event", {"value": 1})
seq_after_once = J.get_job(job["id"])["event_seq"]
once_duplicated = J.append_event_once(job["id"], "idempotent_event", {"value": 1})
check("status updates and append_event_once allocate stable sequence numbers",
      status_event_seq == 1 and once_added and seq_after_once == 2
      and not once_duplicated and J.get_job(job["id"])["event_seq"] == 2)

rows = J.list_jobs()
check("列表含任务", any(r["id"] == job["id"] for r in rows), str(rows))

ok, msg = J.cancel_job(job["id"])
check("取消运行中任务（无 cancel event）", not ok or "已发送" in msg, msg)

queued = J.create_job(messages=[{"role": "user", "content": "排队任务"}])
ok2, _ = J.cancel_job(queued["id"])
check("取消排队任务", ok2 and J.get_job(queued["id"])["status"] == "cancelled", "")

print("== 1b. 恢复与公平队列 ==")
restore_job = J.create_job(messages=[{"role": "user", "content": "重启恢复"}],
                           owner="restore-user", project_id="restore-project")
cancelled_restore = J.create_job(messages=[{"role": "user", "content": "不复活"}],
                                 owner="restore-user", project_id="restore-project")
worker_restore = J.create_job(messages=[{"role": "user", "content": "Worker 恢复排除"}],
                              owner="restore-user", project_id="restore-project",
                              worker_task=True)
interrupted = J.create_job(messages=[{"role": "user", "content": "中断任务"}],
                           owner="restore-user", project_id="restore-project")
with J._lock:
    J._queue = deque([cancelled_restore["id"]])
    J._queued_ids = {cancelled_restore["id"]}
ok_cancel_restart, _ = J.cancel_job(cancelled_restore["id"])
J.update_job(interrupted["id"], status="running", started_at=time.time())
with J._lock:
    J._queue = deque()
    J._queued_ids.clear()
    J._inflight_ids.clear()
restored_count = J._recover_persisted_jobs()
check("restart scan restores persisted Agent queued Jobs only",
      restored_count >= 1 and restore_job["id"] in J._queued_ids
      and worker_restore["id"] not in J._queued_ids
      and J.get_job(worker_restore["id"])["status"] == "queued")
check("queued cancellation stays terminal and never returns after recovery",
      ok_cancel_restart and J.get_job(cancelled_restore["id"])["status"] == "cancelled"
      and cancelled_restore["id"] not in J._queued_ids)
check("restart fails unsafe running work instead of silently rerunning",
      J.get_job(interrupted["id"])["status"] == "failed"
      and any(event.get("kind") == "interrupted"
              for event in J.get_job(interrupted["id"])["events"]))
J.update_job(restore_job["id"], status="completed", finished_at=time.time())
with J._lock:
    J._queue = deque()
    J._queued_ids.clear()

fair_a1 = J.create_job(messages=[{"role": "user", "content": "A1"}],
                       owner="user-a", project_id="shared-project")
fair_a2 = J.create_job(messages=[{"role": "user", "content": "A2"}],
                       owner="user-a", project_id="shared-project")
fair_b = J.create_job(messages=[{"role": "user", "content": "B"}],
                      owner="user-b", project_id="shared-project")
J._recover_persisted_jobs()
fair_order = []
for _ in range(3):
    with J._lock:
        picked = J._fair_choice_locked()
    fair_order.append(picked)
    J.update_job(picked, status="running", started_at=time.time())
    J.update_job(picked, status="completed", finished_at=time.time())
    with J._lock:
        J._inflight_ids.discard(picked)
check("two users in one project alternate fairly", fair_order == [
    fair_a1["id"], fair_b["id"], fair_a2["id"]], fair_order)

project_a1 = J.create_job(messages=[{"role": "user", "content": "PA1"}],
                          owner="same-owner", project_id="project-a")
project_a2 = J.create_job(messages=[{"role": "user", "content": "PA2"}],
                          owner="same-owner", project_id="project-a")
project_b1 = J.create_job(messages=[{"role": "user", "content": "PB1"}],
                          owner="same-owner", project_id="project-b")
project_b2 = J.create_job(messages=[{"role": "user", "content": "PB2"}],
                          owner="same-owner", project_id="project-b")
J._recover_persisted_jobs()
project_order = []
for _ in range(4):
    with J._lock:
        picked = J._fair_choice_locked()
    selected = J.get_job_internal(picked)
    project_order.append(selected["project_id"])
    J.update_job(picked, status="running", started_at=time.time())
    J.update_job(picked, status="completed", finished_at=time.time())
    with J._lock:
        J._inflight_ids.discard(picked)
check("projects receive round-robin dispatch", project_order == [
    "project-a", "project-b", "project-a", "project-b"], project_order)

idem1 = J.create_job(messages=[{"role": "user", "content": "idempotent"}],
                     owner="idem-user", project_id="idem-project",
                     request_id="same-request", request_fingerprint="body-one")
idem_repeat = J.create_job(messages=[{"role": "user", "content": "idempotent"}],
                           owner="idem-user", project_id="idem-project",
                           request_id="same-request", request_fingerprint="body-one")
try:
    J.create_job(messages=[{"role": "user", "content": "different"}],
                 owner="idem-user", project_id="idem-project",
                 request_id="same-request", request_fingerprint="body-two")
    idempotency_conflict = False
except J.JobIdempotencyConflict:
    idempotency_conflict = True
idem_other_user = J.create_job(
    messages=[{"role": "user", "content": "other owner"}],
    owner="another-user", project_id="idem-project", request_id="same-request")
check("request_id deduplicates in one user/project scope and rejects payload reuse",
      idem1["id"] == idem_repeat["id"] and idempotency_conflict
      and idem_other_user["id"] != idem1["id"])
J.update_job(idem1["id"], status="completed", finished_at=time.time())
J.update_job(idem_other_user["id"], status="completed", finished_at=time.time())

print("== 1b. 定时任务 occurrence 幂等 ==")
schedule_id = "daily-schedule-42"
schedule_day = "2026-09-28"
schedule_retry_id = L._schedule_occurrence_request_id(schedule_id, schedule_day)
schedule_same_day_id = L._schedule_occurrence_request_id(schedule_id, schedule_day)
schedule_next_day_id = L._schedule_occurrence_request_id(schedule_id, "2026-09-29")
schedule_job = J.create_job(
    messages=[{"role": "user", "content": "每天跑一次"}],
    owner="schedule-owner", request_id=schedule_retry_id,
    request_fingerprint="daily-prompt")
schedule_retry = J.create_job(
    messages=[{"role": "user", "content": "每天跑一次"}],
    owner="schedule-owner", request_id=schedule_same_day_id,
    request_fingerprint="daily-prompt")
schedule_tomorrow = J.create_job(
    messages=[{"role": "user", "content": "每天跑一次"}],
    owner="schedule-owner", request_id=schedule_next_day_id,
    request_fingerprint="daily-prompt")
check("同一日程同日重试复用 Job、次日创建新 Job",
      schedule_retry_id == schedule_same_day_id
      and schedule_next_day_id != schedule_same_day_id
      and schedule_job["id"] == schedule_retry["id"]
      and schedule_tomorrow["id"] != schedule_job["id"],
      f"{schedule_job['id']}/{schedule_retry['id']}/{schedule_tomorrow['id']}")
for scheduled in (schedule_job, schedule_tomorrow):
    J.update_job(scheduled["id"], status="completed", finished_at=time.time())

print("== 1c. 容量保护 ==")
storage_root = Path(tempfile.mkdtemp(prefix="venus_job_capacity_"))
old_jobs_root, old_max_jobs = J._jobs_root, J._MAX_JOBS
J._jobs_root = lambda: storage_root
J._MAX_JOBS = 3
protected_active = J.create_job(messages=[{"role": "user", "content": "active"}],
                                owner="capacity-user", project_id="capacity-project")
protected_worker = J.create_job(
    messages=[{"role": "user", "content": "worker"}], owner="capacity-user",
    project_id="capacity-project", worker_task=True)
prunable_terminal = J.create_job(
    messages=[{"role": "user", "content": "old terminal"}], owner="capacity-user",
    project_id="capacity-project")
J.update_job(prunable_terminal["id"], status="completed", finished_at=time.time())
new_active = J.create_job(messages=[{"role": "user", "content": "new active"}],
                          owner="capacity-user", project_id="capacity-project")
try:
    J.create_job(messages=[{"role": "user", "content": "over limit"}],
                 owner="capacity-user", project_id="capacity-project")
    capacity_rejected = False
except J.JobQueueLimitError:
    capacity_rejected = True
old_user_limit = J._MAX_ACTIVE_PER_USER
J._MAX_ACTIVE_PER_USER = 2
try:
    J.ensure_queue_capacity(owner="capacity-user", project_id="capacity-project")
    user_quota_rejected = False
except J.JobQueueLimitError:
    user_quota_rejected = True
J._MAX_ACTIVE_PER_USER = old_user_limit
check("capacity cleanup prunes terminal history but preserves active and Worker Jobs",
      not (storage_root / f"{prunable_terminal['id']}.json").exists()
      and (storage_root / f"{protected_active['id']}.json").exists()
      and (storage_root / f"{protected_worker['id']}.json").exists()
      and (storage_root / f"{new_active['id']}.json").exists()
      and capacity_rejected and user_quota_rejected)
J._jobs_root, J._MAX_JOBS = old_jobs_root, old_max_jobs

with J._lock:
    J._queue = deque()
    J._queued_ids.clear()
    J._inflight_ids.clear()

print("== 2. Worker + handler ==")
J.set_job_handler(_noop_handler)
J.start_job_worker()
done_id = J.create_job(messages=[{"role": "user", "content": "后台执行"}])["id"]
J.enqueue_job(done_id)
time.sleep(1.5)
final = J.get_job(done_id)
check("worker 执行完成", final and final.get("status") == "completed", str(final))

handler_started = threading.Event()
handler_release = threading.Event()
handler_calls = []

def _slow_handler(job_id: str) -> None:
    handler_calls.append(job_id)
    handler_started.set()
    handler_release.wait(5)
    J.update_job(job_id, status="completed", finished_at=time.time())

J.set_job_handler(_slow_handler)
deduped_enqueue = J.create_job(messages=[{"role": "user", "content": "只执行一次"}],
                               owner="dedupe-user", project_id="dedupe-project")
first_enqueue = J.enqueue_job(deduped_enqueue["id"])
started_once = handler_started.wait(2)
second_enqueue = J.enqueue_job(deduped_enqueue["id"])
handler_release.set()
deadline = time.time() + 3
while J.get_job(deduped_enqueue["id"])["status"] != "completed" and time.time() < deadline:
    time.sleep(0.02)
check("重复 enqueue 不会重复执行正在处理的 Job",
      first_enqueue and second_enqueue and started_once
      and handler_calls == [deduped_enqueue["id"]], handler_calls)

old_worker = J._worker
if old_worker is not None:
    old_worker._stop_event.set()
    old_worker.join(timeout=2)
try:
    J.start_job_worker()
    restarted_worker_ok = (old_worker is not None and not old_worker.is_alive()
                           and J._worker is not old_worker and J._worker.is_alive())
except Exception as exc:
    restarted_worker_ok = False
    worker_restart_error = repr(exc)
else:
    worker_restart_error = ""
check("worker 退出后 is_alive 检查可用并能重新启动",
      restarted_worker_ok, worker_restart_error)

print("== 3. HTTP API ==")
L._agent_jobs.set_job_handler(_noop_handler)
client = TestClient(L.app)

r = client.post("/api/v1/jobs", json={
    "messages": [{"role": "user", "content": "整理下载文件夹"}],
    "session_id": 2,
})
check("POST /jobs", r.status_code == 200 and r.json().get("job", {}).get("id"), r.text)
api_job_id = r.json()["job"]["id"]

api_idempotency_body = {
    "messages": [{"role": "user", "content": "相同请求不能重复创建"}],
    "request_id": "api-job-idempotency-0001",
}
api_idempotency_first = client.post("/api/v1/jobs", json=api_idempotency_body)
api_idempotency_repeat = client.post("/api/v1/jobs", json=api_idempotency_body)
api_idempotency_conflict = client.post("/api/v1/jobs", json={
    **api_idempotency_body,
    "messages": [{"role": "user", "content": "不同负载"}],
})
first_api_id = (api_idempotency_first.json().get("job") or {}).get("id")
repeat_api_id = (api_idempotency_repeat.json().get("job") or {}).get("id")
check("POST /jobs request_id 幂等且不同负载返回 409",
      api_idempotency_first.status_code == 200
      and api_idempotency_repeat.status_code == 200
      and first_api_id == repeat_api_id
      and api_idempotency_conflict.status_code == 409,
      f"{api_idempotency_first.status_code}/{api_idempotency_repeat.status_code}/"
      f"{api_idempotency_conflict.status_code}")

time.sleep(1.5)
r = client.get(f"/api/v1/jobs/{api_job_id}")
check("GET /jobs/{id}", r.status_code == 200, r.text)
event_response = client.get(f"/api/v1/jobs/{api_job_id}/events",
                            headers={"Last-Event-ID": "1"})
check("SSE resumes using monotonic Last-Event-ID after older events",
      "id: 2\n" in event_response.text and "id: 1\n" not in event_response.text,
      event_response.text[:500])

stream_job = J.create_job(messages=[{"role": "user", "content": "长事件流"}],
                          owner="owner")
J.update_job(stream_job["id"], status="running", started_at=time.time())
for index in range(100):
    J.append_event(stream_job["id"], "bulk", {"n": index})

def _append_after_stream_starts() -> None:
    time.sleep(1.2)
    for index in range(5):
        J.append_event(stream_job["id"], "live_late", {"n": index})
    J.update_job(stream_job["id"], status="completed", finished_at=time.time())

late_events_thread = threading.Thread(target=_append_after_stream_starts, daemon=True)
late_events_thread.start()
with client.stream("GET", f"/api/v1/jobs/{stream_job['id']}/events",
                   headers={"Last-Event-ID": "0"}) as stream_response:
    stream_text = stream_response.read().decode("utf-8", errors="replace")
late_events_thread.join(timeout=2)
check("long SSE stream continues after 100-event truncation",
      stream_response.status_code == 200 and "event: live_late" in stream_text
      and "event: resync" in stream_text and "id: 106\n" in stream_text
      and "event: done" in stream_text,
      stream_text[-500:])

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
