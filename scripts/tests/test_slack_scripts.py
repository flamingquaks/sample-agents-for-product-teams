"""Unit tests for the pure logic in the Slack scripts: the trigger-authz
migration planner and the Slack app-manifest builder. AWS I/O (SSM/DDB writes)
is covered by real runs; these cover the logic that must be correct regardless.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bootstrap_slack  # noqa: E402
import migrate_authz  # noqa: E402


# --- migrate_authz.plan_migration --------------------------------------------


def test_plan_migration_namespaces_bare_ids():
    # Bare github/asana ids get their source prefix (to match the router
    # principal); already-namespaced slack ids pass through unchanged.
    rows = [
        {"kind": "capability", "agent_id": "workitems",
         "authorization_users": ["slack:T0ACME:U1", "octocat"]},
        {"kind": "capability", "agent_id": "docwriter", "authorization_users": ["1202334567890"]},
        {"kind": "repo", "repo": "acme/web"},  # ignored
    ]
    planned, skipped = migrate_authz.plan_migration(rows)
    pairs = {(p["agent_id"], p["subject_id"], p["connector"], p["effect"]) for p in planned}
    assert pairs == {
        ("workitems", "slack:T0ACME:U1", "slack", "permit"),
        ("workitems", "github:octocat", "github", "permit"),   # bare login namespaced
        ("docwriter", "asana:1202334567890", "asana", "permit"),  # numeric gid → asana
    }
    assert skipped == []


def test_plan_migration_skips_wildcard_open_posture():
    # Allow-all ('*') has no principal-grant equivalent — it must be SKIPPED
    # (not written as a dead literal-'*' rule that denies everyone).
    rows = [{"kind": "capability", "agent_id": "workitems", "authorization_users": ["*"]}]
    planned, skipped = migrate_authz.plan_migration(rows)
    assert planned == []
    assert skipped == [("workitems", "*")]


def test_plan_migration_is_deterministic_and_dedups():
    rows = [{"kind": "capability", "agent_id": "workitems",
             "authorization_users": ["octocat", "octocat"]}]
    p1, _ = migrate_authz.plan_migration(rows)
    p2, _ = migrate_authz.plan_migration(rows)
    assert len(p1) == 1  # de-duplicated
    assert p1[0]["pk"] == p2[0]["pk"]  # stable id → idempotent re-run


def test_plan_migration_skips_empty_and_non_capability():
    rows = [
        {"kind": "capability", "agent_id": "a", "authorization_users": ["", "  "]},
        {"kind": "capability", "authorization_users": ["x"]},  # no agent_id
        {"kind": "settings"},
    ]
    planned, skipped = migrate_authz.plan_migration(rows)
    assert planned == [] and skipped == []


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
