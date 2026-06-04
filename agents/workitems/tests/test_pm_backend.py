"""Tests for the selectable PM backend (PM_BACKEND=asana|github).

Covers project_config.build_project_context() and prompts.get_system_prompt()
— the two pieces that branch on the backend. The agent.py wiring (which MCP
clients connect) is exercised indirectly: these guarantee the github path reads
no Asana env and the asana path is unchanged.

project_config reads env at call time, and PM_BACKEND is captured at import, so
each test imports the module fresh under a patched environment.
"""

import importlib
import sys
from pathlib import Path

import pytest

WORKITEMS_DIR = Path(__file__).resolve().parents[1]
# agents/workitems on sys.path so `import project_config` / `import prompts` work
sys.path.insert(0, str(WORKITEMS_DIR))


def _fresh(module_name: str):
    """Import (or reimport) a module so module-level env reads re-run.

    Every agent has its own `project_config`/`prompts`/`tools`, so a sibling
    agent's test (e.g. researcher's) may leave its copy cached in sys.modules.
    Evict the cached module AND force WORKITEMS_DIR to the front of sys.path on
    every call so the reimport resolves THIS agent's copy regardless of which
    agent's test ran first. (Without this the suite passed only by alphabetical
    collection luck — running researcher's test first made workitems reimport
    researcher's prompts/project_config.)
    """
    if module_name in sys.modules:
        del sys.modules[module_name]
    sys.path.insert(0, str(WORKITEMS_DIR))
    return importlib.import_module(module_name)


@pytest.fixture(autouse=True)
def _clean_pm_env(monkeypatch):
    """Clear every PM-related env var before each test; tests set what they need."""
    for var in (
        "PM_BACKEND",
        "ASANA_PROJECT_GID",
        "ASANA_WORKSPACE_GID",
        "ASANA_PROJECT_NAME",
        "GITHUB_REPO",
        "GITHUB_PROJECT_NUMBER",
        "GITHUB_PROJECT_OWNER",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# --- project_config ----------------------------------------------------------


def test_default_backend_is_asana(monkeypatch):
    monkeypatch.setenv("ASANA_PROJECT_GID", "111")
    monkeypatch.setenv("ASANA_WORKSPACE_GID", "222")
    monkeypatch.setenv("GITHUB_REPO", "acme/web")
    pc = _fresh("project_config")
    assert pc.PM_BACKEND == "asana"


def test_github_backend_builds_context_without_any_asana_env(monkeypatch):
    # The whole point: GitHub-only deployments have NO Asana vars set.
    monkeypatch.setenv("PM_BACKEND", "github")
    monkeypatch.setenv("GITHUB_REPO", "acme/web")
    monkeypatch.setenv("GITHUB_PROJECT_NUMBER", "7")
    pc = _fresh("project_config")
    ctx = pc.build_project_context()
    assert "project #7" in ctx
    assert "acme/web" in ctx
    assert "Asana" not in ctx  # no asana leakage


def test_github_backend_does_not_crash_at_import_without_asana(monkeypatch):
    # Regression: the old project_config read ASANA_* at import and crashed.
    monkeypatch.setenv("PM_BACKEND", "github")
    monkeypatch.setenv("GITHUB_REPO", "acme/web")
    monkeypatch.setenv("GITHUB_PROJECT_NUMBER", "7")
    pc = _fresh("project_config")  # must not raise
    assert pc.PM_BACKEND == "github"


def test_github_backend_custom_project_owner(monkeypatch):
    monkeypatch.setenv("PM_BACKEND", "github")
    monkeypatch.setenv("GITHUB_REPO", "acme/web")
    monkeypatch.setenv("GITHUB_PROJECT_NUMBER", "7")
    monkeypatch.setenv("GITHUB_PROJECT_OWNER", "acme-org")
    pc = _fresh("project_config")
    ctx = pc.build_project_context()
    assert 'owned by "acme-org"' in ctx


def test_github_backend_defaults_project_owner_to_repo_owner(monkeypatch):
    monkeypatch.setenv("PM_BACKEND", "github")
    monkeypatch.setenv("GITHUB_REPO", "acme/web")
    monkeypatch.setenv("GITHUB_PROJECT_NUMBER", "7")
    pc = _fresh("project_config")
    ctx = pc.build_project_context()
    assert 'owned by "acme"' in ctx


def test_github_backend_missing_project_number_raises_clear_error(monkeypatch):
    monkeypatch.setenv("PM_BACKEND", "github")
    monkeypatch.setenv("GITHUB_REPO", "acme/web")
    pc = _fresh("project_config")
    with pytest.raises(RuntimeError, match="GITHUB_PROJECT_NUMBER"):
        pc.build_project_context()


def test_asana_backend_builds_context(monkeypatch):
    monkeypatch.setenv("PM_BACKEND", "asana")
    monkeypatch.setenv("ASANA_PROJECT_GID", "111")
    monkeypatch.setenv("ASANA_WORKSPACE_GID", "222")
    monkeypatch.setenv("GITHUB_REPO", "acme/web")
    pc = _fresh("project_config")
    ctx = pc.build_project_context()
    assert "Project GID: 111" in ctx
    assert "Workspace GID: 222" in ctx
    assert "acme/web" in ctx


def test_asana_backend_missing_workspace_raises_clear_error(monkeypatch):
    monkeypatch.setenv("PM_BACKEND", "asana")
    monkeypatch.setenv("ASANA_PROJECT_GID", "111")
    monkeypatch.setenv("GITHUB_REPO", "acme/web")
    pc = _fresh("project_config")
    with pytest.raises(RuntimeError, match="ASANA_WORKSPACE_GID"):
        pc.build_project_context()


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("PM_BACKEND", "trello")
    pc = _fresh("project_config")
    with pytest.raises(RuntimeError, match="Unknown PM_BACKEND"):
        pc.build_project_context()


# --- prompts -----------------------------------------------------------------


def test_get_system_prompt_github_variant():
    prompts = _fresh("prompts")
    gh = prompts.get_system_prompt("github")
    assert "Projects V2 board" in gh
    # shared rules interpolated, project_context placeholder preserved for agent.py
    assert "HARD RULE: Agent triggers" in gh
    assert "{shared_rules}" not in gh
    assert "{project_context}" in gh


def test_get_system_prompt_asana_variant():
    prompts = _fresh("prompts")
    az = prompts.get_system_prompt("asana")
    assert "bridges Asana" in az
    assert "HARD RULE: Agent triggers" in az
    assert "{shared_rules}" not in az
    assert "{project_context}" in az


def test_get_system_prompt_defaults_to_asana_for_unknown():
    prompts = _fresh("prompts")
    assert prompts.get_system_prompt("trello") == prompts.get_system_prompt("asana")


def test_shared_rules_present_in_both_variants():
    prompts = _fresh("prompts")
    for backend in ("asana", "github"):
        p = prompts.get_system_prompt(backend)
        assert "NEVER close issues, merge PRs, or delete tasks" in p
        assert "[Workitems Agent]" in p
