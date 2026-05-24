"""Integration tests for the Discord interactions endpoint + agent reply path.

Requires a sandbox Discord application with a bot in a test guild. These tests:
1. Forge a valid Ed25519-signed request to the interactions endpoint
2. Verify PING echo, signature rejection, and command dispatch
3. Post messages via the bot token and verify they appear
4. Exercise the followup webhook path (interaction_token based)

Run with: pytest tests/integration/test_discord_integration.py -v
"""

import json
import time

import pytest
import requests

try:
    from nacl.signing import SigningKey
    HAS_NACL = True
except ImportError:
    HAS_NACL = False


DISCORD_API = "https://discord.com/api/v10"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _discord_headers(token: str) -> dict:
    return {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }


def _sign_discord_request(body: str, signing_key: SigningKey) -> tuple[str, str]:
    """Produce the X-Signature-Ed25519 and X-Signature-Timestamp headers."""
    timestamp = str(int(time.time()))
    message = (timestamp + body).encode()
    signed = signing_key.sign(message)
    signature = signed.signature.hex()
    return timestamp, signature


# ---------------------------------------------------------------------------
# Test: Bot token is valid
# ---------------------------------------------------------------------------


class TestDiscordBotConnectivity:
    """Verify the sandbox bot token works."""

    def test_get_current_user(self, discord_bot_token):
        resp = requests.get(
            f"{DISCORD_API}/users/@me",
            headers=_discord_headers(discord_bot_token),
            timeout=10,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data.get("bot") is True

    def test_get_guild(self, discord_bot_token, discord_test_guild):
        resp = requests.get(
            f"{DISCORD_API}/guilds/{discord_test_guild}",
            headers=_discord_headers(discord_bot_token),
            timeout=10,
        )
        assert resp.status_code == 200
        assert "id" in resp.json()


# ---------------------------------------------------------------------------
# Test: Interactions endpoint signature verification
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_NACL, reason="PyNaCl not installed")
class TestDiscordWebhookSignature:
    """Send requests to the live interactions endpoint."""

    @pytest.fixture
    def signing_key(self, discord_public_key):
        """Generate a signing key that matches the registered public key.

        NOTE: For real integration tests, you need the PRIVATE key that
        corresponds to the public key registered with Discord. In sandbox
        testing, the test fixture uses a generated keypair and the endpoint
        is configured with the matching public key.
        """
        private_key_hex = pytest.importorskip("os").environ.get("DISCORD_PRIVATE_KEY")
        if not private_key_hex:
            pytest.skip("DISCORD_PRIVATE_KEY not set — cannot sign requests")
        return SigningKey(bytes.fromhex(private_key_hex))

    def test_ping_pong(self, webhook_base_url, signing_key):
        """Discord sends type=1 PING during endpoint registration; must echo type=1."""
        body = json.dumps({"type": 1})
        timestamp, signature = _sign_discord_request(body, signing_key)
        resp = requests.post(
            f"{webhook_base_url}/discord/interactions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Signature-Ed25519": signature,
                "X-Signature-Timestamp": timestamp,
            },
            timeout=15,
        )
        assert resp.status_code == 200
        assert resp.json().get("type") == 1

    def test_invalid_signature_rejected(self, webhook_base_url):
        """A forged signature must return 401."""
        body = json.dumps({"type": 1})
        resp = requests.post(
            f"{webhook_base_url}/discord/interactions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Signature-Ed25519": "0" * 128,
                "X-Signature-Timestamp": str(int(time.time())),
            },
            timeout=15,
        )
        assert resp.status_code == 401

    def test_application_command_deferred(self, webhook_base_url, signing_key):
        """A type=2 APPLICATION_COMMAND should return type=5 (deferred)."""
        body = json.dumps({
            "type": 2,
            "id": "123456789",
            "token": "fake-interaction-token",
            "application_id": "000000000000000000",
            "guild_id": "111111111111111111",
            "channel_id": "222222222222222222",
            "member": {"user": {"id": "333333333333333333", "username": "tester"}},
            "data": {"name": "workitems", "options": [{"name": "prompt", "value": "test"}]},
        })
        timestamp, signature = _sign_discord_request(body, signing_key)
        resp = requests.post(
            f"{webhook_base_url}/discord/interactions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Signature-Ed25519": signature,
                "X-Signature-Timestamp": timestamp,
            },
            timeout=15,
        )
        assert resp.status_code == 200
        assert resp.json().get("type") == 5


# ---------------------------------------------------------------------------
# Test: Bot can post messages to a channel
# ---------------------------------------------------------------------------


class TestDiscordBotPosting:
    """Verify the bot can post and read messages in the test channel."""

    def test_post_message(self, discord_bot_token, discord_test_channel):
        """Post a message to the test channel."""
        resp = requests.post(
            f"{DISCORD_API}/channels/{discord_test_channel}/messages",
            headers=_discord_headers(discord_bot_token),
            json={"content": "[integration-test] Hello from test suite"},
            timeout=10,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        return data["id"]

    def test_post_and_read_back(self, discord_bot_token, discord_test_channel):
        """Post a message and verify it can be read back."""
        unique_text = f"[integration-test] {time.time_ns()}"
        post_resp = requests.post(
            f"{DISCORD_API}/channels/{discord_test_channel}/messages",
            headers=_discord_headers(discord_bot_token),
            json={"content": unique_text},
            timeout=10,
        )
        assert post_resp.status_code == 200
        msg_id = post_resp.json()["id"]

        get_resp = requests.get(
            f"{DISCORD_API}/channels/{discord_test_channel}/messages/{msg_id}",
            headers=_discord_headers(discord_bot_token),
            timeout=10,
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["content"] == unique_text

    def test_post_to_nonexistent_channel(self, discord_bot_token):
        """Posting to a bogus channel returns 404 or similar, not 5xx."""
        resp = requests.post(
            f"{DISCORD_API}/channels/000000000000000001/messages",
            headers=_discord_headers(discord_bot_token),
            json={"content": "should fail"},
            timeout=10,
        )
        assert resp.status_code in (403, 404)


# ---------------------------------------------------------------------------
# Test: Slash commands are registered in the test guild
# ---------------------------------------------------------------------------


class TestDiscordSlashCommands:
    """Verify slash commands are registered for the sandbox app."""

    def test_commands_registered(
        self, discord_bot_token, discord_application_id, discord_test_guild
    ):
        """The bootstrap script should have registered at least one command."""
        resp = requests.get(
            f"{DISCORD_API}/applications/{discord_application_id}/guilds/{discord_test_guild}/commands",
            headers=_discord_headers(discord_bot_token),
            timeout=10,
        )
        assert resp.status_code == 200
        commands = resp.json()
        assert len(commands) > 0
        command_names = {cmd["name"] for cmd in commands}
        assert "workitems" in command_names or "docwriter" in command_names


# ---------------------------------------------------------------------------
# Test: Interaction followup webhook
# ---------------------------------------------------------------------------


class TestDiscordFollowup:
    """Test the interaction followup webhook path.

    This requires a real interaction_token (15-min TTL), so we can only
    validate the shape of the API call. In sandbox testing with a real bot,
    trigger a slash command manually and capture the token, or use the
    deferred-response test above which produces one.
    """

    def test_followup_to_expired_token_returns_error(
        self, discord_application_id
    ):
        """A followup to an expired/fake token should return 401 or 404."""
        resp = requests.post(
            f"{DISCORD_API}/webhooks/{discord_application_id}/expired-token-12345",
            headers={"Content-Type": "application/json"},
            json={"content": "should fail"},
            timeout=10,
        )
        assert resp.status_code in (401, 404)
