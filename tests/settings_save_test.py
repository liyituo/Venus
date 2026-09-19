"""Current VenusChat settings payloads, using stubs rather than a Tk display."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from venuschat_v1 import settings_view as settings  # noqa: E402
from venuschat_v1.api_client import ApiClient  # noqa: E402


class SettingsSaveTest(unittest.TestCase):
    def setUp(self):
        self.view = settings.SettingsView.__new__(settings.SettingsView)
        self.view.app = Mock()
        self.view.client = Mock()
        self.view.client.post.return_value = (200, {"config": {"model": "chosen"}})
        self.view.local_controls = {}
        self.patches = [patch.object(settings, "save_local_config"),
                        patch.object(settings, "load_config", return_value={"llm_base": "http://localhost:9001"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_model_reasoning_and_masked_keys(self):
        self.view.active_page = "model"
        for label, expected in (("最高", "max"), ("高", "high"), ("关闭", "off")):
            for masked in ("", "***", "••••••", "__secure__"):
                with self.subTest(label=label, key=masked):
                    self.view.local_controls = {
                        "API URL": Mock(get=lambda: " https://api.example.invalid/v1 "),
                        "API Key": Mock(get=lambda: masked),
                        "模型": Mock(get=lambda: " chosen "),
                        "reasoning": SimpleNamespace(value=label),
                    }
                    self.view._save()
                    self.view.client.post.assert_called_with("/api/v1/config", {
                        "api_url": "https://api.example.invalid/v1", "model": "chosen",
                        "reasoning_mode": expected,
                    })
        self.view.local_controls["API Key"] = Mock(get=lambda: " new-key ")
        self.view._save()
        self.assertEqual(self.view.client.post.call_args.args[1]["api_key"], "new-key")

    def test_router_boolean_values(self):
        self.view.active_page = "router"
        for enabled in (True, False):
            self.view.local_controls = {"tool_router": SimpleNamespace(value=enabled)}
            self.view._save()
            self.view.client.post.assert_called_with("/api/v1/config", {"tool_router": enabled})

    def test_confirmation_modes(self):
        self.view.active_page = "permissions"
        for label, expected in (("智能", "auto"), ("严格", "strict"), ("只读", "query")):
            self.view.local_controls = {"confirm_mode": SimpleNamespace(value=label)}
            self.view._save()
            self.view.client.post.assert_called_with("/api/v1/confirm-mode", {"mode": expected})

    def test_workspace_payload(self):
        self.view.active_page = "common"
        self.view.local_controls = {"默认工作区": Mock(get=lambda: " C:/work ")}
        self.view._save()
        self.view.client.post.assert_called_with("/api/v1/workspace", {"path": "C:/work"})

    def test_failed_model_save_keeps_settings_open(self):
        self.view.active_page = "model"
        self.view.local_controls = {"模型": Mock(get=lambda: "chosen")}
        self.view.client.post.return_value = (422, {"detail": "invalid"})
        self.view._save()
        self.view.app.show_chat.assert_not_called()
        self.assertIn("invalid", self.view.app.toast.call_args.args[0])
        settings.save_local_config.assert_not_called()

    def test_requests_use_selected_backend(self):
        client = ApiClient("http://127.0.0.1:9099")
        response = Mock(status=200, read=lambda: b'{"ok":true}')
        with patch.object(client, "_headers", return_value={}), \
                patch("urllib.request.urlopen") as open_url:
            open_url.return_value.__enter__.return_value = response
            self.assertEqual(client.post("/api/v1/config", {"model": "chosen"})[0], 200)
            self.assertEqual(open_url.call_args.args[0].full_url, "http://127.0.0.1:9099/api/v1/config")


if __name__ == "__main__":
    unittest.main()
