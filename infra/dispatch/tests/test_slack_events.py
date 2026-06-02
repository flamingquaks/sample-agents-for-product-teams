"""Unit tests for the Slack Events receiver Lambda.

Security-critical surface: signature verification, event routing, and dispatch.
The module instantiates boto3 clients at import time, so we import it under a
patched boto3.client and swap in MagicMocks for _ssm / _lambda (mirroring the
import style of test_router_guardrail.py).
"""

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SIGNING_SECRET = "test-signing-secret"


@pytest.fixture
def mod():
    """Import slack_events fresh with boto3 mocked, then swap in mock clients."""
    with patch("boto3.client"):
        if "slack_events" in sys.modules:
            del sys.modules["slack_events"]
        import slack_events as slack_mod  # noqa: E402
    slack_mod._ssm = MagicMock()
    slack_mod._lambda = MagicMock()
    yield slack_mod


def _sign(secret, timestamp, body):
    """Compute a valid Slack v0 signature for the given timestamp/body."""
    basestring = f"v0:{timestamp}:{body}".encode()
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return "v0=" + digest


# --- verify_signature --------------------------------------------------------


def test_verify_signature_valid(mod):
    ts = str(int(time.time()))
    body = '{"type":"url_verification"}'
    sig = _sign(SIGNING_SECRET, ts, body)
    assert mod.verify_signature(SIGNING_SECRET, ts, body, sig) is True


def test_verify_signature_tampered_invalid(mod):
    ts = str(int(time.time()))
    body = "real body"
    sig = _sign(SIGNING_SECRET, ts, body)
    # Tamper with the body so the recomputed signature differs.
    assert mod.verify_signature(SIGNING_SECRET, ts, "tampered body", sig) is False
    # Also a flat-out garbage signature.
    assert mod.verify_signature(SIGNING_SECRET, ts, body, "v0=deadbeef") is False


def test_verify_signature_empty_timestamp_or_signature(mod):
    ts = str(int(time.time()))
    body = "x"
    sig = _sign(SIGNING_SECRET, ts, body)
    assert mod.verify_signature(SIGNING_SECRET, "", body, sig) is False
    assert mod.verify_signature(SIGNING_SECRET, ts, body, "") is False


def test_verify_signature_replay_rejected(mod):
    old_ts = str(int(time.time()) - 10000)
    body = "x"
    sig = _sign(SIGNING_SECRET, old_ts, body)
    assert mod.verify_signature(SIGNING_SECRET, old_ts, body, sig) is False


def test_verify_signature_non_numeric_timestamp(mod):
    body = "x"
    sig = _sign(SIGNING_SECRET, "not-a-number", body)
    assert mod.verify_signature(SIGNING_SECRET, "not-a-number", body, sig) is False


# --- strip_bot_mention -------------------------------------------------------


def test_strip_bot_mention(mod):
    assert mod.strip_bot_mention("<@U123> hello") == "hello"
    assert mod.strip_bot_mention("<@U123>   @workitems do x") == "@workitems do x"
    assert mod.strip_bot_mention("no mention here") == "no mention here"


# --- process_app_mention -----------------------------------------------------


def test_process_app_mention_uses_router_no_agent_id(mod):
    event = {
        "text": "<@UBOT> @workitems do the thing",
        "channel": "C1",
        "user": "U9",
        "ts": "111.1",
        "thread_ts": "100.0",
        "team": "T1",
    }
    with patch.object(mod, "dispatch_to_router") as mock_router, \
         patch.object(mod, "add_reaction") as mock_react, \
         patch.object(mod, "dispatch") as mock_dispatch:
        mod.process_app_mention(event, "xoxb-token")

    mock_react.assert_called_once_with("xoxb-token", "C1", "111.1", "eyes")
    mock_router.assert_called_once()
    # Must NOT use the pre-resolved dispatch (no agent_id).
    mock_dispatch.assert_not_called()

    kwargs = mock_router.call_args.kwargs
    assert kwargs["trigger_type"] == "mention"
    assert kwargs["body"] == "@workitems do the thing"
    assert kwargs["sender"] == "U9"
    assert "agent_id" not in kwargs
    ctx = kwargs["context"]
    assert ctx["channel_id"] == "C1"
    assert ctx["thread_ts"] == "100.0"
    assert ctx["message_ts"] == "111.1"
    assert ctx["team_id"] == "T1"
    assert ctx["user_id"] == "U9"


