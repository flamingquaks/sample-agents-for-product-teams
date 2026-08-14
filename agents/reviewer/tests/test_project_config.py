"""Tests for Reviewer project_config — multi-repo (dispatch-repo) behavior.

The fleet is multi-repo: build_project_context takes the repo from the dispatch
(source_context.repo), not a baked env var. These pin that the context targets
the dispatched repo when present, defers to the Current Dispatch block when
absent, and always advertises the config defaults so a missing review.yaml never
silences the reviewer.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import project_config as pc  # noqa: E402


def test_context_targets_dispatched_repo():
    out = pc.build_project_context("acme/widgets")
    assert "acme/widgets" in out
    assert "Owner: acme" in out
    assert "Repo name: widgets" in out


def test_context_defers_to_dispatch_block_when_absent():
    out = pc.build_project_context(None)
    assert "taken from the Current Dispatch section" in out
    assert "Operate ONLY on that repository" in out


def test_context_advertises_config_defaults():
    out = pc.build_project_context("acme/widgets")
    # Defaults must be visible so a repo with no review.yaml still reviews.
    assert pc.REVIEW_CONFIG_PATH in out
    assert f"min_severity={pc.DEFAULT_MIN_SEVERITY}" in out
    assert f"max_findings={pc.DEFAULT_MAX_FINDINGS}" in out
    assert "NEVER silences the review" in out


def test_defaults_are_sane():
    assert pc.DEFAULT_MIN_SEVERITY in pc.SEVERITY_ORDER
    assert pc.DEFAULT_REVIEW_DRAFTS is False
    assert pc.DEFAULT_MAX_FINDINGS > 0
    assert "*.lock" in pc.DEFAULT_IGNORE_PATHS
