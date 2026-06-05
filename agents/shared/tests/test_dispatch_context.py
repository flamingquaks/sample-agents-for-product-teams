"""Tests for the shared dispatch-context builder.

This replaces the per-agent ad-hoc coverage of the dispatch block. The key
regression guard is that the `project_item` github case is selected BEFORE the
generic github case (the dead-branch bug that shipped when this logic was
copy-pasted into each agent).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dispatch_context import build_dispatch_context  # noqa: E402


def test_empty_context_returns_blank():
    assert build_dispatch_context("github", {}) == ""
    assert build_dispatch_context("github", None) == ""
    assert build_dispatch_context("unknown", {"x": 1}) == ""


def test_asana():
    out = build_dispatch_context("asana", {"task_gid": "123", "task_name": "Do X"})
    assert "Source: asana" in out
    assert "Task GID: 123" in out
    assert "Reply to: Asana task 123" in out


def test_github_issue():
    out = build_dispatch_context("github", {
        "repo": "acme/web", "issue_number": "42", "issue_title": "T", "issue_body": "B",
    })
    assert "Source: github" in out
    assert "Issue: #42" in out
    assert "Reply to: GitHub issue #42 on acme/web" in out


def test_github_pr_with_labels_and_state():
    # docwriter-style PR review payload.
    out = build_dispatch_context("github", {
        "repo": "acme/web", "pr_number": "9", "is_pr": "true",
        "issue_title": "Add API", "issue_body": "body",
        "issue_labels": "feature,api", "issue_state": "open",
    })
    assert "PR: #9" in out
    assert "Labels: feature,api" in out
    assert "State: open" in out
    assert "Reply to: GitHub PR #9 on acme/web" in out


def test_github_pr_field_fallbacks():
    # adr-style payload uses pr_title / pr_body / comments instead of issue_*.
    out = build_dispatch_context("github", {
        "repo": "acme/web", "pr_number": "5",
        "pr_title": "Refactor", "pr_body": "the body", "comments": "c1\nc2",
    })
    assert "Title: Refactor" in out
    assert "the body" in out
    assert "c1" in out and "c2" in out


def test_project_item_wins_over_generic_github():
    # THE regression guard: a project_item event must render the board block,
    # not the generic issue block, regardless of dict order.
    out = build_dispatch_context("github", {
        "trigger_type": "project_item",
        "repo": "acme/web", "project_number": "7", "item_id": "PVTI_x",
        "issue_number": "42", "status_change": "Todo → In Progress",
    })
    assert "Projects V2 board" in out
    assert "Board item: PVTI_x" in out
    assert "Linked issue: #42" in out
    assert "Todo → In Progress" in out
    # must NOT have fallen through to the generic issue renderer
    assert "Issue: #42" not in out


def test_project_item_without_issue_number_degrades():
    out = build_dispatch_context("github", {
        "trigger_type": "project_item",
        "repo": "", "project_number": "7", "item_id": "PVTI_x",
        "issue_number": "", "content_node_id": "I_abc", "status_change": "→ Done",
    })
    assert "Projects V2 board" in out
    assert "Linked content node: I_abc" in out
    assert "project status update on project #7" in out
    assert "board spans repos" in out  # empty repo rendered as the placeholder


def test_slack_with_and_without_thread():
    with_thread = build_dispatch_context("slack", {"channel_id": "C1", "thread_ts": "170.5"})
    assert "Source: slack" in with_thread
    assert "Reply to: Slack channel C1 thread 170.5" in with_thread

    no_thread = build_dispatch_context("slack", {"channel_id": "C1"})
    assert "Reply to: Slack channel C1\n" in no_thread
    assert "thread" not in no_thread.split("Reply to:")[1]
