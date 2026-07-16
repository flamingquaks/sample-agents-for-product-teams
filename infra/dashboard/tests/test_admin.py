"""Tests for the admin write API (admin.py) + config store, against moto.

Drives the real admin.handler and config_store against an in-process DynamoDB
so the routing, admin authz, validation, and the pending→active write ordering
are exercised without AWS.
"""

import json
import os
import sys
from pathlib import Path

import boto3
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REGION = "us-west-2"
TABLE = "fleet-config-test"
os.environ["AWS_DEFAULT_REGION"] = REGION
os.environ["FLEET_CONFIG_TABLE"] = TABLE

ADMIN = {"sub": "admin-1", "cognito:groups": "[admins]"}
OPERATOR = {"sub": "op-1", "cognito:groups": "[operators]"}


def _event(method, resource, claims=ADMIN, path=None, body=None):
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path,
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": claims}},
    }


def _make_table():
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
    )


def _load_admin():
    # Fresh import per test-run so the cached table handle binds to moto.
    for m in ("admin", "config_store", "auth", "http_responses"):
        sys.modules.pop(m, None)
    import admin

    return admin


def _body(resp):
    return json.loads(resp["body"])


# --- authz -------------------------------------------------------------------


@mock_aws
def test_operator_cannot_write():
    _make_table()
    admin = _load_admin()
    resp = admin.handler(_event("GET", "/admin/repos", claims=OPERATOR))
    assert resp["statusCode"] == 403


@mock_aws
def test_unauthenticated_denied():
    _make_table()
    admin = _load_admin()
    resp = admin.handler(
        {"httpMethod": "GET", "resource": "/admin/repos", "requestContext": {}}
    )
    assert resp["statusCode"] == 403


# --- repo lifecycle ----------------------------------------------------------


@mock_aws
def test_onboard_list_delete_repo():
    _make_table()
    admin = _load_admin()

    # onboard
    resp = admin.handler(_event("POST", "/admin/repos", body={"repo": "acme/web"}))
    assert resp["statusCode"] == 200
    rec = _body(resp)
    assert rec["repo"] == "acme/web"
    assert rec["enabled"] is True and rec["multi_repo_eligible"] is True
    # policy sync is a no-op today → row reaches active
    assert rec["status"] == "active"
    assert rec["onboarded_by"] == "admin-1"

    # list
    resp = admin.handler(_event("GET", "/admin/repos"))
    repos = _body(resp)["repos"]
    assert [r["repo"] for r in repos] == ["acme/web"]

    # delete — greedy {repo+} route carries the slash as one path param
    resp = admin.handler(
        _event("DELETE", "/admin/repos/{repo+}", path={"repo": "acme/web"})
    )
    assert resp["statusCode"] == 200 and _body(resp)["deleted"] is True
    assert _body(admin.handler(_event("GET", "/admin/repos")))["repos"] == []


@mock_aws
def test_delete_missing_repo_reports_not_deleted():
    _make_table()
    admin = _load_admin()
    # Deleting a repo that was never onboarded must NOT report deleted:true
    # (DynamoDB delete_item is idempotent; delete_repo returns ALL_OLD presence).
    resp = admin.handler(
        _event("DELETE", "/admin/repos/{repo+}", path={"repo": "ghost/repo"})
    )
    assert resp["statusCode"] == 200
    assert _body(resp)["deleted"] is False


@mock_aws
def test_delete_route_decodes_encoded_slash():
    _make_table()
    admin = _load_admin()
    admin.handler(_event("POST", "/admin/repos", body={"repo": "acme/web"}))
    # A client that percent-encoded the slash still resolves to the stored repo.
    resp = admin.handler(
        _event("DELETE", "/admin/repos/{repo+}", path={"repo": "acme%2Fweb"})
    )
    assert resp["statusCode"] == 200 and _body(resp)["deleted"] is True
    assert _body(admin.handler(_event("GET", "/admin/repos")))["repos"] == []


@mock_aws
def test_onboard_rejects_bad_repo():
    _make_table()
    admin = _load_admin()
    bad_inputs = [
        "",
        "noslash",
        "a/b/c",
        "own er/repo",
        "owner/",
        # Cedar-injection attempts: quotes, pipes, newlines must be rejected so
        # they can't reach the generated policy statement.
        'x"||true||x/web',
        "owner/re\npo",
        "ow|ner/repo",
        'owner/"web"',
    ]
    for bad in bad_inputs:
        resp = admin.handler(_event("POST", "/admin/repos", body={"repo": bad}))
        assert resp["statusCode"] == 400, bad


