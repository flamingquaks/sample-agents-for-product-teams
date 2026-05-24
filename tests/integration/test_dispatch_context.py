"""Unit tests for agents/shared/dispatch_context.py.

These run without any sandbox credentials — they verify the dispatch context
builder produces correct output for all sources, including the PR-aware
GitHub path and the token-omitting Discord path.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agents"))

from shared.dispatch_context import build_dispatch_context_block  # noqa: E402


class TestGitHubContext:
    """Verify _github renders correctly for issues and PRs."""

    def test_plain_issue(self):
        ctx = {
            "repo": "org/repo",
            "issue_number": "42",
            "issue_title": "Bug report",
            "issue_body": "Something broke",
            "issue_comments": "user1: can confirm",
        }
        result = build_dispatch_context_block("github", ctx)
        assert "issue: #42" in result
        assert "Repository: org/repo" in result
        assert "Title: Bug report" in result
        assert "Reply to: GitHub issue #42 on org/repo" in result
        assert "Labels:" not in result
        assert "State:" not in result

    def test_pr_via_is_pr_flag(self):
        ctx = {
            "repo": "org/repo",
            "issue_number": "99",
            "is_pr": "true",
            "issue_title": "Add feature X",
            "issue_body": "This PR adds...",
            "issue_labels": "enhancement,review-needed",
            "issue_state": "open",
            "issue_comments": "",
        }
        result = build_dispatch_context_block("github", ctx)
        assert "PR: #99" in result
        assert "Reply to: GitHub PR #99 on org/repo" in result
        assert "Labels: enhancement,review-needed" in result
        assert "State: open" in result

    def test_pr_via_pr_number(self):
        ctx = {
            "repo": "org/repo",
            "pr_number": "55",
            "issue_title": "Refactor Y",
            "issue_body": "",
            "issue_labels": "",
            "issue_state": "closed",
        }
        result = build_dispatch_context_block("github", ctx)
        assert "PR: #55" in result
        assert "Reply to: GitHub PR #55" in result

    def test_missing_optional_fields(self):
        ctx = {"repo": "org/repo"}
        result = build_dispatch_context_block("github", ctx)
        assert "issue: #unknown" in result
        assert "Title: unknown" in result


class TestSlackContext:
    """Verify _slack picks the right reply tool based on thread_ts."""

    def test_threaded(self):
        ctx = {
            "channel_id": "C123",
            "thread_ts": "1234567890.123456",
            "user_id": "U456",
        }
        result = build_dispatch_context_block("slack", ctx)
        assert "slack_post_thread" in result
        assert "thread_ts='1234567890.123456'" in result
        assert "slack_post_message" not in result

    def test_top_level(self):
        ctx = {"channel_id": "C123", "thread_ts": "", "user_id": "U456"}
        result = build_dispatch_context_block("slack", ctx)
        assert "slack_post_message" in result
        assert "slack_post_thread" not in result

    def test_missing_thread_ts(self):
        ctx = {"channel_id": "C123", "user_id": "U456"}
        result = build_dispatch_context_block("slack", ctx)
        assert "slack_post_message" in result


class TestDiscordContext:
    """Verify _discord omits interaction_token from prompt text."""

    def test_token_not_in_output(self):
        ctx = {
            "application_id": "APP123",
            "interaction_token": "secret-token-value",
            "channel_id": "CH999",
            "guild_id": "G111",
            "user_id": "U222",
        }
        result = build_dispatch_context_block("discord", ctx)
        assert "secret-token-value" not in result
        assert "Application ID: APP123" in result
        assert "Channel: CH999" in result
        assert "discord_post_followup" in result

    def test_dm_guild(self):
        ctx = {"channel_id": "CH1", "user_id": "U1"}
        result = build_dispatch_context_block("discord", ctx)
        assert "Guild: (dm)" in result


class TestAsanaContext:
    def test_basic(self):
        ctx = {
            "task_gid": "12345",
            "task_name": "Implement feature",
            "task_notes": "Details here",
            "project_name": "Sprint 5",
            "project_gid": "99999",
        }
        result = build_dispatch_context_block("asana", ctx)
        assert "Task GID: 12345" in result
        assert "Project: Sprint 5 (99999)" in result


class TestUnknownSource:
    def test_returns_empty(self):
        assert build_dispatch_context_block("jira", {"key": "val"}) == ""

    def test_none_context(self):
        assert build_dispatch_context_block("github", None) == ""

    def test_empty_context(self):
        assert build_dispatch_context_block("github", {}) == ""
