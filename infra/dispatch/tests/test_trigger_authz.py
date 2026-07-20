"""Tests for trigger_authz — the router's Cedar trigger-authorization check via
AVP (docs/specs/slack-connectors-spec.md §5.3).

Covers the tri-state Decision: allow=None when the store is unset (caller falls
back to the legacy allowlist), allow=True on ALLOW, allow=False on any non-ALLOW
or AVP error (fail-closed). Also asserts the IsAuthorized call is shaped
correctly — principal/action/resource, the workspace/channel/source context, and
group parents + the email attribute.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load():
    sys.modules.pop("trigger_authz", None)
    import trigger_authz

    return trigger_authz


def test_store_unset_returns_none_signal(monkeypatch):
    monkeypatch.delenv("TRIGGER_POLICY_STORE_ID", raising=False)
    ta = _load()
    d = ta.is_authorized(principal="slack:T:U", agent_id="workitems", source="slack", context={})
    assert d.allow is None


def test_allow(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    ta = _load()
    avp = MagicMock()
    avp.is_authorized.return_value = {"decision": "ALLOW"}
    with patch.object(ta, "_avp_client", return_value=avp):
        d = ta.is_authorized(
            principal="slack:T0ACME:U0ALICE",
            agent_id="workitems",
            source="slack",
            context={"workspace": "T0ACME", "channel_id": "C0ENG"},
        )
    assert d.allow is True


def test_deny_maps_reason_from_determining_policy(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    ta = _load()
    avp = MagicMock()
    avp.is_authorized.return_value = {
        "decision": "DENY",
        "determiningPolicies": [{"policyId": "pol-forbid-chan"}],
    }
    with patch.object(ta, "_avp_client", return_value=avp):
        d = ta.is_authorized(principal="slack:T:U", agent_id="workitems", source="slack", context={})
    assert d.allow is False
    assert d.reason == "policy:pol-forbid-chan"


def test_deny_default_reason_when_no_determining_policy(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    ta = _load()
    avp = MagicMock()
    avp.is_authorized.return_value = {"decision": "DENY"}
    with patch.object(ta, "_avp_client", return_value=avp):
        d = ta.is_authorized(principal="slack:T:U", agent_id="a", source="slack", context={})
    assert d.allow is False and d.reason == "no-matching-rule"


def test_avp_error_fails_closed(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    ta = _load()
    avp = MagicMock()
    avp.is_authorized.side_effect = RuntimeError("boom")
    with patch.object(ta, "_avp_client", return_value=avp):
        d = ta.is_authorized(principal="slack:T:U", agent_id="a", source="slack", context={})
    assert d.allow is False and d.reason == "authz-unavailable"


def test_call_shape_context_groups_and_email(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    ta = _load()
    avp = MagicMock()
    avp.is_authorized.return_value = {"decision": "ALLOW"}
    with patch.object(ta, "_avp_client", return_value=avp):
        ta.is_authorized(
            principal="slack:T0ACME:U0ALICE",
            agent_id="workitems",
            source="slack",
            context={
                "workspace": "T0ACME",
                "channel_id": "C0ENG",
                "requester_email": "alice@acme.com",
                "principal_groups": ["eng-oncall", "admins"],
            },
        )
    _, kw = avp.is_authorized.call_args
    assert kw["policyStoreId"] == "ps-1"
    assert kw["principal"] == {"entityType": "SdlcTrigger::User", "entityId": "slack:T0ACME:U0ALICE"}
    assert kw["action"] == {"actionType": "SdlcTrigger::Action", "actionId": "Trigger"}
    assert kw["resource"] == {"entityType": "SdlcTrigger::Agent", "entityId": "workitems"}
    cm = kw["context"]["contextMap"]
    assert cm["workspace"] == {"string": "T0ACME"}
    assert cm["channel"] == {"string": "C0ENG"}
    assert cm["source"] == {"string": "slack"}
    ent = kw["entities"]["entityList"][0]
    assert ent["attributes"] == {"email": {"string": "alice@acme.com"}}
    parents = {(p["entityType"], p["entityId"]) for p in ent["parents"]}
    assert parents == {("SdlcTrigger::Group", "eng-oncall"), ("SdlcTrigger::Group", "admins")}


def test_empty_context_defaults(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    ta = _load()
    avp = MagicMock()
    avp.is_authorized.return_value = {"decision": "ALLOW"}
    with patch.object(ta, "_avp_client", return_value=avp):
        ta.is_authorized(principal="github:octocat", agent_id="adr", source="github", context={})
    _, kw = avp.is_authorized.call_args
    cm = kw["context"]["contextMap"]
    assert cm["workspace"] == {"string": ""} and cm["channel"] == {"string": ""}
    ent = kw["entities"]["entityList"][0]
    assert ent["parents"] == [] and "attributes" not in ent