def test_process_app_mention_thread_ts_falls_back_to_ts(mod):
    event = {
        "text": "<@UBOT> hi",
        "channel": "C1",
        "user": "U9",
        "ts": "222.2",
        "team": "T1",
    }
    with patch.object(mod, "dispatch_to_router") as mock_router, \
         patch.object(mod, "add_reaction"):
        mod.process_app_mention(event, "tok")
    ctx = mock_router.call_args.kwargs["context"]
    assert ctx["thread_ts"] == "222.2"
    assert ctx["message_ts"] == "222.2"


# --- process_assistant_thread ------------------------------------------------


def test_process_assistant_thread_dispatches_workitems_dm(mod):
    event = {
        "text": "decompose this epic",
        "channel": "D1",
        "user": "U5",
        "ts": "333.3",
        "team": "T2",
    }
    with patch.object(mod, "set_typing_indicator") as mock_typing, \
         patch.object(mod, "dispatch") as mock_dispatch:
        mod.process_assistant_thread(event, "tok")

    mock_typing.assert_called_once()
    mock_dispatch.assert_called_once()
    kwargs = mock_dispatch.call_args.kwargs
    assert kwargs["agent_id"] == "workitems"
    assert kwargs["trigger_type"] == "assistant_thread"
    assert kwargs["instruction"] == "decompose this epic"
    assert kwargs["sender"] == "U5"
    assert kwargs["context"]["is_dm"] is True


def test_process_assistant_thread_with_mention_delegates_to_router(mod):
    # A DM that names a specific agent must NOT be hard-routed to workitems —
    # it goes to the Router (no pre-resolved agent_id) for registry resolution.
    event = {
        "text": "@researcher analyze the competitive landscape",
        "channel": "D1",
        "user": "U5",
        "ts": "333.3",
        "team": "T2",
    }
    with patch.object(mod, "set_typing_indicator"), \
         patch.object(mod, "dispatch") as mock_dispatch, \
         patch.object(mod, "dispatch_to_router") as mock_router:
        mod.process_assistant_thread(event, "tok")

    mock_dispatch.assert_not_called()  # not pre-resolved to workitems
    mock_router.assert_called_once()
    kwargs = mock_router.call_args.kwargs
    assert kwargs["trigger_type"] == "assistant_thread"
    assert kwargs["body"] == "@researcher analyze the competitive landscape"
    assert kwargs["context"]["is_dm"] is True


# --- process_slash_command ---------------------------------------------------


def test_process_slash_command_workitems_dispatches_and_acks(mod):
    with patch.object(mod, "dispatch") as mock_dispatch, \
         patch.object(mod.requests, "post") as mock_post:
        mod.process_slash_command(
            "/workitems", "generate a report", "U1", "C1", "https://hooks/resp", "trig1"
        )

    mock_dispatch.assert_called_once()
    kwargs = mock_dispatch.call_args.kwargs
    assert kwargs["agent_id"] == "workitems"
    assert kwargs["trigger_type"] == "slash_command"
    assert kwargs["instruction"] == "generate a report"
    # Ephemeral ack posted to response_url.
    mock_post.assert_called_once()
    assert mock_post.call_args.args[0] == "https://hooks/resp"
    assert mock_post.call_args.kwargs["json"]["response_type"] == "ephemeral"


