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
