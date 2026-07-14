"""Unit tests for agents.shared.assignment enrichment at close.

Covers the dashboard-facing enrichment added to the assignment lifecycle:
duration computation, token/cost capture, trace-ref merge, and the tolerant
token-usage extractor. The DynamoDB table is replaced with a fake that records
update_item calls so we assert on the emitted UpdateExpression/values without
AWS.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shared.assignment as asg  # noqa: E402


class FakeTable:
    """Records update_item/get_item calls; returns a canned created_at."""

    def __init__(self, created_at=1000):
        self.updates = []
        self._created_at = created_at
        # Mimic boto3's client.exceptions.ConditionalCheckFailedException handle.
        self.meta = SimpleNamespace(
            client=SimpleNamespace(
                exceptions=SimpleNamespace(
                    ConditionalCheckFailedException=type(
                        "ConditionalCheckFailedException", (Exception,), {}
                    )
                )
            )
        )

    def get_item(self, **kwargs):
        if self._created_at is None:
            return {}
        return {"Item": {"created_at": self._created_at}}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


@pytest.fixture
def fake_table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(asg, "_get_table", lambda: table)
    return table


# --- extract_token_usage (pure, tolerant) ------------------------------------


def test_extract_token_usage_total():
    result = SimpleNamespace(
        metrics=SimpleNamespace(accumulated_usage={"totalTokens": 1500})
    )
    assert asg.extract_token_usage(result) == 1500


def test_extract_token_usage_sums_in_out_when_no_total():
    result = SimpleNamespace(
        metrics=SimpleNamespace(
            accumulated_usage={"inputTokens": 300, "outputTokens": 200}
        )
    )
    assert asg.extract_token_usage(result) == 500


def test_extract_token_usage_missing_returns_none():
    assert asg.extract_token_usage(SimpleNamespace()) is None
    assert asg.extract_token_usage(SimpleNamespace(metrics=None)) is None
    assert asg.extract_token_usage("not a result") is None


def test_extract_token_usage_zero_is_none():
    result = SimpleNamespace(
        metrics=SimpleNamespace(accumulated_usage={"totalTokens": 0})
    )
    assert asg.extract_token_usage(result) is None


# --- extract_trace_refs_from_result ------------------------------------------


def test_extract_trace_refs_finds_pr_url():
    text = "All 3 issues done. PRs: https://github.com/acme/web/pull/45 and more."
    assert asg.extract_trace_refs_from_result(text) == {
        "pr_url": "https://github.com/acme/web/pull/45",
        "pr_number": "45",
    }


def test_extract_trace_refs_none_when_no_pr():
    assert asg.extract_trace_refs_from_result("Posted a status comment; no PR.") == {}


def test_extract_trace_refs_ignores_issue_url():
    # An issue URL is not a PR — must not be captured as pr_url.
    text = "See https://github.com/acme/web/issues/12 for context."
    assert asg.extract_trace_refs_from_result(text) == {}


def test_extract_trace_refs_tolerates_non_string():
    # str() of an arbitrary result object must not raise.
    assert asg.extract_trace_refs_from_result(object()) == {}


# --- extract_trace_refs_from_messages (structured tool transcript) -----------


def test_messages_extract_branch_and_pr_from_tool_calls():
    # Mirrors a Strands conversation where the agent opened a PR via the GitHub
    # MCP server: a toolUse (create_pull_request) with the head branch, then a
    # toolResult carrying the PR html_url.
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "toolUse": {
                        "name": "github_create_pull_request",
                        "toolUseId": "t1",
                        "input": {"head": "docs/17-readme", "base": "main", "title": "docs"},
                    }
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "t1",
                        "content": [
                            {"text": '{"html_url": "https://github.com/acme/web/pull/9"}'}
                        ],
                    }
                }
            ],
        },
    ]
    assert asg.extract_trace_refs_from_messages(messages) == {
        "branch": "docs/17-readme",
        "pr_url": "https://github.com/acme/web/pull/9",
        "pr_number": "9",
    }


def test_messages_extract_none_when_no_pr_tool():
    messages = [
        {"role": "assistant", "content": [{"text": "just a comment, no PR"}]},
        {
            "role": "assistant",
            "content": [{"toolUse": {"name": "github_add_comment", "input": {"body": "hi"}}}],
        },
    ]
    assert asg.extract_trace_refs_from_messages(messages) == {}


def test_messages_extract_tolerates_junk():
    # Odd/empty shapes must not raise.
    assert asg.extract_trace_refs_from_messages(None) == {}
    assert asg.extract_trace_refs_from_messages([{}, {"content": None}, "nope"]) == {}


# --- complete_assignment -----------------------------------------------------


def _last_update(table):
    return table.updates[-1]


def test_complete_sets_status_and_duration(fake_table):
    asg.complete_assignment("a-1", result_summary="done")
    upd = _last_update(fake_table)
    vals = upd["ExpressionAttributeValues"]
    assert vals[":s"] == "completed"
    assert ":dur" in vals  # created_at=1000 was read → duration computed
    assert vals[":dur"] >= 0
    assert vals[":rs"] == "done"


def test_complete_writes_token_and_cost(fake_table):
    asg.complete_assignment("a-1", token_usage=2000)
    vals = _last_update(fake_table)["ExpressionAttributeValues"]
    assert vals[":token_usage"] == 2000
    # 2000 tokens * $0.03/1k = $0.06
    assert float(vals[":cost_estimate_usd"]) == pytest.approx(0.06)


def test_complete_without_usage_omits_cost(fake_table):
    asg.complete_assignment("a-1")
    vals = _last_update(fake_table)["ExpressionAttributeValues"]
    assert ":token_usage" not in vals
    assert ":cost_estimate_usd" not in vals


def test_duration_omitted_when_created_at_missing(monkeypatch):
    table = FakeTable(created_at=None)
    monkeypatch.setattr(asg, "_get_table", lambda: table)
    asg.complete_assignment("a-1")
    vals = _last_update(table)["ExpressionAttributeValues"]
    assert ":dur" not in vals


def test_sentinel_assignment_id_is_noop(fake_table):
    asg.complete_assignment("default")
    asg.complete_assignment("")
    assert fake_table.updates == []


# --- fail_assignment ---------------------------------------------------------


def test_fail_sets_status_duration_and_usage(fake_table):
    asg.fail_assignment("a-1", error="boom", token_usage=1000)
    vals = _last_update(fake_table)["ExpressionAttributeValues"]
    assert vals[":s"] == "failed"
    assert vals[":rs"] == "boom"
    assert ":dur" in vals
    assert vals[":token_usage"] == 1000


# --- update_trace_refs -------------------------------------------------------


def test_update_trace_refs_merges_keys(fake_table):
    asg.update_trace_refs("a-1", branch="feat/1", pr_url="https://x/pull/9", empty="")
    # Two update_item calls: (1) ensure-map conditional init, (2) set keys.
    assert len(fake_table.updates) == 2
    set_update = fake_table.updates[-1]
    names = set_update["ExpressionAttributeNames"]
    vals = set_update["ExpressionAttributeValues"]
    # empty value dropped; branch + pr_url kept
    assert vals[":branch"] == "feat/1"
    assert vals[":pr_url"] == "https://x/pull/9"
    assert ":empty" not in vals
    assert "#tr" in names


def test_update_trace_refs_noop_when_all_empty(fake_table):
    asg.update_trace_refs("a-1", branch="", pr_url=None)
    assert fake_table.updates == []


def test_update_trace_refs_tolerates_existing_map(fake_table):
    # Simulate the conditional init failing because the map already exists.
    exc = fake_table.meta.client.exceptions.ConditionalCheckFailedException

    calls = {"n": 0}
    orig = fake_table.update_item

    def maybe_raise(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc()
        return orig(**kwargs)

    fake_table.update_item = maybe_raise
    asg.update_trace_refs("a-1", branch="feat/2")
    # The set call still ran after the swallowed conditional failure.
    assert fake_table.updates[-1]["ExpressionAttributeValues"][":branch"] == "feat/2"


def test_update_trace_refs_missing_item_is_noop(fake_table):
    # Both writes fail the attribute_exists(assignment_id) guard when the id
    # doesn't exist → clean no-op (no phantom stub row created, no exception).
    exc = fake_table.meta.client.exceptions.ConditionalCheckFailedException

    def always_conditional(**kwargs):
        raise exc()

    fake_table.update_item = always_conditional
    # Must not raise.
    asg.update_trace_refs("ghost-id", branch="feat/x")


def test_update_trace_refs_swallows_client_error(fake_table):
    # A ValidationException (e.g. legacy non-map trace_refs) on the keyed write
    # must not propagate — trace enrichment is best-effort.
    from botocore.exceptions import ClientError

    calls = {"n": 0}

    def raise_on_second(**kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": "bad path"}},
                "UpdateItem",
            )
        # first (ensure-map) write succeeds
        fake_table.updates.append(kwargs)

    fake_table.update_item = raise_on_second
    # Must not raise.
    asg.update_trace_refs("a-1", branch="feat/z")
