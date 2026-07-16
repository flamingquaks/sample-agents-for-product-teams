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

    # delete
    resp = admin.handler(
        _event("DELETE", "/admin/repos/{repo}", path={"repo": "acme/web"})
    )
    assert resp["statusCode"] == 200 and _body(resp)["deleted"] is True
    assert _body(admin.handler(_event("GET", "/admin/repos")))["repos"] == []


@mock_aws
def test_onboard_rejects_bad_repo():
    _make_table()
    admin = _load_admin()
    for bad in ["", "noslash", "a/b/c", "own er/repo", "owner/"]:
        resp = admin.handler(_event("POST", "/admin/repos", body={"repo": bad}))
        assert resp["statusCode"] == 400, bad


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
def test_pending_left_on_policy_sync_failure(monkeypatch):
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
