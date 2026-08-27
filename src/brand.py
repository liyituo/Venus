"""产品品牌常量（单一真相源）。"""

from __future__ import annotations

PRODUCT_NAME = "Venus"
PRODUCT_NAME_UPPER = "VENUS"
PRODUCT_SLUG = "venus"
APP_VERSION = "0.10.1"
TAGLINE = "个人 Agent 调度台 · 派活即走 · 本地可控"
DAEMON_NAME = "Venus Daemon"

# 数据目录与 CLI 配置
DATA_DIR_NAME = ".venus"
LEGACY_DATA_DIR_NAME = ".pcagent"
CLI_CONFIG_NAME = ".venus.json"
LEGACY_CLI_CONFIG_NAME = ".pcagent.json"

# 环境变量：新名优先，旧名兼容
ENV_DATA_DIR = ("VENUS_DATA_DIR", "PCAGENT_DATA_DIR")
ENV_DISABLE_MCP = ("VENUS_DISABLE_MCP", "PCAGENT_DISABLE_MCP")
ENV_ALLOW_TEST_HOST = ("VENUS_ALLOW_TEST_HOST", "PCAGENT_ALLOW_TEST_HOST")

SYSTEM_IDENTITY = (
    f"\n\n你是 {PRODUCT_NAME}，可以控制用户电脑的智能体"
    "（工具操作、编写和修改代码）。\n"
)


def env_flag(names: tuple[str, ...]) -> str | None:
    """按顺序读取环境变量，返回第一个已设置的值。"""
    import os
    for name in names:
        val = os.environ.get(name)
        if val is not None:
            return val
    return None


def env_is_set(names: tuple[str, ...], expected: str = "1") -> bool:
    val = env_flag(names)
    return val == expected
