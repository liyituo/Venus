"""设置窗口保存回归测试：下拉选项值收集 + bool 兼容显示匹配 + 候选 llm_base。

历史 bug：
1. opts_ 存的是反向映射 {值: 显示文本}，_collect 用显示文本查值 → 永远 miss，
   reasoning_mode / confirm_mode / log_level 保存全部回退旧值。
2. tool_router 以 bool 持久化，而选项值是字符串 "0"/"1"，_select 严格比较
   永远不匹配 → 打开设置总是显示"关闭"（看起来像"没保存"）。

用 stub 对象测试 _collect / _match_option / _opt_value_str，不创建真实 Tk 窗口。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import chat as chat_mod  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


# ============ 1. _opt_value_str 类型归一化 ============
print("== 1. _opt_value_str ==")
check("True → 1", chat_mod._opt_value_str(True) == "1")
check("False → 0", chat_mod._opt_value_str(False) == "0")
check("字符串 1 → 1", chat_mod._opt_value_str("1") == "1")
check("字符串 0 → 0", chat_mod._opt_value_str("0") == "0")
check("max → max", chat_mod._opt_value_str("max") == "max")
check("None → 'None' 不误匹配", chat_mod._opt_value_str(None) != "0")

# ============ 2. _match_option 显示匹配 ============
print("== 2. _match_option（bool ↔ 字符串兼容）==")
opts = {"关闭": "0", "开启": "1"}
check("True 显示 开启", chat_mod._match_option(opts, True) == "开启")
check("False 显示 关闭", chat_mod._match_option(opts, False) == "关闭")
check("字符串 1 显示 开启", chat_mod._match_option(opts, "1") == "开启")
check("未知值回退第一项", chat_mod._match_option(opts, "bogus") == "关闭")
check("None 回退第一项", chat_mod._match_option(opts, None) == "关闭")
opts2 = {"智能 (auto)": "auto", "严格 (strict)": "strict"}
check("普通字符串精确匹配", chat_mod._match_option(opts2, "strict") == "严格 (strict)")

# ============ 3. _collect 下拉收集 ============
print("== 3. SettingsWindow._collect 下拉收集 ==")


class FakeVar:
    def __init__(self, v):
        self._v = v

    def get(self):
        return self._v


class FakeEntry:
    def __init__(self, v=""):
        self._v = v

    def get(self):
        return self._v


def make_window():
    sw = chat_mod.SettingsWindow.__new__(chat_mod.SettingsWindow)
    sw.config = {"reasoning_mode": "max", "confirm_mode": "auto", "log_level": "info",
                 "tool_router": False, "api_token": "", "daemon_token": ""}
    sw.entry_workspace = FakeEntry("")
    sw.entry_api_token = FakeEntry("")
    sw.entry_daemon_token = FakeEntry("")
    # 三个下拉：显示文本 -> 值（正映射，与 _select 中 opts_ 存储方向一致）
    sw.var_reasoning_mode = FakeVar("高")
    sw.opts_reasoning_mode = {"最高": "max", "高": "high", "关闭": "off"}
    sw.var_confirm_mode = FakeVar("严格 (strict)")
    sw.opts_confirm_mode = {"智能 (auto)": "auto", "严格 (strict)": "strict",
                            "信任 (trusted)": "trusted", "只读 (query)": "query",
                            "计划 (plan)": "plan"}
    sw.var_log_level = FakeVar("调试")
    sw.opts_log_level = {"信息": "info", "警告": "warning", "调试": "debug"}
    sw.var_tool_router = FakeVar("关闭")
    sw.opts_tool_router = {"关闭": "0", "开启": "1"}
    return sw


sw = make_window()
cfg = sw._collect()
check("reasoning_mode 收集 high", cfg["reasoning_mode"] == "high", str(cfg))
check("confirm_mode 收集 strict", cfg["confirm_mode"] == "strict", str(cfg))
check("log_level 收集 debug", cfg["log_level"] == "debug", str(cfg))

# tool_router 开关（单独分支：显示文本 → 选项值 "1" → bool True）
sw = make_window()
sw.var_tool_router = FakeVar("开启")
cfg = sw._collect()
check("tool_router 开启 → True", cfg["tool_router"] is True, str(cfg))
sw = make_window()
sw.var_tool_router = FakeVar("关闭")
cfg = sw._collect()
check("tool_router 关闭 → False", cfg["tool_router"] is False, str(cfg))

# 未改动字段保持原值
sw = make_window()
sw.var_reasoning_mode = FakeVar("最高")   # 显示文本对应旧值 max
sw.var_confirm_mode = FakeVar("智能 (auto)")
sw.var_log_level = FakeVar("信息")
sw.var_tool_router = FakeVar("关闭")
cfg = sw._collect()
check("未改动下拉保持原值",
      cfg["reasoning_mode"] == "max" and cfg["confirm_mode"] == "auto"
      and cfg["log_level"] == "info" and cfg["tool_router"] is False, str(cfg))

# ============ 4. 候选 llm_base 参与网络请求 ============
print("== 4. 候选 llm_base（不硬编码端口）==")
sw = make_window()
sw.config["llm_base"] = "http://192.168.1.9:9099"
calls = []


def fake_api(base, method, path, payload=None, timeout=15):
    calls.append((base, path))
    return 200, {"ok": True}, None


import chat as _chat  # noqa: E811
_orig = _chat.api_request
_chat.api_request = fake_api
try:
    sw._do_save = lambda err: None            # 不触发窗口销毁回调
    import types
    sw.win = types.SimpleNamespace(after=lambda ms, fn=None: None)
    cfg = sw._collect()
    cfg["workspace"] = "C:/ws"
    cfg["confirm_mode"] = "strict"
    # 直接执行 _do_save 内部逻辑（绕过 after 回调）
    llm_base = str(cfg.get("llm_base") or "http://127.0.0.1:8001").rstrip("/")
    _chat.api_request(llm_base, "POST", "/api/v1/workspace", {"path": cfg["workspace"]}, timeout=10)
    _chat.api_request(llm_base, "POST", "/api/v1/confirm-mode", {"mode": cfg["confirm_mode"]}, timeout=10)
    check("请求走候选 llm_base", calls and all(b == "http://192.168.1.9:9099" for b, _ in calls),
          str(calls))
    check("路径正确", calls == [("http://192.168.1.9:9099", "/api/v1/workspace"),
                                ("http://192.168.1.9:9099", "/api/v1/confirm-mode")], str(calls))
finally:
    _chat.api_request = _orig

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
