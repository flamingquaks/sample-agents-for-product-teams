"""Tests for the admin API connector routes (admin.py): Slack workspaces,
channel policy, trigger rules, the access simulator, and the channel-onboarding
request approve/deny queue. Drives the real admin.handler + config_store on moto.
"""

import json
import os
import sys
from pathlib import Path

import boto3
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REGION = "us-west-2"
TABLE = "fleet-config-test"
os.environ["AWS_DEFAULT_REGION"] = REGION
os.environ["FLEET_CONFIG_TABLE"] = TABLE
os.environ.setdefault("STAGE", "test")

ADMIN = {"sub": "admin-1", "cognito:groups": "[admins]"}
OPERATOR = {"sub": "op-1", "cognito:groups": "[operators]"}
TEAM = "T0ACME12"


def _event(method, resource, claims=ADMIN, path=None, body=None, query=None):
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path,
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": claims}},
    }


def _make_table():
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
    )


def _load_admin():
    for m in ("admin", "config_store", "auth", "http_responses"):
        sys.modules.pop(m, None)
    import admin

    return admin


def _body(resp):
    return json.loads(resp["body"])


# --- workspaces --------------------------------------------------------------


@mock_aws
def test_workspace_onboard_list_delete():
    _make_table()
    admin = _load_admin()
    resp = admin.handler(_event("POST", "/admin/slack/workspaces",
                                body={"team_id": TEAM, "team_name": "Acme"}))
    assert resp["statusCode"] == 200, resp["body"]
    assert _body(resp)["status"] == "active"

    resp = admin.handler(_event("GET", "/admin/slack/workspaces"))
    assert len(_body(resp)["workspaces"]) == 1

    resp = admin.handler(_event("DELETE", "/admin/slack/workspaces/{team_id}",
                                path={"team_id": TEAM}))
    assert _body(resp)["deleted"] is True


@mock_aws
def test_workspace_bad_team_id_rejected():
    _make_table()
    admin = _load_admin()
    resp = admin.handler(_event("POST", "/admin/slack/workspaces", body={"team_id": "nope"}))
    assert resp["statusCode"] == 400


@mock_aws
def test_operator_cannot_write_workspace():
    _make_table()
    admin = _load_admin()
    resp = admin.handler(_event("POST", "/admin/slack/workspaces",
                                claims=OPERATOR, body={"team_id": TEAM}))
    assert resp["statusCode"] == 403


# --- channel policy ----------------------------------------------------------


@mock_aws
def test_channel_policy_crud():
    _make_table()
    admin = _load_admin()
    resp = admin.handler(_event("POST", "/admin/slack/channels",
                                body={"team_id": TEAM, "channel_id": "C0ENG111", "mode": "allow"}))
    assert resp["statusCode"] == 200, resp["body"]
    resp = admin.handler(_event("GET", "/admin/slack/channels", query={"team_id": TEAM}))
    assert len(_body(resp)["channels"]) == 1
    resp = admin.handler(_event("DELETE", "/admin/slack/channels/{team_id}/{channel_id}",
                                path={"team_id": TEAM, "channel_id": "C0ENG111"}))
    assert _body(resp)["deleted"] is True


# --- trigger rules -----------------------------------------------------------


@mock_aws
def test_trigger_rule_create_list_delete():
    _make_table()
    admin = _load_admin()
    resp = admin.handler(_event("POST", "/admin/trigger-rules",
                                body={"connector": "slack", "subject_type": "user",
                                      "subject_id": "slack:T0ACME12:U0ALICE",
                                      "agent_id": "workitems", "workspace": TEAM,
                                      "effect": "permit"}))
    assert resp["statusCode"] == 200, resp["body"]
    rid = _body(resp)["rule_id"]
    resp = admin.handler(_event("GET", "/admin/trigger-rules", query={"connector": "slack"}))
    assert len(_body(resp)["rules"]) == 1
    resp = admin.handler(_event("DELETE", "/admin/trigger-rules/{rule_id}", path={"rule_id": rid}))
    assert _body(resp)["deleted"] is True


@mock_aws
def test_trigger_rule_bad_effect_rejected():
    _make_table()
    admin = _load_admin()
    resp = admin.handler(_event("POST", "/admin/trigger-rules",
                                body={"connector": "slack", "subject_type": "user",
                                      "subject_id": "u", "effect": "maybe"}))
    assert resp["statusCode"] == 400


# --- access simulator --------------------------------------------------------


