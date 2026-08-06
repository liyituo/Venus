"""
Telegram Bot 前端 — 用手机控制 PC Agent（纯标准库，零依赖）

架构：Telegram Bot 只是 llm_server 的又一个前端，复用全部现有 API——
  手机消息 → 本 bot → POST /api/v1/chat/stream（消费 SSE 流）
                     → ask 确认 → inline keyboard → POST /api/v1/agent/respond
                     → 会话持久化 → POST /api/v1/sessions/{id}/messages
                     → 任务清单 → todo_update 事件

运行：python telegram_bot.py [--config telegram_config.json]

配置（telegram_config.json，已 gitignore 不入库）：
  {
    "bot_token": "123456:ABC...",       # BotFather 获取
    "allowed_chat_ids": [],             # 白名单；为空时第一个发 /start 的人成为 owner
    "proxy": "",                        # 可选：http://127.0.0.1:7890（WSL 访问 Telegram 需要）
    "llm_url": "http://127.0.0.1:8001"  # 本地 llm_server（WSL 隔离测试用 --isolated）
  }

WSL 部署：把本文件 base64 传入 WSL，nohup 后台运行（详见 README）。
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR.parent / "telegram_config.json"
CHATS_FILE = BASE_DIR.parent / ".pcagent" / "telegram_chats.json"
MAX_TEXT = 3800          # Telegram 单消息上限 4096，留余量
STREAM_TICK = 1.0        # 流式回复刷新间隔（秒）
STREAM_TIMEOUT = 600     # 单次 agent 流硬超时（秒）
COMPRESS_THRESHOLD = 0.6 # 上下文达到窗口 60% 时压缩
KEEP_RECENT = 8          # 压缩时保留最近 N 条原文


def estimate_tokens(messages: list[dict]) -> int:
    """粗略估算（字符 × 0.8，与 cli.py 一致），用于压缩阈值判断。"""
    return int(sum(len(m.get("content") or "") for m in messages) * 0.8)


def _proxy_opener(proxy: str):
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def _summarize_result(result: str, ok: bool) -> str:
    """工具结果 UI 精简：成功显示 stdout 首行；失败只报简短原因，不刷原始 stderr。"""
    try:
        d = json.loads(result)
    except Exception:
        return (result or "").strip()[:100]
    if isinstance(d, dict):
        if ok:
            out = (d.get("stdout") or "").strip()
            if out:
                lines = out.splitlines()
                return lines[0][:100] + (" …" if len(lines) > 1 else "")
            return "完成"
        if d.get("error"):
            return str(d["error"])[:100]
        rc = d.get("exit_code")
        return f"失败（exit {rc}）" if rc is not None else "失败"
    return (result or "").strip()[:100]


class Bot:
    def __init__(self, cfg_path: Path):
        self.cfg_path = cfg_path
        self.cfg = self._load_cfg()
        self.opener = _proxy_opener(self.cfg.get("proxy") or "")
        self.llm_url = (self.cfg.get("llm_url") or "http://127.0.0.1:8001").rstrip("/")
        self.chats: dict[int, dict] = self._load_chats()   # chat_id -> {"session_id": int}
        self.messages: dict[int, list[dict]] = {}          # chat_id -> [user/assistant]
        self.busy: set[int] = set()                        # 正在流式的 chat_id
        self.stream_state: dict[int, dict] = {}            # 流式渲染状态
        self.context_window = 65536                        # 压缩阈值基准（run 时从 health 更新）
        self._offset = 0

    # ------------------------------------------------------------------ 配置
    def _load_cfg(self) -> dict:
        default = {"bot_token": "", "allowed_chat_ids": [], "proxy": "", "llm_url": ""}
        if self.cfg_path.exists():
            try:
                return {**default, **json.loads(self.cfg_path.read_text(encoding="utf-8"))}
            except Exception:
                pass
        return default

    def _save_cfg(self) -> None:
        self.cfg_path.write_text(json.dumps(self.cfg, indent=2, ensure_ascii=False),
                                 encoding="utf-8")

    def _load_chats(self) -> dict:
        try:
            if CHATS_FILE.exists():
                return {int(k): v for k, v in json.loads(
                    CHATS_FILE.read_text(encoding="utf-8")).items()}
        except Exception:
            pass
        return {}

    def _save_chats(self) -> None:
        try:
            CHATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            CHATS_FILE.write_text(json.dumps(self.chats, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        except OSError as exc:
            print(f"[bot] chats 持久化失败：{exc}", file=sys.stderr)

    # ------------------------------------------------------------------ Telegram API
    def api(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        """调 Telegram Bot API，返回响应 JSON；失败返回 {"ok": False, ...}。"""
        url = f"https://api.telegram.org/bot{self.cfg.get('bot_token', '')}/{method}"
        body = json.dumps(params or {}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "description": f"HTTP {e.code}"}
        except Exception as exc:
            return {"ok": False, "description": str(exc)[:200]}

    def send_message(self, chat_id: int, text: str, keyboard=None) -> dict:
        params = {"chat_id": chat_id, "text": text[:MAX_TEXT]}
        if keyboard is not None:
            params["reply_markup"] = json.dumps(keyboard)
        return self.api("sendMessage", params)

    def edit_message(self, chat_id: int, msg_id: int, text: str, keyboard=None) -> dict:
        params = {"chat_id": chat_id, "message_id": msg_id, "text": text[:MAX_TEXT]}
        if keyboard is not None:
            params["reply_markup"] = json.dumps(keyboard)
        return self.api("editMessageText", params)

    def answer_callback(self, cq_id: str, text: str = "") -> None:
        self.api("answerCallbackQuery", {"callback_query_id": cq_id, "text": text, "show_alert": False})

    # ------------------------------------------------------------------ LLM 后端 API
    def llm(self, method: str, path: str, payload=None, timeout: float = 30):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.llm_url + path, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            except Exception:
                return e.code, {}
        except Exception:
            return 0, {}

    def get_or_create_session(self, chat_id: int) -> int:
        """按 chat_id 取后端会话，无则创建。"""
        entry = self.chats.get(chat_id) or {}
        if entry.get("session_id"):
            return entry["session_id"]
        status, data = self.llm("POST", "/api/v1/sessions")
        if status == 200 and isinstance(data.get("id"), int):
            self.chats[chat_id] = {"session_id": data["id"]}
            self._save_chats()
            return data["id"]
        return 0

    def load_history(self, chat_id: int) -> None:
        """启动时从后端拉当前会话历史。"""
        sid = self.chats.get(chat_id, {}).get("session_id")
        if not sid:
            return
        status, data = self.llm("GET", f"/api/v1/sessions/{sid}")
        if status == 200:
            self.messages[chat_id] = [dict(m) for m in (data.get("session") or {}).get("messages", [])]

    def append_messages(self, chat_id: int, msgs: list[dict]) -> None:
        sid = self.chats.get(chat_id, {}).get("session_id")
        if not sid:
            return
        self.llm("POST", f"/api/v1/sessions/{sid}/messages", {"messages": msgs}, timeout=8)

    def _maybe_compress(self, chat_id: int, msgs: list[dict]) -> list[dict]:
        """上下文超过窗口 60% 时调用后端压缩，压缩结果替换本地消息并返回。
        静默执行：用户看到「◌ 思考中…」期间完成。"""
        est = estimate_tokens(msgs)
        if est <= self.context_window * COMPRESS_THRESHOLD:
            return msgs
        status, r = self.llm("POST", "/api/v1/compress",
                             {"messages": msgs, "keep_recent": KEEP_RECENT}, timeout=90)
        if status == 200 and r.get("compressed"):
            new_msgs = r.get("messages") or msgs
            self.messages[chat_id] = new_msgs
            print(f"[bot] 上下文已压缩（{est} → 估算 {estimate_tokens(new_msgs)} tokens）")
            return new_msgs
        return msgs

    # ------------------------------------------------------------------ 权限
    def allowed(self, chat_id: int) -> bool:
        ids = self.cfg.get("allowed_chat_ids") or []
        return chat_id in ids

    def _is_greeting(self, text: str) -> bool:
        """纯寒暄/状态询问：直接回复，不走 agent、不调工具、不占历史。"""
        t = text.strip().rstrip("！!？?。.～~")
        return t in ("你还在吗", "还在吗", "在吗", "在不在", "在么", "你好", "您好",
                     "hello", "hi", "hey", "哈喽", "嗨")

    def register_owner(self, chat_id: int) -> bool:
        """白名单为空时，第一个 /start 的人成为 owner。"""
        ids = self.cfg.get("allowed_chat_ids") or []
        if not ids:
            self.cfg["allowed_chat_ids"] = [chat_id]
            self._save_cfg()
            return True
        return False

    # ------------------------------------------------------------------ 主循环
    def run(self) -> None:
        r = self.api("getMe")
        if not r.get("ok"):
            print(f"[bot] 无法连接 Telegram（token 无效或网络不通）：{r.get('description', '')}")
            print("[bot] 提示：国内访问 api.telegram.org 需要代理，可在 telegram_config.json 配 proxy")
            sys.exit(1)
        print(f"[bot] 已连接 @{r['result'].get('username', '?')}，开始轮询…")
        # 拉取上下文窗口（压缩阈值基准）；失败用默认
        status, data = self.llm("GET", "/api/v1/health")
        if status == 200 and data.get("context_window"):
            self.context_window = int(data["context_window"])
            print(f"[bot] 上下文窗口: {self.context_window}")
        while True:
            params = {"offset": self._offset, "timeout": 25}
            r = self.api("getUpdates", params, timeout=40)
            if not r.get("ok"):
                print(f"[bot] getUpdates 失败：{r.get('description', '')}（2 秒后重试）")
                time.sleep(2)
                continue
            for upd in r.get("result", []):
                self._offset = upd["update_id"] + 1
                threading.Thread(target=self.handle_update, args=(upd,), daemon=True).start()

    def handle_update(self, upd: dict) -> None:
        try:
            if "message" in upd:
                self.handle_message(upd["message"])
            elif "callback_query" in upd:
                self.handle_callback(upd["callback_query"])
        except Exception as exc:
            print(f"[bot] update 处理异常：{exc}", file=sys.stderr)

    # ------------------------------------------------------------------ 命令
    def handle_message(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if not self.allowed(chat_id):
            if text == "/start" and self.register_owner(chat_id):
                self.send_message(chat_id, "已登记你为管理员（白名单为空时的首个 /start）。")
            else:
                return   # 非白名单：静默忽略
        if text == "/start" or text == "/help":
            self.cmd_help(chat_id)
        elif text == "/new":
            sid = self.get_or_create_session(chat_id)
            self.messages.pop(chat_id, None)
            self.send_message(chat_id, f"已创建 会话 #{sid}，开始新任务。")
        elif text == "/sessions":
            self.cmd_sessions(chat_id)
        elif text.startswith("/switch"):
            self.cmd_switch(chat_id, text)
        elif text == "/status":
            self.cmd_status(chat_id)
        elif text.startswith("/"):
            self.send_message(chat_id, f"未知命令：{text}（/help 查看）")
        else:
            threading.Thread(target=self.agent_flow, args=(chat_id, text), daemon=True).start()

    def cmd_help(self, chat_id: int) -> None:
        self.send_message(chat_id, (
            "PC Agent（Telegram 前端）\n"
            "直接发消息即可，Agent 自主调用工具。\n\n"
            "/new 新建会话\n/sessions 会话列表\n/switch N 切换会话\n"
            "/status 状态\n/help 帮助\n\n"
            "敏感操作（改文件/提交 git/执行命令）会弹确认按钮。"))

    def cmd_sessions(self, chat_id: int) -> None:
        status, data = self.llm("GET", "/api/v1/sessions")
        if status != 200:
            self.send_message(chat_id, f"获取会话失败（后端未运行？）{data.get('detail', '')}")
            return
        cur = self.chats.get(chat_id, {}).get("session_id")
        lines = []
        for s in sorted(data.get("sessions", []), key=lambda x: x["id"]):
            mark = "→" if s["id"] == cur else " "
            title = s.get("title") or ""
            lines.append(f"{mark} #{s['id']} {title}（{s['message_count']} 条）")
        self.send_message(chat_id, "\n".join(lines) or "（暂无会话）")

    def cmd_switch(self, chat_id: int, text: str) -> None:
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            self.send_message(chat_id, "用法：/switch <会话号>")
            return
        sid = int(parts[1])
        status, data = self.llm("GET", f"/api/v1/sessions/{sid}")
        if status != 200:
            self.send_message(chat_id, f"会话不存在：{sid}")
            return
        self.chats[chat_id] = {"session_id": sid}
        self._save_chats()
        self.load_history(chat_id)
        self.send_message(chat_id, f"已切换到 会话 #{sid}（{len(data['session']['messages'])} 条历史）")

    def cmd_status(self, chat_id: int) -> None:
        status, data = self.llm("GET", "/api/v1/health")
        if status != 200:
            self.send_message(chat_id, f"LLM 后端未连接：{data.get('detail', '')}")
            return
        sid = self.chats.get(chat_id, {}).get("session_id", 0)
        self.send_message(chat_id,
                          f"后端: {'已配置 ' + data.get('model', '') if data.get('configured') else '未配置 API'}\n"
                          f"隔离模式: {'是' if data.get('isolated') else '否'}\n"
                          f"当前会话: #{sid}\n工具: {len(data.get('tools') or [])} 个")

    # ------------------------------------------------------------------ 确认回调
    def handle_callback(self, cq: dict) -> None:
        chat_id = cq["message"]["chat"]["id"]
        data = (cq.get("data") or "").split(":", 1)
        choice, request_id = (data + [""])[:2]
        if choice in ("yes", "no") and request_id:
            self.llm("POST", "/api/v1/agent/respond",
                     {"request_id": request_id, "choice": choice}, timeout=10)
        # 解冻渲染：确认已应答，恢复流式刷新
        st = self.stream_state.get(chat_id)
        if st:
            st["frozen"] = False
        self.answer_callback(cq["id"], "已确认" if choice == "yes" else "已拒绝")

    # ------------------------------------------------------------------ Agent 流程
    def agent_flow(self, chat_id: int, text: str) -> None:
        if chat_id in self.busy:
            self.send_message(chat_id, "正在执行上一个任务，请稍候…")
            return
        self.busy.add(chat_id)
        try:
            self._agent_flow(chat_id, text)
        except Exception as exc:
            try:
                self.send_message(chat_id, f"⚠ 任务异常：{exc}")
            except Exception:
                pass
        finally:
            self.busy.discard(chat_id)

    def _agent_flow(self, chat_id: int, text: str) -> None:
        sid = self.get_or_create_session(chat_id)
        if not sid:
            self.send_message(chat_id, "无法创建会话（llm_server 未运行？）")
            return
        # 惰性加载：bot 重启后从后端恢复本会话历史（记忆上一个任务）
        if chat_id not in self.messages:
            self.load_history(chat_id)
        msgs = self.messages.setdefault(chat_id, [])
        msgs.append({"role": "user", "content": text})
        self.append_messages(chat_id, [{"role": "user", "content": text}])

        # 初始状态
        state = {"log": [], "text": "", "done": False, "msg_id": None,
                 "ask_sent": False, "start": time.monotonic()}
        self.stream_state[chat_id] = state
        holder = self.send_message(chat_id, "◌ 思考中…")
        state["msg_id"] = holder.get("result", {}).get("message_id")
        # 上下文压缩：超过阈值时压缩早期历史（模型永远拿到压缩版，防 tokens 爆炸）
        msgs = self._maybe_compress(chat_id, msgs)

        # 渲染刷新线程：每 1 秒把累积内容编辑进消息（ask 确认期间冻结，防止覆盖确认按钮）
        def renderer():
            last = ""
            while not state["done"]:
                if time.monotonic() - state["start"] > STREAM_TIMEOUT:
                    break
                if state.get("frozen"):
                    time.sleep(STREAM_TICK)   # 确认框显示中：不刷新，保护按钮
                    continue
                content = self._render_text(state)
                if content and content != last:
                    self.edit_message(chat_id, state["msg_id"], content)
                    last = content
                time.sleep(STREAM_TICK)
        threading.Thread(target=renderer, daemon=True).start()

        # 消费 SSE 流
        stream_result = self._consume_stream(chat_id, msgs, state)
        state["done"] = True
        time.sleep(0.2)   # 让渲染线程把最后一帧刷上去

        if stream_result is None:
            # 正常完成：持久化 assistant 回复（纯工具任务无文本时也留痕，保证历史成对）
            content = state["text"].strip()
            reply = content or "✓ 任务完成"
            msgs.append({"role": "assistant", "content": reply})
            self.append_messages(chat_id, [{"role": "assistant", "content": reply}])
            final = self._render_text(state)
            self.edit_message(chat_id, state["msg_id"], final or "✓ 任务完成")
        else:
            # 失败/中断：同样留痕，后续「继续任务」时模型能看到上次任务的状态
            note = f"（上次任务失败或中断：{stream_result[:150]}）"
            msgs.append({"role": "assistant", "content": note})
            self.append_messages(chat_id, [{"role": "assistant", "content": note}])
            self.edit_message(chat_id, state["msg_id"], f"⚠ {stream_result}")

    def _render_text(self, state: dict) -> str:
        """只返回模型最终回复内容（不显示工具日志，保持界面简洁）。"""
        return (state.get("text") or "")[:MAX_TEXT]

    def _consume_stream(self, chat_id: int, msgs: list[dict], state: dict) -> str | None:
        """POST /chat/stream 消费 SSE；返回 None=正常结束，str=错误信息。"""
        body = json.dumps({"messages": msgs, "agent": True}).encode("utf-8")
        req = urllib.request.Request(self.llm_url + "/api/v1/chat/stream", data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=STREAM_TIMEOUT)
        except Exception as exc:
            return f"无法连接 LLM 后端：{exc}"
        event = ""
        try:
            with resp:
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
                    if event == "tool_call":
                        event = ""
                        try:
                            d = json.loads(payload)
                            args = d.get("arguments") or "{}"
                            try:
                                arg_str = ", ".join(f"{k}={v}" for k, v in json.loads(args).items()) or ""
                            except Exception:
                                arg_str = args[:60]
                            state["log"].append(f"⚙ [{d.get('name')}] {arg_str}")
                        except Exception:
                            pass
                        continue
                    if event == "tool_result":
                        event = ""
                        try:
                            d = json.loads(payload)
                            mark = "✓" if d.get("ok") else "✗"
                            state["log"].append(f"  {mark} {_summarize_result(d.get('result') or '', d.get('ok'))}")
                        except Exception:
                            pass
                        continue
                    if event == "ask":
                        event = ""
                        try:
                            d = json.loads(payload)
                            self._ask_user(chat_id, state, d)
                        except Exception:
                            pass
                        continue
                    if event == "todo_update":
                        event = ""
                        continue
                    if payload == "[DONE]":
                        break
                    try:
                        d = json.loads(payload)
                        delta = (d.get("choices") or [{}])[0].get("delta") or {}
                        content = delta.get("content") or ""
                        if content:
                            state["text"] += content
                    except Exception:
                        pass
        except Exception as exc:
            return f"流中断：{exc}"
        return None

    def _ask_user(self, chat_id: int, state: dict, d: dict) -> None:
        """ask 确认：把当前消息替换为带按钮的确认框，等待用户点按钮回传。
        冻结渲染线程，防止后续刷新把确认按钮覆盖掉。"""
        question = d.get("question") or "需要确认"
        diff = d.get("diff")
        text = f"❓ {question}"
        if diff:
            text += f"\n\n<pre>{diff[:1500]}</pre>"
        keyboard = {"inline_keyboard": [[
            {"text": "✓ 允许", "callback_data": f"yes:{d.get('id')}"},
            {"text": "✗ 拒绝", "callback_data": f"no:{d.get('id')}"},
        ]]}
        state["frozen"] = True
        self.edit_message(chat_id, state["msg_id"], text, keyboard=keyboard)


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram Bot 前端（纯标准库）")
    parser.add_argument("--config", default=str(CONFIG_PATH),
                        help="配置文件路径（默认 telegram_config.json）")
    args = parser.parse_args()
    bot = Bot(Path(args.config))
    if not bot.cfg.get("bot_token"):
        print(f"[bot] 请先在 {args.config} 填写 bot_token（BotFather 获取）")
        sys.exit(1)
    bot.run()


if __name__ == "__main__":
    main()
