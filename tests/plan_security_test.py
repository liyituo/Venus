"""计划授权安全测试：命令拼接绕过 / run_code 绑定 / git_commit 范围 / 路径穿越 / 未知工具 fail-closed。"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PCAGENT_DISABLE_MCP", "1")
os.environ.setdefault("PCAGENT_ALLOW_TEST_HOST", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import llm_server as L  # noqa: E402
import security_policy as SP  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


_TMP = tempfile.mkdtemp(prefix="pcagent_plansec_")
WS = Path(_TMP)
L._get_workspace = lambda: WS
(WS / "src").mkdir()
(WS / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
(WS / "secret.txt").write_text("s\n", encoding="utf-8")
(WS / "other.py").write_text("y = 2\n", encoding="utf-8")

WS2 = Path(tempfile.mkdtemp(prefix="pcagent_plansec2_"))
(WS2 / "allowed").mkdir()
(WS2 / "allowed" / "f.txt").write_text("ok\n", encoding="utf-8")
# 符号链接：workspace 内指向 workspace 外（应被 resolve 校验拒绝）
try:
    (WS / "evil_link").symlink_to(WS2, target_is_directory=True)
    HAS_SYMLINK = True
except OSError:
    HAS_SYMLINK = False


def authorized(specs, name, args, ws=None):
    return L._plan_authorized(specs, name, args, workspace=ws or WS)


# ============ 1. 命令拼接绕过（禁止 startswith 授权）============
print("== 1. 命令拼接 ==")
specs = L._plan_build_specs([
    {"step": "查状态", "tools": ["run_shell"], "commands": ["git status"]},
    {"step": "执行命令", "tools": ["run_shell"], "commands": ["echo planned-ok"]},
])
check("精确命令授权", authorized(specs, "run_shell", {"command": "git status"}), "")
check("拼接 & 拒绝", not authorized(specs, "run_shell", {"command": "git status & rm -rf ."}), "")
check("拼接 && 拒绝", not authorized(specs, "run_shell", {"command": "git status && rm x"}), "")
check("管道拒绝", not authorized(specs, "run_shell", {"command": "git status | head"}), "")
check("重定向拒绝", not authorized(specs, "run_shell", {"command": "git status > f.txt"}), "")
check("子命令拒绝", not authorized(specs, "run_shell", {"command": "git status; echo x"}), "")
check("前缀词不匹配（git status 不授权 git statusx）",
      not authorized(specs, "run_shell", {"command": "git statusx"}), "")
check("前缀词不匹配（echo planned-ok 不授权 echo planned-ok2）",
      not authorized(specs, "run_shell", {"command": "echo planned-ok2"}), "")
check("规范化后精确匹配（多余空白）",
      authorized(specs, "run_shell", {"command": "  echo   planned-ok  "}), "")
check("空命令拒绝", not authorized(specs, "run_shell", {"command": ""}), "")

# ============ 2. run_code 绑定文件范围 ============
print("== 2. run_code 绑定 ==")
specs_code = L._plan_build_specs([
    {"step": "跑脚本", "tools": ["run_code"], "files": ["src/"]},
])
check("run_code file 在范围内授权", authorized(specs_code, "run_code", {"file": "src/a.py"}), "")
check("run_code file 超范围拒绝", not authorized(specs_code, "run_code", {"file": "other.py"}), "")
check("run_code 纯 code 不授权（无法预绑定内容）",
      not authorized(specs_code, "run_code", {"code": "print(1)"}), "")
specs_nofile = L._plan_build_specs([{"step": "跑", "tools": ["run_code"]}])
check("run_code 未声明 files 拒绝", not authorized(specs_nofile, "run_code", {"file": "src/a.py"}), "")

# ============ 3. git_commit 绑定文件集合 ============
print("== 3. git_commit 范围 ==")
specs_git = L._plan_build_specs([
    {"step": "提交", "tools": ["git_commit"], "files": ["src/"]},
])
check("git_commit 提交范围内授权",
      authorized(specs_git, "git_commit", {"message": "m", "files": ["src/a.py"]}), "")
check("git_commit 超范围拒绝",
      not authorized(specs_git, "git_commit", {"message": "m", "files": ["other.py"]}), "")
check("git_commit 未声明 files 拒绝",
      not authorized(specs_git, "git_commit", {"message": "m"}), "")

# ============ 4. 路径穿越 / 符号链接 ============
print("== 4. 路径穿越 / 符号链接 ==")
specs_fs = L._plan_build_specs([
    {"step": "改文件", "tools": ["replace_text"], "files": ["src/"]},
])
check("范围内路径授权", authorized(specs_fs, "replace_text", {"file": "src/a.py"}), "")
check(".. 穿越拒绝（resolve 校验）",
      not authorized(specs_fs, "replace_text", {"file": "src/../secret.txt"}), "")
check("直接越界拒绝",
      not authorized(specs_fs, "replace_text", {"file": "secret.txt"}), "")
if HAS_SYMLINK:
    check("符号链接越界拒绝（resolve 校验）",
          not authorized(specs_fs, "replace_text", {"file": "evil_link/../a.py"}), "")
    check("符号链接目标外拒绝",
          not authorized(specs_fs, "replace_text", {"file": "evil_link/f.txt"}), "")
else:
    print("  SKIP  符号链接用例（当前平台不支持 symlink）")

# ============ 5. 未知工具 fail-closed ============
print("== 5. 未知工具 fail-closed ==")
check("未知工具 auto 下需确认", SP._needs_confirm("future_tool", {}) is True, "")
_orig_mode = SP._current_confirm_mode
SP._current_confirm_mode = lambda: "auto"
check("未知工具 auto 策略 ask", SP._confirm_policy("future_tool", {}) == "ask", "")
SP._current_confirm_mode = lambda: "query"
check("未知工具 query 策略 deny", SP._confirm_policy("future_tool", {}) == "deny", "")
SP._current_confirm_mode = _orig_mode

# ============ 6. TOOL_META 完整性（新工具必须声明策略）============
print("== 6. TOOL_META 完整性 ==")
missing = [t["function"]["name"] for t in L.AGENT_TOOLS
           if t["function"]["name"] not in SP.TOOL_META]
check("全部 AGENT_TOOLS 已登记 TOOL_META", missing == [], str(missing))
for name, meta in SP.TOOL_META.items():
    need = {"read_only", "risk_level", "requires_confirmation", "allowed_in_query",
            "allowed_in_isolated", "workspace_scoped", "external_side_effect"}
    check(f"元数据字段完整: {name}", need <= set(meta), str(set(meta)))

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
