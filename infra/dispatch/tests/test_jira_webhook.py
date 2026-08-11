"""Tests for the Jira webhook receiver (jira_webhook.py) — FIT verification,
cloud-id cross-check, dedup, bot-loop guard, ADF mention resolution, and the
automation fallback. FIT verify + REST are patched; DynamoDB via moto."""

import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REGION = "us-west-2"
TABLE = "fleet-config-test"
ASSIGN = "dispatch-assignments-test"
os.environ["AWS_DEFAULT_REGION"] = REGION
os.environ["FLEET_CONFIG_TABLE"] = TABLE
os.environ["ASSIGNMENTS_TABLE"] = ASSIGN

SITE = "55555555-5555-5555-5555-555555555555"
BOT = "712020:bot"


def _make_tables():
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName=TABLE, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                              {"AttributeName": "kind", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[{
            "IndexName": "kind-index",
            "KeySchema": [{"AttributeName": "kind", "KeyType": "HASH"},
                          {"AttributeName": "pk", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"}}],
    )
    ddb.create_table(
        TableName=ASSIGN, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "assignment_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "assignment_id", "KeyType": "HASH"}],
    )
    boto3.resource("dynamodb", region_name=REGION).Table(TABLE).put_item(Item={
        "pk": f"atlassian_site#{SITE}", "kind": "atlassian_site", "site_id": SITE,
        "site_url": "https://acme.atlassian.net", "enabled": True, "status": "active",
        "products": {"jira": True, "confluence": False}, "bot_account_id": BOT,
        "bot_email": "sdlc@acme.com", "forge_app_id": "ari:app/1",
        "api_token_param": "/p", "default_project_policy": "allowlist",
    })
    boto3.resource("dynamodb", region_name=REGION).Table(TABLE).put_item(Item={
        "pk": f"jira_proj#{SITE}#ENG", "kind": "jira_project", "site_id": SITE,
        "project_key": "ENG", "mode": "allow", "repos": [],
    })


def _load(monkeypatch, *, dispatched, verify_ok=True, claims=None):
    for m in ("config_query", "trigger_grants", "automation", "atlassian_client",
              "mentions", "jira_webhook"):
        sys.modules.pop(m, None)
    import mentions
    import atlassian_client as ac
    import jira_webhook as jw
    jw.trigger_grants.reset_cache()

    def fake_verify(token, *, expected_app_id, expected_audience=None, now=None):
        if not verify_ok:
            raise mentions.ForgeTokenError("bad")
        return claims or {"cloudId": SITE, "app": {"id": expected_app_id}}
    monkeypatch.setattr(mentions, "verify_forge_invocation_token", fake_verify)
    monkeypatch.setattr(ac, "fetch_token", lambda s: "tok")
    monkeypatch.setattr(ac, "rest", lambda *a, **k: (200, {"fields": {}, "comments": []}))
    # Capture dispatches instead of invoking Lambda.
    monkeypatch.setattr(jw, "_lambda", _FakeLambda(dispatched))
    # Registry resolves @workitems.
    monkeypatch.setattr(jw._registry, "resolve_mention",
                        lambda text: ("workitems", "do it") if "workitems" in text else None)
    return jw


class _FakeLambda:
    def __init__(self, sink):
        self.sink = sink
    def invoke(self, **kw):
        self.sink.append(json.loads(kw["Payload"]))


def _event(body: dict, site=SITE):
    return {"pathParameters": {"site": site},
            "headers": {"x-forge-invocation-token": "fit"},
            "body": json.dumps(body)}


@mock_aws
def test_bad_token_401(monkeypatch):
    _make_tables()
    jw = _load(monkeypatch, dispatched=[], verify_ok=False)
    resp = jw.handler(_event({"webhookEvent": "comment_created"}))
    assert resp["statusCode"] == 401


@mock_aws
def test_cloud_id_mismatch_dropped(monkeypatch):
    _make_tables()
    jw = _load(monkeypatch, dispatched=[], claims={"cloudId": "other-site"})
    resp = jw.handler(_event({"webhookEvent": "comment_created"}))
    assert resp["statusCode"] == 200 and resp["body"] == "ignored"


@mock_aws
def test_unonboarded_site_dropped(monkeypatch):
    _make_tables()
    jw = _load(monkeypatch, dispatched=[])
    resp = jw.handler(_event({"webhookEvent": "comment_created"},
                             site="00000000-0000-0000-0000-000000000000"))
    assert resp["body"] == "ignored"


@mock_aws
def test_mention_dispatches(monkeypatch):
    _make_tables()
    dispatched = []
    jw = _load(monkeypatch, dispatched=dispatched)
    body = {"webhookEvent": "comment_created",
            "issue": {"id": "1", "key": "ENG-5"},
            "comment": {"id": "c1", "author": {"accountId": "user1"},
                        "body": {"type": "doc", "content": [
                            {"type": "paragraph", "content": [
                                {"type": "text", "text": "@workitems do it"}]}]}},
            "timestamp": "123"}
    resp = jw.handler(_event(body))
    assert resp["statusCode"] == 200
    assert len(dispatched) == 1
    d = dispatched[0]
    assert d["source"] == "jira" and d["agent_id"] == "workitems"
    assert d["sender"] == "atlassian:user1"
    assert d["context"]["issue_key"] == "ENG-5"


@mock_aws
def test_bot_authored_no_dispatch(monkeypatch):
    _make_tables()
    dispatched = []
    jw = _load(monkeypatch, dispatched=dispatched)
    body = {"webhookEvent": "comment_created",
            "issue": {"id": "1", "key": "ENG-5"},
            "comment": {"id": "c1", "author": {"accountId": BOT},
                        "body": {"type": "doc", "content": []}},
            "timestamp": "1"}
    jw.handler(_event(body))
    assert dispatched == []


@mock_aws
def test_first_delivery_pins_forge_app_id(monkeypatch):
    # A site with no pinned forge_app_id gets one recorded from the first
    # verified delivery's claims (§A5/T-54), so later deliveries are app-id
    # pinned. The connect flow doesn't set it, so this is the pinning path.
    _make_tables()
    cfg = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    cfg.update_item(
        Key={"pk": f"atlassian_site#{SITE}"},
        UpdateExpression="SET forge_app_id = :e", ExpressionAttributeValues={":e": ""},
    )
    dispatched = []
    jw = _load(monkeypatch, dispatched=dispatched,
               claims={"cloudId": SITE, "app": {"id": "ari:app/REAL"}})
    body = {"webhookEvent": "comment_created",
            "issue": {"id": "1", "key": "ENG-5"},
            "comment": {"id": "c1", "author": {"accountId": "user1"},
                        "body": {"type": "doc", "content": []}},
            "timestamp": "9"}
    jw.handler(_event(body))
    row = cfg.get_item(Key={"pk": f"atlassian_site#{SITE}"})["Item"]
    assert row["forge_app_id"] == "ari:app/REAL"


@mock_aws
def test_mention_dispatch_carries_linked_repos(monkeypatch):
    # A Jira dispatch must carry the project's linked repos + a primary origin so
    # co-repo GitHub reach works and the gateway origin header is set (§B1.2).
    _make_tables()
    cfg = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    cfg.update_item(
        Key={"pk": f"jira_proj#{SITE}#ENG"},
        UpdateExpression="SET repos = :r", ExpressionAttributeValues={":r": ["acme/web"]},
    )
    dispatched = []
    jw = _load(monkeypatch, dispatched=dispatched)
    body = {"webhookEvent": "comment_created",
            "issue": {"id": "1", "key": "ENG-5"},
            "comment": {"id": "c1", "author": {"accountId": "user1"},
                        "body": {"type": "doc", "content": [
                            {"type": "paragraph", "content": [
                                {"type": "text", "text": "@workitems do it"}]}]}},
            "timestamp": "77"}
    jw.handler(_event(body))
    assert len(dispatched) == 1
    ctx = dispatched[0]["context"]
    assert ctx["repos"] == ["acme/web"]
    assert ctx["repo"] == "acme/web"  # primary origin for the gateway header


@mock_aws
def test_dedup_second_delivery(monkeypatch):
    _make_tables()
    dispatched = []
    jw = _load(monkeypatch, dispatched=dispatched)
    body = {"webhookEvent": "comment_created",
            "issue": {"id": "1", "key": "ENG-5"},
            "comment": {"id": "c1", "author": {"accountId": "user1"},
                        "body": {"type": "doc", "content": [
                            {"type": "paragraph", "content": [
                                {"type": "text", "text": "@workitems x"}]}]}},
            "timestamp": "123"}
    jw.handler(_event(body))
    jw.handler(_event(body))  # same digest → duplicate
    assert len(dispatched) == 1
