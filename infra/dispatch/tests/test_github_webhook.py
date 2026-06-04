"""Tests for the GitHub PM webhook receiver Lambda (github_webhook.py).

Covers signature verification, event routing/normalization for issue
assignment and Projects V2 status changes, the status-change filter (no
dispatch storms), bot-loop prevention, and the ping/secret-failure paths.

The module creates boto3 clients (_ssm, lambda_client) at import time, so we
import it fresh under patched boto3 and swap those globals for mocks — the same
pattern test_slack_events.py / test_router_guardrail.py use.
"""

import hashlib
import hmac
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SECRET = "test-webhook-secret"


@pytest.fixture
def gh(monkeypatch):
    """Import github_webhook fresh with boto3 patched and clients mocked."""
    monkeypatch.setenv("DISPATCH_FUNCTION", "dispatch-router-dev")
    monkeypatch.setenv("WORKITEMS_GH_BOT_LOGIN", "workitems-bot")
    with patch("boto3.client"):
        if "github_webhook" in sys.modules:
            del sys.modules["github_webhook"]
        import github_webhook as mod  # noqa: E402
    mod._ssm = MagicMock()
    mod._ssm.get_parameter.return_value = {"Parameter": {"Value": SECRET}}
    # exceptions.ParameterNotFound must be a real exception class for `except`
    mod._ssm.exceptions.ParameterNotFound = type("ParameterNotFound", (Exception,), {})
    mod.lambda_client = MagicMock()
    # Re-resolve the bot-login map now that the env var is set (it was captured
    # at import, which happened above with the env already set — but be explicit)
    mod.BOT_LOGINS = {"workitems-bot": "workitems"}
    yield mod


def _signed_event(mod, body_obj, github_event, signature=None):
    """Build an API Gateway event with a valid (or overridden) signature."""
    body = json.dumps(body_obj)
    if signature is None:
        signature = "sha256=" + hmac.new(
            SECRET.encode(), body.encode(), hashlib.sha256
        ).hexdigest()
    return {
        "headers": {
            "X-GitHub-Event": github_event,
            "X-Hub-Signature-256": signature,
        },
        "body": body,
    }


def _last_dispatch(mod):
    """Return the payload dict from the most recent lambda_client.invoke."""
    assert mod.lambda_client.invoke.called, "expected a dispatch"
    raw = mod.lambda_client.invoke.call_args.kwargs["Payload"]
    return json.loads(raw)


# --- signature verification --------------------------------------------------


def test_valid_signature_processes(gh):
    event = _signed_event(gh, {"action": "ping"}, "ping")
    resp = gh.handler(event, None)
    assert resp["statusCode"] == 200


def test_invalid_signature_rejected(gh):
    event = _signed_event(gh, {"action": "assigned"}, "issues", signature="sha256=bad")
    resp = gh.handler(event, None)
    assert resp["statusCode"] == 401
    gh.lambda_client.invoke.assert_not_called()


def test_missing_secret_returns_503(gh):
    gh._ssm.get_parameter.side_effect = gh._ssm.exceptions.ParameterNotFound()
    event = _signed_event(gh, {"action": "ping"}, "ping")
    resp = gh.handler(event, None)
    assert resp["statusCode"] == 503


def test_empty_secret_returns_503(gh):
    gh._ssm.get_parameter.return_value = {"Parameter": {"Value": ""}}
    event = _signed_event(gh, {"action": "ping"}, "ping")
    resp = gh.handler(event, None)
    assert resp["statusCode"] == 503


def test_ping_returns_pong_no_dispatch(gh):
    event = _signed_event(gh, {"action": "", "zen": "..."}, "ping")
    resp = gh.handler(event, None)
    assert resp["statusCode"] == 200
    gh.lambda_client.invoke.assert_not_called()


# --- issue assignment --------------------------------------------------------


def test_issue_assigned_to_bot_dispatches_preresolved(gh):
    body = {
        "action": "assigned",
        "assignee": {"login": "workitems-bot"},
        "issue": {"number": 42, "title": "Build X", "body": "do the thing"},
        "repository": {"full_name": "acme/web"},
        "sender": {"login": "alice", "type": "User"},
    }
    resp = gh.handler(_signed_event(gh, body, "issues"), None)
    assert resp["statusCode"] == 200
    p = _last_dispatch(gh)
    assert p["source"] == "github"
    assert p["trigger_type"] == "assignment"
    assert p["agent_id"] == "workitems"  # pre-resolved
    assert p["sender"] == "alice"
    assert p["context"]["repo"] == "acme/web"
    assert p["context"]["issue_number"] == 42
    assert p["context"]["trigger_type"] == "assignment"


def test_issue_assigned_to_unknown_user_no_dispatch(gh):
    body = {
        "action": "assigned",
        "assignee": {"login": "some-human"},
        "issue": {"number": 42},
        "repository": {"full_name": "acme/web"},
        "sender": {"login": "alice", "type": "User"},
    }
    resp = gh.handler(_signed_event(gh, body, "issues"), None)
    assert resp["statusCode"] == 200
    gh.lambda_client.invoke.assert_not_called()


