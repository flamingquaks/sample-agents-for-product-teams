"""Tests for the weekly security rebuilder (capability_rebuilder.py).

Asserts it starts a build for every ACTIVE capability (and only those), passes the
right AGENT_NAME override, marks them building, and never raises when one build
fails to start.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("FLEET_CONFIG_TABLE", "fleet-config-test")


def _fresh():
    for m in ("capability_rebuilder", "config_store"):
        sys.modules.pop(m, None)
    import capability_rebuilder as rb

    return rb


class _FakeCodeBuild:
    def __init__(self, fail_for=()):
        self.started = []
        self._fail_for = set(fail_for)

    def start_build(self, projectName, environmentVariablesOverride):
        overrides = {v["name"]: v["value"] for v in environmentVariablesOverride}
        agent = overrides["AGENT_NAME"]
        if agent in self._fail_for:
            raise RuntimeError("boom")
        self.started.append(overrides)
        return {"build": {"id": f"b-{agent}"}}


def _install(rb, monkeypatch, caps, codebuild):
    os.environ["CAPABILITY_BUILD_PROJECT"] = "sdlc-agent-builder-test"
    monkeypatch.setattr(rb.config_store, "list_capabilities", lambda **kw: caps)
    status_calls = []
    monkeypatch.setattr(rb.config_store, "set_capability_status",
                        lambda a, s, detail="": status_calls.append((a, s)))
    monkeypatch.setattr(rb.boto3, "client", lambda name, *a, **k: codebuild)
    return status_calls


def test_rebuilds_only_active_capabilities(monkeypatch):
    rb = _fresh()
    caps = [
        {"agent_id": "triage", "status": rb.config_store.CAP_ACTIVE},
        {"agent_id": "docwriter", "status": rb.config_store.CAP_ACTIVE},
        {"agent_id": "half", "status": rb.config_store.CAP_BUILDING},
        {"agent_id": "broken", "status": rb.config_store.CAP_FAILED},
        {"agent_id": "off", "status": rb.config_store.CAP_DISABLED},
    ]
    cb = _FakeCodeBuild()
    status_calls = _install(rb, monkeypatch, caps, cb)

    out = rb.handler({"time": "2026-07-26T08:00:00Z"}, None)
    assert out["rebuilt"] == 2 and out["skipped"] == 3
    built = sorted(o["AGENT_NAME"] for o in cb.started)
    assert built == ["docwriter", "triage"]
    # Each rebuilt agent got a distinct, weekly-stamped tag.
    tags = {o["AGENT_NAME"]: o["IMAGE_TAG"] for o in cb.started}
    assert tags["triage"].startswith("weekly-") and tags["triage"].endswith("triage")
    # Marked building.
    assert ("triage", rb.config_store.CAP_BUILDING) in status_calls


def test_one_failed_start_does_not_abort_the_rest(monkeypatch):
    rb = _fresh()
    caps = [
        {"agent_id": "triage", "status": rb.config_store.CAP_ACTIVE},
        {"agent_id": "docwriter", "status": rb.config_store.CAP_ACTIVE},
    ]
    cb = _FakeCodeBuild(fail_for={"triage"})
    _install(rb, monkeypatch, caps, cb)

    out = rb.handler({"time": "2026-07-26T08:00:00Z"}, None)
    # docwriter still rebuilt despite triage's StartBuild throwing.
    assert out["rebuilt"] == 1 and out["skipped"] == 1
    assert [o["AGENT_NAME"] for o in cb.started] == ["docwriter"]


def test_no_build_project_is_a_noop(monkeypatch):
    rb = _fresh()
    os.environ.pop("CAPABILITY_BUILD_PROJECT", None)
    monkeypatch.setattr(rb.config_store, "list_capabilities",
                        lambda **kw: [{"agent_id": "triage", "status": rb.config_store.CAP_ACTIVE}])
    out = rb.handler({}, None)
    assert out["rebuilt"] == 0


def test_pending_review_active_capability_is_skipped(monkeypatch):
    """An edit that adds a novel dep to an active, approved agent re-parks it
    pending_review but leaves status=active. The weekly rebuild MUST skip it —
    rebuilding reads the live row and would pip-install the unapproved dep,
    bypassing the second-admin gate (§7.5)."""
    rb = _fresh()
    caps = [
        {"agent_id": "triage", "status": rb.config_store.CAP_ACTIVE,
         "review_status": "approved"},
        {"agent_id": "sneaky", "status": rb.config_store.CAP_ACTIVE,
         "review_status": "pending_review"},
    ]
    cb = _FakeCodeBuild()
    _install(rb, monkeypatch, caps, cb)
    out = rb.handler({"time": "2026-07-26T08:00:00Z"}, None)
    assert out["rebuilt"] == 1 and out["skipped"] == 1
    assert [o["AGENT_NAME"] for o in cb.started] == ["triage"]
