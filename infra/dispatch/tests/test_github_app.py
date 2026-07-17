"""Unit tests for the dispatch reply Lambda's GitHub App token minter.

The GitHub HTTP layer (requests) and AWS clients (Secrets Manager / SSM /
DynamoDB) are patched, so these exercise the minting logic — JWT shape, install
resolution from the per-owner record, token cache, UTC expiry parse — without
AWS or network. RS256 signing is real (a throwaway key is generated in-process).
"""

import calendar
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import github_app  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    github_app._pem_cache = None
    github_app._jwt_cache = None
    github_app._token_cache.clear()
    monkeypatch.setenv("GITHUB_APP_ID_PARAM", "/x/app-id")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_SECRET_ARN", "arn:secret")
    monkeypatch.setenv("FLEET_CONFIG_TABLE", "fleet-config-test")

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(github_app, "_private_key", lambda: pem)
    monkeypatch.setattr(github_app, "_app_id", lambda: "12345")


def test_app_mode_flag(monkeypatch):
    monkeypatch.setenv("GITHUB_AUTH_MODE", "app")
    assert github_app.app_mode() is True
    monkeypatch.setenv("GITHUB_AUTH_MODE", "pat")
    assert github_app.app_mode() is False
    monkeypatch.delenv("GITHUB_AUTH_MODE", raising=False)
    assert github_app.app_mode() is False  # default pat


def test_installation_resolved_from_owner_record(monkeypatch):
    # The per-owner install record (written at onboard) is preferred — no GitHub
    # round-trip for the installation lookup.
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": {"installation_id": 55}}
    monkeypatch.setattr(github_app, "_table", lambda: fake_table)

    posted = {}

    def fake_post(url, headers=None, timeout=None):
        posted["url"] = url
        resp = MagicMock()
        resp.status_code = 201
        future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        resp.json.return_value = {"token": "ghs_abc", "expires_at": future}
        return resp

    with patch.object(github_app.requests, "post", side_effect=fake_post):
        tok = github_app.installation_token_for_repo("acme/web")
    assert tok == "ghs_abc"
    # Token minted against installation 55 from the record (no /installation GET).
    assert posted["url"].endswith("/app/installations/55/access_tokens")
    fake_table.get_item.assert_called_once()


def test_token_is_cached_and_expiry_parsed_utc(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": {"installation_id": 7}}
    monkeypatch.setattr(github_app, "_table", lambda: fake_table)
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    calls = {"n": 0}

    def fake_post(url, headers=None, timeout=None):
        calls["n"] += 1
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"token": "ghs_x", "expires_at": future}
        return resp

    with patch.object(github_app.requests, "post", side_effect=fake_post):
        github_app.installation_token_for_repo("acme/web")
        github_app.installation_token_for_repo("acme/api")  # same owner → cache hit
    assert calls["n"] == 1
    _tok, exp = github_app._token_cache[7]
    true_epoch = calendar.timegm(time.strptime(future, "%Y-%m-%dT%H:%M:%SZ"))
    assert abs(exp - true_epoch) < 60  # UTC parse (calendar.timegm), not local


def test_missing_installation_raises(monkeypatch):
    # No record and GitHub reports no installation for either owner shape.
    fake_table = MagicMock()
    fake_table.get_item.return_value = {}
    monkeypatch.setattr(github_app, "_table", lambda: fake_table)
    resp = MagicMock()
    resp.status_code = 404
    resp.json.return_value = {}
    with patch.object(github_app.requests, "get", return_value=resp):
        with pytest.raises(github_app.GitHubAppError):
            github_app.installation_token_for_repo("ghost/repo")


def test_app_jwt_cached(monkeypatch):
    signs = {"n": 0}
    real = github_app._app_id

    def counting():
        signs["n"] += 1
        return real()

    monkeypatch.setattr(github_app, "_app_id", counting)
    a = github_app._app_jwt()
    b = github_app._app_jwt()
    assert a == b and signs["n"] == 1
