"""Git 提交范围测试：默认只提交本轮修改 / 无关改动不被提交 / 快照变化拒绝 / files 越界。"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
os.environ.setdefault("PCAGENT_ALLOW_TEST_HOST", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import llm_server as L  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


_TMP = tempfile.mkdtemp(prefix="pcagent_gitc_")
WS = Path(_TMP)
L._get_workspace = lambda: WS
L._agent_modified_files = set()
L._pending_git_snapshot = ""

repo = WS / "repo"
repo.mkdir()
subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], capture_output=True)
subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], capture_output=True)
subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], capture_output=True)


def run(name, arguments: str):
    return L._execute_tool(name, arguments)


def ok_json(result: str) -> dict:
    return json.loads(result)


def git(*args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# ============ 1. 默认只提交本轮修改的文件 ============
print("== 1. 默认提交范围 ==")
# 用户（外部）放了无关文件 b.txt
(repo / "b.txt").write_text("user-file\n", encoding="utf-8")
# Agent 修改 a.txt（记录到本轮修改）
(repo / "a.txt").write_text("v1\n", encoding="utf-8")
ok, res = run("replace_text", json.dumps({"file": "repo/a.txt", "old": "v1", "new": "v2"}))
check("replace_text 记录修改", ok and L._agent_modified_files == {"repo/a.txt"}, str(L._agent_modified_files))

ok, res = run("git_commit", json.dumps({"path": "repo", "message": "agent change"}))
d = ok_json(res)
check("默认提交本轮修改成功", ok and d.get("committed"), res[:120])
check("提交的文件仅 a.txt", d.get("files") == ["a.txt"], str(d.get("files")))
rc, out = git("log", "--oneline")
check("提交只含 agent 修改", "agent change" in out, out)
rc, out = git("show", "--stat", "--oneline", "HEAD")
check("b.txt 未被提交（无关改动保留）", "b.txt" not in out and "a.txt" in out, out[:200])
rc, out = git("status", "--short")
check("b.txt 仍为未跟踪", "b.txt" in out, out)
check("提交后本轮记录清空", L._agent_modified_files == set(), str(L._agent_modified_files))

# ============ 2. 显式 files 提交 ============
print("== 2. 显式 files ==")
(repo / "c.txt").write_text("c1\n", encoding="utf-8")
ok, res = run("git_commit", json.dumps({"path": "repo", "message": "explicit",
                                        "files": ["repo/c.txt"]}))
d = ok_json(res)
check("显式 files 提交", ok and d.get("files") == ["c.txt"], str(d))
rc, out = git("status", "--short")
check("b.txt 仍未被提交", "b.txt" in out, out)

# files 越界路径拒绝
ok, res = run("git_commit", json.dumps({"path": "repo", "message": "x",
                                        "files": ["../outside.txt"]}))
check("files 越界被忽略（无目标）", not ok and "没有可提交" in res, res[:120])

# ============ 3. 快照变化拒绝 ============
print("== 3. 快照变化拒绝 ==")
(repo / "a.txt").write_text("v3\n", encoding="utf-8")
ok, res = run("replace_text", json.dumps({"file": "repo/a.txt", "old": "v3", "new": "v4"}))
# 模拟：确认时记录了快照，随后外部修改了已跟踪文件（porcelain 变化）
L._pending_git_snapshot = L._git_worktree_snapshot(repo)
(repo / "a.txt").write_text("EXTERNAL-CHANGE\n", encoding="utf-8")   # 外部改动（已跟踪文件）
ok, res = run("git_commit", json.dumps({"path": "repo", "message": "should fail"}))
check("工作树变化后提交被拒绝", not ok and "重新确认" in res, res[:120])
check("拒绝后快照清除", L._pending_git_snapshot == "", "")
# 清理外部改动（b.txt 提交掉以便后续干净）——不提交，直接忽略；重置快照态
L._pending_git_snapshot = ""
ok, res = run("git_commit", json.dumps({"path": "repo", "message": "retry"}))
check("快照为空时正常提交", ok and ok_json(res).get("committed"), res[:120])

# ============ 4. 确认预览包含完整文件列表 ============
print("== 4. 确认预览 ==")
(repo / "a.txt").write_text("v5\n", encoding="utf-8")
ok, res = run("replace_text", json.dumps({"file": "repo/a.txt", "old": "v5", "new": "v6"}))
q, diff = L._confirm_question("git_commit", {"path": "repo", "message": "preview test"})
check("预览含文件列表", "a.txt" in q and "将要提交的文件" in q, q[:200])
check("预览含状态", "当前状态" in q, q[:200])
check("未跟踪文件在预览可见", "b.txt" in q, q[:200])

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