@mock_aws
def test_simulate_allow_deny_and_forbid():
    _make_table()
    admin = _load_admin()
    # workspace (allowlist) + an allowed channel + a permit for alice on workitems
    admin.handler(_event("POST", "/admin/slack/workspaces",
                         body={"team_id": TEAM, "default_channel_policy": "allowlist"}))
    admin.handler(_event("POST", "/admin/slack/channels",
                         body={"team_id": TEAM, "channel_id": "C0ENG111", "mode": "allow"}))
    admin.handler(_event("POST", "/admin/trigger-rules",
                         body={"connector": "slack", "subject_type": "user",
                               "subject_id": "slack:T0ACME12:U0ALICE",
                               "agent_id": "workitems", "workspace": TEAM, "effect": "permit"}))

    def sim(**kw):
        return _body(admin.handler(_event("POST", "/admin/trigger-rules/simulate", body=kw)))

    # allowed: granted principal, allowed channel
    r = sim(principal="slack:T0ACME12:U0ALICE", agent_id="workitems",
            workspace=TEAM, channel_id="C0ENG111")
    assert r["decision"] == "ALLOW"
    # denied: not granted
    r = sim(principal="slack:T0ACME12:U0BOB", agent_id="workitems",
            workspace=TEAM, channel_id="C0ENG111")
    assert r["decision"] == "DENY" and r["reason"] == "no-matching-grant"
    # denied: granted principal but channel not allowed (allowlist, no row)
    r = sim(principal="slack:T0ACME12:U0ALICE", agent_id="workitems",
            workspace=TEAM, channel_id="C0RANDOM")
    assert r["decision"] == "DENY" and r["reason"] == "channel-not-allowed"


@mock_aws
def test_simulate_forbid_wins():
    _make_table()
    admin = _load_admin()
    admin.handler(_event("POST", "/admin/slack/workspaces",
                         body={"team_id": TEAM, "default_channel_policy": "denylist"}))
    admin.handler(_event("POST", "/admin/trigger-rules",
                         body={"connector": "slack", "subject_type": "user",
                               "subject_id": "slack:T0ACME12:U0ALICE", "agent_id": "workitems",
                               "workspace": TEAM, "effect": "permit"}))
    admin.handler(_event("POST", "/admin/trigger-rules",
                         body={"connector": "slack", "subject_type": "user",
                               "subject_id": "slack:T0ACME12:U0ALICE", "agent_id": "workitems",
                               "workspace": TEAM, "effect": "forbid"}))
    r = _body(admin.handler(_event("POST", "/admin/trigger-rules/simulate",
                                   body={"principal": "slack:T0ACME12:U0ALICE",
                                         "agent_id": "workitems", "workspace": TEAM,
                                         "channel_id": "C0ENG111"})))
    assert r["decision"] == "DENY" and r["reason"] == "explicitly-denied"


# --- channel onboarding requests --------------------------------------------


@mock_aws
def test_channel_request_approve_creates_allow_and_rules():
    _make_table()
    admin = _load_admin()
    import config_store

    admin.handler(_event("POST", "/admin/slack/workspaces", body={"team_id": TEAM}))
    req = config_store.put_channel_request(
        team_id=TEAM, channel_id="C0ENG111", channel_name="#eng",
        requested_by="slack:T0ACME12:U0ALICE", requested_agents=["workitems"],
    )
    rid = req["request_id"]

    resp = admin.handler(_event("POST", "/admin/channel-requests/{request_id}/approve",
                                path={"request_id": rid}))
    assert resp["statusCode"] == 200, resp["body"]
    b = _body(resp)
    assert b["request"]["status"] == "approved"
    assert b["channel_allowed"] is True
    assert len(b["created_rules"]) == 1
    # the channel is now allowed, and a permit rule for the channel-group exists
    chans = config_store.list_channels(TEAM)
    assert chans and chans[0]["mode"] == "allow"
    rules = config_store.list_trigger_rules("slack")
    assert rules[0]["subject_id"] == f"channel:{TEAM}:C0ENG111"
    assert rules[0]["agent_id"] == "workitems"


@mock_aws
def test_channel_request_deny_records_decision_no_grants():
    _make_table()
    admin = _load_admin()
    import config_store

    req = config_store.put_channel_request(
        team_id=TEAM, channel_id="C0ENG111", requested_by="slack:T0ACME12:U0ALICE",
    )
    resp = admin.handler(_event("POST", "/admin/channel-requests/{request_id}/deny",
                                path={"request_id": req["request_id"]}))
    assert resp["statusCode"] == 200
    assert _body(resp)["request"]["status"] == "denied"
    assert config_store.list_channels(TEAM) == []
    assert config_store.list_trigger_rules("slack") == []


@mock_aws
def test_channel_request_list_and_status_filter():
    _make_table()
    admin = _load_admin()
    import config_store

    config_store.put_channel_request(team_id=TEAM, channel_id="C0AAA111", requested_by="u1")
    config_store.put_channel_request(team_id=TEAM, channel_id="C0BBB222", requested_by="u2")
    resp = admin.handler(_event("GET", "/admin/channel-requests", query={"status": "pending"}))
    assert len(_body(resp)["requests"]) == 2


@mock_aws
def test_channel_request_approve_missing_404():
    _make_table()
    admin = _load_admin()
    resp = admin.handler(_event("POST", "/admin/channel-requests/{request_id}/approve",
                                path={"request_id": "nope"}))
    assert resp["statusCode"] == 404


@mock_aws
def test_operator_cannot_approve_request():
    _make_table()
    admin = _load_admin()
    resp = admin.handler(_event("POST", "/admin/channel-requests/{request_id}/approve",
                                claims=OPERATOR, path={"request_id": "x"}))
    assert resp["statusCode"] == 403
