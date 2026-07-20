"""Tests for the connector record kinds in config_store — Slack workspaces,
channel policy, and trigger-authz rules (docs/specs/slack-connectors-spec.md §4).

Store-level contract Phase 2's admin API + the router build on: id validation
(every id becomes a Cedar literal or an SSM path, so a bad one must be rejected
at the boundary), the paged listings, per-connector rule filtering, and the AVP
linkage stamp. Runs against moto, no AWS.
"""

import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REGION = "us-west-2"
TABLE = "fleet-config-test"
STAGE = "test"
os.environ["AWS_DEFAULT_REGION"] = REGION
os.environ["FLEET_CONFIG_TABLE"] = TABLE


def _make_table():
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
    )


def _load_store():
    sys.modules.pop("config_store", None)
    import config_store

    return config_store


# --- Slack workspaces --------------------------------------------------------


@mock_aws
def test_workspace_roundtrip_and_secret_paths():
    _make_table()
    cs = _load_store()
    rec = cs.put_slack_workspace(
        "T0ACME123", team_name="Acme", stage=STAGE, onboarded_by="admin-1"
    )
    assert rec["status"] == cs.SLACK_WS_PENDING
    assert rec["signing_secret_param"] == "/sdlc-agents/test/slack/T0ACME123/signing-secret"
    assert rec["bot_token_param"] == "/sdlc-agents/test/slack/T0ACME123/bot-token"
    # secret VALUES never live on the row
    assert "signing_secret" not in rec and "bot_token" not in rec
    assert cs.get_slack_workspace("T0ACME123")["team_name"] == "Acme"


@mock_aws
def test_workspace_rejects_bad_team_id():
    _make_table()
    cs = _load_store()
    for bad in ("", "acme", "t0acme123", "X0ACME123", "T0AC"):
        with pytest.raises(ValueError):
            cs.put_slack_workspace(bad, stage=STAGE)


@mock_aws
def test_workspace_rejects_bad_policy_and_status():
    _make_table()
    cs = _load_store()
    with pytest.raises(ValueError):
        cs.put_slack_workspace("T0ACME123", stage=STAGE, default_channel_policy="open")
    with pytest.raises(ValueError):
        cs.put_slack_workspace("T0ACME123", stage=STAGE, status="live")


@mock_aws
def test_workspace_status_transition_and_delete():
    _make_table()
    cs = _load_store()
    cs.put_slack_workspace("T0ACME123", stage=STAGE)
    cs.set_slack_workspace_status("T0ACME123", cs.SLACK_WS_ACTIVE)
    assert cs.get_slack_workspace("T0ACME123")["status"] == cs.SLACK_WS_ACTIVE
    assert cs.delete_slack_workspace("T0ACME123") is True
    assert cs.delete_slack_workspace("T0ACME123") is False  # idempotent no-op
    assert cs.get_slack_workspace("T0ACME123") is None


@mock_aws
def test_workspace_status_write_to_missing_row_fails():
    _make_table()
    cs = _load_store()
    with pytest.raises(Exception):  # ConditionalCheckFailed — no resurrection
        cs.set_slack_workspace_status("T0GHOST99", cs.SLACK_WS_ACTIVE)


@mock_aws
def test_list_workspaces_newest_first():
    _make_table()
    cs = _load_store()
    a = cs.put_slack_workspace("T0AAAAA1", stage=STAGE)
    a["onboarded_at"] = 100
    cs._get_table().put_item(Item=a)
    b = cs.put_slack_workspace("T0BBBBB2", stage=STAGE)
    b["onboarded_at"] = 200
    cs._get_table().put_item(Item=b)
    ids = [w["team_id"] for w in cs.list_slack_workspaces()]
    assert ids == ["T0BBBBB2", "T0AAAAA1"]


# --- Channel policy ----------------------------------------------------------


@mock_aws
def test_channel_policy_roundtrip_and_scoping():
    _make_table()
    cs = _load_store()
    cs.put_channel_policy("T0ACME123", "C0ENG111", mode="allow", channel_name="#eng")
    cs.put_channel_policy("T0ACME123", "C0REL222", mode="deny")
    # a different workspace's channel must not leak into this list
    cs.put_channel_policy("T0OTHER99", "C0XXX333", mode="allow")
    rows = cs.list_channels("T0ACME123")
    assert {r["channel_id"] for r in rows} == {"C0ENG111", "C0REL222"}
    assert cs.delete_channel_policy("T0ACME123", "C0ENG111") is True


