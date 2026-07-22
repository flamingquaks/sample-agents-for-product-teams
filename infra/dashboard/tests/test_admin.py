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


def _load_admin(fake=None):
    # Fresh import per test-run so the cached table handle binds to moto.
    # Onboarding now ALWAYS verifies a GitHub App installation (the PAT path was
    # retired), so inject a github_client — a passing _FakeGitHub by default — for
    # the handler's lazy ``import github_client`` to pick up.
    for m in ("admin", "config_store", "auth", "http_responses", "github_client"):
        sys.modules.pop(m, None)
    sys.modules["github_client"] = fake if fake is not None else _FakeGitHub()
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


# --- co-repo grouping (which repos may run together) --------------------------


@mock_aws
def test_onboard_defaults_to_isolated_co_repo_mode():
    _make_table()
    admin = _load_admin()
    admin.handler(_event("POST", "/admin/repos", body={"repo": "acme/web"}))
    import config_store

    rec = config_store.get_repo("acme/web")
    assert rec["co_repo_mode"] == "isolated"
    # isolated origin reaches only itself
    assert config_store.coreachable_repos("acme/web") == ["acme/web"]


@mock_aws
def test_onboard_group_requires_repo_group():
    _make_table()
    admin = _load_admin()
    resp = admin.handler(
        _event("POST", "/admin/repos", body={"repo": "acme/web", "co_repo_mode": "group"})
    )
    assert resp["statusCode"] == 400
    assert "repo_group" in _body(resp)["error"]


@mock_aws
def test_onboard_rejects_bad_co_repo_mode():
    _make_table()
    admin = _load_admin()
    resp = admin.handler(
        _event("POST", "/admin/repos", body={"repo": "acme/web", "co_repo_mode": "wat"})
    )
    assert resp["statusCode"] == 400


@mock_aws
def test_group_mode_spans_owners_and_isolates_other_groups():
    # acme/web + acme/api + bob/tool in group "platform" (mixed personal + org);
    # zed/x in a different group; iso/repo isolated. web reaches its whole group
    # across owners but not the other group or the isolated repo.
    _make_table()
    admin = _load_admin()
    import config_store

    for repo, group in [("acme/web", "platform"), ("acme/api", "platform"), ("bob/tool", "platform"), ("zed/x", "other")]:
        admin.handler(
            _event(
                "POST",
                "/admin/repos",
                body={"repo": repo, "co_repo_mode": "group", "repo_group": group},
            )
        )
    admin.handler(_event("POST", "/admin/repos", body={"repo": "iso/repo", "co_repo_mode": "isolated"}))

    reach = config_store.coreachable_repos("acme/web")
    assert reach[0] == "acme/web"  # origin first
    assert set(reach) == {"acme/web", "acme/api", "bob/tool"}  # spans acme + bob
    assert "zed/x" not in reach and "iso/repo" not in reach


@mock_aws
def test_all_mode_reaches_every_eligible_repo():
    _make_table()
    admin = _load_admin()
    import config_store

    admin.handler(_event("POST", "/admin/repos", body={"repo": "acme/hub", "co_repo_mode": "all"}))
    admin.handler(_event("POST", "/admin/repos", body={"repo": "bob/svc", "co_repo_mode": "isolated"}))
    admin.handler(_event("POST", "/admin/repos", body={"repo": "zed/lib", "co_repo_mode": "group", "repo_group": "g"}))
    reach = set(config_store.coreachable_repos("acme/hub"))
    # all-mode reaches every eligible repo regardless of their own mode/owner
    assert reach == {"acme/hub", "bob/svc", "zed/lib"}


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
    # fail onboarding — the repo stays active. No warning is surfaced to the user
    # (it's non-actionable jargon); the backend logs it.
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
    # No jargon warning in the response — just a clean onboard result.
    assert "policy_sync_warning" not in rec
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


# --- GitHub App onboarding verification ---------------------------------------


class _FakeGitHub:
    """Stand-in for github_client injected into the handler's lazy import."""

    GitHubError = type("GitHubError", (Exception,), {"status": None})

    def __init__(
        self, *, configured=True, owner_type="User", installation_id=7, reachable=True
    ):
        self._configured = configured
        self._owner_type = owner_type
        self._installation_id = installation_id
        self._reachable = reachable

    def app_configured(self):
        return self._configured

    def find_repo_installation(self, owner, repo):
        # Covered ⇔ App installed on the owner AND repo in its selection.
        if self._installation_id is None or not self._reachable:
            return None
        return self._installation_id, self._owner_type

    def find_installation(self, owner):
        if self._installation_id is None:
            return None
        return self._installation_id, self._owner_type

    def install_url(self, owner=None):
        return "https://github.com/apps/sdlc-fleet/installations/new"


