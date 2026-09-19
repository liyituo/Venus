"""Offline regressions for persistent jobs and bounded SSE history."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ["VENUS_DATA_DIR"] = tempfile.mkdtemp(prefix="venus_recovery_")
os.environ["PCAGENT_DISABLE_MCP"] = "1"
os.environ["PCAGENT_ALLOW_TEST_HOST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import agent_jobs as J  # noqa: E402
import llm_server as L  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class JobRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="venus_jobs_case_"))
        self.root_patch = patch.object(J, "_jobs_root", return_value=self.root)
        self.root_patch.start()
        J._queue = None
        J._worker = None

    def tearDown(self):
        if J._worker:
            J._worker._stop_event.set()
            J._worker.join(3)
            self.assertFalse(J._worker.is_alive())
        J._queue = None
        J._worker = None
        J._cancel_events.clear()
        self.root_patch.stop()

    def create(self):
        return J.create_job(messages=[{"role": "user", "content": "offline probe"}])["id"]

    def test_boot_recovers_queue_and_interrupts_inflight_work(self):
        queued = self.create()
        interrupted = [self.create(), self.create()]
        for jid, state in zip(interrupted, ("running", "waiting_confirm")):
            J.update_job(jid, status=state, pending_ask={"id": "stale"}, confirm_request_id="stale")
        completed = self.create()
        J.update_job(completed, status="completed")
        # Simulate a crash between job and index writes.
        J._index_file().write_text('{"jobs": []}', encoding="utf-8")
        handled = []
        finished = threading.Event()

        def handle(jid):
            handled.append(jid)
            J.update_job(jid, status="completed")
            finished.set()

        J.set_job_handler(handle)
        J.start_job_worker()
        self.assertTrue(finished.wait(3))
        J.start_job_worker()
        self.assertEqual(handled, [queued])
        for jid in interrupted:
            job = J.get_job(jid)
            self.assertEqual(job["status"], "failed")
            self.assertTrue(job["error"])
            self.assertIsNone(job["pending_ask"])
            self.assertEqual(job["confirm_request_id"], "")
        self.assertEqual(len(J.list_jobs()), 4)

    def test_history_limit_never_deletes_active_jobs(self):
        with patch.object(J, "_MAX_JOBS", 2):
            active = [self.create() for _ in range(3)]
            old = self.create()
            J.update_job(old, status="completed")
            self.create()
            self.assertIsNone(J.get_job(old))
            self.assertTrue(all(J.get_job(jid) for jid in active))

    def test_live_stream_continues_after_event_buffer_rolls(self):
        async def run():
            jid = self.create()
            for i in range(100):
                J.append_event(jid, "progress", i)
            response = await L.api_job_events(jid, None)
            stream = response.body_iterator
            for i in range(1, 101):
                self.assertIn(f"id: {i}\n", await anext(stream))
            J.append_event(jid, "progress", 100)
            with patch.object(L.asyncio, "sleep", new=AsyncMock()):
                item = await asyncio.wait_for(anext(stream), 2)
            self.assertIn("id: 101\n", item)
            await stream.aclose()
        asyncio.run(run())

    def test_reconnect_and_expired_history(self):
        jid = self.create()
        for i in range(105):
            J.append_event(jid, "progress", i)
        J.update_job(jid, status="completed")
        client = TestClient(L.app)
        url = f"/api/v1/jobs/{jid}/events"
        response = client.get(url, headers={"Last-Event-ID": "104"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("id: 105\n", response.text)
        self.assertNotIn("id: 104\n", response.text)
        expired = client.get(url, headers={"Last-Event-ID": "1"}).text
        self.assertIn("event: reset", expired)
        self.assertIn('"oldest_id": 6', expired)
        for value in ("bad", "-1", "106"):
            self.assertEqual(client.get(url, headers={"Last-Event-ID": value}).status_code, 400)

    def test_legacy_events_receive_stable_ids(self):
        jid = self.create()
        J.update_job(jid, events=[{"kind": "progress", "data": "legacy"}])
        self.assertEqual(J.get_job(jid)["events"][0]["id"], 1)
        J.append_event(jid, "progress", "new")
        stored = json.loads(J._job_file(jid).read_text(encoding="utf-8"))
        self.assertEqual([e["id"] for e in stored["events"]], [1, 2])


if __name__ == "__main__":
    unittest.main()
