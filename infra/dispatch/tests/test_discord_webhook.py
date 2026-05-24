"""Unit tests for the Discord Interactions Endpoint Lambda.

Coverage:
  - Ed25519 verification: good signature, bad signature, missing headers
  - PING (type 1) echo
  - APPLICATION_COMMAND (type 2): agent resolution, option parsing, deferred ACK
  - Missing / empty SSM public-key param returns 503
  - Async router invoke shape
  - Base64-encoded body handling (D-M1)
"""

import base64
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import botocore.exceptions
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ssm_not_found_error():
    """Return a real botocore ClientError matching ParameterNotFound."""
    return botocore.exceptions.ClientError(
        {"Error": {"Code": "ParameterNotFound", "Message": "not found"}},
        "GetParameter",
    )


@pytest.fixture(autouse=True)
def _mock_boto3(monkeypatch):
    """Suppress all real boto3 client creation at module import.

    SSM and Lambda clients are returned as distinct mocks so tests can
    assert on each independently (D-B2).
    """
    ssm_mock = MagicMock()
    ssm_mock.get_parameter.return_value = {
        "Parameter": {"Value": "a" * 64}  # 32-byte key hex
    }
    # ParameterNotFoundException is the correct boto3 attribute name (D-B1).
    ssm_mock.exceptions.ParameterNotFoundException = botocore.exceptions.ClientError
    lambda_mock = MagicMock()

    def _client_factory(service, **kwargs):
        if service == "ssm":
            return ssm_mock
        return lambda_mock

    with patch("boto3.client", side_effect=_client_factory) as mock_client:
        yield mock_client


@pytest.fixture
def dw(monkeypatch):
    """Import discord_webhook with all external clients mocked.

    Resets the module-level _verify_key cache between tests so that the
    SSM-fetch code path is exercised on every test that checks it.
    """
    for name in ("discord_webhook",):
        sys.modules.pop(name, None)
    import discord_webhook as dw_mod
    dw_mod._verify_key = None
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
        result = dw._verify_ed25519("notvalidhex!", "aabb", "12345", b"{}")
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
        result = dw._verify_ed25519(verify_key_hex, sig_hex, "12345", b"different_body")
        assert result is False

    def test_verify_ed25519_valid_signature_returns_true(self, dw):
        try:
            import nacl.signing
        except ImportError:
            pytest.skip("pynacl not installed")
        signing_key = nacl.signing.SigningKey.generate()
        verify_key_hex = signing_key.verify_key.encode().hex()
        timestamp = "12345"
        body = b'{"type":1}'
        message = timestamp.encode() + body
        signed = signing_key.sign(message)
        sig_hex = signed.signature.hex()
        result = dw._verify_ed25519(verify_key_hex, sig_hex, timestamp, body)
        assert result is True


# ---------------------------------------------------------------------------
# Missing / empty SSM param
# ---------------------------------------------------------------------------


class TestSSMFailures:
    def test_missing_public_key_param_returns_503(self, dw):
        with patch.object(dw._ssm, "get_parameter", side_effect=_make_ssm_not_found_error()):
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


# ---------------------------------------------------------------------------
# D-B1: ParameterNotFoundException (real botocore ClientError, not a mock alias)
# ---------------------------------------------------------------------------


class TestSSMParameterNotFoundException:
    def test_parameter_not_found_raises_client_error(self, dw):
        """ParameterNotFoundException must be a real botocore ClientError so the
        except clause in the handler does not raise AttributeError (D-B1)."""
        err = _make_ssm_not_found_error()
        assert isinstance(err, botocore.exceptions.ClientError)

    def test_handler_catches_client_error_as_503(self, dw):
        """Handler catches the real botocore ClientError from ParameterNotFoundException
        and returns 503 without raising AttributeError (D-B1 regression guard)."""
        with patch.object(dw._ssm, "get_parameter", side_effect=_make_ssm_not_found_error()):
            resp = dw.handler(_make_event("{}"), None)
        assert resp["statusCode"] == 503
        assert "not registered" in resp["body"]


# ---------------------------------------------------------------------------
# D-M1: Base64-encoded body from API Gateway
# ---------------------------------------------------------------------------


class TestBase64Body:
    def test_base64_body_verifies_correctly(self, dw):
        """When isBase64Encoded=True, body is decoded to bytes before signature
        verification -- valid request must pass through to the correct handler."""
        try:
            import nacl.signing
        except ImportError:
            pytest.skip("pynacl not installed")

        signing_key = nacl.signing.SigningKey.generate()
        verify_key_hex = signing_key.verify_key.encode().hex()

        timestamp = "12345"
        body_bytes = json.dumps({"type": 1}).encode()
        message = timestamp.encode() + body_bytes
        signed = signing_key.sign(message)
        sig_hex = signed.signature.hex()

        # Simulate API Gateway base64-encoding the body
        b64_body = base64.b64encode(body_bytes).decode()

        event = {
            "body": b64_body,
            "isBase64Encoded": True,
            "headers": {
                "x-signature-ed25519": sig_hex,
                "x-signature-timestamp": timestamp,
            },
        }

        # Patch in the real key so the handler uses it without SSM
        from nacl.signing import VerifyKey
        dw._verify_key = VerifyKey(bytes.fromhex(verify_key_hex))

        resp = dw.handler(event, None)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"]) == {"type": 1}

    def test_non_base64_body_still_works(self, dw):
        """Plain-text body (isBase64Encoded absent or False) continues to work."""
        body = json.dumps({"type": 1})
        event = _make_event(body)
        with patch.object(dw, "_verify_ed25519", return_value=True):
            resp = dw.handler(event, None)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"]) == {"type": 1}
