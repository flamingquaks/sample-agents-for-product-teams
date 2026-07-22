"""Tests for trigger_authz — the router's data-driven Cedar check via AVP
(spec §5.3). The fleet's ONLY trigger-authz mechanism.

The policy set is FIXED; grants are DATA read from trigger_grants and passed to
AVP as entity attributes. These tests stub trigger_grants + the AVP client and
assert: fail-closed cases (store unset, AVP/grant errors, non-ALLOW), and that
the IsAuthorized call is shaped correctly — the Agent resource carries the grant
sets, the principal carries its groups + email, and context carries
workspace/channel/source/channelAllowed.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load():
    for m in ("trigger_authz", "trigger_grants"):
        sys.modules.pop(m, None)
    import trigger_authz

    return trigger_authz


def _grants(ta, *, ap=(), dp=(), ag=(), dg=(), channel_allowed=True):
    """Stub trigger_grants on the loaded module."""
    from trigger_grants import AgentGrants

    ta.trigger_grants.agent_grants = MagicMock(
        return_value=AgentGrants(
            allowed_principals=list(ap),
            denied_principals=list(dp),
            allowed_groups=list(ag),
            denied_groups=list(dg),
        )
    )
    ta.trigger_grants.channel_allowed = MagicMock(return_value=channel_allowed)


def test_store_unset_fails_closed(monkeypatch):
    monkeypatch.delenv("TRIGGER_POLICY_STORE_ID", raising=False)
    ta = _load()
    avp = MagicMock()
    with patch.object(ta, "_avp_client", return_value=avp):
        d = ta.is_authorized(principal="slack:T:U", agent_id="workitems", source="slack", context={})
    assert d.allow is False and d.reason == "authz-store-unconfigured"
    avp.is_authorized.assert_not_called()


def test_allow(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    ta = _load()
    _grants(ta, ap=["slack:T0ACME:U0ALICE"])
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
    _grants(ta, dp=["slack:T:U"])
    avp = MagicMock()
    avp.is_authorized.return_value = {
        "decision": "DENY",
        "determiningPolicies": [{"policyId": "sdlc_trigger_forbid_denied"}],
    }
    with patch.object(ta, "_avp_client", return_value=avp):
        d = ta.is_authorized(principal="slack:T:U", agent_id="workitems", source="slack", context={})
    assert d.allow is False and d.reason == "policy:sdlc_trigger_forbid_denied"


def test_default_deny_no_grant(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    ta = _load()
    _grants(ta)  # no grants at all
    avp = MagicMock()
    avp.is_authorized.return_value = {"decision": "DENY"}
    with patch.object(ta, "_avp_client", return_value=avp):
        d = ta.is_authorized(principal="slack:T:U", agent_id="a", source="slack", context={})
    assert d.allow is False and d.reason == "no-matching-grant"


def test_avp_error_fails_closed(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    ta = _load()
    _grants(ta, ap=["slack:T:U"])
    avp = MagicMock()
    avp.is_authorized.side_effect = RuntimeError("boom")
    with patch.object(ta, "_avp_client", return_value=avp):
        d = ta.is_authorized(principal="slack:T:U", agent_id="a", source="slack", context={})
    assert d.allow is False and d.reason == "authz-unavailable"


def test_grant_lookup_error_fails_closed(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    ta = _load()
    ta.trigger_grants.agent_grants = MagicMock(side_effect=RuntimeError("ddb down"))
    ta.trigger_grants.channel_allowed = MagicMock(return_value=True)
    avp = MagicMock()
    with patch.object(ta, "_avp_client", return_value=avp):
        d = ta.is_authorized(principal="slack:T:U", agent_id="a", source="slack", context={})
    assert d.allow is False and d.reason == "authz-unavailable"
    avp.is_authorized.assert_not_called()  # never reached AVP


def test_call_shape_entities_and_context(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    ta = _load()
    _grants(
        ta,
        ap=["slack:T0ACME:U0ALICE"],
        dp=["slack:T0ACME:U0MALLORY"],
        ag=["eng"],
        dg=["interns"],
        channel_allowed=True,
    )
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
                "principal_groups": ["eng", "admins"],
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
    assert cm["channelAllowed"] == {"boolean": True}

    ents = {e["identifier"]["entityType"]: e for e in kw["entities"]["entityList"]}
    principal_e = ents["SdlcTrigger::User"]
    agent_e = ents["SdlcTrigger::Agent"]
    # principal carries its id (String, for the grant-set match), groups (as a
    # set attr + as parents), and email
    assert principal_e["attributes"]["principalId"] == {"string": "slack:T0ACME:U0ALICE"}
    assert principal_e["attributes"]["groups"] == {"set": [{"string": "admins"}, {"string": "eng"}]}
    assert principal_e["attributes"]["email"] == {"string": "alice@acme.com"}
    parents = {(p["entityType"], p["entityId"]) for p in principal_e["parents"]}
    assert parents == {("SdlcTrigger::Group", "eng"), ("SdlcTrigger::Group", "admins")}
    # agent resource carries the grant sets
    assert agent_e["attributes"]["allowedPrincipals"] == {"set": [{"string": "slack:T0ACME:U0ALICE"}]}
    assert agent_e["attributes"]["deniedPrincipals"] == {"set": [{"string": "slack:T0ACME:U0MALLORY"}]}
    assert agent_e["attributes"]["allowedGroups"] == {"set": [{"string": "eng"}]}
    assert agent_e["attributes"]["deniedGroups"] == {"set": [{"string": "interns"}]}


def test_channel_blocked_flows_to_context(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    ta = _load()
    _grants(ta, ap=["slack:T:U"], channel_allowed=False)
    avp = MagicMock()
    avp.is_authorized.return_value = {"decision": "DENY", "determiningPolicies": [{"policyId": "sdlc_trigger_forbid_channel"}]}
    with patch.object(ta, "_avp_client", return_value=avp):
        d = ta.is_authorized(
            principal="slack:T:U", agent_id="a", source="slack",
            context={"workspace": "T0ACME", "channel_id": "C0SECRET"},
        )
    _, kw = avp.is_authorized.call_args
    assert kw["context"]["contextMap"]["channelAllowed"] == {"boolean": False}
    assert d.allow is False


def test_empty_context_defaults(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    ta = _load()
    _grants(ta, ap=["github:octocat"])
    avp = MagicMock()
    avp.is_authorized.return_value = {"decision": "ALLOW"}
    with patch.object(ta, "_avp_client", return_value=avp):
        ta.is_authorized(principal="github:octocat", agent_id="adr", source="github", context={})
    _, kw = avp.is_authorized.call_args
    cm = kw["context"]["contextMap"]
    assert cm["workspace"] == {"string": ""} and cm["channel"] == {"string": ""}
    ents = {e["identifier"]["entityType"]: e for e in kw["entities"]["entityList"]}
    principal_e = ents["SdlcTrigger::User"]
    assert principal_e["parents"] == []
    assert principal_e["attributes"]["groups"] == {"set": []}
    assert "email" not in principal_e["attributes"]
