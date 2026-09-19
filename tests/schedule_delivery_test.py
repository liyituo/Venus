"""Backend-only scheduling and Telegram delivery regressions (no network)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ["VENUS_DATA_DIR"] = tempfile.mkdtemp(prefix="venus_schedule_tests_")
os.environ["PCAGENT_DISABLE_MCP"] = "1"
os.environ["PCAGENT_ALLOW_TEST_HOST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import agent_jobs as J  # noqa: E402
import llm_server as L  # noqa: E402
import schedule_store as S  # noqa: E402
import telegram_bot as T  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class ScheduleDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="venus_schedule_case_"))
        self.patches = [patch.object(S, "_path", return_value=self.temp / "schedules.json"),
                        patch.object(J, "_jobs_root", return_value=self.temp / "jobs"),
                        patch.object(J, "enqueue_job"),
                        patch.object(T, "NOTIFICATIONS_FILE", self.temp / "notifications.json")]
        for p in self.patches:
            p.start()
        self.client = TestClient(L.app)
        self.bot = T.Bot.__new__(T.Bot)
        self.bot.cfg = {"allowed_chat_ids": [100], "owner_user_id": 100}
        self.bot._job_notifications = {}
        self.bot.send_message = Mock(return_value={"ok": True})
        self.bot.agent_flow = Mock(side_effect=AssertionError("Bot must not run scheduled agents"))

        def request(method, path, payload=None, timeout=30):
            response = self.client.request(method, path, json=payload)
            return response.status_code, response.json()

        self.bot.llm = Mock(side_effect=request)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def create(self, **kwargs):
        return J.create_job(messages=[{"role": "user", "content": "offline"}],
                            channel="telegram", chat_id=100, **kwargs)["id"]

    def test_legacy_schedule_and_dispatch_retry_are_idempotent(self):
        S._path().write_text(json.dumps([{
            "id": "s7", "time": "08:00", "prompt": "legacy", "chat_id": 100,
            "enabled": True, "last_run": "",
        }]), encoding="utf-8")
        row = S.list_schedules()[0]
        self.assertEqual(row["channel"], "telegram")
        # Crash after creating/enqueuing the job, before marking the schedule.
        with patch.object(S, "due_schedules", return_value=[row]), \
                patch.object(S, "mark_ran", side_effect=OSError("disk unavailable")):
            L._dispatch_due_schedules()
        with patch.object(S, "due_schedules", return_value=[row]):
            L._dispatch_due_schedules()
        jobs = J.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["channel"], "telegram")
        self.assertEqual(jobs[0]["chat_id"], 100)
        self.assertTrue(S.get_schedule("s7")["last_run"])
        self.assertEqual(J.enqueue_job.call_args_list[0], J.enqueue_job.call_args_list[1])

    def test_hour_normalization_and_persistent_ids(self):
        first = S.add_schedule(time_hhmm="8:05", prompt="daily")
        self.assertEqual(first["time"], "08:05")
        self.assertEqual(len(S.due_schedules("08:05", "2026-09-19")), 1)
        S.delete_schedule(first["id"])
        second = S.add_schedule(time_hhmm="09:00", prompt="daily")
        self.assertGreater(int(second["id"]), int(first["id"]))
        S.update_schedule(second["id"], time="7:01")
        self.assertEqual(S.get_schedule(second["id"])["time"], "07:01")

    def test_confirm_then_result_delivered_once_across_bot_restart(self):
        jid = self.create()
        ask = {"id": "ask-1", "question": "Run?", "diff": "+ change", "plan": [{"step": "verify"}]}
        J.update_job(jid, status="waiting_confirm", pending_ask=ask)
        self.bot._poll_job_notifications()
        args = self.bot.send_message.call_args
        self.assertIn("Run?", args.args[1])
        self.assertIn("+ change", args.args[1])
        self.assertEqual(args.kwargs["keyboard"]["inline_keyboard"][0][0]["callback_data"], "yes:ask-1")
        self.bot._poll_job_notifications()
        self.assertEqual(self.bot.send_message.call_count, 1)
        J.update_job(jid, status="completed", result_summary="done", pending_ask=None)
        self.bot._poll_job_notifications()
        self.assertEqual(self.bot.send_message.call_count, 2)
        self.assertIn("done", self.bot.send_message.call_args.args[1])
        self.bot._job_notifications = self.bot._load_job_notifications()
        self.bot._poll_job_notifications()
        self.assertEqual(self.bot.send_message.call_count, 2)
        self.bot.agent_flow.assert_not_called()

    def test_failed_delivery_retries_and_unknown_chat_is_not_notified(self):
        jid = self.create()
        J.update_job(jid, status="failed", error="interrupted")
        other = J.create_job(messages=[{"role": "user", "content": "private"}],
                             channel="telegram", chat_id=999)["id"]
        J.update_job(other, status="completed", result_summary="private")
        self.bot.send_message.return_value = {"ok": False}
        self.bot._poll_job_notifications()
        self.assertNotIn(jid, self.bot._job_notifications)
        self.bot.send_message.return_value = {"ok": True}
        self.bot._poll_job_notifications()
        self.assertEqual(self.bot._job_notifications[jid], "failed")
        self.assertEqual(self.bot.send_message.call_count, 2)
        self.assertTrue(all(c.args[0] == 100 for c in self.bot.send_message.call_args_list))


if __name__ == "__main__":
    unittest.main()
