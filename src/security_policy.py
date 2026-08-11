"""
工具权限策略（集中式元数据与判定入口）— 从 llm_server 拆出的独立模块。

所有工具的权限判断唯一入口：
- TOOL_META：集中式工具元数据（read_only / requires_confirmation / allowed_in_query 等）；
- _is_query_tool / _needs_confirm / _confirm_policy：query 放行、确认需求、模式判定；
- _is_readonly_shell：保守 shell 只读判定（仅单个简单白名单命令，复合命令一律非只读）。

llm_server.py 通过 `from security_policy import *` re-export 全部符号，保持既有引用兼容。
"""

from __future__ import annotations

import re
import sys

# ---- 只读 shell 白名单（保守方案）----
# 只读免确认仅接受「单个简单命令」：命令名必须在此白名单，且不含任何
# shell 控制结构（& | ; > < 反引号 $() 换行等）、不携带写选项。
# 复合命令（dir & del、echo && powershell、cat; rm、ls || x、管道后写、命令替换、
# 嵌套 shell）一律视为写操作：auto/strict 要求确认，query 直接拒绝。
READONLY_SHELL_CMDS: frozenset = frozenset({
    "ls", "cat", "head", "tail", "grep", "find", "echo", "pwd", "whoami", "date",
    "df", "du", "uname", "ps", "env", "which", "type", "file", "stat", "wc",
    "sort", "cut", "awk", "sed", "history", "printenv", "id", "hostname",
    "uptime", "free", "getconf", "locale",
})
# Windows（cmd）额外只读命令（不含 date：cmd 的 date 无参数会交互提示改日期）
READONLY_SHELL_CMDS_WIN: frozenset = READONLY_SHELL_CMDS | frozenset({
    "dir", "more", "findstr", "where", "tasklist", "systeminfo",
    "ver", "set", "path", "cd", "cls", "help", "netstat", "ipconfig", "reg",
})
# 各白名单命令中带写副作用的选项（按命令定制，避免误伤 grep -i 等无害选项）
_READONLY_BAD_FLAGS: dict[str, tuple[str, ...]] = {
    "sed": ("-i",),                  # sed -i 就地写文件
    "sort": ("-o",),                 # sort -o file 写文件
    "find": ("-delete", "-exec", "-ok"),  # find 的删除/执行操作
}
# shell 控制结构：任何出现即视为非只读（含重定向、管道、复合、命令替换、子 shell、换行）
_SHELL_CONTROL_RE = re.compile(r"[&|;<>`\n\r]|\(\s*\$|\$\(")
# 命令名中不允许出现的字符（路径/转义/等号赋值）：白名单命令必须是裸命令名
_CMD_NAME_BAD = re.compile(r"[\\/=\[\]]")

RUN_SHELL_MAX_CMD = 2000     # 命令长度上限（与 llm_server 共用，re-export）


def _is_readonly_shell(command: str) -> bool:
    """保守只读判定：仅接受「单个简单命令」，复合命令一律视为写操作。

    拒绝项（任何出现即非只读，防止命令拼接冒充只读绕过确认）：
    - shell 控制结构：`&` `&&` `||` `;` `|` `>` `<` 反引号 `$(` 换行等；
    - 非 Windows 平台上的反斜杠（bash 转义符）；
    - 命令名不是裸名（含路径分隔 / \\、= 赋值、[ ]）；
    - 命令名不在平台只读白名单；
    - 参数携带该命令的写选项（sed -i / sort -o / find -delete/-exec/-ok）；
    - Windows 特例：set/path 带 `=`（写环境变量）、reg 仅允许 `query` 子命令。
    """
    cmd = (command or "").strip()
    if not cmd or len(cmd) > RUN_SHELL_MAX_CMD:
        return False
    if _SHELL_CONTROL_RE.search(cmd):
        return False
    if sys.platform != "win32" and "\\" in cmd:
        return False
    parts = cmd.split()
    name = parts[0].lower()
    if _CMD_NAME_BAD.search(name):
        return False
    cmds = READONLY_SHELL_CMDS_WIN if sys.platform == "win32" else READONLY_SHELL_CMDS
    if name not in cmds:
        return False
    args = parts[1:]
    for flag in _READONLY_BAD_FLAGS.get(name, ()):
        if any(a == flag or a.startswith(flag + "=") for a in args):
            return False
    if sys.platform == "win32":
        if name in ("set", "path") and any("=" in a for a in args):
            return False      # set FOO=bar / path=... 是写环境变量
        if name == "reg":
            if not args or args[0].lower() != "query":
                return False  # 仅 reg query 只读；reg add/delete 另有黑名单硬拦截
    return True


