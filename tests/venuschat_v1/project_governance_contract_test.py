"""Display-free client contracts for multi-party project governance."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from venuschat_v1.project_governance_view import (  # noqa: E402
    GovernanceRequestIdCache,
    build_governance_proposal_payload,
    governance_can_accept_transfer,
    governance_can_propose,
    governance_can_vote,
    governance_capacity_message,
    governance_eligible_voter_count,
    governance_required_votes,
    governance_proposal_summary,
    governance_voter_is_eligible,
)
from venuschat_v1.project_hub_view import invite_role_allowed  # noqa: E402


def main() -> None:
    members = [
        {"user_id": "owner", "role": "owner", "status": "active",
         "devices": [{"status": "active"}]},
        {"user_id": "member", "name": "Mina", "role": "member", "status": "active",
         "devices": [{"status": "active"}]},
        {"user_id": "viewer", "role": "viewer", "status": "active",
         "devices": [{"status": "active"}]},
        {"user_id": "offline", "role": "admin", "status": "active",
         "devices": [{"status": "revoked"}]},
        {"user_id": "removed", "role": "admin", "status": "removed",
         "devices": [{"status": "active"}]},
    ]
    assert governance_voter_is_eligible(members, "member", "owner")
    assert not governance_voter_is_eligible(members, "owner", "owner")
    assert not governance_voter_is_eligible(members, "viewer", "owner")
    assert not governance_voter_is_eligible(members, "offline", "owner")
    assert not governance_voter_is_eligible(members, "removed", "owner")
    assert governance_eligible_voter_count(members, "owner") == 1

    request_ids = GovernanceRequestIdCache()
    request_signature = ("promote_admin", "member", "")
    first_request_id = request_ids.for_signature(request_signature)
    assert request_ids.for_signature(request_signature) == first_request_id
    request_ids.invalidate_if_changed(("owner_transfer", "member", ""))
    assert request_ids.for_signature(("owner_transfer", "member", "")) != first_request_id
    request_ids.clear()
    assert request_ids.for_signature(request_signature) != first_request_id

    project = {"status": "active", "role": "owner", "required_approvals": 2}
    assert governance_can_propose(project)
    assert not governance_can_propose({**project, "role": "admin"})
    assert not governance_can_propose({**project, "status": "archived"})
    assert not governance_can_propose(project, read_only=True)
    message = governance_capacity_message(project, members, "owner")
    assert "1 位" in message and "2 票" in message
    assert governance_required_votes({}) == 0
    unknown_threshold_message = governance_capacity_message(
        {}, members, "owner")
    assert "至少需要 2 票" in unknown_threshold_message
    assert "无法达到" in unknown_threshold_message

    proposal = {"status": "pending", "proposer_user_id": "owner"}
    assert governance_can_vote(project, proposal, members, "member")
    assert not governance_can_vote(project, proposal, members, "owner")
    assert not governance_can_vote(project, {**proposal, "status": "approved"},
                                   members, "member")
    assert not governance_can_vote({**project, "status": "archived"}, proposal,
                                   members, "member")

    transfer = {"transfer_id": "transfer-1", "to_user_id": "member"}
    assert governance_can_accept_transfer(project, transfer, "member")
    assert not governance_can_accept_transfer(project, transfer, "owner")
    assert not governance_can_accept_transfer({**project, "status": "archived"},
                                              transfer, "member")

    assert build_governance_proposal_payload(
        "promote_admin", "req-1", target_user_id="member") == {
            "action": "promote_admin", "request_id": "req-1",
            "target_user_id": "member",
        }
    assert build_governance_proposal_payload(
        "lower_approval_threshold", "req-2", required_approvals=1,
        current_threshold=2) == {
            "action": "lower_approval_threshold", "request_id": "req-2",
            "required_approvals": 1,
        }
    assert build_governance_proposal_payload(
        "owner_transfer", "req-3", target_user_id="member")["action"] == "owner_transfer"
    for kwargs in (
        {"action": "promote_admin", "request_id": "req", "target_user_id": ""},
        {"action": "lower_approval_threshold", "request_id": "req",
         "required_approvals": 2, "current_threshold": 2},
    ):
        try:
            build_governance_proposal_payload(**kwargs)
            raise AssertionError("invalid governance proposal payload was accepted")
        except ValueError:
            pass

    summary = governance_proposal_summary({
        "action": "promote_admin", "target_user_id": "member",
        "status": "pending", "approvals": 1, "required_approvals": 2,
        "created_at": "now", "expires_at": "tomorrow",
    }, members)
    assert summary["description"] == "将 Mina 提升为管理员"
    assert summary["votes"] == "赞成 1/2"
    for status, label in (("applied", "已执行"), ("stale", "已失效")):
        assert governance_proposal_summary({"action": "owner_transfer", "status": status},
                                           members)["status"] == label
    assert invite_role_allowed("member") and invite_role_allowed("viewer")
    assert not invite_role_allowed("admin")
    print("PASS project governance proposal/vote/acceptance contracts")


if __name__ == "__main__":
    main()
