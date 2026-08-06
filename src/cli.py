#!/usr/bin/env python3
"""
PC Agent CLI — 在 Linux 虚拟机 / 任意终端使用 PC Agent（零依赖，纯标准库）

通过 HTTP 连接 Windows 主机上运行的 llm_server（:8001）：
  - Agent 对话：模型可调用工具（click/type_text/press_key/…）操作主机屏幕
  - 流式输出（打字机效果）+ 工具调用实时显示（ANSI 彩色，自动检测 TTY）
  - 多会话管理（新建/切换/清空）
  - Ctrl+C 中止当前任务；/quit 退出
  - --once 单次模式（脚本/自动化调用）

用法：
  python cli.py                                  # 读取 ~/.pcagent.json 或默认 127.0.0.1:8001
  python cli.py --host 192.168.1.10 --port 8001 --token abc123
  python cli.py --once "屏幕分辨率是多少"        # 单次模式
  python cli.py /config host=192.168.1.10 token=abc123   # 保存连接配置

交互命令：
  /help       显示帮助          /new        新会话
  /sessions   列出会话          /switch N   切换会话
  /clear      清空当前会话历史   /quit       退出
  其他输入                      发送给 Agent
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

CONFIG_PATH = Path.home() / ".pcagent.json"

# ---- 上下文容量与压缩 ----
COMPRESS_THRESHOLD = 0.6      # 估算用量达到窗口的 60% 时触发压缩
KEEP_RECENT = 8               # 压缩时保留最近 N 条消息
_TOKEN_FACTOR = 0.8           # 字符 → token 粗略换算（中文场景保守系数）
_MSG_OVERHEAD = 8             # 每条消息的固定 token 开销（role/标记等）


def estimate_tokens(messages: List[dict]) -> int:
    """估算消息列表的 token 用量（用于容量判断，非精确值）。"""
    total = 0
    for m in messages:
        content = m.get("content") or ""
        total += int(len(content) * _TOKEN_FACTOR) + _MSG_OVERHEAD
        for tc in m.get("tool_calls") or []:
            total += len(tc.get("function", {}).get("arguments") or "") // 2 + 20
    return total

# ---- ANSI 颜色（非 TTY 自动禁用）----
_ANSI = {"reset": "\033[0m", "dim": "\033[2m", "green": "\033[32m", "yellow": "\033[33m",
         "red": "\033[31m", "blue": "\033[34m", "cyan": "\033[36m", "white": "\033[97m",
         "bold": "\033[1m"}


def color(text: str, code: str = "reset") -> str:
    if not sys.stdout.isatty():
        return text
    return f"{_ANSI[code]}{text}{_ANSI['reset']}"


# ======================================================================
# 方向键选择菜单（跨平台：Windows msvcrt / Linux termios，零依赖）
# ======================================================================
def _read_key() -> Optional[str]:
    """读取单个按键。返回 'up'/'down'/'enter'/'esc'/普通字符；非交互环境返回 None。"""
    try:
        if sys.platform == "win32":
            import msvcrt
            if not msvcrt.kbhit():
                return None
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):            # 功能键前缀（方向键等）
                ch2 = msvcrt.getwch()
                return {"H": "up", "P": "down"}.get(ch2, "enter")
            if ch in ("\r", "\n"):
                return "enter"
            if ch == "\x1b":
                return "esc"
            return ch.lower()
        else:
            import termios
            import tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ch = sys.stdin.read(1)
                if ch == "\x1b":
                    seq = sys.stdin.read(2)
                    if seq == "[A":
                        return "up"
                    if seq == "[B":
                        return "down"
                    return "esc"
                if ch in ("\r", "\n"):
                    return "enter"
                return ch.lower()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        return None


def select_menu(title: str, options: List[str], current: int = 0) -> Optional[int]:
    """方向键选择菜单：↑/↓ 移动，Enter 确认，q/Esc 取消。
    非交互环境（管道/重定向）直接打印列表并返回 None。"""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(color(title, "bold"))
        for i, opt in enumerate(options):
            print(color(f"  {'▶' if i == current else ' '} {opt}",
                        "bold" if i == current else "reset"))
        return None
    if sys.platform == "win32":
        os.system("")   # 启用 Windows 控制台 ANSI 转义序列（Win10+）
    n = len(options)
    sel = max(0, min(current, n - 1))
    print()
    print(color(title, "bold"))
    for i, opt in enumerate(options):
        print(color(f"  {'▶' if i == sel else ' '} {opt}", "bold" if i == sel else "reset"))
    hint = color("  ↑/↓ 选择 · Enter 确认 · q/Esc 取消", "dim")
    print(hint)

    def draw() -> None:
        # 光标在 hint 行后，上移 n+1 行到第一个选项行，重写全部选项 + hint
        sys.stdout.write(f"\033[{n + 1}A")
        for i in range(n):
            sys.stdout.write("\033[2K" + color(f"  {'▶' if i == sel else ' '} {options[i]}",
                                               "bold" if i == sel else "reset") + "\n")
        sys.stdout.write("\033[2K" + hint + "\n")
        sys.stdout.flush()

    def cleanup() -> None:
        # 上移 n+2 行（标题+选项+hint），逐行清空，光标复位
        sys.stdout.write(f"\033[{n + 2}A")
        for _ in range(n + 2):
            sys.stdout.write("\033[2K\033[1B")
        sys.stdout.write("\033[2K\r")
        sys.stdout.flush()

    while True:
        key = _read_key()
        if key == "up":
            sel = (sel - 1) % n
            draw()
        elif key == "down":
            sel = (sel + 1) % n
            draw()
        elif key == "enter":
            cleanup()
            return sel
        elif key in ("esc", "q"):
            cleanup()
            return None
        # 其他按键忽略（保持菜单等待）


def _setup_utf8_stdio() -> None:
    """强制 stdio 使用 UTF-8（errors=replace 兜底），防止 WSL 终端
    中文输入（GBK 字节流）导致 UnicodeDecodeError 崩溃。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ======================================================================
