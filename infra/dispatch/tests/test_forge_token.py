"""Tests for verify_forge_invocation_token (mentions.py) — RS256 verification
against a JWKS, expiry, pinned app id, and the JWKS-unreachable fail-closed path
(atlassian-connector spec §A5). Uses a locally-generated RSA key + a fake JWKS."""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mentions  # noqa: E402

jwt = pytest.importorskip("jwt")
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402


@pytest.fixture
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    from jwt.algorithms import RSAAlgorithm

    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "test-kid"
    return priv_pem, jwk


def _install_jwks(monkeypatch, jwk):
    monkeypatch.setattr(mentions, "_jwks_cache", None)
    monkeypatch.setattr(mentions, "_jwks_expires_at", 0.0)
    monkeypatch.setattr(mentions, "_load_jwks", lambda *, force=False: {"keys": [jwk]})


def _token(priv_pem, *, app_id="ari:app/1", exp_offset=300):
    return jwt.encode(
        {"app": {"id": app_id}, "cloudId": "site-1",
         "exp": int(time.time()) + exp_offset},
        priv_pem, algorithm="RS256", headers={"kid": "test-kid"},
    )


def test_valid_token_returns_claims(monkeypatch, keypair):
    priv, jwk = keypair
    _install_jwks(monkeypatch, jwk)
    claims = mentions.verify_forge_invocation_token(
        _token(priv), expected_app_id="ari:app/1"
    )
    assert claims["cloudId"] == "site-1"


def test_expired_token_rejected(monkeypatch, keypair):
    priv, jwk = keypair
    _install_jwks(monkeypatch, jwk)
    with pytest.raises(mentions.ForgeTokenError):
        mentions.verify_forge_invocation_token(
            _token(priv, exp_offset=-10), expected_app_id="ari:app/1"
        )


def test_wrong_app_id_rejected(monkeypatch, keypair):
    priv, jwk = keypair
    _install_jwks(monkeypatch, jwk)
    with pytest.raises(mentions.ForgeTokenError, match="app id"):
        mentions.verify_forge_invocation_token(
            _token(priv, app_id="ari:app/EVIL"), expected_app_id="ari:app/1"
        )


def test_wrong_signature_rejected(monkeypatch, keypair):
    priv, jwk = keypair
    # A token signed by a DIFFERENT key must fail against this JWKS.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _install_jwks(monkeypatch, jwk)
    bad = jwt.encode({"app": {"id": "ari:app/1"}, "exp": int(time.time()) + 300},
                     other_pem, algorithm="RS256", headers={"kid": "test-kid"})
    with pytest.raises(mentions.ForgeTokenError):
        mentions.verify_forge_invocation_token(bad, expected_app_id="ari:app/1")


def test_jwks_unreachable_is_unavailable(monkeypatch, keypair):
    priv, _ = keypair

    def boom(*, force=False):
        raise mentions.ForgeTokenError("network", unavailable=True)

    monkeypatch.setattr(mentions, "_load_jwks", boom)
    with pytest.raises(mentions.ForgeTokenError) as ei:
        mentions.verify_forge_invocation_token(_token(priv), expected_app_id="ari:app/1")
    assert ei.value.unavailable is True


def test_missing_token_rejected():
    with pytest.raises(mentions.ForgeTokenError):
        mentions.verify_forge_invocation_token("", expected_app_id="ari:app/1")
