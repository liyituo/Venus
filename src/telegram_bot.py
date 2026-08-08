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
import logging
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR.parent / "telegram_config.json"
CHATS_FILE = BASE_DIR.parent / ".pcagent" / "telegram_chats.json"
SCHEDULES_FILE = BASE_DIR.parent / ".pcagent" / "schedules.json"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("telegram-bot")


def _setup_file_logging() -> None:
    """统一运行日志：写入项目根 .pcagent/bot.log（1MB 轮转，保留 3 份）。"""
    try:
        from logging.handlers import RotatingFileHandler
        log_dir = BASE_DIR.parent / ".pcagent"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "bot.log", maxBytes=1_000_000,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
        log.info("运行日志已写入 %s", log_dir / "bot.log")
    except Exception as exc:
        log.warning("日志文件初始化失败：%s", exc)


_setup_file_logging()
MAX_TEXT = 3800          # Telegram 单消息上限 4096，留余量
STREAM_TICK = 1.0        # 流式回复刷新间隔（秒）
STREAM_TIMEOUT = 600     # 单次 agent 流硬超时（秒）
COMPRESS_THRESHOLD = 0.6 # 上下文达到窗口 60% 时压缩
KEEP_RECENT = 8          # 压缩时保留最近 N 条原文
MAX_LOCAL_MESSAGES = 100 # bot 本地历史上限（内存控制；发送仍受后端 20 条/12 万字符裁剪）
MAX_UPLOAD_BYTES = 20 * 1024 * 1024   # 手机上传文件大小上限（20MB）
UPLOAD_DIR = "telegram_uploads"       # 上传文件存放目录（工作区内）


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
        self.schedules: list[dict] = self._load_schedules()  # 定时任务
        self._stop = threading.Event()
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

    def _load_schedules(self) -> list[dict]:
        try:
            if SCHEDULES_FILE.exists():
                data = json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
                return data if isinstance(data, list) else []
        except Exception:
            pass
        return []

    def _save_schedules(self) -> None:
        try:
            SCHEDULES_FILE.parent.mkdir(parents=True, exist_ok=True)
            SCHEDULES_FILE.write_text(
                json.dumps(self.schedules, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            log.warning("定时任务保存失败：%s", exc)

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
        if keyboard is not None:
            # 带按钮的消息不分段（确认框文本短）；保持原有行为
            return self.api("sendMessage", {
                "chat_id": chat_id, "text": text[:MAX_TEXT],
                "reply_markup": json.dumps(keyboard)})
        if len(text) <= MAX_TEXT:
            return self.api("sendMessage", {"chat_id": chat_id, "text": text})
        # 超长自动分段：Telegram 单条上限 4096，超长硬截断会让后半段丢失
        last = None
        for i in range(0, len(text), MAX_TEXT):
            last = self.api("sendMessage", {"chat_id": chat_id, "text": text[i:i + MAX_TEXT]})
            if not last.get("ok"):
                log.warning("分段发送第 %d 段失败：%s", i // MAX_TEXT + 1,
                            last.get("description", "?"))
        return last

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
            log.info("上下文已压缩（%s → 估算 %s tokens）", est, estimate_tokens(new_msgs))
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
            log.error("无法连接 Telegram（token 无效或网络不通）：%s", r.get('description', ''))
            print(f"[bot] 无法连接 Telegram（token 无效或网络不通）：{r.get('description', '')}")
            print("[bot] 提示：国内访问 api.telegram.org 需要代理，可在 telegram_config.json 配 proxy")
            sys.exit(1)
        log.info("已连接 @%s，开始轮询", r['result'].get('username', '?'))
        print(f"[bot] 已连接 @{r['result'].get('username', '?')}，开始轮询…")
        # 启动定时任务调度线程
        threading.Thread(target=self._scheduler_loop, daemon=True,
                         name="scheduler").start()
        if self.schedules:
            log.info("定时任务已加载：%d 个", len(self.schedules))
        # 拉取上下文窗口（压缩阈值基准）；失败用默认
        status, data = self.llm("GET", "/api/v1/health")
        if status == 200 and data.get("context_window"):
            self.context_window = int(data["context_window"])
            print(f"[bot] 上下文窗口: {self.context_window}")
        while True:
            params = {"offset": self._offset, "timeout": 25}
            r = self.api("getUpdates", params, timeout=40)
            if not r.get("ok"):
                log.warning("getUpdates 失败：%s（2 秒后重试）", r.get('description', ''))
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
        if not self.allowed(chat_id):
            if text == "/start" and self.register_owner(chat_id):
                self.send_message(chat_id, "已登记你为管理员（白名单为空时的首个 /start）。")
            else:
                return   # 非白名单：静默忽略
        if text:
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
            elif text == "/stats":
                self.cmd_stats(chat_id)
            elif text.startswith("/send"):
                self.cmd_send(chat_id, text)
            elif text.startswith("/schedule"):
                self.cmd_schedule(chat_id, text)
            elif text == "/agents":
                self.cmd_agents(chat_id)
            elif text.startswith("/"):
                self.send_message(chat_id, f"未知命令：{text}（/help 查看）")
            else:
                threading.Thread(target=self.agent_flow, args=(chat_id, text), daemon=True).start()
        elif msg.get("document") or msg.get("photo"):
            # 文件消息：下载到工作区 telegram_uploads/，供 agent 处理
            threading.Thread(target=self.handle_file, args=(chat_id, msg), daemon=True).start()

    def handle_file(self, chat_id: int, msg: dict) -> None:
        """接收手机上传的文件/图片，保存到工作区 telegram_uploads/ 并告知位置。"""
        try:
            if "document" in msg:
                doc = msg["document"]
                file_id = doc["file_id"]
                fname = doc.get("file_name") or f"file_{file_id[:8]}"
                size = doc.get("file_size") or 0
            else:
                photo = msg["photo"][-1]   # 取最大尺寸
                file_id = photo["file_id"]
                fname = f"photo_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
                size = photo.get("file_size") or 0
            if size > MAX_UPLOAD_BYTES:
                self.send_message(chat_id, f"文件过大（{size // 1024} KB > 20MB 上限）")
                return
            # 获取文件路径并下载（走代理）
            r = self.api("getFile", {"file_id": file_id})
            if not r.get("ok"):
                self.send_message(chat_id, f"获取文件失败：{r.get('description', '')}")
                return
            file_path = r["result"].get("file_path", "")
            if not file_path:
                self.send_message(chat_id, "文件下载失败（无 file_path）")
                return
            url = f"https://api.telegram.org/file/bot{self.cfg.get('bot_token', '')}/{file_path}"
            data = self.opener.open(url, timeout=60).read()
            # 安全落盘：文件名清洗（防路径穿越），统一放 telegram_uploads/
            safe_name = Path(fname).name.strip()[:120] or f"file_{int(time.time())}"
            up_dir = Path.home() / "agent_workspace" / UPLOAD_DIR
            up_dir.mkdir(parents=True, exist_ok=True)
            target = up_dir / safe_name
            target.write_bytes(data)
            log.info("收到文件 %s（%d KB）→ %s", safe_name, len(data) // 1024, target)
            self.send_message(
                chat_id,
                f"已收到 `{safe_name}`（{len(data) // 1024} KB）\n"
                f"存放在工作区 `{UPLOAD_DIR}/{safe_name}`\n"
                f"想让我怎么处理？（如「读取并解释」「检查 bug」）")
        except Exception as exc:
            self.send_message(chat_id, f"文件接收失败：{exc}")

    def cmd_agents(self, chat_id: int) -> None:
        """列出可用子 agent（delegate 委派执行，对话里直接说任务即可自动匹配）。"""
        status, data = self.llm("GET", "/api/v1/agents")
        if status != 200:
            self.send_message(chat_id, f"获取子 agent 列表失败：{data.get('detail', '')}")
            return
        agents = data.get("agents") or []
        if not agents:
            self.send_message(chat_id, "暂无子 agent（在 agents/ 目录添加 <名称>.json 即启用）")
            return
        lines = ["可用子 agent（说任务时自动委派）："]
        for a in agents:
            extra = f"（模型 {a['model']}）" if a.get("model") else ""
            lines.append(f"· {a['name']}{extra}：{a.get('description') or '无描述'}")
        self.send_message(chat_id, "\n".join(lines))

    def cmd_help(self, chat_id: int) -> None:
        self.send_message(chat_id, (
            "PC Agent（Telegram 前端）\n"
            "直接发消息即可，Agent 自主调用工具。\n\n"
            "/new 新建会话\n/sessions 会话列表\n/switch N 切换会话\n"
            "/status 状态\n/stats Token 用量\n/send <路径> 发送工作区文件给你\n"
            "/schedule 定时任务\n/agents 子 agent 列表\n/help 帮助\n\n"
            "敏感操作（改文件/提交 git/执行命令）会弹确认按钮。"))

    def cmd_send(self, chat_id: int, text: str) -> None:
        """/send <路径>：把工作区内的文件发送给用户（multipart 上传，20MB 上限）。"""
        parts = text.split(None, 1)
        if len(parts) < 2:
            self.send_message(chat_id, "用法：/send <路径>（相对工作区或绝对路径，限工作区内）")
            return
        ws = Path.home() / "agent_workspace"
        raw = parts[1].strip()
        full = (ws / raw) if not Path(raw).is_absolute() else Path(raw)
        try:
            full = full.resolve()
            full.relative_to(ws.resolve())
        except (ValueError, OSError):
            self.send_message(chat_id, f"拒绝：路径必须位于工作区 `{ws}` 内")
            return
        threading.Thread(target=self._do_send, args=(chat_id, full), daemon=True).start()

    def _do_send(self, chat_id: int, path: Path) -> None:
        """发送文件（multipart/form-data，纯标准库构造）。"""
        try:
            if not path.is_file():
                self.send_message(chat_id, f"文件不存在：`{path}`")
                return
            size = path.stat().st_size
            if size > MAX_UPLOAD_BYTES:
                self.send_message(chat_id, f"文件过大（{size // 1024} KB > 20MB 上限）")
                return
            fname = re.sub(r'[\r\n"]', "_", path.name)[:120]
            url = (f"https://api.telegram.org/bot{self.cfg.get('bot_token', '')}"
                   f"/sendDocument")
            boundary = "----pcagent" + str(int(time.time() * 1000))
            head = (f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="document"; filename="{fname}"\r\n'
                    f"Content-Type: application/octet-stream\r\n\r\n")
            body = head.encode("utf-8") + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            with self.opener.open(req, timeout=120) as resp:
                r = json.loads(resp.read().decode("utf-8"))
            if r.get("ok"):
                self.send_message(chat_id, f"已发送 `{fname}`（{size // 1024} KB）")
            else:
                self.send_message(chat_id, f"发送失败：{r.get('description', '?')}")
        except Exception as exc:
            self.send_message(chat_id, f"文件发送失败：{exc}")

    # ------------------------------------------------------------------ 定时任务
    def cmd_schedule(self, chat_id: int, text: str) -> None:
        """/schedule：列出；/schedule add HH:MM <任务描述>；/schedule del <id>；
        /schedule off|on <id>。到点自动执行并推送结果。"""
        parts = text.split()
        if len(parts) == 1:
            if not self.schedules:
                self.send_message(
                    chat_id, "暂无定时任务。\n用法：/schedule add 08:00 搜索今日科技新闻并总结")
                return
            lines = ["定时任务："]
            for s in self.schedules:
                state = "开" if s.get("enabled", True) else "关"
                lines.append(f"#{s['id']} [{state}] {s.get('time')} "
                             f"{s.get('prompt', '')[:40]}"
                             f"{'（上次 ' + s['last_run'] + '）' if s.get('last_run') else ''}")
            self.send_message(chat_id, "\n".join(lines))
            return
        sub = parts[1].lower()
        if sub == "add":
            if len(parts) < 4:
                self.send_message(chat_id, "用法：/schedule add HH:MM <任务描述>")
                return
            hhmm = parts[2]
            if not re.fullmatch(r"\d{2}:\d{2}", hhmm):
                self.send_message(chat_id, "时间格式应为 HH:MM（24 小时制）")
                return
            prompt = " ".join(parts[3:])
            sid = f"s{len(self.schedules) + 1}"
            self.schedules.append({"id": sid, "time": hhmm, "prompt": prompt,
                                   "chat_id": chat_id, "last_run": "", "enabled": True})
            self._save_schedules()
            self.send_message(chat_id, f"✓ 已添加定时任务 #{sid}：每天 {hhmm} 执行「{prompt[:40]}」")
        elif sub in ("del", "off", "on"):
            if len(parts) < 3:
                self.send_message(chat_id, f"用法：/schedule {sub} <id>")
                return
            target = next((s for s in self.schedules if s["id"] == parts[2]), None)
            if target is None:
                self.send_message(chat_id, f"定时任务不存在：{parts[2]}（/schedule 查看）")
                return
            if sub == "del":
                self.schedules.remove(target)
                self.send_message(chat_id, f"已删除定时任务 #{target['id']}")
            else:
                target["enabled"] = (sub == "on")
                self.send_message(chat_id, f"定时任务 #{target['id']} 已{'开启' if sub == 'on' else '暂停'}")
            self._save_schedules()
        else:
            self.send_message(chat_id, "未知子命令（add / del / on / off），/schedule 查看用法")

    def _scheduler_loop(self) -> None:
        """调度线程：每 30 秒检查 HH:MM 是否到点（每天一次），触发 agent_flow 并推送结果。"""
        while not self._stop.is_set():
            now = time.strftime("%H:%M")
            today = time.strftime("%Y-%m-%d")
            for s in list(self.schedules):
                if not s.get("enabled", True) or s.get("time") != now:
                    continue
                if (s.get("last_run") or "").startswith(today):
                    continue    # 今天已触发
                s["last_run"] = f"{today} {now}"
                self._save_schedules()
                log.info("定时任务 %s 触发: %s", s["id"], s.get("prompt", "")[:50])
                threading.Thread(target=self.agent_flow,
                                 args=(s.get("chat_id"), s.get("prompt", "")),
                                 daemon=True).start()
            self._stop.wait(30)

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
        rm = data.get("reasoning_mode") or "max"
        rm_label = {"max": "最高", "high": "高", "off": "关闭"}.get(rm, rm)
        self.send_message(chat_id,
                          f"后端: v{data.get('version', '?')} · {'已配置 ' + data.get('model', '') if data.get('configured') else '未配置 API'}\n"
                          f"推理强度: {rm}（{rm_label}）\n"
                          f"隔离模式: {'是' if data.get('isolated') else '否'}\n"
                          f"当前会话: #{sid}\n工具: {len(data.get('tools') or [])} 个")

    def cmd_stats(self, chat_id: int) -> None:
        """Token 用量统计（llm_server 进程内聚合，重启清零）。"""
        status, d = self.llm("GET", "/api/v1/stats")
        if status != 200:
            self.send_message(chat_id, f"获取统计失败：{d.get('detail', '')}")
            return
        prompt = d.get("prompt_tokens") or 0
        cached = d.get("cached_tokens") or 0
        pct = d.get("cache_hit_rate_pct") or 0
        lines = [
            "Token 用量统计",
            f"调用次数: {d.get('calls', 0)}",
            f"Prompt: {prompt:,}（缓存命中 {cached:,}，{pct}%）",
            f"Completion: {d.get('completion_tokens', 0):,}（推理 {d.get('reasoning_tokens', 0):,}）",
        ]
        self.send_message(chat_id, "\n".join(lines))

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
        # 本地历史上限：只保留最近 MAX_LOCAL_MESSAGES 条（防内存无限增长）
        if len(msgs) > MAX_LOCAL_MESSAGES:
            del msgs[:len(msgs) - MAX_LOCAL_MESSAGES]
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
        plan = d.get("plan")
        if plan:
            lines = ["📋 执行计划（批准后按计划执行）:"]
            for i, s in enumerate(plan, 1):
                tools = ", ".join(s.get("tools") or []) or "—"
                lines.append(f"{i}. {s.get('step', '')}\n   🛠 需要: {tools}")
                if s.get("reason"):
                    lines.append(f"   原因: {s['reason']}")
            text += "\n\n" + "\n".join(lines)
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
