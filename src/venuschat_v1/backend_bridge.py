"""Session + stream orchestration for VenusChat V1."""

from __future__ import annotations

import queue
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .api_client import ApiClient, ChatStreamWorker
from .config_store import (active_project_for_origin, load_config,
                           save_active_project_for_origin, team_connection)


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
        self._default_client = client
        self._request_context = threading.local()
        self._context_epoch = 0
        self.ui = ui
        self.sessions: dict[int, SessionState] = {}
        self.current_sid: int | None = None
        self.projects: list[dict] = []
        self.active_project_id: str = ""
        self.health: dict = {}
        self.jobs_active: int = 0
        # Keep commands and completed results separate: polling must never
        # consume a command before the worker has a chance to run it.
        self._work_queue: queue.Queue = queue.Queue()
        self._result_queue: queue.Queue = queue.Queue()
        self._worker = self._start_worker(self._work_queue)
        self._stream: ChatStreamWorker | None = None
        self._streaming = False
        self._stream_buf = ""
        self._stream_parts: list[str] = []
        self._stream_dirty = False
        self._stream_task = 0
        self._stream_sid: int | None = None
        self._loading_sids: set[int] = set()

    def submit(self, kind: str, fn: Callable[[], Any]) -> None:
        self._work_queue.put((kind, fn, self._default_client, self._context_epoch))

    def _start_worker(self, work_queue: queue.Queue) -> threading.Thread:
        worker = threading.Thread(target=self._loop, args=(work_queue,), daemon=True)
        worker.start()
        return worker

    @property
    def client(self) -> ApiClient:
        return getattr(self._request_context, "client", self._default_client)

    @client.setter
    def client(self, value: ApiClient) -> None:
        self.set_client(value)

    @property
    def context_epoch(self) -> int:
        return self._context_epoch

    def set_client(self, client: ApiClient) -> None:
        """Change origins atomically; queued work keeps its captured client."""
        old_queue = self._work_queue
        old_queue.put(None)
        self._default_client = client
        self._context_epoch += 1
        self._work_queue = queue.Queue()
        self._worker = self._start_worker(self._work_queue)
        # A load completion from the previous origin will be discarded by the
        # epoch guard; allow the same numeric session id to load on this one.
        self._loading_sids.clear()

    def is_idle(self) -> bool:
        """Whether changing the API origin is safe from queued cross-origin work."""
        return (self._work_queue.unfinished_tasks == 0
                and self._result_queue.empty())

    def emit(self, kind: str, payload: Any) -> None:
        """Deliver progress from a worker without queuing it behind that worker."""
        self._result_queue.put((kind, payload, self._context_epoch))

    def _loop(self, work_queue: queue.Queue) -> None:
        while True:
            item = work_queue.get()
            if item is None:
                work_queue.task_done()
                return
            kind, fn, client, epoch = item
            self._request_context.client = client
            self._request_context.epoch = epoch
            try:
                result = fn()
                self._result_queue.put((kind, result, epoch))
            except Exception as exc:
                self._result_queue.put(("worker_error", (kind, str(exc)), epoch))
            finally:
                self._request_context.__dict__.clear()
                work_queue.task_done()

    def poll(self) -> None:
        # A busy SSE stream can emit faster than Tk can draw. Bound each UI
        # tick so a large backlog cannot freeze clicks and window movement.
        pending_delta = False
        for _ in range(128):
            try:
                kind, payload, epoch = self._result_queue.get_nowait()
            except queue.Empty:
                break
            if epoch != self._context_epoch:
                continue
            if kind == "stream_event":
                task_id, stream_kind, stream_payload = payload
                if not self._streaming or task_id != self._stream_task:
                    continue
                if stream_kind == "delta":
                    pending_delta = self._append_stream_delta(stream_payload) or pending_delta
                    continue
                if pending_delta:
                    self.ui("stream_delta", self._stream_text())
                    pending_delta = False
                self.ui(kind, (stream_kind, stream_payload))
                continue
            if pending_delta:
                self.ui("stream_delta", self._stream_text())
                pending_delta = False
            if kind == "sess_append":
                sid, response = payload
                code, data = response
                if code == 200 and isinstance(data, dict):
                    sess = self.sessions.get(sid)
                    summary = data.get("session") or {}
                    if sess is not None:
                        sess.version = int(summary.get("version") or sess.version)
                        sess.title = str(summary.get("title") or sess.title)
                        sess.message_count = int(
                            summary.get("message_count") or len(sess.messages))
                self.ui(kind, response)
                continue
            self.ui(kind, payload)
        if pending_delta and self._streaming:
            self.ui("stream_delta", self._stream_text())

    # Bootstrap ----------------------------------------------------------------
    def _team_context(self) -> bool:
        try:
            return bool(team_connection(self.client.base))
        except ValueError:
            return False

    def refresh_health(self) -> None:
        # The full health response contains machine-wide details and is not
        # exposed through Tailscale Serve. Its small ready response carries
        # only the state the team workspace needs to enable dispatch.
        path = "/api/v1/ready" if self._team_context() else "/api/v1/health"
        self.submit("health", lambda: ("ok", self.client.get(
            path, timeout=8)))

    def refresh_all(self) -> None:
        self.refresh_health()
        if self._team_context():
            # Shared Hub sessions are intentionally not exposed through Serve.
            self.submit("sessions", lambda: ("ok", (200, {"sessions": []})))
        else:
            self.submit("sessions", lambda: ("ok", self.client.get("/api/v1/sessions", timeout=8)))
        if not self._team_context():
            self.submit("projects", lambda: ("ok", self.client.get("/api/v1/projects", timeout=8)))
            self.submit("jobs", lambda: ("ok", self.client.get("/api/v1/jobs?limit=20", timeout=8)))

    def _apply_health(self, code: int, data: dict) -> None:
        if code == 200:
            self.health = data
            jobs = data.get("jobs") or {}
            self.jobs_active = int(jobs.get("active") or 0)

    def _apply_sessions(self, code: int, data: dict) -> None:
        if code != 200:
            return
        previous = self.sessions
        refreshed: dict[int, SessionState] = {}
        for row in data.get("sessions") or []:
            sid = int(row["id"])
            sess = previous.get(sid) or SessionState(sid=sid)
            sess.title = str(row.get("title") or sess.title or f"会话 {sid}")
            if row.get("version") is not None:
                sess.version = int(row["version"])
            sess.updated = str(row.get("updated") or sess.updated)
            sess.message_count = int(row.get("message_count") or 0)
            refreshed[sid] = sess
        self.sessions = refreshed
        if self.current_sid not in self.sessions:
            self.current_sid = max(self.sessions, default=None)

    def _apply_projects(self, code: int, data: dict) -> None:
        if code != 200:
            return
        self.projects = list(data.get("projects") or [])
        # A joined Hub reports an active project for this user and device.
        # The legacy personal server has process-global selection, which must
        # never override this client's locally selected dispatch target.
        project_ids = {str(row.get("id") or "") for row in self.projects}
        if "active" in data and self._team_context():
            server_active = str(data.get("active") or "")
            self.active_project_id = server_active if server_active in project_ids else ""
            save_active_project_for_origin(self.client.base, self.active_project_id)
            return
        if self.active_project_id not in project_ids:
            self.active_project_id = active_project_for_origin(self.client.base)
        if self.active_project_id not in project_ids:
            self.active_project_id = ""

    # Sessions -----------------------------------------------------------------
    def create_session(self) -> None:
        def _do():
            return ("ok", self.client.post("/api/v1/sessions"))
        self.submit("session_new", _do)

    def load_session(self, sid: int) -> None:
        if sid not in self.sessions:
            self.ui("toast", "对话已不存在，请刷新列表")
            return
        if self._streaming:
            if sid != self._stream_sid:
                self.ui("toast", "请先停止当前回复，再切换对话")
            return
        self.current_sid = sid
        sess = self.sessions.get(sid)
        if sess and sess.loaded:
            self.ui("session_ready", sid)
            return
        if sid in self._loading_sids:
            return
        self._loading_sids.add(sid)

        def _do():
            try:
                return sid, self.client.get(f"/api/v1/sessions/{sid}", timeout=8)
            except Exception as exc:
                return sid, (0, {"detail": str(exc)})
        self.submit("session_load", _do)

    def delete_session(self, sid: int) -> None:
        def _do():
            return self.client.delete(f"/api/v1/sessions/{sid}")
        self.submit("session_delete", _do)

    def _apply_session_load(self, code: int, data: dict, sid: int) -> None:
        self._loading_sids.discard(sid)
        if code != 200:
            if sid == self.current_sid:
                detail = data.get("detail", "加载会话失败") if isinstance(data, dict) else data
                self.ui("error", detail)
            return
        if sid not in self.sessions:
            return
        body = data.get("session") or {}
        msgs = list(body.get("messages") or [])
        sess = self.sessions[sid]
        sess.messages = msgs
        sess.title = str(body.get("title") or sess.title)
        sess.loaded = True
        sess.version = int(body.get("version") or sess.version)
        sess.message_count = len(msgs)
        if sid == self.current_sid:
            self.ui("session_ready", sid)

    # Chat ---------------------------------------------------------------------
    def send_message(self, text: str, message_cb: Callable[[str], Any],
                     *, agent_preference: str = "") -> None:
        if self._streaming:
            self.ui("toast", "请等待当前回复完成")
            return
        sid = self.current_sid
        if sid is None:
            self.ui("toast", "请先新建或选择会话")
            return
        text = text.strip()
        if not text:
            return
        sess = self.sessions.get(sid)
        if sess is None:
            self.ui("toast", "对话已不存在，请刷新列表")
            return
        if not sess.loaded:
            self.ui("toast", "会话加载中…")
            return

        # 异步派活：以 ! 或 /dispatch 开头
        if text.startswith("!") or text.lower().startswith("/dispatch "):
            task = text[1:].strip() if text.startswith("!") else text[len("/dispatch "):].strip()
            self.dispatch(task, sess, agent_preference=agent_preference)
            return

        user_msg = {"role": "user", "content": text}
        sess.messages.append(user_msg)
        self._stream_buf = ""
        self._stream_parts.clear()
        self._stream_dirty = False
        self._streaming = True
        self._stream_sid = sid
        self._stream_task += 1
        task_id = self._stream_task
        handle = message_cb("")
        self.ui("stream_start", handle)

        body = {
            "messages": self._messages_with_preference(sess.messages, agent_preference),
            "agent": True,
            "session_id": sid,
            "request_id": f"v1-{task_id}-{uuid.uuid4().hex[:6]}",
            "workspace": str(load_config().get("workspace") or ""),
            "session_version": sess.version,
            # Ordinary conversation turns stay private to this session. A
            # project is attached only to an explicitly dispatched job.
            "project_id": None,
        }
        self._persist_messages([user_msg], sid=sid)
        stream = ChatStreamWorker(
            self.client,
            on_event=lambda kind, payload: self._on_stream_event(task_id, kind, payload),
            on_done=lambda: self._stream_done(task_id),
            on_error=lambda message: self._stream_error(task_id, message),
        )
        self._stream = stream
        stream.start(body)

    @staticmethod
    def _messages_with_preference(messages: list[dict], preference: str) -> list[dict]:
        selected = " ".join(str(preference).split())[:60]
        result = list(messages)
        if selected and selected != "通用智能体":
            result.insert(0, {
                "role": "system",
                "content": f"用户在界面选择了子 Agent「{selected}」。适合当前任务时，优先通过 delegate 委派给它；不适合时正常完成任务。",
            })
        return result

    def dispatch(self, text: str, sess: SessionState | None = None,
                 *, agent_preference: str = "") -> None:
        sess = sess or (self.sessions.get(self.current_sid or -1) if self.current_sid else None)
        selected_project = next(
            (row for row in self.projects
             if str(row.get("id") or "") == self.active_project_id), None)
        team_dispatch = bool(selected_project and selected_project.get("is_team"))
        if not team_dispatch and not sess:
            self.ui("toast", "请先选择会话")
            return
        # A team job is shared with every project member. Never forward the
        # private conversation history that happened to be open when the
        # user selected a team project; only the explicitly dispatched task
        # crosses that boundary.
        context = ([{"role": "user", "content": text}]
                   if team_dispatch else
                   list(sess.messages) + [{"role": "user", "content": text}])
        messages = self._messages_with_preference(context, agent_preference)
        project_id = self.active_project_id or None

        def _do():
            return ("ok", self.client.post("/api/v1/jobs", {
                "messages": messages,
                "session_id": None if team_dispatch else sess.sid,
                "title": text[:80],
                "project_id": project_id,
                "request_id": f"v1-job-{sess.sid if sess else 'team'}-{uuid.uuid4().hex}",
            }))
        self.submit("dispatch", _do)

    def _persist_messages(self, msgs: list[dict], *, sid: int | None = None) -> None:
        sid = sid if sid is not None else self.current_sid
        if sid is None:
            return

        def _do():
            return sid, self.client.post(
                f"/api/v1/sessions/{sid}/messages",
                {"messages": msgs, "request_id": f"v1-{sid}-{uuid.uuid4().hex}"},
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
        if not self._streaming:
            return
        if self._stream is not None:
            self._stream.cancel()
        self._finish_stream(notify_done=False)

    def _on_stream_event(self, task_id: int, kind: str, payload: Any) -> None:
        self._result_queue.put(("stream_event", (task_id, kind, payload),
                                self._context_epoch))

    def _stream_done(self, task_id: int) -> None:
        self._on_stream_event(task_id, "done", None)

    def _stream_error(self, task_id: int, msg: str) -> None:
        self._on_stream_event(task_id, "error", msg)

    def handle_stream_event(self, kind: str, payload: Any) -> None:
        if not self._streaming:
            return
        if kind == "delta":
            if self._append_stream_delta(payload):
                self.ui("stream_delta", self._stream_text())
        elif kind in ("tool_call", "tool_result", "todo_update"):
            self.ui(f"stream_{kind}", payload)
        elif kind == "ask":
            self.ui("stream_ask", payload)
        elif kind == "done":
            self._finish_stream()
        elif kind == "error":
            self._finish_stream(notify_done=False)
            self.ui("stream_error", str(payload))

    def _append_stream_delta(self, payload: Any) -> bool:
        chunk, _reason = payload
        if not chunk:
            return False
        self._stream_parts.append(str(chunk))
        self._stream_dirty = True
        return True

    def _stream_text(self) -> str:
        if self._stream_dirty:
            self._stream_buf = "".join(self._stream_parts)
            self._stream_dirty = False
        return self._stream_buf

    def _finish_stream(self, *, notify_done: bool = True) -> None:
        if not self._streaming:
            return
        self._streaming = False
        sid = self._stream_sid
        self._stream_sid = None
        self._stream = None
        text = self._stream_text().strip()
        self._stream_buf = ""
        self._stream_parts.clear()
        self._stream_dirty = False
        if text and sid is not None and sid in self.sessions:
            msg = {"role": "assistant", "content": text}
            self.sessions[sid].messages.append(msg)
            self._persist_messages([msg], sid=sid)
        if notify_done:
            self.ui("stream_done", text)
        self.submit("jobs", lambda: ("ok", self.client.get("/api/v1/jobs?limit=20")))
