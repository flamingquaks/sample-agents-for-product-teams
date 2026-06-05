"""Unit tests for the shared PM-backend scaffold (shared/project_config.py).

The scaffold was previously exercised only indirectly via the two agents'
test_pm_backend.py; this covers require_env and the backend dispatcher directly,
so the shared module is self-covered like its siblings (bedrock, slack_post,
post_results, dispatch_context).

Imported by its package-qualified name `shared.project_config` (NOT the bare
`project_config`, which collides with each agent's own project_config module).
PM_BACKEND is read at import, so tests reimport under a patched env.
"""

import importlib
import sys
from pathlib import Path

import pytest

# agents/ on sys.path so `import shared.project_config` resolves the shared pkg.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _load(monkeypatch, backend=None):
    """Reimport shared.project_config under the given PM_BACKEND."""
    if backend is None:
        monkeypatch.delenv("PM_BACKEND", raising=False)
    else:
        monkeypatch.setenv("PM_BACKEND", backend)
    sys.modules.pop("shared.project_config", None)
    return importlib.import_module("shared.project_config")


def test_default_backend_is_asana(monkeypatch):
    pc = _load(monkeypatch, None)
    assert pc.PM_BACKEND == "asana"


def test_require_env_returns_value(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "v1")
    pc = _load(monkeypatch, "asana")
    assert pc.require_env("SOME_VAR", "workitems") == "v1"


def test_require_env_raises_clear_backend_scoped_error(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    pc = _load(monkeypatch, "github")
    with pytest.raises(RuntimeError, match=r"PM_BACKEND='github' requires the MISSING_VAR"):
        pc.require_env("MISSING_VAR", "researcher")
    try:
        pc.require_env("MISSING_VAR", "researcher")
    except RuntimeError as e:
        assert "researcher runtime" in str(e)


def test_dispatch_calls_only_selected_builder(monkeypatch):
    pc = _load(monkeypatch, "github")
    calls = []
    pc.build_project_context(
        asana_builder=lambda: calls.append("asana") or "ASANA",
        github_builder=lambda: calls.append("github") or "GITHUB",
    )
    assert calls == ["github"]  # asana builder must NOT run in github mode


def test_dispatch_asana_mode(monkeypatch):
    pc = _load(monkeypatch, "asana")
    out = pc.build_project_context(lambda: "ASANA-CTX", lambda: "GH-CTX")
    assert out == "ASANA-CTX"


def test_unknown_backend_raises(monkeypatch):
    pc = _load(monkeypatch, "trello")
    with pytest.raises(RuntimeError, match="Unknown PM_BACKEND"):
        pc.build_project_context(lambda: "a", lambda: "g")
