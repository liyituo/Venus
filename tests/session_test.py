"""会话持久化测试：CRUD + 原子写 + 重启恢复 + 上限。

用 TestClient 打 HTTP 端点；会话文件重定向到临时目录（monkeypatch _session_file）。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import llm_server as L  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="pcagent_sess_")
L._session_file = lambda: Path(_TMP) / "sessions.json"
L._sessions = {}
L._sessions_loaded = True

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


client = TestClient(L.app)

# ============ 1. 创建与列表 ============
print("== 1. 创建 / 列表 ==")
r = client.post("/api/v1/sessions")
check("创建会话", r.status_code == 200 and r.json().get("id") == 1, r.text)
r = client.post("/api/v1/sessions")
sid2 = r.json().get("id")
check("创建第二个会话", r.status_code == 200 and sid2 == 2, r.text)

r = client.get("/api/v1/sessions")
data = r.json()
check("列表含 2 个会话", r.status_code == 200 and len(data["sessions"]) == 2, str(data))
check("新会话消息为空", data["sessions"][0]["message_count"] == 0, str(data))

# ============ 2. 追加与自动标题 ============
print("== 2. 追加消息 / 自动标题 ==")
r = client.post("/api/v1/sessions/1/messages", json={
    "messages": [
        {"role": "user", "content": "帮我写一个斐波那契函数，要求递归实现"},
        {"role": "assistant", "content": "好的，代码如下：```python\ndef fib(n): ...```"},
    ]})
check("追加成功", r.status_code == 200, r.text)
s = r.json()["session"]
check("自动标题", s["title"].startswith("帮我写一个斐波那契"), s.get("title"))
check("消息数=2", len(s["messages"]) == 2, str(len(s["messages"])))

r = client.post("/api/v1/sessions/1/messages", json={
    "messages": [{"role": "user", "content": "再解释一下"}]})
check("再次追加", r.status_code == 200 and len(r.json()["session"]["messages"]) == 3, r.text)
check("标题不再覆盖", r.json()["session"]["title"].startswith("帮我写一个斐波那契"), "")

r = client.post("/api/v1/sessions/1/messages", json={"messages": []})
check("空追加 422", r.status_code == 422, r.text)

r = client.post("/api/v1/sessions/999/messages", json={
    "messages": [{"role": "user", "content": "x"}]})
check("追加不存在会话 404", r.status_code == 404, r.text)

# system 消息不落盘（只存 user/assistant 模型上下文）
r = client.post("/api/v1/sessions/1/messages", json={
    "messages": [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "hi"}]})
msgs = r.json()["session"]["messages"]
check("system 不落盘", all(m["role"] != "system" for m in msgs), str(msgs))

# ============ 3. 读取 / 删除 ============
print("== 3. 读取 / 删除 ==")
r = client.get("/api/v1/sessions/1")
check("读取单会话", r.status_code == 200 and len(r.json()["session"]["messages"]) == 4, r.text)
r = client.get("/api/v1/sessions/99")
check("读取不存在 404", r.status_code == 404, r.text)

r = client.delete("/api/v1/sessions/2")
check("删除会话", r.status_code == 200, r.text)
r = client.get("/api/v1/sessions")
check("删除后剩 1 个", len(r.json()["sessions"]) == 1, "")
r = client.delete("/api/v1/sessions/2")
check("重复删除 404", r.status_code == 404, r.text)

# ============ 4. 持久化与重启恢复 ============
print("== 4. 重启恢复 ==")
sess_file = Path(_TMP) / "sessions.json"
check("文件已写盘", sess_file.exists(), str(sess_file))

# 模拟重启：清内存 + 重新加载
L._sessions = {}
L._sessions_loaded = False
L._load_sessions()
r = client.get("/api/v1/sessions")
data = r.json()
check("重启后会话恢复", len(data["sessions"]) == 1 and data["sessions"][0]["id"] == 1, str(data))
check("重启后消息恢复", data["sessions"][0]["message_count"] == 4, str(data))

# 新创建会话 id 不冲突（计数器从磁盘恢复：磁盘最大 id 为 1）
r = client.post("/api/v1/sessions")
check("重启后新建 id 递增", r.json().get("id") == 2, r.text)

# 清空消息（/clear 语义）：保留会话，消息与标题清空
r = client.delete("/api/v1/sessions/1/messages")
check("清空消息", r.status_code == 200 and r.json().get("cleared") == 1, r.text)
r = client.get("/api/v1/sessions/1")
s = r.json()["session"]
check("清空后消息=0 标题空", len(s["messages"]) == 0 and not s["title"], str(s))
r = client.delete("/api/v1/sessions/99/messages")
check("清空不存在会话 404", r.status_code == 404, r.text)

# ============ 5. 上限保护 ============
print("== 5. 会话数/消息数上限 ==")
L.SESSION_MAX = 3
L.SESSION_MAX_MESSAGES = 5
r = client.post("/api/v1/sessions")
check("第三个会话可建", r.status_code == 200, r.text)
r = client.post("/api/v1/sessions")
check("超上限 409", r.status_code == 409, r.text)

msgs = [{"role": "user", "content": f"消息{i}"} for i in range(8)]
r = client.post("/api/v1/sessions/3/messages", json={"messages": msgs})
got = r.json()["session"]["messages"]
check("消息超限截断", r.status_code == 200 and len(got) == 5, str(len(got)))
check("保留最新丢弃最早", got[0]["content"] == "消息3", got[0]["content"])

# ============ 汇总 ============
print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
