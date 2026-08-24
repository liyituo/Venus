"""ChatApp UI 预览截图（开发工具，不参与运行）。

不启动任何后端：把 ChatApp 的自启动逻辑打桩掉，只构建界面并注入
示例消息/任务清单，然后用 PIL.ImageGrab 截图保存到 artifacts/ui-preview/，
供视觉回归与设计迭代使用。

用法：.venv\\Scripts\\python scripts\\preview_chat_ui.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import tkinter as tk  # noqa: E402
from PIL import ImageGrab  # noqa: E402

import chat  # noqa: E402

OUT = ROOT / ".pcagent" / "ui-preview"
OUT.mkdir(parents=True, exist_ok=True)


def _no_backend(app: "chat.ChatApp") -> None:
    """关掉所有会自动拉起后端/轮询网络的逻辑。"""
    app._start = lambda: None
    app._poll_token_stats = lambda: None
    app._refresh_projects_async = lambda: None


def grab(widget: tk.Misc, name: str) -> Path:
    widget.update_idletasks()
    widget.update()
    time.sleep(0.3)
    widget.update_idletasks()
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    w, h = widget.winfo_width(), widget.winfo_height()
    path = OUT / f"{name}.png"
    ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(path)
    print(f"saved {path}")
    return path


def seed_conversation(app: "chat.ChatApp") -> None:
    """注入一段带代码块/粗体/思考过程的示例对话 + 任务清单。"""
    app._sessions[1] = {
        "messages": [], "title": "界面设计评审", "loaded": True, "count": 4,
        "history": [],
    }
    app._sessions[2] = {
        "messages": [], "title": "修复量化中心启动失败", "loaded": True,
        "count": 6, "history": [],
    }
    app._sessions[3] = {
        "messages": [], "title": "给 RAG 服务加健康检查", "loaded": True,
        "count": 2, "history": [],
    }
    app._current_sid = 1

    app._add_message("user", "帮我审查一下 ChatApp 的界面改动，重点看配色和层次。")
    handle = app._add_message(
        "agent",
        "整体方向没问题，给你两点建议：\n"
        "**1. 层次**：表面色阶从 `BG0` 到 `BG5` 已经拉开，边框保持发丝级即可。\n"
        "**2. 强调色**：冰青只留给主操作，状态色不要参与装饰。\n\n"
        "示例令牌定义：\n"
        "```python\n"
        "BG0 = \"#04060c\"   # 窗口底色\n"
        "CY1 = \"#8cecff\"   # 主强调：冰青\n"
        "INK1 = \"#f4f7fb\"  # 主文字\n"
        "```\n"
        "按这个基准再核对一遍侧栏和输入区。")
    app._render_thinking(handle, "先确认色板层级，再检查按钮的对比度是否满足可读性要求。"
                         "胶囊按钮在深色底上的边缘是否干净？聚焦环是否与边框冲突？")
    app._add_message("user", "好的，侧栏的会话项选中态再明显一点。")

    # 任务清单：临时伪装成当前流，让 _on_stream_todo 正常渲染
    app._streaming, app._stream_task_id, app._stream_handle = True, 0, None
    app._on_stream_todo((0, None, {"todos": [
        {"title": "梳理色板令牌与字阶", "status": "completed"},
        {"title": "胶囊按钮替换全部方角按钮", "status": "completed"},
        {"title": "空状态舞台与快捷键键帽", "status": "in_progress"},
        {"title": "设置对话框分组卡片", "status": "pending"},
    ]}))
    app._streaming, app._stream_handle = False, None


def main() -> None:
    root = tk.Tk()
    app = chat.ChatApp(root, "http://127.0.0.1:8000")
    _no_backend(app)
    root.attributes("-topmost", True)
    root.geometry("1280x800+40+30")

    # 状态与侧栏先就位
    app._set_status(True, "online")
    app.status_model.config(text="deepseek-chat")
    app.llm_status.config(text="●  deepseek-chat\n推理 最高  ·  权限 auto", fg=chat.OK)
    app.token_stats.config(text="◈ 累计 42.1k tok · 缓存 78% · 压缩 3 · 调用 128")

    # --- 截图 1：空状态舞台 ---
    seed_conversation(app)
    app._sessions[1]["history"] = []
    for child in app.scroll_frame.winfo_children():
        child.destroy()
    app._update_session_sidebar()
    app._show_empty_workspace()
    grab(root, "1-main-empty")

    # --- 截图 2：对话 + 代码块 + 任务清单 ---
    app._hide_empty_workspace()
    for child in app.scroll_frame.winfo_children():
        child.destroy()
    seed_conversation(app)
    app._update_session_sidebar()
    root.update()
    app.canvas.yview_moveto(1.0)
    grab(root, "2-main-conversation")

    # --- 截图 3：设置窗口 ---
    settings = chat.SettingsWindow(root)
    settings.win.geometry("860x700+120+60")
    settings.win.attributes("-topmost", True)
    grab(settings.win, "3-settings")
    settings.win.destroy()

    root.destroy()
    print("done")


if __name__ == "__main__":
    main()
