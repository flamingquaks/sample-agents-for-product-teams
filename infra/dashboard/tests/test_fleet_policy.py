"""Tests for the fleet Cedar policy generator (fleet_policy)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fleet_policy  # noqa: E402


GW = "arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/sdlcfleetstaging-abc123"


def _reset_pair_mode():
    fleet_policy.REPO_PARAM_MODE = "pair"


def _allow(repos):
    """The repo-allowlist forbid statement for the given repos."""
    return fleet_policy.render_fleet_policies(repos, GW)["sdlc_allowed_repos"]


def _destructive(repos=("acme/web",)):
    return fleet_policy.render_fleet_policies(list(repos), GW)["sdlc_forbid_destructive"]


def test_renders_separate_single_statement_policies():
    # AgentCore accepts one Cedar statement per policy, so the fleet forbids are
    # SEPARATE named policies — each exactly one `forbid(`, no packed set.
    _reset_pair_mode()
    policies = fleet_policy.render_fleet_policies(["acme/web"], GW)
    assert set(policies) == {
        "sdlc_allowed_repos",
        "sdlc_forbid_destructive",
        "sdlc_forbid_noncomment_review",
    }
    for stmt in policies.values():
        assert stmt.count("forbid(") == 1


def test_noncomment_review_forbidden_by_event():
    # The PR-review tool is forbidden when the review event is not COMMENT —
    # the policy-layer backstop for the broker's forced event=COMMENT.
    _reset_pair_mode()
    stmt = fleet_policy.render_fleet_policies(["acme/web"], GW)[
        "sdlc_forbid_noncomment_review"
    ]
    assert 'AgentCore::Action::"GitHubTarget___create_pull_request_review"' in stmt
    assert 'context.input.event != "COMMENT"' in stmt
    # It's a when-conditioned forbid (fires on non-COMMENT), not repo-allowlisted.
    assert "unless" not in stmt


def test_policies_pin_concrete_gateway_resource():
    # A wildcard/unconstrained resource is rejected by the engine — every policy
    # must name the literal gateway ARN.
    _reset_pair_mode()
    for stmt in fleet_policy.render_fleet_policies(["acme/web"], GW).values():
        assert f'resource == AgentCore::Gateway::"{GW}"' in stmt
        # no bare `resource\n` (unconstrained) and no wildcard.
        assert "  resource\n" not in stmt


def test_empty_allowlist_forbids_all_writes():
    _reset_pair_mode()
    stmt = _allow([])
    # With nothing allowed, the unless-clause is `false` → every write is forbidden.
    assert "unless {\n  false\n}" in stmt
    assert 'AgentCore::Action::"GitHubTarget___create_issue"' in stmt


def test_destructive_tools_unconditionally_forbidden():
    _reset_pair_mode()
    # delete_file (and merge/delete-branch) are in the SEPARATE destructive
    # forbid (no `unless`), never in the repo-allowlist forbid — so an allowlisted
    # repo can't lift them.
    allow = _allow(["acme/web"])
    dest = _destructive()
    assert "delete_file" not in allow
    assert 'AgentCore::Action::"GitHubTarget___delete_file"' in dest
    assert "unless" not in dest


def test_repo_literals_lowercased():
    _reset_pair_mode()
    stmt = _allow(["Acme/Web"])
    assert '(context.input.owner == "acme" && context.input.repo == "web")' in stmt
    assert "Acme" not in stmt and "Web" not in stmt


def test_pair_mode_conditions_on_owner_and_repo():
    _reset_pair_mode()
    stmt = _allow(["acme/web"])
    assert '(context.input.owner == "acme" && context.input.repo == "web")' in stmt
    assert "context.input has owner && context.input has repo" in stmt


def test_pair_mode_disjoins_multiple_repos():
    _reset_pair_mode()
    stmt = _allow(["acme/web", "acme/api"])
    assert '(context.input.owner == "acme" && context.input.repo == "web")' in stmt
    assert '(context.input.owner == "acme" && context.input.repo == "api")' in stmt
    assert " ||\n" in stmt


def test_malformed_repo_skipped_falls_back_to_forbid_all():
    _reset_pair_mode()
    assert "unless {\n  false\n}" in _allow(["noslash"])


def test_single_mode_uses_repo_in_list():
    try:
        fleet_policy.REPO_PARAM_MODE = "single"
        stmt = _allow(["acme/web", "acme/api"])
        assert 'context.input.repo in ["acme/web", "acme/api"]' in stmt
        assert "context.input has repo" in stmt
    finally:
        _reset_pair_mode()


def test_statement_within_cedar_length_bounds():
    _reset_pair_mode()
    # Each policy's Cedar Statement must be within 10 KB.
    for stmt in fleet_policy.render_fleet_policies(["acme/web"], GW).values():
        assert 20 <= len(stmt) <= 10000


def test_deterministic_output():
    _reset_pair_mode()
    a = fleet_policy.render_fleet_policies(["acme/web", "acme/api"], GW)
    b = fleet_policy.render_fleet_policies(["acme/web", "acme/api"], GW)
    assert a == b


# --- per-agent permit policies -----------------------------------------------

ACCT = "111122223333"
GW = "arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/sdlcFleetdev-abc123"


def test_permit_principal_is_runtime_role_arn():
    stmt = fleet_policy.render_agent_permit("workitems", ACCT, GW)
    # Principal is the agent's runtime assumed-role ARN (IamEntity), not an
    # AgentCore::Agent — that's the verified IAM-gateway principal shape.
    assert (
        'principal == AgentCore::IamEntity::'
        f'"arn:aws:sts::{ACCT}:assumed-role/workitems-agentcore-runtime"' in stmt
    )
    assert "AgentCore::Agent::" not in stmt


def test_permit_resource_is_literal_gateway_arn():
    stmt = fleet_policy.render_agent_permit("adr", ACCT, GW)
    # Resource must be the literal gateway ARN — AgentCore Cedar forbids wildcards.
    assert f'resource == AgentCore::Gateway::"{GW}"' in stmt
    assert "*" not in stmt.split("resource ==", 1)[1]


def test_permit_actions_are_gateway_shape():
    stmt = fleet_policy.render_agent_permit("workitems", ACCT, GW)
    assert 'AgentCore::Action::"GitHubTarget___create_issue"' in stmt
    assert 'AgentCore::Action::"AsanaTarget___create_task"' in stmt


def test_permit_grants_no_destructive_tools():
    # No agent's permit may name a destructive tool (the forbid would win, but
    # granting it is a red flag / mis-scope).
    destructive = set(fleet_policy.DESTRUCTIVE_TOOLS)
    for agent, actions in fleet_policy.AGENT_TOOL_GRANTS.items():
        tools = {a.split("___", 1)[1] for a in actions}
        assert not (tools & destructive), f"{agent} grants a destructive tool"


def test_adr_permit_has_no_write_beyond_labels_comments_and_review():
    # adr writes only labels, issue comments, and diff-anchored PR review
    # comments (create_pull_request_review, forced to event=COMMENT in the
    # broker). It never creates issues/PRs or writes code/files.
    tools = {a.split("___", 1)[1] for a in fleet_policy.AGENT_TOOL_GRANTS["adr"]}
    assert "create_issue" not in tools
    assert "create_pull_request" not in tools
    assert "create_or_update_file" not in tools
    assert "push_files" not in tools
    assert "create_branch" not in tools
    assert "add_issue_comment" in tools and "add_labels_to_issue" in tools
    # Mode 2/3 capability: read the PR diff/files + post an anchored review.
    assert "get_pull_request_diff" in tools
    assert "list_pull_request_files" in tools
    assert "create_pull_request_review" in tools


def test_no_orphaned_write_tools():
    # Every enforced WRITE_TOOL must be granted to at least one agent — otherwise
    # it's dead surface (implemented + forbid-gated but no principal can call it).
    # create_pull_request_review was previously orphaned; adr now owns it.
    granted = {
        a.split("___", 1)[1]
        for actions in fleet_policy.AGENT_TOOL_GRANTS.values()
        for a in actions
        if a.startswith(fleet_policy.GITHUB_TARGET + "___")
    }
    orphans = set(fleet_policy.WRITE_TOOLS) - granted
    assert not orphans, f"WRITE_TOOLS granted to no agent (dead surface): {sorted(orphans)}"


def test_researcher_permit_is_asana_only():
    actions = fleet_policy.AGENT_TOOL_GRANTS["researcher"]
    assert actions and all(a.startswith("AsanaTarget___") for a in actions)


def test_agent_permit_policies_names():
    policies = fleet_policy.agent_permit_policies(ACCT, GW)
    assert set(policies) == {
        "sdlc_permit_workitems",
        "sdlc_permit_docwriter",
        "sdlc_permit_adr",
        "sdlc_permit_researcher",
    }
    for stmt in policies.values():
        assert stmt.startswith("permit(")
        assert 35 <= len(stmt) <= 10000


def test_permit_deterministic():
    a = fleet_policy.render_agent_permit("docwriter", ACCT, GW)
    b = fleet_policy.render_agent_permit("docwriter", ACCT, GW)
    assert a == b


# --- classified tool catalog + data-driven grants (P2, spec §3.5) ------------


def test_tool_classes_are_disjoint():
    r, w, d = set(fleet_policy.READ_TOOLS), set(fleet_policy.WRITE_TOOLS), set(fleet_policy.DESTRUCTIVE_TOOLS)
    assert not (r & w) and not (r & d) and not (w & d)


def test_classify_tool_read_write_destructive():
    gh = fleet_policy.GITHUB_TARGET
    assert fleet_policy.classify_tool(f"{gh}___get_issue") == "read"
    assert fleet_policy.classify_tool(f"{gh}___create_issue") == "write"
    assert fleet_policy.classify_tool(f"{gh}___delete_branch") == "destructive"
    assert fleet_policy.classify_tool("AsanaTarget___get_task") == "read"
    assert fleet_policy.classify_tool("AsanaTarget___create_task") == "write"
    # Unknown target / tool / malformed → None (fails closed).
    assert fleet_policy.classify_tool(f"{gh}___nonexistent") is None
    assert fleet_policy.classify_tool("Unknown___get_task") is None
    assert fleet_policy.classify_tool("no-separator") is None


def test_tool_catalog_excludes_destructive():
    catalog = fleet_policy.tool_catalog()
    klasses = {t["klass"] for t in catalog}
    assert klasses == {"read", "write"}  # never destructive
    ids = {t["action_id"] for t in catalog}
    assert f"{fleet_policy.GITHUB_TARGET}___get_issue" in ids
    assert f"{fleet_policy.GITHUB_TARGET}___delete_branch" not in ids


def test_data_driven_grants_render_custom_agent():
    grants = {"triage": [f"{fleet_policy.GITHUB_TARGET}___get_issue"]}
    policies = fleet_policy.agent_permit_policies(ACCT, GW, grants)
    assert set(policies) == {"sdlc_permit_triage"}
    assert "GitHubTarget___get_issue" in policies["sdlc_permit_triage"]


def test_empty_grant_agent_omitted():
    # A custom agent that granted no tools gets no permit → default-deny.
    policies = fleet_policy.agent_permit_policies(ACCT, GW, {"triage": []})
    assert policies == {}
