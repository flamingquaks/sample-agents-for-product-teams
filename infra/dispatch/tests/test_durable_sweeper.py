"""Tests for the durable-work sweeper (durable_sweeper.py, spec Phase 3).

Covers the abandoned-pause timeout (flip + wip cleanup), the stuck-resume
revert, the mid-sweep race (conditional flip loses → no cleanup), and the
age windows. DynamoDB and the GitHub branch delete are faked.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import durable_sweeper as sweeper

NOW = 1_700_000_000


class FakeTable:
    class _CondFail(Exception):
        pass

    def __init__(self, rows):
        self.rows = {r["assignment_id"]: dict(r) for r in rows}

        class _Meta:
            pass

        self.meta = _Meta()
        self.meta.client = _Meta()
        self.meta.client.exceptions = _Meta()
        self.meta.client.exceptions.ConditionalCheckFailedException = self._CondFail

    def scan(self, **kwargs):
        # The filter is status + agent_id-exists; emulate just enough.
        items = [r for r in self.rows.values() if r.get("agent_id")]
        return {"Items": items}

    def update_item(self, Key, ConditionExpression=None, **kwargs):
        row = self.rows[Key["assignment_id"]]
        values = kwargs.get("ExpressionAttributeValues", {})
        if ConditionExpression and row.get("status") != values.get(":from"):
            raise self._CondFail()
        row["status"] = values[":to"]


@pytest.fixture
def deleted_branches(monkeypatch):
    deleted = []
    monkeypatch.setattr(
        sweeper, "_delete_wip_branches",
        lambda row: deleted.append(row["assignment_id"]),
    )
    return deleted


def _use(monkeypatch, rows):
    table = FakeTable(rows)
    monkeypatch.setattr(sweeper, "_table", lambda: table)
    return table


def test_old_pause_times_out_and_cleans_wip(monkeypatch, deleted_branches):
    table = _use(monkeypatch, [{
        "assignment_id": "a-old", "agent_id": "docwriter",
        "status": "awaiting_input",
        "paused_at": NOW - 72 * 3600,
        "workspace_snapshot": [{"repo": "acme/web", "branch": "wip/a-old", "sha": "x"}],
    }])
    assert sweeper.sweep_abandoned_pauses(now=NOW) == 1
    assert table.rows["a-old"]["status"] == "timed_out"
    assert deleted_branches == ["a-old"]


def test_fresh_pause_left_alone(monkeypatch, deleted_branches):
    table = _use(monkeypatch, [{
        "assignment_id": "a-new", "agent_id": "docwriter",
        "status": "awaiting_input", "paused_at": NOW - 3600,
    }])
    assert sweeper.sweep_abandoned_pauses(now=NOW) == 0
    assert table.rows["a-new"]["status"] == "awaiting_input"
    assert deleted_branches == []


def test_race_reply_wins_no_cleanup(monkeypatch, deleted_branches):
    """A reply landing mid-sweep flips the row first — the sweep's conditional
    write loses and MUST NOT delete the wip branches out from under the resume."""
    table = _use(monkeypatch, [{
        "assignment_id": "a-race", "agent_id": "docwriter",
        "status": "awaiting_input", "paused_at": NOW - 72 * 3600,
        "workspace_snapshot": [{"repo": "acme/web", "branch": "wip/a-race", "sha": "x"}],
    }])
    original = table.update_item

    def racing_update(Key, ConditionExpression=None, **kwargs):
        table.rows[Key["assignment_id"]]["status"] = "resuming"  # reply lands first
        return original(Key, ConditionExpression=ConditionExpression, **kwargs)

    table.update_item = racing_update
    assert sweeper.sweep_abandoned_pauses(now=NOW) == 0
    assert deleted_branches == []


def test_stuck_resume_reverts_to_awaiting(monkeypatch):
    table = _use(monkeypatch, [{
        "assignment_id": "a-stuck", "agent_id": "docwriter",
        "status": "resuming",
        "resume_started_at": NOW - 3600,
        "interrupt_id": "int-1", "pending_question": "Q?",
    }])
    assert sweeper.sweep_stuck_resumes(now=NOW) == 1
    assert table.rows["a-stuck"]["status"] == "awaiting_input"
    # Pause fields untouched — the thread is resumable again.
    assert table.rows["a-stuck"]["interrupt_id"] == "int-1"


def test_recent_resume_left_alone(monkeypatch):
    table = _use(monkeypatch, [{
        "assignment_id": "a-live", "agent_id": "docwriter",
        "status": "resuming", "resume_started_at": NOW - 60,
    }])
    assert sweeper.sweep_stuck_resumes(now=NOW) == 0
    assert table.rows["a-live"]["status"] == "resuming"


def test_wip_cleanup_only_deletes_wip_refs(monkeypatch):
    """The branch delete must never touch a non-wip ref, whatever the row says."""
    calls = []

    class FakeResp:
        status_code = 204

    import requests

    monkeypatch.setattr(
        sweeper.github_app, "scoped_installation_token", lambda repo, permissions: "t"
    )
    monkeypatch.setattr(
        requests, "delete", lambda url, **kw: calls.append(url) or FakeResp()
    )
    sweeper._delete_wip_branches({
        "workspace_snapshot": [
            {"repo": "acme/web", "branch": "wip/a-1"},
            {"repo": "acme/web", "branch": "main"},
            {"repo": "", "branch": "wip/a-1"},
        ]
    })
    assert len(calls) == 1
    assert calls[0].endswith("/repos/acme/web/git/refs/heads/wip/a-1")


def test_handler_runs_all_sweeps(monkeypatch):
    _use(monkeypatch, [])
    out = sweeper.handler()
    assert out == {"timed_out": 0, "resumes_reverted": 0, "stale_failed": 0}


# --- stale dispatched (dead runtime) sweep --------------------------------------


def test_stale_dispatch_fails(monkeypatch):
    table = _use(monkeypatch, [{
        "assignment_id": "a-ghost", "agent_id": "docwriter",
        "status": "dispatched", "created_at": NOW - 6 * 3600,
    }])
    assert sweeper.sweep_stale_dispatches(now=NOW) == 1
    assert table.rows["a-ghost"]["status"] == "failed"


def test_recent_dispatch_left_alone(monkeypatch):
    table = _use(monkeypatch, [{
        "assignment_id": "a-run", "agent_id": "docwriter",
        "status": "dispatched", "created_at": NOW - 600,
    }])
    assert sweeper.sweep_stale_dispatches(now=NOW) == 0
    assert table.rows["a-run"]["status"] == "dispatched"


def test_resumed_dispatch_ages_from_resumed_at(monkeypatch):
    """Regression: a run resumed hours after creation must NOT be swept —
    staleness keys on resumed_at when present."""
    table = _use(monkeypatch, [{
        "assignment_id": "a-resumed", "agent_id": "docwriter",
        "status": "dispatched",
        "created_at": NOW - 20 * 3600,
        "resumed_at": NOW - 600,
    }])
    assert sweeper.sweep_stale_dispatches(now=NOW) == 0
    assert table.rows["a-resumed"]["status"] == "dispatched"


def test_stale_resumed_dispatch_still_swept(monkeypatch):
    table = _use(monkeypatch, [{
        "assignment_id": "a-dead", "agent_id": "docwriter",
        "status": "dispatched",
        "created_at": NOW - 20 * 3600,
        "resumed_at": NOW - 6 * 3600,
    }])
    assert sweeper.sweep_stale_dispatches(now=NOW) == 1
    assert table.rows["a-dead"]["status"] == "failed"
