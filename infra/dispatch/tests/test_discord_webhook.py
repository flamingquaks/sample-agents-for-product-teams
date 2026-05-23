"""Unit tests for the Discord Interactions Endpoint Lambda.

Coverage:
  - Ed25519 verification: good signature, bad signature, missing headers
  - PING (type 1) echo
  - APPLICATION_COMMAND (type 2): agent resolution, option parsing, deferred ACK
  - Missing / empty SSM public-key param returns 503
  - Async router invoke shape
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_boto3(monkeypatch):
    """Suppress all real boto3 client creation at module import."""
    with patch("boto3.client") as mock_client:
        # SSM: default returns a valid public key hex
        ssm_mock = MagicMock()
        ssm_mock.get_parameter.return_value = {
            "Parameter": {"Value": "a" * 64}  # 32-byte key hex
        }
        ssm_mock.exceptions.ParameterNotFound = Exception
        mock_client.return_value = ssm_mock
        yield mock_client


@pytest.fixture
def dw(monkeypatch):
    """Import discord_webhook with all external clients mocked."""
    for name in ("discord_webhook",):
        sys.modules.pop(name, None)
    import discord_webhook as dw_mod
    return dw_mod


# ---------------------------------------------------------------------------
# Ed25519 verification helpers
# ---------------------------------------------------------------------------


def _make_event(body: str, sig: str = "aabbcc", ts: str = "12345") -> dict:
    return {
        "body": body,
        "headers": {
            "x-signature-ed25519": sig,
            "x-signature-timestamp": ts,
        },
    }


class TestEd25519Verification:
    def test_missing_sig_header_returns_401(self, dw):
        event = {"body": "{}", "headers": {"x-signature-timestamp": "12345"}}
        with patch.object(dw, "_verify_ed25519", return_value=True):
            resp = dw.handler(event, None)
        assert resp["statusCode"] == 401
        assert "missing" in resp["body"].lower()

    def test_missing_ts_header_returns_401(self, dw):
        event = {"body": "{}", "headers": {"x-signature-ed25519": "aabbcc"}}
        with patch.object(dw, "_verify_ed25519", return_value=True):
            resp = dw.handler(event, None)
        assert resp["statusCode"] == 401

    def test_invalid_signature_returns_401(self, dw):
        event = _make_event("{}", sig="deadbeef", ts="12345")
        with patch.object(dw, "_verify_ed25519", return_value=False):
            resp = dw.handler(event, None)
        assert resp["statusCode"] == 401
        assert "invalid" in resp["body"].lower()

    def test_valid_signature_passes_through(self, dw):
        body = json.dumps({"type": 1})
        event = _make_event(body)
        with patch.object(dw, "_verify_ed25519", return_value=True):
            resp = dw.handler(event, None)
        assert resp["statusCode"] == 200

    def test_verify_ed25519_bad_hex_returns_false(self, dw):
        result = dw._verify_ed25519("notvalidhex!", "aabb", "12345", "{}")
        assert result is False

    def test_verify_ed25519_bad_signature_returns_false(self, dw):
        # Use real key material so pynacl is exercised but signature is wrong
        try:
            import nacl.signing
        except ImportError:
            pytest.skip("pynacl not installed")
        signing_key = nacl.signing.SigningKey.generate()
        verify_key_hex = signing_key.verify_key.encode().hex()
        # Sign different content than what we present
        signed = signing_key.sign(b"12345{}")
        sig_hex = signed.signature.hex()
        # Now verify against different message -- should fail
        result = dw._verify_ed25519(verify_key_hex, sig_hex, "12345", "different_body")
        assert result is False

    def test_verify_ed25519_valid_signature_returns_true(self, dw):
        try:
            import nacl.signing
        except ImportError:
            pytest.skip("pynacl not installed")
        signing_key = nacl.signing.SigningKey.generate()
        verify_key_hex = signing_key.verify_key.encode().hex()
        timestamp = "12345"
        body = '{"type":1}'
        message = (timestamp + body).encode()
        signed = signing_key.sign(message)
        sig_hex = signed.signature.hex()
        result = dw._verify_ed25519(verify_key_hex, sig_hex, timestamp, body)
        assert result is True


# ---------------------------------------------------------------------------
# Missing / empty SSM param
# ---------------------------------------------------------------------------


class TestSSMFailures:
    def test_missing_public_key_param_returns_503(self, dw):
        with patch.object(dw._ssm, "get_parameter", side_effect=dw._ssm.exceptions.ParameterNotFound):
            resp = dw.handler(_make_event("{}"), None)
        assert resp["statusCode"] == 503
        assert "not registered" in resp["body"]

    def test_empty_public_key_returns_503(self, dw):
        with patch.object(dw._ssm, "get_parameter", return_value={"Parameter": {"Value": ""}}):
            resp = dw.handler(_make_event("{}"), None)
        assert resp["statusCode"] == 503


# ---------------------------------------------------------------------------
# PING handling (type 1)
# ---------------------------------------------------------------------------


class TestPingHandshake:
    def test_ping_echoes_type_1(self, dw):
        body = json.dumps({"type": 1})
        event = _make_event(body)
        with patch.object(dw, "_verify_ed25519", return_value=True):
            resp = dw.handler(event, None)
        assert resp["statusCode"] == 200
        payload = json.loads(resp["body"])
        assert payload == {"type": 1}

    def test_ping_requires_valid_signature(self, dw):
        body = json.dumps({"type": 1})
        event = _make_event(body)
        with patch.object(dw, "_verify_ed25519", return_value=False):
            resp = dw.handler(event, None)
        assert resp["statusCode"] == 401


# ---------------------------------------------------------------------------
# APPLICATION_COMMAND (type 2) parsing
# ---------------------------------------------------------------------------


def _slash_event(command: str, instruction: str, user_id: str = "111222333", guild_id: str = "999", channel_id: str = "888") -> dict:
    body = json.dumps({
        "type": 2,
        "application_id": "777",
        "token": "interaction_token_abc",
        "guild_id": guild_id,
        "channel_id": channel_id,
        "member": {"user": {"id": user_id}},
        "data": {
            "name": command,
            "options": [{"name": "instruction", "value": instruction}],
        },
    })
    return _make_event(body)


class TestApplicationCommand:
    def test_workitems_command_dispatches_correctly(self, dw):
        event = _slash_event("workitems", "generate a status report")
        with patch.object(dw, "_verify_ed25519", return_value=True), \
             patch.object(dw.lambda_client, "invoke") as mock_invoke:
            resp = dw.handler(event, None)

        assert resp["statusCode"] == 200
        payload = json.loads(resp["body"])
        # Must be deferred response (type 5)
        assert payload["type"] == 5

        # Verify dispatch payload shape
        mock_invoke.assert_called_once()
        call_kwargs = mock_invoke.call_args.kwargs
        assert call_kwargs["InvocationType"] == "Event"
        dispatch_payload = json.loads(call_kwargs["Payload"].decode())
        assert dispatch_payload["source"] == "discord"
        assert dispatch_payload["trigger_type"] == "slash_command"
        assert dispatch_payload["agent_id"] == "workitems"
        assert dispatch_payload["instruction"] == "generate a status report"
        assert dispatch_payload["sender"] == "111222333"
        ctx = dispatch_payload["context"]
        assert ctx["channel_id"] == "888"
        assert ctx["guild_id"] == "999"
        assert ctx["application_id"] == "777"
        assert ctx["interaction_token"] == "interaction_token_abc"
        assert ctx["user_id"] == "111222333"

    def test_docwriter_command_dispatches_correctly(self, dw):
        event = _slash_event("docwriter", "update the API docs for the payments endpoint")
        with patch.object(dw, "_verify_ed25519", return_value=True), \
             patch.object(dw.lambda_client, "invoke") as mock_invoke:
            resp = dw.handler(event, None)

        assert resp["statusCode"] == 200
        dispatch_payload = json.loads(mock_invoke.call_args.kwargs["Payload"].decode())
        assert dispatch_payload["agent_id"] == "docwriter"

    def test_unknown_command_returns_inline_error(self, dw):
        event = _slash_event("unknown_agent", "do something")
        with patch.object(dw, "_verify_ed25519", return_value=True), \
             patch.object(dw.lambda_client, "invoke") as mock_invoke:
            resp = dw.handler(event, None)

        mock_invoke.assert_not_called()
        assert resp["statusCode"] == 200
        payload = json.loads(resp["body"])
        # type 4 = inline message (not deferred)
        assert payload["type"] == 4
        assert "unknown_agent" in payload["data"]["content"].lower()

    def test_empty_instruction_gets_fallback(self, dw):
        body = json.dumps({
            "type": 2,
            "application_id": "777",
            "token": "tok",
            "guild_id": "999",
            "channel_id": "888",
            "member": {"user": {"id": "123"}},
            "data": {"name": "workitems", "options": []},
        })
        event = _make_event(body)
        with patch.object(dw, "_verify_ed25519", return_value=True), \
             patch.object(dw.lambda_client, "invoke") as mock_invoke:
            resp = dw.handler(event, None)

        assert resp["statusCode"] == 200
        dispatch_payload = json.loads(mock_invoke.call_args.kwargs["Payload"].decode())
        # Instruction should be non-empty fallback text
        assert len(dispatch_payload["instruction"]) > 0

    def test_dm_context_uses_user_not_member(self, dw):
        """In DMs, interaction.user is populated; interaction.member is absent."""
        body = json.dumps({
            "type": 2,
            "application_id": "777",
            "token": "tok",
            "channel_id": "888",
            "user": {"id": "DM_USER_999"},  # DM context -- no member
            "data": {
                "name": "workitems",
                "options": [{"name": "instruction", "value": "hello"}],
            },
        })
        event = _make_event(body)
        with patch.object(dw, "_verify_ed25519", return_value=True), \
             patch.object(dw.lambda_client, "invoke") as mock_invoke:
            resp = dw.handler(event, None)

        dispatch_payload = json.loads(mock_invoke.call_args.kwargs["Payload"].decode())
        assert dispatch_payload["sender"] == "DM_USER_999"

    def test_dispatch_failure_returns_inline_error(self, dw):
        event = _slash_event("workitems", "do something")
        with patch.object(dw, "_verify_ed25519", return_value=True), \
             patch.object(dw.lambda_client, "invoke", side_effect=Exception("invoke failed")):
            resp = dw.handler(event, None)

        # Should return inline error message, not propagate exception
        assert resp["statusCode"] == 200
        payload = json.loads(resp["body"])
        assert payload["type"] == 4
        assert "dispatch failed" in payload["data"]["content"].lower()

    def test_unknown_interaction_type_returns_400(self, dw):
        body = json.dumps({"type": 99})
        event = _make_event(body)
        with patch.object(dw, "_verify_ed25519", return_value=True):
            resp = dw.handler(event, None)
        assert resp["statusCode"] == 400