# --- Projects V2 status change ----------------------------------------------


def _project_item_body(field_type="single_select", field_name="Status",
                       from_name="Todo", to_name="In Progress", action="edited"):
    return {
        "action": action,
        "projects_v2_item": {
            "node_id": "PVTI_abc",
            "content_type": "Issue",
            "content_node_id": "I_123",
            "project_number": 7,
        },
        "changes": {
            "field_value": {
                "field_type": field_type,
                "field_name": field_name,
                "from": {"name": from_name},
                "to": {"name": to_name},
            }
        },
        "sender": {"login": "alice", "type": "User"},
    }


def test_projects_v2_status_change_dispatches(gh):
    resp = gh.handler(_signed_event(gh, _project_item_body(), "projects_v2_item"), None)
    assert resp["statusCode"] == 200
    p = _last_dispatch(gh)
    assert p["source"] == "github"
    assert p["trigger_type"] == "project_item"
    # PM agent owns the board
    assert p.get("agent_id") == "workitems"
    ctx = p["context"]
    assert ctx["trigger_type"] == "project_item"
    assert ctx["project_number"] == 7
    assert ctx["item_id"] == "PVTI_abc"
    assert "Todo" in ctx["status_change"] and "In Progress" in ctx["status_change"]


def test_projects_v2_non_status_edit_ignored(gh):
    # A text-field or date edit must NOT dispatch (no storm).
    body = _project_item_body(field_type="text", field_name="Notes")
    resp = gh.handler(_signed_event(gh, body, "projects_v2_item"), None)
    assert resp["statusCode"] == 200
    gh.lambda_client.invoke.assert_not_called()


def test_projects_v2_status_by_field_name_even_if_type_differs(gh):
    # field_name == "status" should count even if field_type isn't single_select
    body = _project_item_body(field_type="", field_name="Status")
    resp = gh.handler(_signed_event(gh, body, "projects_v2_item"), None)
    assert resp["statusCode"] == 200
    assert gh.lambda_client.invoke.called


def test_projects_v2_realistic_payload_carries_node_id_not_fake_number(gh):
    # GitHub's real projects_v2_item payload has content_node_id + content_type,
    # NOT a nested content.number. issue_number must be empty (not invented), the
    # content_node_id must be carried for downstream resolution, and repo must
    # NOT be aliased to the org login (which would break the reply URL).
    body = _project_item_body()
    body["projects_v2_item"]["content_type"] = "Issue"
    body["projects_v2_item"]["content_node_id"] = "I_kwDOabc123"
    body["organization"] = {"login": "acme-org"}
    body.pop("repository", None)  # board spans repos — no repository in payload
    resp = gh.handler(_signed_event(gh, body, "projects_v2_item"), None)
    assert resp["statusCode"] == 200
    ctx = _last_dispatch(gh)["context"]
    assert ctx["issue_number"] == ""            # not fabricated
    assert ctx["content_node_id"] == "I_kwDOabc123"
    assert ctx["repo"] == ""                     # NOT "acme-org"


def test_projects_v2_explicit_content_number_is_used(gh):
    # The rare payload variant that DOES nest content.number should populate it.
    body = _project_item_body()
    body["projects_v2_item"]["content_type"] = "Issue"
    body["projects_v2_item"]["content"] = {"number": 99}
    resp = gh.handler(_signed_event(gh, body, "projects_v2_item"), None)
    assert resp["statusCode"] == 200
    assert _last_dispatch(gh)["context"]["issue_number"] == 99


def test_projects_v2_draft_content_has_no_issue_number(gh):
    body = _project_item_body()
    body["projects_v2_item"]["content_type"] = "DraftIssue"
    body["projects_v2_item"]["content_node_id"] = "DI_abc"
    resp = gh.handler(_signed_event(gh, body, "projects_v2_item"), None)
    assert resp["statusCode"] == 200
    ctx = _last_dispatch(gh)["context"]
    assert ctx["issue_number"] == ""
    assert ctx["content_node_id"] == ""  # only set for content_type == "Issue"


# --- issue_comment mention pass-through --------------------------------------


def test_issue_comment_with_mention_dispatches_without_preresolved_agent(gh):
    body = {
        "action": "created",
        "comment": {"body": "@workitems status please", "user": {"login": "bob"}},
        "issue": {"number": 9, "title": "T"},
        "repository": {"full_name": "acme/web"},
        "sender": {"login": "bob", "type": "User"},
    }
    resp = gh.handler(_signed_event(gh, body, "issue_comment"), None)
    assert resp["statusCode"] == 200
    p = _last_dispatch(gh)
    assert p["trigger_type"] == "comment_mention"
    assert "agent_id" not in p  # Router resolves from the live registry
    assert p["context"]["issue_number"] == 9


