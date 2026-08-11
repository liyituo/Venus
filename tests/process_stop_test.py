"""后台进程停止回归测试：stop 后 wait/poll 验证、状态保留、不变成未知 PID、stop_failed。

Windows 与 Linux 均运行（taskkill / killpg 路径）。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

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


_TMP = tempfile.mkdtemp(prefix="pcagent_proc_")
WS = Path(_TMP)
L._get_workspace = lambda: WS
L._processes.clear()

py = sys.executable


def run(name, arguments: str):
    return L._execute_tool(name, arguments)


def ok_json(result: str) -> dict:
    return json.loads(result)


# ============ 1. 启动 → 停止 → 状态验证 ============
print("== 1. 启动 / 停止 / 状态 ==")
code = "import time; print('proc-start', flush=True); time.sleep(600)"
ok, res = run("start_process", json.dumps({"command": f'"{py}" -u -c "{code}"'}))
check("start_process 启动", ok and "pid" in ok_json(res), res)
pid = ok_json(res).get("pid")
time.sleep(0.8)

ok, res = run("process_output", json.dumps({"pid": pid}))
d = ok_json(res)
check("运行中可查输出", ok and d.get("running") is True and "proc-start" in d.get("output", ""), res[:150])

ok, res = run("stop_process", json.dumps({"pid": pid}))
d = ok_json(res)
check("stop_process 返回 stopped=True", ok and d.get("stopped") is True, res)
check("stop 后 exit_code 有效", ok and d.get("exit_code") is not None, res)
check("stop 后 stop_failed=False", ok and d.get("stop_failed") is False, res)

# 停止后 process_output 仍可查询（不变成未知 PID）
ok, res = run("process_output", json.dumps({"pid": pid}))
d = ok_json(res)
check("停止后 process_output running=False", ok and d.get("running") is False, res[:120])
check("停止后 exit_code 可查", ok and d.get("exit_code") is not None, res[:120])

# list_processes 保留 stopped 状态
ok, res = run("list_processes", "{}")
procs = {p["pid"]: p for p in ok_json(res).get("processes", [])}
entry = procs.get(pid)
check("list_processes 保留停止条目", entry is not None, str(list(procs)))
check("list_processes stopped=True", entry is not None and entry.get("stopped") is True, str(entry))
check("list_processes running=False", entry is not None and entry.get("running") is False, str(entry))

# ============ 2. 未知 pid 拒绝 ============
print("== 2. 未知 pid ==")
ok, res = run("stop_process", json.dumps({"pid": 999999}))
check("未知 pid 拒绝", not ok and "不存在" in res, res)
ok, res = run("process_output", json.dumps({"pid": 999999}))
check("未知 pid 输出拒绝", not ok, res)

# ============ 3. stop_failed：kill 后进程不退出（mock wait 超时）============
print("== 3. stop_failed 不谎报 ==")
L._processes.clear()
proc_fake = mock.Mock()
proc_fake.pid = 12345
proc_fake.stdout = mock.Mock()
proc_fake.poll = mock.Mock(return_value=None)        # 进程一直"运行中"
proc_fake.wait = mock.Mock(side_effect=subprocess.TimeoutExpired("cmd", 8))
L._processes[12345] = {"proc": proc_fake, "cmd": "sleep", "started": "00:00:00",
                       "started_ts": time.time(), "lines": [],
                       "stopped": False, "stop_failed": False, "exit_code": None}
ok, res = run("stop_process", json.dumps({"pid": 12345}))
d = ok_json(res)
check("超时返回 stopped=False", not ok and d.get("stopped") is False, res)
check("超时返回 stop_failed=True", not ok and d.get("stop_failed") is True, res)
check("条目标记 stop_failed", L._processes[12345].get("stop_failed") is True, "")
# 进程表保留该条目（可查询 stop_failed 状态）
ok, res = run("list_processes", "{}")
procs = {p["pid"]: p for p in ok_json(res).get("processes", [])}
check("stop_failed 条目保留可查", procs.get(12345, {}).get("stop_failed") is True, str(procs))
L._processes.clear()

# ============ 4. 自然退出进程保留状态（不立即变未知 PID）============
print("== 4. 自然退出状态保留 ==")
code2 = "print('quick-exit', flush=True)"
ok, res = run("start_process", json.dumps({"command": f'"{py}" -u -c "{code2}"'}))
pid2 = ok_json(res).get("pid")
time.sleep(1.2)   # 等进程自然退出
ok, res = run("process_output", json.dumps({"pid": pid2}))
d = ok_json(res)
check("自然退出 running=False", ok and d.get("running") is False, res[:120])
check("自然退出 exit_code 可查", ok and d.get("exit_code") is not None, res[:120])
ok, res = run("list_processes", "{}")
procs = {p["pid"]: p for p in ok_json(res).get("processes", [])}
check("自然退出条目保留", procs.get(pid2) is not None, str(list(procs)))
L._processes.clear()

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
