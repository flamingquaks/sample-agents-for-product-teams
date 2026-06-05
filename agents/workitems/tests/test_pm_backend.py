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
AGENTS_DIR = WORKITEMS_DIR.parent
# agents/workitems for `import project_config`/`prompts`; agents/ for `shared.*`
# (project_config now imports from shared.project_config).
for _p in (str(AGENTS_DIR), str(WORKITEMS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _fresh(module_name: str):
    """Import (or reimport) a module so module-level env reads re-run.

    Two things must happen on every call:
    1. Evict the agent's own module AND the shared.* modules it imports —
       shared.project_config / shared.prompts read PM_BACKEND at THEIR import,
       so a stale cache would ignore this test's patched PM_BACKEND.
    2. Force WORKITEMS_DIR to the front of sys.path so the reimport resolves
       THIS agent's project_config/prompts, not a sibling agent's (every agent
       has its own). Without (2) the suite passed only by alphabetical luck.
    """
    for cached in (module_name, "shared.project_config", "shared.prompts"):
        sys.modules.pop(cached, None)
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


_CTX = "<<PROJECT-CTX-SENTINEL>>"


def test_get_system_prompt_github_variant():
    prompts = _fresh("prompts")
    gh = prompts.get_system_prompt("github", _CTX)
    assert "Projects V2 board" in gh
    assert "HARD RULE: Agent triggers" in gh  # shared rules spliced in
    # Both placeholders are now fully substituted (no .format downstream).
    assert "{shared_rules}" not in gh
    assert "{project_context}" not in gh
    assert _CTX in gh  # project_context was substituted


def test_get_system_prompt_asana_variant():
    prompts = _fresh("prompts")
    az = prompts.get_system_prompt("asana", _CTX)
    assert "bridges Asana" in az
    assert "HARD RULE: Agent triggers" in az
    assert "{shared_rules}" not in az
    assert "{project_context}" not in az
    assert _CTX in az


def test_get_system_prompt_defaults_to_asana_for_unknown():
    prompts = _fresh("prompts")
    assert prompts.get_system_prompt("trello", _CTX) == prompts.get_system_prompt("asana", _CTX)


def test_shared_rules_present_in_both_variants():
    prompts = _fresh("prompts")
    for backend in ("asana", "github"):
        p = prompts.get_system_prompt(backend, _CTX)
        assert "NEVER close issues, merge PRs, or delete tasks" in p
        assert "[Workitems Agent]" in p
