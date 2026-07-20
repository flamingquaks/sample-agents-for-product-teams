"""Tests for trigger_grants — the dispatch-side reader that resolves the
trigger-authz DATA (trigger_rule + slack_channel/workspace rows) into the Cedar
entity attributes trigger_authz passes to AVP (spec §5).

Runs against moto. Exercises the WHO axis (agent_grants: permit/forbid, user vs
group, agent/workspace wildcard matching) and the WHERE axis (channel_allowed:
allowlist vs denylist posture, unknown workspace, non-Slack).
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
os.environ["AWS_DEFAULT_REGION"] = REGION
os.environ["FLEET_CONFIG_TABLE"] = TABLE


def _make_table():
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
    )


def _load():
    sys.modules.pop("trigger_grants", None)
    import trigger_grants as tg

    tg.reset_cache()
    return tg


def _put(tg, item):
    tg._get_table().put_item(Item=item)


def _rule(**over):
    base = {
        "kind": "trigger_rule",
        "connector": "slack",
        "subject_type": "user",
        "subject_id": "slack:T0ACME:U0ALICE",
        "agent_id": "workitems",
        "workspace": "T0ACME",
        "effect": "permit",
    }
    base.update(over)
    rid = base.get("rule_id", base["subject_id"] + base["agent_id"] + base["effect"])
    base["pk"] = f"trigger_rule#{rid}"
    return base


# --- WHO axis: agent_grants --------------------------------------------------


@mock_aws
def test_agent_grants_partitions_by_effect_and_subject_type():
    _make_table()
    tg = _load()
    _put(tg, _rule(subject_id="slack:T0ACME:U0ALICE", effect="permit", rule_id="1"))
    _put(tg, _rule(subject_id="slack:T0ACME:U0MALLORY", effect="forbid", rule_id="2"))
    _put(tg, _rule(subject_type="group", subject_id="eng", effect="permit", rule_id="3"))
    _put(tg, _rule(subject_type="group", subject_id="interns", effect="forbid", rule_id="4"))
    g = tg.agent_grants("workitems", "T0ACME")
    assert g.allowed_principals == ["slack:T0ACME:U0ALICE"]
    assert g.denied_principals == ["slack:T0ACME:U0MALLORY"]
    assert g.allowed_groups == ["eng"]
    assert g.denied_groups == ["interns"]


@mock_aws
def test_agent_grants_agent_wildcard_matches_any_agent():
    _make_table()
    tg = _load()
    _put(tg, _rule(subject_id="slack:T0ACME:U0ALICE", agent_id="*", rule_id="1"))
    assert tg.agent_grants("workitems", "T0ACME").allowed_principals == ["slack:T0ACME:U0ALICE"]
    assert tg.agent_grants("docwriter", "T0ACME").allowed_principals == ["slack:T0ACME:U0ALICE"]


@mock_aws
def test_agent_grants_concrete_agent_does_not_match_other_agent():
    _make_table()
    tg = _load()
    _put(tg, _rule(subject_id="slack:T0ACME:U0ALICE", agent_id="workitems", rule_id="1"))
    assert tg.agent_grants("docwriter", "T0ACME").allowed_principals == []


@mock_aws
def test_agent_grants_workspace_wildcard_and_empty_request_ws():
    _make_table()
    tg = _load()
    # A "*" workspace rule (e.g. a github/asana grant) matches any request ws,
    # including the empty ws github/asana carry.
    _put(tg, _rule(subject_id="github:octocat", workspace="*", rule_id="1"))
    assert tg.agent_grants("workitems", "").allowed_principals == ["github:octocat"]
    assert tg.agent_grants("workitems", "T0ACME").allowed_principals == ["github:octocat"]


@mock_aws
def test_agent_grants_concrete_workspace_scopes_to_that_workspace():
    _make_table()
    tg = _load()
    _put(tg, _rule(subject_id="slack:T0ACME:U0ALICE", workspace="T0ACME", rule_id="1"))
    assert tg.agent_grants("workitems", "T0OTHER").allowed_principals == []
    assert tg.agent_grants("workitems", "").allowed_principals == []


# --- WHERE axis: channel_allowed --------------------------------------------


@mock_aws
def test_channel_non_slack_always_allowed():
    _make_table()
    tg = _load()
    assert tg.channel_allowed("", "") is True  # github/asana carry no workspace


@mock_aws
def test_channel_unknown_workspace_fails_closed():
    _make_table()
    tg = _load()
    assert tg.channel_allowed("T0GHOST", "C0X") is False


@mock_aws
def test_channel_allowlist_posture():
    _make_table()
    tg = _load()
    _put(tg, {"pk": "slack_ws#T0ACME", "kind": "slack_workspace", "team_id": "T0ACME",
              "default_channel_policy": "allowlist"})
    _put(tg, {"pk": "slack_chan#T0ACME#C0ENG", "kind": "slack_channel", "team_id": "T0ACME",
              "channel_id": "C0ENG", "mode": "allow"})
    tg.reset_cache()
    assert tg.channel_allowed("T0ACME", "C0ENG") is True   # explicit allow
    assert tg.channel_allowed("T0ACME", "C0RANDOM") is False  # default-deny


@mock_aws
def test_channel_denylist_posture():
    _make_table()
    tg = _load()
    _put(tg, {"pk": "slack_ws#T0ACME", "kind": "slack_workspace", "team_id": "T0ACME",
              "default_channel_policy": "denylist"})
    _put(tg, {"pk": "slack_chan#T0ACME#C0SECRET", "kind": "slack_channel", "team_id": "T0ACME",
              "channel_id": "C0SECRET", "mode": "deny"})
    tg.reset_cache()
    assert tg.channel_allowed("T0ACME", "C0SECRET") is False  # explicit deny
    assert tg.channel_allowed("T0ACME", "C0RANDOM") is True    # default-allow


@mock_aws
def test_cache_ttl_refresh(monkeypatch):
    _make_table()
    tg = _load()
    assert tg.agent_grants("workitems", "T0ACME").allowed_principals == []
    _put(tg, _rule(rule_id="1"))
    # still cached (TTL not elapsed) → stale empty
    assert tg.agent_grants("workitems", "T0ACME").allowed_principals == []
    tg.reset_cache()
    assert tg.agent_grants("workitems", "T0ACME").allowed_principals == ["slack:T0ACME:U0ALICE"]
