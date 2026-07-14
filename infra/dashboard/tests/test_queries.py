"""Tests for the dashboard DynamoDB query layer.

A FakeTable emulates just enough of the boto3 resource Table API — a
``query`` over the fleet index with ExclusiveStartKey paging and a ``get_item``
— to exercise pagination, filtering, trace, and stats without AWS.
"""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import queries  # noqa: E402


def test_encode_cursor_handles_decimal():
    # Regression: DynamoDB returns Decimal numbers; json.dumps must not choke.
    key = {
        "assignment_id": "a-1",
        "gsi_all": "run",
        "created_at": Decimal("1784041404"),
    }
    token = queries._encode_cursor(key)
    assert token
    assert queries._decode_cursor(token) == {
        "assignment_id": "a-1",
        "gsi_all": "run",
        "created_at": 1784041404,  # integral Decimal → int on round-trip
    }


def _run(i, **over):
    """Build a fake assignment row. created_at descends with i so newest-first
    ordering is deterministic in the fake."""
    row = {
        "assignment_id": f"a-{i}",
        "gsi_all": queries.ALL_RUNS_PK,
        "created_at": 10_000 - i,
        "agent_id": "workitems",
        "status": "dispatched",
        "source": "github",
        "requester": "alice",
        "trace_refs": {"repo": "org/app", "issue_number": str(i)},
    }
    row.update(over)
    return row


class FakeTable:
    def __init__(self, rows):
        # rows already in the desired newest-first order.
        self.rows = rows

    def query(self, **kwargs):
        assert kwargs["IndexName"] == queries.ALL_RUNS_INDEX
        assert kwargs["ScanIndexForward"] is False
        limit = kwargs.get("Limit", len(self.rows))
        start = 0
        esk = kwargs.get("ExclusiveStartKey")
        if esk:
            # resume after the row whose assignment_id matches
            ids = [r["assignment_id"] for r in self.rows]
            start = ids.index(esk["assignment_id"]) + 1
        page = self.rows[start : start + limit]
        resp = {"Items": page}
        if start + limit < len(self.rows):
            last = page[-1]
            resp["LastEvaluatedKey"] = {
                "assignment_id": last["assignment_id"],
                "gsi_all": last["gsi_all"],
                # boto3's resource interface returns numbers as Decimal — the
                # cursor encoder must handle that (regression: a plain int here
                # hid a json.dumps(Decimal) TypeError that 500'd real paging).
                "created_at": Decimal(str(last["created_at"])),
            }
        return resp

    def get_item(self, Key):
        for r in self.rows:
            if r["assignment_id"] == Key["assignment_id"]:
                return {"Item": r}
        return {}


@pytest.fixture
def table(monkeypatch):
    t = FakeTable([_run(i) for i in range(10)])
    monkeypatch.setattr(queries, "_get_table", lambda: t)
    return t


def test_list_runs_newest_first_and_limit(table):
    out = queries.list_runs({"limit": "3"})
    assert [r["assignment_id"] for r in out["runs"]] == ["a-0", "a-1", "a-2"]
    assert out["next_token"] is not None


def test_list_runs_pagination_no_gap_no_dup(table):
    seen = []
    token = None
    for _ in range(10):
        params = {"limit": "3"}
        if token:
            params["next_token"] = token
        out = queries.list_runs(params)
        seen.extend(r["assignment_id"] for r in out["runs"])
        token = out["next_token"]
        if not token:
            break
    assert seen == [f"a-{i}" for i in range(10)]  # all rows, in order, no dupes


def test_list_runs_exhausted_has_no_token(table):
    out = queries.list_runs({"limit": "100"})
    assert len(out["runs"]) == 10
    assert out["next_token"] is None


def test_list_runs_filter(monkeypatch):
    rows = [
        _run(0, status="completed"),
        _run(1, status="dispatched"),
        _run(2, status="completed"),
    ]
    t = FakeTable(rows)
    monkeypatch.setattr(queries, "_get_table", lambda: t)
    out = queries.list_runs({"status": "completed", "limit": "10"})
    assert {r["assignment_id"] for r in out["runs"]} == {"a-0", "a-2"}


def test_list_runs_selective_filter_beyond_scan_window_is_reachable(monkeypatch):
    # Regression for the silent-truncation bug: with limit=1 the loop scans one
    # row per page, and its 20-page cap covers only the first 20 rows. The only
    # matching row sits at index 50 — far past that window. The first call must
    # NOT report "done" (empty page + no token); it must hand back a continuation
    # token so paging eventually reaches the match.
    rows = [_run(i, status="dispatched") for i in range(60)]
    rows[50] = _run(50, status="failed")
    t = FakeTable(rows)
    monkeypatch.setattr(queries, "_get_table", lambda: t)

    seen = []
    token = None
    for _ in range(200):  # generous bound; must terminate well before this
        params = {"status": "failed", "limit": "1"}
        if token:
            params["next_token"] = token
        out = queries.list_runs(params)
        seen.extend(r["assignment_id"] for r in out["runs"])
        token = out["next_token"]
        if not token:
            break
    assert seen == ["a-50"]  # the deep match is found, not silently dropped


def test_list_runs_page_fill_mid_dynamo_page_no_skip(monkeypatch):
    # When a single DynamoDB page holds more matches than `limit`, the page fills
    # mid-scan; the next page must resume right after the last RETURNED row, not
    # past the whole DynamoDB page (which would skip the unreturned matches).
    rows = [_run(i, status="failed") for i in range(10)]
    t = FakeTable(rows)
    monkeypatch.setattr(queries, "_get_table", lambda: t)

    seen = []
    token = None
    for _ in range(50):
        params = {"status": "failed", "limit": "3"}
        if token:
            params["next_token"] = token
        out = queries.list_runs(params)
        seen.extend(r["assignment_id"] for r in out["runs"])
        token = out["next_token"]
        if not token:
            break
    assert seen == [f"a-{i}" for i in range(10)]  # no skips, no dupes


def test_get_run(table):
    assert queries.get_run("a-4")["assignment_id"] == "a-4"
    assert queries.get_run("missing") is None


def test_trace_matches_dimension(monkeypatch):
    rows = [
        _run(0, trace_refs={"jira_key": "ABC-1"}),
        _run(1, trace_refs={"jira_key": "ABC-2"}),
        _run(2, trace_refs={"jira_key": "ABC-1"}),
    ]
    t = FakeTable(rows)
    monkeypatch.setattr(queries, "_get_table", lambda: t)
    out = queries.trace("jira_key", "ABC-1")
    assert {r["assignment_id"] for r in out["runs"]} == {"a-0", "a-2"}
    assert out["truncated"] is False


def test_stats_rollup(monkeypatch):
    rows = [
        _run(0, status="dispatched", agent_id="workitems", source="github"),
        _run(1, status="completed", agent_id="docwriter", source="asana"),
        _run(2, status="completed", agent_id="workitems", source="github"),
    ]
    t = FakeTable(rows)
    monkeypatch.setattr(queries, "_get_table", lambda: t)
    s = queries.stats()
    assert s["total"] == 3
    assert s["active"] == 1  # only 'dispatched'
    assert s["by_status"] == {"dispatched": 1, "completed": 2}
    assert s["by_agent"] == {"workitems": 2, "docwriter": 1}
    assert s["by_source"] == {"github": 2, "asana": 1}