# ---- 集中式工具权限元数据 ----
# 所有工具的权限判断唯一入口：query 放行 / 确认需求 / 隔离可用 全部由此派生，
# 不允许在多个位置分别维护不一致的集合（CONFIRM_TOOLS / QUERY_TOOLS / _FILE_TOOLS
# 均从此表派生，兼容旧引用）。
#
# 字段含义：
#   category               工具类别（screen/file/execute/edit/git/process/todo/index/skill/vision/agent）
#   read_only              是否无副作用只读（决定 query 放行、auto/strict 免确认的基础）
#   risk_level             low / medium / high / critical
#   workspace_scoped       是否绑定当前工作区（路径安全校验）
#   requires_confirmation  auto 模式下默认是否确认（run_shell 按命令判定、create_file 按目标存在性判定）
#   allowed_in_query       query 模式是否放行
#   allowed_in_isolated    隔离模式（--isolated）是否保留
#   external_side_effect   是否对外部系统（网络/外部 API）产生副作用
TOOL_META: dict[str, dict] = {
    # ---- 屏幕 ----
    "get_screen_size": {"category": "screen", "read_only": True, "risk_level": "low",
                        "workspace_scoped": False, "requires_confirmation": False,
                        "allowed_in_query": True, "allowed_in_isolated": False,
                        "external_side_effect": False},
    "click": {"category": "screen", "read_only": False, "risk_level": "medium",
              "workspace_scoped": False, "requires_confirmation": False,
              "allowed_in_query": False, "allowed_in_isolated": False,
              "external_side_effect": False},
    "type_text": {"category": "screen", "read_only": False, "risk_level": "medium",
                  "workspace_scoped": False, "requires_confirmation": False,
                  "allowed_in_query": False, "allowed_in_isolated": False,
                  "external_side_effect": False},
    "press_key": {"category": "screen", "read_only": False, "risk_level": "medium",
                  "workspace_scoped": False, "requires_confirmation": False,
                  "allowed_in_query": False, "allowed_in_isolated": False,
                  "external_side_effect": False},
    "stop": {"category": "system", "read_only": False, "risk_level": "high",
             "workspace_scoped": False, "requires_confirmation": False,
             "allowed_in_query": False, "allowed_in_isolated": False,
             "external_side_effect": False},
    # ---- 文件 ----
    "create_folder": {"category": "file", "read_only": False, "risk_level": "low",
                      "workspace_scoped": True, "requires_confirmation": False,
                      "allowed_in_query": False, "allowed_in_isolated": True,
                      "external_side_effect": False},
    "list_folder": {"category": "file", "read_only": True, "risk_level": "low",
                    "workspace_scoped": True, "requires_confirmation": False,
                    "allowed_in_query": True, "allowed_in_isolated": True,
                    "external_side_effect": False},
    "create_file": {"category": "file", "read_only": False, "risk_level": "medium",
                    "workspace_scoped": True, "requires_confirmation": True,
                    "allowed_in_query": False, "allowed_in_isolated": True,
                    "external_side_effect": False},
    "read_file": {"category": "file", "read_only": True, "risk_level": "low",
                  "workspace_scoped": True, "requires_confirmation": False,
                  "allowed_in_query": True, "allowed_in_isolated": True,
                  "external_side_effect": False},
    "delete_file": {"category": "file", "read_only": False, "risk_level": "high",
                    "workspace_scoped": True, "requires_confirmation": True,
                    "allowed_in_query": False, "allowed_in_isolated": True,
                    "external_side_effect": False},
    "delete_folder": {"category": "file", "read_only": False, "risk_level": "high",
                      "workspace_scoped": True, "requires_confirmation": True,
                      "allowed_in_query": False, "allowed_in_isolated": True,
                      "external_side_effect": False},
    "move_file": {"category": "file", "read_only": False, "risk_level": "medium",
                  "workspace_scoped": True, "requires_confirmation": True,
                  "allowed_in_query": False, "allowed_in_isolated": True,
                  "external_side_effect": False},
    "rename_file": {"category": "file", "read_only": False, "risk_level": "medium",
                    "workspace_scoped": True, "requires_confirmation": True,
                    "allowed_in_query": False, "allowed_in_isolated": True,
                    "external_side_effect": False},
    "copy_file": {"category": "file", "read_only": False, "risk_level": "medium",
                  "workspace_scoped": True, "requires_confirmation": True,
                  "allowed_in_query": False, "allowed_in_isolated": True,
                  "external_side_effect": False},
    # ---- 执行 ----
    "run_code": {"category": "execute", "read_only": False, "risk_level": "high",
                 "workspace_scoped": True, "requires_confirmation": True,
                 "allowed_in_query": False, "allowed_in_isolated": True,
                 "external_side_effect": False},
    "run_shell": {"category": "execute", "read_only": False, "risk_level": "high",
                  "workspace_scoped": True, "requires_confirmation": True,
                  "allowed_in_query": False, "allowed_in_isolated": True,
                  "external_side_effect": False},
    # ---- 检索 / 编辑 ----
    "search_text": {"category": "edit", "read_only": True, "risk_level": "low",
                    "workspace_scoped": True, "requires_confirmation": False,
                    "allowed_in_query": True, "allowed_in_isolated": True,
                    "external_side_effect": False},
    "glob_files": {"category": "edit", "read_only": True, "risk_level": "low",
                   "workspace_scoped": True, "requires_confirmation": False,
                   "allowed_in_query": True, "allowed_in_isolated": True,
                   "external_side_effect": False},
    "list_symbols": {"category": "edit", "read_only": True, "risk_level": "low",
                     "workspace_scoped": True, "requires_confirmation": False,
                     "allowed_in_query": True, "allowed_in_isolated": True,
                     "external_side_effect": False},
    "replace_text": {"category": "edit", "read_only": False, "risk_level": "medium",
                     "workspace_scoped": True, "requires_confirmation": True,
                     "allowed_in_query": False, "allowed_in_isolated": True,
                     "external_side_effect": False},
    "undo": {"category": "edit", "read_only": False, "risk_level": "medium",
             "workspace_scoped": True, "requires_confirmation": True,
             "allowed_in_query": False, "allowed_in_isolated": True,
             "external_side_effect": False},
    "create_plan": {"category": "edit", "read_only": True, "risk_level": "low",
                    "workspace_scoped": False, "requires_confirmation": False,
                    "allowed_in_query": False, "allowed_in_isolated": True,
                    "external_side_effect": False},
    # ---- Git ----
    "git_status": {"category": "git", "read_only": True, "risk_level": "low",
                   "workspace_scoped": True, "requires_confirmation": False,
                   "allowed_in_query": True, "allowed_in_isolated": True,
                   "external_side_effect": False},
    "git_diff": {"category": "git", "read_only": True, "risk_level": "low",
                 "workspace_scoped": True, "requires_confirmation": False,
                 "allowed_in_query": True, "allowed_in_isolated": True,
                 "external_side_effect": False},
    "git_log": {"category": "git", "read_only": True, "risk_level": "low",
                "workspace_scoped": True, "requires_confirmation": False,
                "allowed_in_query": True, "allowed_in_isolated": True,
                "external_side_effect": False},
    "git_commit": {"category": "git", "read_only": False, "risk_level": "high",
                   "workspace_scoped": True, "requires_confirmation": True,
                   "allowed_in_query": False, "allowed_in_isolated": True,
                   "external_side_effect": True},
    # ---- 后台进程 ----
    "start_process": {"category": "process", "read_only": False, "risk_level": "high",
                      "workspace_scoped": True, "requires_confirmation": True,
                      "allowed_in_query": False, "allowed_in_isolated": True,
                      "external_side_effect": False},
    "process_output": {"category": "process", "read_only": True, "risk_level": "low",
                       "workspace_scoped": False, "requires_confirmation": False,
                       "allowed_in_query": True, "allowed_in_isolated": True,
                       "external_side_effect": False},
    "stop_process": {"category": "process", "read_only": False, "risk_level": "medium",
                     "workspace_scoped": False, "requires_confirmation": False,
                     "allowed_in_query": False, "allowed_in_isolated": True,
                     "external_side_effect": False},
    "list_processes": {"category": "process", "read_only": True, "risk_level": "low",
                       "workspace_scoped": False, "requires_confirmation": False,
                       "allowed_in_query": True, "allowed_in_isolated": True,
                       "external_side_effect": False},
    # ---- 任务规划 ----
    "create_todo": {"category": "todo", "read_only": False, "risk_level": "low",
                    "workspace_scoped": False, "requires_confirmation": False,
                    "allowed_in_query": False, "allowed_in_isolated": True,
                    "external_side_effect": False},
    "update_todo": {"category": "todo", "read_only": False, "risk_level": "low",
                    "workspace_scoped": False, "requires_confirmation": False,
                    "allowed_in_query": False, "allowed_in_isolated": True,
                    "external_side_effect": False},
    "list_todos": {"category": "todo", "read_only": True, "risk_level": "low",
                   "workspace_scoped": False, "requires_confirmation": False,
                   "allowed_in_query": True, "allowed_in_isolated": True,
                   "external_side_effect": False},
    # ---- 项目索引 ----
    "repo_map": {"category": "index", "read_only": True, "risk_level": "low",
                 "workspace_scoped": True, "requires_confirmation": False,
                 "allowed_in_query": True, "allowed_in_isolated": True,
                 "external_side_effect": False},
    # ---- 技能 / 系统 / 视觉 / 子 agent ----
    "load_skill": {"category": "skill", "read_only": True, "risk_level": "low",
                   "workspace_scoped": False, "requires_confirmation": False,
                   "allowed_in_query": True, "allowed_in_isolated": True,
                   "external_side_effect": False},
    "system_status": {"category": "system", "read_only": True, "risk_level": "low",
                      "workspace_scoped": False, "requires_confirmation": False,
                      "allowed_in_query": True, "allowed_in_isolated": True,
                      "external_side_effect": False},
    "view_image": {"category": "vision", "read_only": True, "risk_level": "low",
                   "workspace_scoped": True, "requires_confirmation": False,
                   "allowed_in_query": True, "allowed_in_isolated": True,
                   "external_side_effect": True},
    "delegate": {"category": "agent", "read_only": False, "risk_level": "high",
                 "workspace_scoped": False, "requires_confirmation": False,
                 "allowed_in_query": False, "allowed_in_isolated": True,
                 "external_side_effect": False},
    "fetch_result": {"category": "system", "read_only": True, "risk_level": "low",
                     "workspace_scoped": False, "requires_confirmation": False,
                     "allowed_in_query": True, "allowed_in_isolated": True,
                     "external_side_effect": False},
}