def _load_admin_app_mode(monkeypatch, fake):
    # Onboarding always verifies a GitHub App install now; this just loads admin
    # with a specific fake github_client and stubs the policy-sync seam.
    admin = _load_admin(fake)
    monkeypatch.setattr(admin, "_sync_repo_policy", lambda: None)
    return admin


@mock_aws
def test_onboard_verifies_and_stores_installation(monkeypatch):
    _make_table()
    admin = _load_admin_app_mode(
        monkeypatch, _FakeGitHub(owner_type="Organization", installation_id=55)
    )
    resp = admin.handler(_event("POST", "/admin/repos", body={"repo": "acme/web"}))
    assert resp["statusCode"] == 200
    import config_store

    rec = config_store.get_repo("acme/web")
    assert rec["installation_id"] == 55 and rec["status"] == "active"
    # per-owner install record written
    inst = config_store.get_installation("acme")
    assert inst["owner_type"] == "Organization" and inst["installation_id"] == 55


@mock_aws
def test_onboard_409_when_app_not_installed(monkeypatch):
    _make_table()
    fake = _FakeGitHub(installation_id=None)  # App not installed on the owner
    admin = _load_admin_app_mode(monkeypatch, fake)
    resp = admin.handler(_event("POST", "/admin/repos", body={"repo": "acme/web"}))
    assert resp["statusCode"] == 409
    body = _body(resp)
    assert "install_url" in body and body["install_url"].startswith(
        "https://github.com/apps/"
    )
    import config_store

    assert config_store.get_repo("acme/web") is None  # NOT onboarded


@mock_aws
def test_onboard_409_when_repo_not_covered(monkeypatch):
    _make_table()
    admin = _load_admin_app_mode(monkeypatch, _FakeGitHub(reachable=False))
    resp = admin.handler(_event("POST", "/admin/repos", body={"repo": "acme/web"}))
    assert resp["statusCode"] == 409
    assert "covered" in _body(resp)["error"]


@mock_aws
def test_onboard_409_when_app_not_configured(monkeypatch):
    _make_table()
    admin = _load_admin_app_mode(monkeypatch, _FakeGitHub(configured=False))
    resp = admin.handler(_event("POST", "/admin/repos", body={"repo": "acme/web"}))
    assert resp["statusCode"] == 409
    assert "not set up" in _body(resp)["error"]


@mock_aws
def test_update_existing_repo_skips_verification(monkeypatch):
    # An already-onboarded repo can be updated (e.g. DISABLED during an incident)
    # even when the App is now unreachable — verification gates NEW onboards only,
    # never updates, so the admin is never locked out of turning a repo off.
    _make_table()
    admin = _load_admin_app_mode(monkeypatch, _FakeGitHub(installation_id=55))
    admin.handler(_event("POST", "/admin/repos", body={"repo": "acme/web"}))
    import config_store

    assert config_store.get_repo("acme/web")["installation_id"] == 55

    # App becomes unreachable; disabling the repo must still succeed.
    admin2 = _load_admin_app_mode(monkeypatch, _FakeGitHub(installation_id=None))
    resp = admin2.handler(
        _event("POST", "/admin/repos", body={"repo": "acme/web", "enabled": False})
    )
    assert resp["statusCode"] == 200
    rec = config_store.get_repo("acme/web")
    assert rec["enabled"] is False
    assert rec["installation_id"] == 55  # preserved, not wiped


@mock_aws
def test_bulk_onboard_shared_group(monkeypatch):
    # Several repos in one action, shared access together: one group-mode batch
    # → every row lands active with the same group, ONE policy sync.
    _make_table()
    syncs = {"n": 0}
    admin = _load_admin(_FakeGitHub(installation_id=9))

    def count_sync():
        syncs["n"] += 1

    monkeypatch.setattr(admin, "_sync_repo_policy", count_sync)
    resp = admin.handler(
        _event(
            "POST",
            "/admin/repos",
            body={
                "repos": ["acme/web", "acme/api", "acme/docs"],
                "co_repo_mode": "group",
                "repo_group": "batch-1",
            },
        )
    )
    assert resp["statusCode"] == 200
    recs = _body(resp)["repos"]
    assert [r["repo"] for r in recs] == ["acme/web", "acme/api", "acme/docs"]
    assert all(r["status"] == "active" and r["repo_group"] == "batch-1" for r in recs)
    assert syncs["n"] == 1  # one sync for the whole batch, not one per repo


