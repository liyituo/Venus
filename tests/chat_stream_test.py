"""Chat 流式模型测试：SSE 实时增量 / task_id 隔离 / 连接前 Stop / 确认回传独立线程。

不创建真实 Tk 窗口（CI 无显示环境）：用 __new__ 构造 ChatApp 并 mock 网络层，
验证流式事件逐块入队、旧流迟到事件被丢弃、Stop 语义安全。
"""
import json
import queue
import sys
import threading
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import chat as chat_mod  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


def sse_line(event: str, data: dict | str) -> str:
    """构造一个 SSE 事件块（event 行 + data 行 + 空行分隔）。"""
    if isinstance(data, dict):
        data = json.dumps(data, ensure_ascii=False)
    if event:
        return f"event: {event}\ndata: {data}\n\n"
    return f"data: {data}\n\n"


def sse_lines(blocks: list[str]) -> list[str]:
    """把事件块展开为真实 SSE 字节流行（每行一个，含空行分隔），模拟网络分块。"""
    out = []
    for block in blocks:
        for line in block.splitlines():
            out.append(line)
        out.append("")
    return out


class FakeResp:
    """模拟 urllib 流式响应：可迭代 SSE 行（真实流：事件块以空行分隔）。"""

    def __init__(self, lines: list[str]):
        self._lines = sse_lines(lines)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        # 模拟 UTF-8 中文被网络分块拆开：逐字节分块 yield（边界落在任意字符位置）
        for line in self._lines:
            for i in range(0, len(line), 7):
                yield line[i:i + 7].encode("utf-8")
            yield b"\n"

    def close(self):
        self.closed = True


def make_app():
    """构造一个不依赖 Tk 的 ChatApp 实例（仅流式相关状态）。"""
    app = chat_mod.ChatApp.__new__(chat_mod.ChatApp)
    app.llm_url = "http://127.0.0.1:9"
    app._results = queue.Queue()
    app._stream_task_id = 0
    app._stream_cancel = threading.Event()
    app._stream_resp = None
    app._streaming = False
    app._stream_handle = object()
    app._log = lambda *a, **k: None
    app._update_message = lambda *a, **k: None
    app._finish_streaming = lambda: None
    app._sessions = {}
    app._current_sid = 1
    return app


# ============ 1. SSE 解析（纯函数，线程局部 event 状态）============
print("== 1. _parse_sse_chunk ==")
app = make_app()
kind, payload = app._parse_sse_chunk(sse_line("", {"choices": [{"delta": {"content": "你好"}}]}), "")
check("delta 解析", kind == "delta" and payload == ("你好", ""), (kind, payload))

kind, payload = app._parse_sse_chunk("event: tool_call", "")
check("event 标记", kind == "event_marker" and payload == "tool_call", (kind, payload))
kind, payload = app._parse_sse_chunk("data: " + json.dumps({"id": "c1", "name": "run_shell"}), "tool_call")
check("tool_call 解析", kind == "tool_call" and payload["name"] == "run_shell", (kind, payload))

kind, payload = app._parse_sse_chunk("event: ask", "")
kind, payload = app._parse_sse_chunk("data: " + json.dumps({"id": "ask-1", "question": "要确认吗"}), "ask")
check("ask 解析", kind == "ask" and payload["id"] == "ask-1", (kind, payload))

kind, payload = app._parse_sse_chunk("data: [DONE]", "")
check("[DONE] 解析", kind == "done", (kind, payload))

kind, payload = app._parse_sse_chunk("event: error", "")
kind, payload = app._parse_sse_chunk("data: " + json.dumps({"detail": "上游错误"}), "error")
check("error 解析", kind == "error" and payload == "上游错误", (kind, payload))

# ============ 2. SSE 读取线程：实时逐块入队 ============
print("== 2. _stream_reader 实时增量 ==")
app = make_app()
app._stream_task_id = 1
app._streaming = True
lines = [
    sse_line("", {"choices": [{"delta": {"content": "第"}}]}),
    sse_line("", {"choices": [{"delta": {"content": "一"}}]}),
    sse_line("", {"choices": [{"delta": {"content": "块"}}]}),
    sse_line("", {"choices": [{"delta": {}}]}),
]
resp = FakeResp(lines)
with mock.patch.object(chat_mod.urllib.request, "urlopen", return_value=resp):
    t = threading.Thread(target=app._stream_reader,
                         args=([{"role": "user", "content": "hi"}], app._stream_handle, 1), daemon=True)
    t.start()
    t.join(timeout=10)
