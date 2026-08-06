"""llm_server 编程工具冒烟测试：检索/编辑/git/进程/todo/repo_map 分支逻辑。

直接调用 _execute_tool（纯函数），workspace 指向临时目录，不碰真实文件系统。
不触发上游 API 调用（不测试 _agent_loop 循环本身）。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import llm_server as L  # noqa: E402

# ---- 把工作区重定向到临时目录 ----
_TMP = tempfile.mkdtemp(prefix="pcagent_tools_")
WS = Path(_TMP)
L._get_workspace = lambda: WS
L._todos = []                 # 清空内存任务清单
L._todos_loaded = True

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


def run(name, arguments: str):
    """调用 _execute_tool，返回 (ok, result)。"""
    return L._execute_tool(name, arguments)


def ok_json(result: str) -> dict:
    return json.loads(result)


# ============ 1. 检索 ============
print("== 1. search_text / glob_files / list_symbols ==")
(WS / "demo").mkdir(parents=True, exist_ok=True)
(WS / "demo" / "fib.py").write_text(
    "import sys\n\n\ndef fib(n):\n    \"\"\"fib\"\"\"\n    if n < 2:\n        return n\n"
    "    return fib(n - 1) + fib(n - 2)\n\n\nclass Fibber:\n    pass\n",
    encoding="utf-8")
(WS / "demo" / "notes.md").write_text("todo list\n# fib demo\n", encoding="utf-8")
(WS / ".venv").mkdir(exist_ok=True)
(WS / ".venv" / "junk.py").write_text("secret = 'skip me'\n", encoding="utf-8")

ok, res = run("search_text", json.dumps({"pattern": "def fib"}))
data = ok_json(res)
check("search_text 命中", ok and data.get("matches") == 1, res)
check("search_text 命中行号", ok and "fib.py:4:" in data.get("results", ""), res)
check("search_text 跳过 .venv", ok and "junk" not in data.get("results", ""), res)

ok, res = run("search_text", json.dumps({"pattern": "fib", "file_pattern": "*.md"}))
data = ok_json(res)
check("search_text 文件过滤", ok and data.get("matches") == 1 and "notes.md" in data.get("results", ""), res)

ok, res = run("search_text", json.dumps({"pattern": "nonexistent_xyz"}))
data = ok_json(res)
check("search_text 无命中", ok and data.get("matches") == 0, res)

ok, res = run("search_text", json.dumps({"pattern": ""}))
check("search_text 空 pattern 拒绝", not ok, res)

ok, res = run("glob_files", json.dumps({"pattern": "*.py"}))
data = ok_json(res)
paths = [f["path"] for f in data.get("files", [])]
check("glob_files 匹配", ok and "demo/fib.py" in paths, str(paths))
check("glob_files 跳过 .venv", ok and not any("junk" in p for p in paths), str(paths))

ok, res = run("list_symbols", json.dumps({"file": "demo/fib.py"}))
data = ok_json(res)
syms = data.get("symbols", [])
check("list_symbols def", ok and any("fib" in s and s.startswith("4:") for s in syms), str(syms))
check("list_symbols class", ok and any("Fibber" in s for s in syms), str(syms))

ok, res = run("list_symbols", json.dumps({"file": "../escape.py"}))
check("list_symbols 越界路径拒绝", not ok, res)

# ============ 2. 精确编辑 ============
print("== 2. replace_text（diff 生成）==")
ok, res = run("replace_text", json.dumps({
    "file": "demo/fib.py",
    "old": "return fib(n - 1) + fib(n - 2)",
    "new": "return fib(n - 2) + fib(n - 1)"}))
data = ok_json(res)
check("replace_text 执行成功", ok, res)
check("replace_text 返回 diff", ok and "+    return fib(n - 2)" in data.get("diff", ""), data.get("diff", "")[:100])
check("replace_text 落盘生效", ok and "fib(n - 2) + fib(n - 1)" in (WS / "demo" / "fib.py").read_text(encoding="utf-8"), "")

ok, res = run("replace_text", json.dumps({"file": "demo/fib.py", "old": "不存在的文本", "new": "x"}))
check("replace_text 未找到报错", not ok and "未找到" in res, res)

ok, res = run("replace_text", json.dumps({"file": "demo/fib.py", "old": "fib(n - 2) + fib(n - 1)", "new": "x", "occurrence": 9}))
check("replace_text occurrence 越界", not ok, res)

ok, res = run("replace_text", json.dumps({"file": "demo/../nope.py", "old": "a", "new": "b"}))
check("replace_text 越界路径拒绝", not ok, res)

# ============ 3. Git 闭环 ============
print("== 3. git_status / git_diff / git_commit / git_log ==")
repo = WS / "repo1"
repo.mkdir(exist_ok=True)
r = subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], capture_output=True)
check("git init 前置", r.returncode == 0, r.stderr.decode(errors="replace")[:200])
subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@test"], capture_output=True)
subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], capture_output=True)
(repo / "a.txt").write_text("hello\n", encoding="utf-8")

ok, res = run("git_status", json.dumps({"path": "repo1"}))
check("git_status 显示变更", ok and "??" in res and "a.txt" in res, res)

ok, res = run("git_diff", json.dumps({"path": "repo1"}))
check("git_diff 无改动（未跟踪）", ok and "无改动" in res, res)

ok, res = run("git_commit", json.dumps({"path": "repo1", "message": "first commit"}))
check("git_commit 成功", ok and "committed" in ok_json(res), res)

ok, res = run("git_log", json.dumps({"path": "repo1", "n": 5}))
check("git_log 有提交", ok and "first commit" in res, res)

ok, res = run("git_diff", json.dumps({"path": "repo1"}))
check("git_diff 提交后无改动", ok and "无改动" in res, res)

# 仓库根越界：在工作区外建仓库
outside = Path(tempfile.mkdtemp(prefix="pcagent_out_"))
subprocess.run(["git", "init", "-q", str(outside)], capture_output=True)
(outside / "x.txt").write_text("x\n", encoding="utf-8")
ok, res = run("git_status", json.dumps({"path": ""}))
# 工作区内没有仓库 -> 拒绝（_find_git_root 找不到或越界）
check("git 工作区外仓库拒绝", not ok, res)

# ============ 4. 后台进程 ============
print("== 4. start_process / process_output / stop_process ==")
py = sys.executable
code = "import time; print('started', flush=True); time.sleep(60)"
ok, res = run("start_process", json.dumps({"command": f'"{py}" -u -c "{code}"', "cwd": "."}))
check("start_process 启动", ok and "pid" in ok_json(res), res)
pid = ok_json(res).get("pid") if ok else 0
time.sleep(0.8)

ok, res = run("process_output", json.dumps({"pid": pid}))
data = ok_json(res)
check("process_output 有输出", ok and "started" in data.get("output", ""), res)
check("process_output 运行中", ok and data.get("running") is True, res)

ok, res = run("stop_process", json.dumps({"pid": pid}))
check("stop_process 停止", ok, res)
time.sleep(0.5)
ok, res = run("process_output", json.dumps({"pid": pid}))
data = ok_json(res)
check("停止后 running=False", ok and data.get("running") is False, res)

ok, res = run("stop_process", json.dumps({"pid": 999999}))
check("stop_process 未知 pid", not ok, res)

ok, res = run("list_processes", "{}")
check("list_processes 可用", ok and "processes" in ok_json(res), res)

# 危险命令拦截
ok, res = run("start_process", json.dumps({"command": "rm -rf /etc"}))
check("start_process 黑名单拦截", not ok and "拦截" in res, res)

# ============ 4.5 超时 debug ============
print("== 4.5 run_shell / run_code 超时 debug ==")
L.RUN_SHELL_TIMEOUT = 2
cmd = f'{py} -c "import time; print(\'progress-x\', flush=True); time.sleep(10)"'
ok, res = run("run_shell", json.dumps({"command": cmd}))
data = ok_json(res)
check("run_shell 超时返回现场", not ok and "超时" in data.get("error", ""), res[:150])
check("run_shell 带回部分输出", "progress-x" in data.get("partial_stdout", ""), str(data)[:200])
check("run_shell 超时 hint 建议", "start_process" in data.get("hint", ""), "")

L.RUN_CODE_TIMEOUT = 2
ok, res = run("run_code", json.dumps({"code": "import time; print('code-progress', flush=True); time.sleep(10)"}))
data = ok_json(res)
check("run_code 超时返回现场", not ok and "超时" in data.get("error", ""), res[:150])
check("run_code 带回部分输出", "code-progress" in data.get("partial_stdout", ""), str(data)[:200])
L.RUN_SHELL_TIMEOUT = 30
L.RUN_CODE_TIMEOUT = 30

# ============ 4.6 Windows shell 适配（平台相关）============
print("== 4.6 shell 平台适配 ==")
check("只读判定 Linux 命令", L._is_readonly_shell("ls -la"), "")
check("重定向视为写", not L._is_readonly_shell("echo hi > f.txt"), "")
if sys.platform == "win32":
    check("Windows 只读命令免确认", L._is_readonly_shell("dir /b"), "")
    check("Windows 只读命令2", L._is_readonly_shell("tasklist"), "")
else:
    check("Linux 环境 dir 不算只读（cmd 专有）", not L._is_readonly_shell("dir"), "")
check("黑名单: format C:", bool([p for p in L.DANGEROUS_PATTERNS if re.search(p, "format C:")]), "")
check("黑名单: rd /s /q C:\\", bool([p for p in L.DANGEROUS_PATTERNS
                                     if re.search(p, "rd /s /q C:\\")]), "")
check("黑名单: diskpart", bool([p for p in L.DANGEROUS_PATTERNS
                                if re.search(p, "diskpart")]), "")
check("黑名单: reg add", bool([p for p in L.DANGEROUS_PATTERNS
                               if re.search(p, "reg add HKLM")]), "")
check("正常命令不受影响", not any(re.search(p, "dir /b") for p in L.DANGEROUS_PATTERNS), "")

# ============ 5. 任务规划 ============
print("== 5. create_todo / update_todo / list_todos ==")
L._todos = []
L._todos_loaded = True
ok, res = run("create_todo", json.dumps({"title": "修复登录 bug", "description": "表单校验"}))
data = ok_json(res)
check("create_todo 创建", ok and data.get("created", {}).get("status") == "pending", res)
tid = data.get("created", {}).get("id")

ok, res = run("create_todo", json.dumps({"title": ""}))
check("create_todo 空标题拒绝", not ok, res)

ok, res = run("update_todo", json.dumps({"id": tid, "status": "in_progress"}))
check("update_todo 更新", ok and any(t.get("status") == "in_progress" for t in ok_json(res).get("todos", [])), res)

ok, res = run("update_todo", json.dumps({"id": tid, "status": "bogus"}))
check("update_todo 非法状态拒绝", not ok, res)

ok, res = run("update_todo", json.dumps({"id": 999, "status": "completed"}))
check("update_todo 未知 id 拒绝", not ok, res)

ok, res = run("list_todos", "{}")
data = ok_json(res)
check("list_todos 列表", ok and len(data.get("todos", [])) == 1, res)

# 持久化 + 半恢复注入
todo_file = WS / ".pcagent" / "todos.json"
check("todo 持久化文件", todo_file.exists(), str(todo_file))
note = L._todos_system_note()
check("todo 注入 system", note and "修复登录 bug" in note, note[:100])

# ============ 6. 项目索引 ============
print("== 6. repo_map ==")
ok, res = run("repo_map", json.dumps({"depth": 3}))
check("repo_map 输出结构", ok and "fib.py" in res, res[:200])
check("repo_map 含符号", ok and "fib" in res, res[:200])
check("repo_map 跳过 .venv", ok and "junk" not in res, "")

ok, res = run("repo_map", json.dumps({"path": "不存在目录"}))
check("repo_map 目录不存在", not ok or "不存在" in res, res)

# 缓存生效（第二次立即返回）
t0 = time.monotonic()
run("repo_map", json.dumps({"depth": 3}))
check("repo_map 缓存", time.monotonic() - t0 < 0.5, "")

# ============ 汇总 ============
print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
