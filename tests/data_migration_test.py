"""数据目录迁移测试。"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("VENUS_DISABLE_MCP", "1")

_TMP = tempfile.mkdtemp(prefix="venus_migrate_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from data_migration import ensure_data_dir
from data_paths import workspace_data_dir

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}", flush=True)
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}", flush=True)


print("== data_migration ==", flush=True)
root = Path(_TMP) / "repo"
root.mkdir()
legacy = root / ".pcagent"
target = root / ".venus"
legacy.mkdir()
(legacy / "sessions.json").write_text('{"sessions":[]}', encoding="utf-8")

ok, msg = ensure_data_dir(legacy, target)
check("move legacy -> venus", ok and target.is_dir() and (target / "sessions.json").exists(), msg)
check("marker written", (target / ".migrated_from_pcagent").exists(), "")

ws = Path(_TMP) / "workspace"
ws.mkdir()
(ws / ".pcagent").mkdir()
(ws / ".pcagent" / "todos.json").write_text('{"todos":[]}', encoding="utf-8")
wd = workspace_data_dir(ws)
check("workspace_data_dir", (wd / "todos.json").exists(), str(wd))

print(f"\n{'=' * 40}\n  {passed} passed, {failed} failed\n{'=' * 40}", flush=True)
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(1 if failed else 0)
