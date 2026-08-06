"""临时冒烟测试：验证队列架构的 busy/queued 计数、止停取消排队任务、
import os
os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
执行中任务自然完成、坐标校验、中文输入分段等。不执行真实鼠标/键盘动作。"""
import sys
import threading
import time
from pathlib import Path

import pyautogui

# 打桩：所有 GUI 副作用替换为无害操作；click 延迟 0.6s 模拟长任务
pyautogui.click = lambda *a, **k: time.sleep(0.6)
pyautogui.write = lambda *a, **k: None
pyautogui.press = lambda *a, **k: None
pyautogui.hotkey = lambda *a, **k: None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as daemon  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

passed = failed = 0

def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name} {extra}")

with TestClient(daemon.app) as c:
    print("== 1. 基础状态 ==")
    s = c.get("/api/v1/status").json()
    check("初始 idle", s["mode"] == "idle" and s["is_busy"] is False and s["queued"] == 0, s)
    check("首页 200", c.get("/").status_code == 200)

    print("== 2. 并发执行 + busy/queued 计数 ==")
    results = {}
    def run(i):
        results[i] = c.post("/api/v1/execute", json={"action": "click", "x": 100, "y": 100})
    ts = [threading.Thread(target=run, args=(i,)) for i in range(3)]
    [t.start() for t in ts]
    time.sleep(0.25)                       # 第一个已进入执行(0.6s)，后两个排队
    s = c.get("/api/v1/status").json()
    check("执行中 busy + current_action=click", s["is_busy"] and s["current_action"] == "click", s)
    check("排队 2 个任务", s["queued"] == 2, s)

    print("== 3. 止停: 取消排队任务, 执行中的自然完成 ==")
    r = c.post("/api/v1/stop").json()
    check("stop 返回取消 2 个", r["canceled_pending"] == 2, r)
    [t.join() for t in ts]
    codes = sorted(results[i].status_code for i in results)  # 提交顺序由线程调度决定
    check("恰有一个执行中的任务自然完成 200", codes == [200, 409, 409], codes)
    check("两个排队任务被取消 409", sum(1 for i in results if results[i].status_code == 409) == 2, codes)

    print("== 4. 止停期间拒绝/恢复 ==")
    s = c.get("/api/v1/status").json()
    check("状态 stopped", s["mode"] == "stopped" and s["stop_requested"], s)
    check("止停时 execute 拒绝 423",
          c.post("/api/v1/execute", json={"action": "screenshot"}).status_code == 423)
    check("reset 恢复", c.post("/api/v1/reset").json()["stop_requested"] is False)
    s = c.get("/api/v1/status").json()
    check("恢复后 idle", s["mode"] == "idle" and s["queued"] == 0, s)

    print("== 5. 参数校验 ==")
    check("x 无 y → 422", c.post("/api/v1/execute", json={"action": "click", "x": 10}).status_code == 422)
    check("坐标越界 → 422",
          c.post("/api/v1/execute", json={"action": "click", "x": 99999, "y": 99999}).status_code == 422)
    check("未知按键 → 422",
          c.post("/api/v1/execute", json={"action": "press_key", "key": "notakey"}).status_code == 422)
    check("非法 button → 422",
          c.post("/api/v1/execute", json={"action": "click", "x": 10, "y": 10, "button": "side"}).status_code == 422)
    check("组合键 ctrl+c → 200 (已打桩)",
          c.post("/api/v1/execute", json={"action": "press_key", "key": "ctrl+c"}).status_code == 200)

    print("== 6. 真截图（无害）==")
    r = c.post("/api/v1/execute", json={"action": "screenshot"})
    check("execute screenshot 200 + base64", r.status_code == 200 and r.json().get("screenshot_base64"))
    r = c.get("/api/v1/screenshot")
    check("GET screenshot image/jpeg",
          r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and len(r.content) > 10000)

print("== 7. 中文输入分段逻辑（纯函数）==")
runs = daemon._type_runs("你好 world！foo bar")
check("ASCII/中文正确分段", runs == [("clip", "你"), ("clip", "好"), ("keys", " world"), ("clip", "！"), ("keys", "foo bar")], runs)
runs = daemon._type_runs("Hello, 世界 🌍!")
check("emoji 走剪贴板", any(k == "clip" and v == "🌍" for k, v in runs), runs)
runs = daemon._type_runs("plain ascii 123")
check("纯 ASCII 全 keys", all(k == "keys" for k, _ in runs) and runs[0][1] == "plain ascii 123", runs)

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
