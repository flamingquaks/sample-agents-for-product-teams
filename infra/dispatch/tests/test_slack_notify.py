"""Tests for slack_notify.py — the `/sdlc-notify` modal build + submit parse and
the onboarded-repos scope read (spec §18.2). Runs against moto.

Focus: the repo multi-select round-trip. Slack caps an option ``value`` at 75
chars, so the modal encodes each repo as its list INDEX and resolves it back on
submit via the repo list stashed in ``private_metadata`` — a repo full_name over
75 chars must still round-trip. Also covers onboarded_repos() reading the
kind-index.
"""

import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REGION = "us-west-2"
TABLE = "fleet-config-test"
os.environ["AWS_DEFAULT_REGION"] = REGION
os.environ["FLEET_CONFIG_TABLE"] = TABLE


def _make_table():
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
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


@pytest.fixture
def sn():
    with mock_aws():
        _make_table()
        import config_query
        import slack_notify as sn_mod

        config_query._table = None
        yield sn_mod


def _put_repo(repo, enabled=True):
    boto3.resource("dynamodb", region_name=REGION).Table(TABLE).put_item(
        Item={"pk": f"repo#{repo}", "kind": "repo", "repo": repo, "enabled": enabled}
    )


def _submission_view(modal, selected_indices):
    """Build a fake view_submission ``view`` from a built modal, selecting the
    repo options at ``selected_indices`` (mirrors Slack's state shape)."""
    repo_opts = None
    for b in modal["blocks"]:
        if b.get("block_id") == "repos":
            repo_opts = b["element"]["options"]
    selected = [repo_opts[i] for i in selected_indices]
    return {
        "private_metadata": modal["private_metadata"],
        "state": {"values": {"repos": {"selected": {"selected_options": selected}}}},
    }


def test_onboarded_repos_reads_enabled_only(sn):
    _put_repo("acme/web")
    _put_repo("acme/api")
    _put_repo("acme/legacy", enabled=False)
    assert sn.onboarded_repos() == ["acme/api", "acme/web"]


def test_option_values_are_short_indices(sn):
    long_repo = "acme/" + "x" * 90  # full_name well over Slack's 75-char cap
    modal = sn.build_notify_modal(
        team_id="T1", channel_id="C1", channel_name="eng", repos=["acme/web", long_repo]
    )
    repo_block = next(b for b in modal["blocks"] if b.get("block_id") == "repos")
    for opt in repo_block["element"]["options"]:
        assert len(opt["value"]) <= 75  # the fix: value is an index, never the repo


def test_long_repo_round_trips_through_submit(sn):
    long_repo = "acme/" + "y" * 90
    modal = sn.build_notify_modal(
        team_id="T1", channel_id="C1", channel_name="eng", repos=["acme/web", long_repo]
    )
    # Select the >75-char repo (index 1).
    view = _submission_view(modal, [1])
    parsed = sn.parse_view_submission(view)
    assert parsed["repos"] == [long_repo]  # resolved back to the full name
    assert parsed["team_id"] == "T1" and parsed["channel_id"] == "C1"


def test_out_of_range_index_ignored(sn):
    modal = sn.build_notify_modal(
        team_id="T1", channel_id="C1", channel_name="eng", repos=["acme/web"]
    )
    # A tampered submit with a bogus option value must not raise or resolve.
    view = {
        "private_metadata": modal["private_metadata"],
        "state": {
            "values": {"repos": {"selected": {"selected_options": [{"value": "99"}]}}}
        },
    }
    parsed = sn.parse_view_submission(view)
    assert parsed["repos"] == []


def test_save_subscription_bounds_repos_to_onboarded(sn):
    _put_repo("acme/web")
    sn.save_subscription(
        {"team_id": "T1", "channel_id": "C1", "repos": ["acme/web", "evil/repo"], "tiers": {}}
    )
    row = boto3.resource("dynamodb", region_name=REGION).Table(TABLE).get_item(
        Key={"pk": "notif_sub#T1#C1"}
    )["Item"]
    assert row["repos"] == ["acme/web"]  # evil/repo dropped — not onboarded
