"""Unit tests for the Slack Webhook Receiver Lambda.

Covers the cases specified in the deliverable:
- Signature verification: valid, bad HMAC, expired timestamp, missing v0= prefix
- URL verification echo (Slack challenge) — now requires valid signature
- app_mention parsing with <@BOT_ID> prefix stripping
- Slash-command form parsing and ephemeral ack shape
- Async dispatch invokes the router with the right payload shape
"""

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import slack_webhook  # noqa: E402


# ---------------------------------------------------------------------------
# Reset module-level signing-secret cache between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_signing_secret_cache():
    """Clear the module-level signing-secret cache before each test."""
    slack_webhook._cached_signing_secret = None
    slack_webhook._cached_signing_secret_at = 0.0
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signature(body: str, secret: str, ts: str | None = None) -> tuple[str, str]:
    """Return (timestamp, signature) for a valid Slack request."""
    ts = ts or str(int(time.time()))
    sig_base = f"v0:{ts}:{body}"
    digest = hmac.new(secret.encode(), sig_base.encode(), hashlib.sha256).hexdigest()
    return ts, f"v0={digest}"


def _make_event(body: str, path: str = "/slack/events", secret: str = "test-secret", ts: str | None = None) -> dict:
    """Build a minimal API Gateway event dict with valid Slack headers."""
    timestamp, signature = _make_signature(body, secret, ts)
    return {
        "path": path,
        "headers": {
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": signature,
        },
        "body": body,
    }


def _ssm_mock(secret: str = "test-secret") -> MagicMock:
    mock = MagicMock()
    mock.get_parameter.return_value = {"Parameter": {"Value": secret}}
    return mock


# ---------------------------------------------------------------------------
# URL Verification
# ---------------------------------------------------------------------------


class TestUrlVerification:
    def test_echoes_challenge(self):
        """Slack signs url_verification challenges; handler must verify before echoing."""
        body = json.dumps({"type": "url_verification", "challenge": "abc123"})
        event = _make_event(body)
        with patch.object(slack_webhook, "_ssm", _ssm_mock()):
            resp = slack_webhook.handler(event, {})
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["challenge"] == "abc123"

    def test_echoes_empty_challenge(self):
        body = json.dumps({"type": "url_verification", "challenge": ""})
        event = _make_event(body)
        with patch.object(slack_webhook, "_ssm", _ssm_mock()):
            resp = slack_webhook.handler(event, {})
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["challenge"] == ""

    def test_challenge_without_signature_rejected(self):
        """url_verification without a valid signature must be rejected (S-B3)."""
        body = json.dumps({"type": "url_verification", "challenge": "abc123"})
        event = {"path": "/slack/events", "headers": {}, "body": body}
        with patch.object(slack_webhook, "_ssm", _ssm_mock()):
            resp = slack_webhook.handler(event, {})
        assert resp["statusCode"] == 401


# ---------------------------------------------------------------------------
# Signature Verification
# ---------------------------------------------------------------------------


class TestSignatureVerification:
    def test_valid_signature_passes(self):
        body = json.dumps({"type": "event_callback", "event": {"type": "app_mention", "text": "<@U123> @workitems do something", "user": "U456", "channel": "C789", "ts": "111.222"}})
        event = _make_event(body)
        with patch.object(slack_webhook, "_ssm", _ssm_mock()), \
             patch.object(slack_webhook, "lambda_client") as mock_lc:
            resp = slack_webhook.handler(event, {})
        assert resp["statusCode"] == 200

    def test_bad_hmac_rejected(self):
        body = json.dumps({"type": "event_callback", "event": {}})
        ts = str(int(time.time()))
        event = {
            "path": "/slack/events",
            "headers": {
                "x-slack-request-timestamp": ts,
                "x-slack-signature": "v0=deadbeefdeadbeef",
            },
            "body": body,
        }
        with patch.object(slack_webhook, "_ssm", _ssm_mock()):
            resp = slack_webhook.handler(event, {})
        assert resp["statusCode"] == 401

    def test_expired_timestamp_rejected(self):
        """Requests older than 5 minutes must be rejected (replay prevention)."""
        body = json.dumps({"type": "event_callback", "event": {}})
        stale_ts = str(int(time.time()) - 400)  # 6+ minutes ago
        _, sig = _make_signature(body, "test-secret", stale_ts)
        event = {
            "path": "/slack/events",
            "headers": {
                "x-slack-request-timestamp": stale_ts,
                "x-slack-signature": sig,
            },
            "body": body,
        }
        with patch.object(slack_webhook, "_ssm", _ssm_mock()):
            resp = slack_webhook.handler(event, {})
        assert resp["statusCode"] == 401

    def test_missing_headers_rejected(self):
        body = json.dumps({"type": "event_callback", "event": {}})
        event = {"path": "/slack/events", "headers": {}, "body": body}
        with patch.object(slack_webhook, "_ssm", _ssm_mock()):
            resp = slack_webhook.handler(event, {})
        assert resp["statusCode"] == 401

    def test_empty_signing_secret_fails_closed(self):
        """A misconfigured empty signing secret must return 503, not 200."""
        body = json.dumps({"type": "event_callback", "event": {}})
        ts = str(int(time.time()))
        _, sig = _make_signature(body, "", ts)
        event = {
            "path": "/slack/events",
            "headers": {
                "x-slack-request-timestamp": ts,
                "x-slack-signature": sig,
            },
            "body": body,
        }
        mock_ssm = _ssm_mock(secret="")
        with patch.object(slack_webhook, "_ssm", mock_ssm):
            resp = slack_webhook.handler(event, {})
        assert resp["statusCode"] == 503

    def test_signature_without_v0_prefix_rejected(self):
        """Signatures lacking the v0= prefix must be rejected with 401 (S-M2)."""
        body = json.dumps({"type": "event_callback", "event": {}})
        ts = str(int(time.time()))
        sig_base = f"v0:{ts}:{body}"
        digest = hmac.new("test-secret".encode(), sig_base.encode(), hashlib.sha256).hexdigest()
        event = {
            "path": "/slack/events",
            "headers": {
                "x-slack-request-timestamp": ts,
                "x-slack-signature": digest,  # missing v0= prefix
            },
            "body": body,
        }
        with patch.object(slack_webhook, "_ssm", _ssm_mock()):
            resp = slack_webhook.handler(event, {})
        assert resp["statusCode"] == 401


