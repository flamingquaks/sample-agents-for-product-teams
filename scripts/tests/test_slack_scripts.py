"""Unit tests for the pure logic in the Slack scripts: the Slack app-manifest
builder. AWS I/O (SSM writes) is covered by real runs; this covers the manifest
logic that must be correct regardless.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bootstrap_slack  # noqa: E402


# --- bootstrap_slack.build_manifest ------------------------------------------


def test_manifest_urls_and_scopes():
    m = bootstrap_slack.build_manifest("https://x.example.com/dev/")
    settings = m["settings"]["event_subscriptions"]
    assert settings["request_url"] == "https://x.example.com/dev/slack/events"
    assert settings["bot_events"] == ["app_mention"]
    scopes = m["oauth_config"]["scopes"]["bot"]
    assert "app_mentions:read" in scopes and "chat:write" in scopes and "commands" in scopes
    cmds = {c["command"]: c["url"] for c in m["features"]["slash_commands"]}
    assert cmds["/onboard-channel"] == "https://x.example.com/dev/slack/commands"
    assert cmds["/fleet"] == "https://x.example.com/dev/slack/commands"


def test_manifest_trailing_slash_normalized():
    m1 = bootstrap_slack.build_manifest("https://x/dev")
    m2 = bootstrap_slack.build_manifest("https://x/dev/")
    assert m1 == m2
