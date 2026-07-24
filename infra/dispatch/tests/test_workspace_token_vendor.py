"""Tests for the workspace token vendor (workspace_token_vendor.py).

The vendor is a pure authorization funnel: repo boundary (onboarded +
co-reachable from the dispatch origin) ∩ the agent's GitHub permission tier ∩
the requested read/write level. Every denial path must fail closed with an
``{"error": ...}`` body — never a token.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import workspace_token_vendor as vendor


@pytest.fixture
def fleet(monkeypatch):
    """acme/web + acme/api are onboarded and co-grouped; acme/secret is not."""
    eligible = {"acme/web", "acme/api"}
    monkeypatch.setattr(
        vendor.fleet_config, "is_repo_cross_repo_eligible",
        lambda repo: repo.casefold() in eligible,
    )
    monkeypatch.setattr(
        vendor.fleet_config, "coreachable_repos",
        lambda origin: ["acme/web", "acme/api"] if origin.casefold() in eligible else [],
    )
    minted = []
    monkeypatch.setattr(
        vendor.github_app, "scoped_installation_token",
        lambda repo, permissions: minted.append({"repo": repo, "permissions": permissions})
        or "ghs_test",
    )
    return minted


def _event(**overrides):
    event = {"repo": "acme/web", "agent": "docwriter", "origin": "acme/web", "write": False}
    event.update(overrides)
    return event


def test_read_token_for_repo_capable_agent(fleet):
    out = vendor.handler(_event())
    assert out == {"token": "ghs_test"}
    assert fleet[0]["permissions"] == {"contents": "read", "metadata": "read"}


def test_write_token_for_docwriter(fleet):
    out = vendor.handler(_event(write=True))
    assert out == {"token": "ghs_test"}
    assert fleet[0]["permissions"] == {"contents": "write", "metadata": "read"}


def test_cross_repo_within_group_allowed(fleet):
    assert vendor.handler(_event(repo="acme/api")) == {"token": "ghs_test"}


def test_repo_outside_group_refused(fleet, monkeypatch):
    monkeypatch.setattr(
        vendor.fleet_config, "is_repo_cross_repo_eligible", lambda repo: True
    )
    out = vendor.handler(_event(repo="other/repo"))
    assert "error" in out and "not approved to run with" in out["error"]
    assert fleet == []


def test_not_onboarded_repo_refused(fleet):
    out = vendor.handler(_event(repo="acme/secret"))
    assert "error" in out and "not onboarded" in out["error"]
    assert fleet == []


def test_no_origin_refused(fleet):
    out = vendor.handler(_event(origin=""))
    assert "error" in out and "no dispatch origin" in out["error"]
    assert fleet == []


def test_workitems_has_no_contents_tier(fleet):
    """workitems' tier has no contents grant — even a read is refused."""
    out = vendor.handler(_event(agent="workitems"))
    assert "error" in out and "no repository-contents access" in out["error"]
    assert fleet == []


def test_adr_read_ok_write_refused(fleet):
    assert vendor.handler(_event(agent="adr")) == {"token": "ghs_test"}
    out = vendor.handler(_event(agent="adr", write=True))
    assert "error" in out and "may not write" in out["error"]


def test_unknown_agent_gets_default_read_tier(fleet):
    """A custom agent (no built-in tier) can clone but not push."""
    assert vendor.handler(_event(agent="my-custom-agent")) == {"token": "ghs_test"}
    out = vendor.handler(_event(agent="my-custom-agent", write=True))
    assert "error" in out


def test_malformed_repo_refused(fleet):
    out = vendor.handler(_event(repo="not-a-repo"))
    assert "error" in out
    assert fleet == []


def test_mint_failure_returns_error(fleet, monkeypatch):
    def boom(repo, permissions):
        raise vendor.github_app.GitHubAppError("no installation")

    monkeypatch.setattr(vendor.github_app, "scoped_installation_token", boom)
    out = vendor.handler(_event())
    assert "error" in out and "could not mint" in out["error"]
