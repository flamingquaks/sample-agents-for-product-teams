"""Tests for the Slack webhook receiver (slack_webhook.py).

Covers the security contract (v0 signature verification, replay window,
per-workspace secret selection, unknown/disabled workspace rejection), event
handling (url_verification, app_mention → dispatch, bot-loop, dedup), and slash
commands (mention dispatch + the /onboard-channel request flow). External
clients (SSM, Lambda, DDB dedup, trigger_grants, registry) are stubbed.
"""

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("REGISTRY_PARAM", "/sdlc-agents/test/registry")
os.environ.setdefault("DISPATCH_FUNCTION", "dispatch-router-test")
os.environ.setdefault("STAGE", "test")

SECRET = "slack-signing-secret"
TEAM = "T0ACME12"
REGISTRY = {"agents": {"workitems": {"aliases": ["pm", "plan"]}}}


def _sign(body: str, secret=SECRET, ts=None):
    ts = str(int(ts if ts is not None else time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return {"x-slack-request-timestamp": ts, "x-slack-signature": sig}


def _fresh(monkeypatch, *, secret=SECRET, ws_enabled=True):
    for m in ("slack_webhook", "trigger_grants", "mentions", "reply"):
        sys.modules.pop(m, None)
    import slack_webhook as sw

    state = {"dispatched": [], "requests": [], "seen": set()}

    class _ParamNotFound(Exception):
        pass

    class _SSM:
        exceptions = type("E", (), {"ParameterNotFound": _ParamNotFound})()

        def get_parameter(self, Name, WithDecryption=False):
            if Name == sw.REGISTRY_PARAM:
                return {"Parameter": {"Value": json.dumps(REGISTRY)}}
            if Name.endswith("/signing-secret"):
                if secret is None:
                    raise _ParamNotFound()
                return {"Parameter": {"Value": secret}}
            raise _ParamNotFound()

    class _Lambda:
        def invoke(self, **kw):
            state["dispatched"].append(json.loads(kw["Payload"]))

    monkeypatch.setattr(sw, "_ssm", _SSM())
    monkeypatch.setattr(sw, "_lambda", _Lambda())
    sw._registry._ssm_provider = lambda: _SSM()
    sw._registry._cache = None
    # Stub the dispatch-side reader (its own DDB is exercised in test_trigger_grants).
    monkeypatch.setattr(sw.trigger_grants, "is_workspace_enabled", lambda t: ws_enabled)
    monkeypatch.setattr(
        sw.trigger_grants,
        "put_channel_request",
        lambda **kw: state["requests"].append(kw),
    )
    # Dedup: an in-memory set instead of DynamoDB. The receiver now checks
    # (_already_seen) before processing and records (_mark_seen) only after — so
    # a delivery that fails mid-process is never marked and Slack's retry runs.
    monkeypatch.setattr(sw, "_already_seen", lambda eid: bool(eid) and eid in state["seen"])
    monkeypatch.setattr(sw, "_mark_seen", lambda eid: eid and state["seen"].add(eid))
    return sw, state


def _events_event(body_obj, *, secret=SECRET, ts=None):
    body = json.dumps(body_obj)
    return {"resource": "/slack/events", "headers": _sign(body, secret, ts), "body": body}


def _command_event(fields, *, secret=SECRET, ts=None):
    body = urlencode(fields)
    return {"resource": "/slack/commands", "headers": _sign(body, secret, ts), "body": body}


# --- signature / workspace gating -------------------------------------------


def test_url_verification_challenge(monkeypatch):
    sw, _ = _fresh(monkeypatch)
    ev = _events_event({"type": "url_verification", "challenge": "xyz", "team_id": TEAM})
    resp = sw.handler(ev)
    assert resp["statusCode"] == 200 and resp["body"] == "xyz"


def test_url_verification_without_team_id(monkeypatch):
    # Slack's real url_verification payload carries NO team_id. Verification is
    # app-level, so the challenge must still succeed (regression guard).
    sw, _ = _fresh(monkeypatch)
    ev = _events_event({"type": "url_verification", "challenge": "abc"})
    resp = sw.handler(ev)
    assert resp["statusCode"] == 200 and resp["body"] == "abc"


def test_bad_signature_rejected(monkeypatch):
    sw, _ = _fresh(monkeypatch)
    body = json.dumps({"type": "event_callback", "team_id": TEAM, "event": {}})
    ev = {"resource": "/slack/events", "headers": {"x-slack-request-timestamp": str(int(time.time())),
          "x-slack-signature": "v0=deadbeef"}, "body": body}
    assert sw.handler(ev)["statusCode"] == 401


def test_replay_old_timestamp_rejected(monkeypatch):
    sw, _ = _fresh(monkeypatch)
    ev = _events_event({"type": "url_verification", "challenge": "x", "team_id": TEAM},
                       ts=time.time() - 10_000)
    assert sw.handler(ev)["statusCode"] == 401


def test_unconfigured_signing_secret_returns_503(monkeypatch):
    # The signing secret is app-level; if it isn't stored yet that's a server
    # misconfiguration (503), not a client auth failure.
    sw, _ = _fresh(monkeypatch, secret=None)
    ev = _events_event({"type": "url_verification", "challenge": "x", "team_id": TEAM})
    assert sw.handler(ev)["statusCode"] == 503


def test_disabled_workspace_ignored(monkeypatch):
    sw, state = _fresh(monkeypatch, ws_enabled=False)
    ev = _events_event({"type": "event_callback", "team_id": TEAM, "event_id": "e1",
                        "event": {"type": "app_mention", "text": "<@U0BOT> @workitems go",
                                  "user": "U1", "channel": "C1", "ts": "1.1"}})
    resp = sw.handler(ev)
    assert resp["statusCode"] == 200
    assert state["dispatched"] == []


# --- app_mention dispatch ----------------------------------------------------


def test_app_mention_dispatches(monkeypatch):
    sw, state = _fresh(monkeypatch)
    ev = _events_event({"type": "event_callback", "team_id": TEAM, "event_id": "e1",
                        "event": {"type": "app_mention", "text": "<@U0BOT> @workitems break this up",
                                  "user": "U0ALICE", "channel": "C0ENG", "ts": "111.2"}})
    assert sw.handler(ev)["statusCode"] == 200
    assert len(state["dispatched"]) == 1
    d = state["dispatched"][0]
    assert d["source"] == "slack" and d["agent_id"] == "workitems"
    assert d["sender"] == "slack:T0ACME12:U0ALICE"
    assert d["instruction"] == "break this up"
    assert d["context"] == {"workspace": TEAM, "channel_id": "C0ENG",
                            "thread_ts": "111.2", "message_ts": "111.2",
                            "principal_groups": ["channel:T0ACME12:C0ENG"]}


def test_app_mention_alias_resolves(monkeypatch):
    sw, state = _fresh(monkeypatch)
    ev = _events_event({"type": "event_callback", "team_id": TEAM, "event_id": "e2",
                        "event": {"type": "app_mention", "text": "<@U0BOT> @pm status?",
                                  "user": "U1", "channel": "C1", "ts": "1.1"}})
    sw.handler(ev)
    assert state["dispatched"][0]["agent_id"] == "workitems"  # via alias "pm"


def test_unknown_mention_no_dispatch(monkeypatch):
    sw, state = _fresh(monkeypatch)
    ev = _events_event({"type": "event_callback", "team_id": TEAM, "event_id": "e3",
                        "event": {"type": "app_mention", "text": "<@U0BOT> @nobody hi",
                                  "user": "U1", "channel": "C1", "ts": "1.1"}})
    assert sw.handler(ev)["statusCode"] == 200
    assert state["dispatched"] == []


def test_bot_message_ignored(monkeypatch):
    sw, state = _fresh(monkeypatch)
    ev = _events_event({"type": "event_callback", "team_id": TEAM, "event_id": "e4",
                        "event": {"type": "app_mention", "text": "<@U0BOT> @workitems x",
                                  "user": "U1", "channel": "C1", "ts": "1.1", "bot_id": "B1"}})
    sw.handler(ev)
    assert state["dispatched"] == []


def test_duplicate_event_id_dropped(monkeypatch):
    sw, state = _fresh(monkeypatch)
    mk = lambda: _events_event({"type": "event_callback", "team_id": TEAM, "event_id": "dup",
                                "event": {"type": "app_mention", "text": "<@U0BOT> @workitems x",
                                          "user": "U1", "channel": "C1", "ts": "1.1"}})
    sw.handler(mk())
    sw.handler(mk())
    assert len(state["dispatched"]) == 1


def test_transient_failure_not_marked_seen_so_retry_runs(monkeypatch):
    # A delivery that fails mid-processing must NOT be recorded as seen, so
    # Slack's retry of the same event_id is processed rather than dropped.
    sw, state = _fresh(monkeypatch)
    ev = _events_event({"type": "event_callback", "team_id": TEAM, "event_id": "retry-me",
                        "event": {"type": "app_mention", "text": "<@U0BOT> @workitems x",
                                  "user": "U1", "channel": "C1", "ts": "1.1"}})
    # First delivery: dispatch raises → 500, id not marked.
    calls = {"n": 0}
    orig = sw._process_app_mention
    def flaky(evd, tid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return orig(evd, tid)
    monkeypatch.setattr(sw, "_process_app_mention", flaky)
    assert sw.handler(ev)["statusCode"] == 500
    assert "retry-me" not in state["seen"]
    # Slack retries: now it processes + dispatches, then marks seen.
    assert sw.handler(ev)["statusCode"] == 200
    assert len(state["dispatched"]) == 1
    assert "retry-me" in state["seen"]


# --- slash commands ----------------------------------------------------------


def test_slash_command_mention_dispatches(monkeypatch):
    sw, state = _fresh(monkeypatch)
    ev = _command_event({"command": "/fleet", "text": "@workitems plan the sprint",
                         "team_id": TEAM, "user_id": "U0ALICE", "channel_id": "C0ENG",
                         "channel_name": "eng"})
    resp = sw.handler(ev)
    assert resp["statusCode"] == 200
    assert len(state["dispatched"]) == 1
    d = state["dispatched"][0]
    assert d["agent_id"] == "workitems" and d["trigger_type"] == "slash_command"
    assert d["sender"] == "slack:T0ACME12:U0ALICE"


def test_onboard_channel_files_request(monkeypatch):
    sw, state = _fresh(monkeypatch)
    ev = _command_event({"command": "/onboard-channel", "text": "workitems, researcher",
                         "team_id": TEAM, "user_id": "U0ALICE", "channel_id": "C0ENG",
                         "channel_name": "eng"})
    resp = sw.handler(ev)
    assert resp["statusCode"] == 200
    assert "Request filed" in json.loads(resp["body"])["text"]
    assert state["dispatched"] == []  # a request, not a dispatch
    assert len(state["requests"]) == 1
    req = state["requests"][0]
    assert req["team_id"] == TEAM and req["channel_id"] == "C0ENG"
    assert req["requested_by"] == "slack:T0ACME12:U0ALICE"
    assert req["requested_agents"] == ["workitems", "researcher"]


def test_onboard_channel_no_scope_means_any(monkeypatch):
    sw, state = _fresh(monkeypatch)
    ev = _command_event({"command": "/onboard-channel", "text": "",
                         "team_id": TEAM, "user_id": "U1", "channel_id": "C0ENG",
                         "channel_name": "eng"})
    sw.handler(ev)
    assert state["requests"][0]["requested_agents"] == []


def test_slash_command_unknown_agent_ephemeral(monkeypatch):
    sw, state = _fresh(monkeypatch)
    ev = _command_event({"command": "/fleet", "text": "@ghost do x", "team_id": TEAM,
                         "user_id": "U1", "channel_id": "C1", "channel_name": "c"})
    resp = sw.handler(ev)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["response_type"] == "ephemeral"
    assert state["dispatched"] == []