# ---------------------------------------------------------------------------
# app_mention parsing
# ---------------------------------------------------------------------------


class TestAppMention:
    def _mention_event(self, text: str, user: str = "U456", channel: str = "C789", ts: str = "111.222", thread_ts: str | None = None) -> dict:
        inner = {"type": "app_mention", "text": text, "user": user, "channel": channel, "ts": ts}
        if thread_ts:
            inner["thread_ts"] = thread_ts
        body = json.dumps({"type": "event_callback", "event": inner})
        return _make_event(body)

    def test_strips_bot_mention_prefix(self):
        """<@U123> prefix must be stripped before @agent resolution."""
        event = self._mention_event("<@U123BOT> @workitems break this into issues")
        with patch.object(slack_webhook, "_ssm", _ssm_mock()), \
             patch.object(slack_webhook, "lambda_client") as mock_lc:
            resp = slack_webhook.handler(event, {})
        assert resp["statusCode"] == 200
        payload = json.loads(mock_lc.invoke.call_args.kwargs["Payload"])
        assert payload["agent_id"] == "workitems"
        assert payload["instruction"] == "break this into issues"

    def test_resolves_alias(self):
        """Agent aliases (e.g. @docs → docwriter) must resolve correctly."""
        event = self._mention_event("<@U123BOT> @docs update the readme")
        with patch.object(slack_webhook, "_ssm", _ssm_mock()), \
             patch.object(slack_webhook, "lambda_client") as mock_lc:
            slack_webhook.handler(event, {})
        payload = json.loads(mock_lc.invoke.call_args.kwargs["Payload"])
        assert payload["agent_id"] == "docwriter"

    def test_uses_existing_thread_ts(self):
        """If the mention is already in a thread, thread_ts should carry through."""
        event = self._mention_event("<@U123> @workitems status", thread_ts="999.000")
        with patch.object(slack_webhook, "_ssm", _ssm_mock()), \
             patch.object(slack_webhook, "lambda_client") as mock_lc:
            slack_webhook.handler(event, {})
        payload = json.loads(mock_lc.invoke.call_args.kwargs["Payload"])
        assert payload["context"]["thread_ts"] == "999.000"

    def test_falls_back_to_event_ts_when_no_thread(self):
        """When no thread_ts, the message's own ts becomes the thread anchor."""
        event = self._mention_event("<@U123> @workitems status")
        with patch.object(slack_webhook, "_ssm", _ssm_mock()), \
             patch.object(slack_webhook, "lambda_client") as mock_lc:
            slack_webhook.handler(event, {})
        payload = json.loads(mock_lc.invoke.call_args.kwargs["Payload"])
        assert payload["context"]["thread_ts"] == "111.222"

    def test_sender_is_user_id(self):
        """sender must be the immutable user ID, not a display name (T-4)."""
        event = self._mention_event("<@U123> @workitems do x", user="UIMMUTABLE")
        with patch.object(slack_webhook, "_ssm", _ssm_mock()), \
             patch.object(slack_webhook, "lambda_client") as mock_lc:
            slack_webhook.handler(event, {})
        payload = json.loads(mock_lc.invoke.call_args.kwargs["Payload"])
        assert payload["sender"] == "UIMMUTABLE"

    def test_unknown_mention_ignored(self):
        """A mention with no recognized @agent token must return 200 silently."""
        event = self._mention_event("<@U123> hello bot")
        with patch.object(slack_webhook, "_ssm", _ssm_mock()), \
             patch.object(slack_webhook, "lambda_client") as mock_lc:
            resp = slack_webhook.handler(event, {})
        assert resp["statusCode"] == 200
        mock_lc.invoke.assert_not_called()

    def test_dispatch_payload_shape(self):
        """Router receives source=slack, trigger_type=mention, and context dict."""
        event = self._mention_event("<@U123> @workitems update backlog", user="UABC", channel="CXYZ")
        with patch.object(slack_webhook, "_ssm", _ssm_mock()), \
             patch.object(slack_webhook, "lambda_client") as mock_lc:
            slack_webhook.handler(event, {})
        payload = json.loads(mock_lc.invoke.call_args.kwargs["Payload"])
        assert payload["source"] == "slack"
        assert payload["trigger_type"] == "mention"
        assert payload["agent_id"] == "workitems"
        assert payload["context"]["channel_id"] == "CXYZ"
        assert payload["sender"] == "UABC"
        assert mock_lc.invoke.call_args.kwargs["InvocationType"] == "Event"

    def test_researcher_mention_not_dispatched(self):
        """researcher has no Slack triggers; mention must be silently ignored (S-B1)."""
        event = self._mention_event("<@U123BOT> @researcher analyze backlog")
        with patch.object(slack_webhook, "_ssm", _ssm_mock()), \
             patch.object(slack_webhook, "lambda_client") as mock_lc:
            resp = slack_webhook.handler(event, {})
        assert resp["statusCode"] == 200
        mock_lc.invoke.assert_not_called()

    def test_adr_mention_not_dispatched(self):
        """adr has no Slack triggers; mention must be silently ignored (S-B1)."""
        event = self._mention_event("<@U123BOT> @adr review pr")
        with patch.object(slack_webhook, "_ssm", _ssm_mock()), \
             patch.object(slack_webhook, "lambda_client") as mock_lc:
            resp = slack_webhook.handler(event, {})
        assert resp["statusCode"] == 200
        mock_lc.invoke.assert_not_called()


