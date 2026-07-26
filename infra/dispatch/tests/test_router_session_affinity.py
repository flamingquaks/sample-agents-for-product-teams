"""Tests for AgentCore runtime-session affinity (warm-session reuse).

A Slack thread derives one deterministic ``runtimeSessionId`` per (workspace,
channel, thread_ts, agent), so a reply within AgentCore's ~15-minute idle
window lands on the still-warm microVM that served the first request. Covers:
the id derivation, its use on dispatch AND resume invokes, and the
RetryableConflictException fallback to a fresh per-assignment session.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REGISTRY = {
    "agents": {
        "docwriter": {
            "agent_id": "docwriter",
            "runtime_arn": "arn:aws:bedrock-agentcore:us-west-2:123:runtime/dw",
            "limits": {"max_concurrent": 5},
        }
    }
}

SLACK_CTX = {"workspace": "T1", "channel_id": "C1", "thread_ts": "111.222"}


@pytest.fixture
def router(monkeypatch):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-test")
    monkeypatch.setenv("ASSIGNMENTS_TABLE", "t")
    monkeypatch.setenv("STAGE", "dev")
    with patch("boto3.resource"), patch("boto3.client"):
        for m in ("router", "guardrail", "reply"):
            sys.modules.pop(m, None)
        import router as router_mod
    router_mod._registry_cache = REGISTRY
    router_mod._registry_expires_at = float("inf")
    router_mod.agentcore = MagicMock()
    return router_mod


def _conflict():
    return ClientError(
        {"Error": {"Code": "RetryableConflictException", "Message": "session busy"}},
        "InvokeAgentRuntime",
    )


# --- thread_runtime_session_id -----------------------------------------------


def test_session_id_deterministic_per_thread(router):
    a = router.thread_runtime_session_id("docwriter", "slack", SLACK_CTX)
    b = router.thread_runtime_session_id("docwriter", "slack", dict(SLACK_CTX))
    assert a == b
    assert a.startswith("thread-")
    # Must satisfy the InvokeAgentRuntime bound (33–256 chars).
    assert 33 <= len(a) <= 256


def test_session_id_distinct_per_agent_and_thread(router):
    base = router.thread_runtime_session_id("docwriter", "slack", SLACK_CTX)
    other_agent = router.thread_runtime_session_id("workitems", "slack", SLACK_CTX)
    other_thread = router.thread_runtime_session_id(
        "docwriter", "slack", {**SLACK_CTX, "thread_ts": "333.444"}
    )
    assert len({base, other_agent, other_thread}) == 3


def test_session_id_none_off_slack_or_incomplete_context(router):
    assert router.thread_runtime_session_id("docwriter", "github", SLACK_CTX) is None
    assert router.thread_runtime_session_id("docwriter", "slack", None) is None
    for missing in ("workspace", "channel_id", "thread_ts"):
        ctx = {k: v for k, v in SLACK_CTX.items() if k != missing}
        assert router.thread_runtime_session_id("docwriter", "slack", ctx) is None


# --- invoke_agent wiring ------------------------------------------------------


def test_slack_dispatch_uses_thread_session_id(router):
    router.invoke_agent(
        REGISTRY["agents"]["docwriter"], "write docs", "slack", SLACK_CTX, "a-1"
    )
    call = router.agentcore.invoke_agent_runtime.call_args
    expected = router.thread_runtime_session_id("docwriter", "slack", SLACK_CTX)
    assert call.kwargs["runtimeSessionId"] == expected


def test_nonslack_dispatch_uses_assignment_session_id(router):
    router.invoke_agent(
        REGISTRY["agents"]["docwriter"],
        "write docs",
        "github",
        {"repo": "acme/web"},
        "a-2",
    )
    call = router.agentcore.invoke_agent_runtime.call_args
    assert call.kwargs["runtimeSessionId"] == "a-2"


def test_busy_thread_session_falls_back_to_fresh(router):
    """Two quick messages in one thread: the second invoke hits
    RetryableConflictException on the shared session and must retry ONCE on
    the per-assignment id instead of failing the dispatch."""
    router.agentcore.invoke_agent_runtime.side_effect = [_conflict(), {}]
    router.invoke_agent(
        REGISTRY["agents"]["docwriter"], "write docs", "slack", SLACK_CTX, "a-3"
    )
    calls = router.agentcore.invoke_agent_runtime.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["runtimeSessionId"].startswith("thread-")
    assert calls[1].kwargs["runtimeSessionId"] == "a-3"
    # Same payload both times — only the session placement changes.
    assert calls[0].kwargs["payload"] == calls[1].kwargs["payload"]


def test_conflict_on_fallback_session_raises(router):
    """When the per-assignment session itself conflicts (no thread id in play)
    there is nothing safe to fall back to — the error must propagate."""
    router.agentcore.invoke_agent_runtime.side_effect = _conflict()
    with pytest.raises(ClientError):
        router.invoke_agent(
            REGISTRY["agents"]["docwriter"], "x", "github", {"repo": "a/b"}, "a-4"
        )
    assert router.agentcore.invoke_agent_runtime.call_count == 1


def test_nonconflict_client_error_raises_without_retry(router):
    router.agentcore.invoke_agent_runtime.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "InvokeAgentRuntime",
    )
    with pytest.raises(ClientError):
        router.invoke_agent(
            REGISTRY["agents"]["docwriter"], "x", "slack", SLACK_CTX, "a-5"
        )
    assert router.agentcore.invoke_agent_runtime.call_count == 1


# --- resume affinity ----------------------------------------------------------


def test_resume_reuses_thread_session_id(router, monkeypatch):
    """The whole point: a resume reply must target the SAME runtimeSessionId
    the original dispatch used, so it lands on the warm microVM that paused."""
    import identity as identity_mod

    router.assignments_table = _fake_table(
        {
            "a-1": {
                "assignment_id": "a-1",
                "agent_id": "docwriter",
                "status": "awaiting_input",
                "source": "slack",
                "interrupt_id": "int-7",
                "workspace_snapshot": [],
                "source_context": SLACK_CTX,
            }
        }
    )
    monkeypatch.setattr(router, "_post_block_reply", lambda *a, **k: True)
    monkeypatch.setattr(router, "_put_metric", lambda *a, **k: None)
    monkeypatch.setattr(router, "authorize_trigger", lambda *a, **k: (True, ""))
    monkeypatch.setattr(
        router,
        "resolve_dispatch_identity",
        lambda *a, **k: identity_mod.Identity(
            identity_id="id-1", email="a@b.c", status="active"
        ),
    )
    monkeypatch.setattr(router, "check_concurrency", lambda *a, **k: True)
    with patch("guardrail.check_prompt") as gp:
        gp.return_value = MagicMock(outcome="passed", reason="")
        resp = router.handler(
            {
                "resume_of": "a-1",
                "body": "use us-east-1",
                "sender": "slack:T1:U1",
                "context": SLACK_CTX,
            },
            None,
        )
    assert resp["statusCode"] == 200
    call = router.agentcore.invoke_agent_runtime.call_args
    dispatch_session = router.thread_runtime_session_id("docwriter", "slack", SLACK_CTX)
    assert call.kwargs["runtimeSessionId"] == dispatch_session
    payload = json.loads(call.kwargs["payload"])
    assert payload["resume"]["interrupt_id"] == "int-7"


def _fake_table(rows):
    """Minimal assignments-table stand-in for the resume path."""

    class _CondFail(Exception):
        pass

    class _T:
        def __init__(self):
            self.rows = rows
            self.meta = MagicMock()
            self.meta.client.exceptions.ConditionalCheckFailedException = _CondFail

        def get_item(self, Key, **kwargs):
            item = self.rows.get(Key["assignment_id"])
            return {"Item": item} if item else {}

        def query(self, **kwargs):
            return {"Count": 0}

        def update_item(self, Key, **kwargs):
            row = self.rows.setdefault(
                Key["assignment_id"], {"assignment_id": Key["assignment_id"]}
            )
            cond = kwargs.get("ConditionExpression")
            values = kwargs.get("ExpressionAttributeValues", {})
            if cond == "#s = :awaiting" and row.get("status") != values.get(
                ":awaiting"
            ):
                raise _CondFail()
            if ":resuming" in values:
                row["status"] = values[":resuming"]

    return _T()


# --- Slack permalink capture (dashboard → Slack traceability) ------------------


def _assignment_row(router):
    """The assignment Item among the fixture mock's put_item calls (a Slack
    dispatch also puts a thread_binding bookkeeping row after it)."""
    for call in router.assignments_table.put_item.call_args_list:
        item = call.kwargs.get("Item") or (call.args[0] if call.args else {})
        if item.get("kind") != "thread_binding" and "source_context" in item:
            return item
    raise AssertionError("no assignment row was written")


def _stub_dispatch_pipeline(router, monkeypatch):
    import identity as identity_mod

    monkeypatch.setattr(router, "_post_block_reply", lambda *a, **k: True)
    monkeypatch.setattr(router, "_put_metric", lambda *a, **k: None)
    monkeypatch.setattr(router, "_notify_fleet_event", lambda *a, **k: None)
    monkeypatch.setattr(router, "authorize_trigger", lambda *a, **k: (True, ""))
    monkeypatch.setattr(router, "check_repo_allowed", lambda *a, **k: True)
    monkeypatch.setattr(router, "check_concurrency", lambda *a, **k: True)
    monkeypatch.setattr(
        router,
        "resolve_dispatch_identity",
        lambda *a, **k: identity_mod.Identity(
            identity_id="id-1", email="a@b.c", status="active"
        ),
    )
    router.assignments_table = MagicMock()
    router.assignments_table.query.return_value = {"Count": 0}


def test_slack_dispatch_captures_permalink_on_assignment(router, monkeypatch):
    """The router resolves the triggering message's permalink once and stores
    it in the assignment's source_context — the dashboard's link back to the
    conversation."""
    _stub_dispatch_pipeline(router, monkeypatch)
    with patch("guardrail.check_prompt") as gp, patch(
        "reply.slack_permalink",
        return_value="https://acme.slack.com/archives/C1/p111222",
    ) as pl:
        gp.return_value = MagicMock(outcome="passed", reason="")
        resp = router.handler(
            {
                "source": "slack",
                "trigger_type": "comment_mention",
                "agent_id": "docwriter",
                "body": "write docs",
                "instruction": "write docs",
                "sender": "slack:T1:U1",
                "context": {**SLACK_CTX, "message_ts": "111.222"},
            },
            None,
        )
    assert resp["statusCode"] == 200
    pl.assert_called_once_with("T1", "C1", "111.222")
    stored = _assignment_row(router)
    assert (
        stored["source_context"]["slack_permalink"]
        == "https://acme.slack.com/archives/C1/p111222"
    )


def test_permalink_miss_dispatches_without_link(router, monkeypatch):
    """A failed permalink lookup (returns "") must not block dispatch or write
    an empty key."""
    _stub_dispatch_pipeline(router, monkeypatch)
    with patch("guardrail.check_prompt") as gp, patch(
        "reply.slack_permalink", return_value=""
    ):
        gp.return_value = MagicMock(outcome="passed", reason="")
        resp = router.handler(
            {
                "source": "slack",
                "trigger_type": "comment_mention",
                "agent_id": "docwriter",
                "body": "write docs",
                "instruction": "write docs",
                "sender": "slack:T1:U1",
                "context": {**SLACK_CTX, "message_ts": "111.222"},
            },
            None,
        )
    assert resp["statusCode"] == 200
    stored = _assignment_row(router)
    assert "slack_permalink" not in stored["source_context"]


def test_nonslack_dispatch_skips_permalink(router, monkeypatch):
    _stub_dispatch_pipeline(router, monkeypatch)
    with patch("guardrail.check_prompt") as gp, patch(
        "reply.slack_permalink"
    ) as pl:
        gp.return_value = MagicMock(outcome="passed", reason="")
        router.handler(
            {
                "source": "github",
                "trigger_type": "comment_mention",
                "agent_id": "docwriter",
                "body": "write docs",
                "sender": "alice",
                "context": {"repo": "acme/web", "issue_number": 7},
            },
            None,
        )
    pl.assert_not_called()
