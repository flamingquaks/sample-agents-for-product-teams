"""Unit tests for the block-reply helpers used by the Dispatch Router."""

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


@pytest.fixture(autouse=True)
def _mock_github_app(monkeypatch):
    # GitHub replies always authenticate with a per-owner GitHub App installation
    # token (the shared PAT was retired). Stub the minter so the GitHub-comment
    # tests exercise the posting logic without AWS/network; the Asana tests are
    # unaffected (they read the Asana PAT via _get_secret / _ssm).
    import github_app

    monkeypatch.setattr(
        github_app, "installation_token_for_repo", lambda repo: "fake-token"
    )


def _mock_response(status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    return resp


def test_github_comment_hits_expected_url():
    with patch.object(reply.requests, "post", return_value=_mock_response()) as mock_post:
        ok = reply.post_github_comment("acme/web", 42, "blocked")
    assert ok is True
    url = mock_post.call_args.args[0]
    assert url == "https://api.github.com/repos/acme/web/issues/42/comments"
    assert mock_post.call_args.kwargs["json"] == {"body": "blocked"}
    assert mock_post.call_args.kwargs["headers"]["Authorization"] == "Bearer fake-token"


def test_github_comment_swallows_http_error():
    import requests
    err = requests.RequestException("boom")
    with patch.object(reply.requests, "post", side_effect=err):
        ok = reply.post_github_comment("acme/web", 42, "blocked")
    assert ok is False


def test_github_comment_rejects_missing_identifiers():
    with patch.object(reply.requests, "post") as mock_post:
        assert reply.post_github_comment("", 42, "x") is False
        assert reply.post_github_comment("acme/web", "", "x") is False
    mock_post.assert_not_called()


def test_asana_comment_hits_expected_url():
    with patch.object(reply.requests, "post", return_value=_mock_response()) as mock_post:
        ok = reply.post_asana_comment("12345", "blocked")
    assert ok is True
    url = mock_post.call_args.args[0]
    assert url == "https://app.asana.com/api/1.0/tasks/12345/stories"
    assert mock_post.call_args.kwargs["json"] == {"data": {"text": "blocked"}}


def test_asana_comment_swallows_http_error():
    import requests
    err = requests.RequestException("boom")
    with patch.object(reply.requests, "post", side_effect=err):
        ok = reply.post_asana_comment("12345", "blocked")
    assert ok is False


def test_github_comment_uses_per_owner_app_token(monkeypatch):
    # The reply authenticates with a per-owner GitHub App installation token
    # scoped to the target repo (the shared PAT was retired).
    import github_app

    monkeypatch.setattr(
        github_app, "installation_token_for_repo", lambda repo: f"ghs_for_{repo}"
    )
    with patch.object(reply.requests, "post", return_value=_mock_response()) as mock_post:
        ok = reply.post_github_comment("acme/web", 42, "blocked")
    assert ok is True
    assert mock_post.call_args.kwargs["headers"]["Authorization"] == "Bearer ghs_for_acme/web"


def test_github_comment_app_mint_failure_is_nonfatal(monkeypatch):
    # If minting fails, the reply returns False (non-fatal — the block decision
    # already stands) and never posts with a bad/missing token.
    import github_app

    def boom(repo):
        raise github_app.GitHubAppError("no installation")

    monkeypatch.setattr(github_app, "installation_token_for_repo", boom)
    with patch.object(reply.requests, "post") as mock_post:
        ok = reply.post_github_comment("acme/web", 42, "blocked")
    assert ok is False
    mock_post.assert_not_called()


# --- Slack (post_slack_message) ---------------------------------------------


def test_post_slack_message_success(monkeypatch):
    monkeypatch.setenv("STAGE", "test")
    resp = _mock_response()
    resp.json = lambda: {"ok": True}
    with patch.object(reply.requests, "post", return_value=resp) as mock_post:
        ok = reply.post_slack_message("T0ACME12", "C0ENG", "hello", thread_ts="1.2")
    assert ok is True
    kw = mock_post.call_args.kwargs
    assert kw["json"]["channel"] == "C0ENG" and kw["json"]["thread_ts"] == "1.2"
    assert kw["headers"]["Authorization"] == "Bearer fake-token"


def test_post_slack_message_logical_error_is_false(monkeypatch):
    # Slack returns HTTP 200 with {"ok": false} on a logical failure.
    monkeypatch.setenv("STAGE", "test")
    resp = _mock_response()
    resp.json = lambda: {"ok": False, "error": "channel_not_found"}
    with patch.object(reply.requests, "post", return_value=resp):
        assert reply.post_slack_message("T0ACME12", "C0BAD", "hi") is False


def test_post_slack_message_missing_args():
    assert reply.post_slack_message("", "C0ENG", "hi") is False
    assert reply.post_slack_message("T0ACME12", "", "hi") is False


def test_slack_bot_token_param_layout(monkeypatch):
    monkeypatch.setenv("STAGE", "gamma")
    assert reply.slack_bot_token_param("T0ACME12") == "/sdlc-agents/gamma/slack/T0ACME12/bot-token"


def test_slack_user_profile_prefers_display_name(monkeypatch):
    monkeypatch.setenv("STAGE", "test")
    resp = _mock_response()
    resp.json = lambda: {"ok": True, "user": {"real_name": "Alice A.",
                         "profile": {"display_name": "alice", "real_name": "Alice A.",
                                     "email": "alice@example.com"}}}
    with patch.object(reply.requests, "get", return_value=resp) as mock_get:
        out = reply.slack_user_profile("T0ACME12", "U0ALICE")
    assert out == {"display_name": "alice", "email": "alice@example.com"}
    kw = mock_get.call_args.kwargs
    assert kw["params"] == {"user": "U0ALICE"}
    assert kw["headers"]["Authorization"] == "Bearer fake-token"


def test_slack_user_profile_falls_back_to_real_name(monkeypatch):
    # A user who never set a display_name: real_name is the label.
    monkeypatch.setenv("STAGE", "test")
    resp = _mock_response()
    resp.json = lambda: {"ok": True, "user": {"profile": {"display_name": "",
                         "real_name": "Bob Barker", "email": ""}}}
    with patch.object(reply.requests, "get", return_value=resp):
        out = reply.slack_user_profile("T0ACME12", "U0BOB")
    assert out == {"display_name": "Bob Barker", "email": ""}


def test_slack_user_profile_logical_error_returns_empty(monkeypatch):
    monkeypatch.setenv("STAGE", "test")
    resp = _mock_response()
    resp.json = lambda: {"ok": False, "error": "user_not_found"}
    with patch.object(reply.requests, "get", return_value=resp):
        assert reply.slack_user_profile("T0ACME12", "U0GHOST") == {"display_name": "", "email": ""}


def test_slack_user_profile_missing_args():
    assert reply.slack_user_profile("", "U0ALICE") == {"display_name": "", "email": ""}
    assert reply.slack_user_profile("T0ACME12", "") == {"display_name": "", "email": ""}


def test_slack_user_profile_swallows_http_error(monkeypatch):
    import requests

    monkeypatch.setenv("STAGE", "test")
    with patch.object(reply.requests, "get", side_effect=requests.RequestException("boom")):
        assert reply.slack_user_profile("T0ACME12", "U0ALICE") == {"display_name": "", "email": ""}