def test_process_slash_command_empty_text_usage_error(mod):
    with patch.object(mod, "dispatch") as mock_dispatch, \
         patch.object(mod, "_slash_error") as mock_err:
        mod.process_slash_command("/workitems", "   ", "U1", "C1", "https://hooks/resp", "trig1")
    mock_dispatch.assert_not_called()
    mock_err.assert_called_once()
    # Usage message references the command.
    assert "/workitems" in mock_err.call_args.args[1]


def test_process_slash_command_unknown_command_error(mod):
    with patch.object(mod, "dispatch") as mock_dispatch, \
         patch.object(mod, "_slash_error") as mock_err:
        mod.process_slash_command("/bogus", "anything", "U1", "C1", "https://hooks/resp", "trig1")
    mock_dispatch.assert_not_called()
    mock_err.assert_called_once()
    assert "Unknown command" in mock_err.call_args.args[1]


def test_process_slash_command_fleet_routes_to_handler(mod):
    with patch.object(mod, "handle_fleet_command") as mock_fleet, \
         patch.object(mod, "dispatch") as mock_dispatch:
        mod.process_slash_command("/fleet", "status", "U1", "C1", "https://hooks/resp", "trig1")
    mock_fleet.assert_called_once_with("status", "https://hooks/resp")
    mock_dispatch.assert_not_called()


# --- handle_fleet_command ----------------------------------------------------


@pytest.mark.parametrize(
    "text,marker",
    [
        ("status", "operational"),
        ("budget", "Budget"),
        ("health", "healthy"),
        ("nonsense", "Unknown fleet subcommand"),
    ],
)
def test_handle_fleet_command_branches(mod, text, marker):
    with patch.object(mod.requests, "post") as mock_post:
        mod.handle_fleet_command(text, "https://hooks/resp")
    mock_post.assert_called_once()
    assert marker in mock_post.call_args.kwargs["json"]["text"]


# --- handler (end-to-end) ----------------------------------------------------


def _signed_event(mod, body, path="/slack/events", content_type="application/json"):
    ts = str(int(time.time()))
    sig = _sign(SIGNING_SECRET, ts, body)
    return {
        "headers": {
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
            "Content-Type": content_type,
        },
        "body": body,
        "path": path,
    }


def test_handler_signing_secret_fetch_failure_returns_503(mod):
    mod._ssm.get_parameter.side_effect = Exception("ssm down")
    resp = mod.handler(_signed_event(mod, '{"type":"url_verification"}'), None)
    assert resp["statusCode"] == 503


def test_handler_invalid_signature_returns_401(mod):
    mod._ssm.get_parameter.return_value = {"Parameter": {"Value": SIGNING_SECRET}}
    with patch.object(mod, "verify_signature", return_value=False):
        resp = mod.handler(_signed_event(mod, '{"type":"url_verification"}'), None)
    assert resp["statusCode"] == 401


