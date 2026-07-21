"""Unit tests for deploy_fleet's built-in capability seeding (spec §8.3).

Covers the idempotent, non-destructive seed of the 4 system agents against a
moto-backed fleet-config table: a fresh seed writes disabled+builtin rows, and a
re-seed of an already-ENABLED/active built-in preserves its lifecycle + deploy
state (so re-running the deployer never knocks a live agent out of the registry).
"""

import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import deploy_fleet  # noqa: E402

REGION = "us-west-2"
TABLE = "fleet-config-test"


class _Runner:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run


def _make_table():
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
    )
    return boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def _get(table, agent_id):
    return table.get_item(Key={"pk": f"capability#{agent_id}"}).get("Item")


@mock_aws
def test_seed_writes_all_builtins_disabled():
    import os
    os.environ["AWS_DEFAULT_REGION"] = REGION
    table = _make_table()
    deploy_fleet.seed_builtin_capabilities(_Runner(), {"FleetConfigTableName": TABLE})
    for agent_id in ("workitems", "researcher", "docwriter", "adr"):
        row = _get(table, agent_id)
        assert row is not None, agent_id
        assert row["builtin"] is True
        assert row["enabled"] is False
        assert row["status"] == "disabled"
        assert row["kind"] == "capability"
        assert row["aliases"]  # non-empty


@mock_aws
def test_reseed_preserves_enabled_and_deploy_state():
    import os
    os.environ["AWS_DEFAULT_REGION"] = REGION
    table = _make_table()
    # Simulate an already-enabled, active built-in with a live runtime.
    table.put_item(Item={
        "pk": "capability#workitems",
        "kind": "capability",
        "agent_id": "workitems",
        "builtin": True,
        "enabled": True,
        "status": "active",
        "runtime_arn": "arn:aws:bedrock-agentcore:us-west-2:1:runtime/workitems",
        "image_tag": "build-123",
        "onboarded_at": 1000,
    })
    deploy_fleet.seed_builtin_capabilities(_Runner(), {"FleetConfigTableName": TABLE})
    row = _get(table, "workitems")
    assert row["enabled"] is True           # NOT reset to disabled
    assert row["status"] == "active"        # lifecycle preserved
    assert row["runtime_arn"].endswith("runtime/workitems")  # deploy state kept
    assert row["image_tag"] == "build-123"
    assert row["onboarded_at"] == 1000      # not clobbered


@mock_aws
def test_seed_noop_without_table_output():
    # No FleetConfigTableName (dashboard off) → no-op, no exception.
    deploy_fleet.seed_builtin_capabilities(_Runner(), {})


@mock_aws
def test_seed_dry_run_writes_nothing():
    import os
    os.environ["AWS_DEFAULT_REGION"] = REGION
    table = _make_table()
    deploy_fleet.seed_builtin_capabilities(_Runner(dry_run=True), {"FleetConfigTableName": TABLE})
    assert _get(table, "workitems") is None
