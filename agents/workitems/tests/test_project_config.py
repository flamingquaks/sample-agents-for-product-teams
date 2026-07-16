"""Tests for Workitems project_config — multi-repo (dispatch-repo) behavior.

The fleet is multi-repo: build_project_context takes the repo from the dispatch
(source_context.repo), not a baked GITHUB_REPO env var. These pin that the
context targets the dispatched repo when present and defers to the Current
Dispatch block when absent.
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def project_config(monkeypatch):
    monkeypatch.setenv("ASANA_PROJECT_GID", "PROJ123")
    monkeypatch.setenv("ASANA_WORKSPACE_GID", "WS456")
    monkeypatch.delenv("GITHUB_REPO", raising=False)  # no longer read
    sys.modules.pop("project_config", None)
    import project_config as pc

    importlib.reload(pc)
    return pc


def test_dispatched_repo_is_pinned(project_config):
    ctx = project_config.build_project_context("acme/web")
    assert "acme/web" in ctx
    assert 'ALWAYS use repo "acme/web"' in ctx
    # owner/name split is surfaced too
    assert "Owner: acme" in ctx
    assert "Repo name: web" in ctx


def test_no_dispatched_repo_defers_to_dispatch_block(project_config):
    ctx = project_config.build_project_context(None)
    # No baked repo — the agent is told to read it from the Current Dispatch.
    assert "Current Dispatch" in ctx
    assert "Never invent or assume a different repo" in ctx


def test_asana_project_always_present(project_config):
    for repo in ("acme/web", None):
        ctx = project_config.build_project_context(repo)
        assert "PROJ123" in ctx
        assert "WS456" in ctx


def test_import_does_not_require_github_repo(project_config):
    # The module must import with GITHUB_REPO unset (multi-repo: no baked repo).
    assert not hasattr(project_config, "GITHUB_REPO")