# ---------------------------------------------------------------------------
# Slash command parsing
# ---------------------------------------------------------------------------


class TestSlashCommands:
    def _slash_event(self, command: str, text: str = "", user_id: str = "USLASH", channel_id: str = "CCMD") -> dict:
        from urllib.parse import urlencode
        form_data = {
            "command": command,
            "text": text,
            "user_id": user_id,
            "channel_id": channel_id,
            "team_id": "TTEAM",
            "thread_ts": "",
        }
        body = urlencode(form_data)
        return _make_event(body, path="/slack/commands")

    def test_known_command_returns_ephemeral_ack(self):
        event = self._slash_event("/workitems", "break this epic")
        with patch.object(slack_webhook, "_ssm", _ssm_mock()), \
             patch.object(slack_webhook, "lambda_client"):
            resp = slack_webhook.handler(event, {})
        assert resp["statusCode"] == 200
        ack = json.loads(resp["body"])
        assert ack["response_type"] == "ephemeral"
        assert "workitems" in ack["text"]

    def test_unknown_command_returns_ephemeral_error(self):
        event = self._slash_event("/unknown")
        with patch.object(slack_webhook, "_ssm", _ssm_mock()), \
             patch.object(slack_webhook, "lambda_client") as mock_lc:
            resp = slack_webhook.handler(event, {})
        assert resp["statusCode"] == 200
        ack = json.loads(resp["body"])
        assert ack["response_type"] == "ephemeral"
        mock_lc.invoke.assert_not_called()

    def test_slash_command_dispatches_async(self):
        event = self._slash_event("/docwriter", "update api docs")
        with patch.object(slack_webhook, "_ssm", _ssm_mock()), \
             patch.object(slack_webhook, "lambda_client") as mock_lc:
            slack_webhook.handler(event, {})
        payload = json.loads(mock_lc.invoke.call_args.kwargs["Payload"])
        assert payload["source"] == "slack"
        assert payload["trigger_type"] == "slash_command"
        assert payload["agent_id"] == "docwriter"
        assert payload["instruction"] == "update api docs"
        assert mock_lc.invoke.call_args.kwargs["InvocationType"] == "Event"

    def test_sender_is_user_id_for_slash_command(self):
        """Slash-command sender must be user_id, not display name (T-4 analogue)."""
        event = self._slash_event("/workitems", user_id="UFIXED")
        with patch.object(slack_webhook, "_ssm", _ssm_mock()), \
             patch.object(slack_webhook, "lambda_client") as mock_lc:
            slack_webhook.handler(event, {})
        payload = json.loads(mock_lc.invoke.call_args.kwargs["Payload"])
        assert payload["sender"] == "UFIXED"
