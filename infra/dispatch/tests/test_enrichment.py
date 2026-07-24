"""Tests for run-record enrichment (trace_refs, participants, fleet key).

These are pure functions with no AWS dependency — they derive structured,
traceable dimensions from the dispatch event the router already receives.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import enrichment  # noqa: E402


# --- trace_refs: GitHub ------------------------------------------------------


def test_github_issue_refs():
    refs = enrichment.derive_trace_refs(
        "github",
        {"repo": "org/app", "issue_number": "42", "is_pr": "false"},
    )
    assert refs == {"repo": "org/app", "issue_number": "42"}


def test_github_pr_uses_pr_number_key():
    # PRs arrive with the number in issue_number + is_pr=true; it must surface
    # under pr_number so the dashboard can distinguish PR work from issue work.
    refs = enrichment.derive_trace_refs(
        "github",
        {"repo": "org/app", "issue_number": "7", "is_pr": "true"},
    )
    assert refs["pr_number"] == "7"
    assert "issue_number" not in refs


def test_github_jira_key_extracted_from_instruction():
    refs = enrichment.derive_trace_refs(
        "github",
        {"repo": "org/app", "issue_number": "1", "is_pr": "false"},
        instruction="please sync this with PROJ-1234 in Jira",
    )
    assert refs["jira_key"] == "PROJ-1234"


def test_jira_key_not_matched_from_lowercase_token():
    # A lowercase token (``utf-8``) is not Jira-key-shaped — the project key must
    # be uppercase — so it is correctly ignored. (An uppercase ``UTF-8`` would
    # match the shape; jira_key is documented as a best-effort hint, not an
    # authority, precisely because of that.)
    refs = enrichment.derive_trace_refs(
        "github",
        {"repo": "org/app", "issue_number": "1", "is_pr": "false"},
        instruction="update the utf-8 handling",
    )
    assert "jira_key" not in refs


# --- trace_refs: Asana -------------------------------------------------------


def test_asana_refs():
    refs = enrichment.derive_trace_refs(
        "asana",
        {"task_gid": "111", "project_gid": "222", "project_name": "Roadmap"},
    )
    assert refs == {
        "asana_task_gid": "111",
        "project_gid": "222",
        "project_name": "Roadmap",
    }


def test_empty_values_are_omitted():
    refs = enrichment.derive_trace_refs(
        "github",
        {"repo": "", "issue_number": None, "is_pr": "false"},
    )
    assert refs == {}


def test_unknown_source_yields_no_refs():
    assert enrichment.derive_trace_refs("teams", {"channel_id": "C1"}) == {}


def test_slack_refs():
    refs = enrichment.derive_trace_refs(
        "slack",
        {"workspace": "T0ACME", "channel_id": "C0ENG", "thread_ts": "111.2"},
        "please look at PROJ-42",
    )
    assert refs == {
        "slack_workspace": "T0ACME",
        "slack_channel": "C0ENG",
        "slack_thread_ts": "111.2",
        "slack_thread": "T0ACME#C0ENG#111.2",
        "jira_key": "PROJ-42",
    }


def test_slack_refs_omit_empty():
    refs = enrichment.derive_trace_refs("slack", {"channel_id": "C0ENG"})
    assert refs == {"slack_channel": "C0ENG"}


def test_slack_thread_composite_requires_all_parts():
    # Missing thread_ts → no composite key (parts still emitted individually).
    refs = enrichment.derive_trace_refs(
        "slack", {"workspace": "T0ACME", "channel_id": "C0ENG"}
    )
    assert "slack_thread" not in refs
    assert refs["slack_workspace"] == "T0ACME"


def test_slack_thread_composite_identical_for_followups():
    """A D8 follow-up in the same thread (new assignment, same context) must
    derive the SAME slack_thread ref as the original — that equality is what
    groups a whole conversation into one dashboard trace."""
    ctx = {"workspace": "T0ACME", "channel_id": "C0ENG", "thread_ts": "111.2"}
    first = enrichment.derive_trace_refs("slack", ctx, "do the thing")
    followup = enrichment.derive_trace_refs("slack", ctx, "now also add tests")
    assert first["slack_thread"] == followup["slack_thread"]


def test_slack_requester_is_a_participant():
    parts = enrichment.derive_participants(
        "slack", "slack:T0ACME:U0ALICE", {"channel_id": "C0ENG"}
    )
    assert parts == [{"id": "slack:T0ACME:U0ALICE", "kind": "requester", "source": "slack"}]


# --- participants ------------------------------------------------------------


def test_github_participants_dedup_and_rank():
    parts = enrichment.derive_participants(
        "github",
        requester="alice",
        source_context={
            "issue_assignees": "bob, carol",
            "issue_comments": "[alice at 2026-01-01T00:00:00Z]:\nhi\n"
            "[dave at 2026-01-02T00:00:00Z]:\n@workitems go\n",
        },
    )
    by_id = {p["id"]: p["kind"] for p in parts}
    # alice is both requester and commenter → strongest role (requester) kept.
    assert by_id["alice"] == "requester"
    assert by_id["bob"] == "assignee"
    assert by_id["carol"] == "assignee"
    assert by_id["dave"] == "commenter"
    # requester first
    assert parts[0]["id"] == "alice"


def test_github_bot_commenter_is_captured():
    # GitHub bot logins carry a "[bot]" suffix; they must be captured, not
    # dropped by the header regex.
    parts = enrichment.derive_participants(
        "github",
        requester="alice",
        source_context={
            "issue_comments": "[github-actions[bot] at 2026-01-01T00:00:00Z]:\nbuild ok\n",
        },
    )
    by_id = {p["id"]: p["kind"] for p in parts}
    assert by_id["github-actions[bot]"] == "commenter"


def test_markdown_in_comment_body_is_not_a_participant():
    # A "[label at url]" token inside a comment BODY (user-controlled) must not
    # be mis-captured as a commenter — only real line-start headers count.
    parts = enrichment.derive_participants(
        "github",
        requester="alice",
        source_context={
            "issue_comments": "[bob at 2026-01-01T00:00:00Z]:\n"
            "see [docs at http://example.com] and [ref at x]\n",
        },
    )
    ids = {p["id"] for p in parts}
    assert ids == {"alice", "bob"}
    assert "docs" not in ids
    assert "ref" not in ids


def test_participants_skip_sentinels():
    parts = enrichment.derive_participants(
        "asana",
        requester="asana_rule",
        source_context={"task_assignee_gid": "999"},
    )
    ids = {p["id"] for p in parts}
    assert "asana_rule" not in ids
    assert "999" in ids


def test_unresolved_requester_not_recorded():
    parts = enrichment.derive_participants(
        "github", requester="unknown", source_context={}
    )
    assert parts == []


# --- fleet key ---------------------------------------------------------------


def test_all_runs_pk_is_constant():
    assert enrichment.ALL_RUNS_PK == "run"
