"""Tests for the Slack webhook receiver (slack_webhook.py).

Covers the security contract (v0 signature verification, replay window,
per-workspace secret selection, unknown/disabled workspace rejection), event
handling (url_verification, app_mention → dispatch, bot-loop, dedup), and slash
commands (mention dispatch + the /sdlc-onboard-channel request flow). External
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


def _fresh(monkeypatch, *, secret=SECRET, ws_enabled=True, channel_repos=None):
    for m in ("slack_webhook", "trigger_grants", "mentions", "reply", "slack_modals", "slack_notify"):
        sys.modules.pop(m, None)
    import slack_webhook as sw

    state = {"dispatched": [], "requests": [], "seen": set(),
             "modals": [], "posted": []}

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
    # Modal plumbing: capture views.open + visible confirmations; channel repo
    # grants come from the fixture arg.
    monkeypatch.setattr(
        sw.slack_notify, "open_modal",
        lambda **kw: state["modals"].append(kw) or True,
    )
    monkeypatch.setattr(sw.slack_notify, "onboarded_repos", lambda: ["acme/web", "acme/api"])
    monkeypatch.setattr(
        sw.trigger_grants, "channel_repos",
        lambda t, c: list(channel_repos or []),
    )
    monkeypatch.setattr(
        sw.reply, "post_slack_message",
        lambda team, chan, text, thread_ts=None, agent_id=None: state["posted"].append(
            {"team": team, "channel": chan, "text": text, "agent_id": agent_id}) or True,
    )
    # The modal path posts via post_slack_message_ts to capture the thread anchor.
    monkeypatch.setattr(
        sw.reply, "post_slack_message_ts",
        lambda team, chan, text, thread_ts=None, agent_id=None: (
            state["posted"].append(
                {"team": team, "channel": chan, "text": text, "agent_id": agent_id}
            ) or (True, "1700000000.000100")
        ),
    )
    # users.info profile lookup: default to an empty profile (no verified name /
    # email captured) so a test that doesn't care about it sees the bare context.
    # Tests exercising capture override state["profile"].
    state["profile"] = {"display_name": "", "email": ""}
    monkeypatch.setattr(
        sw.reply, "slack_user_profile",
        lambda team, user: dict(state["profile"]),
    )
    return sw, state


def _interaction_event(payload, *, secret=SECRET, ts=None):
    body = urlencode({"payload": json.dumps(payload)})
    return {"resource": "/slack/interactions", "headers": _sign(body, secret, ts), "body": body}


def _view_submission(callback_id, private_metadata, values, user="U0ALICE"):
    return {
        "type": "view_submission",
        "team": {"id": TEAM},
        "user": {"id": user},
        "view": {
            "callback_id": callback_id,
            "private_metadata": json.dumps(private_metadata),
            "state": {"values": values},
        },
    }


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
                            "repo": "", "repos": [],
                            "principal_groups": ["channel:T0ACME12:C0ENG"]}


def _mention_ev(text, event_id="e-repo"):
    return _events_event({"type": "event_callback", "team_id": TEAM, "event_id": event_id,
                          "event": {"type": "app_mention", "text": text,
                                    "user": "U0ALICE", "channel": "C0ENG", "ts": "111.2"}})


def test_app_mention_attaches_channel_approved_repos(monkeypatch):
    # The channel's approved repo grants become the dispatch's codebase scope —
    # without this the agent has no bridge to any repo and the gateway
    # interceptor refuses every GitHub tool call (no dispatch origin).
    sw, state = _fresh(monkeypatch, channel_repos=["acme/web", "acme/api"])
    sw.handler(_mention_ev("<@U0BOT> @workitems break this up"))
    d = state["dispatched"][0]
    assert d["context"]["repo"] == "acme/web"  # first grant = origin
    assert d["context"]["repos"] == ["acme/web", "acme/api"]
    assert "acme/web" in d["instruction"]  # scope stated in the instruction


def test_app_mention_named_approved_repo_becomes_origin(monkeypatch):
    # Naming an approved repo in the message pins it as the dispatch origin.
    sw, state = _fresh(monkeypatch, channel_repos=["acme/web", "acme/api"])
    sw.handler(_mention_ev("<@U0BOT> @workitems fix the login bug in acme/api"))
    d = state["dispatched"][0]
    assert d["context"]["repo"] == "acme/api"
    assert d["context"]["repos"] == ["acme/web", "acme/api"]


def test_app_mention_unapproved_named_repo_not_honored(monkeypatch):
    # A repo named in the message but NOT approved for the channel must not
    # become the origin — the channel grant is the authorization boundary.
    sw, state = _fresh(monkeypatch, channel_repos=["acme/web"])
    sw.handler(_mention_ev("<@U0BOT> @workitems look at secret/repo please"))
    d = state["dispatched"][0]
    assert d["context"]["repo"] == "acme/web"
    assert "secret/repo" not in d["context"]["repos"]


def test_app_mention_captures_verified_profile(monkeypatch):
    """When users.info resolves a name + email, the dispatch context carries the
    verified sender_name + requester_email (with requester_email_verified) so the
    router's identity map stores a friendly name and joins on email (§16.5)."""
    sw, state = _fresh(monkeypatch)
    state["profile"] = {"display_name": "Alice Anderson", "email": "alice@example.com"}
    ev = _events_event({"type": "event_callback", "team_id": TEAM, "event_id": "ep1",
                        "event": {"type": "app_mention", "text": "<@U0BOT> @workitems go",
                                  "user": "U0ALICE", "channel": "C0ENG", "ts": "1.1"}})
    sw.handler(ev)
    ctx = state["dispatched"][0]["context"]
    assert ctx["sender_name"] == "Alice Anderson"
    assert ctx["requester_email"] == "alice@example.com"
    assert ctx["requester_email_verified"] is True


