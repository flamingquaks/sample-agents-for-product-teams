"""Tests for the Part II config_store surface (spec §16–§18): identities,
user-onboarding requests, permission groups, notification subscriptions — and
the admin API composers (_decide_user_request, notif-sub repo bounding).

Runs against moto.
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
def store():
    with mock_aws():
        _make_table()
        import config_store

        config_store._table = None
        yield config_store
        config_store._table = None


@pytest.fixture
def admin_mod():
    with mock_aws():
        _make_table()
        import config_store
        import admin

        config_store._table = None
        yield admin, config_store
        config_store._table = None


# --- identities --------------------------------------------------------------


def test_put_and_get_identity(store):
    rec = store.put_identity(email="Jane@Acme.com", handles={"github": "jane-gh"})
    assert rec["email"] == "jane@acme.com"  # normalized
    assert "github:jane-gh" in rec["handle_keys"]
    assert store.get_identity(rec["identity_id"])["status"] == "pending"


def test_set_identity_groups_validates(store):
    rec = store.put_identity(handles={"github": "jane-gh"})
    store.set_identity_groups(rec["identity_id"], ["edtech-eng"])
    assert store.get_identity(rec["identity_id"])["groups"] == ["edtech-eng"]
    with pytest.raises(ValueError):
        store.set_identity_groups(rec["identity_id"], ["Bad Group!"])


# --- permission groups -------------------------------------------------------


def test_perm_group_crud_and_members(store):
    store.put_perm_group("edtech-eng", name="EdTech Engineers", recommended=True)
    assert store.get_perm_group("edtech-eng")["recommended"] is True
    ident = store.put_identity(handles={"github": "jane-gh"}, groups=["edtech-eng"])
    members = store.group_members("edtech-eng")
    assert [m["identity_id"] for m in members] == [ident["identity_id"]]
    with pytest.raises(ValueError):
        store.put_perm_group("Bad Id!")


# --- user requests -----------------------------------------------------------


def test_user_request_request_once(store):
    ident = store.put_identity(handles={"github": "jane-gh"})
    store.put_user_request(identity_id=ident["identity_id"], source="github")
    store.put_user_request(identity_id=ident["identity_id"], source="github")
    assert len(store.list_user_requests()) == 1  # deterministic id ⇒ one row
    assert store.find_user_request_for_identity(ident["identity_id"]) is not None


# --- notification subscriptions ----------------------------------------------


def test_notif_sub_validates_tiers(store):
    sub = store.put_notif_sub(
        "T0ACME01", "C0ENG001",
        repos=["Acme/Web"],
        tiers={"actionable": ["proposal_ready"], "error": ["run_failed"]},
        min_severity="error",
    )
    assert sub["repos"] == ["acme/web"]  # normalized
    assert sub["tiers"]["error"] == ["run_failed"]
    with pytest.raises(ValueError):
        store.put_notif_sub("T0ACME01", "C0ENG001", tiers={"bogus": []})


# --- admin composers ---------------------------------------------------------


def _admin_event(resource, method, path_params=None, body=None, sub="admin-1"):
    return {
        "resource": resource,
        "httpMethod": method,
        "pathParameters": path_params or {},
        "body": None if body is None else __import__("json").dumps(body),
        "requestContext": {"authorizer": {"claims": {"sub": sub, "cognito:groups": "admins"}}},
    }


def test_decide_user_request_approve_activates_and_groups(admin_mod, monkeypatch):
    admin, store = admin_mod
    monkeypatch.setattr(admin.auth, "is_admin", lambda e: True)
    store.put_perm_group("edtech-eng")
    ident = store.put_identity(handles={"github": "jane-gh"})
    store.put_user_request(identity_id=ident["identity_id"], source="github")
    req_id = f"user-{ident['identity_id']}"

    resp = admin.handler(
        _admin_event(
            "/admin/user-requests/{request_id}/approve", "POST",
            {"request_id": req_id}, {"groups": ["edtech-eng"]},
        )
    )
    assert resp["statusCode"] == 200
    updated = store.get_identity(ident["identity_id"])
    assert updated["status"] == "active"
    assert updated["groups"] == ["edtech-eng"]
    assert updated["verified"].get("github") is True  # admin approval = verification


def test_decide_user_request_rejects_unknown_group(admin_mod, monkeypatch):
    admin, store = admin_mod
    monkeypatch.setattr(admin.auth, "is_admin", lambda e: True)
    ident = store.put_identity(handles={"github": "jane-gh"})
    store.put_user_request(identity_id=ident["identity_id"], source="github")
    resp = admin.handler(
        _admin_event(
            "/admin/user-requests/{request_id}/approve", "POST",
            {"request_id": f"user-{ident['identity_id']}"}, {"groups": ["nonexistent"]},
        )
    )
    assert resp["statusCode"] == 400


def test_notif_sub_repo_bound_to_grants(admin_mod, monkeypatch):
    admin, store = admin_mod
    monkeypatch.setattr(admin.auth, "is_admin", lambda e: True)
    store.put_repo("acme/web", enabled=True, status="active")
    # Subscribing to a granted repo works…
    ok_resp = admin.handler(
        _admin_event("/admin/notif-subs", "POST", body={
            "team_id": "T0ACME01", "channel_id": "C0ENG001", "repos": ["acme/web"],
            "tiers": {"error": ["run_failed"]},
        })
    )
    assert ok_resp["statusCode"] == 200
    # …but a repo the fleet doesn't manage is rejected (§18.2).
    bad_resp = admin.handler(
        _admin_event("/admin/notif-subs", "POST", body={
            "team_id": "T0ACME01", "channel_id": "C0ENG001", "repos": ["evil/repo"],
            "tiers": {"error": ["run_failed"]},
        })
    )
    assert bad_resp["statusCode"] == 403
