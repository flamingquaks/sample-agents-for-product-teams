"""Tests for policy_sync — the runtime fleet-policy push to the policy engine.

The bedrock-agentcore-control client is faked (no AWS), so these exercise the
find-by-name → create/update → poll-until-ready flow, the no-op-when-unprovisioned
path, and PolicySyncError on failure/timeout.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ENGINE = "sdlcEngine-abcdef0123"


def _load(monkeypatch, *, engine_id=ENGINE, enforcement="LOG_ONLY", allowed=None):
    for m in ("policy_sync", "admin", "config_store", "fleet_policy", "http_responses"):
        sys.modules.pop(m, None)
    if engine_id is None:
        monkeypatch.delenv("POLICY_ENGINE_ID", raising=False)
    else:
        monkeypatch.setenv("POLICY_ENGINE_ID", engine_id)
    monkeypatch.setenv("FLEET_POLICY_ENFORCEMENT", enforcement)
    monkeypatch.setenv("FLEET_CONFIG_TABLE", "unused-in-these-tests")
    # Per-agent permit sync is opt-in on these two env vars; default tests run
    # with them UNSET (permit path skipped) so the fleet-policy assertions below
    # see exactly one create/update. test_syncs_agent_permits sets them.
    monkeypatch.delenv("FLEET_GATEWAY_ARN", raising=False)
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
    import config_store
    import policy_sync

    monkeypatch.setattr(config_store, "allowed_repos", lambda: allowed or ["acme/web"])
    # Skip real polling latency.
    monkeypatch.setattr(policy_sync, "_POLL_SLEEP_SECONDS", 0)
    return policy_sync


class _FakeClient:
    """Minimal stand-in for the bedrock-agentcore-control client."""

    def __init__(self, *, existing=None, statuses=None):
        self.existing = existing  # policy list returned by list_policies
        self.statuses = list(statuses or ["ACTIVE"])
        self.created = []
        self.updated = []

    def get_paginator(self, name):
        policies = self.existing or []

        class _P:
            def paginate(self, **_):
                yield {"policies": policies}

        return _P()

    def create_policy(self, **kwargs):
        self.created.append(kwargs)
        return {"policyId": "pol-new"}

    def update_policy(self, **kwargs):
        self.updated.append(kwargs)
        return {}

    def get_policy(self, **_):
        # Pop successive statuses; keep returning the last once exhausted.
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"status": status, "statusReasons": "boom"}


def test_noop_when_engine_unset(monkeypatch):
    ps = _load(monkeypatch, engine_id=None)
    # No client is built and nothing raises.
    ps.sync_fleet_policy()


def test_creates_policy_when_absent(monkeypatch):
    ps = _load(monkeypatch)
    fake = _FakeClient(existing=[], statuses=["CREATING", "LOG_ONLY"])
    monkeypatch.setattr(ps, "_get_client", lambda: fake)
    ps.sync_fleet_policy()
    assert len(fake.created) == 1
    assert fake.created[0]["name"] == ps.FLEET_POLICY_NAME
    assert fake.created[0]["enforcementMode"] == "LOG_ONLY"
    assert "cedar" in fake.created[0]["definition"]
    assert not fake.updated


def test_updates_policy_when_present(monkeypatch):
    ps = _load(monkeypatch, enforcement="ACTIVE")
    fake = _FakeClient(
        existing=[{"name": ps.FLEET_POLICY_NAME, "policyId": "pol-1"}],
        statuses=["UPDATING", "ACTIVE"],
    )
    monkeypatch.setattr(ps, "_get_client", lambda: fake)
    ps.sync_fleet_policy()
    assert len(fake.updated) == 1
    assert fake.updated[0]["policyId"] == "pol-1"
    assert fake.updated[0]["enforcementMode"] == "ACTIVE"
    assert not fake.created


def test_raises_on_failed_status(monkeypatch):
    ps = _load(monkeypatch)
    import admin  # PolicySyncError lives here

    fake = _FakeClient(existing=[], statuses=["CREATE_FAILED"])
    monkeypatch.setattr(ps, "_get_client", lambda: fake)
    with pytest.raises(admin.PolicySyncError):
        ps.sync_fleet_policy()


def test_raises_on_timeout(monkeypatch):
    ps = _load(monkeypatch)
    import admin

    monkeypatch.setattr(ps, "_POLL_ATTEMPTS", 3)
    fake = _FakeClient(existing=[], statuses=["CREATING"])  # never ready
    monkeypatch.setattr(ps, "_get_client", lambda: fake)
    with pytest.raises(admin.PolicySyncError):
        ps.sync_fleet_policy()


def test_control_plane_error_is_policy_sync_error(monkeypatch):
    ps = _load(monkeypatch)
    import admin
    from botocore.exceptions import ClientError

    class _Boom(_FakeClient):
        def create_policy(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ValidationException"}}, "CreatePolicy"
            )

    fake = _Boom(existing=[])
    monkeypatch.setattr(ps, "_get_client", lambda: fake)
    with pytest.raises(admin.PolicySyncError):
        ps.sync_fleet_policy()


def test_syncs_agent_permits_when_gateway_env_set(monkeypatch):
    ps = _load(monkeypatch)
    monkeypatch.setenv(
        "FLEET_GATEWAY_ARN",
        "arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/sdlcFleetdev-abc",
    )
    monkeypatch.setenv("AWS_ACCOUNT_ID", "111122223333")
    fake = _FakeClient(existing=[], statuses=["ACTIVE"])
    monkeypatch.setattr(ps, "_get_client", lambda: fake)
    ps.sync_fleet_policy()
    names = [c["name"] for c in fake.created]
    # The fleet allowlist policy + one permit per agent.
    assert ps.FLEET_POLICY_NAME in names
    assert "sdlc_permit_workitems" in names
    assert "sdlc_permit_researcher" in names
    # A permit statement carries the runtime-role principal.
    permit = next(
        c for c in fake.created if c["name"] == "sdlc_permit_workitems"
    )
    assert "assumed-role/workitems-agentcore-runtime" in (
        permit["definition"]["cedar"]["statement"]
    )


def test_permits_skipped_without_gateway_env(monkeypatch):
    # Default _load leaves FLEET_GATEWAY_ARN/AWS_ACCOUNT_ID unset → only the
    # fleet allowlist policy is written, no permits.
    ps = _load(monkeypatch)
    fake = _FakeClient(existing=[], statuses=["ACTIVE"])
    monkeypatch.setattr(ps, "_get_client", lambda: fake)
    ps.sync_fleet_policy()
    assert [c["name"] for c in fake.created] == [ps.FLEET_POLICY_NAME]
