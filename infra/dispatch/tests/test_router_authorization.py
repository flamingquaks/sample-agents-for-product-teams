"""Tests for the Dispatch Router's authorization check.

Focuses on the fail-closed semantics around unresolved sender identities
(empty string, "unknown") and the empty-allowlist rejection, which together
prevent a misconfigured allowlist from becoming a universal bypass when the
upstream event receiver fails to resolve a stable identity (threat T-4).
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def router_module(monkeypatch):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-test")
    monkeypatch.setenv("ASSIGNMENTS_TABLE", "t")
    with patch("boto3.resource"), patch("boto3.client"):
        for name in ("router", "guardrail", "reply"):
            sys.modules.pop(name, None)
        import router as router_mod
    yield router_mod


def test_empty_allowlist_rejects_all(router_module):
    agent_config = {"agent_id": "workitems", "authorization": {"users": []}}
    assert router_module.check_authorization(agent_config, "alice", "github") is False


def test_missing_authorization_key_rejects_all(router_module):
    agent_config = {"agent_id": "workitems"}
    assert router_module.check_authorization(agent_config, "alice", "github") is False


def test_wildcard_allows_resolved_sender(router_module):
    agent_config = {"agent_id": "workitems", "authorization": {"users": ["*"]}}
    assert router_module.check_authorization(agent_config, "alice", "github") is True


@pytest.mark.parametrize("sender", ["", "unknown"])
def test_wildcard_does_not_allow_unresolved_sender(router_module, sender):
    agent_config = {"agent_id": "workitems", "authorization": {"users": ["*"]}}
    assert router_module.check_authorization(agent_config, sender, "asana") is False


@pytest.mark.parametrize("sender", ["", "unknown"])
def test_explicit_allowlist_does_not_allow_unresolved_sender(router_module, sender):
    agent_config = {
        "agent_id": "workitems",
        "authorization": {"users": [sender, "1202334567890123"]},
    }
    assert router_module.check_authorization(agent_config, sender, "asana") is False


def test_asana_gid_match(router_module):
    agent_config = {
        "agent_id": "workitems",
        "authorization": {"users": ["1202334567890123"]},
    }
    assert router_module.check_authorization(
        agent_config, "1202334567890123", "asana"
    ) is True
    assert router_module.check_authorization(
        agent_config, "9999999999999999", "asana"
    ) is False


# --- Cedar trigger-authz seam (back-compat + delegation) ---------------------
# With TRIGGER_POLICY_STORE_ID unset (the default, and how the fixture leaves the
# env), authorize_trigger MUST behave exactly like the legacy allowlist so the
# whole trigger-authz spine ships dark. When the store IS set, the decision comes
# from trigger_authz (AVP), not the allowlist.


def test_backcompat_uses_legacy_allowlist_when_store_unset(router_module, monkeypatch):
    monkeypatch.delenv("TRIGGER_POLICY_STORE_ID", raising=False)
    agent_config = {"agent_id": "workitems", "authorization": {"users": ["alice"]}}
    ok, reason = router_module.authorize_trigger(agent_config, "alice", "github")
    assert ok is True and reason == ""
    ok, reason = router_module.authorize_trigger(agent_config, "mallory", "github")
    assert ok is False and reason == "not-in-allowlist"


def test_unresolved_sender_rejected_before_avp(router_module, monkeypatch):
    # Even with the store configured, an unresolved sender is rejected locally and
    # never passed to AVP as a principal (threat T-4).
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    agent_config = {"agent_id": "workitems", "authorization": {"users": ["*"]}}
    with patch.object(router_module.trigger_authz, "is_authorized") as spy:
        ok, reason = router_module.authorize_trigger(agent_config, "", "slack", {})
    assert ok is False and reason == "unresolved-sender"
    spy.assert_not_called()


def test_cedar_path_allows_and_bypasses_allowlist(router_module, monkeypatch):
    # An empty allowlist would reject under the legacy path; the Cedar ALLOW wins,
    # proving the decision is delegated, not the allowlist.
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    agent_config = {"agent_id": "workitems", "authorization": {"users": []}}
    from trigger_authz import Decision

    with patch.object(
        router_module.trigger_authz, "is_authorized", return_value=Decision(allow=True)
    ):
        ok, reason = router_module.authorize_trigger(
            agent_config, "slack:T0ACME:U0ALICE", "slack", {"channel_id": "C0ENG"}
        )
    assert ok is True and reason == ""


def test_cedar_path_denies_with_reason(router_module, monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    agent_config = {"agent_id": "workitems", "authorization": {"users": ["*"]}}
    from trigger_authz import Decision

    with patch.object(
        router_module.trigger_authz,
        "is_authorized",
        return_value=Decision(allow=False, reason="policy:pol-1"),
    ):
        ok, reason = router_module.authorize_trigger(
            agent_config, "slack:T0ACME:U0MALLORY", "slack", {"channel_id": "C0SECRET"}
        )
    assert ok is False and reason == "policy:pol-1"
