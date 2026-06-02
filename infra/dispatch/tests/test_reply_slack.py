"""Unit tests for the Slack dispatch-layer helpers in reply.py."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reply  # noqa: E402


@pytest.fixture(autouse=True)
def _mock_ssm():
    with patch.object(reply, "_ssm") as mock:
        mock.get_parameter.return_value = {"Parameter": {"Value": "fake-token"}}
        yield mock


def _slack_response(json_payload):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = json_payload
    return resp


# --- post_slack_message -------------------------------------------------


def test_post_slack_message_success():
    resp = _slack_response({"ok": True})
    with patch.object(reply.requests, "post", return_value=resp) as mock_post:
        ok = reply.post_slack_message("C123", "hello")
    assert ok is True
    url = mock_post.call_args.args[0]
    assert url == "https://slack.com/api/chat.postMessage"
    payload = mock_post.call_args.kwargs["json"]
    assert payload["channel"] == "C123"
    assert payload["text"] == "hello"


def test_post_slack_message_thread_ts_added_when_provided():
    resp = _slack_response({"ok": True})
    with patch.object(reply.requests, "post", return_value=resp) as mock_post:
        reply.post_slack_message("C123", "hello", thread_ts="1.234")
    payload = mock_post.call_args.kwargs["json"]
    assert payload["thread_ts"] == "1.234"


def test_post_slack_message_thread_ts_absent_when_not_provided():
    resp = _slack_response({"ok": True})
    with patch.object(reply.requests, "post", return_value=resp) as mock_post:
        reply.post_slack_message("C123", "hello")
    payload = mock_post.call_args.kwargs["json"]
    assert "thread_ts" not in payload


def test_post_slack_message_blocks_added_when_provided():
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}]
    resp = _slack_response({"ok": True})
    with patch.object(reply.requests, "post", return_value=resp) as mock_post:
        reply.post_slack_message("C123", "hello", blocks=blocks)
    payload = mock_post.call_args.kwargs["json"]
    assert payload["blocks"] == blocks


def test_post_slack_message_empty_channel_returns_false_no_post():
    with patch.object(reply.requests, "post") as mock_post:
        ok = reply.post_slack_message("", "hello")
    assert ok is False
    mock_post.assert_not_called()


def test_post_slack_message_missing_token_returns_false_no_post():
    with patch.object(reply, "_get_secret", return_value=None):
        with patch.object(reply.requests, "post") as mock_post:
            ok = reply.post_slack_message("C123", "hello")
    assert ok is False
    mock_post.assert_not_called()


def test_post_slack_message_slack_error_returns_false():
    resp = _slack_response({"ok": False, "error": "channel_not_found"})
    with patch.object(reply.requests, "post", return_value=resp):
        ok = reply.post_slack_message("C123", "hello")
    assert ok is False


def test_post_slack_message_swallows_request_exception():
    import requests
    err = requests.RequestException("boom")
    with patch.object(reply.requests, "post", side_effect=err):
        ok = reply.post_slack_message("C123", "hello")
    assert ok is False


# --- add_slack_reaction -------------------------------------------------


def test_add_slack_reaction_success():
    resp = _slack_response({"ok": True})
    with patch.object(reply.requests, "post", return_value=resp) as mock_post:
        ok = reply.add_slack_reaction("C123", "1.234", "eyes")
    assert ok is True
    url = mock_post.call_args.args[0]
    assert url == "https://slack.com/api/reactions.add"
    payload = mock_post.call_args.kwargs["json"]
    assert payload["channel"] == "C123"
    assert payload["timestamp"] == "1.234"
    assert payload["name"] == "eyes"


def test_add_slack_reaction_missing_timestamp_returns_false_no_post():
    with patch.object(reply.requests, "post") as mock_post:
        ok = reply.add_slack_reaction("C123", "", "eyes")
    assert ok is False
    mock_post.assert_not_called()


def test_add_slack_reaction_already_reacted_is_success():
    resp = _slack_response({"ok": False, "error": "already_reacted"})
    with patch.object(reply.requests, "post", return_value=resp):
        ok = reply.add_slack_reaction("C123", "1.234", "eyes")
    assert ok is True


def test_add_slack_reaction_other_error_returns_false():
    resp = _slack_response({"ok": False, "error": "bad"})
    with patch.object(reply.requests, "post", return_value=resp):
        ok = reply.add_slack_reaction("C123", "1.234", "eyes")
    assert ok is False


def test_add_slack_reaction_swallows_request_exception():
    import requests
    err = requests.RequestException("boom")
    with patch.object(reply.requests, "post", side_effect=err):
        ok = reply.add_slack_reaction("C123", "1.234", "eyes")
    assert ok is False
