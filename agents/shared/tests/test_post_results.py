"""Unit tests for the shared post_results tool.

post_results used to be copied into each agent (workitems/docwriter/researcher),
policed by a cross-copy "consistency" test. It now lives once at
agents/shared/tools/post_results.py, so this is a normal single-source unit test
of the platform-routing instructions it returns. The routed tool names
(add_comment / add_issue_comment / slack_post_message) are load-bearing — they
must match what the MCP servers / slack_post tool actually expose.
"""

import sys
from pathlib import Path

# agents/shared on sys.path so `from tools.post_results import ...` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.post_results import post_results  # noqa: E402


def test_asana_routes_to_add_comment():
    out = post_results(platform="asana", target_id="999", message="done")
    assert "add_comment" in out
    assert "999" in out
    assert "done" in out


def test_github_routes_to_add_issue_comment():
    out = post_results(platform="github", target_id="42", message="done")
    assert "add_issue_comment" in out
    assert "42" in out


def test_slack_routes_to_slack_post_message_no_thread():
    out = post_results(platform="slack", target_id="C123", message="hello world")
    assert "slack_post_message" in out
    assert "C123" in out
    assert "hello world" in out


def test_slack_includes_thread_when_provided():
    out = post_results(platform="slack", target_id="C123", message="hi", thread_id="170.5")
    assert "slack_post_message" in out
    assert "170.5" in out


def test_message_is_always_included():
    for platform, target in (("asana", "1"), ("github", "2"), ("slack", "C3")):
        out = post_results(platform=platform, target_id=target, message="THE-MESSAGE")
        assert "THE-MESSAGE" in out, f"{platform} dropped the message"


def test_unknown_platform_falls_back_to_github_instruction():
    # The tool should not crash on an unexpected platform; it falls back to a
    # GitHub-comment instruction rather than returning nothing.
    out = post_results(platform="bogus", target_id="x", message="m")
    assert out  # non-empty
    assert "m" in out
