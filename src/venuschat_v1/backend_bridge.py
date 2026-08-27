"""Session + stream orchestration for VenusChat V1."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .api_client import ApiClient, ChatStreamWorker
from .config_store import load_config


@dataclass
class SessionState:
    sid: int
    title: str = ""
    messages: list[dict] = field(default_factory=list)
    loaded: bool = False
    version: int = 0
    updated: str = ""
    message_count: int = 0


class BackendBridge:
    """Background worker bridging llm_server APIs to Tk main thread."""

    def __init__(self, client: ApiClient, ui: Callable[[str, Any], None]) -> None:
        self.client = client
        self.ui = ui
        self.sessions: dict[int, SessionState] = {}
        self.current_sid: int | None = None
        self.projects: list[dict] = []
        self.active_project_id: str = ""
        self.health: dict = {}
        self.jobs_active: int = 0
        self._queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()
        self._stream = ChatStreamWorker(
            client,
            on_event=self._on_stream_event,
            on_done=self._stream_done,
            on_error=self._stream_error,
        )
        self._streaming = False
        self._stream_buf = ""
        self._stream_task = 0
        self._message_cb: Callable[[str], Any] | None = None

    def submit(self, kind: str, fn: Callable[[], Any]) -> None:
        self._queue.put((kind, fn))

    def _loop(self) -> None:
        while True:
            kind, fn = self._queue.get()
            try:
                result = fn()
                self._queue.put(("__result__", (kind, result)))
            except Exception as exc:
                self._queue.put(("__result__", (kind, ("error", str(exc)))))

    def poll(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item[0] == "__result__":
                kind, payload = item[1]
                self.ui(kind, payload)
            else:
                break

    # Bootstrap ----------------------------------------------------------------
    def refresh_all(self) -> None:
        self.submit("health", lambda: ("ok", self.client.get("/api/v1/health")))
        self.submit("sessions", lambda: ("ok", self.client.get("/api/v1/sessions")))
        self.submit("projects", lambda: ("ok", self.client.get("/api/v1/projects")))
        self.submit("jobs", lambda: ("ok", self.client.get("/api/v1/jobs?limit=20")))

    def _apply_health(self, code: int, data: dict) -> None:
        if code == 200:
            self.health = data
            jobs = data.get("jobs") or {}
            self.jobs_active = int(jobs.get("active") or 0)

    def _apply_sessions(self, code: int, data: dict) -> None:
        if code != 200:
            return
        self.sessions.clear()
        for row in data.get("sessions") or []:
            sid = int(row["id"])
            self.sessions[sid] = SessionState(
                sid=sid,
                title=str(row.get("title") or f"会话 {sid}"),
                loaded=False,
                version=int(row.get("version") or 0),
                updated=str(row.get("updated") or ""),
                message_count=int(row.get("message_count") or 0),
            )
        if self.sessions and self.current_sid not in self.sessions:
            self.current_sid = max(self.sessions)

    def _apply_projects(self, code: int, data: dict) -> None:
        if code != 200:
            return
        self.projects = list(data.get("projects") or [])
        self.active_project_id = str(data.get("active") or "")

    # Sessions -----------------------------------------------------------------
    def create_session(self) -> None:
        def _do():
            return ("ok", self.client.post("/api/v1/sessions"))
        self.submit("session_new", _do)

    def load_session(self, sid: int) -> None:
        self.current_sid = sid
        sess = self.sessions.get(sid)
        if sess and sess.loaded:
            self.ui("session_ready", sid)
            return

        def _do():
            return ("ok", self.client.get(f"/api/v1/sessions/{sid}"))
        self.submit("session_load", _do)

    def delete_session(self, sid: int) -> None:
        def _do():
            return self.client.delete(f"/api/v1/sessions/{sid}")
        self.submit("session_delete", _do)

    def _apply_session_load(self, code: int, data: dict, sid: int) -> None:
        if code != 200:
            self.ui("error", data.get("detail", "加载会话失败"))
            return
        body = data.get("session") or {}
        msgs = list(body.get("messages") or [])
        sess = self.sessions.setdefault(sid, SessionState(sid=sid))
        sess.messages = msgs
        sess.title = str(body.get("title") or sess.title)
        sess.loaded = True
        sess.version = int(body.get("version") or 0)
        self.ui("session_ready", sid)

    # Chat ---------------------------------------------------------------------
    def send_message(self, text: str, message_cb: Callable[[str], Any]) -> None:
        if self._streaming:
            self.ui("toast", "请等待当前回复完成")
            return
        if self.current_sid is None:
            self.ui("toast", "请先新建或选择会话")
            return
        text = text.strip()
        if not text:
            return
        sess = self.sessions[self.current_sid]
        if not sess.loaded:
            self.ui("toast", "会话加载中…")
            return

        # 异步派活：以 ! 或 /dispatch 开头
        if text.startswith("!") or text.lower().startswith("/dispatch "):
            task = text[1:].strip() if text.startswith("!") else text[len("/dispatch "):].strip()
            self.dispatch(task, sess)
            return

        user_msg = {"role": "user", "content": text}
        sess.messages.append(user_msg)
        self._message_cb = message_cb
        self._stream_buf = ""
        self._streaming = True
        self._stream_task += 1
        task_id = self._stream_task
        handle = message_cb("")
        self.ui("stream_start", handle)

        body = {
            "messages": sess.messages,
            "agent": True,
            "session_id": self.current_sid,
            "request_id": f"v1-{task_id}-{uuid.uuid4().hex[:6]}",
            "workspace": str(load_config().get("workspace") or ""),
            "session_version": sess.version,
            "project_id": self.active_project_id or None,
        }
        self._stream.start(body)
        self._persist_messages([user_msg])

    def dispatch(self, text: str, sess: SessionState | None = None) -> None:
        sess = sess or (self.sessions.get(self.current_sid or -1) if self.current_sid else None)
        if not sess:
            self.ui("toast", "请先选择会话")
            return
        messages = list(sess.messages) + [{"role": "user", "content": text}]

        def _do():
            return ("ok", self.client.post("/api/v1/jobs", {
                "messages": messages,
                "session_id": sess.sid,
                "title": text[:80],
            }))
        self.submit("dispatch", _do)

    def _persist_messages(self, msgs: list[dict]) -> None:
        sid = self.current_sid
        if sid is None:
            return

        def _do():
            return self.client.post(
                f"/api/v1/sessions/{sid}/messages",
                {"messages": msgs, "request_id": f"v1-{sid}-{int(time.time() * 1000)}"},
            )
        self.submit("sess_append", _do)

    def respond_confirm(self, allowed: bool, request_id: str) -> None:
        # llm_server 判定用的是 choice == "yes"，必须原样回传。
        def _do():
            return self.client.post("/api/v1/agent/respond", {
                "request_id": request_id,
                "choice": "yes" if allowed else "no",
            })
        self.submit("confirm", _do)

    def cancel_stream(self) -> None:
        self._stream.cancel()
        self._streaming = False

    def _on_stream_event(self, kind: str, payload: Any) -> None:
        self._queue.put(("__result__", ("stream_event", (kind, payload))))

    def _stream_done(self) -> None:
        self._queue.put(("__result__", ("stream_event", ("done", None))))

    def _stream_error(self, msg: str) -> None:
        self._queue.put(("__result__", ("stream_event", ("error", msg))))

    def handle_stream_event(self, kind: str, payload: Any) -> None:
        if kind == "delta":
            chunk, _reason = payload
            if chunk:
                self._stream_buf += str(chunk)
                self.ui("stream_delta", self._stream_buf)
        elif kind in ("tool_call", "tool_result", "todo_update"):
            self.ui(f"stream_{kind}", payload)
        elif kind == "ask":
            self.ui("stream_ask", payload)
        elif kind == "done":
            self._finish_stream()
        elif kind == "error":
            self.ui("stream_error", str(payload))
            self._finish_stream()

    def _finish_stream(self) -> None:
        if not self._streaming:
            return
        self._streaming = False
        text = self._stream_buf.strip()
        if text and self.current_sid is not None:
            msg = {"role": "assistant", "content": text}
            self.sessions[self.current_sid].messages.append(msg)
            self._persist_messages([msg])
        self.ui("stream_done", text)
        self.submit("jobs", lambda: ("ok", self.client.get("/api/v1/jobs?limit=20")))
