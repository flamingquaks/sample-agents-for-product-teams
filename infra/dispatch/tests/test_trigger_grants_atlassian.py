"""Tests for the Atlassian WHERE-axis in trigger_grants — container_allowed
posture math per product, product-enablement gating, and the linked-repo edge
(docs/specs/atlassian-connector-spec.md §A7). Moto, no AWS."""

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

SITE = "44444444-4444-4444-4444-444444444444"


def _make_table():
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "kind", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[{
            "IndexName": "kind-index",
            "KeySchema": [
                {"AttributeName": "kind", "KeyType": "HASH"},
                {"AttributeName": "pk", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
    )


def _put(pk, **attrs):
    boto3.resource("dynamodb", region_name=REGION).Table(TABLE).put_item(
        Item={"pk": pk, **attrs}
    )


def _load():
    for m in ("config_query", "trigger_grants"):
        sys.modules.pop(m, None)
    import trigger_grants
    trigger_grants.reset_cache()
    return trigger_grants


def _site(enabled=True, status="active", jira=True, conf=True,
          proj_policy="allowlist", space_policy="allowlist"):
    _put(f"atlassian_site#{SITE}", kind="atlassian_site", site_id=SITE,
         enabled=enabled, status=status,
         products={"jira": jira, "confluence": conf},
         default_project_policy=proj_policy, default_space_policy=space_policy)


@mock_aws
def test_allowlist_requires_allow_row():
    _make_table()
    _site()
    _put(f"jira_proj#{SITE}#ENG", kind="jira_project", site_id=SITE,
         project_key="ENG", mode="allow", repos=["acme/web"])
    tg = _load()
    assert tg.container_allowed(SITE, "ENG", "jira") is True
    assert tg.container_allowed(SITE, "OPS", "jira") is False  # no allow row
    assert tg.container_repos("jira", SITE, "ENG") == ["acme/web"]


@mock_aws
def test_denylist_allows_unless_deny_row():
    _make_table()
    _site(proj_policy="denylist")
    _put(f"jira_proj#{SITE}#SECRET", kind="jira_project", site_id=SITE,
         project_key="SECRET", mode="deny")
    tg = _load()
    assert tg.container_allowed(SITE, "ANY", "jira") is True
    assert tg.container_allowed(SITE, "SECRET", "jira") is False


@mock_aws
def test_disabled_product_fails_closed():
    _make_table()
    _site(conf=False)
    _put(f"confluence_space#{SITE}#DOCS", kind="confluence_space", site_id=SITE,
         space_key="DOCS", mode="allow")
    tg = _load()
    # jira on, confluence off → a confluence container is never allowed.
    assert tg.container_allowed(SITE, "DOCS", "confluence") is False
    assert tg.atlassian_product_enabled(SITE, "confluence") is False
    assert tg.atlassian_product_enabled(SITE, "jira") is True


@mock_aws
def test_unknown_site_fails_closed():
    _make_table()
    tg = _load()
    assert tg.container_allowed("99999999-9999-9999-9999-999999999999", "ENG", "jira") is False


@mock_aws
def test_disabled_site_fails_closed():
    _make_table()
    _site(status="disabled")
    _put(f"jira_proj#{SITE}#ENG", kind="jira_project", site_id=SITE,
         project_key="ENG", mode="allow")
    tg = _load()
    assert tg.container_allowed(SITE, "ENG", "jira") is False
