"""VenusChat V1 程序化调试台。

1. 接管 report_callback_exception，捕获所有 Tk 回调里的异常；
2. 遍历设置页全部 14 页 + 主对话流程（建会话、发消息、搜索、开关、分段、最大化…）；
3. 扫描可见控件树：文本被裁切、高度溢出、子控件越出父容器（Canvas 滚动容器除外）；
4. 每个关键状态截图到 .venus/ui-debug/。

运行：python scripts/debug_venuschat_v1.py
"""

from __future__ import annotations

import sys
import time
import ctypes
import traceback
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import tkinter as tk  # noqa: E402
from tkinter import font as tkfont  # noqa: E402
from PIL import ImageGrab  # noqa: E402

from venuschat_v1 import VenusChatV1, theme  # noqa: E402
from venuschat_v1 import widgets as W  # noqa: E402

OUT = ROOT / ".venus" / "ui-debug"
ERRORS: list[str] = []
FLAGS: list[str] = []


def pump(root: tk.Tk, times: int = 8) -> None:
    for _ in range(times):
        root.update()
        time.sleep(.03)


def shot(root: tk.Tk, name: str) -> None:
    pump(root)
    root.update_idletasks()
    import ctypes.wintypes as wt

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    r = RECT()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom)).save(OUT / f"{name}.png")
    print(f"  shot {name}")


def path_of(w) -> str:
    parts = []
    cur = w
    while cur is not None:
        name = cur.__class__.__name__
        txt = ""
        try:
            if isinstance(cur, (tk.Label, tk.Button)):
                t = cur.cget("text")
                if not t and isinstance(cur, tk.Label):
                    try:
                        t = str(cur.cget("textvariable").get())
                    except Exception:
                        t = ""
                if t:
                    txt = f"«{str(t)[:10]}»"
        except tk.TclError:
            pass
        parts.append(name + txt)
        cur = getattr(cur, "master", None)
    return "/".join(reversed(parts))


def scan(root: tk.Tk) -> None:
    def walk(w):
        try:
            kids = w.winfo_children()
        except tk.TclError:
            return
        for c in kids:
            try:
                if isinstance(c, tk.Toplevel):
                    continue      # 弹窗是独立顶层窗口，不参与父边界检查
                if c.winfo_ismapped():
                    if isinstance(c, tk.Label):
                        txt = str(c.cget("text"))
                        if not txt:
                            try:
                                txt = str(c.cget("textvariable").get())
                            except Exception:
                                txt = ""
                        if txt and "\n" not in txt:
                            fo = tkfont.Font(font=c.cget("font"))
                            padx = int(c.cget("padx"))
                            need = fo.measure(txt) + 2 * padx
                            have = c.winfo_width()
                            if not c.cget("wraplength") and need > have + 2:
                                FLAGS.append(
                                    f"横切  {path_of(c)}  need={need} have={have}")
                            if fo.metrics("linespace") > c.winfo_height() + 2:
                                FLAGS.append(
                                    f"纵溢  {path_of(c)}  h={c.winfo_height()}")
                    if not isinstance(w, (tk.Canvas, tk.Menu)):
                        pw, ph = w.winfo_width(), w.winfo_height()
                        cx = c.winfo_x() + c.winfo_width() - pw
                        cy = c.winfo_y() + c.winfo_height() - ph
                        if cx > 2 or cy > 2:
                            FLAGS.append(
                                f"越界  {path_of(c)}  dx={cx} dy={cy}")
                    walk(c)
            except tk.TclError:
                pass
    walk(root)


