"""扩展注册表：插件 catalog、启用状态、安装到 skills/agents/MCP。"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Any

from data_paths import data_dir

log = logging.getLogger("extension_registry")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CATALOG = _REPO_ROOT / "plugins" / "catalog.json"
_STATE_FILE = lambda: data_dir() / "extensions.json"
_INSTALLED = lambda: data_dir() / "plugins" / "installed"
_LOCK = threading.Lock()

VALID_TYPES = frozenset({"skill", "agent", "mcp", "tool_pack"})


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


def load_state() -> dict:
    with _LOCK:
        state = _read_json(_STATE_FILE(), {"enabled": [], "settings": {}})
        if not isinstance(state, dict):
            state = {"enabled": [], "settings": {}}
        state.setdefault("enabled", [])
        state.setdefault("settings", {})
        return state


def save_state(state: dict) -> None:
    with _LOCK:
        _write_json(_STATE_FILE(), state)


def load_catalog() -> list[dict]:
    raw = _read_json(_CATALOG, {"plugins": []})
    plugins = raw.get("plugins") if isinstance(raw, dict) else raw
    if not isinstance(plugins, list):
        return []
    out: list[dict] = []
    for item in plugins:
        if isinstance(item, dict) and item.get("id"):
            out.append(item)
    return out


def catalog_entry(plugin_id: str) -> dict | None:
    for item in load_catalog():
        if item.get("id") == plugin_id:
            return item
    return None


def list_extensions() -> dict:
    catalog = load_catalog()
    state = load_state()
    enabled = set(state.get("enabled") or [])
    installed_dir = _INSTALLED()
    installed_ids = {p.name for p in installed_dir.iterdir()} if installed_dir.exists() else set()
    items = []
    for entry in catalog:
        pid = entry["id"]
        items.append({
            **{k: entry.get(k) for k in ("id", "name", "version", "type", "description", "author")},
            "builtin": bool(entry.get("builtin")),
            "installed": pid in installed_ids or bool(entry.get("builtin")),
            "enabled": pid in enabled,
        })
    return {"ok": True, "extensions": items, "enabled": sorted(enabled)}


def _plugin_source_dir(entry: dict) -> Path | None:
    rel = (entry.get("source") or "").strip()
    if not rel:
        return None
    root = _REPO_ROOT.resolve()
    src = (root / rel).resolve()
    try:
        src.relative_to(root)
    except ValueError:
        return None
    if not src.is_dir():
        return None
    return src


def install_plugin(plugin_id: str) -> tuple[bool, str]:
    entry = catalog_entry(plugin_id)
    if entry is None:
        return False, f"插件不在 catalog 中：{plugin_id}"
    src = _plugin_source_dir(entry)
    if src is None or not src.is_dir():
        return False, f"插件源目录不存在：{entry.get('source')}"
    dest = _INSTALLED() / plugin_id
    with _LOCK:
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
    return True, f"已安装：{plugin_id}"


def _deploy_skill(plugin_id: str, src: Path) -> None:
    skill_dir = src / "skill"
    if not skill_dir.is_dir():
        return
    name = plugin_id
    manifest = _read_json(src / "manifest.json", {})
    if isinstance(manifest, dict) and manifest.get("skill_name"):
        name = str(manifest["skill_name"])
    target = _REPO_ROOT / "skills" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(skill_dir, target)


def _deploy_agent(plugin_id: str, src: Path) -> None:
    agents = list(src.glob("agents/*.json"))
    if not agents:
        agent_file = src / "agent.json"
        if agent_file.exists():
            agents = [agent_file]
    dest_dir = _REPO_ROOT / "agents"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for agent in agents:
        shutil.copy2(agent, dest_dir / agent.name)


def _deploy_mcp(plugin_id: str, src: Path) -> None:
    mcp_file = src / "mcp.json"
    if not mcp_file.exists():
        return
    raw = _read_json(mcp_file, {})
    servers = raw.get("servers") if isinstance(raw, dict) else raw
    if not isinstance(servers, dict) or not servers:
        return
    from browser_tools import merge_mcp_servers
    merge_mcp_servers(servers)


def get_setting(key: str, default: Any = None) -> Any:
    state = load_state()
    settings = state.get("settings") or {}
    if not isinstance(settings, dict):
        return default
    return settings.get(key, default)


def set_setting(key: str, value: Any) -> None:
    state = load_state()
    settings = dict(state.get("settings") or {})
    settings[key] = value
    state["settings"] = settings
    save_state(state)


def enable_plugin(plugin_id: str) -> tuple[bool, str]:
    entry = catalog_entry(plugin_id)
    if entry is None:
        return False, f"插件不在 catalog 中：{plugin_id}"
    src = _INSTALLED() / plugin_id
    if not src.exists():
        ok, msg = install_plugin(plugin_id)
        if not ok:
            return False, msg
        src = _INSTALLED() / plugin_id
    ptype = str(entry.get("type") or "")
    try:
        if ptype == "skill":
            _deploy_skill(plugin_id, src)
        elif ptype == "agent":
            _deploy_agent(plugin_id, src)
        elif ptype == "mcp":
            _deploy_mcp(plugin_id, src)
    except OSError as exc:
        return False, f"部署失败：{exc}"
    state = load_state()
    enabled = list(state.get("enabled") or [])
    if plugin_id not in enabled:
        enabled.append(plugin_id)
    state["enabled"] = enabled
    save_state(state)
    return True, f"已启用：{plugin_id}"


def disable_plugin(plugin_id: str) -> tuple[bool, str]:
    state = load_state()
    enabled = [x for x in (state.get("enabled") or []) if x != plugin_id]
    if len(enabled) == len(state.get("enabled") or []):
        return False, f"插件未启用：{plugin_id}"
    state["enabled"] = enabled
    save_state(state)
    return True, f"已禁用：{plugin_id}"