@mock_aws
def test_onboard_syncs_policy_including_new_repo():
    # Regression: the policy sync must see the repo being onboarded. Previously
    # the row was written 'pending' before the sync, and allowed_repos() filters
    # to 'active', so the just-onboarded repo was omitted from the synced policy.
    _make_table()
    admin = _load_admin()
    import config_store

    seen = {}

    def capture():
        seen["allowed"] = config_store.allowed_repos()

    # Replace the sync seam with a capture of what allowed_repos() returns AT
    # sync time (mirrors what policy_sync would render).
    admin._sync_repo_policy = capture
    resp = admin.handler(_event("POST", "/admin/repos", body={"repo": "acme/web"}))
    assert resp["statusCode"] == 200
    assert seen["allowed"] == ["acme/web"], seen


@mock_aws
def test_onboard_normalizes_case():
    _make_table()
    admin = _load_admin()
    import config_store

    resp = admin.handler(_event("POST", "/admin/repos", body={"repo": "Acme/Web"}))
    assert resp["statusCode"] == 200
    # Stored + returned lowercased so dispatch (casefold) and Cedar agree.
    assert _body(resp)["repo"] == "acme/web"
    assert config_store.get_repo("ACME/WEB")["repo"] == "acme/web"
    assert config_store.allowed_repos() == ["acme/web"]


@mock_aws
def test_onboard_eligible_false_is_dispatchable_not_eligible():
    _make_table()
    admin = _load_admin()
    admin.handler(
        _event(
            "POST",
            "/admin/repos",
            body={"repo": "acme/web", "multi_repo_eligible": False},
        )
    )
    import config_store

    assert config_store.get_repo("acme/web")["enabled"] is True
    # not eligible → excluded from the tool-call allowlist
    assert config_store.allowed_repos() == []


@mock_aws
def test_pending_left_on_policy_sync_failure_when_enforcing(monkeypatch):
    # ENFORCE: a sync failure must roll the repo back to pending and 502, so
    # dispatch never widens ahead of a tool-call policy that still denies.
    monkeypatch.setenv("GATEWAY_ENFORCEMENT", "ACTIVE")
    _make_table()
    admin = _load_admin()

    def boom():
        raise admin.PolicySyncError("gateway policy update failed")

    monkeypatch.setattr(admin, "_sync_repo_policy", boom)
    resp = admin.handler(_event("POST", "/admin/repos", body={"repo": "acme/web"}))
    assert resp["statusCode"] == 502
    import config_store

    # left pending → NOT allowed (dispatch won't treat a pending repo as active)
    assert config_store.get_repo("acme/web")["status"] == "pending"
    assert config_store.allowed_repos() == []


@mock_aws
def test_onboard_succeeds_on_sync_failure_when_log_only(monkeypatch):
    # LOG_ONLY (default): the policy blocks nothing, so a sync failure must NOT
    # fail onboarding — the repo stays active and the response carries a warning.
    monkeypatch.setenv("GATEWAY_ENFORCEMENT", "LOG_ONLY")
    _make_table()
    admin = _load_admin()

    def boom():
        raise admin.PolicySyncError("gateway not fully wired")

    monkeypatch.setattr(admin, "_sync_repo_policy", boom)
    resp = admin.handler(_event("POST", "/admin/repos", body={"repo": "acme/web"}))
    assert resp["statusCode"] == 200
    rec = _body(resp)
    assert rec["status"] == "active"
    assert "policy_sync_warning" in rec
    import config_store

    assert config_store.allowed_repos() == ["acme/web"]


# --- settings ----------------------------------------------------------------


@mock_aws
def test_settings_default_and_update():
    _make_table()
    admin = _load_admin()
    assert (
        _body(admin.handler(_event("GET", "/admin/settings")))["restrict_repos"]
        is False
    )
    resp = admin.handler(
        _event("PUT", "/admin/settings", body={"restrict_repos": True})
    )
    assert resp["statusCode"] == 200
    assert (
        _body(admin.handler(_event("GET", "/admin/settings")))["restrict_repos"] is True
    )


@mock_aws
def test_settings_put_requires_field():
    _make_table()
    admin = _load_admin()
    assert admin.handler(_event("PUT", "/admin/settings", body={}))["statusCode"] == 400


@mock_aws
def test_unknown_route_404():
    _make_table()
    admin = _load_admin()
    assert admin.handler(_event("GET", "/admin/nope"))["statusCode"] == 404