# 兼容派生（旧引用保留；权限判断统一走 _is_query_tool / _needs_confirm / _confirm_policy）
CONFIRM_TOOLS: frozenset = frozenset(
    n for n, m in TOOL_META.items() if m["requires_confirmation"])
QUERY_TOOLS: frozenset = frozenset(
    n for n, m in TOOL_META.items() if m["read_only"] and m["allowed_in_query"])
_SCREEN_TOOLS = {"get_screen_size", "click", "type_text", "press_key"}
_FILE_TOOLS: frozenset = frozenset(
    n for n, m in TOOL_META.items() if m["allowed_in_isolated"])


def _current_confirm_mode() -> str:
    """当前问询模式。

    优先调用 llm_server 模块的当前实现（支持测试/调用方 monkeypatch 覆盖），
    否则回退 load_config（惰性导入避免循环依赖）。
    """
    import llm_server as _ls
    impl = getattr(_ls, "_current_confirm_mode", None)
    if impl is not None and impl is not _current_confirm_mode:
        return str(impl())
    return str(_ls.load_config().get("confirm_mode", "auto"))


def _is_readonly_mcp(name: str) -> bool:
    """MCP 工具只读判定：只来自显式配置声明（read_only_tools/write_tools）。

    未声明一律按写处理（保守）；不依赖任何 server 名称前缀后门。
    优先使用 llm_server 模块的 _mcp_manager（测试可 monkeypatch），否则惰性初始化。
    """
    from mcp_manager import _ensure_mcp
    import llm_server as _ls
    mcp = getattr(_ls, "_mcp_manager", None)
    if mcp is None:
        mcp = _ensure_mcp()
    if mcp is not None:
        try:
            r = mcp.is_readonly(name)
            if r is not None:
                return r
        except Exception:
            pass
    return False


