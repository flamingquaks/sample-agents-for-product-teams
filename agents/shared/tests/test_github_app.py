"""Unit tests for the agent-runtime GitHub App token minter
(agents/shared/tools/github_app.py). AWS clients + GitHub HTTP are patched;
RS256 signing is real (throwaway key). Run with PYTHONPATH=agents."""

import calendar
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.tools import github_app  # noqa: E402


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


def test_app_mode_default_pat(monkeypatch):
    monkeypatch.delenv("GITHUB_AUTH_MODE", raising=False)
    assert github_app.app_mode() is False
    monkeypatch.setenv("GITHUB_AUTH_MODE", "app")
    assert github_app.app_mode() is True


def test_token_for_dispatch_accepts_owner_or_owner_repo(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": {"installation_id": 99}}
    monkeypatch.setattr(github_app, "_table", lambda: fake_table)

    seen = {}

    def fake_request(method, url, *, token):
        seen["url"] = url
        future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        return 201, {"token": "ghs_z", "expires_at": future}

    monkeypatch.setattr(github_app, "_request", fake_request)
    # owner/repo form
    assert github_app.token_for_dispatch("acme/web") == "ghs_z"
    assert seen["url"].endswith("/app/installations/99/access_tokens")
    # bare owner form resolves the same owner record
    github_app._token_cache.clear()
    assert github_app.token_for_dispatch("acme") == "ghs_z"


def test_expiry_parsed_as_utc(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": {"installation_id": 5}}
    monkeypatch.setattr(github_app, "_table", lambda: fake_table)
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    monkeypatch.setattr(
        github_app,
        "_request",
        lambda *a, **k: (201, {"token": "ghs_x", "expires_at": future}),
    )
    github_app.installation_token_for_owner("acme")
    _tok, exp = github_app._token_cache[5]
    assert abs(exp - calendar.timegm(time.strptime(future, "%Y-%m-%dT%H:%M:%SZ"))) < 60


def test_no_installation_raises(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {}  # no per-owner record
    monkeypatch.setattr(github_app, "_table", lambda: fake_table)

    def fake_request(method, url, *, token):
        raise github_app.GitHubAppError("404")

    monkeypatch.setattr(github_app, "_request", fake_request)
    with pytest.raises(github_app.GitHubAppError):
        github_app.installation_token_for_owner("ghost")


def test_empty_owner_raises():
    with pytest.raises(github_app.GitHubAppError):
        github_app.installation_token_for_owner("")
