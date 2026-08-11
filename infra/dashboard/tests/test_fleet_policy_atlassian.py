"""Tests for the Atlassian additions to fleet_policy — tool classification, the
grantable catalog, and the sdlc_allowed_projects/spaces container forbids
(docs/specs/atlassian-connector-spec.md §B2.3/§C2.3)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fleet_policy as fp  # noqa: E402

GATEWAY = 'arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/fleet-abc'


def test_classify_jira_and_confluence_tools():
    assert fp.classify_tool("JiraTarget___get_issue") == fp.CLASS_READ
    assert fp.classify_tool("JiraTarget___transition_issue") == fp.CLASS_WRITE
    assert fp.classify_tool("ConfluenceTarget___get_page") == fp.CLASS_READ
    assert fp.classify_tool("ConfluenceTarget___update_page") == fp.CLASS_WRITE
    # A delete-shaped name is unknown (unclassified → not grantable).
    assert fp.classify_tool("JiraTarget___delete_issue") is None


def test_tool_catalog_includes_atlassian():
    actions = {t["action_id"] for t in fp.tool_catalog()}
    assert "JiraTarget___create_issue" in actions
    assert "ConfluenceTarget___create_page" in actions
    # Destructive/unknown never in the catalog.
    assert "JiraTarget___delete_issue" not in actions


def test_builtin_grants_read_everything_write_least_privilege():
    g = fp.AGENT_TOOL_GRANTS
    # workitems owns Jira ticket state.
    assert "JiraTarget___transition_issue" in g["workitems"]
    # docwriter owns the Confluence doc surface.
    assert "ConfluenceTarget___create_page" in g["docwriter"]
    # adr comments on Jira but cannot transition, and labels pages but can't edit.
    assert "JiraTarget___transition_issue" not in g["adr"]
    assert "ConfluenceTarget___update_page" not in g["adr"]
    assert "ConfluenceTarget___add_label" in g["adr"]
    # researcher is read-only everywhere.
    assert not any(
        fp.classify_tool(a) == fp.CLASS_WRITE
        for a in g["researcher"]
        if a.startswith(("JiraTarget", "ConfluenceTarget"))
    )


def test_container_policies_empty_render_false():
    pols = fp.render_container_policies([], [], GATEWAY)
    assert "unless {\n  false\n}" in pols["sdlc_allowed_projects"]
    assert "unless {\n  false\n}" in pols["sdlc_allowed_spaces"]
    # The forbid names only WRITE tools, never reads.
    assert "JiraTarget___get_issue" not in pols["sdlc_allowed_projects"]
    assert "JiraTarget___transition_issue" in pols["sdlc_allowed_projects"]


def test_container_policies_render_allowlist():
    pols = fp.render_container_policies(["ENG", "OPS"], ["DOCS"], GATEWAY)
    assert 'context.input.project_key in ["ENG", "OPS"]' in pols["sdlc_allowed_projects"]
    assert 'context.input.space_key in ["DOCS"]' in pols["sdlc_allowed_spaces"]
    assert GATEWAY in pols["sdlc_allowed_projects"]
