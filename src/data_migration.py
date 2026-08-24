"""数据目录迁移：.pcagent → .venus（幂等、可合并）。"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path

log = logging.getLogger("data_migration")

_LOCK = threading.RLock()
MARKER_NAME = ".migrated_from_pcagent"


def _marker_path(target: Path) -> Path:
    return target / MARKER_NAME


def _write_marker(target: Path, legacy: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    payload = {
        "from": str(legacy),
        "to": str(target),
        "migrated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _marker_path(target).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _merge_tree(src: Path, dst: Path) -> None:
    """把 legacy 树合并进 target（不覆盖已存在文件）。"""
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        out = dst / rel
        if item.is_dir():
            out.mkdir(parents=True, exist_ok=True)
            continue
        if out.exists():
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, out)


def _retire_legacy(legacy: Path) -> None:
    readme = legacy / "README_MIGRATED.txt"
    try:
        readme.write_text(
            "此目录已迁移到 .venus/。\n"
            "若无特殊需要可手动删除本目录（建议确认 .venus/ 数据完整后再删）。\n",
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("写入 legacy README 失败：%s", exc)


def ensure_data_dir(legacy: Path, target: Path) -> tuple[bool, str]:
    """确保 target 存在；若仅有 legacy 则迁移（move 或 merge）。"""
    legacy = legacy.resolve()
    target = target.resolve()
    with _LOCK:
        if _marker_path(target).exists():
            target.mkdir(parents=True, exist_ok=True)
            return True, "already migrated"

        if target.exists() and any(target.iterdir()):
            if legacy.exists() and any(legacy.iterdir()):
                _merge_tree(legacy, target)
                _write_marker(target, legacy)
                _retire_legacy(legacy)
                log.info("已合并迁移 %s → %s", legacy, target)
                return True, "merged"
            target.mkdir(parents=True, exist_ok=True)
            return True, "target exists"

        if legacy.exists() and any(legacy.iterdir()):
            try:
                if not target.exists():
                    shutil.move(str(legacy), str(target))
                    _write_marker(target, legacy)
                    log.info("已移动迁移 %s → %s", legacy, target)
                    return True, "moved"
            except OSError as exc:
                log.warning("move 失败，改 merge：%s", exc)
            target.mkdir(parents=True, exist_ok=True)
            _merge_tree(legacy, target)
            _write_marker(target, legacy)
            _retire_legacy(legacy)
            log.info("已复制合并迁移 %s → %s", legacy, target)
            return True, "merged"

        target.mkdir(parents=True, exist_ok=True)
        return True, "initialized"


def migrate_cli_config(legacy_name: str, target_name: str) -> Path:
    """~/.pcagent.json → ~/.venus.json（保留旧文件）。"""
    home = Path.home()
    legacy = home / legacy_name
    target = home / target_name
    with _LOCK:
        if target.exists():
            return target
        if legacy.exists():
            try:
                shutil.copy2(legacy, target)
                log.info("已迁移 CLI 配置 %s → %s", legacy, target)
            except OSError as exc:
                log.warning("CLI 配置迁移失败：%s", exc)
        return target
