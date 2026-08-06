"""CLI 会话模块测试：摘要恢复 + 按需懒加载（不启动全量拉取）。

用 FakeClient 模拟后端，验证：
- restore 只拉摘要（无 messages）
- switch/send 时才懒加载单个会话（且不重复加载）
- new_session 走后端创建
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import cli as cli_mod  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


class FakeClient:
    """模拟后端：摘要列表 / 单会话 / 创建。记录所有请求路径。"""

    def __init__(self):
        self.calls = []
        self.sessions_data = [
            {"id": 1, "title": "会话一", "message_count": 4, "updated": "x"},
            {"id": 2, "title": "会话二", "message_count": 0, "updated": "x"},
        ]
        self.full_data = {
            1: {"id": 1, "title": "会话一", "messages": [
                {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                {"role": "user", "content": "c"}, {"role": "assistant", "content": "d"}]},
        }

    def api(self, method, path, payload=None, timeout=8):
        self.calls.append((method, path))
        if method == "GET" and path == "/api/v1/sessions":
            return 200, {"ok": True, "sessions": self.sessions_data}
        if method == "GET" and path.startswith("/api/v1/sessions/"):
            sid = int(path.rsplit("/", 1)[-1])
            if sid in self.full_data:
                return 200, {"ok": True, "session": self.full_data[sid]}
            return 200, {"ok": True, "session": {"id": sid, "messages": []}}
        if method == "POST" and path == "/api/v1/sessions":
            sid = max(s["id"] for s in self.sessions_data) + 1
            self.sessions_data.append({"id": sid, "title": "", "message_count": 0, "updated": ""})
            return 200, {"ok": True, "id": sid}
        if method == "DELETE" and path.endswith("/messages"):
            return 200, {"ok": True, "cleared": 1}
        return 404, {}

    def health(self):
        return True, {"context_window": 65536, "tools": []}

    def compress(self, messages, keep_recent=8):
        return {"ok": True, "compressed": False}

    def stream_chat(self, messages, on_event):
        return None


def make_cli():
    c = cli_mod.Cli.__new__(cli_mod.Cli)
    c.client = FakeClient()
    c.sessions = {}
    c.current = 0
    c.context_window = 65536
    c._compress_log = []
    return c


# ============ 1. 摘要恢复 ============
print("== 1. 摘要恢复（不拉消息）==")
c = make_cli()
c.restore_sessions()
check("恢复 2 个会话", len(c.sessions) == 2, str(c.sessions))
check("当前会话 = 最新", c.current == 2, str(c.current))
check("本地 messages 为空（懒加载）",
      all(not s["messages"] for s in c.sessions.values()), str(c.sessions))
check("count 来自摘要", c.sessions[1]["count"] == 4, "")
check("只调用了摘要接口（未拉单会话）",
      all(path != "/api/v1/sessions/1" for _, path in c.client.calls), str(c.client.calls))

# ============ 2. 按需懒加载 ============
print("== 2. 按需懒加载 ==")
c = make_cli()
c.restore_sessions()
c.switch(1)
check("switch 触发懒加载", len(c.sessions[1]["messages"]) == 4,
      str(c.sessions[1]["messages"]))
n_single = sum(1 for _, p in c.client.calls if p.startswith("/api/v1/sessions/1"))
check("单会话只拉一次", n_single == 1, str(c.client.calls))
c.switch(1)
n_single2 = sum(1 for _, p in c.client.calls if p.startswith("/api/v1/sessions/1"))
check("重复 switch 不重复拉取", n_single2 == 1, "")

# 空会话不拉取
c.switch(2)
check("空会话不触发拉取", len(c.sessions[2]["messages"]) == 0, "")
n_single2b = sum(1 for _, p in c.client.calls if p.startswith("/api/v1/sessions/2"))
check("会话2 未请求", n_single2b == 0, str(c.client.calls))

# ============ 3. 新建会话 ============
print("== 3. 新建会话 ==")
c = make_cli()
c.restore_sessions()
c.new_session()
check("新建走后端 id=3", 3 in c.sessions and c.current == 3, str(c.sessions))
check("新会话无消息无拉取", c.sessions[3]["messages"] == [] and c.sessions[3]["count"] == 0, "")

# ============ 4. 降级（后端不可用）============
print("== 4. 降级 ==")
c = make_cli()
c.client = FakeClient()
c.client.api = lambda m, p, pl=None, t=8: (0, {})   # 后端全挂
c.restore_sessions()
check("后端不可用降级本地会话", len(c.sessions) == 1 and c.current == 1, str(c.sessions))

# ============ 汇总 ============
print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