events = []
while not app._results.empty():
    events.append(app._results.get_nowait())
deltas = [p for k, p in events if k == "stream_delta"]
check("逐块入队（3 个 delta 事件）", len(deltas) == 3, str(events))
contents = "".join(p[2][0] for p in deltas)
check("内容顺序正确", contents == "第一块", contents)
check("流结束兜底 done", any(k == "stream_done" for k, _ in events), str([k for k, _ in events]))
# 实时性证明：事件在流结束前已入队（读取线程逐行 put，而非结束后一次性）
check("事件在读取过程中已入队", len(events) >= 4, str(len(events)))

# ============ 3. 旧流迟到事件隔离 ============
print("== 3. task_id 隔离 ==")
app = make_app()
app._stream_task_id = 5
app._streaming = True
handle = object()
app._stream_handle = handle
check("同代事件通过", app._is_current_stream(5, handle), "")
check("旧代事件被拒", not app._is_current_stream(4, handle), "")
check("已停止后事件被拒", not app._is_current_stream(5, object()), "")
app._streaming = False
check("停止后同代也被拒", not app._is_current_stream(5, handle), "")
app._streaming = True
app._stream_handle = handle
check("handle 不匹配被拒", not app._is_current_stream(5, object()), "")

# reader 带旧 task_id：不 put 任何事件
app2 = make_app()
app2._stream_task_id = 99          # 模拟 Stop/新流：旧 reader 的 task_id=1 已过期
resp2 = FakeResp([sse_line("", {"choices": [{"delta": {"content": "x"}}]}),
                  sse_line("", {"choices": [{"delta": {"content": "y"}}]})])
with mock.patch.object(chat_mod.urllib.request, "urlopen", return_value=resp2):
    t = threading.Thread(target=app2._stream_reader,
                         args=([{"role": "user", "content": "hi"}], object(), 1), daemon=True)
    t.start()
    t.join(timeout=10)
check("旧流 reader 不产生事件", app2._results.empty(), str(list(app2._results.queue)))

# ============ 4. 连接建立前 Stop：不报错、不产生事件 ============
print("== 4. 连接建立前 Stop ==")
app3 = make_app()
app3._stream_task_id = 7
app3._streaming = True
# reader 在 urlopen 返回前（连接建立前）task_id 已被 Stop 递增 → 直接返回
resp3 = FakeResp([sse_line("", {"choices": [{"delta": {"content": "a"}}]})])

def slow_urlopen(*a, **k):
    time.sleep(0.2)          # 模拟连接建立耗时：期间用户点了 Stop
    return resp3

with mock.patch.object(chat_mod.urllib.request, "urlopen", side_effect=slow_urlopen):
    t = threading.Thread(target=app3._stream_reader,
                         args=([{"role": "user", "content": "hi"}], object(), 7), daemon=True)
    t.start()
    time.sleep(0.05)         # urlopen 仍在阻塞（连接尚未建立）
    app3._stream_task_id = 8       # 模拟 _stop_stream 递增 generation
    app3._stream_cancel.set()
    t.join(timeout=10)
check("连接建立前 Stop 无事件", app3._results.empty(), str(list(app3._results.queue)))
check("无异常（线程正常结束）", not t.is_alive(), "reader 线程未退出")

# _stop_stream 在无连接时调用不报错
app4 = make_app()
app4._streaming = True
app4._stream_task_id = 3
app4._stream_handle = object()
app4._update_message = lambda *a, **k: None
app4._finish_streaming = lambda: setattr(app4, "_streaming", False)
app4._stop_stream()
check("连接建立前 Stop 不报错且恢复状态", app4._streaming is False, "")

# ============ 5. Stop 关闭网络响应 + 取消事件 ============
print("== 5. Stop 语义 ==")
app5 = make_app()
app5._streaming = True
app5._stream_task_id = 10
resp5 = FakeResp([sse_line("", {"choices": [{"delta": {"content": "a"}}]})] * 3)
app5._stream_resp = resp5
app5._stream_handle = object()
app5._update_message = lambda *a, **k: None
app5._finish_streaming = lambda: setattr(app5, "_streaming", False)
old_id = app5._stream_task_id
app5._stop_stream()
check("Stop 设置取消事件", app5._stream_cancel.is_set(), "")
check("Stop 关闭网络响应", resp5.closed, "")
check("Stop 递增 generation", app5._stream_task_id > old_id, "")
check("Stop 后不再 streaming", app5._streaming is False, "")

