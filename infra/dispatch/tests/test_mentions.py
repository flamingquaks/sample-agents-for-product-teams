"""Tests for the shared webhook-receiver helpers (mentions.py): HMAC
verification, registry-backed @mention resolution, and the short-TTL cache."""

import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mentions  # noqa: E402

REGISTRY = {
    "agents": {
        "workitems": {"aliases": ["pm", "status", "plan"]},
        "docwriter": {"aliases": ["docs", "doc", "writer"]},
        "adr": {"aliases": []},
    }
}


# --- HMAC --------------------------------------------------------------------


def _sig(secret, body, prefix=""):
    return prefix + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def test_hmac_github_prefixed():
    body = '{"a":1}'
    assert mentions.verify_hmac_sha256("s3cr3t", body, _sig("s3cr3t", body, "sha256="), prefix="sha256=")


def test_hmac_asana_bare_hex():
    body = '{"events":[]}'
    assert mentions.verify_hmac_sha256("k", body, _sig("k", body))


def test_hmac_rejects_wrong_secret_and_tamper():
    body = "payload"
    assert not mentions.verify_hmac_sha256("k", body, _sig("wrong", body))
    assert not mentions.verify_hmac_sha256("k", body + "x", _sig("k", body))


def test_hmac_rejects_empty_secret_or_signature():
    body = "payload"
    assert not mentions.verify_hmac_sha256("", body, _sig("k", body))
    assert not mentions.verify_hmac_sha256("k", body, "")


def test_hmac_rejects_missing_prefix():
    body = "payload"
    # A bare-hex signature must not verify when the github prefix is required.
    assert not mentions.verify_hmac_sha256("k", body, _sig("k", body), prefix="sha256=")


# --- resolution --------------------------------------------------------------


def test_resolve_direct_id_and_instruction():
    assert mentions.resolve_mention("@workitems break this down", REGISTRY) == (
        "workitems",
        "break this down",
    )


def test_resolve_alias_to_canonical():
    assert mentions.resolve_mention("@docs update the readme", REGISTRY) == (
        "docwriter",
        "update the readme",
    )


def test_resolve_first_matching_mention_wins():
    # A non-agent @word is skipped; the first REAL agent mention resolves.
    assert mentions.resolve_mention("hey @someone ask @adr to review", REGISTRY) == (
        "adr",
        "to review",
    )


def test_resolve_unknown_returns_none():
    assert mentions.resolve_mention("@ghost do a thing", REGISTRY) is None
    assert mentions.resolve_mention("no mention here", REGISTRY) is None


def test_resolve_empty_registry_returns_none():
    assert mentions.resolve_mention("@workitems go", {"agents": {}}) is None
    assert mentions.resolve_mention("@workitems go", {}) is None


def test_resolve_agent_returns_id_only():
    assert mentions.resolve_agent("@pm plan the sprint", REGISTRY) == "workitems"
    assert mentions.resolve_agent("nope", REGISTRY) is None


# --- RegistryCache -----------------------------------------------------------


class _FakeSSM:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def get_parameter(self, Name, WithDecryption=False):
        self.calls += 1
        if self.value is None:
            raise RuntimeError("ssm down")
        return {"Parameter": {"Value": self.value}}


def test_cache_loads_and_reuses_within_ttl():
    ssm = _FakeSSM(json.dumps(REGISTRY))
    cache = mentions.RegistryCache("/p", lambda: ssm, ttl_seconds=300)
    assert cache.resolve_agent("@workitems go") == "workitems"
    assert cache.resolve_agent("@docs go") == "docwriter"
    assert ssm.calls == 1  # second call served from cache


def test_cache_refreshes_after_ttl():
    ssm = _FakeSSM(json.dumps(REGISTRY))
    # ttl=0 → every load is past expiry, so each resolve refetches.
    cache = mentions.RegistryCache("/p", lambda: ssm, ttl_seconds=0)
    cache.resolve_agent("@workitems go")
    cache.resolve_agent("@workitems go")
    assert ssm.calls == 2


def test_cache_error_serves_last_known_good():
    ssm = _FakeSSM(json.dumps(REGISTRY))
    cache = mentions.RegistryCache("/p", lambda: ssm, ttl_seconds=0)
    assert cache.resolve_agent("@workitems go") == "workitems"
    # SSM now fails; the cache keeps serving the previously-loaded registry.
    ssm.value = None
    assert cache.resolve_agent("@workitems go") == "workitems"


def test_cache_error_with_no_prior_load_is_empty():
    ssm = _FakeSSM(None)
    cache = mentions.RegistryCache("/p", lambda: ssm)
    # No cache yet + read error → empty registry → nothing resolves (no raise).
    assert cache.resolve_agent("@workitems go") is None
    assert cache.load() == {}