def _is_query_tool(name: str, args: dict) -> bool:
    """集中权限入口：判断是否为真正的无副作用查询（query 放行 / auto、strict 免确认的基础）。

    - 本地工具按 TOOL_META（read_only + allowed_in_query）；
    - run_shell 只读判定走 _is_readonly_shell（仅单个简单白名单命令，复合命令一律非只读）；
    - MCP 工具按配置声明（_is_readonly_mcp：前缀 / 精确名 / read_only_tools 声明）。
    """
    if name == "run_shell":
        return _is_readonly_shell((args.get("command") or "").strip())
    meta = TOOL_META.get(name)
    if meta is not None:
        return bool(meta["read_only"] and meta["allowed_in_query"])
    if name.startswith("mcp_"):
        return _is_readonly_mcp(name)
    return False


def _needs_confirm(name: str, args: dict) -> bool:
    """集中权限入口：auto 模式下该工具调用是否需要用户确认。

    - create_file：新建/覆盖一律确认（问题中展示目标路径），由已批准计划或确认授权；
    - run_shell：按 _is_readonly_shell 判定（简单只读免确认，其余确认）；
    - 其余按 TOOL_META.requires_confirmation；
    - 未登记到 TOOL_META 的未知工具默认确认（fail closed：未知权限不得自动执行）。
    """
    meta = TOOL_META.get(name)
    if meta is None:
        return True   # 未知工具：默认要求确认（fail closed）
    if not meta["requires_confirmation"]:
        return False
    if name == "run_shell":
        return not _is_readonly_shell((args.get("command") or "").strip())
    return True


def _confirm_policy(name: str, args: dict) -> str:
    """集中权限入口：按当前问询模式决定工具处理方式。
    返回：allow（直接执行）/ ask（需用户确认）/ deny（直接拒绝）

    模式语义：
    - trusted：放行确认环节，但危险命令黑名单（执行时硬拦截）、工作区路径限制
      （_safe_join）、工具调用数量/时间上限（agent 循环）仍全部生效；
    - query：仅放行无副作用查询（含简单只读 shell）；run_code/写文件/删除/移动/
      Shell 复合/启动进程/Git 提交/MCP 写操作/屏幕输入一律拒绝；
    - strict：所有非只读操作确认；
    - auto：按 requires_confirmation + run_shell 只读判定；MCP 写操作保守确认。
    """
    mode = _current_confirm_mode()
    if mode == "trusted":
        return "allow"
    is_query = _is_query_tool(name, args)
    if mode == "query":
        return "allow" if is_query else "deny"
    if mode == "strict":
        return "allow" if is_query else "ask"
    # auto
    if name.startswith("mcp_"):
        return "allow" if _is_readonly_mcp(name) else "ask"  # MCP 写操作保守确认
    return "allow" if not _needs_confirm(name, args) else "ask"
