"""Tests for assignment_notifier.py — the DynamoDB-stream notifier that fans
agent-side status transitions out to Slack (spec §18.1).

notify.notify is patched; these cover the status-transition → tier/event mapping,
the no-op cases (INSERT, no status change, bookkeeping rows), and the actor
namespacing from the recorded requester.
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import assignment_notifier as an  # noqa: E402


def _img(d: dict) -> dict:
    """Wrap a plain dict as a DynamoDB stream image (string/number attrs only —
    enough for the fields the notifier reads)."""
    out = {}
    for k, v in d.items():
        if isinstance(v, bool):
            out[k] = {"BOOL": v}
        elif isinstance(v, (int, float)):
            out[k] = {"N": str(v)}
        elif isinstance(v, dict):
            out[k] = {"M": _img(v)}
        else:
            out[k] = {"S": str(v)}
    return out


def _record(event_name="MODIFY", new=None, old=None):
    ddb = {}
    if new is not None:
        ddb["NewImage"] = _img(new)
    if old is not None:
        ddb["OldImage"] = _img(old)
    return {"eventName": event_name, "dynamodb": ddb}


def test_completed_transition_notifies_informative():
    rec = _record(
        new={"assignment_id": "a1", "agent_id": "workitems", "status": "completed",
             "requester": "github:alice", "source": "github",
             "source_context": {"repo": "acme/web"}},
        old={"assignment_id": "a1", "status": "dispatched"},
    )
    with patch.object(an.notify, "notify", return_value=1) as m:
        an.handler({"Records": [rec]})
    assert m.call_count == 1
    kw = m.call_args.kwargs
    assert kw["tier"] == "informative" and kw["event"] == "run_completed"
    assert kw["repo"] == "acme/web" and kw["unit"] == "a1"
    assert kw["actor"] == {"source": "github", "handle": "alice", "workspace": ""}


def test_failed_transition_notifies_error():
    rec = _record(
        new={"assignment_id": "a2", "agent_id": "adr", "status": "failed",
             "requester": "asana:12009", "result_summary": "boom"},
        old={"status": "dispatched"},
    )
    with patch.object(an.notify, "notify", return_value=1) as m:
        an.handler({"Records": [rec]})
    kw = m.call_args.kwargs
    assert kw["tier"] == "error" and kw["event"] == "run_failed"
    assert kw["actor"] == {"source": "asana", "handle": "12009", "workspace": ""}


def test_awaiting_approval_is_actionable():
    rec = _record(
        new={"assignment_id": "a3", "agent_id": "workitems", "status": "awaiting_approval",
             "requester": "slack:T0ACME01:U9"},
        old={"status": "dispatched"},
    )
    with patch.object(an.notify, "notify", return_value=1) as m:
        an.handler({"Records": [rec]})
    kw = m.call_args.kwargs
    assert kw["tier"] == "actionable" and kw["event"] == "awaiting_approval"
    assert kw["actor"] == {"source": "slack", "handle": "U9", "workspace": "T0ACME01"}


def test_insert_is_noop():
    # INSERT is the router's create — it emits run_started itself; the stream
    # notifier must not double-notify.
    rec = _record(event_name="INSERT", new={"assignment_id": "a4", "status": "dispatched"})
    with patch.object(an.notify, "notify") as m:
        an.handler({"Records": [rec]})
    m.assert_not_called()


def test_no_status_change_is_noop():
    # A token-usage update to an already-completed row (status unchanged).
    rec = _record(
        new={"assignment_id": "a5", "status": "completed", "token_usage": 100},
        old={"assignment_id": "a5", "status": "completed"},
    )
    with patch.object(an.notify, "notify") as m:
        an.handler({"Records": [rec]})
    m.assert_not_called()


def test_unmapped_status_is_noop():
    rec = _record(new={"assignment_id": "a6", "status": "dispatched"}, old={"status": ""})
    with patch.object(an.notify, "notify") as m:
        an.handler({"Records": [rec]})
    m.assert_not_called()


def test_bookkeeping_rows_skipped():
    # notif_thread# / slack_event_dedup rows share the table but aren't runs.
    for kind in ("slack_event_dedup", "notif_thread"):
        rec = _record(new={"assignment_id": f"x#{kind}", "kind": kind, "status": "whatever"},
                      old={"status": "other"})
        with patch.object(an.notify, "notify") as m:
            an.handler({"Records": [rec]})
        m.assert_not_called()


def test_one_bad_record_does_not_fail_batch():
    good = _record(
        new={"assignment_id": "a7", "agent_id": "workitems", "status": "completed",
             "requester": "github:alice"},
        old={"status": "dispatched"},
    )
    bad = {"eventName": "MODIFY"}  # malformed — no dynamodb key
    with patch.object(an.notify, "notify", return_value=1) as m:
        resp = an.handler({"Records": [bad, good]})
    assert resp["statusCode"] == 200
    assert m.call_count == 1  # the good record still processed


# --- origin reply (the user-facing answer back to the Slack thread) -----------


def _slack_completed_record(summary="Here is your research result.", thread_ts="123.456"):
    ctx = {"workspace": "T0ACME01", "channel_id": "C0ENG"}
    if thread_ts:
        ctx["thread_ts"] = thread_ts
    return _record(
        new={"assignment_id": "a7", "agent_id": "researcher", "status": "completed",
             "requester": "slack:T0ACME01:U9", "source": "slack",
             "result_summary": summary, "source_context": ctx},
        old={"status": "dispatched"},
    )


def test_completed_slack_run_posts_result_to_origin_thread():
    rec = _slack_completed_record()
    with patch.object(an.notify, "notify", return_value=1), \
         patch.object(an.reply, "post_slack_message", return_value=True) as post:
        an.handler({"Records": [rec]})
    assert post.call_count == 1
    args, kwargs = post.call_args
    assert args[0] == "T0ACME01" and args[1] == "C0ENG"
    assert "Here is your research result." in args[2]
    assert kwargs["thread_ts"] == "123.456"
    assert kwargs["agent_id"] == "researcher"


def test_failed_slack_run_posts_error_to_origin():
    rec = _record(
        new={"assignment_id": "a8", "agent_id": "docwriter", "status": "failed",
             "requester": "slack:T0ACME01:U9", "source": "slack",
             "result_summary": "gateway timed out",
             "source_context": {"workspace": "T0ACME01", "channel_id": "C0ENG"}},
        old={"status": "dispatched"},
    )
    with patch.object(an.notify, "notify", return_value=1), \
         patch.object(an.reply, "post_slack_message", return_value=True) as post:
        an.handler({"Records": [rec]})
    text = post.call_args[0][2]
    assert "couldn't complete" in text and "gateway timed out" in text


def test_non_slack_run_does_not_post_origin_reply():
    rec = _record(
        new={"assignment_id": "a9", "agent_id": "workitems", "status": "completed",
             "requester": "github:alice", "source": "github",
             "source_context": {"repo": "acme/web"}},
        old={"status": "dispatched"},
    )
    with patch.object(an.notify, "notify", return_value=1), \
         patch.object(an.reply, "post_slack_message") as post:
        an.handler({"Records": [rec]})
    post.assert_not_called()


def test_origin_reply_failure_does_not_block_fanout():
    rec = _slack_completed_record()
    with patch.object(an.notify, "notify", return_value=1) as fanout, \
         patch.object(an.reply, "post_slack_message", side_effect=RuntimeError("slack down")):
        an.handler({"Records": [rec]})
    fanout.assert_called_once()


def test_long_result_truncated_for_slack():
    rec = _slack_completed_record(summary="x" * 5000)
    with patch.object(an.notify, "notify", return_value=1), \
         patch.object(an.reply, "post_slack_message", return_value=True) as post:
        an.handler({"Records": [rec]})
    text = post.call_args[0][2]
    assert len(text) < 4000 and "…" in text


# --- awaiting_input (durable pause, durable-repo-work spec) ---------------------


def test_awaiting_input_is_actionable_fanout():
    rec = _record(
        new={"assignment_id": "a7", "agent_id": "docwriter", "status": "awaiting_input",
             "requester": "slack:T1:U9", "pending_question": "Which region?"},
        old={"status": "dispatched"},
    )
    with patch.object(an.notify, "notify", return_value=1) as m:
        an.handler({"Records": [rec]})
    kw = m.call_args.kwargs
    assert kw["tier"] == "actionable" and kw["event"] == "awaiting_input"


def test_awaiting_input_posts_question_with_resume_copy():
    """The origin-thread reply carries the agent's question AND the how-to-
    resume instructions (D6: reply @sdlc-agents in-thread)."""
    rec = _record(
        new={"assignment_id": "a7", "agent_id": "docwriter", "status": "awaiting_input",
             "requester": "slack:T1:U9", "source": "slack",
             "pending_question": "Deploy to us-east-1 or us-west-2?",
             "source_context": {"workspace": "T1", "channel_id": "C1", "thread_ts": "9.9"}},
        old={"status": "dispatched"},
    )
    with patch.object(an.notify, "notify", return_value=1), \
         patch.object(an.reply, "post_slack_message", return_value=True) as post:
        an.handler({"Records": [rec]})
    assert post.call_count == 1
    args, kwargs = post.call_args
    text = args[2]
    assert "Deploy to us-east-1 or us-west-2?" in text
    assert "@sdlc-agents" in text
    assert kwargs["thread_ts"] == "9.9"
    assert kwargs["agent_id"] == "docwriter"


def test_thread_binding_rows_skipped():
    rec = _record(
        new={"assignment_id": "thread_binding#T1#C1#9.9", "kind": "thread_binding",
             "status": "awaiting_input"},
        old={"status": "x"},
    )
    with patch.object(an.notify, "notify") as m:
        an.handler({"Records": [rec]})
    m.assert_not_called()


# --- dashboard deep links + conversation-keyed fan-out threading --------------


def test_origin_reply_links_assignment_to_dashboard(monkeypatch):
    """When DASHBOARD_URL is set, the result reply's assignment footer is a
    Slack-markup deep link to the run's dashboard page."""
    monkeypatch.setenv("DASHBOARD_URL", "https://d123.cloudfront.net/")
    rec = _slack_completed_record()
    with patch.object(an.notify, "notify", return_value=1), \
         patch.object(an.reply, "post_slack_message", return_value=True) as post:
        an.handler({"Records": [rec]})
    text = post.call_args[0][2]
    assert "<https://d123.cloudfront.net/#/run/" in text
    assert "|assignment `" in text


def test_origin_reply_bare_id_without_dashboard(monkeypatch):
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    rec = _slack_completed_record()
    with patch.object(an.notify, "notify", return_value=1), \
         patch.object(an.reply, "post_slack_message", return_value=True) as post:
        an.handler({"Records": [rec]})
    text = post.call_args[0][2]
    assert "assignment `" in text and "<http" not in text


def test_fanout_unit_keys_on_slack_conversation():
    """Fan-out notifications for a Slack-threaded run must key on the
    conversation (so linked follow-up assignments continue the same ops
    thread), not the assignment id."""
    rec = _record(
        new={"assignment_id": "a9", "agent_id": "docwriter", "status": "completed",
             "requester": "slack:T1:U9", "source": "slack",
             "result_summary": "done",
             "source_context": {"workspace": "T1", "channel_id": "C1",
                                "thread_ts": "111.2"}},
        old={"status": "dispatched"},
    )
    with patch.object(an.notify, "notify", return_value=1) as m, \
         patch.object(an.reply, "post_slack_message", return_value=True):
        an.handler({"Records": [rec]})
    assert m.call_args.kwargs["unit"] == "thread:T1#C1#111.2"