def test_app_mention_partial_profile_omits_missing_keys(monkeypatch):
    """A name but no email: the name is captured, but no unverified email leaks
    into the context (email stays absent, verified flag not set)."""
    sw, state = _fresh(monkeypatch)
    state["profile"] = {"display_name": "Bob", "email": ""}
    ev = _events_event({"type": "event_callback", "team_id": TEAM, "event_id": "ep2",
                        "event": {"type": "app_mention", "text": "<@U0BOT> @workitems go",
                                  "user": "U0BOB", "channel": "C0ENG", "ts": "1.1"}})
    sw.handler(ev)
    ctx = state["dispatched"][0]["context"]
    assert ctx["sender_name"] == "Bob"
    assert "requester_email" not in ctx
    assert "requester_email_verified" not in ctx


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
    ev = _command_event({"command": "/sdlc-onboard-channel", "text": "workitems, researcher",
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


def test_onboard_channel_without_text_opens_modal(monkeypatch):
    # Bare /sdlc-onboard-channel is now interactive: it opens the agent+repo
    # picker modal instead of filing an unscoped request.
    sw, state = _fresh(monkeypatch)
    ev = _command_event({"command": "/sdlc-onboard-channel", "text": "",
                         "team_id": TEAM, "user_id": "U1", "channel_id": "C0ENG",
                         "channel_name": "eng", "trigger_id": "trig.1"})
    resp = sw.handler(ev)
    assert resp["statusCode"] == 200
    assert state["requests"] == []  # nothing filed yet — the modal submit files
    assert len(state["modals"]) == 1
    view = state["modals"][0]["view"]
    assert view["callback_id"] == "sdlc_onboard_channel"
    meta = json.loads(view["private_metadata"])
    assert meta["channel_id"] == "C0ENG"
    assert meta["repos"] == ["acme/web", "acme/api"]
    assert meta["agents"] == ["workitems"]  # from the registry


def test_onboard_modal_submit_files_request_and_posts(monkeypatch):
    sw, state = _fresh(monkeypatch)
    import slack_modals

    meta = {"team_id": TEAM, "channel_id": "C0ENG", "channel_name": "eng",
            "agents": ["workitems", "researcher"], "repos": ["acme/web", "acme/api"]}
    values = {
        "agents": {"selected": {"selected_options": [{"value": "0"}]}},
        "repos": {"selected": {"selected_options": [{"value": "1"}]}},
    }
    ev = _interaction_event(
        _view_submission(slack_modals.ONBOARD_VIEW_CALLBACK, meta, values))
    resp = sw.handler(ev)
    assert resp["statusCode"] == 200
    assert len(state["requests"]) == 1
    req = state["requests"][0]
    assert req["requested_agents"] == ["workitems"]
    assert req["requested_repos"] == ["acme/api"]
    assert req["requested_by"] == "slack:T0ACME12:U0ALICE"
    # Visible in-channel confirmation posted.
    assert state["posted"] and state["posted"][0]["channel"] == "C0ENG"


def test_message_agent_command_opens_modal_with_channel_repos(monkeypatch):
    sw, state = _fresh(monkeypatch, channel_repos=["acme/web"])
    ev = _command_event({"command": "/sdlc-message-agent", "text": "",
                         "team_id": TEAM, "user_id": "U1", "channel_id": "C0ENG",
                         "channel_name": "eng", "trigger_id": "trig.2"})
    resp = sw.handler(ev)
    assert resp["statusCode"] == 200
    view = state["modals"][0]["view"]
    assert view["callback_id"] == "sdlc_message_agent"
    meta = json.loads(view["private_metadata"])
    # Only the CHANNEL'S approved repos are offered, not the whole fleet.
    assert meta["repos"] == ["acme/web"]


def test_message_agent_submit_dispatches_and_posts_visibly(monkeypatch):
    sw, state = _fresh(monkeypatch, channel_repos=["acme/web"])
    import slack_modals

    meta = {"team_id": TEAM, "channel_id": "C0ENG", "channel_name": "eng",
            "agents": ["workitems"], "repos": ["acme/web"]}
    values = {
        "agent": {"selected": {"selected_option": {"value": "0"}}},
        "repos": {"selected": {"selected_options": [{"value": "0"}]}},
        "message": {"text": {"value": "Plan the sprint"}},
    }
    ev = _interaction_event(
        _view_submission(slack_modals.MESSAGE_VIEW_CALLBACK, meta, values))
    resp = sw.handler(ev)
    assert resp["statusCode"] == 200
    assert len(state["dispatched"]) == 1
    d = state["dispatched"][0]
    assert d["agent_id"] == "workitems"
    assert d["sender"] == "slack:T0ACME12:U0ALICE"
    # The first selected repo anchors the dispatch origin for co-repo mechanics.
    assert d["context"]["repo"] == "acme/web"
    assert d["context"]["repos"] == ["acme/web"]
    assert "Plan the sprint" in d["instruction"]
    # Visible in-channel confirmation.
    assert state["posted"] and "workitems" in state["posted"][0]["text"]
    # The confirmation's ts becomes the run's thread anchor: the router's ack
    # and the final result thread under it instead of new channel posts.
    assert d["context"]["thread_ts"] == "1700000000.000100"


def test_message_agent_submit_rejects_revoked_repo(monkeypatch):
    # The grant may be revoked between modal open and submit — the submit
    # re-checks the LIVE channel grant and keeps the modal open with an error.
    sw, state = _fresh(monkeypatch, channel_repos=[])  # revoked by submit time
    import slack_modals

    meta = {"team_id": TEAM, "channel_id": "C0ENG", "channel_name": "eng",
            "agents": ["workitems"], "repos": ["acme/web"]}
    values = {
        "agent": {"selected": {"selected_option": {"value": "0"}}},
        "repos": {"selected": {"selected_options": [{"value": "0"}]}},
        "message": {"text": {"value": "Plan"}},
    }
    ev = _interaction_event(
        _view_submission(slack_modals.MESSAGE_VIEW_CALLBACK, meta, values))
    resp = sw.handler(ev)
    body = json.loads(resp["body"])
    assert body["response_action"] == "errors"
    assert state["dispatched"] == []


def test_slash_command_unknown_agent_ephemeral(monkeypatch):
    sw, state = _fresh(monkeypatch)
    ev = _command_event({"command": "/fleet", "text": "@ghost do x", "team_id": TEAM,
                         "user_id": "U1", "channel_id": "C1", "channel_name": "c"})
    resp = sw.handler(ev)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["response_type"] == "ephemeral"
    assert state["dispatched"] == []


# --- bound-thread replies (durable-repo-work spec) -----------------------------


def _thread_mention_ev(text, thread_ts="100.1", event_id="e-thread"):
    return _events_event({"type": "event_callback", "team_id": TEAM, "event_id": event_id,
                          "event": {"type": "app_mention", "text": text,
                                    "user": "U0ALICE", "channel": "C0ENG",
                                    "ts": "111.2", "thread_ts": thread_ts}})


def _bind(monkeypatch, sw, *, assignment_id="a-9", agent_id="workitems", status="awaiting_input"):
    monkeypatch.setattr(
        sw, "_thread_binding",
        lambda team, chan, ts: {"bound_assignment_id": assignment_id, "agent_id": agent_id}
        if ts == "100.1" else None,
    )
    monkeypatch.setattr(sw, "_bound_assignment_status", lambda aid: status)


def test_reply_on_paused_thread_resumes(monkeypatch):
    """An in-thread reply on an awaiting_input assignment dispatches a RESUME —
    same assignment, the reply is the answer, no agent name needed."""
    sw, state = _fresh(monkeypatch)
    _bind(monkeypatch, sw, status="awaiting_input")
    sw.handler(_thread_mention_ev("<@U0BOT> use us-east-1 please"))
    assert len(state["dispatched"]) == 1
    d = state["dispatched"][0]
    assert d["resume_of"] == "a-9"
    assert d["body"] == "use us-east-1 please"
    assert d["sender"] == "slack:T0ACME12:U0ALICE"
    assert "agent_id" not in d  # resolved from the binding by the router


def test_reply_on_completed_thread_starts_linked_assignment(monkeypatch):
    """D8: a reply on a completed thread is a NEW assignment linked via
    parent_assignment_id — agent resolved from the binding, not the text."""
    sw, state = _fresh(monkeypatch)
    _bind(monkeypatch, sw, status="completed", agent_id="workitems")
    sw.handler(_thread_mention_ev("<@U0BOT> also add error handling"))
    assert len(state["dispatched"]) == 1
    d = state["dispatched"][0]
    assert d["parent_assignment_id"] == "a-9"
    assert d["agent_id"] == "workitems"
    assert d["instruction"] == "also add error handling"
    assert d["context"]["thread_ts"] == "100.1"


def test_reply_on_in_flight_thread_falls_through(monkeypatch):
    """A bound thread whose assignment is still running gets NORMAL mention
    handling (which needs an agent name; free text without one is dropped)."""
    sw, state = _fresh(monkeypatch)
    _bind(monkeypatch, sw, status="dispatched")
    sw.handler(_thread_mention_ev("<@U0BOT> how is it going?"))
    assert state["dispatched"] == []


def test_unbound_thread_mention_still_dispatches_normally(monkeypatch):
    sw, state = _fresh(monkeypatch)
    monkeypatch.setattr(sw, "_thread_binding", lambda *a: None)
    sw.handler(_thread_mention_ev("<@U0BOT> @workitems plan this"))
    assert len(state["dispatched"]) == 1
    assert state["dispatched"][0]["agent_id"] == "workitems"
