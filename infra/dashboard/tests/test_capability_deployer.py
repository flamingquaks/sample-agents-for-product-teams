"""Tests for the capability deployer (capability_deployer.py).

The deployer runs off a CodeBuild-completion EventBridge event and drives the
runtime lifecycle. moto doesn't model bedrock-agentcore-control or the runtime
role dance, so we stub the agentcore + iam clients and the config_store writes,
and assert the ORCHESTRATION: success → active + republish; build failure →
failed with no runtime touched; runtime-not-READY → failed; a working runtime is
never torn down on error.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("FLEET_CONFIG_TABLE", "fleet-config-test")
os.environ.setdefault("REGISTRY_PARAM", "/sdlc-agents/test/registry")
os.environ.setdefault("STAGE", "test")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault(
    "RUNTIME_PERMISSIONS_BOUNDARY_ARN",
    "arn:aws:iam::123456789012:policy/sdlc-capability-runtime-boundary-test",
)
os.environ.setdefault("BEDROCK_GUARDRAIL_ID", "gr-1")
os.environ.setdefault("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
os.environ.setdefault("GATEWAY_MCP_URL", "https://gw.example/mcp")


def _fresh():
    """Import the deployer with its module-level clients reset per test."""
    for m in ("capability_deployer", "config_store"):
        sys.modules.pop(m, None)
    import capability_deployer as cd

    return cd


class _FakeAgentCore:
    def __init__(self, existing=None, statuses=None, tags=None):
        # existing: name -> id. tags: arn -> {k: v} (fleet tag presence per runtime).
        self._existing = existing or {}
        self._statuses = statuses or {}
        self._tags = tags or {}
        self.created = []
        self.updated = []

    def _arn(self, rt_id):
        return f"arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/{rt_id}"

    def get_paginator(self, _name):
        rts = [
            {"agentRuntimeName": n, "agentRuntimeId": i, "agentRuntimeArn": self._arn(i)}
            for n, i in self._existing.items()
        ]

        class _P:
            def paginate(self):
                return [{"agentRuntimes": rts}]

        return _P()

    def list_tags_for_resource(self, resourceArn):
        return {"tags": self._tags.get(resourceArn, {})}

    def create_agent_runtime(self, **kw):
        self.created.append(kw)
        return {"agentRuntimeId": "new-rt-id", "agentRuntimeArn": self._arn("new-rt-id")}

    def update_agent_runtime(self, **kw):
        self.updated.append(kw)
        return {
            "agentRuntimeId": kw["agentRuntimeId"],
            "agentRuntimeArn": self._arn(kw["agentRuntimeId"]),
        }

    def get_agent_runtime(self, agentRuntimeId):
        seq = self._statuses.get(agentRuntimeId, ["READY"])
        return {"status": seq.pop(0) if len(seq) > 1 else seq[0]}


class _FakeIam:
    class exceptions:
        class EntityAlreadyExistsException(Exception):
            pass

    def __init__(self, existing_roles=()):
        self._existing = set(existing_roles)
        self.created = []
        self.put_policies = []
        self.attached = []

    def create_role(self, **kw):
        if kw["RoleName"] in self._existing:
            raise _FakeIam.exceptions.EntityAlreadyExistsException()
        self.created.append(kw)

    def put_role_policy(self, **kw):
        self.put_policies.append(kw)

    def attach_role_policy(self, **kw):
        self.attached.append(kw)


def _install_fakes(cd, monkeypatch, agentcore, iam):
    monkeypatch.setattr(cd, "_acc", lambda: agentcore)
    monkeypatch.setattr(cd, "_iam_client", lambda: iam)
    monkeypatch.setattr(cd, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    # Record config_store transitions instead of hitting DynamoDB.
    calls = {"status": [], "deploy": [], "published": 0}
    monkeypatch.setattr(cd.config_store, "set_capability_status",
                        lambda a, s, detail="": calls["status"].append((a, s, detail)))
    monkeypatch.setattr(cd.config_store, "set_capability_deploy_state",
                        lambda a, **kw: calls["deploy"].append((a, kw)))
    def _pub():
        calls["published"] += 1
    monkeypatch.setattr(cd.config_store, "publish_registry", _pub)
    return calls


def _build_event(agent_id, image_tag, status):
    return {
        "detail": {
            "build-status": status,
            "additional-information": {
                "environment": {
                    "environment-variables": [
                        {"name": "AGENT_NAME", "value": agent_id},
                        {"name": "IMAGE_TAG", "value": image_tag},
                    ]
                }
            },
        }
    }


def test_successful_build_creates_runtime_and_activates(monkeypatch):
    cd = _fresh()
    agentcore = _FakeAgentCore(statuses={"new-rt-id": ["CREATING", "READY"]})
    iam = _FakeIam()
    calls = _install_fakes(cd, monkeypatch, agentcore, iam)
    monkeypatch.setattr(cd.config_store, "get_capability",
                        lambda a: {"agent_id": a, "env": {"ASANA_PROJECT_GID": "123"}})

    out = cd.handler(_build_event("triage", "build-1", "SUCCEEDED"))
    assert out == {"ok": True, "agent_id": "triage"}
    # A role was created under the capabilities path with the boundary.
    assert iam.created and iam.created[0]["Path"] == "/sdlc-agents/capabilities/"
    assert iam.created[0]["PermissionsBoundary"].endswith("runtime-boundary-test")
    # Runtime created with the merged env (base gates + capability env).
    assert agentcore.created
    env = agentcore.created[0]["environmentVariables"]
    assert env["BEDROCK_GUARDRAIL_ID"] == "gr-1"
    assert env["GATEWAY_MCP_URL"] == "https://gw.example/mcp"
    assert env["ASANA_PROJECT_GID"] == "123"
    # Marked active + registry republished.
    assert ("triage", cd.config_store.CAP_ACTIVE, "ready") in calls["status"]
    assert calls["published"] == 1
    # The ARN written to the registry is the one the SERVICE returned, not a
    # locally re-synthesized string.
    assert calls["deploy"]
    _agent, dkw = calls["deploy"][0]
    assert dkw["runtime_arn"] == agentcore._arn("new-rt-id")


def test_registry_publish_failure_keeps_capability_active(monkeypatch):
    """A transient SSM publish error must NOT flip an already-active, READY
    runtime back to failed — publish happens after the active commit."""
    cd = _fresh()
    agentcore = _FakeAgentCore(statuses={"new-rt-id": ["READY"]})
    iam = _FakeIam()
    calls = _install_fakes(cd, monkeypatch, agentcore, iam)
    monkeypatch.setattr(cd.config_store, "get_capability", lambda a: {"agent_id": a, "env": {}})

    def _boom():
        raise RuntimeError("ssm throttled")
    monkeypatch.setattr(cd.config_store, "publish_registry", _boom)

    out = cd.handler(_build_event("triage", "build-1", "SUCCEEDED"))
    assert out == {"ok": True, "agent_id": "triage"}
    # Still active; never marked failed despite the publish error.
    assert any(s[1] == cd.config_store.CAP_ACTIVE for s in calls["status"])
    assert not any(s[1] == cd.config_store.CAP_FAILED for s in calls["status"])


def test_missing_required_base_env_fails_fast_without_runtime(monkeypatch):
    """A missing guardrail/gateway env is a deploy misconfiguration — fail the
    capability before creating any role or runtime, with a clear reason."""
    cd = _fresh()
    agentcore = _FakeAgentCore()
    iam = _FakeIam()
    calls = _install_fakes(cd, monkeypatch, agentcore, iam)
    monkeypatch.setattr(cd.config_store, "get_capability", lambda a: {"agent_id": a, "env": {}})
    monkeypatch.delenv("GATEWAY_MCP_URL", raising=False)

    cd.deploy_capability("triage", "build-1")
    # No role or runtime mutation; marked failed with a misconfiguration reason.
    assert not agentcore.created and not agentcore.updated and not iam.created
    failed = [s for s in calls["status"] if s[1] == cd.config_store.CAP_FAILED]
    assert failed and "GATEWAY_MCP_URL" in failed[0][2]
    assert calls["published"] == 0


def _fleet_arn(rt_id):
    return f"arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/{rt_id}"


def test_new_runtime_is_tagged_as_fleet(monkeypatch):
    cd = _fresh()
    agentcore = _FakeAgentCore(statuses={"new-rt-id": ["READY"]})
    iam = _FakeIam()
    _install_fakes(cd, monkeypatch, agentcore, iam)
    monkeypatch.setattr(cd.config_store, "get_capability", lambda a: {"agent_id": a, "env": {}})

    cd.handler(_build_event("triage", "build-1", "SUCCEEDED"))
    assert agentcore.created[0]["tags"] == {"sdlc-fleet": "capability-test"}


def test_existing_fleet_runtime_is_updated_not_created(monkeypatch):
    cd = _fresh()
    agentcore = _FakeAgentCore(
        existing={"triage": "rt-existing"},
        statuses={"rt-existing": ["READY"]},
        tags={_fleet_arn("rt-existing"): {"sdlc-fleet": "capability-test"}},
    )
    iam = _FakeIam(existing_roles={"triage-agentcore-runtime"})
    _install_fakes(cd, monkeypatch, agentcore, iam)
    monkeypatch.setattr(cd.config_store, "get_capability", lambda a: {"agent_id": a, "env": {}})

    cd.handler(_build_event("triage", "build-2", "SUCCEEDED"))
    assert agentcore.updated and not agentcore.created
    assert agentcore.updated[0]["agentRuntimeId"] == "rt-existing"


def test_name_collision_with_foreign_runtime_is_refused(monkeypatch):
    """A same-named runtime WITHOUT the fleet tag must not be overwritten."""
    cd = _fresh()
    agentcore = _FakeAgentCore(
        existing={"triage": "rt-foreign"},
        statuses={"rt-foreign": ["READY"]},
        tags={},  # no fleet tag → foreign
    )
    iam = _FakeIam(existing_roles={"triage-agentcore-runtime"})
    calls = _install_fakes(cd, monkeypatch, agentcore, iam)
    monkeypatch.setattr(cd.config_store, "get_capability", lambda a: {"agent_id": a, "env": {}})

    cd.handler(_build_event("triage", "build-x", "SUCCEEDED"))
    # Refused: neither updated nor created; capability marked failed.
    assert not agentcore.updated and not agentcore.created
    assert any(s[1] == cd.config_store.CAP_FAILED for s in calls["status"])
    assert not any(s[1] == cd.config_store.CAP_ACTIVE for s in calls["status"])


def test_invalid_image_tag_is_ignored(monkeypatch):
    cd = _fresh()
    agentcore = _FakeAgentCore()
    iam = _FakeIam()
    _install_fakes(cd, monkeypatch, agentcore, iam)
    out = cd.handler(_build_event("triage", "evil@sha256:deadbeef", "SUCCEEDED"))
    assert out["ok"] is False
    assert not agentcore.created and not agentcore.updated


def test_failed_build_marks_failed_and_touches_no_runtime(monkeypatch):
    cd = _fresh()
    agentcore = _FakeAgentCore()
    iam = _FakeIam()
    calls = _install_fakes(cd, monkeypatch, agentcore, iam)
    monkeypatch.setattr(cd.config_store, "get_capability", lambda a: {"agent_id": a})

    out = cd.handler(_build_event("triage", "build-3", "FAILED"))
    assert out["ok"] is False and out["build_status"] == "FAILED"
    # Never created/updated a runtime or a role on a failed build.
    assert not agentcore.created and not agentcore.updated and not iam.created
    assert any(s[1] == cd.config_store.CAP_FAILED for s in calls["status"])


def test_runtime_never_ready_marks_failed_without_teardown(monkeypatch):
    cd = _fresh()
    # Existing working FLEET runtime; a rebuild's update never goes READY.
    agentcore = _FakeAgentCore(
        existing={"triage": "rt-existing"},
        statuses={"rt-existing": ["UPDATE_FAILED"]},
        tags={_fleet_arn("rt-existing"): {"sdlc-fleet": "capability-test"}},
    )
    iam = _FakeIam(existing_roles={"triage-agentcore-runtime"})
    calls = _install_fakes(cd, monkeypatch, agentcore, iam)
    monkeypatch.setattr(cd.config_store, "get_capability", lambda a: {"agent_id": a, "env": {}})

    cd.handler(_build_event("triage", "build-4", "SUCCEEDED"))
    # Marked failed, but we never issued a delete — the working runtime stands.
    assert any(s[1] == cd.config_store.CAP_FAILED for s in calls["status"])
    assert not hasattr(agentcore, "deleted")
    # Not marked active, registry not republished.
    assert not any(s[1] == cd.config_store.CAP_ACTIVE for s in calls["status"])
    assert calls["published"] == 0


def test_event_without_valid_agent_name_ignored(monkeypatch):
    cd = _fresh()
    out = cd.handler({"detail": {"build-status": "SUCCEEDED",
                                 "additional-information": {"environment": {"environment-variables": []}}}})
    assert out["ok"] is False


def test_missing_capability_row_is_a_noop(monkeypatch):
    cd = _fresh()
    agentcore = _FakeAgentCore()
    iam = _FakeIam()
    calls = _install_fakes(cd, monkeypatch, agentcore, iam)
    monkeypatch.setattr(cd.config_store, "get_capability", lambda a: None)

    cd.deploy_capability("ghost", "build-5")
    assert not agentcore.created and not iam.created and calls["published"] == 0
