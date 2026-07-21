"""Tests for the capability record kind + registry rendering in config_store.

A "capability" is a UI-onboarded agent. These exercise the store-level contract
that Phase 2's admin API and the Dispatch Router build on: validation, the
declarative-vs-deploy-state split, lifecycle status, and that render_registry
reproduces exactly the shape router.py consumes — against moto, no AWS.
"""

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
REGISTRY_PARAM = "/sdlc-agents/test/registry"
os.environ["AWS_DEFAULT_REGION"] = REGION
os.environ["FLEET_CONFIG_TABLE"] = TABLE
os.environ["REGISTRY_PARAM"] = REGISTRY_PARAM

ADMIN = {"sub": "admin-1", "cognito:groups": "[admins]"}
OPERATOR = {"sub": "op-1", "cognito:groups": "[operators]"}


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
    # Fresh import so the cached table handle binds to the moto-backed table.
    for m in ("config_store",):
        sys.modules.pop(m, None)
    import config_store

    return config_store


def _load_admin():
    for m in ("admin", "config_store", "auth", "http_responses"):
        sys.modules.pop(m, None)
    # Default to no build pipeline so a test that doesn't stub CodeBuild leaves a
    # capability pending rather than reaching for a real StartBuild. Tests that
    # exercise the build path set this via _stub_codebuild.
    os.environ.pop("CAPABILITY_BUILD_PROJECT", None)
    import admin

    return admin


def _event(method, resource, claims=ADMIN, path=None, body=None):
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path,
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": claims}},
    }


def _read_registry():
    """The rendered registry the admin routes published to SSM (parsed)."""
    import yaml

    val = boto3.client("ssm", region_name=REGION).get_parameter(Name=REGISTRY_PARAM)[
        "Parameter"
    ]["Value"]
    return yaml.safe_load(val)


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
    # Exactly the keys router.py's resolve_agent / limit checks read. NO
    # authorization block — who may trigger is decided by Cedar trigger rules,
    # not a per-capability allowlist carried in the registry.
    assert set(entry) == {
        "description",
        "runtime_arn",
        "aliases",
        "triggers",
        "limits",
    }
    assert entry["runtime_arn"] == "arn:runtime/triage-xyz"
    assert entry["aliases"] == ["tri"]  # lowercased + de-duped
    assert "authorization" not in entry
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
def test_live_agent_stays_routable_while_rebuilding_or_failed():
    """A weekly rebuild of a LIVE agent must not drop it from dispatch: while it's
    building — and even if that rebuild fails — its existing runtime still serves,
    so it stays in the registry on its current ARN. A first-onboard (no runtime
    yet) is still excluded while building/failed."""
    _make_table()
    cs = _load_store()
    # Live agent with a working runtime.
    cs.put_capability("triage")
    cs.set_capability_deploy_state("triage", runtime_arn="arn:runtime/triage-live")
    cs.set_capability_status("triage", cs.CAP_ACTIVE)

    # Weekly rebuild marks it building — still routable on the old ARN.
    cs.set_capability_status("triage", cs.CAP_BUILDING)
    assert cs.render_registry()["agents"]["triage"]["runtime_arn"] == "arn:runtime/triage-live"
    # Rebuild fails — STILL routable on the old runtime (never taken down).
    cs.set_capability_status("triage", cs.CAP_FAILED)
    assert cs.render_registry()["agents"]["triage"]["runtime_arn"] == "arn:runtime/triage-live"

    # A brand-new agent that never deployed (no runtime_arn) is excluded while
    # building/failed.
    cs.put_capability("newbie")
    cs.set_capability_status("newbie", cs.CAP_BUILDING)
    assert "newbie" not in cs.render_registry()["agents"]


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


# --- admin API routes (Phase 2) ----------------------------------------------


@mock_aws
def test_operator_cannot_touch_capabilities():
    _make_table()
    admin = _load_admin()
    assert (
        admin.handler(_event("GET", "/admin/capabilities", claims=OPERATOR))[
            "statusCode"
        ]
        == 403
    )
    assert (
        admin.handler(
            _event("POST", "/admin/capabilities", claims=OPERATOR, body={"agent_id": "x"})
        )["statusCode"]
        == 403
    )


def _stub_codebuild(admin, monkeypatch, sink):
    """Make admin's lazily-imported boto3.client('codebuild').start_build record
    the call instead of hitting AWS. The build project env var is set so the
    onboard path takes the 'start a build' branch."""
    os.environ["CAPABILITY_BUILD_PROJECT"] = "sdlc-agent-builder-test"
    import boto3

    real_client = boto3.client

    class _CB:
        def start_build(self, **kw):
            sink.append(kw)
            return {"build": {"id": "b-1"}}

    def fake_client(name, *a, **k):
        if name == "codebuild":
            return _CB()
        return real_client(name, *a, **k)

    monkeypatch.setattr(boto3, "client", fake_client)


