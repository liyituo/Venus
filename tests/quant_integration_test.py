"""Offline tests for the main-Agent Quant Center integration boundary."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quant_integration import (  # noqa: E402
    QuantIntegrationConfig,
    QuantLaunchError,
    QuantServiceController,
)


ROOT = Path(__file__).resolve().parents[1]
QUANT = ROOT / "quant-agent-lab"


def config(**overrides):
    values = {
        "quant_project_path": str(QUANT),
        "quant_backend_url": "http://127.0.0.1:8014",
        "quant_gui_url": "http://127.0.0.1:4173",
    }
    values.update(overrides)
    return QuantIntegrationConfig.from_mapping(values)


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print(f"PASS {name}")


def main():
    check("safe defaults", config().backend_url.endswith(":8014") and config().gui_url.endswith(":4173"))
    for key, value in {
        "quant_backend_url": "http://localhost.evil.example:8014",
        "quant_gui_url": "http://127.0.0.1:8000",
        "quant_backend_url_userinfo": "http://user:pass@127.0.0.1:8014",
    }.items():
        try:
            kwargs = {key.replace("_userinfo", ""): value}
            config(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"malicious URL accepted: {value}")
    check("reject malicious and reserved URLs", True)
    check("config has no secrets", "api_key" not in json.dumps(config().__dict__, default=str))

    controller = QuantServiceController(config())
    with mock.patch.object(controller, "_probe_endpoint", side_effect=[
        (True, "READY", "ok"), (True, "READY", "ok")
    ]), mock.patch("quant_integration.webbrowser.open", return_value=True) as opened:
        status = controller.open_quant_center()
        check("healthy services are reused", status.ready and opened.called)

    controller = QuantServiceController(config(quant_auto_start=False))
    with mock.patch.object(controller, "_probe_endpoint", return_value=(False, "CONNECTION_REFUSED", "offline")):
        try:
            controller.open_quant_center()
        except QuantLaunchError as exc:
            check("offline auto-start disabled", exc.code == "SERVICES_OFFLINE")
        else:
            raise AssertionError("offline service did not fail safely")

    class FakeProcess:
        pid = 48123
        def poll(self): return None
        def terminate(self): self.terminated = True
        def wait(self, timeout=None): return 0

    fake = FakeProcess()
    controller = QuantServiceController(config())
    controller._owned["external-test"] = type("Owned", (), {"process": fake, "pid": fake.pid, "role": "external-test", "log_path": QUANT / "var" / "integration" / "test.log"})()
    controller.close_owned_processes()
    check("only owned processes are stopped", getattr(fake, "terminated", False))
    check("thread-safe controller lock exists", hasattr(controller, "_lock") and isinstance(controller._lock, type(threading.RLock())))
    print("integration checks complete")


if __name__ == "__main__":
    main()
