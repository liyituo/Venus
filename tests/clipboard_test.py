"""剪贴板快照/恢复回归测试：完全 mock，绝不操作真实剪贴板。

覆盖：正常输入后全格式恢复、多格式快照（文本/DIB/HDROP）、输入异常时
恢复、恢复异常返回可见错误、SendInput 优先路径。
"""
import os
import sys
import unittest.mock as um
from pathlib import Path

os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
os.environ.setdefault("PCAGENT_ALLOW_TEST_HOST", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as A  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


# ---- 模拟 win32clipboard：格式枚举/读写全部在内存中 ----
CLIP_STORE: dict[int, object] = {}


class FakeWin32Clipboard:
    """内存剪贴板：Open/Enum/Get/Empty/Set/Close。"""

    @staticmethod
    def OpenClipboard():
        pass

    @staticmethod
    def CloseClipboard():
        pass

    @staticmethod
    def EnumClipboardFormats(fmt):
        fmts = sorted(CLIP_STORE)
        if fmt == 0:
            return fmts[0] if fmts else 0
        for f in fmts:
            if f > fmt:
                return f
        return 0

    @staticmethod
    def GetClipboardData(fmt):
        if fmt not in CLIP_STORE:
            raise OSError("format not present")
        return CLIP_STORE[fmt]

    @staticmethod
    def EmptyClipboard():
        CLIP_STORE.clear()

    @staticmethod
    def SetClipboardData(fmt, data):
        CLIP_STORE[fmt] = data
        return fmt


CF_UNICODETEXT = 13
CF_DIB = 8
CF_HDROP = 15
CF_HTML = 49413


def _install_fake_clipboard() -> None:
    CLIP_STORE.clear()
    import sys as _s
    _s.modules["win32clipboard"] = FakeWin32Clipboard


# ============ 1. 全格式快照与恢复 ============
print("== 1. 快照与恢复（多格式）==")
_install_fake_clipboard()
CLIP_STORE[CF_UNICODETEXT] = "原始文本"
CLIP_STORE[CF_DIB] = b"fake-dib-bytes"
CLIP_STORE[CF_HDROP] = ["C:\\a.txt", "C:\\b.txt"]
snap = A._snapshot_clipboard()
check("快照包含全部 3 种格式", set(snap) == {CF_UNICODETEXT, CF_DIB, CF_HDROP},
      str(set(snap) if snap else None))
check("快照内容正确", snap[CF_UNICODETEXT] == "原始文本" and snap[CF_DIB] == b"fake-dib-bytes",
      str(snap))
# 覆盖后恢复
CLIP_STORE.clear()
CLIP_STORE[CF_UNICODETEXT] = "输入的新文本"
A._restore_clipboard_snapshot(snap, "fallback")
check("恢复后原始 3 格式全部还原",
      CLIP_STORE.get(CF_UNICODETEXT) == "原始文本"
      and CLIP_STORE.get(CF_DIB) == b"fake-dib-bytes"
      and CLIP_STORE.get(CF_HDROP) == ["C:\\a.txt", "C:\\b.txt"],
      str(CLIP_STORE))

# ============ 2. 输入流程：快照 → 覆盖 → 恢复一次 ============
print("== 2. _paste_via_clipboard 全流程 ==")
_install_fake_clipboard()
CLIP_STORE[CF_UNICODETEXT] = "用户原有内容"
CLIP_STORE[CF_DIB] = b"img-data"
with um.patch.object(A.pyautogui, "hotkey") as mock_hotkey:
    with um.patch("pyperclip.copy") as mock_copy:
        with um.patch("pyperclip.paste", return_value="用户原有内容"):
            A._paste_via_clipboard("中文输入")
check("ctrl+v 被调用", mock_hotkey.call_count == 1
      and mock_hotkey.call_args.args == ("ctrl", "v"), str(mock_hotkey.call_args_list))
check("输入文本写入剪贴板", mock_copy.call_args.args[0] == "中文输入",
      str(mock_copy.call_args_list))
check("恢复后原多格式仍在", CLIP_STORE.get(CF_UNICODETEXT) == "用户原有内容"
      and CLIP_STORE.get(CF_DIB) == b"img-data", str(CLIP_STORE))

# ============ 3. 输入异常：仍然恢复 ============
print("== 3. 输入异常时恢复 ==")
_install_fake_clipboard()
CLIP_STORE[CF_UNICODETEXT] = "异常前的用户内容"
with um.patch.object(A.pyautogui, "hotkey", side_effect=OSError("input fail")):
    with um.patch("pyperclip.copy") as mock_copy:
        with um.patch("pyperclip.paste", return_value="异常前的用户内容"):
            try:
                A._paste_via_clipboard("输入")
                raised = False
            except Exception:
                raised = True
check("输入失败抛出可见错误", raised, "")
check("异常路径仍恢复原内容", CLIP_STORE.get(CF_UNICODETEXT) == "异常前的用户内容",
      str(CLIP_STORE))

# ============ 4. 恢复异常：返回可见错误，不静默 ============
print("== 4. 恢复异常可见 ==")
_install_fake_clipboard()
CLIP_STORE[CF_UNICODETEXT] = "原始"
with um.patch.object(A.pyautogui, "hotkey") as mock_hotkey:
    with um.patch("pyperclip.copy"):
        with um.patch("pyperclip.paste", return_value="原始"):
            # SetClipboardData 抛错 → 恢复失败
            with um.patch.object(FakeWin32Clipboard, "SetClipboardData",
                                 side_effect=OSError("clipboard busy")):
                try:
                    A._paste_via_clipboard("输入")
                    raised = False
                except Exception:
                    raised = True
check("恢复失败时抛出可见错误（500 语义）", raised, "")

# ============ 5. win32clipboard 缺失 → 文本快照回退 ============
print("== 5. 无 win32clipboard 回退 ==")
_install_fake_clipboard()
import sys as _sys
with um.patch.dict(_sys.modules, {"win32clipboard": None}):   # import 失败 → ImportError
    with um.patch("pyperclip.paste", return_value="无格式环境文本"):
        snap2 = A._snapshot_clipboard()
check("无 win32clipboard 时快照为 None（保守路径）", snap2 is None, str(snap2))
with um.patch.dict(_sys.modules, {"win32clipboard": None}):
    with um.patch("pyperclip.copy") as mock_copy:
        A._restore_clipboard_snapshot(None, "回退文本")
check("回退恢复为纯文本", mock_copy.call_args.args[0] == "回退文本",
      str(mock_copy.call_args_list))

# ============ 6. SendInput 优先路径 ============
print("== 6. SendInput 优先 ==")
with um.patch.object(A, "_type_unicode_sendinput", return_value=True) as mock_si:
    with um.patch.object(A, "_paste_via_clipboard") as mock_paste:
        A._handle_type_text(type("R", (), {"text": "你好🌌"}))
check("SendInput 成功时不走剪贴板", mock_si.called and not mock_paste.called,
      str(mock_si.call_args_list))
with um.patch.object(A, "_type_unicode_sendinput", return_value=False):
    with um.patch.object(A, "_paste_via_clipboard") as mock_paste:
        A._handle_type_text(type("R", (), {"text": "你好🌌"}))
check("SendInput 失败回退剪贴板", mock_paste.called, str(mock_paste.call_args_list))

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