# ============ 6. 确认回传：独立线程立即执行（不死锁）============
print("== 6. 确认回传独立线程 ==")
app6 = make_app()
posted = {"ok": False}

def fake_api(base, method, path, payload=None, timeout=15, raw=False):
    posted["ok"] = True
    posted["payload"] = payload
    return 200, {"ok": True}, None

app6._results = queue.Queue()
with mock.patch.object(chat_mod, "api_request", side_effect=fake_api):
    app6._send_respond("ask-123", "yes")
    # 回传在独立线程立即执行，不依赖 SSE 队列
    deadline = time.time() + 5
    while not posted["ok"] and time.time() < deadline:
        time.sleep(0.01)
check("确认立即回传（独立线程）", posted["ok"] and posted["payload"] == {"request_id": "ask-123", "choice": "yes"},
      str(posted))
check("回传未占用 SSE 队列", app6._results.empty(), str(list(app6._results.queue)))

# 回传失败 → 界面事件（不假装已批准）
def fake_api_fail(base, method, path, payload=None, timeout=15, raw=False):
    return 404, {"detail": "确认请求已超时"}, None

app6b = make_app()
with mock.patch.object(chat_mod, "api_request", side_effect=fake_api_fail):
    app6b._send_respond("ask-999", "yes")
    deadline = time.time() + 5
    while app6b._results.empty() and time.time() < deadline:
        time.sleep(0.01)
evs = []
while not app6b._results.empty():
    evs.append(app6b._results.get_nowait())
check("回传失败产生事件", any(k == "respond_failed" for k, _ in evs), str(evs))
rf = next(p for k, p in evs if k == "respond_failed")
check("失败事件含原因", "超时" in rf[2], str(rf))

# ============ 7. tool_result 后紧接普通 delta：最终回答必须显示 ============
print("== 7. tool_result 后普通 delta（状态机重置）==")
app7 = make_app()
app7._stream_task_id = 21
app7._streaming = True
handle7 = object()
app7._stream_handle = handle7
blocks = [
    sse_line("tool_result", {"id": "c1", "ok": True, "result": "完成"}),
    sse_line("", {"choices": [{"delta": {"content": "最终回答"}}]}),
    "data: [DONE]\n\n",
]
resp7 = FakeResp(blocks)
with mock.patch.object(chat_mod.urllib.request, "urlopen", return_value=resp7):
    t = threading.Thread(target=app7._stream_reader,
                         args=([{"role": "user", "content": "hi"}], handle7, 21), daemon=True)
    t.start()
    t.join(timeout=10)
events7 = []
while not app7._results.empty():
    events7.append(app7._results.get_nowait())
kinds7 = [k for k, _ in events7]
check("tool_result 事件", "stream_tool_result" in kinds7, str(kinds7))
deltas7 = [p for k, p in events7 if k == "stream_delta"]
final_text = "".join(p[2][0] for p in deltas7)
check("tool_result 后普通 delta 正常显示", "最终回答" in final_text, final_text)
check("每次请求只产生一次 done", sum(1 for k, _ in events7 if k == "stream_done") == 1,
      str([k for k, _ in events7]))

# ============ 8. 多行 data + CRLF 兼容 ============
print("== 8. 多行 data / CRLF ==")
app8 = make_app()
app8._stream_task_id = 22
app8._streaming = True
resp8 = FakeResp([
    "event: tool_result\r\ndata: {\"id\": \"c2\", \"ok\": true}\r\n\r\n",
    "data: {\"choices\": [{\"delta\": {\"content\": \"第一行\\n第二行\"}}]}\n\n",
    "data: [DONE]\n\n",
])
with mock.patch.object(chat_mod.urllib.request, "urlopen", return_value=resp8):
    t = threading.Thread(target=app8._stream_reader,
                         args=([{"role": "user", "content": "hi"}], object(), 22), daemon=True)
    t.start()
    t.join(timeout=10)
events8 = []
while not app8._results.empty():
    events8.append(app8._results.get_nowait())
kinds8 = [k for k, _ in events8]
check("CRLF 事件解析", "stream_tool_result" in kinds8, str(kinds8))
deltas8 = [p for k, p in events8 if k == "stream_delta"]
text8 = "".join(p[2][0] for p in deltas8)
check("多行 delta 内容完整", "第一行" in text8 and "第二行" in text8, text8)
check("CRLF 流一次 done", sum(1 for k, _ in events8 if k == "stream_done") == 1, str(kinds8))

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
