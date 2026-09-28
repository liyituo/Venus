"""Exercise the request/result handoff without a display or a backend."""
from __future__ import annotations

import queue
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from venuschat_v1.backend_bridge import BackendBridge  # noqa: E402


class BridgeTest(unittest.TestCase):
    def test_poll_does_not_consume_requests(self):
        bridge = BackendBridge.__new__(BackendBridge)
        bridge._default_client = None
        bridge._context_epoch = 0
        bridge._work_queue = queue.Queue()
        bridge._result_queue = queue.Queue()
        bridge.ui = lambda *args: self.fail("No result should be ready")
        bridge.submit("health", lambda: "ok")
        bridge.poll()
        self.assertEqual(bridge._work_queue.qsize(), 1)

    def test_worker_results_and_stream_events_reach_ui_once(self):
        received = []
        ui_thread = threading.get_ident()

        def ui(kind, payload):
            self.assertEqual(threading.get_ident(), ui_thread)
            received.append((kind, payload))

        bridge = BackendBridge(None, ui)
        bridge._streaming = True
        bridge._stream_task = 7
        release = threading.Event()
        started = threading.Event()
        executed = []

        def blocked_request():
            started.set()
            self.assertTrue(release.wait(3))
            executed.append("first")
            return "ok"

        def failed_request():
            executed.append("second")
            raise ValueError("offline")

        bridge.submit("first", blocked_request)
        self.assertTrue(started.wait(3))
        bridge.submit("second", failed_request)
        bridge._on_stream_event(7, "tool_call", {"name": "demo"})
        bridge.poll()
        self.assertEqual(received, [("stream_event", ("tool_call", {"name": "demo"}))])
        release.set()
        deadline = time.monotonic() + 3
        while len(received) < 3 and time.monotonic() < deadline:
            bridge.poll()
            time.sleep(0.01)
        self.assertEqual(received[1:], [("first", "ok"), ("worker_error", ("second", "offline"))])
        self.assertEqual(executed, ["first", "second"])
        bridge._stream_done(7)
        bridge._stream_error(7, "disconnected")
        bridge.poll()
        self.assertEqual(received[-2:], [
            ("stream_event", ("done", None)), ("stream_event", ("error", "disconnected"))])
        bridge.poll()
        self.assertEqual(len(received), 5)


if __name__ == "__main__":
    unittest.main()
