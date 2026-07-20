"""Tests for the capability record kind + registry rendering in config_store.

A "capability" is a UI-onboarded agent. These exercise the store-level contract
that Phase 2's admin API and the Dispatch Router build on: validation, the
declarative-vs-deploy-state split, lifecycle status, and that render_registry
reproduces exactly the shape router.py consumes — against moto, no AWS.
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


def _load_store():
    # Fresh import so the cached table handle binds to the moto-backed table.
    for m in ("config_store",):
        sys.modules.pop(m, None)
    import config_store

    return config_store


@mock_aws
def test_invalid_agent_id_rejected():
    _make_table()
    cs = _load_store()
    for bad in ["../evil", "Bad", "a", "x" * 70, "a b", "", "1lead", "trailing-"]:
        with pytest.raises(ValueError):
            cs.put_capability(bad)
    # A canonical id is accepted.
    cs.put_capability("triage")
    assert cs.get_capability("triage") is not None


@mock_aws
def test_pending_capability_excluded_from_registry():
    _make_table()
    cs = _load_store()
    cs.put_capability("triage", description="Triage bot")
    # No runtime yet, status pending → not routable.
    assert cs.render_registry() == {"agents": {}}


@mock_aws
def test_active_capability_rendered_in_router_shape():
    _make_table()
    cs = _load_store()
    cs.put_capability(
        "triage",
        description="Triage bot",
        aliases=["Tri", "tri", " TRI "],
        triggers={"github": ["issue_comment"]},
        authorization_users=["alice", ""],
        limits={"max_concurrent": 3, "timeout_minutes": 10},
        env={"FOO": "bar"},
        onboarded_by="admin-1",
    )
    cs.set_capability_deploy_state(
        "triage", image_tag="abc123", runtime_arn="arn:runtime/triage-xyz", build_id="b1"
    )
    cs.set_capability_status("triage", cs.CAP_ACTIVE, detail="ready")

    reg = cs.render_registry()
    assert list(reg["agents"]) == ["triage"]
    entry = reg["agents"]["triage"]
    # Exactly the keys router.py's resolve_agent / limit checks read.
    assert set(entry) == {
        "description",
        "runtime_arn",
        "aliases",
        "triggers",
        "authorization",
        "limits",
    }
    assert entry["runtime_arn"] == "arn:runtime/triage-xyz"
    assert entry["aliases"] == ["tri"]  # lowercased + de-duped
    assert entry["authorization"] == {"users": ["alice"]}  # empty principal dropped
    assert entry["triggers"] == {"github": ["issue_comment"]}
    assert entry["limits"] == {"max_concurrent": 3, "timeout_minutes": 10}
    # env is runtime-injected config, NOT part of the router registry.
    assert "env" not in entry


@mock_aws
def test_resubmit_preserves_deploy_state_and_onboarded_at():
    _make_table()
    cs = _load_store()
    cs.put_capability("triage", description="v1", onboarded_by="admin-1")
    cs.set_capability_deploy_state("triage", runtime_arn="arn:runtime/triage-xyz")
    before = cs.get_capability("triage")

    # Re-submitting the onboarding form edits declarative fields only.
    cs.put_capability("triage", description="v2")
    after = cs.get_capability("triage")
    assert after["runtime_arn"] == "arn:runtime/triage-xyz", "deploy state clobbered"
    assert after["onboarded_at"] == before["onboarded_at"]
    assert after["onboarded_by"] == "admin-1"
    assert after["description"] == "v2"


@mock_aws
def test_disable_hides_from_registry_but_keeps_row():
    _make_table()
    cs = _load_store()
    cs.put_capability("triage")
    cs.set_capability_deploy_state("triage", runtime_arn="arn:runtime/triage-xyz")
    cs.set_capability_status("triage", cs.CAP_ACTIVE)
    assert "triage" in cs.render_registry()["agents"]

    cs.set_capability_status("triage", cs.CAP_DISABLED)
    assert cs.render_registry() == {"agents": {}}
    assert cs.get_capability("triage") is not None  # row retained for re-enable


@mock_aws
def test_status_write_to_missing_row_fails():
    _make_table()
    cs = _load_store()
    with pytest.raises(Exception):  # ConditionalCheckFailed — no such capability
        cs.set_capability_status("ghost", cs.CAP_ACTIVE)


@mock_aws
def test_delete_capability_reports_removal():
    _make_table()
    cs = _load_store()
    cs.put_capability("triage")
    assert cs.delete_capability("triage") is True
    assert cs.delete_capability("triage") is False  # idempotent no-op
    assert cs.get_capability("triage") is None


@mock_aws
def test_repos_and_capabilities_coexist():
    """The new kind must not disturb the existing repo/settings kinds sharing the
    single table — list_repos and list_capabilities each filter to their own."""
    _make_table()
    cs = _load_store()
    cs.put_repo("acme/app", onboarded_by="admin-1", status="active")
    cs.put_capability("triage", onboarded_by="admin-1")
    assert [r["repo"] for r in cs.list_repos()] == ["acme/app"]
    assert [c["agent_id"] for c in cs.list_capabilities()] == ["triage"]
