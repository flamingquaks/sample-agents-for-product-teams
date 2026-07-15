"""Tests for the single-repo guard (check_repo_allowed).

The fleet's agents bake GITHUB_REPO into their runtime, so a GitHub mention from
a different repo would feed the agent a dispatched repo that contradicts its
hardcoded one — risking work landing in the wrong repo. The Dispatch Router
rejects that mismatch up front when FLEET_GITHUB_REPO is set, and is a no-op when
it isn't (preserving prior any-repo behavior).

FLEET_GITHUB_REPO is read at import time, so each fixture sets the env and
re-imports the router module.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _import_router(monkeypatch, fleet_repo):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-test")
    monkeypatch.setenv("ASSIGNMENTS_TABLE", "t")
    if fleet_repo is None:
        monkeypatch.delenv("FLEET_GITHUB_REPO", raising=False)
    else:
        monkeypatch.setenv("FLEET_GITHUB_REPO", fleet_repo)
    with patch("boto3.resource"), patch("boto3.client"):
        for name in ("router", "guardrail", "reply"):
            sys.modules.pop(name, None)
        import router as router_mod
    return router_mod


@pytest.fixture
def bound_router(monkeypatch):
    return _import_router(monkeypatch, "acme/web")


@pytest.fixture
def unbound_router(monkeypatch):
    return _import_router(monkeypatch, None)


# --- bound: mismatch rejected, match allowed ---------------------------------


def test_bound_allows_matching_repo(bound_router):
    assert bound_router.check_repo_allowed("github", {"repo": "acme/web"}) is True


def test_bound_rejects_other_repo(bound_router):
    assert bound_router.check_repo_allowed("github", {"repo": "acme/other"}) is False


def test_bound_match_is_case_insensitive(bound_router):
    # GitHub owner/repo is case-insensitive.
    assert bound_router.check_repo_allowed("github", {"repo": "ACME/Web"}) is True


def test_bound_rejects_github_without_repo(bound_router):
    # An unverifiable GitHub dispatch (no repo) must not slip through.
    assert bound_router.check_repo_allowed("github", {}) is False


def test_bound_ignores_non_github_sources(bound_router):
    # Only GitHub carries a repo; Asana/Slack always pass the repo guard.
    assert bound_router.check_repo_allowed("asana", {"task_gid": "1"}) is True
    assert bound_router.check_repo_allowed("slack", {"channel_id": "C1"}) is True


# --- unbound: check disabled (backward compatible) ---------------------------


def test_unbound_allows_any_repo(unbound_router):
    assert (
        unbound_router.check_repo_allowed("github", {"repo": "anyone/anything"}) is True
    )
    assert unbound_router.check_repo_allowed("github", {}) is True
