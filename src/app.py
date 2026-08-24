"""
Venus Daemon — 异步解耦的桌面自动化守护进程（骨架）

核心设计
--------
1. 异步解耦：PyAutoGUI / pynput 等 GUI 操作是同步阻塞调用，全部经
   ThreadPoolExecutor(max_workers=1) 单线程池串行执行。事件循环线程上
   跑一个 worker 协程：从任务队列取指令 → 丢进线程池 → 结果写回对应的
   Future。FastAPI 主事件循环永不执行阻塞调用，同时 busy / queued 计数
   精确、止停可以精确取消"排队中"的任务。

2. Kill-Switch 三重保险：
   - FAILSAFE（物理层）：鼠标甩到屏幕四角，PyAutoGUI 自动抛出
     FailSafeException，执行中的动作立即中断，并自动进入止停状态；
   - stop_requested（逻辑层）：POST /api/v1/stop 后所有新指令被拒绝
     （423 Locked），排队中尚未执行的任务被取消，直到 POST /api/v1/reset；
   - pynput 全局热键（物理层，可选）：Ctrl+Alt+Shift+X 一键止停。

3. 客户端断连安全：任务的完成由 worker 持有，HTTP 请求断开只影响"等待
   结果"的协程，不会中断正在执行的 GUI 操作（不会留下半完成的输入状态）。

运行：python app.py            # 或 uvicorn app:app --host 127.0.0.1 --port 8000
API 文档：http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import pyautogui
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from brand import APP_VERSION, DAEMON_NAME, PRODUCT_NAME, env_is_set

# --------------------------------------------------------------------------
# 全局配置
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

# Kill-Switch #1：开启 FAILSAFE —— 鼠标移到屏幕任意角落即触发异常终止
# 注：pyautogui 在 Windows 导入时已自动调用 SetProcessDPIAware()，
#     因此 size()/screenshot() 均以物理像素为坐标基准，与前端映射一致。
pyautogui.FAILSAFE = True
# 每个 PyAutoGUI 动作之间的最小停顿，避免动作过快失控
pyautogui.PAUSE = 0.05

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("agent-daemon")

# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------
ActionType = Literal["click", "type_text", "press_key", "screenshot"]
MouseButton = Literal["left", "right", "middle"]


class ActionRequest(BaseModel):
    """POST /api/v1/execute 请求体"""

    action: ActionType
    x: int | None = Field(default=None, ge=0, description="click 的屏幕绝对横坐标")
    y: int | None = Field(default=None, ge=0, description="click 的屏幕绝对纵坐标")
    text: str | None = Field(default=None, description="type_text 要输入的文字")
    key: str | list[str] | None = Field(
        default=None,
        description="press_key 的按键，如 'enter'；组合键用 '+' 如 'ctrl+c'，或按键列表",
    )
    clicks: int = Field(default=1, ge=1, le=3, description="click 次数（1=单击 2=双击）")
    button: MouseButton = Field(default="left", description="left / right / middle")


class FailsafeTriggered(Exception):
    """PyAutoGUI FAILSAFE 已触发（鼠标进入屏幕角落）。"""


def _safe_action_log(req: ActionRequest) -> dict:
    """动作日志脱敏：只记录动作类型与元数据，绝不记录 type_text 正文等敏感内容。"""
    meta = {"action": req.action}
    if req.action == "type_text":
        meta["chars"] = len(req.text or "")          # 只记字符数，不记内容
    elif req.action == "click":
        if req.x is not None and req.y is not None:
            meta["position"] = [req.x, req.y]
        meta["clicks"] = req.clicks
        meta["button"] = req.button
    elif req.action == "press_key":
        meta["key"] = req.key
    return meta


# --------------------------------------------------------------------------
# DaemonState：任务队列 + 单线程 GUI 执行池 + Kill-Switch 状态
# --------------------------------------------------------------------------
class DaemonState:
    """守护进程全局状态。

    线程模型
    --------
    - _queue：worker 协程与 submit() 都在事件循环线程操作，但 stop 的
      排空逻辑可能被 call_soon_threadsafe 从其它线程调度进来，故用
      _qlock 保护（所有操作都是微秒级的短临界区）；
    - _stop_requested：pynput 热键回调 / FAILSAFE 来自任意线程，用 _lock；
    - _current / _last_action：只在事件循环线程读写（worker 是协程）。

    任务生命周期：submit() 入队 → worker 取队头 → run_in_executor 丢进
    单线程池执行 → 结果/异常写回 Future → 等待中的 HTTP 请求拿到结果。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._qlock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gui-worker")
        self._queue: deque[tuple[ActionRequest, asyncio.Future]] = deque()
        self._wakeup = asyncio.Event()
        self._worker_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown = False
        self._stop_requested = False
        self._current: str | None = None       # 正在执行的动作名
        self._last_action: str | None = None
        self._last_action_at: float | None = None
        self.started_at = time.time()

    # ---------- 生命周期（事件循环线程） ----------
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._worker_task = loop.create_task(self._worker_loop())

    async def stop_worker(self) -> None:
        self.cancel_queued()               # 先取消排队任务
        with self._qlock:
            self._shutdown = True
        self._wakeup.set()                 # 唤醒 worker 让它退出
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        # 正在执行的 GUI 操作无法强行打断（由 FAILSAFE 兜底），
        # cancel_futures 只清掉线程池中尚未开始的任务
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def _worker_loop(self) -> None:
        """串行消费任务队列：取一个 → 执行 → 结果写回，绝不并发。"""
        while True:
            item = await self._dequeue()
            if item is None:
                break
            req, fut = item
            self._current = req.action
            try:
                result = await asyncio.get_running_loop().run_in_executor(
                    self._executor, self._dispatch, req
                )
            except asyncio.CancelledError:
                # 进程关闭中：让等待方感知取消；线程池里的任务自然继续
                fut.cancel()
                raise
            except BaseException as exc:
                if not fut.done():
                    fut.set_exception(exc)
            else:
                if not fut.done():
                    fut.set_result(result)
            finally:
                self._current = None

    async def _dequeue(self) -> tuple[ActionRequest, asyncio.Future] | None:
        """取队头任务；shutdown 且队列空时返回 None。"""
        while True:
            with self._qlock:
                if self._queue:
                    return self._queue.popleft()
                if self._shutdown:
                    return None
                # 清除唤醒信号后必须在同一临界区内复查队列，
                # 否则会与入队的 set() 形成丢失唤醒竞争
                self._wakeup.clear()
                if self._queue:
                    continue
            await self._wakeup.wait()

    # ---------- 任务提交 / 止停 ----------
    def submit(self, req: ActionRequest) -> asyncio.Future:
        """入队一个动作指令并返回其 Future（调用方 await 它拿结果）。

        止停检查与入队在同一临界区（_qlock）：stop 与 submit 并发时，
        stop 后不可能再有新动作进入普通队列（fail closed）。
        """
        fut: asyncio.Future = self._loop.create_future()
        with self._qlock:
            if self._stop_requested:
                fut.set_exception(HTTPException(
                    status_code=423,
                    detail="Daemon 处于止停状态，拒绝新指令；请先调用 POST /api/v1/reset"))
                return fut
            self._queue.append((req, fut))
        self._wakeup.set()
        return fut

    def request_stop(self) -> None:
        """设置止停标志；可从任意线程调用（热键回调 / FAILSAFE / API）。"""
        with self._lock:
            self._stop_requested = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.cancel_queued)

    def cancel_queued(self) -> int:
        """取消排队中（尚未开始执行）的任务，返回取消个数。在事件循环线程调用。"""
        n = 0
        with self._qlock:
            items = list(self._queue)
            self._queue.clear()
        for _, fut in items:
            if not fut.done():
                fut.cancel()
                n += 1
        return n

    def request_reset(self) -> None:
        with self._lock:
            self._stop_requested = False

    @property
    def stop_requested(self) -> bool:
        with self._lock:
            return self._stop_requested

    # ---------- 状态快照 ----------
    def snapshot(self) -> dict:
        with self._qlock:
            queued = len(self._queue)
        with self._lock:
            stopped = self._stop_requested
            last_action = self._last_action
            last_action_at = self._last_action_at
        return {
            "is_busy": self._current is not None or queued > 0,
            "queued": queued,
            "current_action": self._current,
            "stop_requested": stopped,
            "last_action": last_action,
            "last_action_at": last_action_at,
        }

    # ---------- 线程池中的阻塞执行 ----------
    async def run_blocking(self, fn: Callable, *args):
        """把任意阻塞调用提交到 GUI 线程池执行（供截图等非动作类操作使用）。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    def _dispatch(self, req: ActionRequest) -> dict:
        """在 GUI 工作线程内执行阻塞操作（本方法全程不碰事件循环）。"""
        t0 = time.monotonic()
        # 日志脱敏：只记录动作类型与元数据，绝不记录 type_text 正文等敏感内容
        log.info("执行动作: %s", _safe_action_log(req))
        try:
            try:
                return HANDLERS[req.action](req)
            except pyautogui.FailSafeException:
                # Kill-Switch #1 触发：鼠标已进入屏幕角落
                self.request_stop()
                raise FailsafeTriggered() from None
        finally:
            with self._lock:
                self._last_action = req.action
                self._last_action_at = time.time()
            log.info("动作完成: %s (%.0fms)", req.action, (time.monotonic() - t0) * 1000)


# --------------------------------------------------------------------------
# 动作处理器（全部在 GUI 工作线程中运行）
# --------------------------------------------------------------------------
def _handle_click(req: ActionRequest) -> dict:
    if req.x is not None or req.y is not None:
        if req.x is None or req.y is None:
            raise HTTPException(422, "click 的 x/y 必须成对提供")
        # 越界防护：坐标来自前端缩放换算，防御异常值
        if SCREEN_WIDTH and SCREEN_HEIGHT and not (
            0 <= req.x < SCREEN_WIDTH and 0 <= req.y < SCREEN_HEIGHT
        ):
            raise HTTPException(
                422, f"坐标越界 ({req.x},{req.y})，屏幕范围 {SCREEN_WIDTH}x{SCREEN_HEIGHT}"
            )
        pyautogui.click(x=req.x, y=req.y, clicks=req.clicks, button=req.button)
    else:
        pyautogui.click(clicks=req.clicks, button=req.button)
    return {"ok": True, "action": "click", "position": [req.x, req.y], "clicks": req.clicks}


def _is_typeable(ch: str) -> bool:
    """该字符能否被 pyautogui.write() 直接敲出（可打印 ASCII，含大小写）。"""
    return 32 <= ord(ch) <= 126


def _type_runs(text: str) -> list[tuple[str, str]]:
    """把文本切分为 ('keys', ...) / ('clip', ...) 两种执行段。

    pyautogui.write() 无法输入中文 / emoji 等非 ASCII 字符，
    这些内容改走剪贴板粘贴；**连续的非 ASCII 段合并为一次粘贴**
    （避免每个汉字覆盖一次剪贴板），ASCII 段也合并减少按键切换。
    """
    runs: list[tuple[str, str]] = []
    buf: list[str] = []
    clip_buf: list[str] = []
    for ch in text:
        if _is_typeable(ch):
            if clip_buf:
                runs.append(("clip", "".join(clip_buf)))
                clip_buf = []
            buf.append(ch)
        else:
            if buf:
                runs.append(("keys", "".join(buf)))
                buf = []
            clip_buf.append(ch)
    if buf:
        runs.append(("keys", "".join(buf)))
    if clip_buf:
        runs.append(("clip", "".join(clip_buf)))
    return runs


def _snapshot_clipboard() -> dict[int, object] | None:
    """拍摄剪贴板全格式快照（在第一次写入之前调用）。

    枚举全部已注册格式并复制数据；win32clipboard 不可用时返回 None
    （调用方退回纯文本快照）。失败不抛异常——宁可走保守路径。
    """
    try:
        import win32clipboard
    except ImportError:
        return None
    try:
        win32clipboard.OpenClipboard()
        try:
            saved: dict[int, object] = {}
            fmt = 0
            while True:
                fmt = win32clipboard.EnumClipboardFormats(fmt)
                if fmt == 0:
                    break
                try:
                    saved[fmt] = win32clipboard.GetClipboardData(fmt)
                except Exception:
                    continue
            return saved
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return None


def _restore_clipboard_snapshot(saved: dict[int, object] | None, fallback_text: str) -> None:
    """恢复剪贴板快照：EmptyClipboard 后按原格式回写。

    - 快照有效：整体恢复（图片/DIB、HDROP、HTML、文本等全部格式）；
    - 快照无效：退回纯文本恢复；
    - 单格式回写失败只跳过该格式；**全部格式都失败时抛异常**，
      由调用方返回可见错误（不默默销毁用户剪贴板内容）。
    """
    if not saved:
        import pyperclip
        pyperclip.copy(fallback_text or "")
        return
    try:
        import win32clipboard
    except ImportError:
        import pyperclip
        pyperclip.copy(fallback_text or "")
        return
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            restored = 0
            for fmt, data in saved.items():
                try:
                    win32clipboard.SetClipboardData(fmt, data)
                    restored += 1
                except Exception:
                    continue
            if restored == 0 and saved:
                raise OSError("无法恢复任何剪贴板格式（原内容可能丢失）")
        finally:
            win32clipboard.CloseClipboard()
    except OSError:
        raise
    except Exception:
        import pyperclip
        pyperclip.copy(fallback_text or "")


def _type_unicode_sendinput(text: str) -> bool:
    """Windows Unicode SendInput 逐字符输入（KEYEVENTF_UNICODE）。

    优先于剪贴板路径（不覆盖/不依赖用户剪贴板）。失败返回 False，
    调用方回退到剪贴板粘贴。非 Windows 或 ctypes 不可用时恒返回 False。
    """
    if sys.platform != "win32" or not text:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        KEYEVENTF_UNICODE = 0x0004
        KEYEVENTF_KEYUP = 0x0002
        INPUT_KEYBOARD = 1

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_ulong),
            ]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("ki", KEYBDINPUT)]

        inputs: list[INPUT] = []
        for ch in text:
            code = ord(ch)
            if code > 0xFFFF:
                return False  # 代理对超 SendInputW 单字符范围，回退剪贴板
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                inputs.append(INPUT(INPUT_KEYBOARD, KEYBDINPUT(0, code, flags, 0, 0)))
        if not inputs:
            return False
        arr = (INPUT * len(inputs))(*inputs)
        sent = ctypes.windll.user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        return sent == len(inputs)
    except Exception:
        return False


def _paste_via_clipboard(text: str) -> None:
    """非 ASCII 文本经剪贴板粘贴（ctrl+v），完成后还原用户原剪贴板。

    - 第一次写入前拍摄全格式快照（含图片/HTML/文件列表），
      输入结束（含失败/异常）在最外层 finally 中恢复一次；
    - 快照不可用（win32clipboard 缺失）时退回文本快照；
    - 恢复失败时给出明确错误，不静默吞掉。
    """
    import pyperclip  # pyautogui 的固有依赖

    snapshot = _snapshot_clipboard()
    previous_text = pyperclip.paste()
    restore_error = None
    try:
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
    except Exception as exc:
        raise HTTPException(500, f"非 ASCII 文本输入失败（剪贴板不可用）: {exc}") from exc
    finally:
        # 无条件恢复一次：优先全格式快照，其次文本
        try:
            if snapshot is not None:
                _restore_clipboard_snapshot(snapshot, previous_text)
            else:
                pyperclip.copy(previous_text or "")
        except Exception as exc:
            restore_error = exc
    if restore_error is not None:
        raise HTTPException(500, f"剪贴板恢复失败（原内容可能丢失）: {restore_error}")


def _restore_clipboard(text: str) -> None:
    """兼容入口：恢复剪贴板为纯文本（保留旧 API）。"""
    import pyperclip
    pyperclip.copy(text or "")


def _handle_type_text(req: ActionRequest) -> dict:
    if not req.text:
        raise HTTPException(422, "type_text 需要提供 text 参数")
    # write() 无法输入换行符：按行拆分，行间以 Enter 键衔接
    for i, line in enumerate(req.text.split("\n")):
        if i > 0:
            pyautogui.press("enter")
        for kind, content in _type_runs(line):
            if kind == "keys":
                pyautogui.write(content, interval=0.02)
            else:
                # SendInput 优先（不触碰用户剪贴板）；失败回退剪贴板粘贴
                if not _type_unicode_sendinput(content):
                    _paste_via_clipboard(content)
    return {"ok": True, "action": "type_text", "chars": len(req.text)}


def _handle_press_key(req: ActionRequest) -> dict:
    key = req.key
    if key is None:
        raise HTTPException(422, "press_key 需要提供 key 参数")
    if isinstance(key, str) and "+" in key:
        # 组合键，如 "ctrl+c" / "alt+tab"
        parts = [k.strip().lower() for k in key.split("+")]
        _validate_keys(parts)
        pyautogui.hotkey(*parts)
    else:
        keys = [key] if isinstance(key, str) else key
        _validate_keys(keys)
        pyautogui.press(*keys)
    return {"ok": True, "action": "press_key", "key": key}


def _validate_keys(keys: list[str]) -> None:
    for k in keys:
        if k.lower() not in pyautogui.KEYBOARD_KEYS:
            raise HTTPException(
                422,
                f"未知按键: {k!r}（可用键列表见 pyautogui.KEYBOARD_KEYS）",
            )


def _handle_screenshot(req: ActionRequest) -> dict:
    img = pyautogui.screenshot()
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return {
        "ok": True,
        "action": "screenshot",
        "format": "jpeg",
        "size": [img.width, img.height],
        "screenshot_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
    }


HANDLERS: dict[str, Callable[[ActionRequest], dict]] = {
    "click": _handle_click,
    "type_text": _handle_type_text,
    "press_key": _handle_press_key,
    "screenshot": _handle_screenshot,
}

# --------------------------------------------------------------------------
# 可选：pynput 全局热键（Ctrl+Alt+Shift+X）一键紧急止停
# --------------------------------------------------------------------------
def _start_hotkey_listener(state: DaemonState):
    """注册全局止停热键；pynput 未安装或环境不支持时静默降级。"""
    try:
        from pynput import keyboard

        listener = keyboard.GlobalHotKeys({"<ctrl>+<alt>+<shift>+x": state.request_stop})
        listener.start()
        log.info("已注册紧急止停热键: Ctrl+Alt+Shift+X")
        return listener
    except Exception as exc:  # ImportError / 无桌面会话等
        log.warning("紧急止停热键不可用（可忽略）: %s", exc)
        return None


# --------------------------------------------------------------------------
# FastAPI 应用
# --------------------------------------------------------------------------
state = DaemonState()
SCREEN_WIDTH = 0
SCREEN_HEIGHT = 0
SCREEN_OK = False       # 启动自检：当前会话能否访问屏幕（截图）
hotkey_listener = None
AUTH_TOKEN = ""         # --token 启用：屏幕控制端点须带 X-Api-Token（常量时间比较）
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _parse_host_header(host: str) -> str | None:
    """解析 Host 头为规范 hostname；非法格式（userinfo/多余冒号/坏端口）返回 None。

    支持：127.0.0.1、localhost、[::1]、上述形式携带合法数字端口。
    """
    host = (host or "").strip()
    if not host:
        return None
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return None
        inner = host[1:end]
        rest = host[end + 1:]
        if rest:
            if not rest.startswith(":") or not rest[1:].isdigit():
                return None
        return inner.lower()
    if "@" in host or host.count(":") > 1:
        return None
    if ":" in host:
        hostname, _, port = host.partition(":")
        if not port.isdigit():
            return None
        return hostname.lower()
    return host.lower()


def _token_ok(request) -> bool:
    if not AUTH_TOKEN:
        return True
    import hmac
    return hmac.compare_digest(request.headers.get("X-Api-Token", ""), AUTH_TOKEN)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global SCREEN_WIDTH, SCREEN_HEIGHT, SCREEN_OK, hotkey_listener
    try:
        SCREEN_WIDTH, SCREEN_HEIGHT = pyautogui.size()
        pyautogui.screenshot()          # 屏幕权限自检（桌面会话可用性）
        SCREEN_OK = True
    except Exception as exc:
        # 无桌面会话（SSH / 服务方式运行）时截图与输入都会失败
        log.warning("屏幕访问不可用（需在交互式桌面会话中运行）: %s", exc)
    state.start(asyncio.get_running_loop())
    log.info(
        "Daemon 启动 | 屏幕 %dx%d | screen_access=%s | FAILSAFE=%s | GUI 线程池 max_workers=1",
        SCREEN_WIDTH, SCREEN_HEIGHT, SCREEN_OK, pyautogui.FAILSAFE,
    )
    hotkey_listener = _start_hotkey_listener(state)
    yield
    if hotkey_listener is not None:
        hotkey_listener.stop()
    await state.stop_worker()
    log.info("Daemon 已退出")


app = FastAPI(
    title=DAEMON_NAME,
    version=APP_VERSION,
    description="异步解耦的桌面自动化守护进程：网页控制电脑（点击 / 输入 / 按键 / 截图）",
    lifespan=lifespan,
)


@app.middleware("http")
async def host_guard(request, call_next):
    """Host/Origin 限制：防 DNS rebinding（恶意域名解析到本机）与跨站请求。"""
    origin = request.headers.get("origin")
    if origin:
        # URL 解析后精确比较 hostname（拒绝 http://localhost.evil.example 等欺骗）
        from urllib.parse import urlparse as _up
        try:
            o = _up(origin)
            ohost = (o.hostname or "").lower()
        except Exception:
            ohost = ""
        if ohost not in _LOOPBACK_HOSTS or o.scheme not in ("http", "https"):
            return Response(status_code=403, content="非法 Origin（仅允许本机来源）")
    host = request.headers.get("host", "")
    hostname = _parse_host_header(host)
    if hostname is None or hostname not in _LOOPBACK_HOSTS:
        # 测试环境（TestClient 默认 testserver）白名单：不放开任意 Host
        if env_is_set(("VENUS_ALLOW_TEST_HOST", "PCAGENT_ALLOW_TEST_HOST")) \
                and hostname in ("testserver", "testclient", "localhost"):
            return await call_next(request)
        return Response(status_code=403, content="非法 Host（仅允许本机回环访问）")
    return await call_next(request)


@app.middleware("http")
async def auth_middleware(request, call_next):
    """屏幕控制端点（execute/stop/reset）必须鉴权；查询类（status/screenshot/静态页）免认证。"""
    if request.url.path in ("/api/v1/execute", "/api/v1/stop", "/api/v1/reset"):
        if not _token_ok(request):
            return Response(status_code=401,
                            content="未授权：需要正确的 X-Api-Token（daemon 已启用 token 鉴权）")
    return await call_next(request)


# 静态前端控制台
app.mount("/static", StaticFiles(directory=BASE_DIR.parent / "static"), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(BASE_DIR.parent / "static" / "index.html")


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
@app.post("/api/v1/execute", summary="执行动作指令")
async def execute(req: ActionRequest) -> dict:
    """执行 click / type_text / press_key / screenshot。

    止停状态下直接拒绝（423）；动作经队列在后台单线程池串行执行，
    本接口等待结果但不阻塞事件循环。
    """
    if state.stop_requested:
        raise HTTPException(
            status_code=423,
            detail="Daemon 处于止停状态，拒绝新指令；请先调用 POST /api/v1/reset",
        )
    fut = state.submit(req)
    try:
        return await fut
    except FailsafeTriggered:
        raise HTTPException(
            status_code=409,
            detail="PyAutoGUI FAILSAFE 触发（鼠标进入屏幕角落），已自动进入止停状态",
        ) from None
    except asyncio.CancelledError:
        if fut.cancelled():
            # 止停取消了排队中的任务
            raise HTTPException(status_code=409, detail="任务已被紧急止停取消") from None
        raise  # 客户端断开：任务继续在后台执行，不产生半完成的输入状态
    except HTTPException:
        raise
    except Exception as exc:  # 工作线程内的未知异常兜底
        log.exception("动作执行失败")
        raise HTTPException(status_code=500, detail=f"动作执行失败: {exc}") from exc


@app.get("/api/v1/status", summary="Daemon 状态")
async def get_status() -> dict:
    snap = state.snapshot()
    mode = "stopped" if snap["stop_requested"] else ("busy" if snap["is_busy"] else "idle")
    return {
        "ok": True,
        "daemon": "running",
        "mode": mode,
        "is_busy": snap["is_busy"],
        "queued": snap["queued"],                      # 排队中尚未执行的任务数
        "current_action": snap["current_action"],      # 正在执行的动作名
        "stop_requested": snap["stop_requested"],
        "screen_size": {"width": SCREEN_WIDTH, "height": SCREEN_HEIGHT},
        "screen_access": SCREEN_OK,            # 当前会话能否访问屏幕（截图/输入）
        "fail_safe": pyautogui.FAILSAFE,
        "last_action": snap["last_action"],
        "last_action_at": snap["last_action_at"],
        "uptime": round(time.time() - state.started_at, 1),
    }


@app.post("/api/v1/stop", summary="紧急止停")
async def stop_daemon() -> dict:
    """紧急止停：设置全局止停标志 + 取消所有排队中的任务。

    正在执行中的动作无法从外部打断——由 FAILSAFE（甩鼠标到角落）
    或自然结束兜底。
    """
    state.request_stop()
    n = state.cancel_queued()  # 端点就在事件循环线程，直接清队列；
                               # request_stop 调度的那次排空会是空操作
    log.warning("紧急止停: 已取消 %d 个排队任务", n)
    return {
        "ok": True,
        "stop_requested": True,
        "canceled_pending": n,
        "note": "正在执行的 GUI 操作将自然结束或由 FAILSAFE 终止",
    }


@app.post("/api/v1/reset", summary="恢复正常运行")
async def reset_daemon() -> dict:
    state.request_reset()
    log.info("已恢复正常运行状态")
    return {"ok": True, "stop_requested": False}


@app.get("/api/v1/screenshot", summary="获取屏幕截图（image/jpeg 字节流）")
async def screenshot() -> Response:
    """返回当前屏幕的 JPEG 字节流，前端 <img src> 可直接引用。

    截图同样经线程池执行，不阻塞事件循环；止停状态下依然可用（被动观察）。
    """
    img = await state.run_blocking(pyautogui.screenshot)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return Response(
        content=buf.getvalue(),
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Screen-Width": str(img.width),
            "X-Screen-Height": str(img.height),
        },
    )


def _write_pid_metadata(pid_file: Path) -> None:
    """写入 PID metadata JSON（原子替换）：pid/启动时间/可执行文件/nonce。

    停止脚本据此校验进程身份（PID 复用/过期文件不误杀无关进程）。
    """
    import json
    import time as _time
    payload = {
        "pid": os.getpid(),
        "started": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "executable": sys.executable,
        "nonce": os.urandom(8).hex(),
        "created": _time.time(),
    }
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = pid_file.with_suffix(".pid.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(pid_file)


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description=DAEMON_NAME)
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址（保持 127.0.0.1 防止局域网内他人控制）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    parser.add_argument("--token", default="",
                        help="启用 token 鉴权（屏幕控制端点须带 X-Api-Token；绑定非回环地址时必填）")
    parser.add_argument("--pid-file", default="",
                        help="写入 PID metadata JSON 路径（停止脚本据此校验进程身份，不盲目杀端口占用者）")
    args = parser.parse_args()
    # 安全要求：绑定非回环地址时必须提供 token（否则拒绝启动）
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
        print(f"错误：绑定非回环地址（{args.host}）时必须提供 --token 鉴权。\n"
              f"请加 --token <随机字符串> 后重试，或改回 127.0.0.1。")
        sys.exit(1)
    AUTH_TOKEN = args.token
    if args.token:
        log.warning("token 鉴权已启用：屏幕控制端点需携带 X-Api-Token 头")
    pid_file = Path(args.pid_file) if args.pid_file else None
    if pid_file is not None:
        try:
            _write_pid_metadata(pid_file)
        except OSError as exc:
            print(f"警告：无法写入 PID 文件：{exc}")
    try:
        uvicorn.run(app, host=args.host, port=args.port, reload=args.reload, log_level="info")
    finally:
        if pid_file is not None:
            try:
                pid_file.unlink(missing_ok=True)
            except OSError:
                pass
