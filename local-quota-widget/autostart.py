"""开机自启：在 Windows 启动文件夹放快捷方式。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SHORTCUT_NAME = "AI Quota Widget.lnk"
START_BAT = ROOT / "start.bat"


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path() -> Path:
    return startup_dir() / SHORTCUT_NAME


def is_enabled() -> bool:
    return shortcut_path().exists()


def set_enabled(enabled: bool) -> None:
    path = shortcut_path()
    if not enabled:
        if path.exists():
            path.unlink()
        return
    if not START_BAT.exists():
        raise FileNotFoundError(f"找不到启动脚本：{START_BAT}")
    path.parent.mkdir(parents=True, exist_ok=True)
    lnk = str(path)
    target = str(START_BAT)
    workdir = str(ROOT)
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut({json.dumps(lnk)}); "
        f"$s.TargetPath = {json.dumps(target)}; "
        f"$s.WorkingDirectory = {json.dumps(workdir)}; "
        "$s.WindowStyle = 7; "
        '$s.Description = "AI Quota Desktop Widget"; '
        "$s.Save()"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", script],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode != 0 or not path.exists():
        err = (completed.stderr or completed.stdout or "无法创建启动快捷方式").strip()
        raise OSError(err)
