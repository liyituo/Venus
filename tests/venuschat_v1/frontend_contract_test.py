"""Display-free contract checks for the isolated VenusChat V1 frontend."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "venuschat_v1"
sys.path.insert(0, str(ROOT / "src"))

from venuschat_v1 import theme  # noqa: E402
from venuschat_v1.project_hub_view import project_access_from_results  # noqa: E402
from venuschat_v1.worker_task_view import render_call_detail  # noqa: E402


class _TextWidgetStub:
    def __init__(self) -> None:
        self.content = ""
        self.state = None

    def configure(self, **kwargs) -> None:
        self.state = kwargs.get("state", self.state)

    def delete(self, _start, _end) -> None:
        self.content = ""

    def insert(self, _index, value) -> None:
        self.content = value


def main() -> None:
    files = {path.name: path.read_text(encoding="utf-8") for path in SRC.glob("*.py")}
    combined = "\n".join(files.values())
    assert "import chat" not in combined
    assert "import llm_server" not in combined
    assert "from chat_theme" not in combined
    assert "class VenusChatV1" in files["app.py"]
    assert "class ChatView" in files["chat_view.py"]
    assert "class SettingsView" in files["settings_view.py"]
    assert "今天，想完成什么？" in files["chat_view.py"]
    assert "backend_bridge" in files["chat_view.py"] or "ApiClient" in combined
    assert "我的任务" in files["chat_view.py"] and "团队任务" in files["chat_view.py"]
    assert "open_team_collab" in files["chat_view.py"]
    assert "class TeamCollabWindow" in files["team_collab_view.py"]
    assert "threading.Thread" in files["team_collab_view.py"]
    assert "/review" in files["team_collab_view.py"]
    assert "有效批准：" in files["team_collab_view.py"]
    assert 'self._action("merge")' in files["team_collab_view.py"]
    assert "提交变更" in files["team_collab_view.py"]
    assert "团队与成员" in files["settings_view.py"]
    assert "实际 Serve 身份" in files["settings_view.py"]
    assert "created_at" in files["settings_view.py"]
    assert 'self.app.bridge.submit("settings_team"' in files["settings_view.py"]
    assert '"status": "submitting"' in files["settings_view.py"]
    assert 'removeprefix("app_")' in files["settings_view.py"]
    assert "切回个人对话" in files["chat_view.py"]
    assert "派活目标：未指定" in files["chat_view.py"]
    assert "团队任务必须只发送派发内容" in files["backend_bridge.py"] or "Never forward the" in files["backend_bridge.py"]
    assert '"project_id": None' in files["backend_bridge.py"]
    assert "switch_backend_context" in files["app.py"]
    assert "class ProjectHubWindow" in files["project_hub_view.py"]
    detail_widget = _TextWidgetStub()
    render_call_detail(detail_widget, {"status": "approved"})
    assert '"approved"' in detail_widget.content
    assert detail_widget.state == "disabled"
    render_call_detail(detail_widget)
    assert detail_widget.content == "" and detail_widget.state == "disabled"
    assert project_access_from_results(200, 200, 403) == (True, False, 0)
    assert project_access_from_results(200, 200, 200) == (True, True, 0)
    assert project_access_from_results(404, 200, 403) == (False, False, 404)
    assert "self._set_management_enabled(can_manage)" in files["project_hub_view.py"]
    assert "if not self._can_manage_project:" in files["project_hub_view.py"]
    assert "threading.Thread" in files["project_hub_view.py"]
    assert "_poll_results" in files["project_hub_view.py"]
    assert "pending_claim" in files["project_hub_view.py"]
    assert "_show_pending_claim" in files["project_hub_view.py"]
    assert '"active" in projects' in files["project_hub_view.py"]
    assert "_show_secret_once" in files["project_hub_view.py"]
    assert "_preview_code" in files["project_hub_view.py"]
    assert "set_active_project" in files["project_api.py"]
    assert "list_hub_users" in files["project_api.py"]
    assert "复制此 Hub 终端码 VN" in files["project_hub_view.py"]
    assert '"/api/v1/projects/active"' in files["app.py"]
    assert "open_project_hub" in files["chat_view.py"]
    assert "手动认领" in files["chat_view.py"]
    assert "project_preference_key" in files["config_store.py"]
    assert "此 Hub 终端码（VN）" in files["settings_view.py"]
    assert "terminal_identity" in combined
    assert "VI-" in files["terminal_identity.py"]
    assert "本机安装码（VI，公开标识）" in files["settings_view.py"]
    assert "此 Hub 终端码（VN）" in files["project_hub_view.py"]
    assert "VI 码只辅助核对终端来源" in files["settings_view.py"]
    assert "post_with_optional_installation_code" in files["api_client.py"]
    assert 'request_payload["installation_code"] = installation_code' in files["settings_view.py"]
    assert '"installation_code_binding"' in files["settings_view.py"]
    assert 'if installation_code and installation_code != self._installation_code:' in files["settings_view.py"]
    assert "打开 Agent 任务面板" in files["project_hub_view.py"]
    agent_nav = files["project_hub_view.py"].split("    def open_agent_tasks(self)", 1)[1].split("    def _selected_project", 1)[0]
    assert agent_nav.index("self.withdraw()") < agent_nav.index("self.app.show_chat()")
    assert agent_nav.index("chat_view._request_jobs_refresh()") < agent_nav.index("self.destroy()")
    assert "self.deiconify()" in agent_nav and "self.lift()" in agent_nav
    assert "Worker 派活与审批使用独立队列" in files["project_hub_view.py"]
    assert "_set_action_buttons_busy" in files["project_hub_view.py"]
    assert "X-Team-Device-Token" in files["api_client.py"]
    assert "deny_redirects=True" in files["settings_view.py"]
    assert "模型与推理" in files["settings_view.py"]
    assert theme.luminance(theme.CANVAS) > theme.luminance(theme.INK)
    assert theme.luminance(theme.SURFACE) > theme.luminance(theme.INK_SOFT)
    assert theme.TERRACOTTA != theme.SUCCESS
    print("PASS VenusChat V1 isolated frontend contract")


if __name__ == "__main__":
    main()
