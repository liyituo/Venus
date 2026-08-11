"""Telegram bot 修复测试：/new 全新会话 ID / 定时时间校验与 ID 单调 / callback 权限 / 并发串行。"""
import sys
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import telegram_bot as tg  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


def make_bot():
    """构造不依赖网络的 Bot 实例。"""
    bot = tg.Bot.__new__(tg.Bot)
    bot.cfg = {"bot_token": "test", "allowed_chat_ids": [100], "proxy": "", "llm_url": "",
               "allowed_user_ids": [], "owner_user_id": 42}
    bot.chats = {}
    bot.messages = {}
    bot.schedules = []
    bot.busy = set()
    bot.stream_state = {}
    bot.context_window = 65536
    bot._state_lock = threading.Lock()
    bot._chat_locks = {}
    bot._schedules_lock = threading.Lock()
    bot._save_chats = mock.Mock()
    bot._save_schedules = mock.Mock()
    bot.send_message = mock.Mock()
    bot.answer_callback = mock.Mock()
    return bot


# ============ 1. /new 强制全新会话 ID ============
print("== 1. /new 全新会话 ID ==")
bot = make_bot()
bot.chats[100] = {"session_id": 5}          # 已有旧会话
created = iter([10, 11, 12])
bot.llm = mock.Mock(side_effect=lambda method, path, payload=None, timeout=30:
                    (200, {"id": next(created)}) if path == "/api/v1/sessions" else (200, {}))
sid1 = bot._create_new_session(100)
check("/new 不复用旧 ID", sid1 == 10 and bot.chats[100]["session_id"] == 10, str(sid1))
check("/new 清空内存历史", 100 not in bot.messages, "")
sid2 = bot._create_new_session(100)
check("再次 /new 又是新 ID", sid2 == 11 and bot.chats[100]["session_id"] == 11, str(sid2))
check("两次 /new ID 不同", sid1 != sid2, f"{sid1} vs {sid2}")
# handle_message 走 /new 分支（不经过 get_or_create_session）；owner 发送才授权
bot.cfg["allowed_chat_ids"] = [100]
bot.cfg["owner_user_id"] = 42
bot.llm = mock.Mock(return_value=(200, {"id": 20}))
bot.handle_message({"chat": {"id": 100}, "from": {"id": 42}, "text": "/new"})
check("handle_message /new 创建新会话", bot.chats[100]["session_id"] == 20, str(bot.chats[100]))
# 未授权成员（非 owner / 不在 allowed_user_ids）→ 静默拒绝，不执行命令
bot.llm.reset_mock()
bot.handle_message({"chat": {"id": 100}, "from": {"id": 1}, "text": "/new"})
check("handle_message 未授权成员被拒", bot.llm.call_count == 0, str(bot.llm.call_args_list))

# ============ 2. 定时任务时间校验 ============
print("== 2. 定时时间校验 ==")
bot = make_bot()
bot.cmd_schedule(100, "/schedule add 25:00 任务")
bot.cmd_schedule(100, "/schedule add 08:60 任务")
bot.cmd_schedule(100, "/schedule add 99:99 任务")
check("非法时间拒绝（3 次）", bot.send_message.call_count == 3
      and all("时间" in c.args[1] or "格式" in c.args[1] for c in bot.send_message.call_args_list),
      str([c.args[1] for c in bot.send_message.call_args_list]))
check("非法时间不添加", bot.schedules == [], str(bot.schedules))

bot.send_message.reset_mock()
bot.cmd_schedule(100, "/schedule add 08:30 每日新闻")
check("合法时间添加", len(bot.schedules) == 1 and bot.schedules[0]["time"] == "08:30", str(bot.schedules))
check("添加提示", bot.send_message.call_count == 1 and "已添加" in bot.send_message.call_args.args[1], "")

# ============ 3. 删除后新建 ID 不重复 ============
print("== 3. 删除后 ID 单调 ==")
bot = make_bot()
bot.schedules = [{"id": "s1", "time": "08:00", "prompt": "a", "chat_id": 100,
                  "last_run": "", "enabled": True},
                 {"id": "s2", "time": "09:00", "prompt": "b", "chat_id": 100,
                  "last_run": "", "enabled": True}]
bot.send_message.reset_mock()
bot.cmd_schedule(100, "/schedule del s1")
new_id = bot._next_schedule_id()
check("删除 s1 后新建不重复", new_id == "s3", new_id)
bot.cmd_schedule(100, "/schedule add 10:00 新任务")
check("新建 ID = s3", bot.schedules[-1]["id"] == "s3", str(bot.schedules[-1]))

