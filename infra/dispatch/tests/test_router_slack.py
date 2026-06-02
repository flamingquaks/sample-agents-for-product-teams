"""Unit tests for the Dispatch Router's Slack reply path.

Exercises both the low-level _post_block_reply slack branch and the handler
end-to-end for slack-sourced events with a pre-resolved agent_id. Bedrock /
DynamoDB / reply clients are mocked. Fixture style mirrors
test_router_guardrail.py.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


REGISTRY = {
    "agents": {
        "workitems": {
            "runtime_arn": "arn:aws:bedrock-agentcore:us-west-2:123:runtime/wi",
            "authorization": {"users": ["U_ALICE"]},
            "limits": {"max_concurrent": 5},
        }
    }
}


@pytest.fixture
def router(monkeypatch):
    """Import router with env primed and external clients mocked."""
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-test")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
    monkeypatch.setenv("ASSIGNMENTS_TABLE", "t")
    monkeypatch.setenv("STAGE", "dev")

    with patch("boto3.resource"), patch("boto3.client"):
        if "router" in sys.modules:
            del sys.modules["router"]
        if "guardrail" in sys.modules:
            del sys.modules["guardrail"]
        if "reply" in sys.modules:
            del sys.modules["reply"]
        import router as router_mod  # noqa: E402
    router_mod._registry_cache = REGISTRY
    # Avoid real DynamoDB calls:
    router_mod.assignments_table = MagicMock()
    router_mod.assignments_table.query.return_value = {"Count": 0}
    yield router_mod


def _event_slack(body="@workitems hi", instruction="hi", sender="U_ALICE"):
    return {
        "source": "slack",
        "trigger_type": "mention",
        "agent_id": "workitems",
        "body": body,
        "instruction": instruction,
        "sender": sender,
        "context": {"channel_id": "C1", "thread_ts": "170.1"},
    }


# --- Case 7: _post_block_reply slack branch ---------------------------------


def test_post_block_reply_slack_calls_post_slack_message(router):
    with patch("reply.post_slack_message", return_value=True) as mock_post:
        ok = router._post_block_reply(
            "slack",
            {"channel_id": "C1", "thread_ts": "170.1"},
            "blocked",
        )
    assert ok is True
    mock_post.assert_called_once_with(
        channel_id="C1",
        body="blocked",
        thread_ts="170.1",
    )


# --- Case 8: guardrail block on a slack event -------------------------------


def test_guardrail_block_on_slack_posts_reply_and_returns_400(router):
    with patch("guardrail.check_prompt") as mock_check, \
         patch("reply.post_slack_message", return_value=True) as mock_post, \
         patch.object(router, "invoke_agent") as mock_invoke:
        mock_check.return_value = MagicMock(outcome="blocked", reason="PROMPT_ATTACK")
        resp = router.handler(_event_slack(), None)
    assert resp["statusCode"] == 400
    assert "blocked" in json.loads(resp["body"])["error"].lower()
    mock_post.assert_called_once()
    # The reply went to the slack channel/thread from the event context.
    kwargs = mock_post.call_args.kwargs
    assert kwargs["channel_id"] == "C1"
    assert kwargs["thread_ts"] == "170.1"
    mock_invoke.assert_not_called()
    calls = router.assignments_table.update_item.call_args_list
    joined = "".join(str(c) for c in calls)
    assert "blocked_guardrail" in joined


# --- Case 9: passing slack event dispatches ---------------------------------


def test_guardrail_pass_on_slack_dispatches_pre_resolved_agent(router):
    with patch("guardrail.check_prompt") as mock_check, \
         patch.object(router, "invoke_agent") as mock_invoke:
        mock_check.return_value = MagicMock(outcome="passed", reason="")
        resp = router.handler(_event_slack(), None)
    assert resp["statusCode"] == 200
    mock_invoke.assert_called_once()
    # The pre-resolved workitems agent was invoked.
    agent_config = mock_invoke.call_args.args[0]
    assert agent_config["agent_id"] == "workitems"


# --- Case 10: sender not in allowlist -> 403 fail-closed --------------------


def test_slack_sender_not_in_allowlist_returns_403(router):
    with patch("guardrail.check_prompt") as mock_check, \
         patch.object(router, "invoke_agent") as mock_invoke:
        mock_check.return_value = MagicMock(outcome="passed", reason="")
        resp = router.handler(_event_slack(sender="U_MALLORY"), None)
    assert resp["statusCode"] == 403
    mock_invoke.assert_not_called()