def run() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    theme.enable_dpi_awareness()
    root = tk.Tk()

    def rce(exc_type, exc, tb):
        ERRORS.append("".join(traceback.format_exception(exc_type, exc, tb)))

    root.report_callback_exception = rce

    app = VenusChatV1(root)
    k = theme.scale_factor()
    root.geometry(f"{theme.s(1500)}x{theme.s(900)}+{theme.s(30)}+{theme.s(30)}")
    root.attributes("-topmost", True)

    print(f"scale={k}")
    print("== 1. 主界面 ==")
    pump(root)
    shot(root, "1-main")
    scan_state = []

    # hover 交互FlatButton
    chat = app.chat_view
    flat = [w for w in _all(root) if isinstance(w, W.FlatButton)]
    print(f"FlatButton 实例 {len(flat)}，Enter/Leave + invoke 逐个探测…")
    for b in flat:
        try:
            b._enter(); pump(root, 2)
            b._leave(); pump(root, 2)
        except Exception:
            ERRORS.append(traceback.format_exc())
    scan(root)

    print("== 2. 会话流（离线回合模拟，不消耗上游 Token） ==")
    chat.open_conversation(0)
    pump(root, 30)
    shot(root, "2-conversation")
    chat.input_box.insert("1.0", "调试：多行输入检查\n第二行验证 wraplength 表现")
    chat._update_placeholder()
    shot(root, "2b-composer-input")
    chat.input_box.delete("1.0", "end")
    chat._update_placeholder()
    chat.handle_backend("stream_start", chat._make_turn())
    chat.handle_backend("stream_delta", "好的，我先**查看目录**，再写入 notes.md。")
    chat.handle_backend("stream_tool_call", {"id": "call_a", "name": "list_dir",
                                             "arguments": '{"path": "."}',
                                             "step": 1, "max_steps": 64})
    chat.handle_backend("stream_tool_result", {"id": "call_a", "ok": True,
                                               "result": "files: 12"})
    chat.handle_backend("stream_todo_update", {"todos": [
        {"id": 1, "title": "读取工作区目录结构", "status": "done"},
        {"id": 2, "title": "整理会议纪要并写入 notes.md", "status": "in_progress"},
        {"id": 3, "title": "复核生成结果", "status": "pending"}]})
    pump(root, 12)
    shot(root, "3-send")     # 工具卡 + 任务面板 + 清单
    chat.handle_backend("stream_done", "")
    pump(root, 6)
    scan(root)

    print("== 3. 搜索开合 ==")
    chat._toggle_search(); pump(root)
    chat._toggle_search(); pump(root)

    print("== 4. 设置全部页面：逐页点击控件 ==")
    app.show_settings()
    pump(root)
    shot(root, "4-settings-model")
    from venuschat_v1.settings_view import PAGE_META
    for key in PAGE_META:
        app.settings_view.select_page(key)
        pump(root, 4)
        before = len(ERRORS)
        # 点击该页全部开关与分段
        for s_ in [w for w in _all(root) if isinstance(w, W.Switch)]:
            s_.toggle(); s_.toggle(); pump(root, 2)
        for g in [w for w in _all(root) if isinstance(w, W.SegmentedControl)]:
            for v in list(g.labels):
                g.set(v)
            pump(root, 2)
        for b in [w for w in _all(root) if isinstance(w, W.FlatButton)]:
            b._enter(); pump(root, 2)
            if str(b.text) in {"搜索", "任务面板", "收起", "取消", "查看诊断  ›", "连接管理"}:
                b._press(); b._release(None)   # 白名单内才真正 invoke，避免打到上游
            pump(root, 2)
        if len(ERRORS) > before:
            print(f"  !! 页面 {key} 抛异常")
        scan(root)
    shot(root, "5-settings-generic")

    print("== 5. 最小尺寸压力 ==")
    root.geometry(f"{theme.s(app.MIN_WIDTH)}x{theme.s(app.MIN_HEIGHT)}+40+40")
    pump(root, 14)
    shot(root, "5b-minsize")
    scan(root)
    root.geometry(f"{theme.s(1500)}x{theme.s(900)}+{theme.s(30)}+{theme.s(30)}")
    pump(root)

    print("== 6. 窗口操作 ==")
    app.toggle_maximize(); pump(root, 10)
    shot(root, "6-maximized")
    scan(root)
    app.toggle_maximize(); pump(root, 10)
    chat.new_chat(); pump(root)
    shot(root, "7-newchat")

    print("== 7. 浮层：MenuPopup / MessageDialog / ApprovalDialog ==")
    def _popups(cls):
        return [w for w in root.winfo_children() if isinstance(w, cls)]
    def _close_popups():
        for w in _popups(W.MenuPopup):
            w._dismiss()
        for w in _popups(W.MessageDialog) + _popups(W.ApprovalDialog):
            w._answer(False)
        pump(root, 4)

    # 7a 菜单：外部点击必须消散
    try:
        chat._show_agent_menu(); pump(root, 8)
        menus = _popups(W.MenuPopup)
        assert menus and menus[0]._watcher_active, "watcher 未挂载"
        shot(root, "8-agent-menu")
        tb = chat.toolbar_title
        tb.event_generate("<ButtonPress-1>", x=5, y=5)
        pump(root, 6)
        assert not _popups(W.MenuPopup), "点击外部后菜单未消散"
        print("  外部点击消散 OK；再验证行点击选择")
        chat._show_agent_menu(); pump(root, 8)
        menus = _popups(W.MenuPopup)
        assert menus, "菜单未重新打开"
        row = menus[0]._rows[0]
        row.event_generate("<ButtonPress-1>", x=20, y=8)
        pump(root, 6)
        assert not _popups(W.MenuPopup), "点击行后菜单未消散"
        # 监视器不应残留：再发一次任意点击，不得产生异常
        tb.event_generate("<ButtonPress-1>", x=5, y=5)
        pump(root, 6)
        scan(root)
    except Exception:
        ERRORS.append(traceback.format_exc())
    _close_popups()

    # 7b 对话框：无 grab，按钮事件直达
    try:
        W.MessageDialog(root, app.fonts, title="删除对话",
                        message="确定删除「示例会话」？该会话的全部消息将从本地存储中移除，操作不可恢复。",
                        confirm_text="删除", danger=True,
                        on_choice=lambda ok: None)
        pump(root, 8)
        shot(root, "9-delete-dialog")
        _close_popups()
        W.ApprovalDialog(root, app.fonts, {
            "id": "demo", "name": "create_file",
            "question": "要在工作区创建文件 demo.txt（21 字符），允许吗？",
            "diff": "--- /dev/null\n+++ b/demo.txt\n+hello venus\n"},
            lambda choice: None)
        pump(root, 8)
        shot(root, "10-approval"); _close_popups()
    except Exception:
        ERRORS.append(traceback.format_exc())

    root.attributes("-topmost", False)
    root.destroy()

    print("\n========== 结果 ==========")
    if ERRORS:
        print(f"回调/交互异常 {len(ERRORS)} 类:")
        seen = set()
        for e in ERRORS:
            key = e.strip().splitlines()[-1]
            if key in seen:
                continue
            seen.add(key)
            print("-" * 60)
            print(e.strip()[:900])
    else:
        print("回调/交互异常: 0")
    if FLAGS:
        uniq = {}
        for f in FLAGS:
            uniq.setdefault(f, 0)
            uniq[f] += 1
        print(f"\n布局异常 {len(uniq)} 种（去重）:")
        for f, n in sorted(uniq.items()):
            print(f"  [{n}x] {f}")
    else:
        print("布局异常: 0")
    return 0


def _all(w):
    out = [w]
    for c in w.winfo_children():
        out.extend(_all(c))
    return out


if __name__ == "__main__":
    raise SystemExit(run())
