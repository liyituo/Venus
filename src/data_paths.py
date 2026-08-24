"""统一数据目录解析。

所有程序数据（会话、备份、日志、secure store、chat/Telegram/MCP 配置、
schedules、PID 元数据）统一落在数据目录：

1. 显式设置 ``VENUS_DATA_DIR``（或兼容 ``PCAGENT_DATA_DIR``）时优先；
2. 否则默认项目根目录下 ``.venus/``（首次启动自动从 ``.pcagent/`` 迁移）。

工作区内数据（todos、backups、sandbox_tmp）使用 ``workspace_data_dir()``，
同样自动从工作区内的 ``.pcagent/`` 迁移到 ``.venus/``。

任何模块不得再各自拼接 ``.pcagent`` 或 ``.venus`` 路径。
"""

from __future__ import annotations

from pathlib import Path

from brand import (
    CLI_CONFIG_NAME,
    DATA_DIR_NAME,
    ENV_DATA_DIR,
    LEGACY_CLI_CONFIG_NAME,
    LEGACY_DATA_DIR_NAME,
    env_flag,
)
from data_migration import ensure_data_dir, migrate_cli_config

BASE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = BASE_DIR.parent


def data_dir() -> Path:
    """返回程序数据根目录（VENUS_DATA_DIR / PCAGENT_DATA_DIR 优先）。"""
    override = env_flag(ENV_DATA_DIR)
    if override:
        return Path(override).expanduser().resolve()
    legacy = _REPO_ROOT / LEGACY_DATA_DIR_NAME
    target = _REPO_ROOT / DATA_DIR_NAME
    ensure_data_dir(legacy, target)
    return target


def data_file(name: str) -> Path:
    """返回数据目录下的具体文件路径。"""
    return data_dir() / name


def workspace_data_dir(workspace: Path) -> Path:
    """工作区内的数据目录（.venus），自动从 .pcagent 迁移。"""
    ws = workspace.expanduser().resolve()
    legacy = ws / LEGACY_DATA_DIR_NAME
    target = ws / DATA_DIR_NAME
    ensure_data_dir(legacy, target)
    return target


def cli_config_path() -> Path:
    """CLI 连接配置路径（~/.venus.json），自动从 ~/.pcagent.json 迁移。"""
    return migrate_cli_config(LEGACY_CLI_CONFIG_NAME, CLI_CONFIG_NAME)
