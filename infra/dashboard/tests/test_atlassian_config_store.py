"""Tests for the Atlassian record kinds in config_store — sites, Jira projects,
Confluence spaces, automation rules, and per-user notif prefs
(docs/specs/atlassian-connector-spec.md §A6/§A8/§A9/§A11).

Store-level contract the admin API + router build on: id validation (every id
becomes a Cedar literal, an SSM path, or a REST key, so a bad one is rejected at
the boundary), the per-site container listings, the allowlist derivations that
feed the Cedar forbids, and the automation-rule template-var allowlist. Moto, no
AWS.
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

SITE = "33333333-3333-3333-3333-333333333333"


def _make_table():
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "kind", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "kind-index",
                "KeySchema": [
                    {"AttributeName": "kind", "KeyType": "HASH"},
                    {"AttributeName": "pk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )


def _load_store():
    sys.modules.pop("config_store", None)
    import config_store

    return config_store


def _active_site(cs, **kw):
    return cs.put_atlassian_site(
        SITE, site_url="https://acme.atlassian.net", stage=STAGE,
        products={"jira": True, "confluence": True},
        bot_account_id="712020:abc", bot_email="sdlc@acme.com",
        status=cs.ATLASSIAN_SITE_ACTIVE, **kw,
    )


# --- site validation ----------------------------------------------------------


@mock_aws
def test_site_roundtrip_and_token_path():
    _make_table()
    cs = _load_store()
    rec = _active_site(cs)
    assert rec["api_token_param"] == f"/sdlc-agents/test/atlassian/{SITE}/api-token"
    assert rec["products"] == {"jira": True, "confluence": True}
    assert rec["webhook_last_seen"] == {"jira": None, "confluence": None}


@mock_aws
def test_bad_site_id_rejected():
    _make_table()
    cs = _load_store()
    with pytest.raises(ValueError):
        cs.put_atlassian_site("not-a-uuid", site_url="https://x.atlassian.net", stage=STAGE)


@mock_aws
def test_bad_site_url_rejected():
    _make_table()
    cs = _load_store()
    with pytest.raises(ValueError):
        cs.put_atlassian_site(SITE, site_url="javascript:alert(1)", stage=STAGE)


@mock_aws
def test_set_products_and_status():
    _make_table()
    cs = _load_store()
    _active_site(cs)
    cs.set_atlassian_products(SITE, {"jira": True, "confluence": False})
    assert cs.get_atlassian_site(SITE)["products"] == {"jira": True, "confluence": False}
    cs.set_atlassian_site_status(SITE, cs.ATLASSIAN_SITE_DISABLED)
    assert cs.get_atlassian_site(SITE)["status"] == "disabled"


# --- Jira projects ------------------------------------------------------------


@mock_aws
def test_jira_project_key_validation():
    _make_table()
    cs = _load_store()
    _active_site(cs)
    cs.put_jira_project(SITE, "ENG", mode="allow", repos=["Acme/Web"])
    assert cs.get_jira_project(SITE, "ENG")["repos"] == ["acme/web"]
    with pytest.raises(ValueError):
        cs.put_jira_project(SITE, "eng", mode="allow")  # lowercase key
    with pytest.raises(ValueError):
        cs.put_jira_project(SITE, "ENG", mode="maybe")  # bad mode


@mock_aws
def test_allowed_jira_projects_only_allow_on_active_jira_site():
    _make_table()
    cs = _load_store()
    _active_site(cs)
    cs.put_jira_project(SITE, "ENG", mode="allow")
    cs.put_jira_project(SITE, "OPS", mode="deny")
    got = {p["project_key"] for p in cs.allowed_jira_projects()}
    assert got == {"ENG"}
    # Disable jira on the site → no projects render.
    cs.set_atlassian_products(SITE, {"jira": False, "confluence": True})
    assert cs.allowed_jira_projects() == []


# --- Confluence spaces --------------------------------------------------------


@mock_aws
def test_confluence_space_rejects_personal_and_bad_write_mode():
    _make_table()
    cs = _load_store()
    _active_site(cs)
    cs.put_confluence_space(SITE, "DOCS", mode="allow", write_mode="propose",
                            write_agents=["docwriter"])
    row = cs.get_confluence_space(SITE, "DOCS")
    assert row["write_mode"] == "propose" and row["write_agents"] == ["docwriter"]
    with pytest.raises(ValueError):
        cs.put_confluence_space(SITE, "~712020abc", mode="allow")  # personal
    with pytest.raises(ValueError):
        cs.put_confluence_space(SITE, "DOCS", mode="allow", write_mode="yolo")


@mock_aws
def test_allowed_confluence_spaces_gated_by_site():
    _make_table()
    cs = _load_store()
    _active_site(cs)
    cs.put_confluence_space(SITE, "DOCS", mode="allow")
    cs.put_confluence_space(SITE, "SECRET", mode="deny")
    assert {s["space_key"] for s in cs.allowed_confluence_spaces()} == {"DOCS"}


# --- automation rules ---------------------------------------------------------


@mock_aws
def test_automation_rule_template_var_allowlist():
    _make_table()
    cs = _load_store()
    with pytest.raises(ValueError, match="unknown template variable"):
        cs.put_automation_rule(
            connector="jira", event="issue_transitioned",
            match={"to_status": "Code Review"}, agent_id="adr",
            instruction_template="Review {{issue_key}} and {{bogus_var}}",
        )


@mock_aws
def test_automation_rule_roundtrip_and_event_validation():
    _make_table()
    cs = _load_store()
    rule = cs.put_automation_rule(
        connector="jira", event="issue_transitioned",
        match={"to_status": "Code Review", "project": "ENG"}, agent_id="adr",
        instruction_template="Review the code for {{issue_key}}: {{summary}}.",
    )
    assert rule["action"]["agent_id"] == "adr"
    assert cs.get_automation_rule(rule["rule_id"])["enabled"] is True
    cs.set_automation_rule_enabled(rule["rule_id"], False)
    assert cs.get_automation_rule(rule["rule_id"])["enabled"] is False
    with pytest.raises(ValueError):
        cs.put_automation_rule(
            connector="confluence", event="issue_transitioned",  # wrong family
            agent_id="adr", instruction_template="x",
        )


@mock_aws
def test_automation_principal_namespace():
    cs = _load_store()
    assert cs.automation_principal("jira", "r1") == "automation:jira:r1"


# --- notif prefs --------------------------------------------------------------


@mock_aws
def test_notif_pref_roundtrip():
    _make_table()
    cs = _load_store()
    cs.put_notif_pref("id-1", tiers={"actionable": ["agent_replied"]}, min_tier="actionable")
    assert cs.get_notif_pref("id-1")["tiers"] == {"actionable": ["agent_replied"]}
    assert cs.delete_notif_pref("id-1") is True


# --- per-product trigger rules ------------------------------------------------


@mock_aws
def test_trigger_rule_atlassian_workspace_is_site_id():
    _make_table()
    cs = _load_store()
    rule = cs.put_trigger_rule(
        connector="jira", subject_type="group", subject_id="grp-eng",
        agent_id="workitems", workspace=SITE, effect="permit",
    )
    assert rule["workspace"] == SITE
    with pytest.raises(ValueError):
        cs.put_trigger_rule(
            connector="jira", subject_type="group", subject_id="g",
            workspace="T0SLACK",  # a slack team id is not a valid atlassian site
        )
