"""Tests for the Dispatch Router's authorization seam.

Trigger authorization is decided ONLY by the Cedar SdlcTrigger policy store
(trigger_authz.is_authorized) — there is no per-capability allowlist. These tests
cover the router's thin wrapper: the fail-closed unresolved-sender guard applied
BEFORE any AVP call (threat T-4), and that the allow/deny decision + reason are
delegated to trigger_authz for every source.
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
    # A store id so the seam exercises the Cedar path (we patch is_authorized).
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    with patch("boto3.resource"), patch("boto3.client"):
        for name in ("router", "guardrail", "reply", "trigger_authz"):
            sys.modules.pop(name, None)
        import router as router_mod
    yield router_mod


_AGENT = {"agent_id": "workitems"}


@pytest.mark.parametrize("sender", ["", "unknown"])
def test_unresolved_sender_rejected_before_avp(router_module, sender):
    # An unresolved sender is rejected locally and never passed to AVP as a
    # principal — a misconfigured policy must not become a universal bypass (T-4).
    with patch.object(router_module.trigger_authz, "is_authorized") as spy:
        ok, reason = router_module.authorize_trigger(_AGENT, sender, "slack", {})
    assert ok is False and reason == "unresolved-sender"
    spy.assert_not_called()


def test_delegates_allow_to_cedar(router_module):
    from trigger_authz import Decision

    with patch.object(
        router_module.trigger_authz, "is_authorized", return_value=Decision(allow=True)
    ) as spy:
        ok, reason = router_module.authorize_trigger(
            _AGENT, "slack:T0ACME:U0ALICE", "slack", {"channel_id": "C0ENG"}
        )
    assert ok is True and reason == ""
    # the request dimensions are forwarded to the Cedar check
    _, kw = spy.call_args
    assert kw["principal"] == "slack:T0ACME:U0ALICE"
    assert kw["agent_id"] == "workitems"
    assert kw["source"] == "slack"
    assert kw["context"] == {"channel_id": "C0ENG"}


def test_delegates_deny_with_reason(router_module):
    from trigger_authz import Decision

    with patch.object(
        router_module.trigger_authz,
        "is_authorized",
        return_value=Decision(allow=False, reason="policy:pol-1"),
    ):
        ok, reason = router_module.authorize_trigger(
            _AGENT, "slack:T0ACME:U0MALLORY", "slack", {"channel_id": "C0SECRET"}
        )
    assert ok is False and reason == "policy:pol-1"


def test_check_authorization_bool_wrapper(router_module):
    from trigger_authz import Decision

    with patch.object(
        router_module.trigger_authz, "is_authorized", return_value=Decision(allow=True)
    ):
        assert router_module.check_authorization(_AGENT, "github:octocat", "github") is True
    with patch.object(
        router_module.trigger_authz,
        "is_authorized",
        return_value=Decision(allow=False, reason="no-matching-rule"),
    ):
        assert router_module.check_authorization(_AGENT, "github:octocat", "github") is False


def test_namespaced_principal(router_module):
    ns = router_module.namespaced_principal
    # github/asana bare ids get their source prefix
    assert ns("octocat", "github") == "github:octocat"
    assert ns("1202334567890", "asana") == "asana:1202334567890"
    # already-namespaced (slack, or a re-dispatch) is left as-is
    assert ns("slack:T0ACME:U1", "slack") == "slack:T0ACME:U1"
    assert ns("github:octocat", "github") == "github:octocat"
    # empty passes through (the unresolved-sender guard handles it upstream)
    assert ns("", "github") == ""


def test_authorize_passes_namespaced_principal_to_cedar(router_module):
    from trigger_authz import Decision

    with patch.object(
        router_module.trigger_authz, "is_authorized", return_value=Decision(allow=True)
    ) as spy:
        router_module.authorize_trigger(_AGENT, "octocat", "github", {})
    assert spy.call_args.kwargs["principal"] == "github:octocat"


def test_all_sources_use_one_cedar_path(router_module):
    # github / asana / slack all authorize through the same call — only the
    # namespaced principal + context differ.
    from trigger_authz import Decision

    for source, principal, ctx in [
        ("github", "github:octocat", {}),
        ("asana", "asana:1202334567890123", {}),
        ("slack", "slack:T0ACME:U0ALICE", {"workspace": "T0ACME", "channel_id": "C0ENG"}),
    ]:
        with patch.object(
            router_module.trigger_authz,
            "is_authorized",
            return_value=Decision(allow=True),
        ) as spy:
            ok, _ = router_module.authorize_trigger(_AGENT, principal, source, ctx)
        assert ok is True
        assert spy.call_args.kwargs["principal"] == principal
