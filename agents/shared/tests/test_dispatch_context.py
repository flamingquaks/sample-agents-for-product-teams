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
