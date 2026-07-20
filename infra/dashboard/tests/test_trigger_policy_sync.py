"""Tests for trigger_policy_sync — the pure Cedar renderer and the AVP
projection/revocation primitives (docs/specs/slack-connectors-spec.md §5.5).

The renderer tests are pure (no AWS). The project/revoke tests mock the
verifiedpermissions client to assert the store-unset no-op, the create+stamp
path, the replace-deletes-old path, and that a failure raises so the admin caller
can honor the rollback invariant.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")


def _load():
    for m in ("trigger_policy_sync", "config_store"):
        sys.modules.pop(m, None)
    import trigger_policy_sync as tps

    return tps


def _rule(**over):
    base = dict(
        rule_id="r1",
        connector="slack",
        subject_type="user",
        subject_id="slack:T0ACME123:U0AL1CE",
        agent_id="workitems",
        workspace="T0ACME123",
        channels=["C0ENG111", "C0REL222"],
        effect="permit",
    )
    base.update(over)
    return base


# --- renderer ----------------------------------------------------------------


def test_render_user_permit_with_workspace_and_channels():
    tps = _load()
    stmt = tps.render_rule_statement(_rule())
    assert stmt.startswith("permit(")
    assert 'principal == SdlcTrigger::User::"slack:T0ACME123:U0AL1CE"' in stmt
    assert 'action == SdlcTrigger::Action::"Trigger"' in stmt
    assert 'resource == SdlcTrigger::Agent::"workitems"' in stmt
    assert 'context.workspace == "T0ACME123"' in stmt
    assert '["C0ENG111", "C0REL222"].contains(context.channel)' in stmt
    assert stmt.rstrip().endswith("};")


def test_render_group_uses_in():
    tps = _load()
    stmt = tps.render_rule_statement(_rule(subject_type="group", subject_id="eng-oncall"))
    assert 'principal in SdlcTrigger::Group::"eng-oncall"' in stmt


def test_render_agent_wildcard_leaves_resource_unconstrained():
    tps = _load()
    stmt = tps.render_rule_statement(_rule(agent_id="*"))
    assert "resource == SdlcTrigger::Agent" not in stmt
    # bare `resource` scope element
    assert "\n  resource\n)" in stmt


def test_render_wildcards_omit_conditions():
    tps = _load()
    stmt = tps.render_rule_statement(_rule(workspace="*", channels=["*"]))
    assert "when {" not in stmt  # no conditions → no when-clause
    assert stmt.rstrip().endswith(");")


def test_render_forbid_effect():
    tps = _load()
    stmt = tps.render_rule_statement(_rule(effect="forbid"))
    assert stmt.startswith("forbid(")


def test_render_escapes_quotes_in_subject():
    tps = _load()
    stmt = tps.render_rule_statement(_rule(subject_id='ev"il'))
    assert r'\"' in stmt  # the embedded quote is escaped, can't break the literal


def test_render_rejects_bad_effect():
    tps = _load()
    with pytest.raises(tps.TriggerPolicySyncError):
        tps.render_rule_statement(_rule(effect="maybe"))


# --- projection / revocation -------------------------------------------------


def test_project_noop_when_store_unset(monkeypatch):
    monkeypatch.delenv("TRIGGER_POLICY_STORE_ID", raising=False)
    tps = _load()
    assert tps.project_rule(_rule()) is None


def test_project_creates_and_stamps(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    tps = _load()
    avp = MagicMock()
    avp.create_policy.return_value = {"policyId": "pol-new"}
    with patch.object(tps, "_get_client", return_value=avp), patch.object(
        tps.config_store, "set_trigger_rule_policy_id"
    ) as stamp:
        pid = tps.project_rule(_rule())
    assert pid == "pol-new"
    stamp.assert_called_once_with("r1", "pol-new")
    # the rendered statement was passed to AVP as a static policy
    _, kwargs = avp.create_policy.call_args
    assert kwargs["policyStoreId"] == "ps-1"
    assert "permit(" in kwargs["definition"]["static"]["statement"]


def test_project_replace_deletes_old_policy(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    tps = _load()
    avp = MagicMock()
    avp.create_policy.return_value = {"policyId": "pol-new"}
    with patch.object(tps, "_get_client", return_value=avp), patch.object(
        tps.config_store, "set_trigger_rule_policy_id"
    ):
        tps.project_rule(_rule(avp_policy_id="pol-old"))
    avp.delete_policy.assert_called_once_with(policyStoreId="ps-1", policyId="pol-old")


def test_project_raises_on_avp_error(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    tps = _load()
    from botocore.exceptions import ClientError

    avp = MagicMock()
    avp.create_policy.side_effect = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "bad cedar"}}, "CreatePolicy"
    )
    with patch.object(tps, "_get_client", return_value=avp), patch.object(
        tps.config_store, "set_trigger_rule_policy_id"
    ) as stamp:
        with pytest.raises(tps.TriggerPolicySyncError):
            tps.project_rule(_rule())
    stamp.assert_not_called()  # nothing stamped → admin rolls the row back


def test_revoke_deletes_and_clears(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    tps = _load()
    avp = MagicMock()
    with patch.object(tps, "_get_client", return_value=avp), patch.object(
        tps.config_store, "set_trigger_rule_policy_id"
    ) as stamp:
        tps.revoke_rule(_rule(avp_policy_id="pol-x"))
    avp.delete_policy.assert_called_once_with(policyStoreId="ps-1", policyId="pol-x")
    stamp.assert_called_once_with("r1", None)


def test_revoke_noop_when_never_projected(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    tps = _load()
    avp = MagicMock()
    with patch.object(tps, "_get_client", return_value=avp):
        tps.revoke_rule(_rule())  # no avp_policy_id
    avp.delete_policy.assert_not_called()


def test_revoke_raises_on_avp_error(monkeypatch):
    monkeypatch.setenv("TRIGGER_POLICY_STORE_ID", "ps-1")
    tps = _load()
    from botocore.exceptions import ClientError

    avp = MagicMock()
    # a real client exposes .exceptions.ResourceNotFoundException; make ours a
    # distinct type so the generic ClientError path is what triggers.
    avp.exceptions.ResourceNotFoundException = type("RNF", (Exception,), {})
    avp.delete_policy.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "DeletePolicy"
    )
    with patch.object(tps, "_get_client", return_value=avp):
        with pytest.raises(tps.TriggerPolicySyncError):
            tps.revoke_rule(_rule(avp_policy_id="pol-x"))