# ============ 4. callback 身份验证 ============
print("== 4. callback 权限 ==")
bot = make_bot()
bot.cfg["allowed_chat_ids"] = [100]
bot.cfg["owner_user_id"] = 42
bot.llm = mock.Mock(return_value=(200, {}))
cq = {"id": "cq1", "from": {"id": 999}, "message": {"chat": {"id": 100}},
      "data": "yes:ask-1"}
bot.handle_callback(cq)
check("非 owner 成员 callback 拒绝", bot.llm.call_count == 0, str(bot.llm.call_args_list))
check("拒绝提示", bot.answer_callback.called and "未授权" in bot.answer_callback.call_args.args[1], "")

bot.llm.reset_mock()
bot.answer_callback.reset_mock()
cq_owner = {"id": "cq2", "from": {"id": 42}, "message": {"chat": {"id": 100}},
            "data": "yes:ask-2"}
bot.handle_callback(cq_owner)
check("owner callback 放行并回传", bot.llm.call_count == 1
      and bot.llm.call_args.args[2] == {"request_id": "ask-2", "choice": "yes"}, str(bot.llm.call_args_list))

# allowed_user_ids 命中
bot.llm.reset_mock()
bot.cfg["allowed_user_ids"] = [777]
cq_uid = {"id": "cq3", "from": {"id": 777}, "message": {"chat": {"id": 100}},
          "data": "no:ask-3"}
bot.handle_callback(cq_uid)
check("allowed_user_ids 成员放行", bot.llm.call_count == 1
      and bot.llm.call_args.args[2] == {"request_id": "ask-3", "choice": "no"}, "")

# 群聊任意成员（无用户级配置）→ 新安全策略：拒绝（不因聊天白名单放行）
bot2 = make_bot()
bot2.cfg["allowed_chat_ids"] = [100]
bot2.cfg["owner_user_id"] = None
bot2.cfg["allowed_user_ids"] = []
bot2.llm = mock.Mock(return_value=(200, {}))
cq_legacy = {"id": "cq4", "from": {"id": 5}, "message": {"chat": {"id": 100}},
             "data": "yes:ask-4"}
bot2.handle_callback(cq_legacy)
check("无用户级配置时群聊成员被拒绝（不再兼容聊天白名单）",
      bot2.llm.call_count == 0 and bot2.answer_callback.called, "")

# ============ 5. 并发：同 chat 串行，busy 原子 ============
print("== 5. 并发串行 ==")
bot = make_bot()
bot.llm = mock.Mock(return_value=(200, {"id": 1}))
started = threading.Event()
release = threading.Event()

def fake_flow(chat_id, text):
    started.set()
    release.wait(5)
    return None

bot._agent_flow = mock.Mock(side_effect=fake_flow)
t1 = threading.Thread(target=bot.agent_flow, args=(100, "任务1"), daemon=True)
t1.start()
started.wait(5)
# 第一个任务执行中：第二个任务应被 busy 拒绝
bot.send_message.reset_mock()
bot.agent_flow(100, "任务2")
check("并发第二个任务被拒", bot.send_message.called
      and "正在执行" in bot.send_message.call_args.args[1], str(bot.send_message.call_args_list))
release.set()
t1.join(timeout=10)
check("任务结束后 busy 释放", 100 not in bot.busy, "")
# 结束后可再次执行
bot.send_message.reset_mock()
bot.agent_flow(100, "任务3")
check("结束后新任务可执行", bot._agent_flow.call_count == 2, str(bot._agent_flow.call_count))

# ============ 6. 文件唯一名（不静默覆盖）============
print("== 6. 文件唯一名 ==")
bot = make_bot()
bot.api = mock.Mock(side_effect=lambda method, params=None, timeout=30:
                    {"ok": True, "result": {"file_path": "f/x.bin"}})
import tempfile  # noqa: E402
tmpdir = tempfile.mkdtemp(prefix="tg_upload_")
import unittest.mock as um
with um.patch.object(Path, "home", return_value=Path(tmpdir)):
    bot.llm = mock.Mock(return_value=(200, {"workspace": str(Path(tmpdir) / "agent_workspace")}))
    opener = mock.Mock()
    opener.open = mock.Mock(return_value=mock.Mock(**{"read.return_value": b"data" * 100}))
    bot.opener = opener
    bot.handle_file(100, {"document": {"file_id": "f1", "file_name": "同.txt",
                                       "file_size": 400}}, 42)
    bot.handle_file(100, {"document": {"file_id": "f2", "file_name": "同.txt",
                                       "file_size": 400}}, 42)
    files = sorted(p.name for p in Path(tmpdir).glob("agent_workspace/telegram_uploads/*"))
check("同名文件不覆盖（唯一名）", len(files) == 2 and files[0] != files[1], str(files))
check("文件名被清洗", all(Path(f).name == f for f in files), str(files))

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
