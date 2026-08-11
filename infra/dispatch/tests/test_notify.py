"""Tests for notify.py — the fleet notification fan-out (spec §18).

Runs against moto. Covers subscription matching (tier/event/repo + severity
floor), threading (first post vs. follow-up), and identity-resolved mentions on
actionable/error tiers only.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REGION = "us-west-2"
CONFIG_TABLE = "fleet-config-test"
ASSIGN_TABLE = "dispatch-assignments-test"
os.environ["AWS_DEFAULT_REGION"] = REGION
os.environ["FLEET_CONFIG_TABLE"] = CONFIG_TABLE
os.environ["ASSIGNMENTS_TABLE"] = ASSIGN_TABLE


def _make_tables():
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName=CONFIG_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "kind", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "kind-index",
                "KeySchema": [
                    {"AttributeName": "kind", "KeyType": "HASH"},
                    {"AttributeName": "pk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    ddb.create_table(
        TableName=ASSIGN_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "assignment_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "assignment_id", "KeyType": "HASH"}],
    )


def _sub(table, *, team="T0ACME01", channel="C0ENG001", repos=None, tiers=None, min_severity="informative"):
    table.put_item(Item={
        "pk": f"notif_sub#{team}#{channel}",
        "kind": "notif_sub",
        "team_id": team,
        "channel_id": channel,
        "repos": repos or [],
        "tiers": tiers or {},
        "min_severity": min_severity,
    })


@pytest.fixture
def notify_mod():
    with mock_aws():
        _make_tables()
        import notify

        notify._table = None
        notify._assignments = None
        notify.reset_cache()
        yield notify
        notify._table = None
        notify._assignments = None
        notify.reset_cache()


def test_matches_and_posts_to_subscribed_channel(notify_mod):
    cfg = boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE)
    _sub(cfg, tiers={"error": ["run_failed"]}, min_severity="error")
    with patch("reply.post_slack_message_ts", return_value=(True, "111.222")) as post:
        n = notify_mod.notify(tier="error", event="run_failed", text="boom")
    assert n == 1
    post.assert_called_once()


def test_severity_floor_skips_lower_tier(notify_mod):
    cfg = boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE)
    _sub(cfg, tiers={"informative": ["run_started"], "error": ["run_failed"]}, min_severity="error")
    with patch("reply.post_slack_message_ts", return_value=(True, "1")) as post:
        n = notify_mod.notify(tier="informative", event="run_started", text="fyi")
    assert n == 0  # informative is below the 'error' floor
    post.assert_not_called()


def test_repo_scope_gates(notify_mod):
    cfg = boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE)
    _sub(cfg, repos=["acme/web"], tiers={"error": ["run_failed"]}, min_severity="error")
    with patch("reply.post_slack_message_ts", return_value=(True, "1")) as post:
        # An event for a repo NOT in the subscription's scope is skipped.
        assert notify_mod.notify(tier="error", event="run_failed", text="x", repo="other/repo") == 0
        # …but the subscribed repo matches.
        assert notify_mod.notify(tier="error", event="run_failed", text="x", repo="acme/web") == 1


def test_threading_remembers_first_ts(notify_mod):
    cfg = boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE)
    _sub(cfg, tiers={"informative": ["run_started", "run_completed"]})
    with patch("reply.post_slack_message_ts", return_value=(True, "111.222")) as post:
        notify_mod.notify(tier="informative", event="run_started", text="start", unit="assign-1")
        # First post had no thread_ts (new thread).
        assert post.call_args.kwargs["thread_ts"] is None
    notify_mod.reset_cache()
    with patch("reply.post_slack_message_ts", return_value=(True, "333.444")) as post2:
        notify_mod.notify(tier="informative", event="run_completed", text="done", unit="assign-1")
        # Follow-up threads under the remembered parent ts.
        assert post2.call_args.kwargs["thread_ts"] == "111.222"


def test_mention_only_on_actionable_error(notify_mod):
    cfg = boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE)
    _sub(cfg, tiers={"informative": ["run_started"], "error": ["run_failed"]})
    # Seed an identity with a slack handle in this workspace.
    cfg.put_item(Item={
        "pk": "identity#id1", "kind": "identity", "identity_id": "id1",
        "email": "jane@acme.com", "handles": {"github": "jane-gh", "slack": {"T0ACME01": "U9"}},
        "handle_keys": ["github:jane-gh", "slack:T0ACME01:U9"], "groups": [], "status": "active",
    })
    import identity as identity_mod
    identity_mod._table = None
    identity_mod.reset_cache()
    actor = {"source": "github", "handle": "jane-gh", "workspace": ""}
    with patch("reply.post_slack_message_ts", return_value=(True, "1")) as post:
        notify_mod.notify(tier="error", event="run_failed", text="boom", actor=actor)
        assert "<@U9>" in post.call_args.kwargs["body"]  # error tier mentions
    with patch("reply.post_slack_message_ts", return_value=(True, "1")) as post2:
        notify_mod.notify(tier="informative", event="run_started", text="fyi", actor=actor)
        assert "<@U9>" not in post2.call_args.kwargs["body"]  # informative does not
    identity_mod._table = None
    identity_mod.reset_cache()


# --- unit_for / dashboard_run_url (Slack↔dashboard traceability) --------------


def test_unit_for_keys_slack_threads_on_conversation(notify_mod):
    """A D8 follow-up is a NEW assignment in the SAME Slack thread — both must
    produce the same unit so ops-channel notifications share one thread."""
    ctx = {"workspace": "T1", "channel_id": "C1", "thread_ts": "111.2"}
    assert notify_mod.unit_for("a-1", ctx) == notify_mod.unit_for("a-2", ctx)
    assert notify_mod.unit_for("a-1", ctx) == "thread:T1#C1#111.2"


def test_unit_for_falls_back_to_assignment(notify_mod):
    assert notify_mod.unit_for("a-1", {"repo": "acme/web"}) == "a-1"
    assert notify_mod.unit_for("a-1", None) == "a-1"
    # Partial Slack context (no thread) also falls back.
    assert notify_mod.unit_for("a-1", {"workspace": "T1", "channel_id": "C1"}) == "a-1"


def test_dashboard_run_url(notify_mod, monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://d123.cloudfront.net/")
    assert (
        notify_mod.dashboard_run_url("a-1")
        == "https://d123.cloudfront.net/#/run/a-1"
    )
    monkeypatch.delenv("DASHBOARD_URL")
    assert notify_mod.dashboard_run_url("a-1") == ""
    monkeypatch.setenv("DASHBOARD_URL", "https://d123.cloudfront.net/")
    assert notify_mod.dashboard_run_url("") == ""


# --- Atlassian: per-user DM notifications (§A9.2) + container scopes ----------

def test_subscription_wants_project_scope():
    import notify
    sub = {"min_severity": "informative", "tiers": {"informative": ["automation_fired"]},
           "projects": ["ENG"]}
    assert notify._subscription_wants(sub, tier="informative", event="automation_fired",
                                      repo="", project="ENG") is True
    assert notify._subscription_wants(sub, tier="informative", event="automation_fired",
                                      repo="", project="OPS") is False


def test_subscription_wants_space_scope():
    import notify
    sub = {"min_severity": "informative", "tiers": {"informative": ["page_published"]},
           "spaces": ["DOCS"]}
    assert notify._subscription_wants(sub, tier="informative", event="page_published",
                                      repo="", space="DOCS") is True
    assert notify._subscription_wants(sub, tier="informative", event="page_published",
                                      repo="", space="SECRET") is False


def test_notify_user_requires_optin(monkeypatch):
    import notify
    notify.reset_prefs_cache()
    # No pref row → no DM.
    monkeypatch.setattr(notify, "_prefs_snapshot", lambda now=None: [])
    assert notify.notify_user("id-1", tier="actionable", event="agent_replied",
                              text="hi", team_id="T1") is False


def test_notify_user_silent_degrade_without_handle(monkeypatch):
    import notify
    monkeypatch.setattr(notify, "_prefs_snapshot", lambda now=None: [
        {"identity_id": "id-1", "min_tier": "actionable",
         "tiers": {"actionable": ["agent_replied"]}}])
    # Opted in, but no slack handle in this workspace → degrade (False), no post.
    monkeypatch.setattr(notify.identity_map, "slack_handle_for", lambda iid, team: None)
    posted = []
    monkeypatch.setattr(notify.reply, "post_slack_message_ts",
                        lambda **k: posted.append(1) or (True, "1"))
    assert notify.notify_user("id-1", tier="actionable", event="agent_replied",
                              text="hi", team_id="T1") is False
    assert posted == []


def test_notify_user_dms_when_opted_in(monkeypatch):
    import notify
    monkeypatch.setattr(notify, "_prefs_snapshot", lambda now=None: [
        {"identity_id": "id-1", "min_tier": "actionable",
         "tiers": {"actionable": ["agent_replied"]}}])
    monkeypatch.setattr(notify.identity_map, "slack_handle_for", lambda iid, team: "U9")
    monkeypatch.setattr(notify, "_open_im", lambda team, uid: "D123")
    sent = {}
    def fake_post(**k):
        sent.update(k)
        return True, "ts1"
    monkeypatch.setattr(notify.reply, "post_slack_message_ts", fake_post)
    assert notify.notify_user("id-1", tier="actionable", event="agent_replied",
                              text="you were mentioned", team_id="T1") is True
    assert sent["channel"] == "D123" and "mentioned" in sent["body"]