@mock_aws
def test_onboard_capability_persists_lists_and_starts_build(monkeypatch):
    _make_table()
    admin = _load_admin()
    builds: list = []
    _stub_codebuild(admin, monkeypatch, builds)
    resp = admin.handler(
        _event(
            "POST",
            "/admin/capabilities",
            body={
                "agent_id": "triage",
                "description": "Triage bot",
                "aliases": ["tri"],
                "triggers": {"github": ["issue_comment"]},
                "limits": {"max_concurrent": 3},
                "env": {"FOO": "bar"},
            },
        )
    )
    assert resp["statusCode"] == 200, resp["body"]
    body = json.loads(resp["body"])
    assert body["agent_id"] == "triage"
    assert body["status"] == "building"  # build started, not yet active
    assert body["onboarded_by"] == "admin-1"

    # The shared build project was triggered with the AGENT_NAME override.
    assert len(builds) == 1
    overrides = {v["name"]: v["value"] for v in builds[0]["environmentVariablesOverride"]}
    assert overrides["AGENT_NAME"] == "triage"
    assert overrides["IMAGE_TAG"].startswith("build-")

    listed = json.loads(
        admin.handler(_event("GET", "/admin/capabilities"))["body"]
    )["capabilities"]
    assert [c["agent_id"] for c in listed] == ["triage"]
    # Building capability is not yet in the published registry (no runtime).
    assert _read_registry() == {"agents": {}}


@mock_aws
def test_onboard_rejects_reserved_env_keys(monkeypatch):
    """A capability must not be able to set the fleet security gates (guardrail /
    gateway URL) — that would disable the guardrail or repoint the tool boundary."""
    _make_table()
    admin = _load_admin()
    for key in ("BEDROCK_GUARDRAIL_ID", "GATEWAY_MCP_URL", "BEDROCK_GUARDRAIL_VERSION"):
        resp = admin.handler(
            _event("POST", "/admin/capabilities",
                   body={"agent_id": "triage", "env": {key: "x"}})
        )
        assert resp["statusCode"] == 400, key
        assert "reserved" in json.loads(resp["body"])["error"]


def test_capability_env_pairs_base_gates_always_win():
    """Even if a row somehow carries a reserved key, the merge lets the base
    (stack) value win — defense in depth behind the API rejection."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "config_store_x", str(Path(__file__).resolve().parents[1] / "config_store.py")
    )
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)
    base = {"BEDROCK_GUARDRAIL_ID": "real", "GATEWAY_MCP_URL": "https://real/mcp"}
    cap = {"env": {"BEDROCK_GUARDRAIL_ID": "", "GATEWAY_MCP_URL": "https://evil", "FOO": "bar"}}
    merged = cs.capability_env_pairs(cap, base)
    assert merged["BEDROCK_GUARDRAIL_ID"] == "real"
    assert merged["GATEWAY_MCP_URL"] == "https://real/mcp"
    assert merged["FOO"] == "bar"


@mock_aws
def test_onboard_without_build_project_stays_pending(monkeypatch):
    _make_table()
    admin = _load_admin()
    os.environ.pop("CAPABILITY_BUILD_PROJECT", None)
    resp = admin.handler(
        _event("POST", "/admin/capabilities", body={"agent_id": "triage"})
    )
    assert resp["statusCode"] == 200
    # No build pipeline wired → left pending, not failed.
    assert json.loads(resp["body"])["status"] == "pending"


@mock_aws
def test_onboard_rejects_bad_agent_id():
    _make_table()
    admin = _load_admin()
    resp = admin.handler(
        _event("POST", "/admin/capabilities", body={"agent_id": "../evil"})
    )
    assert resp["statusCode"] == 400
    assert "agent_id" in json.loads(resp["body"])["error"]


@mock_aws
def test_onboard_rejects_bad_trigger_source_and_env():
    _make_table()
    admin = _load_admin()
    bad_trigger = admin.handler(
        _event(
            "POST",
            "/admin/capabilities",
            body={"agent_id": "triage", "triggers": {"pager": ["x"]}},
        )
    )
    assert bad_trigger["statusCode"] == 400
    bad_env = admin.handler(
        _event(
            "POST",
            "/admin/capabilities",
            body={"agent_id": "triage", "env": {"lower_case": "x"}},
        )
    )
    assert bad_env["statusCode"] == 400
    comma_env = admin.handler(
        _event(
            "POST",
            "/admin/capabilities",
            body={"agent_id": "triage", "env": {"K": "a,b"}},
        )
    )
    assert comma_env["statusCode"] == 400


@mock_aws
def test_edit_live_capability_republishes_registry(monkeypatch):
    """Editing an already-active capability (e.g. add an alias) must take effect
    in the router registry immediately."""
    _make_table()
    admin = _load_admin()
    cs = _load_store()
    builds: list = []
    _stub_codebuild(admin, monkeypatch, builds)
    admin.handler(
        _event("POST", "/admin/capabilities", body={"agent_id": "triage", "aliases": ["tri"]})
    )
    # Simulate the build/runtime lifecycle bringing it up.
    cs.set_capability_deploy_state("triage", runtime_arn="arn:runtime/triage-xyz")
    cs.set_capability_status("triage", cs.CAP_ACTIVE)

    # Edit: add an alias. Republish should reflect it.
    admin.handler(
        _event(
            "POST",
            "/admin/capabilities",
            body={"agent_id": "triage", "aliases": ["tri", "triage-bot"]},
        )
    )
    reg = _read_registry()
    assert reg["agents"]["triage"]["aliases"] == ["tri", "triage-bot"]
    assert reg["agents"]["triage"]["runtime_arn"] == "arn:runtime/triage-xyz"


@mock_aws
def test_delete_capability_route():
    _make_table()
    admin = _load_admin()
    admin.handler(_event("POST", "/admin/capabilities", body={"agent_id": "triage"}))
    resp = admin.handler(
        _event(
            "DELETE",
            "/admin/capabilities/{agent_id}",
            path={"agent_id": "triage"},
        )
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"agent_id": "triage", "deleted": True}
    assert (
        json.loads(admin.handler(_event("GET", "/admin/capabilities"))["body"])[
            "capabilities"
        ]
        == []
    )
