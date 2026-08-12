"""Real local smoke test for the main-Agent quant service controller.

This test is intentionally opt-in because it starts local child services on
8014/4173.  It never opens a browser, generates a report, approves a plan or
executes Paper Trading.

注意：文件名不含 _test.py 后缀——run_all_tests.py 只自动发现 *_test.py，
本脚本不会在 CI 上跑（CI 无 Node/无本地子服务环境）。
手动运行：python tests/quant_integration_e2e.py
"""

from __future__ import annotations

import json
import shutil
import sys
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
QUANT = ROOT / "quant-agent-lab"
sys.path.insert(0, str(ROOT / "src"))

from quant_integration import QuantServiceController


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _environment_ready() -> bool:
    """环境守卫：quant 子项目与 Node 缺一不可；缺失时 SKIP（不失败）。

    防止脚本被误放进自动发现（改名后仍可能被手动/误配置在 CI 执行），
    缺失环境时安全跳过而不是抛 QuantLaunchError。
    """
    if not QUANT.is_dir():
        print("SKIP: quant-agent-lab/ 不存在（真实 E2E 需要完整子项目）")
        return False
    if shutil.which("node") is None:
        print("SKIP: 未找到 Node.js（GUI 构建需要 node）")
        return False
    return True


def main() -> None:
    if not _environment_ready():
        return
    config = {
        "quant_project_path": str(QUANT),
        "quant_backend_url": "http://127.0.0.1:8014",
        "quant_gui_url": "http://127.0.0.1:4173",
        "quant_auto_start": True,
        "quant_open_mode": "browser",
    }
    controller = QuantServiceController.from_mapping(config, startup_timeout=20)
    before_reports = {path.name for path in (QUANT / "var" / "reports").glob("*")}
    try:
        with mock.patch("quant_integration.webbrowser.open", return_value=True) as opened:
            first = controller.open_quant_center()
            second = controller.open_quant_center()
        assert first.ready and second.ready, (first.to_dict(), second.to_dict())
        assert opened.call_count == 2
        assert first.owned_pids == second.owned_pids

        backend = get_json("http://127.0.0.1:8014/api/v1/health")
        gui = get_json("http://127.0.0.1:4173/healthz")
        connection = get_json("http://127.0.0.1:4173/api/connection")
        dashboard = post_json(
            "http://127.0.0.1:4173/bridge",
            {"name": "quant_get_dashboard", "arguments": {}},
        )
        assert backend["status"] == "ok" and backend["live_broker"] == "disabled"
        assert gui["status"] == "ok" and gui["mode"] == "PAPER_TRADING"
        assert connection["status"] == "ok"
        assert dashboard["structuredContent"]["schema_version"] == "dashboard.v1"
        assert not ({path.name for path in (QUANT / "var" / "reports").glob("*")} - before_reports)

        root_html = urllib.request.urlopen("http://127.0.0.1:4173/#/dashboard", timeout=5).read().decode("utf-8")
        assert "返回主 Agent" in root_html and "PAPER ONLY" in root_html
        print(f"PASS real quant E2E; owned_pids={list(first.owned_pids)}")
        print("PASS backend 8014 + standalone GUI 4173 + dashboard bridge")
        print("PASS second open reused the same controller-owned PIDs")
    finally:
        controller.close_owned_processes()


if __name__ == "__main__":
    main()
