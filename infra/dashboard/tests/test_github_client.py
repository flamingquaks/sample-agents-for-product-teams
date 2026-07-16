"""Tests for github_client — App JWT, manifest, token minting, verification.

The GitHub HTTP layer (_request) and the AWS clients (Secrets Manager / SSM) are
monkeypatched, so these exercise the module's logic (JWT shape, manifest schema,
token cache, owner-type/installation/reachability branches) without AWS or
network. RS256 signing is real — a throwaway key is generated in-process.
"""

import calendar
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import github_client  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # Clear module caches + point env at fake param/secret names.
    github_client._pem_cache = None
    github_client._jwt_cache = None
    github_client._token_cache.clear()
    monkeypatch.setenv("GITHUB_APP_ID_PARAM", "/x/app-id")
    monkeypatch.setenv("GITHUB_APP_SLUG_PARAM", "/x/app-slug")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_SECRET_ARN", "arn:secret")
    # Real RSA key so _app_jwt actually signs; app_id/slug via a fake SSM.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(github_client, "_private_key", lambda: pem)
    monkeypatch.setattr(github_client, "_app_id", lambda: "12345")
    monkeypatch.setattr(github_client, "app_slug", lambda: "sdlc-fleet")


def test_app_configured(monkeypatch):
    assert github_client.app_configured() is True
    monkeypatch.delenv("GITHUB_APP_ID_PARAM", raising=False)
    assert github_client.app_configured() is False


def test_app_not_configured_when_placeholder(monkeypatch):
    # The template seeds app-id with "unset" — that must read as NOT configured,
    # else the UI shows a registered App + a /apps/unset/ install link.
    monkeypatch.setattr(github_client, "_app_id", lambda: "unset")
    assert github_client.app_configured() is False


def test_app_not_configured_when_key_secret_empty(monkeypatch):
    # A real app-id but an empty private-key secret (template placeholder / a
    # failed key write) must read as NOT configured — else verification passes
    # its "not set up" guard and the first JWT sign 500s on an empty PEM.
    monkeypatch.setattr(github_client, "_private_key", lambda: "")
    assert github_client.app_configured() is False


def test_app_jwt_cached_across_calls(monkeypatch):
    # One onboarding calls _app_jwt three times (owner-type + installation +
    # mint); it must sign once and reuse, not re-sign + re-read app-id each time.
    signs = {"n": 0}
    real_app_id = github_client._app_id

    def counting_app_id():
        signs["n"] += 1
        return real_app_id()

    monkeypatch.setattr(github_client, "_app_id", counting_app_id)
    a = github_client._app_jwt()
    b = github_client._app_jwt()
    assert a == b
    assert signs["n"] == 1  # second call served from cache, no re-sign/app-id read


def test_app_jwt_is_three_segments_with_iss():
    import base64
    import json

    jwt = github_client._app_jwt()
    parts = jwt.split(".")
    assert len(parts) == 3
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    assert payload["iss"] == "12345"
    assert payload["exp"] - payload["iat"] <= 600 + 60  # ≤10min + drift buffer


def test_generate_manifest_shape():
    m = github_client.generate_manifest(
        "App", "https://api.example/staging", "https://d.cf/"
    )
    assert m["name"] == "App"
    assert m["hook_attributes"]["active"] is False  # webhook disabled
    assert m["default_permissions"] == github_client.APP_PERMISSIONS
    assert "contents" in m["default_permissions"]
    # redirect_url must be fragment-free (GitHub rejects "#..."); a real path.
    assert m["redirect_url"] == "https://d.cf/github-app-callback"
    assert "#" not in m["redirect_url"]


def test_get_owner_type(monkeypatch):
    monkeypatch.setattr(
        github_client, "_request", lambda *a, **k: (200, {"type": "Organization"})
    )
    assert github_client.get_owner_type("acme") == "Organization"


def test_find_installation_present_and_absent(monkeypatch):
    monkeypatch.setattr(github_client, "_request", lambda *a, **k: (200, {"id": 42}))
    assert github_client.find_installation("acme", "Organization") == 42

    def not_found(*a, **k):
        raise github_client.GitHubError("nope", status=404)

    monkeypatch.setattr(github_client, "_request", not_found)
    assert github_client.find_installation("alice", "User") is None