@mock_aws
def test_bulk_onboard_atomic_on_verify_failure(monkeypatch):
    # One uncovered repo fails the WHOLE batch before anything is written.
    class _OnlyWeb(_FakeGitHub):
        def find_repo_installation(self, owner, repo):
            return (9, "User") if repo == "web" else None

    _make_table()
    admin = _load_admin_app_mode(monkeypatch, _OnlyWeb())
    resp = admin.handler(
        _event("POST", "/admin/repos", body={"repos": ["acme/web", "acme/ghost"]})
    )
    assert resp["statusCode"] == 409
    import config_store

    assert config_store.list_repos() == []  # nothing half-onboarded


@mock_aws
def test_bulk_onboard_rejects_bad_entry(monkeypatch):
    _make_table()
    admin = _load_admin_app_mode(monkeypatch, _FakeGitHub())
    resp = admin.handler(
        _event("POST", "/admin/repos", body={"repos": ["acme/web", "not-a-repo"]})
    )
    assert resp["statusCode"] == 400


@mock_aws
def test_github_app_repos_listing(monkeypatch):
    # The onboarding picker's source: installations + reachable repos, with
    # already-onboarded ones flagged.
    class _Listing(_FakeGitHub):
        def list_installations(self):
            return [{"installation_id": 9, "owner": "acme", "owner_type": "User"}]

        def list_installation_repos(self, installation_id):
            return [
                {"repo": "acme/web", "private": True},
                {"repo": "acme/api", "private": False},
            ]

    _make_table()
    admin = _load_admin_app_mode(monkeypatch, _Listing())
    admin.handler(_event("POST", "/admin/repos", body={"repo": "acme/web"}))
    resp = admin.handler(_event("GET", "/admin/github-app/repos"))
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["configured"] is True
    repos = body["installations"][0]["repos"]
    assert {r["repo"]: r["onboarded"] for r in repos} == {
        "acme/web": True,
        "acme/api": False,
    }


@mock_aws
def test_github_app_repos_unconfigured(monkeypatch):
    _make_table()
    admin = _load_admin_app_mode(monkeypatch, _FakeGitHub(configured=False))
    resp = admin.handler(_event("GET", "/admin/github-app/repos"))
    assert resp["statusCode"] == 200
    assert _body(resp) == {"configured": False, "installations": []}


@mock_aws
def test_verification_error_is_actionable_not_500(monkeypatch):
    # A non-GitHubError during verification (e.g. bad key → ValueError) must
    # surface as an actionable 502, not an opaque 500.
    class _Boom(_FakeGitHub):
        def find_repo_installation(self, owner, repo):
            raise ValueError("could not deserialize key data")

    _make_table()
    admin = _load_admin_app_mode(monkeypatch, _Boom())
    resp = admin.handler(_event("POST", "/admin/repos", body={"repo": "acme/web"}))
    assert resp["statusCode"] == 502
    assert "private key" in _body(resp)["error"]


@mock_aws
def test_delete_last_repo_removes_install_record(monkeypatch):
    # Deleting an owner's LAST repo cleans up the shared per-owner install record;
    # a sibling repo keeps it alive.
    _make_table()
    admin = _load_admin_app_mode(monkeypatch, _FakeGitHub(installation_id=55))
    admin.handler(_event("POST", "/admin/repos", body={"repo": "acme/web"}))
    admin.handler(_event("POST", "/admin/repos", body={"repo": "acme/api"}))
    import config_store

    assert config_store.get_installation("acme") is not None

    # Delete one of two — install record survives (acme still has a repo).
    admin.handler(_event("DELETE", "/admin/repos/{repo+}", path={"repo": "acme/web"}))
    assert config_store.get_installation("acme") is not None

    # Delete the last — install record is cleaned up.
    admin.handler(_event("DELETE", "/admin/repos/{repo+}", path={"repo": "acme/api"}))
    assert config_store.get_installation("acme") is None