# PC Agent 标准启动横幅（figlet 风格 ASCII 大字）
# ======================================================================
PC_AGENT_LOGO = [
    " ██████╗ ██████╗     █████╗  ██████╗ ███████╗███╗   ██╗████████╗",
    "██╔════╝██╔════╝    ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝",
    "██║     ██║         ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ",
    "██║     ██║         ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ",
    "╚██████╗╚██████╗    ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ",
    " ╚═════╝ ╚═════╝    ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝   ",
]


def print_banner() -> None:
    """打印 PC Agent 标准横幅（非 TTY 时跳过，避免污染管道输出）。"""
    if not sys.stdout.isatty():
        return
    for line in PC_AGENT_LOGO:
        print(color(line, "cyan"))
    print(color("  PC Agent — 桌面智能体 · 通过指令控制电脑", "bold"))
    print()


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ======================================================================
# 客户端
# ======================================================================
class AgentClient:
    def __init__(self, host: str, port: int, token: str = ""):
        self.base = f"http://{host}:{port}"
        self.token = token

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["X-Api-Token"] = self.token
        return h

    def api(self, method: str, path: str, payload=None, timeout: float = 8):
        """通用 JSON 请求，返回 (status, dict)。失败时 status=0。"""
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            except Exception:
                return e.code, {"detail": f"HTTP {e.code}"}
        except Exception:
            return 0, {}

    def health(self):
        """返回 (ok, 信息文本)。"""
        req = urllib.request.Request(self.base + "/api/v1/health", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
                return True, data
        except urllib.error.HTTPError as e:
            try:
                return False, json.loads(e.read().decode("utf-8")).get("detail", f"HTTP {e.code}")
            except Exception:
                return False, f"HTTP {e.code}"
        except Exception as e:
            return False, f"无法连接 {self.base}：{e}"

    def get_stats(self):
        """获取 Token 用量统计（缓存命中率）。"""
        req = urllib.request.Request(self.base + "/api/v1/stats", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"ok": False, "detail": f"HTTP {e.code}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def compress(self, messages: List[dict], keep_recent: int = 8):
        """调用 llm_server 压缩上下文（模型摘要早期对话）。"""
        body = json.dumps({"messages": messages, "keep_recent": keep_recent}).encode("utf-8")
        req = urllib.request.Request(self.base + "/api/v1/compress", data=body,
                                     headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "detail": f"HTTP {e.code}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def get_config(self):
        """查看 API 配置（Key 脱敏）。"""
        req = urllib.request.Request(self.base + "/api/v1/config", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"ok": False, "detail": f"HTTP {e.code}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def get_confirm_mode(self):
        req = urllib.request.Request(self.base + "/api/v1/confirm-mode", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"ok": False, "detail": f"HTTP {e.code}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def set_confirm_mode(self, mode: str):
        body = json.dumps({"mode": mode}).encode("utf-8")
        req = urllib.request.Request(self.base + "/api/v1/confirm-mode", data=body,
                                     headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "detail": f"HTTP {e.code}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def update_config(self, fields: dict):
        """更新 API 配置（api_url/api_key/model/context_window），实时生效。"""
        body = json.dumps(fields).encode("utf-8")
        req = urllib.request.Request(self.base + "/api/v1/config", data=body,
                                     headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "detail": f"HTTP {e.code}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def _send_respond(self, request_id: str, choice: str) -> None:
        """把用户的确认选择发回 llm_server。"""
        body = json.dumps({"request_id": request_id, "choice": choice}).encode("utf-8")
        req = urllib.request.Request(self.base + "/api/v1/agent/respond", data=body,
                                     headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception:
            pass  # 响应失败时 llm_server 侧会超时默认拒绝，安全兜底

    def stream_chat(self, messages: List[dict], on_event) -> Optional[str]:
        """阻塞读取 SSE 流；on_event(kind, payload) 回调。
        ask 事件：回调返回用户选择（str），随后自动发回 llm_server。
        返回错误信息或 None。

        kind: delta(str) / tool_call(dict) / tool_result(dict) / ask(dict)
        """
        body = json.dumps({"messages": messages, "agent": True}).encode("utf-8")
        req = urllib.request.Request(self.base + "/api/v1/chat/stream", data=body,
                                     headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=None) as resp:
                event = ""
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("event:"):
                        event = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if event == "error":
                        event = ""
                        try:
                            return json.loads(payload).get("detail", payload)
                        except Exception:
                            return payload
                    if event == "ask":
                        event = ""
                        data = _try_json(payload)
                        if "id" in data:
                            choice = on_event("ask", data) or "no"
                            self._send_respond(data["id"], choice)
                        continue
                    if event == "tool_call":
                        event = ""
                        on_event("tool_call", _try_json(payload))
                        continue
                    if event == "tool_result":
                        event = ""
                        on_event("tool_result", _try_json(payload))
                        continue
                    if event == "todo_update":
                        event = ""
                        on_event("todo_update", _try_json(payload))
                        continue
                    if payload == "[DONE]":
                        return None
                    try:
                        d = json.loads(payload)
                        delta = (d.get("choices") or [{}])[0].get("delta") or {}
                        content = delta.get("content") or ""
                        if content:
                            on_event("delta", content)
                    except Exception:
                        pass
                return None
        except KeyboardInterrupt:
            return "已由用户中止（Ctrl+C）"
        except Exception as e:
            return f"连接失败：{e}"


def _try_json(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        return {"raw": text}


# ======================================================================
# CLI
# ======================================================================
class Cli:
    def __init__(self, host: str, port: int, token: str = ""):
        self.client = AgentClient(host, port, token)
        self.sessions: Dict[int, dict] = {}
        self.current = 0
        self._last_content = ""
        self._compress_log: List[str] = []
        self.context_window = 65536
        self.available_tools: List[str] = []
        ok, info = self.client.health()
        if ok:
            self.context_window = info.get("context_window") or 65536
            self.available_tools = info.get("tools") or []
        self.restore_sessions()

    # ---- 会话 ----
    def restore_sessions(self) -> None:
        """启动时从后端恢复持久化会话（权威源）；失败降级为本地新建。"""
        status, data = self.client.api("GET", "/api/v1/sessions")
        if status == 200 and isinstance(data, dict):
            for s in data.get("sessions") or []:
                sid = int(s["id"])
                self.sessions[sid] = {"messages": [dict(m) for m in (s.get("messages") or [])],
                                      "title": s.get("title") or ""}
            if self.sessions:
                self.current = max(self.sessions)
                n = len(self.sessions[self.current]["messages"]) // 2
                print(color(f"已恢复 {len(self.sessions)} 个持久化会话"
                            f"（当前 会话 #{self.current}，{n} 轮）", "dim"))
                return
        self.new_session()

    def new_session(self) -> None:
        """新建会话（后端登记持久化；失败降级本地自增）。"""
        status, data = self.client.api("POST", "/api/v1/sessions")
        if status == 200 and isinstance(data.get("id"), int):
            sid = data["id"]
        else:
            sid = (max(self.sessions) + 1) if self.sessions else 1
        self.sessions[sid] = {"messages": [], "title": ""}
        self.switch(sid)

    def switch(self, sid: int) -> None:
        if sid not in self.sessions:
            print(color(f"✗ 会话 #{sid} 不存在", "red"))
            return
        self.current = sid
        n = len(self.sessions[sid]["messages"]) // 2
        print(color(f"已切换到 会话 #{sid}（{n} 轮对话）", "cyan"))

    def clear(self) -> None:
        self.sessions[self.current]["messages"] = []
        # 同步清空后端持久化（失败静默，重启会恢复——属降级行为）
        self.client.api("DELETE", f"/api/v1/sessions/{self.current}/messages", timeout=5)
        print(color("已清空当前会话历史", "cyan"))

    # ---- 事件渲染 ----
    def on_event(self, kind: str, payload) -> Optional[str]:
        if kind == "tool_call":
            try:
                args = json.loads(payload.get("arguments") or "{}")
                arg_str = ", ".join(f"{k}={v}" for k, v in args.items()) or "—"
            except Exception:
                arg_str = str(payload.get("arguments", "—"))
            step = payload.get("step")
            step_str = f"（轮次 {step}/{payload.get('max_steps')}）" if step else ""
            print(color(f"  ⚙ [{payload.get('name')}] {arg_str} {step_str}", "blue"), flush=True)
        elif kind == "tool_result":
            ok = payload.get("ok")
            result = (payload.get("result") or "")[:120]
            print(color(f"    {'✓' if ok else '✗'} {result}", "green" if ok else "red"), flush=True)
        elif kind == "delta":
            self._last_content += payload
            print(payload, end="", flush=True)
        elif kind == "ask":
            # 敏感操作确认：显示问题、diff（如有）与选项，等待用户输入，返回选择
            try:
                args = json.loads(payload.get("arguments") or "{}")
                arg_str = ", ".join(f"{k}={v}" for k, v in args.items()) or ""
            except Exception:
                arg_str = str(payload.get("arguments", ""))
            print()
            print(color(f"  ❓ {payload.get('question', '需要确认')}", "yellow"))
            if arg_str:
                print(color(f"     {arg_str}", "dim"))
            diff = payload.get("diff")
            if diff:
                print(color("  ── diff ────────────────────────────", "dim"))
                for dl in diff.splitlines()[:60]:
                    c = "green" if dl.startswith("+") else ("red" if dl.startswith("-") else "dim")
                    print(color(f"  {dl[:200]}", c))
                print(color("  ────────────────────────────────────", "dim"))
            options = payload.get("options") or ["yes", "no"]
            for i, opt in enumerate(options, 1):
                print(color(f"     [{i}] {opt}", "dim"))
            try:
                line = input(color("  选择 (数字 / yes / no，直接回车=拒绝): ", "bold")).strip()
            except (EOFError, KeyboardInterrupt):
                return "no"
            if line.isdigit() and 1 <= int(line) <= len(options):
                return options[int(line) - 1]
            if line.lower() in ("y", "yes", "是", "确认", "同意", "ok"):
                return "yes"
            return "no"
        elif kind == "todo_update":
            todos = (payload or {}).get("todos") or []
            if not todos:
                return None
            print()
            print(color("  ☑ 任务清单", "cyan"))
            for t in todos[-10:]:
                st = t.get("status", "pending")
                c = {"pending": "yellow", "in_progress": "cyan",
                     "completed": "green", "failed": "red", "cancelled": "dim"}.get(st, "yellow")
                print(color(f"    [{st}] #{t.get('id')} {t.get('title')}", c))
            return None

    # ---- 发送 ----
    def send(self, text: str) -> None:
        sess = self.sessions[self.current]
        sess["messages"].append({"role": "user", "content": text})
        snapshot = list(sess["messages"])
        # 上下文容量检查：达到阈值先压缩再发送
        est = estimate_tokens(snapshot)
        if est > self.context_window * COMPRESS_THRESHOLD:
            print(color(f"  ◇ 上下文估算 {est:,} tokens / {self.context_window:,}（{est * 100 // self.context_window}%）"
                        f"——正在压缩早期对话…", "yellow"))
            r = self.client.compress(snapshot, keep_recent=KEEP_RECENT)
            if r.get("compressed"):
                sess["messages"] = r["messages"]
                snapshot = list(sess["messages"])
                st = r.get("stats", {})
                print(color(f"  ◇ 压缩完成：{st.get('before_messages')} 条 → {st.get('after_messages')} 条"
                            f"，节省 {st.get('saved_chars', 0):,} 字符", "green"))
                self._compress_log.append(r.get("summary", ""))
            else:
                print(color(f"  ◇ 压缩未执行：{r.get('stats', {}).get('reason', r.get('detail', '?'))}", "yellow"))
        print(color("  ◌ 思考中…", "dim"))
        err = self.client.stream_chat(snapshot, self.on_event)
        print()
        # 持久化：user 消息必存；回复成功再存 assistant
        persist = [{"role": "user", "content": text}]
        if err:
            print(color(f"✗ {err}", "red"))
        else:
            # 完整回复已通过 delta 打印；回存 assistant 消息供多轮上下文
            sess["messages"].append({"role": "assistant", "content": self._last_content})
            persist.append({"role": "assistant", "content": self._last_content})
        self.client.api("POST", f"/api/v1/sessions/{self.current}/messages",
                        {"messages": persist}, timeout=5)

    # ---- 交互 ----
    def repl(self) -> None:
        while True:
            try:
                line = input(color("\n>>> ", "bold")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                print(color("再见！", "cyan"))
                return
            if not line:
                continue
            if line.startswith("/"):
                if self.handle_command(line):
                    return
            else:
                self.send(line)

    def handle_command(self, line: str) -> bool:
        parts = line.split()
        cmd = parts[0].lower()
        if cmd == "/quit" or cmd == "/exit":
            return True
        if cmd == "/help":
            self.print_help()
        elif cmd == "/new":
            self.new_session()
        elif cmd == "/sessions":
            for sid in sorted(self.sessions):
                n = len(self.sessions[sid]["messages"]) // 2
                title = self.sessions[sid].get("title") or ""
                mark = "→" if sid == self.current else " "
                label = f"会话 #{sid}" + (f"：{title}" if title else "")
                print(color(f"  {mark} {label}（{n} 轮）",
                            "bold" if sid == self.current else "reset"))
        elif cmd == "/switch":
            if len(parts) < 2 or not parts[1].isdigit():
                print(color("用法：/switch <会话号>", "yellow"))
            else:
                self.switch(int(parts[1]))
        elif cmd == "/clear":
            self.clear()
        elif cmd == "/stats":
            self.show_stats()
        elif cmd == "/status":
            self.show_status()
        elif cmd == "/apiconfig":
            self.handle_apiconfig(parts[1:])
        elif cmd == "/model":
            self.handle_model(parts[1:])
        elif cmd == "/confirm-mode":
            self.handle_confirm_mode(parts[1:])
        elif cmd == "/config":
            self.handle_config(parts[1:])
        else:
            print(color(f"未知命令：{cmd}（/help 查看帮助）", "yellow"))
        return False

    def show_stats(self) -> None:
        """显示 Token 用量统计与缓存命中率。"""
        s = self.client.get_stats()
        if not s.get("ok"):
            print(color(f"✗ 获取统计失败：{s.get('detail', '?')}", "red"))
            return
        print(color("=== Token 用量统计 ===", "bold"))
        print(color(f"  调用次数:        {s['calls']}", "reset"))
        print(color(f"  Prompt tokens:   {s['prompt_tokens']:,}", "reset"))
        print(color(f"  缓存命中 tokens: {s['cached_tokens']:,}", "green"))
        print(color(f"  缓存未命中:      {s['prompt_tokens'] - s['cached_tokens']:,}", "yellow"))
        print(color(f"  Completion:      {s['completion_tokens']:,}（推理 {s['reasoning_tokens']:,}）", "reset"))
        pct = s.get("cache_hit_rate_pct", 0)
        color_code = "green" if pct >= 50 else ("yellow" if pct >= 20 else "red")
        print(color(f"  缓存命中率:      {pct}%", color_code))
        if s["calls"] == 0:
            print(color("  （尚无调用——发送一条消息后再次查看）", "dim"))

    def show_status(self) -> None:
        """上下文容量状态：窗口 / 当前估算用量 / 剩余 / 压缩记录。"""
        sess = self.sessions[self.current]
        est = estimate_tokens(sess["messages"])
        n = len(sess["messages"])
        used_pct = est * 100 // self.context_window
        left = max(self.context_window - est, 0)
        pct_color = "green" if used_pct < 60 else ("yellow" if used_pct < 85 else "red")
        print(color("=== 上下文容量 ===", "bold"))
        print(color(f"  模型窗口:      {self.context_window:,} tokens", "reset"))
        print(color(f"  当前用量(估):  {est:,} tokens（{n} 条消息）", "reset"))
        print(color(f"  占用率:        {used_pct}%", pct_color))
        print(color(f"  剩余:          {left:,} tokens", "reset"))
        print(color(f"  压缩阈值:      {int(self.context_window * COMPRESS_THRESHOLD):,}"
                    f"（达到即自动压缩）", "dim"))
        if self._compress_log:
            print(color(f"  已压缩 {len(self._compress_log)} 次，最近摘要:", "dim"))
            print(color(f"    {self._compress_log[-1][:120]}", "cyan"))
        else:
            print(color("  尚未发生压缩", "dim"))

    # ---- API 配置与模型切换 ----
    _FIELD_ALIAS = {"url": "api_url", "key": "api_key"}

    def _print_config(self, cfg: dict) -> None:
        c = cfg.get("config", {})
        print(color("=== API 配置 ===", "bold"))
        print(color(f"  API URL:  {c.get('api_url') or '（未设置）'}", "reset"))
        print(color(f"  API Key:  {c.get('api_key') or '（未设置）'}", "reset"))
        print(color(f"  Model:    {c.get('model') or '（未设置）'}", "reset"))
        print(color(f"  上下文窗口: {c.get('context_window', '?')} tokens", "reset"))

    def handle_apiconfig(self, args: List[str]) -> None:
        """设置 API：/apiconfig url=... key=... model=...（无参数时查看当前配置）"""
        if not args:
            cfg = self.client.get_config()
            if cfg.get("ok"):
                self._print_config(cfg)
            else:
                print(color(f"✗ 获取配置失败：{cfg.get('detail', '?')}", "red"))
            print(color("  用法: /apiconfig url=<API地址> key=<Key> model=<模型>", "dim"))
            return
        fields = {}
        for a in args:
            if "=" not in a:
                print(color(f"✗ 参数格式错误：{a}（应为 key=value）", "red"))
                return
            k, v = a.split("=", 1)
            k = self._FIELD_ALIAS.get(k.strip().lower(), k.strip().lower())
            if k not in ("api_url", "api_key", "model", "context_window"):
                print(color(f"✗ 未知字段：{k}（支持 api_url/url、api_key/key、model、context_window）", "red"))
                return
            fields[k] = v.strip()
        if "context_window" in fields:
            try:
                fields["context_window"] = int(fields["context_window"])
            except ValueError:
                print(color("✗ context_window 必须是数字", "red"))
                return
        r = self.client.update_config(fields)
        if r.get("ok"):
            print(color(f"✓ 已更新：{', '.join(r.get('updated', []))}（实时生效，无需重启）", "green"))
            self._print_config(r)
        else:
            print(color(f"✗ 更新失败：{r.get('detail', '?')}", "red"))

    def handle_model(self, args: List[str]) -> None:
        """查看/切换模型：/model 或 /model <模型名>"""
        cfg = self.client.get_config()
        if not cfg.get("ok"):
            print(color(f"✗ 获取配置失败：{cfg.get('detail', '?')}", "red"))
            return
        current = cfg["config"].get("model") or "（未设置）"
        if not args:
            print(color(f"当前模型: {current}", "bold"))
            print(color("  常用: deepseek-v4-flash / deepseek-chat / deepseek-reasoner", "dim"))
            print(color("  切换: /model <模型名>", "dim"))
            return
        r = self.client.update_config({"model": args[0]})
        if r.get("ok"):
            print(color(f"✓ 模型已切换: {current} → {args[0]}（实时生效）", "green"))
        else:
            print(color(f"✗ 切换失败：{r.get('detail', '?')}", "red"))

    def handle_confirm_mode(self, args: List[str]) -> None:
        """问询模式：/confirm-mode 进入方向键选择菜单；或 /confirm-mode <模式名> 直接切换。"""
        r = self.client.get_confirm_mode()
        if not r.get("ok"):
            print(color(f"✗ 获取模式失败：{r.get('detail', '?')}", "red"))
            return
        modes = r.get("modes") or list((r.get("descriptions") or {}).keys())
        if args:
            self._set_confirm_mode(args[0])
            return
        # 交互选择：方向键 ↑/↓ 移动，Enter 确认
        current = modes.index(r.get("mode")) if r.get("mode") in modes else 0
        options = [f"{m:<9} {(r.get('descriptions') or {}).get(m, '')}" for m in modes]
        sel = select_menu(f"问询模式选择（当前: {r.get('mode')}）", options, current)
        if sel is None:
            print(color("已取消", "dim"))
            return
        target = modes[sel]
        if target == r.get("mode"):
            print(color(f"✓ 已是当前模式：{target}", "dim"))
            return
        self._set_confirm_mode(target)

    def _set_confirm_mode(self, mode: str) -> None:
        r = self.client.set_confirm_mode(mode)
        if r.get("ok"):
            print(color(f"✓ 问询模式已切换为 {r.get('mode')}：{r.get('description', '')}", "green"))
        else:
            print(color(f"✗ 切换失败：{r.get('detail', '?')}", "red"))

    def handle_config(self, args: List[str]) -> None:
        cfg = load_config()
        for a in args:
            if "=" not in a:
                print(color(f"用法：/config host=IP token=KEY port=8001", "yellow"))
                return
            k, v = a.split("=", 1)
            cfg[k.strip()] = v.strip()
        save_config(cfg)
        print(color("配置已保存到 ~/.pcagent.json（重启后生效）", "cyan"))

    def print_help(self) -> None:
        print(color("PC Agent CLI — 通过 llm_server 控制电脑", "bold"))
        print()
        print(color("== 会话管理 ==", "cyan"))
        print(color("  /new              新建会话", "reset"))
        print(color("  /sessions         列出全部会话", "reset"))
        print(color("  /switch <N>       切换到会话 N", "reset"))
        print(color("  /clear            清空当前会话历史", "reset"))
        print()
        print(color("== 状态与统计 ==", "cyan"))
        print(color("  /status           上下文容量（窗口/用量/剩余/压缩记录）", "reset"))
        print(color("  /stats            Token 用量与缓存命中率", "reset"))
        print()
        print(color("== API 与模型 ==", "cyan"))
        print(color("  /apiconfig        查看/设置 API（url=/key=/model=）", "reset"))
        print(color("  /model            查看/切换模型（/model 模型名）", "reset"))
        print(color("  /confirm-mode     问询模式（auto/strict/trusted/query）", "reset"))
        print()
        print(color("== 配置 ==", "cyan"))
        print(color("  /config k=v       保存连接配置到 ~/.pcagent.json（host/port/token）", "reset"))
        print(color("  /help             显示本帮助", "reset"))
        print(color("  /quit             退出", "reset"))
        print()
        print(color("== 常用用法（直接输入自然语言，Agent 自主调用工具）==", "cyan"))
        print(color('  "先了解项目结构"                  → repo_map（目录树+符号）', "reset"))
        print(color('  "找一下 xxx 相关的代码"           → search_text（正则搜索）', "reset"))
        print(color('  "把 a.py 里的 old 改成 new"       → replace_text（弹 diff 确认）', "reset"))
        print(color('  "看看改动 / 提交一下"             → git_status / git_diff / git_commit', "reset"))
        print(color('  "列个任务清单"                    → create_todo / update_todo', "reset"))
        print(color('  "后台跑个服务"                    → start_process / process_output', "reset"))
        print(color('  "写个 Python 程序并运行"          → create_file + run_code', "reset"))
        print(color('  "创建文件夹 projects/2026"        → create_folder', "reset"))
        print(color('  "浏览工作区"                      → list_folder', "reset"))
        if self.available_tools:
            print(color(f"  可用工具: {' / '.join(self.available_tools)}", "dim"))
        else:
            print(color("  （工具列表获取失败）", "dim"))
        print()
        print(color("== 快捷键 ==", "cyan"))
        print(color("  Ctrl+C   中止当前任务（流式输出期间）", "reset"))
        print(color("  Ctrl+D   退出", "reset"))


# ======================================================================
def main() -> int:
    _setup_utf8_stdio()
    parser = argparse.ArgumentParser(description="PC Agent CLI（Linux/终端使用）")
    parser.add_argument("--host", default=None, help="llm_server 地址（默认读 ~/.pcagent.json 或 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="llm_server 端口（默认 8001）")
    parser.add_argument("--token", default=None, help="鉴权 token（llm_server --token 启用时必填）")
    parser.add_argument("--once", default=None, help="单次模式：发送一条消息后退出（脚本用）")
    args = parser.parse_args()

    cfg = load_config()
    host = args.host or cfg.get("host") or "127.0.0.1"
    port = args.port or int(cfg.get("port", 8001))
    token = args.token if args.token is not None else cfg.get("token", "")

    cli = Cli(host, port, token)

    # Roxy 头像横幅（进入 Agent 时的欢迎画面）
    print_banner()

    # 健康检查
    ok, info = cli.client.health()
    if not ok:
        print(color(f"✗ 无法连接 llm_server（{host}:{port}）：{info}", "red"))
        print(color("  请确认主机已运行：llm_server.py --host 0.0.0.0 --token xxx", "yellow"))
        print(color("  并确保 Windows 防火墙放行该端口", "yellow"))
        return 1
    if ok:
        model = info.get("model", "?")
        configured = "已配置" if info.get("configured") else "未配置 API（在主机 Chat 的 Settings 中设置）"
        print(color(f"✓ 已连接 llm_server（{host}:{port}）| 模型: {model} | {configured}", "green"))

    if args.once:
        cli._last_content = ""
        err = cli.client.stream_chat([{"role": "user", "content": args.once}], cli.on_event)
        print()
        if err:
            print(color(f"✗ {err}", "red"))
            return 1
        print(color(f"（单次模式完成，共 {len(cli._last_content)} 字符）", "dim"))
        return 0

    cli.repl()
    return 0


if __name__ == "__main__":
    sys.exit(main())
