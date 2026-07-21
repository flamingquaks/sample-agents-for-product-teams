"""Tests for identity.py — the dispatch-side cross-source identity resolver
(spec §16). Runs against moto.

Covers get-or-create on first touch, progressive enrichment (backfill), the
onboarding gate (pending ⇒ not usable), auto-merge on an email match, per-source
handle keying (incl. per-workspace Slack), and the user-request writer.
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
def identity():
    with mock_aws():
        _make_table()
        import identity as identity_mod

        identity_mod._table = None
        identity_mod.reset_cache()
        yield identity_mod
        identity_mod._table = None
        identity_mod.reset_cache()


def test_first_touch_creates_pending_unusable(identity):
    person = identity.resolve(source="github", handle="jane-gh")
    assert person.created is True
    assert person.status == "pending"
    assert person.usable is False
    assert person.identity_id  # a uuid was minted
    assert person.email == ""  # GitHub first touch may carry no email


def test_second_touch_same_handle_is_hit_not_create(identity):
    first = identity.resolve(source="github", handle="jane-gh")
    identity.reset_cache()
    second = identity.resolve(source="github", handle="jane-gh")
    assert second.created is False
    assert second.identity_id == first.identity_id


def test_enrichment_backfills_email_and_slack_handle(identity):
    first = identity.resolve(source="github", handle="jane-gh")
    identity.reset_cache()
    # Later a VERIFIED email surfaces (e.g. from Slack users.info) → enriched.
    identity.resolve(
        source="github", handle="jane-gh", email="jane@acme.com", email_verified=True
    )
    identity.reset_cache()
    got = identity.find_by_handle("github", "jane-gh")
    assert got.email == "jane@acme.com"
    assert got.identity_id == first.identity_id


def test_unverified_email_is_not_stored(identity):
    # An email NOT asserted as verified (e.g. a user-editable profile field) must
    # never be written to the record — else it could later be matched to join a
    # caller onto someone else's identity (T-42).
    identity.resolve(source="github", handle="jane-gh", email="jane@acme.com")
    identity.reset_cache()
    got = identity.find_by_handle("github", "jane-gh")
    assert got.email == ""


def test_unverified_email_does_not_join_to_another_identity(identity):
    # Victim owns jane@acme.com (verified). An attacker with a NEW handle supplies
    # the victim's email UNVERIFIED — it must not attach the attacker onto the
    # victim's (active, grouped) identity.
    victim = identity.resolve(
        source="slack", handle="U1", workspace="T0ACME", email="jane@acme.com", email_verified=True
    )
    identity.reset_cache()
    attacker = identity.resolve(source="github", handle="mallory-gh", email="jane@acme.com")
    assert attacker.identity_id != victim.identity_id  # a fresh, separate record
    assert attacker.email == ""  # the forged email was dropped


def test_slack_handle_is_per_workspace(identity):
    p1 = identity.resolve(source="slack", handle="U1", workspace="T0ACME")
    identity.reset_cache()
    # Same uid string in a DIFFERENT workspace is a different person key.
    p2 = identity.resolve(source="slack", handle="U1", workspace="T0OTHER")
    assert p1.identity_id != p2.identity_id
    identity.reset_cache()
    assert identity.slack_handle_for(p1.identity_id, "T0ACME") == "U1"
    assert identity.slack_handle_for(p1.identity_id, "T0OTHER") is None


def test_auto_merge_on_email_match(identity):
    # GitHub-first, email-less.
    gh = identity.resolve(source="github", handle="jane-gh")
    identity.reset_cache()
    # Slack-first with a VERIFIED email → new record.
    sl = identity.resolve(
        source="slack", handle="U1", workspace="T0ACME", email="jane@acme.com", email_verified=True
    )
    assert sl.identity_id != gh.identity_id
    identity.reset_cache()
    # Now a GitHub touch reveals the SAME verified email → merge the two records.
    merged = identity.resolve(
        source="github", handle="jane-gh", email="jane@acme.com", email_verified=True
    )
    assert merged.identity_id == gh.identity_id  # kept the handle record
    # The golden email must survive the merge onto the (email-less) survivor,
    # not be dropped — otherwise every future email join / grant would break.
    assert merged.email == "jane@acme.com"
    identity.reset_cache()
    # Both handles now resolve to the one surviving identity.
    by_gh = identity.find_by_handle("github", "jane-gh")
    by_sl = identity.find_by_handle("slack", "U1", "T0ACME")
    assert by_gh.identity_id == by_sl.identity_id == gh.identity_id
    assert by_gh.email == "jane@acme.com"
    # The email join still resolves to the survivor (a brand-new handle carrying
    # only the verified email lands on the merged identity, not a fresh record).
    joined = identity.resolve(
        source="asana", handle="jane-asana", email="jane@acme.com", email_verified=True
    )
    assert joined.identity_id == gh.identity_id
    identity.reset_cache()
    # The dropped record is gone.
    assert identity.find_by_handle("slack", "U1", "T0ACME").identity_id == gh.identity_id


def test_active_identity_is_usable(identity):
    person = identity.resolve(source="github", handle="jane-gh")
    # Promote it (as an admin onboarding would).
    boto3.resource("dynamodb", region_name=REGION).Table(TABLE).update_item(
        Key={"pk": f"identity#{person.identity_id}"},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "active"},
    )
    identity.reset_cache()
    again = identity.resolve(source="github", handle="jane-gh")
    assert again.status == "active"
    assert again.usable is True


def test_unresolvable_source_fails_closed(identity):
    person = identity.resolve(source="bogus", handle="x")
    assert person.identity_id == ""
    assert person.usable is False


def test_ensure_user_request_is_idempotent_per_identity(identity):
    person = identity.resolve(source="github", handle="jane-gh")
    identity.ensure_user_request(
        identity_id=person.identity_id, source="github", source_context={"repo": "acme/web"}
    )
    identity.ensure_user_request(
        identity_id=person.identity_id, source="github", source_context={"repo": "acme/web"}
    )
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    rows = table.scan(
        FilterExpression="kind = :k",
        ExpressionAttributeValues={":k": "user_request"},
    )["Items"]
    assert len(rows) == 1  # request-once
    assert rows[0]["identity_id"] == person.identity_id
    assert rows[0]["status"] == "pending"
