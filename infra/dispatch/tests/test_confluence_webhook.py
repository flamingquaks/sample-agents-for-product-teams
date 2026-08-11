"""Tests for the Confluence webhook receiver (confluence_webhook.py) — comment
mention resolution via the REST fetch, inline-selection capture, bot-loop guard,
and the automation fallback (atlassian-connector spec §C1.1)."""

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

SITE = "66666666-6666-6666-6666-666666666666"
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
        "products": {"jira": False, "confluence": True}, "bot_account_id": BOT,
        "bot_email": "sdlc@acme.com", "forge_app_id": "ari:app/1", "api_token_param": "/p",
        "default_space_policy": "allowlist",
    })
    boto3.resource("dynamodb", region_name=REGION).Table(TABLE).put_item(Item={
        "pk": f"confluence_space#{SITE}#DOCS", "kind": "confluence_space",
        "site_id": SITE, "space_key": "DOCS", "mode": "allow", "write_mode": "propose",
    })


class _FakeLambda:
    def __init__(self, sink):
        self.sink = sink
    def invoke(self, **kw):
        self.sink.append(json.loads(kw["Payload"]))


def _load(monkeypatch, *, dispatched, comment_body_adf, inline=None):
    for m in ("config_query", "trigger_grants", "automation", "atlassian_client",
              "mentions", "confluence_webhook"):
        sys.modules.pop(m, None)
    import mentions
    import atlassian_client as ac
    import confluence_webhook as cw
    cw.trigger_grants.reset_cache()

    monkeypatch.setattr(mentions, "verify_forge_invocation_token",
                        lambda t, **k: {"cloudId": SITE, "app": {"id": "ari:app/1"}})
    monkeypatch.setattr(ac, "fetch_token", lambda s: "tok")

    comment_doc = {"body": {"atlas_doc_format": {"value": json.dumps(comment_body_adf)}},
                   "pageId": "900"}
    if inline:
        comment_doc["inlineProperties"] = {"originalSelection": inline}

    def fake_rest(site, method, path, token, *, params=None, body=None):
        if "/comments/" in path:
            return 200, comment_doc
        if path.endswith("/pages/900"):
            return 200, {"title": "Runbook", "version": {"number": 2}, "spaceId": "500"}
        if "/spaces" in path:
            return 200, {"results": [{"key": "DOCS", "id": "500"}]}
        return 200, {}
    monkeypatch.setattr(ac, "rest", fake_rest)
    monkeypatch.setattr(cw, "_lambda", _FakeLambda(dispatched))
    monkeypatch.setattr(cw._registry, "resolve_mention",
                        lambda text: ("docwriter", "update it") if "docwriter" in text else None)
    return cw


def _event(body):
    return {"pathParameters": {"site": SITE},
            "headers": {"x-forge-invocation-token": "fit"},
            "body": json.dumps(body)}


def _adf(text):
    return {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


@mock_aws
def test_comment_mention_dispatches_with_inline_selection(monkeypatch):
    _make_tables()
    dispatched = []
    cw = _load(monkeypatch, dispatched=dispatched,
               comment_body_adf=_adf("@docwriter update it"),
               inline="the deploy step")
    body = {"event": "comment_created",
            "comment": {"id": "c1", "createdBy": {"accountId": "user1"}},
            "timestamp": "1"}
    resp = cw.handler(_event(body))
    assert resp["statusCode"] == 200
    assert len(dispatched) == 1
    d = dispatched[0]
    assert d["source"] == "confluence" and d["agent_id"] == "docwriter"
    assert d["context"]["inline_selection"] == "the deploy step"
    assert d["context"]["space_key"] == "DOCS"


@mock_aws
def test_dispatch_carries_space_repos_and_write_mode(monkeypatch):
    # A Confluence dispatch must carry the space's write_mode + linked repos +
    # a primary origin (§C1.2/§C3.2) so the dispatch block shows propose/direct
    # correctly and co-repo GitHub reach + the gateway origin header work.
    _make_tables()
    cfg = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    cfg.update_item(
        Key={"pk": f"confluence_space#{SITE}#DOCS"},
        UpdateExpression="SET repos = :r, write_mode = :w",
        ExpressionAttributeValues={":r": ["acme/docs"], ":w": "direct"},
    )
    dispatched = []
    cw = _load(monkeypatch, dispatched=dispatched,
               comment_body_adf=_adf("@docwriter update it"))
    body = {"event": "comment_created",
            "comment": {"id": "c1", "createdBy": {"accountId": "user1"}},
            "timestamp": "5"}
    cw.handler(_event(body))
    assert len(dispatched) == 1
    ctx = dispatched[0]["context"]
    assert ctx["space_key"] == "DOCS"
    assert ctx["write_mode"] == "direct"
    assert ctx["repos"] == ["acme/docs"] and ctx["repo"] == "acme/docs"


@mock_aws
def test_bot_comment_no_dispatch(monkeypatch):
    _make_tables()
    dispatched = []
    cw = _load(monkeypatch, dispatched=dispatched,
               comment_body_adf=_adf("@docwriter x"))
    body = {"event": "comment_created",
            "comment": {"id": "c1", "createdBy": {"accountId": BOT}},
            "timestamp": "1"}
    cw.handler(_event(body))
    assert dispatched == []
