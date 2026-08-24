#!/usr/bin/env python3
"""一次性迁移：项目根 .pcagent → .venus，工作区 .pcagent → .venus，~/.pcagent.json → ~/.venus.json。

可重复执行（幂等）。建议在升级后、重启服务前运行一次。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from brand import CLI_CONFIG_NAME, DATA_DIR_NAME, LEGACY_CLI_CONFIG_NAME, LEGACY_DATA_DIR_NAME
from data_migration import ensure_data_dir, migrate_cli_config
from data_paths import cli_config_path, data_dir


def main() -> int:
    repo_legacy = ROOT / LEGACY_DATA_DIR_NAME
    repo_target = ROOT / DATA_DIR_NAME
    ok, msg = ensure_data_dir(repo_legacy, repo_target)
    print(f"[repo] {repo_target}: {msg}")

    cfg = cli_config_path()
    print(f"[cli]  config: {cfg}")

    ws = Path.home() / "agent_workspace"
    if ws.is_dir():
        from data_paths import workspace_data_dir
        wd = workspace_data_dir(ws)
        print(f"[workspace] {ws} -> {wd}")

    print(f"[ok] data_dir = {data_dir()}")
    legacy_cfg = Path.home() / LEGACY_CLI_CONFIG_NAME
    if legacy_cfg.exists() and cfg.exists():
        print(f"     legacy CLI config kept at {legacy_cfg}")
    if repo_legacy.exists():
        print(f"     legacy data dir still present: {repo_legacy} (可确认后手动删除)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
