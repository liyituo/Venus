"""HTTP + SSE client for VenusChat V1 → llm_server."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

from .config_store import llm_base, token_for_base


class ApiClient:
    def __init__(self, base: str | None = None) -> None:
        self.base = (base or llm_base()).rstrip("/")

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        tok = token_for_base(self.base)
        if tok:
            h["X-Api-Token"] = tok
        return h

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        timeout: float = 15,
    ) -> tuple[int, dict]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                try:
                    return resp.status, json.loads(body.decode("utf-8"))
                except ValueError:
                    return resp.status, {"detail": "non-JSON response"}
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except Exception:
                detail = {"detail": f"HTTP {exc.code}"}
            return exc.code, detail
        except Exception as exc:
            return 0, {"detail": str(exc)}

    def get(self, path: str, **kw: Any) -> tuple[int, dict]:
        return self.request("GET", path, **kw)

    def post(self, path: str, payload: dict | None = None, **kw: Any) -> tuple[int, dict]:
        return self.request("POST", path, payload, **kw)

    def put(self, path: str, payload: dict | None = None, **kw: Any) -> tuple[int, dict]:
        return self.request("PUT", path, payload, **kw)

    def patch(self, path: str, payload: dict | None = None, **kw: Any) -> tuple[int, dict]:
        return self.request("PATCH", path, payload, **kw)

    def delete(self, path: str, **kw: Any) -> tuple[int, dict]:
        return self.request("DELETE", path, **kw)


def parse_sse_block(event: str, data_lines: list[str]) -> tuple[str | None, Any]:
    payload = "\n".join(data_lines)
    if event in ("tool_call", "tool_result", "ask", "todo_update"):
        try:
            return event, json.loads(payload)
        except Exception:
            return event, payload
    if event == "error":
        try:
            d = json.loads(payload)
            return "error", d.get("detail", d) if isinstance(d, dict) else d
        except Exception:
            return "error", payload
    if payload == "[DONE]":
        return "done", None
    try:
        data = json.loads(payload)
        delta = (data.get("choices") or [{}])[0].get("delta") or {}
        return "delta", (delta.get("content") or "", delta.get("reasoning_content") or "")
    except Exception:
        return None, None


class ChatStreamWorker:
    """Background SSE reader for /api/v1/chat/stream."""

    def __init__(
        self,
        client: ApiClient,
        *,
        on_event: Callable[[str, Any], None],
        on_done: Callable[[], None],
        on_error: Callable[[str], None],
    ) -> None:
        self.client = client
        self.on_event = on_event
        self.on_done = on_done
        self.on_error = on_error
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._resp = None

    def cancel(self) -> None:
        self._cancel.set()
        if self._resp is not None:
            try:
                self._resp.close()
            except Exception:
                pass

    def start(self, body: dict) -> None:
        self.cancel()
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(body,), daemon=True)
        self._thread.start()

    def _run(self, body: dict) -> None:
        url = f"{self.client.base}/api/v1/chat/stream"
        headers = self.client._headers()
        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        done_sent = False
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                self._resp = resp
                current_event = ""
                buf_lines: list[str] = []
                byte_buf = b""
                for raw in resp:
                    if self._cancel.is_set():
                        return
                    byte_buf += raw
                    while b"\n" in byte_buf:
                        line_b, byte_buf = byte_buf.split(b"\n", 1)
                        line = line_b.decode("utf-8", "replace").rstrip("\r")
                        if line == "":
                            if not buf_lines:
                                continue
                            kind, payload = parse_sse_block(current_event, buf_lines)
                            buf_lines = []
                            current_event = ""
                            if kind is None:
                                continue
                            if kind == "delta" and not (payload[0] or payload[1]):
                                continue
                            self.on_event(kind, payload)
                            if kind in ("done", "error"):
                                done_sent = True
                                if kind == "error":
                                    self.on_error(str(payload))
                                else:
                                    self.on_done()
                                return
                            continue
                        if line.startswith("event:"):
                            current_event = line[6:].strip()
                        elif line.startswith("data:"):
                            buf_lines.append(line[5:].strip())
        except Exception as exc:
            if not self._cancel.is_set():
                self.on_error(f"流式连接失败：{exc}")
            return
        if not done_sent and not self._cancel.is_set():
            self.on_done()
