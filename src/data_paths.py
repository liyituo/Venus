"""统一数据目录解析。

所有程序数据（会话、备份、日志、secure store、chat/Telegram/MCP 配置、
schedules、PID 元数据）统一落在 ``PCAGENT_DATA_DIR`` 指向的目录：

1. 显式设置 ``PCAGENT_DATA_DIR``（测试用它指向独立临时目录）时优先；
2. 否则默认项目根目录下 ``.pcagent/``。

任何模块不得再各自拼接用户目录或项目根路径。
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def data_dir() -> Path:
    """返回程序数据根目录（PCAGENT_DATA_DIR 优先，否则项目根 .pcagent/）。"""
    override = os.environ.get("PCAGENT_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return BASE_DIR.parent / ".pcagent"


def data_file(name: str) -> Path:
    """返回数据目录下的具体文件路径。"""
    return data_dir() / name