def test_mint_installation_token_caches(monkeypatch):
    calls = {"n": 0}
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))

    def fake(method, url, **k):
        calls["n"] += 1
        return 201, {"token": "ghs_abc", "expires_at": future}

    monkeypatch.setattr(github_client, "_request", fake)
    t1, _ = github_client.mint_installation_token(99)
    t2, _ = github_client.mint_installation_token(99)  # served from cache
    assert t1 == t2 == "ghs_abc"
    assert calls["n"] == 1  # second call hit the cache, no HTTP

    # The cached expiry must be parsed as UTC (calendar.timegm), not local time
    # (time.mktime) — else a non-UTC host skews it. Assert the stored epoch is
    # within a minute of the true UTC epoch for the "...Z" timestamp.
    _tok, cached_exp = github_client._token_cache[99]
    true_epoch = calendar.timegm(time.strptime(future, "%Y-%m-%dT%H:%M:%SZ"))
    assert abs(cached_exp - true_epoch) < 60


def test_repo_reachable_true_false(monkeypatch):
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    monkeypatch.setattr(
        github_client,
        "mint_installation_token",
        lambda _id: ("ghs_x", future),
    )
    monkeypatch.setattr(github_client, "_request", lambda *a, **k: (200, {"id": 1}))
    assert github_client.repo_reachable("acme", "web", 1) is True

    def not_found(*a, **k):
        raise github_client.GitHubError("404", status=404)

    monkeypatch.setattr(github_client, "_request", not_found)
    assert github_client.repo_reachable("acme", "ghost", 1) is False


def test_install_url_uses_slug():
    assert (
        github_client.install_url()
        == "https://github.com/apps/sdlc-fleet/installations/new"
    )


def test_exchange_manifest_code_persists(monkeypatch):
    monkeypatch.setattr(
        github_client,
        "_request",
        lambda *a, **k: (201, {"id": 777, "slug": "sdlc-fleet", "pem": "PEMDATA"}),
    )
    put_secret = {}
    put_params = {}

    class _SM:
        def put_secret_value(self, **kw):
            put_secret.update(kw)

    class _SSM:
        def put_parameter(self, **kw):
            put_params[kw["Name"]] = kw["Value"]

    monkeypatch.setattr(github_client, "_sm", lambda: _SM())
    monkeypatch.setattr(github_client, "_ssm_client", lambda: _SSM())
    result = github_client.exchange_manifest_code("thecode")
    assert result == {"app_id": "777", "slug": "sdlc-fleet"}
    assert put_secret["SecretString"] == "PEMDATA"
    assert put_params["/x/app-id"] == "777"
    assert put_params["/x/app-slug"] == "sdlc-fleet"


def test_exchange_manifest_code_never_leaks_code(monkeypatch):
    # SECURITY: on a failed exchange the still-redeemable manifest code must NOT
    # appear in the raised error (it would flow into the 502 body + CloudWatch).
    def boom(method, url, **k):
        # _request embeds the URL (which contains the code) in the message.
        raise github_client.GitHubError(f"GitHub {method} {url} -> 500: oops", status=500)

    monkeypatch.setattr(github_client, "_request", boom)
    with pytest.raises(github_client.GitHubError) as ei:
        github_client.exchange_manifest_code("SUPER_SECRET_CODE")
    assert "SUPER_SECRET_CODE" not in str(ei.value)


def test_exchange_manifest_code_writes_app_id_last(monkeypatch):
    # CRASH-CONSISTENCY: app_configured() keys on app-id, so app-id must be the
    # LAST write — if the slug write fails, app-id must not have been written
    # (no half-registered App that reports configured with no key/slug).
    monkeypatch.setattr(
        github_client,
        "_request",
        lambda *a, **k: (201, {"id": 777, "slug": "sdlc-fleet", "pem": "PEMDATA"}),
    )
    order = []

    class _SM:
        def put_secret_value(self, **kw):
            order.append("secret")

    class _SSM:
        def put_parameter(self, **kw):
            if kw["Name"] == "/x/app-slug":
                order.append("slug")
                raise RuntimeError("transient SSM failure")
            order.append("app-id")

    monkeypatch.setattr(github_client, "_sm", lambda: _SM())
    monkeypatch.setattr(github_client, "_ssm_client", lambda: _SSM())
    with pytest.raises(RuntimeError):
        github_client.exchange_manifest_code("thecode")
    # The slug write blew up before app-id was ever written.
    assert "app-id" not in order
    assert order == ["secret", "slug"]
