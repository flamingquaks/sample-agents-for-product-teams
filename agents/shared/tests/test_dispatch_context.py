"""Tests for shared/dispatch_context.py — the Slack→codebase bridge block."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dispatch_context import slack_dispatch_block  # noqa: E402


def test_empty_context_renders_nothing():
    assert slack_dispatch_block(None) == ""
    assert slack_dispatch_block({}) == ""


def test_repos_render_with_primary_and_guidance():
    ctx = {"workspace": "T1", "channel_id": "C1", "sender_name": "alice",
           "repo": "acme/api", "repos": ["acme/web", "acme/api"]}
    block = slack_dispatch_block(ctx)
    assert "## Current Dispatch" in block
    assert "- acme/api (primary)" in block
    assert "- acme/web" in block
    assert "use your GitHub tools against these repositories" in block.replace("\n", " ")
    assert "NOT" in block  # unreachable-repo warning present


def test_origin_missing_from_repos_is_prepended():
    ctx = {"repo": "acme/api", "repos": ["acme/web"]}
    block = slack_dispatch_block(ctx)
    assert "- acme/api (primary)" in block and "- acme/web" in block


def test_no_repos_says_tools_will_refuse():
    ctx = {"workspace": "T1", "channel_id": "C1"}
    block = slack_dispatch_block(ctx)
    assert "No repositories are approved" in block
    assert "GitHub tool calls" in block


def test_reply_delivery_is_automatic():
    block = slack_dispatch_block({"workspace": "T1", "channel_id": "C1"})
    assert "delivered there automatically" in block


# --- Atlassian dispatch blocks (atlassian-connector spec §B1.3/§C1.3) --------

from dispatch_context import confluence_dispatch_block, jira_dispatch_block  # noqa: E402


def test_jira_block_renders_issue_and_reply_rule():
    ctx = {"workspace": "site-1", "project_key": "ENG", "issue_key": "ENG-142",
           "issue_summary": "Fix login", "issue_status": "In Progress",
           "repos": ["acme/web"], "comment_history": "[bob at t]:\nplease look"}
    block = jira_dispatch_block(ctx)
    assert "Source: jira" in block
    assert "ENG-142" in block and "Fix login" in block
    assert "JiraTarget add_comment" in block
    assert "NEVER transition an issue to Done/Closed without explicit human approval" in block
    assert "- acme/web (primary)" in block
    assert "Comment history" in block and "please look" in block


def test_jira_block_empty_is_blank():
    assert jira_dispatch_block(None) == ""


def test_confluence_block_propose_mode_and_inline_selection():
    ctx = {"workspace": "site-1", "space_key": "DOCS", "page_id": "5",
           "page_title": "Runbook", "page_version": 3, "write_mode": "propose",
           "inline_selection": "the deploy step", "repos": []}
    block = confluence_dispatch_block(ctx)
    assert "Source: confluence" in block
    assert "Runbook" in block and "version 3" in block
    assert "PROPOSE mode" in block and "add_comment" in block
    assert "Inline selection" in block and "the deploy step" in block
    assert "No repositories are linked" in block


def test_confluence_block_direct_mode_says_get_page_first():
    ctx = {"workspace": "s", "space_key": "DOCS", "page_id": "5", "write_mode": "direct"}
    block = confluence_dispatch_block(ctx)
    assert "DIRECT mode" in block
    assert "get_page first" in block and "base_version" in block
