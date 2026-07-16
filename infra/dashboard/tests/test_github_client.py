"""Tests for github_client — App JWT, manifest, token minting, verification.

The GitHub HTTP layer (_request) and the AWS clients (Secrets Manager / SSM) are
monkeypatched, so these exercise the module's logic (JWT shape, manifest schema,
token cache, owner-type/installation/reachability branches) without AWS or
network. RS256 signing is real — a throwaway key is generated in-process.
"""

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
    assert m["redirect_url"].endswith("#/admin/github-app/setup-callback")


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
