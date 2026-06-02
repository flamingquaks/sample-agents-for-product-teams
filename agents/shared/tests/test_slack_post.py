"""Unit tests for the shared Slack posting tools (`tools.slack_post`)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import slack_post as mod  # noqa: E402
from tools.slack_post import (  # noqa: E402
    get_slack_token,
    slack_add_reaction,
    slack_post_message,
)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Reset module-level token cache / SSM client and clear Slack env vars.

    Tests that need the env set should do so explicitly via monkeypatch.
    """
    mod._cached_token = None
    mod._ssm = None
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN_PARAM", raising=False)
    yield
    mod._cached_token = None
    mod._ssm = None


def _ok_response(data=None):
    resp = MagicMock()
    resp.json.return_value = data if data is not None else {"ok": True}
    return resp


# --------------------------------------------------------------------------
# get_slack_token
# --------------------------------------------------------------------------


def test_get_slack_token_returns_env_without_ssm(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")
    mock_ssm = MagicMock()
    with patch.object(mod, "_get_ssm", return_value=mock_ssm):
        assert get_slack_token() == "xoxb-from-env"
    mock_ssm.get_parameter.assert_not_called()


def test_get_slack_token_falls_back_to_ssm_and_caches(monkeypatch):
    mock_ssm = MagicMock()
    mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "xoxb-from-ssm"}}
    with patch.object(mod, "_get_ssm", return_value=mock_ssm) as get_ssm:
        first = get_slack_token()
        second = get_slack_token()
    assert first == "xoxb-from-ssm"
    assert second == "xoxb-from-ssm"
    # Cached: SSM consulted only once across both calls.
    assert mock_ssm.get_parameter.call_count == 1
    assert get_ssm.call_count == 1


def test_get_slack_token_returns_none_on_client_error(monkeypatch):
    mock_ssm = MagicMock()
    mock_ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "ParameterNotFound", "Message": "nope"}},
        "GetParameter",
    )
    with patch.object(mod, "_get_ssm", return_value=mock_ssm):
        assert get_slack_token() is None


def test_get_slack_token_uses_custom_param_name(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN_PARAM", "/custom/slack-token")
    mock_ssm = MagicMock()
    mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "xoxb-custom"}}
    with patch.object(mod, "_get_ssm", return_value=mock_ssm):
        assert get_slack_token() == "xoxb-custom"
    assert mock_ssm.get_parameter.call_args.kwargs["Name"] == "/custom/slack-token"


# --------------------------------------------------------------------------
# slack_post_message
# --------------------------------------------------------------------------


def test_post_message_empty_channel_no_request():
    with patch.object(mod.requests, "post") as mock_post:
        result = slack_post_message("", "hi")
    assert result == "Cannot post to Slack: no channel_id provided."
    mock_post.assert_not_called()


def test_post_message_no_token_returns_not_configured(monkeypatch):
    # Env unset (fixture) and SSM raises -> no token.
    mock_ssm = MagicMock()
    mock_ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "ParameterNotFound", "Message": "nope"}},
        "GetParameter",
    )
    with patch.object(mod, "_get_ssm", return_value=mock_ssm):
        with patch.object(mod.requests, "post") as mock_post:
            result = slack_post_message("C1", "hi")
    assert "not configured" in result
    mock_post.assert_not_called()


def test_post_message_success_payload_without_thread(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    with patch.object(mod.requests, "post", return_value=_ok_response()) as mock_post:
        result = slack_post_message("C123", "hello world")
    assert "C123" in result
    assert result.startswith("Posted message to Slack")
    url = mock_post.call_args.args[0]
    assert url == "https://slack.com/api/chat.postMessage"
    payload = mock_post.call_args.kwargs["json"]
    assert payload["channel"] == "C123"
    assert payload["text"] == "hello world"
    assert "thread_ts" not in payload


def test_post_message_includes_thread_ts(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    with patch.object(mod.requests, "post", return_value=_ok_response()) as mock_post:
        result = slack_post_message("C123", "reply", thread_ts="1700000000.000100")
    payload = mock_post.call_args.kwargs["json"]
    assert payload["thread_ts"] == "1700000000.000100"
    assert "1700000000.000100" in result


def test_post_message_slack_error(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    resp = _ok_response({"ok": False, "error": "channel_not_found"})
    with patch.object(mod.requests, "post", return_value=resp):
        result = slack_post_message("C123", "hi")
    assert "channel_not_found" in result
    assert result.startswith("Failed to post to Slack")


def test_post_message_request_exception_is_graceful(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    err = requests.RequestException("boom")
    with patch.object(mod.requests, "post", side_effect=err):
        result = slack_post_message("C123", "hi")
    assert result.startswith("Failed to post to Slack")
    assert "boom" in result


# --------------------------------------------------------------------------
# slack_add_reaction
# --------------------------------------------------------------------------


def test_add_reaction_missing_timestamp_no_request():
    with patch.object(mod.requests, "post") as mock_post:
        result = slack_add_reaction("C123", "", "eyes")
    assert "required" in result
    mock_post.assert_not_called()


def test_add_reaction_already_reacted_is_success(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    resp = _ok_response({"ok": False, "error": "already_reacted"})
    with patch.object(mod.requests, "post", return_value=resp):
        result = slack_add_reaction("C123", "1700000000.000100", "eyes")
    assert result == "Added :eyes: reaction in C123."


def test_add_reaction_success_payload(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    with patch.object(mod.requests, "post", return_value=_ok_response()) as mock_post:
        result = slack_add_reaction("C123", "1700000000.000100", "white_check_mark")
    assert result == "Added :white_check_mark: reaction in C123."
    url = mock_post.call_args.args[0]
    assert url == "https://slack.com/api/reactions.add"
    payload = mock_post.call_args.kwargs["json"]
    assert payload == {
        "channel": "C123",
        "timestamp": "1700000000.000100",
        "name": "white_check_mark",
    }


def test_add_reaction_slack_error(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    resp = _ok_response({"ok": False, "error": "message_not_found"})
    with patch.object(mod.requests, "post", return_value=resp):
        result = slack_add_reaction("C123", "1700000000.000100", "eyes")
    assert "message_not_found" in result
    assert result.startswith("Failed to add Slack reaction")
