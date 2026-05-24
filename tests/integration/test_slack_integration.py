"""Integration tests for the Slack webhook receiver + agent reply path.

Requires a sandbox Slack workspace with a bot installed. These tests:
1. Forge a valid signed request to the webhook endpoint (simulating Slack)
2. Verify the endpoint accepts it and dispatches correctly
3. Post a message via the bot token and verify it appears
4. Exercise the reply path (slack_post_message / slack_post_thread)

Run with: pytest tests/integration/test_slack_integration.py -v
"""

import hashlib
import hmac
import json
import time

import pytest
import requests


SLACK_API = "https://slack.com/api"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sign_request(body: str, secret: str, ts: str | None = None) -> tuple[str, str]:
    """Compute Slack's v0 HMAC-SHA256 signature for a request body."""
    ts = ts or str(int(time.time()))
    sig_base = f"v0:{ts}:{body}"
    digest = hmac.new(secret.encode(), sig_base.encode(), hashlib.sha256).hexdigest()
    return ts, f"v0={digest}"


def _slack_get(endpoint: str, token: str, params: dict | None = None) -> dict:
    """Call a Slack Web API GET endpoint."""
    resp = requests.get(
        f"{SLACK_API}/{endpoint}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    assert data.get("ok"), f"Slack API error: {data.get('error')}"
    return data


def _slack_post(endpoint: str, token: str, payload: dict) -> dict:
    """Call a Slack Web API POST endpoint."""
    resp = requests.post(
        f"{SLACK_API}/{endpoint}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    assert data.get("ok"), f"Slack API error: {data.get('error')}"
    return data


# ---------------------------------------------------------------------------
# Test: Bot token is valid and can read its own identity
# ---------------------------------------------------------------------------


class TestSlackBotConnectivity:
    """Verify the sandbox bot token works before running heavier tests."""

    def test_auth_test(self, slack_bot_token):
        data = _slack_get("auth.test", slack_bot_token)
        assert "bot_id" in data or "user_id" in data

    def test_bot_can_list_conversations(self, slack_bot_token):
        data = _slack_get("conversations.list", slack_bot_token, {"limit": "1"})
        assert "channels" in data


# ---------------------------------------------------------------------------
# Test: Webhook signature verification (against deployed endpoint)
# ---------------------------------------------------------------------------


class TestSlackWebhookSignature:
    """Send requests to the live webhook endpoint and verify behavior."""

    def test_url_verification_challenge(
        self, webhook_base_url, slack_signing_secret
    ):
        """Slack sends a challenge during app setup; endpoint must echo it."""
        challenge = "test_challenge_token_12345"
        body = json.dumps({
            "type": "url_verification",
            "challenge": challenge,
        })
        ts, sig = _sign_request(body, slack_signing_secret)
        resp = requests.post(
            f"{webhook_base_url}/slack/events",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": sig,
            },
            timeout=15,
        )
        assert resp.status_code == 200
        assert resp.json().get("challenge") == challenge

    def test_invalid_signature_rejected(self, webhook_base_url):
        """A forged signature must be rejected."""
        body = json.dumps({"type": "url_verification", "challenge": "x"})
        ts = str(int(time.time()))
        resp = requests.post(
            f"{webhook_base_url}/slack/events",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": "v0=0000000000000000000000000000000000000000000000000000000000000000",
            },
            timeout=15,
        )
        assert resp.status_code in (401, 403)

    def test_expired_timestamp_rejected(
        self, webhook_base_url, slack_signing_secret
    ):
        """A request older than 5 minutes must be rejected (replay defense)."""
        body = json.dumps({"type": "url_verification", "challenge": "x"})
        old_ts = str(int(time.time()) - 600)
        _, sig = _sign_request(body, slack_signing_secret, ts=old_ts)
        resp = requests.post(
            f"{webhook_base_url}/slack/events",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Slack-Request-Timestamp": old_ts,
                "X-Slack-Signature": sig,
            },
            timeout=15,
        )
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Test: app_mention dispatch path
# ---------------------------------------------------------------------------


class TestSlackAppMention:
    """Send a properly-signed app_mention event and verify dispatch."""

    def test_app_mention_dispatches(
        self, webhook_base_url, slack_signing_secret, slack_test_channel
    ):
        """An app_mention with @workitems should be accepted (200/202)."""
        event_payload = {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": "<@U123BOT> @workitems summarize status",
                "user": "U_TESTER",
                "channel": slack_test_channel,
                "ts": "1234567890.123456",
            },
        }
        body = json.dumps(event_payload)
        ts, sig = _sign_request(body, slack_signing_secret)
        resp = requests.post(
            f"{webhook_base_url}/slack/events",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": sig,
            },
            timeout=15,
        )
        assert resp.status_code in (200, 202)


# ---------------------------------------------------------------------------
# Test: Bot can post and reply in a thread
# ---------------------------------------------------------------------------


class TestSlackBotPosting:
    """Verify the bot can post messages and thread replies."""

    def test_post_message(self, slack_bot_token, slack_test_channel):
        """Post a top-level message to the test channel."""
        data = _slack_post("chat.postMessage", slack_bot_token, {
            "channel": slack_test_channel,
            "text": "[integration-test] Top-level message",
        })
        assert data.get("ts")
        return data["ts"]

    def test_post_thread_reply(self, slack_bot_token, slack_test_channel):
        """Post a top-level message, then reply in the thread."""
        parent = _slack_post("chat.postMessage", slack_bot_token, {
            "channel": slack_test_channel,
            "text": "[integration-test] Thread parent",
        })
        parent_ts = parent["ts"]

        reply = _slack_post("chat.postMessage", slack_bot_token, {
            "channel": slack_test_channel,
            "text": "[integration-test] Thread reply",
            "thread_ts": parent_ts,
        })
        assert reply.get("ts")
        assert reply.get("message", {}).get("thread_ts") == parent_ts

    def test_post_to_nonexistent_channel_fails(self, slack_bot_token):
        """Posting to a bogus channel should return ok=false, not crash."""
        resp = requests.post(
            f"{SLACK_API}/chat.postMessage",
            headers={
                "Authorization": f"Bearer {slack_bot_token}",
                "Content-Type": "application/json",
            },
            json={"channel": "C000NONEXISTENT", "text": "should fail"},
            timeout=10,
        )
        data = resp.json()
        assert data.get("ok") is False
        assert data.get("error") == "channel_not_found"


# ---------------------------------------------------------------------------
# Test: Slash-command endpoint
# ---------------------------------------------------------------------------


class TestSlackSlashCommand:
    """Verify the /slack/commands endpoint accepts slash commands."""

    def test_slash_command_ack(
        self, webhook_base_url, slack_signing_secret, slack_test_channel
    ):
        """A well-formed slash command should get an ephemeral ack."""
        form_body = (
            f"command=%2Fworkitems"
            f"&text=status+report"
            f"&user_id=U_TESTER"
            f"&channel_id={slack_test_channel}"
            f"&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2Ftest"
        )
        ts, sig = _sign_request(form_body, slack_signing_secret)
        resp = requests.post(
            f"{webhook_base_url}/slack/commands",
            data=form_body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": sig,
            },
            timeout=15,
        )
        assert resp.status_code == 200
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if body:
            assert body.get("response_type") in ("ephemeral", "in_channel")
