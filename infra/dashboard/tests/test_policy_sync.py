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


def _load(monkeypatch, *, engine_id=ENGINE, allowed=None):
    for m in ("policy_sync", "admin", "config_store", "fleet_policy", "http_responses"):
        sys.modules.pop(m, None)
    if engine_id is None:
        monkeypatch.delenv("POLICY_ENGINE_ID", raising=False)
    else:
        monkeypatch.setenv("POLICY_ENGINE_ID", engine_id)
    monkeypatch.setenv("FLEET_CONFIG_TABLE", "unused-in-these-tests")
    # FLEET_GATEWAY_ARN is required for the sync (Cedar needs the concrete gateway
    # resource). AWS_ACCOUNT_ID is opt-in for the per-agent permit path; default
    # tests leave it UNSET so only the two fleet forbid policies are written.
    monkeypatch.setenv(
        "FLEET_GATEWAY_ARN",
        "arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/sdlcfleetstaging-abc",
    )
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
    import config_store
    import policy_sync

    monkeypatch.setattr(config_store, "allowed_repos", lambda: allowed or ["acme/web"])
    # Atlassian container allowlists default empty (no site onboarded in these
    # tests) — the container forbids render `unless { false }` and are skipped
    # unless their target is deployed on the fake gateway.
    monkeypatch.setattr(config_store, "allowed_jira_projects", lambda: [])
    monkeypatch.setattr(config_store, "allowed_confluence_spaces", lambda: [])
    # Skip real polling latency.
    monkeypatch.setattr(policy_sync, "_POLL_SLEEP_SECONDS", 0)
    return policy_sync


class _FakeClient:
    """Minimal stand-in for the bedrock-agentcore-control client. ``targets``
    is what list_gateway_targets pages out — the sync filters agent grants to
    deployed target names, so the default exposes both fleet targets (grants
    pass through untouched)."""

    def __init__(self, *, existing=None, statuses=None, targets=("GitHubTarget", "AsanaTarget")):
        self.existing = existing  # policy list returned by list_policies
        self.statuses = list(statuses or ["ACTIVE"])
        self.targets = [{"name": t, "targetId": f"tid-{t}"} for t in targets]
        self.created = []
        self.updated = []
        self.deleted = []

    def get_paginator(self, name):
        page = (
            {"items": self.targets}
            if name == "list_gateway_targets"
            else {"policies": self.existing or []}
        )

        class _P:
            def paginate(self, **_):
                yield page

        return _P()

    def create_policy(self, **kwargs):
        self.created.append(kwargs)
        return {"policyId": "pol-new"}

    def update_policy(self, **kwargs):
        self.updated.append(kwargs)
        return {}

    def delete_policy(self, **kwargs):
        self.deleted.append(kwargs)
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
    fake = _FakeClient(existing=[], statuses=["ACTIVE"])
    monkeypatch.setattr(ps, "_get_client", lambda: fake)
    ps.sync_fleet_policy()
    # Fleet forbid policies (one Cedar statement each): allowlist +
    # non-COMMENT-review backstop. (No destructive forbid — those tools are
    # absent from the target schema, and Cedar rejects unknown actions.)
    names = [c["name"] for c in fake.created]
    assert names == [
        "sdlc_allowed_repos",
        "sdlc_forbid_noncomment_review",
    ]
    # enforcementMode is NOT sent — it lives on the gateway→engine attachment,
    # and CreatePolicy in the deployed SDK rejects the parameter.
    for c in fake.created:
        assert "enforcementMode" not in c
        assert "cedar" in c["definition"]
    assert not fake.updated


def test_updates_policy_when_present(monkeypatch):
    ps = _load(monkeypatch)
    fake = _FakeClient(
        existing=[
            {"name": "sdlc_allowed_repos", "policyId": "pol-1"},
            {"name": "sdlc_forbid_destructive", "policyId": "pol-2"},  # retired
            {"name": "sdlc_forbid_noncomment_review", "policyId": "pol-3"},
        ],
        statuses=["ACTIVE"],
    )
    monkeypatch.setattr(ps, "_get_client", lambda: fake)
    ps.sync_fleet_policy()
    updated_ids = {u["policyId"] for u in fake.updated}
    assert updated_ids == {"pol-1", "pol-3"}
    for u in fake.updated:
        assert "enforcementMode" not in u
    assert not fake.created
    # The retired destructive forbid is removed from the engine (it names tools
    # the target schema no longer exposes — a leftover would sit CREATE_FAILED).
    assert [d["policyId"] for d in fake.deleted] == ["pol-2"]


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


