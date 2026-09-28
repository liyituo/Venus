"""Typed path helpers for the multi-project Hub API contract."""

from __future__ import annotations

from typing import Any

from .api_client import ApiClient


class ProjectHubApi:
    """Small, testable client wrapper for project membership endpoints."""

    def __init__(self, client: ApiClient) -> None:
        self.client = client

    def list_projects(self):
        return self.client.get("/api/v1/projects", timeout=10)

    def project_detail(self, project_id: str):
        return self.client.get(f"/api/v1/projects/{project_id}", timeout=10)

    def archive_project(self, project_id: str):
        return self.client.post(f"/api/v1/projects/{project_id}/archive", {}, timeout=12)

    def list_governance_proposals(self, project_id: str):
        return self.client.get(
            f"/api/v1/projects/{project_id}/governance-proposals", timeout=12)

    def governance_proposal(self, project_id: str, proposal_id: str):
        return self.client.get(
            f"/api/v1/projects/{project_id}/governance-proposals/{proposal_id}",
            timeout=12)

    def create_governance_proposal(self, project_id: str, action: str,
                                   request_id: str, *, target_user_id: str = "",
                                   required_approvals: int | None = None):
        body: dict[str, Any] = {"action": action, "request_id": request_id}
        if target_user_id:
            body["target_user_id"] = target_user_id
        if required_approvals is not None:
            body["required_approvals"] = required_approvals
        return self.client.post(
            f"/api/v1/projects/{project_id}/governance-proposals", body, timeout=15)

    def vote_governance_proposal(self, project_id: str, proposal_id: str,
                                 decision: str):
        return self.client.post(
            f"/api/v1/projects/{project_id}/governance-proposals/{proposal_id}/votes",
            {"decision": decision}, timeout=12)

    def pending_owner_transfer(self, project_id: str):
        return self.client.get(
            f"/api/v1/projects/{project_id}/owner-transfer/pending", timeout=12)

    def accept_owner_transfer(self, project_id: str):
        return self.client.post(
            f"/api/v1/projects/{project_id}/owner-transfer/accept", {}, timeout=12)

    def set_active_project(self, project_id: str):
        return self.client.post("/api/v1/projects/active", {
            "project_id": project_id,
        }, timeout=10)

    def create_project(self, name: str, description: str = ""):
        return self.client.post("/api/v1/projects", {
            "name": name, "description": description,
        }, timeout=15)

    def claim_project(self, project_id: str, claim_code: str):
        return self.client.post(f"/api/v1/projects/{project_id}/claim", {
            "claim_code": claim_code,
        }, timeout=12)

    def list_invites(self, project_id: str):
        return self.client.get(f"/api/v1/projects/{project_id}/invites", timeout=10)

    def create_invite(self, project_id: str, terminal_display_code: str,
                      user_id: str, role: str, expires_in: int | None = None):
        body: dict[str, Any] = {
            "terminal_display_code": terminal_display_code,
            "user_id": user_id,
            "role": role,
        }
        if expires_in is not None:
            body["expires_in"] = expires_in
        return self.client.post(f"/api/v1/projects/{project_id}/invites",
                                body, timeout=12)

    def preview_invite(self, invite_code: str):
        return self.client.post("/api/v1/project-invites/preview", {
            "invite_code": invite_code,
        }, timeout=10)

    def accept_invite(self, invite_id: str, invite_code: str):
        return self.client.post(
            f"/api/v1/project-invites/{invite_id}/accept",
            {"invite_code": invite_code}, timeout=12)

    def list_members(self, project_id: str):
        return self.client.get(f"/api/v1/projects/{project_id}/members", timeout=10)

    def list_hub_users(self):
        return self.client.get("/api/v1/team", timeout=10)

    def remove_member(self, project_id: str, user_id: str):
        return self.client.delete(
            f"/api/v1/projects/{project_id}/members/{user_id}", timeout=12)

    def revoke_device(self, project_id: str, device_id: str):
        return self.client.post(
            f"/api/v1/projects/{project_id}/devices/{device_id}/revoke", {}, timeout=12)

    def list_worker_calls(self, project_id: str):
        return self.client.get(f"/api/v1/worker/calls?project_id={project_id}", timeout=10)

    def create_worker_task(self, project_id: str, target_device_id: str,
                           tool: str, args: dict[str, Any], title: str = ""):
        return self.client.post("/api/v1/worker/tasks", {
            "project_id": project_id, "target_device_id": target_device_id,
            "tool": tool, "args": args, "title": title,
        }, timeout=15)

    def vote_worker_call(self, call_id: str, decision: str):
        return self.client.post(f"/api/v1/worker/calls/{call_id}/votes", {
            "decision": decision,
        }, timeout=12)
