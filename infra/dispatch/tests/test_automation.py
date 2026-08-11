"""Tests for the automation-rule engine (automation.py) — fact normalization,
match semantics (AND/wildcard/labels_any), template-var allowlist, and the loop
brakes: cooldown, hourly ceiling, chain depth (atlassian-connector spec §A8)."""

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

SITE = {"site_id": "s-1", "site_url": "https://acme.atlassian.net"}


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
            "Projection": {"ProjectionType": "ALL"},
        }],
    )
    ddb.create_table(
        TableName=ASSIGN, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "assignment_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "assignment_id", "KeyType": "HASH"}],
    )


def _rule(**kw):
    base = {"pk": f"automation_rule#{kw['rule_id']}", "kind": "automation_rule",
            "enabled": True, "connector": "jira", "event": "issue_transitioned",
            "match": {}, "cooldown_seconds": 3600,
            "action": {"agent_id": "adr", "instruction_template": "Review {{issue_key}}"}}
    base.update(kw)
    boto3.resource("dynamodb", region_name=REGION).Table(TABLE).put_item(Item=base)


def _load():
    for m in ("config_query", "automation"):
        sys.modules.pop(m, None)
    import automation
    automation.reset_cache()
    return automation


def test_jira_facts_detects_transition():
    au = _load()
    payload = {"webhookEvent": "jira:issue_updated",
               "issue": {"key": "ENG-1", "fields": {"summary": "hi"}},
               "changelog": {"items": [{"field": "status", "fromString": "To Do",
                                        "toString": "Code Review"}]}}
    facts = au.jira_facts(payload, SITE)
    assert facts["event"] == "issue_transitioned"
    assert facts["to_status"] == "Code Review" and facts["project"] == "ENG"


def test_jira_facts_forge_product_trigger_shape():
    # The Forge forwarder delivers eventType="avi:jira:updated:issue" (not the
    # classic webhookEvent). Fact normalization must map it the same way, or Jira
    # automation never fires.
    au = _load()
    transition = {"eventType": "avi:jira:updated:issue",
                  "issue": {"key": "ENG-2", "fields": {}},
                  "changelog": {"items": [{"field": "status", "toString": "Done"}]}}
    f = au.jira_facts(transition, SITE)
    assert f["event"] == "issue_transitioned" and f["to_status"] == "Done"

    created = {"eventType": "avi:jira:created:issue", "issue": {"key": "ENG-3", "fields": {}}}
    assert au.jira_facts(created, SITE)["event"] == "issue_created"

    commented = {"eventType": "avi:jira:created:comment",
                 "issue": {"key": "ENG-4", "fields": {}}, "comment": {"id": "c1"}}
    assert au.jira_facts(commented, SITE)["event"] == "issue_commented"


def test_confluence_facts_label():
    au = _load()
    payload = {"event": "label_added",
               "page": {"id": "9", "title": "T", "space": {"key": "DOCS"}},
               "label": "sdlc-review"}
    facts = au.confluence_facts(payload, SITE)
    assert facts["event"] == "page_labeled" and facts["label"] == "sdlc-review"
    assert facts["space"] == "DOCS"


def test_confluence_facts_forge_shape_and_label_removed_ignored():
    au = _load()
    # Forge product-trigger shape for a label add → page_labeled.
    added = {"eventType": "avi:confluence:created:label",
             "content": {"id": "9", "space": {"key": "DOCS"}}, "label": "sdlc-review"}
    assert au.confluence_facts(added, SITE)["event"] == "page_labeled"
    # Label REMOVED (avi:confluence:deleted:label) must NOT fire page_labeled.
    removed = {"eventType": "avi:confluence:deleted:label",
               "content": {"id": "9", "space": {"key": "DOCS"}}, "label": "sdlc-review"}
    assert au.confluence_facts(removed, SITE) is None
    # Forge page-created/updated shapes.
    assert au.confluence_facts(
        {"eventType": "avi:confluence:created:page",
         "content": {"id": "9", "space": {"key": "DOCS"}}}, SITE)["event"] == "page_created"
    assert au.confluence_facts(
        {"eventType": "avi:confluence:updated:page",
         "content": {"id": "9", "space": {"key": "DOCS"}}}, SITE)["event"] == "page_updated"