@mock_aws
def test_channel_policy_rejects_bad_ids_and_mode():
    _make_table()
    cs = _load_store()
    with pytest.raises(ValueError):
        cs.put_channel_policy("bad", "C0ENG111", mode="allow")
    with pytest.raises(ValueError):
        cs.put_channel_policy("T0ACME123", "bad", mode="allow")
    with pytest.raises(ValueError):
        cs.put_channel_policy("T0ACME123", "C0ENG111", mode="maybe")


# --- Trigger rules -----------------------------------------------------------


@mock_aws
def test_trigger_rule_roundtrip_defaults():
    _make_table()
    cs = _load_store()
    rec = cs.put_trigger_rule(
        connector="slack",
        subject_type="user",
        subject_id="slack:T0ACME123:U0AL1CE",
        agent_id="workitems",
        workspace="T0ACME123",
        channels=["C0ENG111"],
        effect="permit",
        created_by="admin-1",
    )
    assert rec["rule_id"]
    assert cs.get_trigger_rule(rec["rule_id"])["agent_id"] == "workitems"


@mock_aws
def test_trigger_rule_wildcards_allowed():
    _make_table()
    cs = _load_store()
    rec = cs.put_trigger_rule(
        connector="slack",
        subject_type="group",
        subject_id="group:eng-oncall",
        agent_id="*",
        workspace="*",
        channels=["*"],
        effect="permit",
    )
    assert rec["agent_id"] == "*" and rec["workspace"] == "*" and rec["channels"] == ["*"]


@mock_aws
def test_trigger_rule_validation():
    _make_table()
    cs = _load_store()
    base = dict(subject_type="user", subject_id="u", effect="permit")
    with pytest.raises(ValueError):
        cs.put_trigger_rule(connector="teams", **base)  # unknown connector
    with pytest.raises(ValueError):
        cs.put_trigger_rule(connector="slack", subject_type="robot", subject_id="u", effect="permit")
    with pytest.raises(ValueError):
        cs.put_trigger_rule(connector="slack", subject_type="user", subject_id="u", effect="maybe")
    with pytest.raises(ValueError):
        cs.put_trigger_rule(connector="slack", subject_id="", **{k: v for k, v in base.items() if k != "subject_id"})
    with pytest.raises(ValueError):  # bad agent id
        cs.put_trigger_rule(connector="slack", agent_id="Bad Id", **base)
    with pytest.raises(ValueError):  # bad workspace
        cs.put_trigger_rule(connector="slack", workspace="nope", **base)
    with pytest.raises(ValueError):  # bad channel
        cs.put_trigger_rule(connector="slack", channels=["nope"], **base)


@mock_aws
def test_trigger_rule_per_connector_filtering():
    _make_table()
    cs = _load_store()
    cs.put_trigger_rule(connector="slack", subject_type="user", subject_id="u1", effect="permit")
    cs.put_trigger_rule(connector="asana", subject_type="user", subject_id="u2", effect="permit")
    assert len(cs.list_trigger_rules()) == 2
    slack = cs.list_trigger_rules(connector="slack")
    assert len(slack) == 1 and slack[0]["connector"] == "slack"


@mock_aws
def test_trigger_rule_policy_id_stamp_and_clear_and_preserve():
    _make_table()
    cs = _load_store()
    rec = cs.put_trigger_rule(connector="slack", subject_type="user", subject_id="u", effect="permit")
    rid = rec["rule_id"]
    cs.set_trigger_rule_policy_id(rid, "pol-123")
    assert cs.get_trigger_rule(rid)["avp_policy_id"] == "pol-123"
    # a REPLACE (same rule_id) must preserve the AVP linkage
    cs.put_trigger_rule(
        connector="slack", subject_type="user", subject_id="u", effect="forbid", rule_id=rid
    )
    assert cs.get_trigger_rule(rid)["avp_policy_id"] == "pol-123"
    cs.set_trigger_rule_policy_id(rid, None)
    assert "avp_policy_id" not in cs.get_trigger_rule(rid)
    assert cs.delete_trigger_rule(rid) is True