def test_permits_skipped_without_account(monkeypatch):
    # Default _load sets FLEET_GATEWAY_ARN but leaves AWS_ACCOUNT_ID unset → the
    # fleet forbid policies are written, but no per-agent permits.
    ps = _load(monkeypatch)
    fake = _FakeClient(existing=[], statuses=["ACTIVE"])
    monkeypatch.setattr(ps, "_get_client", lambda: fake)
    ps.sync_fleet_policy()
    assert [c["name"] for c in fake.created] == [
        "sdlc_allowed_repos",
        "sdlc_forbid_noncomment_review",
    ]


# --- data-driven tool grants (P2, spec §3.5) ---------------------------------


def test_grants_by_agent_merges_custom_over_builtins(monkeypatch):
    ps = _load(monkeypatch)
    import config_store
    import fleet_policy

    monkeypatch.setattr(config_store, "list_capabilities", lambda: [
        # Custom, enabled → its authored grants are used.
        {"agent_id": "triage", "enabled": True, "status": "active",
         "builtin": False, "tool_grants": ["GitHubTarget___get_issue"]},
        # Built-in, enabled → keeps its fixed code-defined grants (row ignored).
        {"agent_id": "workitems", "enabled": True, "status": "active",
         "builtin": True, "tool_grants": ["SHOULD_BE_IGNORED"]},
        # Disabled custom → dropped (no permit).
        {"agent_id": "old", "enabled": False, "status": "disabled",
         "builtin": False, "tool_grants": ["GitHubTarget___get_issue"]},
    ])
    grants, complete = ps._grants_by_agent()
    assert complete is True
    assert grants["triage"] == ["GitHubTarget___get_issue"]
    assert grants["workitems"] == fleet_policy.AGENT_TOOL_GRANTS["workitems"]  # fixed
    assert "old" not in grants  # disabled → no permit


def test_grants_by_agent_falls_back_on_read_error(monkeypatch):
    ps = _load(monkeypatch)
    import config_store
    import fleet_policy

    def boom():
        raise RuntimeError("table gone")

    monkeypatch.setattr(config_store, "list_capabilities", boom)
    # Degraded read → built-in defaults preserved (agents never lose permits),
    # and complete=False so the caller skips the stale-permit deletion pass.
    grants, complete = ps._grants_by_agent()
    assert grants == fleet_policy.AGENT_TOOL_GRANTS
    assert complete is False


def test_stale_agent_permit_deleted(monkeypatch):
    """A sdlc_permit_* policy on the engine whose agent no longer has a grant
    (disabled/deleted) must be DELETED by the sync — an orphaned permit would
    keep a torn-down agent's role authorized on the Gateway indefinitely.
    Fleet forbid policies and foreign policies are never touched."""
    ps = _load(monkeypatch)
    import config_store

    monkeypatch.setenv("AWS_ACCOUNT_ID", "111122223333")
    monkeypatch.setattr(config_store, "list_capabilities", lambda: [
        {"agent_id": "gone", "enabled": False, "status": "disabled",
         "builtin": False, "tool_grants": ["GitHubTarget___get_issue"]},
    ])
    fake = _FakeClient(
        existing=[
            {"name": "sdlc_allowed_repos", "policyId": "pol-1"},
            {"name": "sdlc_permit_gone", "policyId": "pol-stale"},
            {"name": "some_foreign_policy", "policyId": "pol-foreign"},
        ],
        statuses=["ACTIVE"],
    )
    monkeypatch.setattr(ps, "_get_client", lambda: fake)
    ps.sync_fleet_policy()
    deleted_ids = [d["policyId"] for d in fake.deleted]
    assert deleted_ids == ["pol-stale"]
    # Live built-in permits were still upserted.
    created_names = [c["name"] for c in fake.created]
    assert "sdlc_permit_workitems" in created_names


def test_grants_filtered_to_deployed_targets(monkeypatch):
    """A grant naming a target that isn't on the gateway (AsanaTarget while its
    OAuth provider is pending) must be OMITTED from the rendered permit — Cedar
    validation rejects unknown actions, which would fail the agent's whole
    permit. An agent whose grants ALL reference absent targets gets no permit."""
    ps = _load(monkeypatch)
    monkeypatch.setenv("AWS_ACCOUNT_ID", "111122223333")
    fake = _FakeClient(existing=[], statuses=["ACTIVE"], targets=("GitHubTarget",))
    monkeypatch.setattr(ps, "_get_client", lambda: fake)
    ps.sync_fleet_policy()
    created = {c["name"]: c for c in fake.created}
    # workitems keeps its GitHub grants but loses the Asana ones.
    stmt = created["sdlc_permit_workitems"]["definition"]["cedar"]["statement"]
    assert "GitHubTarget___get_issue" in stmt
    assert "AsanaTarget" not in stmt
    # researcher keeps its GitHub READ grants but loses the Asana ones.
    r_stmt = created["sdlc_permit_researcher"]["definition"]["cedar"]["statement"]
    assert "GitHubTarget___get_file_contents" in r_stmt
    assert "AsanaTarget" not in r_stmt
