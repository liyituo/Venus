"""浏览器工具与沙箱执行器测试（无真实 MCP/浏览器连接）。"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
os.environ.setdefault("PCAGENT_ALLOW_TEST_HOST", "1")

_TMP = tempfile.mkdtemp(prefix="pcagent_browser_sandbox_")
os.environ["PCAGENT_DATA_DIR"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import browser_tools as bt  # noqa: E402
import sandbox_runner as sr  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}", flush=True)
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}", flush=True)


print("== browser_tools ==", flush=True)
with tempfile.TemporaryDirectory() as td:
  mcp_path = Path(td) / "mcp_config.json"
  with mock.patch.object(bt, "_MCP_CONFIG", mcp_path):
    deps = bt.check_dependencies()
    check("check_dependencies 结构", isinstance(deps, dict) and "ready" in deps, str(deps))
    ok, msg = bt.enable_browser()
    check("enable_browser", ok, msg)
    check("mcp 已写入 chrome", "chrome" in bt.load_mcp_servers(), str(bt.load_mcp_servers().keys()))
    check("is_browser_enabled", bt.is_browser_enabled(), "")
    aliases = bt.alias_tool_definitions()
    check("别名工具数量", len(aliases) == 5, str(len(aliases)))
    called = {}

    def fake_mcp(name, args_json):
        called["name"] = name
        called["args"] = json.loads(args_json)
        return True, "ok"

    ok, out = bt.execute_alias("browser_open", {"url": "https://example.com"}, fake_mcp)
    check("browser_open 转发", ok and called.get("name") == "mcp_chrome_browser_navigate", str(called))
    check("browser_open url", called.get("args", {}).get("url") == "https://example.com", str(called))
    ok_d, _ = bt.disable_browser()
    check("disable_browser", ok_d, "")
    check("chrome 已移除", "chrome" not in bt.load_mcp_servers(), "")

print("== sandbox_runner ==", flush=True)
ws = Path(_TMP) / "workspace"
ws.mkdir(parents=True)
(ws / "sub").mkdir()
audit = Path(_TMP) / "sandbox_audit.jsonl"

with mock.patch.object(sr, "AUDIT_FILE", lambda: audit):
    ok, out = sr.run_sandboxed_shell(ws, "echo hello-sandbox", mode="workspace", timeout=30)
    check("run_sandboxed_shell echo", ok, out[:200])
    check("审计日志存在", audit.exists(), str(audit))
    bad, msg = sr.run_sandboxed_shell(ws, "rm -rf /", mode="workspace")
    check("危险命令拦截", not bad, msg[:120])
    bad_cwd, msg_cwd = sr.run_sandboxed_shell(ws, "echo x", mode="workspace", cwd_rel="../..")
    check("cwd 逃逸拒绝", not bad_cwd, msg_cwd[:120])
    ok_code, out_code = sr.run_sandboxed_code(ws, code="print(42)", mode="workspace", timeout=30)
    check("run_sandboxed_code", ok_code, out_code[:200])
    ok_mode, msg_mode = sr.set_sandbox_default("workspace")
    check("set_sandbox_default", ok_mode, msg_mode)
    check("get_sandbox_default", sr.get_sandbox_default() == "workspace", sr.get_sandbox_default())

print("== llm_server 集成 ==", flush=True)
import llm_server as L  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(L.app)
r = client.get("/api/v1/browser/status")
check("GET /browser/status", r.status_code == 200 and r.json().get("ok"), r.text[:200])
r = client.get("/api/v1/sandbox/status")
check("GET /sandbox/status", r.status_code == 200 and "default_mode" in r.json(), r.text[:200])
with mock.patch.object(bt, "is_browser_enabled", return_value=True):
    names = [t["function"]["name"] for t in L._agent_tools()]
    check("启用时注入 browser_*", "browser_snapshot" in names, str([n for n in names if n.startswith("browser_")]))

print(f"\n{'=' * 40}\n  {passed} passed, {failed} failed\n{'=' * 40}", flush=True)
sys.exit(1 if failed else 0)