def test_match_and_semantics_and_wildcard():
    au = _load()
    rule = {"connector": "jira", "event": "issue_transitioned", "enabled": True,
            "rule_id": "r", "match": {"to_status": "Code Review", "project": "*"}}
    facts = {"to_status": "Code Review", "project": "ENG"}
    assert au._rule_matches(rule["match"], facts) is True
    assert au._rule_matches({"to_status": "Done"}, facts) is False


def test_match_labels_any_or_set():
    au = _load()
    facts = {"labels_present": ["needs-review", "urgent"]}
    assert au._rule_matches({"labels_any": ["needs-review"]}, facts) is True
    assert au._rule_matches({"labels_any": ["other"]}, facts) is False


def test_render_template_allowlist_only():
    au = _load()
    out = au.render_template("jira", "Do {{issue_key}} now {{secret_field}}",
                             {"issue_key": "ENG-1", "secret_field": "x"})
    assert out == "Do ENG-1 now "  # unknown var renders empty


@mock_aws
def test_cooldown_suppresses_refire():
    _make_tables()
    _rule(rule_id="r1", match={"to_status": "Code Review"})
    au = _load()
    fired = []
    def dispatch(agent, instr, sender, ctx, tt):
        fired.append(sender)
    facts = {"event": "issue_transitioned", "to_status": "Code Review",
             "issue_key": "ENG-1", "unit": "ENG-1", "project": "ENG"}
    n1 = au.match_and_dispatch(connector="jira", event="issue_transitioned",
                               facts=facts, site=SITE, dispatch=dispatch)
    n2 = au.match_and_dispatch(connector="jira", event="issue_transitioned",
                               facts=facts, site=SITE, dispatch=dispatch)
    assert n1 == 1 and n2 == 0  # second fire suppressed by cooldown
    assert fired == ["automation:jira:r1"]


@mock_aws
def test_dispatch_context_carries_linked_repos(monkeypatch):
    # An automation dispatch must carry the project's linked repos + a primary
    # origin, same as a mention dispatch (§B1.2), so co-repo GitHub reach works.
    _make_tables()
    _rule(rule_id="r1", match={"to_status": "Code Review"})
    au = _load()
    monkeypatch.setattr(au, "_dispatch_context", au._dispatch_context)  # keep real
    import trigger_grants
    monkeypatch.setattr(trigger_grants, "container_repos",
                        lambda product, sid, key: ["acme/web"] if key == "ENG" else [])
    captured = {}
    def dispatch(agent, instr, sender, ctx, tt):
        captured.update(ctx)
    facts = {"event": "issue_transitioned", "to_status": "Code Review",
             "issue_key": "ENG-1", "unit": "ENG-1", "project": "ENG"}
    au.match_and_dispatch(connector="jira", event="issue_transitioned",
                          facts=facts, site=SITE, dispatch=dispatch)
    assert captured["repos"] == ["acme/web"]
    assert captured["repo"] == "acme/web"


@mock_aws
def test_chain_depth_cap():
    _make_tables()
    _rule(rule_id="r1", match={})
    au = _load()
    fired = []
    au.match_and_dispatch(connector="jira", event="issue_transitioned",
                          facts={"unit": "u", "issue_key": "E-1"}, site=SITE,
                          dispatch=lambda *a: fired.append(1),
                          chain=["a", "b", "c"])  # depth 3 → cap reached
    assert fired == []


@mock_aws
def test_rule_not_in_own_chain():
    _make_tables()
    _rule(rule_id="r1", match={})
    au = _load()
    fired = []
    au.match_and_dispatch(connector="jira", event="issue_transitioned",
                          facts={"unit": "u", "issue_key": "E-1"}, site=SITE,
                          dispatch=lambda *a: fired.append(1), chain=["r1"])
    assert fired == []  # r1 already in the chain → skipped


@mock_aws
def test_disabled_rule_inert():
    _make_tables()
    _rule(rule_id="r1", enabled=False, match={})
    au = _load()
    n = au.match_and_dispatch(connector="jira", event="issue_transitioned",
                              facts={"unit": "u", "issue_key": "E-1"}, site=SITE,
                              dispatch=lambda *a: None)
    assert n == 0
