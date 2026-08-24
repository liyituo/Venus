"""统一数据目录解析。

所有程序数据（会话、备份、日志、secure store、chat/Telegram/MCP 配置、
schedules、PID 元数据）统一落在数据目录：

1. 显式设置 ``VENUS_DATA_DIR``（或兼容 ``PCAGENT_DATA_DIR``）时优先；
2. 否则默认项目根目录下 ``.pcagent/``（历史目录名，与产品名无关）。

任何模块不得再各自拼接用户目录或项目根路径。
"""

from __future__ import annotations

import os
from pathlib import Path

from brand import DATA_DIR_NAME, ENV_DATA_DIR, env_flag

BASE_DIR = Path(__file__).resolve().parent


def data_dir() -> Path:
    """返回程序数据根目录（VENUS_DATA_DIR / PCAGENT_DATA_DIR 优先）。"""
    override = env_flag(ENV_DATA_DIR)
    if override:
        return Path(override).expanduser().resolve()
    return BASE_DIR.parent / DATA_DIR_NAME


def data_file(name: str) -> Path:
    """返回数据目录下的具体文件路径。"""
    return data_dir() / name
