"""Tests for the per-repo Bedrock Mantle project helper (mantle.py)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _fresh(monkeypatch, enabled=True, fake_client=None):
    sys.modules.pop("mantle", None)
    import mantle

    monkeypatch.setenv("STAGE", "test")
    if enabled:
        monkeypatch.setenv("SDLC_MANTLE_ENABLED", "true")
    else:
        monkeypatch.delenv("SDLC_MANTLE_ENABLED", raising=False)
    if fake_client is not None:
        monkeypatch.setattr(mantle, "_mantle", lambda: fake_client)
    return mantle


def test_project_name_is_deterministic_and_safe(monkeypatch):
    mantle = _fresh(monkeypatch)
    # slashes/dots normalized to hyphens; lowercased; stage-prefixed.
    assert mantle.project_name_for("Acme/Web.API", "staging") == "sdlc-staging-acme-web-api"


def test_disabled_returns_none_without_calling(monkeypatch):
    called = []

    class _CB:
        def create_project(self, **kw):
            called.append(kw)
            return {"id": "p1"}

    mantle = _fresh(monkeypatch, enabled=False, fake_client=_CB())
    assert mantle.ensure_project("acme/web") is None
    assert called == []


def test_enabled_creates_project_and_returns_id(monkeypatch):
    class _CB:
        def __init__(self):
            self.kw = None

        def create_project(self, **kw):
            self.kw = kw
            return {"id": "proj-123"}

    cb = _CB()
    mantle = _fresh(monkeypatch, fake_client=cb)
    assert mantle.ensure_project("acme/web") == "proj-123"
    assert cb.kw["name"] == "sdlc-test-acme-web"
    assert cb.kw["tags"]["repo"] == "acme/web"


def test_create_failure_is_best_effort_none(monkeypatch):
    class _CB:
        def create_project(self, **kw):
            raise RuntimeError("mantle down")

        def list_projects(self):
            # No matching project exists — the create failure is a real outage.
            return {"projects": []}

    mantle = _fresh(monkeypatch, fake_client=_CB())
    # Never raises — repo onboard proceeds under the default project.
    assert mantle.ensure_project("acme/web") is None


def test_duplicate_name_falls_back_to_existing_project(monkeypatch):
    """A re-onboard hits an already-existing project name; ensure_project must
    find and reuse it rather than dropping cost attribution."""

    class _Dup(Exception):
        pass

    class _CB:
        def create_project(self, **kw):
            raise _Dup("project already exists")

        def list_projects(self):
            return {
                "projects": [
                    {"name": "sdlc-test-other", "id": "proj-other"},
                    {"name": "sdlc-test-acme-web", "id": "proj-existing"},
                ]
            }

    mantle = _fresh(monkeypatch, fake_client=_CB())
    assert mantle.ensure_project("acme/web") == "proj-existing"
