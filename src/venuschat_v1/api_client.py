"""HTTP + SSE client for VenusChat V1 → llm_server."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import quote, urlparse

from .config_store import llm_base, team_token_for_base, token_for_base


def installation_code_confirmation(data: dict, expected_code: str = "") -> tuple[str, bool]:
    """Interpret only server-returned evidence about installation-code binding."""
    if not isinstance(data, dict):
        return "", False
    application = data.get("application")
    application = application if isinstance(application, dict) else {}
    binding = str(application.get("installation_code_binding") or
                  data.get("installation_code_binding") or "")
    returned_code = str(application.get("installation_code") or
                         data.get("installation_code") or "")
    if binding == "bound" or (expected_code and returned_code == expected_code):
        return "bound", True
    return binding, False


class _DenyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward enrollment/device credentials to a redirected origin."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ApiClient:
    def __init__(self, base: str | None = None, *, token: str | None = None,
                 token_header: str | None = None, use_default_token: bool = True,
                 deny_redirects: bool = False) -> None:
        self.base = (base or llm_base()).rstrip("/")
        self._explicit_token = token
        self._explicit_token_header = token_header
        self._use_default_token = use_default_token
        self._deny_redirects = deny_redirects
        self._no_redirect_opener = urllib.request.build_opener(_DenyRedirectHandler())
        self._tailnet_opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _DenyRedirectHandler())

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._explicit_token_header:
            if self._explicit_token:
                h[self._explicit_token_header] = self._explicit_token
            return h
        if not self._use_default_token:
            return h
        is_team_origin, team_token = team_token_for_base(self.base)
        if is_team_origin:
            if team_token:
                h["X-Team-Device-Token"] = team_token
            return h
        tok = token_for_base(self.base)
        if tok:
            h["X-Api-Token"] = tok
        return h

    def open(self, request: urllib.request.Request, *, timeout: float):
        host = (urlparse(self.base).hostname or "").casefold()
        if host.endswith(".ts.net"):
            # MagicDNS resolves inside the tailnet. A system HTTPS proxy cannot
            # reach it and must never receive a team device credential.
            return self._tailnet_opener.open(request, timeout=timeout)
        is_team_origin, _token = team_token_for_base(self.base)
        if self._deny_redirects or is_team_origin:
            return self._no_redirect_opener.open(request, timeout=timeout)
        return urllib.request.urlopen(request, timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        timeout: float = 15,
    ) -> tuple[int, dict]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        # Project IDs can contain Chinese characters; urllib requires an ASCII
        # URL while existing percent escapes and query separators stay intact.
        url = quote(self.base + path, safe=":/?&=%+@")
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self._headers())
        try:
            with self.open(req, timeout=timeout) as resp:
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

    @staticmethod
    def _rejects_optional_installation_code(data: dict) -> bool:
        detail = (data or {}).get("detail") if isinstance(data, dict) else None
        if isinstance(detail, list):
            for issue in detail:
                if not isinstance(issue, dict):
                    continue
                location = issue.get("loc") or ()
                issue_type = str(issue.get("type") or "").casefold()
                if ("installation_code" in location
                        and (issue_type.endswith("extra_forbidden")
                             or issue_type.endswith("value_error.extra"))):
                    return True
        detail_text = str(detail or "").casefold()
        return ("installation_code" in detail_text
                and ("extra fields not permitted" in detail_text
                     or "extra inputs are not permitted" in detail_text
                     or "extra_forbidden" in detail_text))

    def post_with_optional_installation_code(
        self, path: str, payload: dict, *, timeout: float = 15,
    ) -> tuple[int, dict, bool]:
        """POST a join/claim body, falling back only if an old Hub rejects the field.

        Returns ``(status, body, sent_code)``.  A new Hub validates and binds
        the public installation code.  A legacy Hub that explicitly rejects
        the optional field receives the unchanged legacy request contract.
        """
        body = dict(payload or {})
        has_code = bool(body.get("installation_code"))
        code, data = self.post(path, body, timeout=timeout)
        if not (has_code and code == 422
                and self._rejects_optional_installation_code(data)):
            return code, data, has_code
        legacy_body = dict(body)
        legacy_body.pop("installation_code", None)
        code, data = self.post(path, legacy_body, timeout=timeout)
        return code, data, False

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
    if event == "done":
        return "done", None
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
            with self.client.open(req, timeout=600) as resp:
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
                            if kind in ("done", "error"):
                                done_sent = True
                                if kind == "error":
                                    self.on_error(str(payload))
                                else:
                                    self.on_done()
                                return
                            self.on_event(kind, payload)
                            continue
                        if line.startswith("event:"):
                            current_event = line[6:].strip()
                        elif line.startswith("data:"):
                            buf_lines.append(line[5:].strip())
        except urllib.error.HTTPError as exc:
            try:
                error_body = json.loads(exc.read(4096).decode("utf-8", "replace"))
                detail = error_body.get("detail") if isinstance(error_body, dict) else None
                detail = str(detail or f"HTTP {exc.code}")
            except (ValueError, OSError):
                detail = f"HTTP {exc.code}"
            if not self._cancel.is_set():
                self.on_error(detail)
            return
        except Exception as exc:
            if not self._cancel.is_set():
                self.on_error(f"流式连接失败：{exc}")
            return
        if not done_sent and not self._cancel.is_set():
            self.on_done()
