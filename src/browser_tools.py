"""浏览器一等公民：依赖探测、MCP chrome 启停、别名工具转发。"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Any, Callable

from data_paths import data_dir

log = logging.getLogger("browser_tools")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MCP_CONFIG = _REPO_ROOT / "mcp_config.json"
_CHROME_SERVER = "chrome"
_LOCK = threading.RLock()

DEFAULT_CHROME_SERVER = {
    "command": "npx",
    "args": ["-y", "@playwright/mcp@latest", "--browser", "chrome"],
    "read_only_tools": ["browser_snapshot", "browser_tabs"],
    "write_tools": [
        "browser_navigate", "browser_click", "browser_type",
        "browser_fill", "browser_press_key", "browser_scroll",
        "browser_drag", "browser_hover", "browser_select_option",
        "browser_close", "browser_wait_for",
    ],
}

# 别名 -> (MCP 工具名, 参数映射：别名参数名 -> MCP 参数名)
BROWSER_ALIASES: dict[str, tuple[str, dict[str, str]]] = {
    "browser_open": ("browser_navigate", {"url": "url"}),
    "browser_snapshot": ("browser_snapshot", {}),
    "browser_click": ("browser_click", {"element": "element", "ref": "ref"}),
    "browser_type": ("browser_type", {"element": "element", "ref": "ref", "text": "text"}),
    "browser_tabs": ("browser_tabs", {"action": "action"}),
}

ALIAS_TOOL_NAMES = frozenset(BROWSER_ALIASES)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def mcp_config_path() -> Path:
    return _MCP_CONFIG


def load_mcp_servers() -> dict:
    raw = _read_json(_MCP_CONFIG, {})
    servers = raw.get("servers") if isinstance(raw, dict) else {}
    return servers if isinstance(servers, dict) else {}


def _backup_mcp_config() -> None:
    if not _MCP_CONFIG.exists():
        return
    backup = data_dir() / "mcp_config.backup.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(_MCP_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")


def merge_mcp_servers(servers: dict[str, dict]) -> None:
    """合并 MCP server 配置到 mcp_config.json（保留已有项）。"""
    if not servers:
        return
    with _LOCK:
        _backup_mcp_config()
        raw = _read_json(_MCP_CONFIG, {})
        if not isinstance(raw, dict):
            raw = {}
        existing = raw.get("servers") or {}
        if not isinstance(existing, dict):
            existing = {}
        existing.update(servers)
        raw["servers"] = existing
        _write_json(_MCP_CONFIG, raw)


def remove_mcp_servers(names: list[str]) -> None:
    with _LOCK:
        raw = _read_json(_MCP_CONFIG, {})
        if not isinstance(raw, dict):
            return
        servers = raw.get("servers") or {}
        if not isinstance(servers, dict):
            return
        for name in names:
            servers.pop(name, None)
        raw["servers"] = servers
        _write_json(_MCP_CONFIG, raw)


def _extension_settings() -> dict:
    from extension_registry import load_state
    state = load_state()
    settings = state.get("settings") or {}
    return settings if isinstance(settings, dict) else {}


def _set_extension_setting(key: str, value: Any) -> None:
    from extension_registry import load_state, save_state
    state = load_state()
    settings = dict(state.get("settings") or {})
    settings[key] = value
    state["settings"] = settings
    save_state(state)


def is_browser_enabled() -> bool:
    settings = _extension_settings()
    if settings.get("browser_enabled") is True:
        return True
    return _CHROME_SERVER in load_mcp_servers()


def check_dependencies() -> dict[str, Any]:
    """探测 node/npx/chrome 可用性。"""
    node = shutil.which("node")
    npx = shutil.which("npx")
    chrome_paths = []
    if shutil.which("chrome"):
        chrome_paths.append("PATH:chrome")
    for candidate in (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path("/usr/bin/google-chrome"),
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ):
        if candidate.exists():
            chrome_paths.append(str(candidate))
    ready = bool(npx or node)
    return {
        "node": node,
        "npx": npx,
        "chrome": chrome_paths[0] if chrome_paths else None,
        "chrome_candidates": chrome_paths,
        "ready": ready,
        "mcp_configured": _CHROME_SERVER in load_mcp_servers(),
        "enabled": is_browser_enabled(),
    }


def browser_status() -> dict[str, Any]:
    deps = check_dependencies()
    servers = load_mcp_servers()
    chrome_cfg = servers.get(_CHROME_SERVER)
    return {
        "ok": True,
        "enabled": is_browser_enabled(),
        "dependencies": deps,
        "chrome_server": chrome_cfg is not None,
        "aliases": sorted(ALIAS_TOOL_NAMES),
    }


def enable_browser() -> tuple[bool, str]:
    deps = check_dependencies()
    if not deps["ready"]:
        return False, "缺少 node/npx，无法启动 Playwright MCP（请先安装 Node.js）"
    with _LOCK:
        merge_mcp_servers({_CHROME_SERVER: dict(DEFAULT_CHROME_SERVER)})
        _set_extension_setting("browser_enabled", True)
    return True, "已启用浏览器 MCP（chrome server 已写入 mcp_config.json，重启对话或等待 MCP 重连后生效）"


def disable_browser() -> tuple[bool, str]:
    with _LOCK:
        remove_mcp_servers([_CHROME_SERVER])
        _set_extension_setting("browser_enabled", False)
    return True, "已禁用浏览器 MCP（chrome server 已从 mcp_config.json 移除）"


def alias_tool_definitions() -> list[dict]:
    """返回 AGENT_TOOLS 格式的浏览器别名工具定义。"""
    return [
        {"type": "function", "function": {
            "name": "browser_open",
            "description": "在浏览器中打开 URL（内部转发 Playwright MCP browser_navigate）。需已启用浏览器工具。",
            "parameters": {"type": "object",
                           "properties": {"url": {"type": "string", "description": "要打开的 URL"}},
                           "required": ["url"]},
        }},
        {"type": "function", "function": {
            "name": "browser_snapshot",
            "description": "获取当前页面的可访问性快照（只读，用于理解页面结构后再点击/输入）。",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "browser_click",
            "description": "点击页面元素。ref 来自 browser_snapshot 返回的快照。",
            "parameters": {"type": "object",
                           "properties": {
                               "ref": {"type": "string", "description": "快照中的元素 ref"},
                               "element": {"type": "string", "description": "可选：人类可读元素描述"},
                           },
                           "required": ["ref"]},
        }},
        {"type": "function", "function": {
            "name": "browser_type",
            "description": "在输入框中输入文字。ref 来自 browser_snapshot。",
            "parameters": {"type": "object",
                           "properties": {
                               "ref": {"type": "string"},
                               "text": {"type": "string"},
                               "element": {"type": "string"},
                           },
                           "required": ["ref", "text"]},
        }},
        {"type": "function", "function": {
            "name": "browser_tabs",
            "description": "列出或切换浏览器标签页。action: list | new | close | select。",
            "parameters": {"type": "object",
                           "properties": {
                               "action": {"type": "string",
                                          "enum": ["list", "new", "close", "select"],
                                          "default": "list"},
                           }},
        }},
    ]


def _map_alias_args(alias: str, args: dict) -> dict:
    mcp_tool, mapping = BROWSER_ALIASES[alias]
    if not mapping:
        return dict(args)
    out: dict[str, Any] = {}
    for src, dst in mapping.items():
        if src in args and args[src] is not None:
            out[dst] = args[src]
    for key, value in args.items():
        if key not in mapping and value is not None:
            out[key] = value
    return out


def execute_alias(
    alias: str,
    args: dict,
    mcp_call: Callable[[str, str], tuple[bool, str]],
) -> tuple[bool, str]:
    """执行浏览器别名工具，mcp_call(tool_name, arguments_json) 由 llm_server 注入。"""
    if alias not in BROWSER_ALIASES:
        return False, f"未知浏览器别名：{alias}"
    if not is_browser_enabled():
        return False, "浏览器工具未启用。请调用 POST /api/v1/browser/enable 或在 mcp_config.json 配置 chrome server"
    mcp_tool, _ = BROWSER_ALIASES[alias]
    mcp_name = f"mcp_{_CHROME_SERVER}_{mcp_tool}"
    payload = _map_alias_args(alias, args)
    if alias == "browser_open" and not (payload.get("url") or "").strip():
        return False, "browser_open 需要 url 参数"
    return mcp_call(mcp_name, json.dumps(payload, ensure_ascii=False))


def reload_mcp_hint() -> str:
    """返回 MCP 重载提示（实际重连由 mcp_client 退避机制处理）。"""
    return "若工具列表未更新，请重启 llm_server 或等待 MCP 自动重连。"
