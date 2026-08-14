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
    for agent_id in ("workitems", "researcher", "docwriter", "adr", "reviewer"):
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


# --- foundation parameter assembly (single full-deploy) ----------------------


class _RecordingRunner:
    """Captures the commands deploy_foundation would run (never executes)."""

    def __init__(self):
        self.dry_run = False
        self.commands = []

    def run(self, cmd, cwd=None):
        self.commands.append(list(cmd))

    def deploy_cmd(self):
        return next(c for c in self.commands if c[:2] == ["sam", "deploy"])

    def overrides(self):
        cmd = self.deploy_cmd()
        i = cmd.index("--parameter-overrides")
        # Everything after the flag is a ParameterKey=..,ParameterValue=.. token.
        out = {}
        for tok in cmd[i + 1:]:
            if not tok.startswith("ParameterKey="):
                break
            key = tok.split("ParameterKey=", 1)[1].split(",", 1)[0]
            val = tok.split("ParameterValue=", 1)[1]
            out[key] = val
        return out


def test_new_stack_full_params_passed_in_one_deploy(monkeypatch):
    # No existing stack → overrides merge with Stage and reach sam deploy, so a
    # single invocation deploys the FULL solution (dashboard + gateway on).
    monkeypatch.setattr(deploy_fleet, "_describe_stack", lambda region, stack: None)
    r = _RecordingRunner()
    deploy_fleet.deploy_foundation(
        r, "staging", "us-east-1", auto_approve=True,
        param_overrides={
            "DeployDashboard": "true", "DeployGateway": "true",
            "GatewayPolicyEnforcement": "LOG_ONLY",
        },
    )
    ov = r.overrides()
    assert ov["Stage"] == "staging"
    assert ov["DeployDashboard"] == "true"
    assert ov["DeployGateway"] == "true"
    assert ov["GatewayPolicyEnforcement"] == "LOG_ONLY"
    # Non-interactive path requested.
    assert "--no-confirm-changeset" in r.deploy_cmd()


def test_empty_param_value_passed_literally(monkeypatch):
    # An empty Asana GID must reach sam as ParameterValue= (not a parse error).
    monkeypatch.setattr(deploy_fleet, "_describe_stack", lambda region, stack: None)
    r = _RecordingRunner()
    deploy_fleet.deploy_foundation(
        r, "staging", "us-east-1", auto_approve=True,
        param_overrides={"WorkitemsBotGID": ""},
    )
    assert "ParameterKey=WorkitemsBotGID,ParameterValue=" in r.deploy_cmd()


def test_override_wins_over_preserved_on_redeploy(monkeypatch):
    # Existing stack has the gateway off; this run flips it on. The override must
    # win over the preserved value, and untouched preserved params carry forward.
    monkeypatch.setattr(
        deploy_fleet, "_describe_stack",
        lambda region, stack: {"Parameters": [
            {"ParameterKey": "Stage", "ParameterValue": "staging"},
            {"ParameterKey": "DeployDashboard", "ParameterValue": "true"},
            {"ParameterKey": "DeployGateway", "ParameterValue": "false"},
            {"ParameterKey": "WorkitemsBotGID", "ParameterValue": "12345"},
        ]},
    )
    r = _RecordingRunner()
    deploy_fleet.deploy_foundation(
        r, "staging", "us-east-1", auto_approve=True,
        param_overrides={"DeployGateway": "true"},
    )
    ov = r.overrides()
    assert ov["DeployGateway"] == "true"       # override won
    assert ov["DeployDashboard"] == "true"     # preserved
    assert ov["WorkitemsBotGID"] == "12345"    # preserved


def test_confirm_changeset_by_default(monkeypatch):
    monkeypatch.setattr(deploy_fleet, "_describe_stack", lambda region, stack: None)
    r = _RecordingRunner()
    deploy_fleet.deploy_foundation(r, "staging", "us-east-1", auto_approve=False)
    assert "--confirm-changeset" in r.deploy_cmd()
    assert "--no-confirm-changeset" not in r.deploy_cmd()