def test_issue_comment_without_mention_ignored(gh):
    body = {
        "action": "created",
        "comment": {"body": "just a normal human comment", "user": {"login": "bob"}},
        "issue": {"number": 9},
        "repository": {"full_name": "acme/web"},
        "sender": {"login": "bob", "type": "User"},
    }
    resp = gh.handler(_signed_event(gh, body, "issue_comment"), None)
    assert resp["statusCode"] == 200
    gh.lambda_client.invoke.assert_not_called()


# --- bot-loop prevention -----------------------------------------------------


def test_bot_sender_ignored_for_comment(gh):
    body = {
        "action": "created",
        "comment": {"body": "@workitems do x", "user": {"login": "some-bot"}},
        "issue": {"number": 9},
        "repository": {"full_name": "acme/web"},
        "sender": {"login": "some-bot", "type": "Bot"},
    }
    resp = gh.handler(_signed_event(gh, body, "issue_comment"), None)
    assert resp["statusCode"] == 200
    gh.lambda_client.invoke.assert_not_called()


def test_bot_sender_still_allowed_for_assignment(gh):
    # A bot/automation assigning a bot login is the whole point — not filtered.
    body = {
        "action": "assigned",
        "assignee": {"login": "workitems-bot"},
        "issue": {"number": 42, "title": "T", "body": ""},
        "repository": {"full_name": "acme/web"},
        "sender": {"login": "automation", "type": "Bot"},
    }
    resp = gh.handler(_signed_event(gh, body, "issues"), None)
    assert resp["statusCode"] == 200
    assert gh.lambda_client.invoke.called


# --- status-change helpers (unit) --------------------------------------------


def test_is_status_change_helper(gh):
    assert gh._is_status_change({"field_value": {"field_type": "single_select"}}) is True
    assert gh._is_status_change({"field_value": {"field_name": "Status"}}) is True
    assert gh._is_status_change({"field_value": {"field_type": "text", "field_name": "Notes"}}) is False
    assert gh._is_status_change({}) is False


def test_describe_status_change_helper(gh):
    changes = {"field_value": {"from": {"name": "Todo"}, "to": {"name": "Done"}}}
    assert gh._describe_status_change(changes) == "Todo → Done"
    assert gh._describe_status_change({"field_value": {"to": {"name": "Done"}}}) == "→ Done"


# --- base64-encoded body (API Gateway) ---------------------------------------


def test_base64_encoded_body_verified_and_dispatched(gh):
    # API Gateway may base64-encode the body; the handler must decode to the raw
    # bytes GitHub signed BEFORE computing the HMAC, then dispatch normally.
    import base64
    body = json.dumps({
        "action": "assigned",
        "assignee": {"login": "workitems-bot"},
        "issue": {"number": 7, "title": "T", "body": ""},
        "repository": {"full_name": "acme/web"},
        "sender": {"login": "alice", "type": "User"},
    })
    sig = "sha256=" + hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    event = {
        "headers": {"X-GitHub-Event": "issues", "X-Hub-Signature-256": sig},
        "body": base64.b64encode(body.encode()).decode(),
        "isBase64Encoded": True,
    }
    resp = gh.handler(event, None)
    assert resp["statusCode"] == 200
    assert _last_dispatch(gh)["agent_id"] == "workitems"


def test_invalid_base64_body_returns_400(gh):
    event = {
        "headers": {"X-GitHub-Event": "issues", "X-Hub-Signature-256": "sha256=x"},
        "body": "!!!not-base64!!!",
        "isBase64Encoded": True,
    }
    resp = gh.handler(event, None)
    assert resp["statusCode"] == 400


# --- status-column -> agent routing (PROJECT_STATUS_AGENT_MAP) ----------------


def test_project_item_routes_by_status_map(gh):
    # A move to "Needs Research" should dispatch to researcher, not the default.
    gh.PROJECT_STATUS_AGENT_MAP = {"needs research": "researcher"}
    body = _project_item_body(from_name="Todo", to_name="Needs Research")
    resp = gh.handler(_signed_event(gh, body, "projects_v2_item"), None)
    assert resp["statusCode"] == 200
    assert _last_dispatch(gh)["agent_id"] == "researcher"


def test_project_item_unmapped_status_falls_back_to_default(gh):
    gh.PROJECT_STATUS_AGENT_MAP = {"needs research": "researcher"}
    body = _project_item_body(from_name="Todo", to_name="In Progress")
    resp = gh.handler(_signed_event(gh, body, "projects_v2_item"), None)
    assert resp["statusCode"] == 200
    assert _last_dispatch(gh)["agent_id"] == gh.PROJECT_ITEM_AGENT


def test_parse_status_agent_map_helper(gh):
    m = gh._parse_status_agent_map("Needs Research:researcher, In Progress:workitems")
    assert m == {"needs research": "researcher", "in progress": "workitems"}
    assert gh._parse_status_agent_map("") == {}
    assert gh._parse_status_agent_map("garbage,no-colon") == {}
