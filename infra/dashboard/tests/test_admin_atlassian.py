"""Tests for the Atlassian admin API routes (admin.py) — sites (connect +
products), Jira projects, Confluence spaces, automation rules (with grant
coupling), and per-user notif prefs (atlassian-connector spec §A11). Drives the
real admin.handler + config_store on moto; the connect route's Atlassian HTTP
calls + the gateway policy sync are stubbed."""

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
os.environ["AWS_DEFAULT_REGION"] = REGION
os.environ["FLEET_CONFIG_TABLE"] = TABLE
os.environ.setdefault("STAGE", "test")

ADMIN = {"sub": "admin-1", "cognito:groups": "[admins]"}
SITE = "77777777-7777-7777-7777-777777777777"


def _event(method, resource, path=None, body=None, query=None):
    return {
        "httpMethod": method, "resource": resource, "pathParameters": path,
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": ADMIN}},
    }


def _make_table():
    boto3.client("dynamodb", region_name=REGION).create_table(
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


def _load_admin(monkeypatch):
    for m in ("admin", "config_store", "auth", "http_responses", "fleet_policy",
              "policy_sync"):
        sys.modules.pop(m, None)
    import admin
    # No gateway in these tests — the policy sync is a no-op.
    monkeypatch.setattr(admin, "_sync_repo_policy", lambda: None)
    return admin


def _body(resp):
    return json.loads(resp["body"])


def _active_site(admin):
    return admin.config_store.put_atlassian_site(
        SITE, site_url="https://acme.atlassian.net", stage="test",
        products={"jira": True, "confluence": True}, bot_account_id="712020:abc",
        bot_email="sdlc@acme.com", status=admin.config_store.ATLASSIAN_SITE_ACTIVE,
    )


@mock_aws
def test_connect_verifies_token_and_writes_row(monkeypatch):
    _make_table()
    admin = _load_admin(monkeypatch)
    monkeypatch.setattr(admin, "_atlassian_token_ok",
                        lambda url, email, tok: ({"accountId": "712020:bot"}, ""))
    monkeypatch.setattr(admin, "_resolve_cloud_id", lambda url, email, tok: SITE)
    # SSM PutParameter goes through moto.
    resp = admin.handler(_event("POST", "/admin/atlassian/sites/connect", body={
        "site_url": "https://acme.atlassian.net", "bot_email": "sdlc@acme.com",
        "api_token": "scoped-token", "products": {"jira": True, "confluence": False},
    }))
    assert resp["statusCode"] == 200
    rec = _body(resp)
    assert rec["site_id"] == SITE and rec["bot_account_id"] == "712020:bot"
    assert rec["status"] == "active"
    # The token landed as a SecureString at the derived path.
    val = boto3.client("ssm", region_name=REGION).get_parameter(
        Name=f"/sdlc-agents/test/atlassian/{SITE}/api-token", WithDecryption=True
    )["Parameter"]["Value"]
    assert val == "scoped-token"


@mock_aws
def test_connect_rejects_bad_token(monkeypatch):
    _make_table()
    admin = _load_admin(monkeypatch)
    monkeypatch.setattr(admin, "_atlassian_token_ok",
                        lambda url, email, tok: (None, "Atlassian rejected the token (HTTP 401)"))
    resp = admin.handler(_event("POST", "/admin/atlassian/sites/connect", body={
        "site_url": "https://acme.atlassian.net", "bot_email": "x@acme.com",
        "api_token": "bad",
    }))
    assert resp["statusCode"] == 400


@mock_aws
def test_products_toggle(monkeypatch):
    _make_table()
    admin = _load_admin(monkeypatch)
    _active_site(admin)
    resp = admin.handler(_event("PUT", "/admin/atlassian/sites/{site_id}/products",
                                path={"site_id": SITE},
                                body={"products": {"jira": True, "confluence": False}}))
    assert _body(resp)["products"] == {"jira": True, "confluence": False}


@mock_aws
def test_jira_project_crud_and_sync(monkeypatch):
    _make_table()
    admin = _load_admin(monkeypatch)
    _active_site(admin)
    synced = []
    monkeypatch.setattr(admin, "_sync_repo_policy", lambda: synced.append(1))
    resp = admin.handler(_event("POST", "/admin/atlassian/projects", body={
        "site_id": SITE, "project_key": "ENG", "mode": "allow", "repos": ["acme/web"],
    }))
    assert resp["statusCode"] == 200 and synced  # writes trigger the policy sync
    resp = admin.handler(_event("GET", "/admin/atlassian/projects", query={"site_id": SITE}))
    assert len(_body(resp)["projects"]) == 1
    resp = admin.handler(_event("DELETE", "/admin/atlassian/projects/{site_id}/{key}",
                                path={"site_id": SITE, "key": "ENG"}))
    assert _body(resp)["deleted"] is True


@mock_aws
def test_confluence_space_write_mode(monkeypatch):
    _make_table()
    admin = _load_admin(monkeypatch)
    _active_site(admin)
    resp = admin.handler(_event("POST", "/admin/atlassian/spaces", body={
        "site_id": SITE, "space_key": "DOCS", "mode": "allow",
        "write_mode": "propose", "write_agents": ["docwriter"],
    }))
    assert resp["statusCode"] == 200
    assert _body(resp)["write_mode"] == "propose"


@mock_aws
def test_automation_rule_grant_coupling(monkeypatch):
    _make_table()
    admin = _load_admin(monkeypatch)
    _active_site(admin)
    # Create an enabled rule → an auto- permit trigger rule is authored.
    resp = admin.handler(_event("POST", "/admin/automation-rules", body={
        "connector": "jira", "event": "issue_transitioned",
        "match": {"to_status": "Code Review", "site": SITE}, "agent_id": "adr",
        "instruction_template": "Review {{issue_key}}",
    }))
    assert resp["statusCode"] == 200
    rule_id = _body(resp)["rule_id"]
    rules = admin.config_store.list_trigger_rules("jira")
    assert any(r["subject_id"] == f"automation:jira:{rule_id}" and r["effect"] == "permit"
               for r in rules)
    # Disable → the grant is removed (default-deny backstop).
    admin.handler(_event("POST", "/admin/automation-rules/{rule_id}/disable",
                         path={"rule_id": rule_id}))
    rules = admin.config_store.list_trigger_rules("jira")
    assert not any(r["subject_id"] == f"automation:jira:{rule_id}" for r in rules)


@mock_aws
def test_automation_rule_bad_template_rejected(monkeypatch):
    _make_table()
    admin = _load_admin(monkeypatch)
    resp = admin.handler(_event("POST", "/admin/automation-rules", body={
        "connector": "jira", "event": "issue_transitioned", "agent_id": "adr",
        "instruction_template": "Review {{nope}}",
    }))
    assert resp["statusCode"] == 400


@mock_aws
def test_simulate_atlassian_uses_container_not_channel(monkeypatch):
    # The "Test access" simulator must resolve the WHERE axis from the Jira
    # project / Confluence space key (never channel_id) and use the site's
    # container posture — mirroring trigger_authz.is_authorized. Before the fix
    # it always reported workspace-not-enabled/channel-not-allowed for Atlassian.
    _make_table()
    admin = _load_admin(monkeypatch)
    _active_site(admin)  # jira + confluence enabled, allowlist default
    admin.config_store.put_jira_project(SITE, "ENG", mode="allow")
    admin.config_store.put_trigger_rule(
        connector="jira", subject_type="user", subject_id="atlassian:712020:alice",
        agent_id="workitems", workspace=SITE, effect="permit",
    )

    def sim(**kw):
        return _body(admin.handler(_event("POST", "/admin/trigger-rules/simulate", body=kw)))

    # allowed: granted principal, onboarded project (passed as project_key)
    r = sim(principal="atlassian:712020:alice", agent_id="workitems",
            source="jira", workspace=SITE, project_key="ENG")
    assert r["decision"] == "ALLOW", r
    # denied: granted principal but a project that isn't onboarded (allowlist)
    r = sim(principal="atlassian:712020:alice", agent_id="workitems",
            source="jira", workspace=SITE, project_key="OPS")
    assert r["decision"] == "DENY" and r["reason"] == "container-not-allowed"
    # denied: not granted
    r = sim(principal="atlassian:712020:bob", agent_id="workitems",
            source="jira", workspace=SITE, project_key="ENG")
    assert r["decision"] == "DENY" and r["reason"] == "no-matching-grant"


@mock_aws
def test_simulate_atlassian_site_product_disabled(monkeypatch):
    _make_table()
    admin = _load_admin(monkeypatch)
    # Site active but confluence NOT enabled → a confluence sim is site-not-enabled.
    admin.config_store.put_atlassian_site(
        SITE, site_url="https://acme.atlassian.net", stage="test",
        products={"jira": True, "confluence": False}, status=admin.config_store.ATLASSIAN_SITE_ACTIVE,
    )
    r = _body(admin.handler(_event("POST", "/admin/trigger-rules/simulate", body={
        "principal": "atlassian:712020:alice", "agent_id": "docwriter",
        "source": "confluence", "workspace": SITE, "space_key": "DOCS",
    })))
    assert r["decision"] == "DENY" and r["reason"] == "site-not-enabled"


@mock_aws
def test_simulate_atlassian_forbid_wins(monkeypatch):
    _make_table()
    admin = _load_admin(monkeypatch)
    _active_site(admin)
    admin.config_store.put_confluence_space(SITE, "DOCS", mode="allow")
    admin.config_store.put_trigger_rule(
        connector="confluence", subject_type="user", subject_id="atlassian:712020:alice",
        agent_id="docwriter", workspace=SITE, effect="permit",
    )
    admin.config_store.put_trigger_rule(
        connector="confluence", subject_type="user", subject_id="atlassian:712020:alice",
        agent_id="docwriter", workspace=SITE, effect="forbid",
    )
    r = _body(admin.handler(_event("POST", "/admin/trigger-rules/simulate", body={
        "principal": "atlassian:712020:alice", "agent_id": "docwriter",
        "source": "confluence", "workspace": SITE, "space_key": "DOCS",
    })))
    assert r["decision"] == "DENY" and r["reason"] == "explicitly-denied"


@mock_aws
def test_notif_pref_requires_verified_slack(monkeypatch):
    _make_table()
    admin = _load_admin(monkeypatch)
    cs = admin.config_store
    # Active identity but NO verified slack handle → rejected.
    ident = cs.put_identity(email="a@x.com", status=cs.IDENTITY_ACTIVE,
                            handles={"slack": {"T1": "U1"}})
    iid = ident["identity_id"]
    resp = admin.handler(_event("PUT", "/admin/notif-prefs/{identity_id}",
                                path={"identity_id": iid},
                                body={"tiers": {"actionable": ["agent_replied"]}}))
    assert resp["statusCode"] == 400
    # Verify the slack handle → now allowed.
    cs.set_identity_verified(iid, "slack", True)
    resp = admin.handler(_event("PUT", "/admin/notif-prefs/{identity_id}",
                                path={"identity_id": iid},
                                body={"tiers": {"actionable": ["agent_replied"]},
                                      "min_tier": "actionable"}))
    assert resp["statusCode"] == 200
    assert _body(resp)["tiers"] == {"actionable": ["agent_replied"]}
