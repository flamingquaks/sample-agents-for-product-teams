"""Tests for the multi-repo allowlist guard (fleet_config + check_repo_allowed).

Which GitHub repos may dispatch is now runtime config an admin sets in the
dashboard (fleet-config table), not a baked FLEET_GITHUB_REPO binding. A repo is
dispatchable iff it is onboarded, enabled, and active; when the fleet is
restricted it must additionally be multi_repo_eligible. Non-GitHub sources carry
no repo and always pass. Everything fails closed.

These drive the real fleet_config reader against an in-process DynamoDB (moto),
then check_repo_allowed on the router on top of it.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REGION = "us-west-2"
TABLE = "fleet-config-test"


def _make_table():
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
    )


def _put_repo(repo, *, enabled=True, eligible=True, status="active"):
    boto3.resource("dynamodb", region_name=REGION).Table(TABLE).put_item(
        Item={
            "pk": f"repo#{repo.casefold()}",
            "kind": "repo",
            "repo": repo,
            "enabled": enabled,
            "multi_repo_eligible": eligible,
            "status": status,
        }
    )


def _put_settings(*, restrict_repos):
    boto3.resource("dynamodb", region_name=REGION).Table(TABLE).put_item(
        Item={"pk": "settings", "kind": "settings", "restrict_repos": restrict_repos}
    )


@pytest.fixture
def modules(monkeypatch):
    """fleet_config bound to a fresh moto table, plus the router on top of it."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("FLEET_CONFIG_TABLE", TABLE)
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-test")
    monkeypatch.setenv("ASSIGNMENTS_TABLE", "t")
    with mock_aws():
        _make_table()
        # fleet_config talks to real (moto) DynamoDB; router's other clients are
        # patched out (it isn't exercised beyond check_repo_allowed here).
        for name in ("fleet_config", "router", "guardrail", "reply"):
            sys.modules.pop(name, None)
        import fleet_config

        with patch("router.boto3.resource"), patch("router.boto3.client"):
            # router imports fleet_config; keep the real one bound to moto.
            import router

        fleet_config.reset_cache()
        yield router, fleet_config
        fleet_config.reset_cache()


# --- onboarded/enabled/active gate -------------------------------------------


def test_onboarded_active_repo_allowed(modules):
    router, _ = modules
    _put_repo("acme/web")
    assert router.check_repo_allowed("github", {"repo": "acme/web"}) is True


def test_non_onboarded_repo_rejected(modules):
    router, _ = modules
    _put_repo("acme/web")
    assert router.check_repo_allowed("github", {"repo": "acme/other"}) is False


def test_match_is_case_insensitive(modules):
    router, _ = modules
    _put_repo("acme/web")
    assert router.check_repo_allowed("github", {"repo": "ACME/Web"}) is True


def test_disabled_repo_rejected(modules):
    router, fc = modules
    _put_repo("acme/web", enabled=False)
    fc.reset_cache()
    assert router.check_repo_allowed("github", {"repo": "acme/web"}) is False


def test_pending_repo_rejected(modules):
    # A row whose Gateway policy sync hasn't landed is not yet dispatchable —
    # dispatch never widens ahead of the tool-call policy.
    router, fc = modules
    _put_repo("acme/web", status="pending")
    fc.reset_cache()
    assert router.check_repo_allowed("github", {"repo": "acme/web"}) is False


def test_github_without_repo_rejected(modules):
    router, _ = modules
    assert router.check_repo_allowed("github", {}) is False


# --- restrict-to-allowlist (multi_repo_eligible) -----------------------------


def test_unrestricted_allows_enabled_ineligible_repo(modules):
    # restrict off → any enabled+active repo passes, eligibility ignored.
    router, fc = modules
    _put_settings(restrict_repos=False)
    _put_repo("acme/web", eligible=False)
    fc.reset_cache()
    assert router.check_repo_allowed("github", {"repo": "acme/web"}) is True


def test_restricted_rejects_ineligible_repo(modules):
    router, fc = modules
    _put_settings(restrict_repos=True)
    _put_repo("acme/web", eligible=False)
    fc.reset_cache()
    assert router.check_repo_allowed("github", {"repo": "acme/web"}) is False


def test_restricted_allows_eligible_repo(modules):
    router, fc = modules
    _put_settings(restrict_repos=True)
    _put_repo("acme/web", eligible=True)
    fc.reset_cache()
    assert router.check_repo_allowed("github", {"repo": "acme/web"}) is True


# --- non-GitHub sources always pass ------------------------------------------


def test_non_github_sources_pass(modules):
    router, _ = modules
    assert router.check_repo_allowed("asana", {"task_gid": "1"}) is True
    assert router.check_repo_allowed("slack", {"channel_id": "C1"}) is True


# --- TTL cache refresh --------------------------------------------------------


def test_cache_refreshes_after_ttl(modules):
    router, fc = modules
    # Not onboarded yet → rejected, and the (empty) snapshot is cached.
    assert router.check_repo_allowed("github", {"repo": "acme/web"}) is False
    _put_repo("acme/web")
    # Within the TTL the stale snapshot still says no...
    assert router.check_repo_allowed("github", {"repo": "acme/web"}) is False
    # ...past the TTL a reload picks up the onboarding.
    later = fc.time.time() + fc._CACHE_TTL_SECONDS + 1
    with patch.object(fc.time, "time", return_value=later):
        assert router.check_repo_allowed("github", {"repo": "acme/web"}) is True
