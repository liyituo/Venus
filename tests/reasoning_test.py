"""推理强度档位测试：_apply_reasoning 映射 + config 端点读写 + health 展示。
import os
os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")

直接调用 _apply_reasoning（纯函数）；config 端点走 TestClient，
CONFIG_PATH 重定向到临时文件，绝不触碰真实 chat_config.json。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import llm_server as L  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="pcagent_reason_")
L.CONFIG_PATH = Path(_TMP) / "chat_config.json"
L.CONFIG_PATH.write_text(json.dumps({
    "api_url": "https://api.deepseek.com/v1/chat/completions",
    "api_key": "sk-test",
    "model": "deepseek-v4-flash",
    "reasoning_mode": "max",
}), encoding="utf-8")

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}  {detail}")


# ============ 1. _apply_reasoning 映射 ============
print("== 1. _apply_reasoning 档位映射 ==")
p = {}
L._apply_reasoning(p, "max")
check("max → reasoning_effort=max", p == {"reasoning_effort": "max"}, p)
p = {}
L._apply_reasoning(p, "high")
check("high → reasoning_effort=high", p == {"reasoning_effort": "high"}, p)
p = {}
L._apply_reasoning(p, "off")
check("off → thinking disabled", p == {"thinking": {"type": "disabled"}}, p)
check("off 不带 reasoning_effort", "reasoning_effort" not in p, p)
p = {}
L._apply_reasoning(p, "bogus")
check("非法档位回退 max", p == {"reasoning_effort": "max"}, p)
p = {}
L._apply_reasoning(p)
check("默认档位跟随配置(max)", p == {"reasoning_effort": "max"}, p)
p = {"model": "x", "temperature": 0.7}
L._apply_reasoning(p, "off")
check("不覆盖已有字段", p["model"] == "x" and p["temperature"] == 0.7, p)

# ============ 2. config 端点读写 ============
print("== 2. config 端点读写 ==")
c = TestClient(L.app)
r = c.get("/api/v1/config")
cfg = r.json()["config"]
check("GET 返回 reasoning_mode", r.status_code == 200 and cfg.get("reasoning_mode") == "max", r.text)
r = c.post("/api/v1/config", json={"reasoning_mode": "off"})
check("POST 更新 off",
      r.status_code == 200 and r.json()["config"]["reasoning_mode"] == "off", r.text)
check("配置文件已持久化",
      json.loads(L.CONFIG_PATH.read_text(encoding="utf-8"))["reasoning_mode"] == "off", "")
r = c.post("/api/v1/config", json={"reasoning_mode": "ultra"})
check("非法档位 422", r.status_code == 422, r.text)
r = c.post("/api/v1/config", json={})
check("空更新 422", r.status_code == 422, r.text)
r = c.post("/api/v1/config", json={"reasoning_mode": "high"})
check("回切 high",
      r.status_code == 200 and r.json()["config"]["reasoning_mode"] == "high", r.text)

# ============ 3. health 展示 ============
print("== 3. health 展示 ==")
r = c.get("/api/v1/health")
check("health 含 reasoning_mode",
      r.status_code == 200 and r.json().get("reasoning_mode") == "high", r.text)

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
