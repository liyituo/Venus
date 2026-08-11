"""服务鉴权测试：token 常量时间比较 / Host 限制 / 非 loopback 强制 token / daemon 端点鉴权。"""
import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
os.environ.setdefault("PCAGENT_ALLOW_TEST_HOST", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as daemon_mod    # noqa: E402
import llm_server as L      # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


# ============ 1. 非 loopback 强制 token ============
print("== 1. 非 loopback 强制 token ==")
check("127.0.0.1 是回环", L._is_loopback("127.0.0.1"), "")
check("localhost 是回环", L._is_loopback("localhost"), "")
check("0.0.0.0 非回环（须 token）", not L._is_loopback("0.0.0.0"), "")
check("192.168.x 非回环（须 token）", not L._is_loopback("192.168.1.10"), "")

# ============ 2. llm_server token 鉴权 ============
print("== 2. llm_server token ==")
_orig_token = L.AUTH_TOKEN
L.AUTH_TOKEN = "secret-token-1"
client = TestClient(L.app)
r = client.get("/api/v1/health")
check("无 token 401", r.status_code == 401, str(r.status_code))
r = client.get("/api/v1/health", headers={"X-Api-Token": "wrong"})
check("错误 token 401", r.status_code == 401, str(r.status_code))
r = client.get("/api/v1/health", headers={"X-Api-Token": "secret-token-1"})
check("正确 token 200", r.status_code == 200, str(r.status_code))
r = client.post("/api/v1/sessions", headers={"X-Api-Token": "secret-token-1"})
check("敏感接口（会话创建）受保护", r.status_code == 200, str(r.status_code))
r = client.delete("/api/v1/sessions/999", headers={"X-Api-Token": "secret-token-1"})
check("会话删除受保护且 404", r.status_code == 404, str(r.status_code))
L.AUTH_TOKEN = _orig_token

# ============ 3. Host 限制 ============
print("== 3. Host 限制 ==")
client2 = TestClient(L.app)
r = client2.get("/api/v1/health", headers={"Host": "evil.example.com"})
check("恶意 Host 403", r.status_code == 403, str(r.status_code))
r = client2.get("/api/v1/health", headers={"Host": "127.0.0.1:8001"})
check("本机 Host 放行", r.status_code == 200, str(r.status_code))
r = client2.get("/api/v1/health", headers={"Host": "localhost:8001"})
check("localhost Host 放行", r.status_code == 200, str(r.status_code))
r = client2.get("/api/v1/health", headers={"Origin": "http://evil.com"})
check("恶意 Origin 403", r.status_code == 403, str(r.status_code))
r = client2.get("/api/v1/health", headers={"Origin": "http://127.0.0.1:8001"})
check("本机 Origin 放行", r.status_code == 200, str(r.status_code))
r = client2.get("/api/v1/health", headers={"Origin": "http://localhost.evil.example"})
check("localhost.evil 欺骗拒绝", r.status_code == 403, str(r.status_code))
r = client2.get("/api/v1/health", headers={"Origin": "http://127.0.0.1.evil.example"})
check("127.0.0.1.evil 欺骗拒绝", r.status_code == 403, str(r.status_code))
r = client2.get("/api/v1/health", headers={"Origin": "http://evil.com:8001"})
check("异源端口拒绝", r.status_code == 403, str(r.status_code))
r = client2.get("/api/v1/health", headers={"Origin": "http://localhost:8001"})
check("localhost 端口放行", r.status_code == 200, str(r.status_code))

# ============ 4. daemon（app.py）端点鉴权 ============
print("== 4. daemon 端点鉴权 ==")
_orig_daemon_token = daemon_mod.AUTH_TOKEN
daemon_mod.AUTH_TOKEN = "daemon-secret"
with TestClient(daemon_mod.app) as client3:
    r = client3.get("/api/v1/status")
    check("daemon 查询端点免认证", r.status_code == 200, str(r.status_code))
    r = client3.post("/api/v1/execute",
                     json={"action": "screenshot"}, headers={"X-Api-Token": "daemon-secret"})
    check("daemon 控制端点带 token 200", r.status_code == 200, str(r.status_code))
    r = client3.post("/api/v1/execute", json={"action": "screenshot"})
    check("daemon 控制端点无 token 401", r.status_code == 401, str(r.status_code))
    r = client3.post("/api/v1/execute",
                     json={"action": "screenshot"}, headers={"X-Api-Token": "wrong"})
    check("daemon 控制端点错误 token 401", r.status_code == 401, str(r.status_code))
    r = client3.post("/api/v1/stop", headers={"X-Api-Token": "daemon-secret"})
    check("daemon stop 带 token", r.status_code == 200, str(r.status_code))
    # 恢复
    daemon_mod.AUTH_TOKEN = _orig_daemon_token
    r = client3.post("/api/v1/reset")
    check("token 关闭后恢复免认证", r.status_code == 200, str(r.status_code))
daemon_mod.AUTH_TOKEN = _orig_daemon_token

# ============ 5. 常量时间比较 ============
print("== 5. 常量时间比较 ==")
L.AUTH_TOKEN = "compare-me"
req = mock.Mock()
req.headers = {"X-Api-Token": "compare-me"}
check("正确 token 通过", L._token_ok(req) is True, "")
req.headers = {"X-Api-Token": "compare-me-extra"}
check("错误 token 拒绝", L._token_ok(req) is False, "")
L.AUTH_TOKEN = ""

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
