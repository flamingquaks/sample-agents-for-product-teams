"""Tests for the fleet Cedar policy generator (fleet_policy)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fleet_policy  # noqa: E402


def _reset_pair_mode():
    fleet_policy.REPO_PARAM_MODE = "pair"


def test_empty_allowlist_forbids_all_writes():
    _reset_pair_mode()
    stmt = fleet_policy.render_fleet_policy([])
    # With nothing allowed, the unless-clause is `false` → every write is forbidden.
    assert "unless {\n  false\n}" in stmt
    assert stmt.startswith("forbid(")
    # Write actions are enumerated with the target prefix + triple underscore.
    assert 'AgentCore::Action::"GitHubTarget___create_issue"' in stmt


def test_pair_mode_conditions_on_owner_and_repo():
    _reset_pair_mode()
    stmt = fleet_policy.render_fleet_policy(["acme/web"])
    assert '(context.input.owner == "acme" && context.input.repo == "web")' in stmt
    assert "context.input has owner && context.input has repo" in stmt


def test_pair_mode_disjoins_multiple_repos():
    _reset_pair_mode()
    stmt = fleet_policy.render_fleet_policy(["acme/web", "acme/api"])
    assert '(context.input.owner == "acme" && context.input.repo == "web")' in stmt
    assert '(context.input.owner == "acme" && context.input.repo == "api")' in stmt
    assert " ||\n" in stmt


def test_malformed_repo_skipped_falls_back_to_forbid_all():
    _reset_pair_mode()
    # A single malformed entry yields no valid pairs → forbid-all (`false`).
    stmt = fleet_policy.render_fleet_policy(["noslash"])
    assert "unless {\n  false\n}" in stmt


def test_single_mode_uses_repo_in_list():
    try:
        fleet_policy.REPO_PARAM_MODE = "single"
        stmt = fleet_policy.render_fleet_policy(["acme/web", "acme/api"])
        assert 'context.input.repo in ["acme/web", "acme/api"]' in stmt
        assert "context.input has repo" in stmt
    finally:
        _reset_pair_mode()


def test_statement_within_cedar_length_bounds():
    _reset_pair_mode()
    # AWS::BedrockAgentCore::Policy Cedar Statement must be 35..10000 chars.
    stmt = fleet_policy.render_fleet_policy(["acme/web"])
    assert 35 <= len(stmt) <= 10000


def test_deterministic_output():
    _reset_pair_mode()
    a = fleet_policy.render_fleet_policy(["acme/web", "acme/api"])
    b = fleet_policy.render_fleet_policy(["acme/web", "acme/api"])
    assert a == b
