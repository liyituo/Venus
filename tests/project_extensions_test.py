"""项目模式与扩展注册表测试。"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
os.environ.setdefault("PCAGENT_ALLOW_TEST_HOST", "1")

_TMP = tempfile.mkdtemp(prefix="pcagent_proj_")
os.environ["PCAGENT_DATA_DIR"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import extension_registry as ext  # noqa: E402
import project_store as ps  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}", flush=True)
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}", flush=True)


print("== project_store ==", flush=True)
ok, created = ps.create_project("测试项目", "完成阶段0和1", [{"title": "扩展框架"}])
check("create_project", ok is True, str(created))
pid = created["created"]["id"] if ok else ""
ok2, _ = ps.add_milestone(pid, "里程碑二")
check("add_milestone", ok2, "")
ok3, _ = ps.save_checkpoint(pid, "已完成基础模块", "接入 API", "")
check("save_checkpoint", ok3, "")
note = ps.project_system_note(pid)
check("project_system_note 含标题", "测试项目" in note, note[:120])
ps.set_active_project(pid)
check("set_active", ps.get_active_project_id() == pid, ps.get_active_project_id())

print("== extension_registry ==", flush=True)
cat = ext.load_catalog()
check("catalog 非空", len(cat) >= 2, str(len(cat)))
listed = ext.list_extensions()
check("list_extensions ok", listed.get("ok") is True, str(listed))
ok_i, msg_i = ext.install_plugin("daily-brief")
check("install daily-brief", ok_i, msg_i)
ok_e, msg_e = ext.enable_plugin("daily-brief")
check("enable daily-brief", ok_e, msg_e)
skill = Path(__file__).resolve().parent.parent / "skills" / "daily-brief" / "SKILL.md"
check("skill 已部署", skill.exists(), str(skill))

print("== llm_server api ==", flush=True)
import llm_server as L  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(L.app)
r = client.get("/api/v1/projects")
check("GET /projects", r.status_code == 200 and r.json().get("ok"), r.text[:200])
r = client.post("/api/v1/projects/active", json={"project_id": pid})
check("POST /projects/active", r.status_code == 200, r.text[:200])
r = client.get("/api/v1/extensions")
check("GET /extensions", r.status_code == 200 and "extensions" in r.json(), r.text[:200])
ok, out = L._execute_tool("list_projects", "{}")
check("tool list_projects", ok, out[:120])
ok, out = L._execute_tool("create_project", json.dumps({
    "title": "工具创建", "goal": "via tool", "milestones": [{"title": "一步"}],
}))
check("tool create_project", ok, out[:120])

print(f"\n{'=' * 40}\n  {passed} passed, {failed} failed\n{'=' * 40}", flush=True)
sys.exit(1 if failed else 0)
