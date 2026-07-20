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
    def __init__(self, existing=None, statuses=None):
        self._existing = existing or {}  # name -> id
        self._statuses = statuses or {}  # id -> [status, ...]
        self.created = []
        self.updated = []

    def get_paginator(self, _name):
        rts = [{"agentRuntimeName": n, "agentRuntimeId": i} for n, i in self._existing.items()]
        outer = self

        class _P:
            def paginate(self):
                return [{"agentRuntimes": rts}]

        return _P()

    def create_agent_runtime(self, **kw):
        self.created.append(kw)
        return {"agentRuntimeId": "new-rt-id"}

    def update_agent_runtime(self, **kw):
        self.updated.append(kw)
        return {"agentRuntimeId": kw["agentRuntimeId"]}

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


def test_existing_runtime_is_updated_not_created(monkeypatch):
    cd = _fresh()
    agentcore = _FakeAgentCore(existing={"triage": "rt-existing"},
                               statuses={"rt-existing": ["READY"]})
    iam = _FakeIam(existing_roles={"triage-agentcore-runtime"})
    calls = _install_fakes(cd, monkeypatch, agentcore, iam)
    monkeypatch.setattr(cd.config_store, "get_capability", lambda a: {"agent_id": a, "env": {}})

    cd.handler(_build_event("triage", "build-2", "SUCCEEDED"))
    assert agentcore.updated and not agentcore.created
    assert agentcore.updated[0]["agentRuntimeId"] == "rt-existing"


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
    # Existing working runtime; a rebuild's update never goes READY.
    agentcore = _FakeAgentCore(existing={"triage": "rt-existing"},
                               statuses={"rt-existing": ["UPDATE_FAILED"]})
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