def test_handler_url_verification_echoes_challenge(mod):
    mod._ssm.get_parameter.return_value = {"Parameter": {"Value": SIGNING_SECRET}}
    body = json.dumps({"type": "url_verification", "challenge": "abc123"})
    with patch.object(mod, "verify_signature", return_value=True):
        resp = mod.handler(_signed_event(mod, body), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["challenge"] == "abc123"


def test_handler_non_event_callback_returns_ok(mod):
    mod._ssm.get_parameter.return_value = {"Parameter": {"Value": SIGNING_SECRET}}
    body = json.dumps({"type": "something_else"})
    with patch.object(mod, "verify_signature", return_value=True):
        resp = mod.handler(_signed_event(mod, body), None)
    assert resp["statusCode"] == 200
    assert resp["body"] == "ok"


def test_handler_empty_signing_secret_returns_503(mod):
    # An empty secret must hard-fail, not verify against "".
    mod._ssm.get_parameter.return_value = {"Parameter": {"Value": ""}}
    body = json.dumps({"type": "url_verification", "challenge": "x"})
    resp = mod.handler(_signed_event(mod, body), None)
    assert resp["statusCode"] == 503


def test_handler_base64_encoded_body_is_decoded_before_verify(mod):
    # API Gateway base64-encodes some bodies. The handler must decode to the raw
    # bytes Slack signed BEFORE verifying, so a correctly-signed base64 request
    # passes and the challenge round-trips.
    import base64
    mod._ssm.get_parameter.return_value = {"Parameter": {"Value": SIGNING_SECRET}}
    raw = json.dumps({"type": "url_verification", "challenge": "deadbeef"})
    ts = str(int(time.time()))
    sig = _sign(SIGNING_SECRET, ts, raw)  # signature over the RAW (decoded) body
    event = {
        "headers": {
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
            "Content-Type": "application/json",
        },
        "body": base64.b64encode(raw.encode()).decode(),
        "isBase64Encoded": True,
        "path": "/slack/events",
    }
    resp = mod.handler(event, None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["challenge"] == "deadbeef"


def test_handler_app_mention_invokes_processor(mod):
    # First get_parameter -> signing secret, second -> bot token.
    mod._ssm.get_parameter.side_effect = [
        {"Parameter": {"Value": SIGNING_SECRET}},
        {"Parameter": {"Value": "xoxb-bot-token"}},
    ]
    body = json.dumps(
        {"type": "event_callback", "event": {"type": "app_mention", "text": "<@U> hi"}}
    )
    with patch.object(mod, "verify_signature", return_value=True), \
         patch.object(mod, "process_app_mention") as mock_proc:
        resp = mod.handler(_signed_event(mod, body), None)
    assert resp["statusCode"] == 200
    mock_proc.assert_called_once()
    # Bot token was fetched (two SSM reads) and passed to the processor.
    assert mod._ssm.get_parameter.call_count == 2
    assert mock_proc.call_args.args[1] == "xoxb-bot-token"


def test_handler_bot_message_not_processed(mod):
    mod._ssm.get_parameter.side_effect = [
        {"Parameter": {"Value": SIGNING_SECRET}},
        {"Parameter": {"Value": "xoxb-bot-token"}},
    ]
    body = json.dumps(
        {"type": "event_callback", "event": {"type": "app_mention", "bot_id": "B1"}}
    )
    with patch.object(mod, "verify_signature", return_value=True), \
         patch.object(mod, "process_app_mention") as mock_proc:
        resp = mod.handler(_signed_event(mod, body), None)
    assert resp["statusCode"] == 200
    mock_proc.assert_not_called()


def test_handler_slash_path_routes_to_slash_handler(mod):
    mod._ssm.get_parameter.return_value = {"Parameter": {"Value": SIGNING_SECRET}}
    body = "command=%2Fworkitems&text=do+x&user_id=U1&channel_id=C1&response_url=https%3A%2F%2Fh&trigger_id=t1"
    event = _signed_event(
        mod, body, path="/slack/slash", content_type="application/x-www-form-urlencoded"
    )
    with patch.object(mod, "verify_signature", return_value=True), \
         patch.object(mod, "process_slash_command") as mock_proc:
        resp = mod.handler(event, None)
    assert resp["statusCode"] == 200
    mock_proc.assert_called_once()
    # parse_qs decoded the command correctly.
    assert mock_proc.call_args.args[0] == "/workitems"


def test_handler_processor_exception_returns_500(mod):
    mod._ssm.get_parameter.side_effect = [
        {"Parameter": {"Value": SIGNING_SECRET}},
        {"Parameter": {"Value": "xoxb-bot-token"}},
    ]
    body = json.dumps(
        {"type": "event_callback", "event": {"type": "app_mention", "text": "x"}}
    )
    with patch.object(mod, "verify_signature", return_value=True), \
         patch.object(mod, "process_app_mention", side_effect=RuntimeError("boom")):
        resp = mod.handler(_signed_event(mod, body), None)
    assert resp["statusCode"] == 500
