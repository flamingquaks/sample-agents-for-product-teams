"""Run-record enrichment: trace refs, participants, and fleet-index key.

Pure, dependency-free helpers shared by the Dispatch Router (which stamps
these onto every assignment at dispatch time) and, later, the dashboard query
API (which reads them). Kept side-effect free so it is trivially unit-testable
without AWS.

Three things are derived here, all from data the router *already* receives —
nothing new is collected from external systems:

1. ``trace_refs`` — a flat map of the cross-cutting identifiers a body of work
   can be traced by (repo, branch, pr_number, issue_number, jira_key,
   asana_task_gid, project_gid, …). The dashboard renders whatever keys are
   present, so **adding a new integration is a matter of emitting a new key
   here (or from an agent via ``update_trace_refs``)** — no schema migration,
   no dashboard change. Only non-empty values are included so the dashboard's
   data-driven dimension list stays clean.

2. ``participants`` — the humans involved in the work, distilled from the
   trigger event: the requester plus assignees/commenters already present in
   ``source_context``. Structured as ``[{id, kind, source}]``.

3. The fleet-index partition key (``ALL_RUNS_PK`` under ``gsi_all``) that lets
   the dashboard page every run newest-first via a single GSI query. See
   ``AllRunsIndex`` in ``infra/foundation/template.yaml``.
"""

import re

# Constant partition-key value for the "all runs, newest-first" GSI. Every
# assignment carries ``gsi_all = ALL_RUNS_PK`` so the dashboard can Query the
# whole fleet time-ordered (SK = created_at) without a scatter-gather across
# status/source partitions. Single-partition write concentration is acceptable
# at sample volume; a sharded key (``run#<n>``) is the documented scale
# follow-up.
ALL_RUNS_PK = "run"

# Jira issue keys look like ``ABC-123`` / ``PROJ2-4567``: an uppercase project
# key, a hyphen, then digits. Used to opportunistically surface a Jira reference
# typed into the triggering comment / issue body even though Jira is not a
# first-class dispatch source today. This is a best-effort convenience signal,
# not an authority: an uppercase token like ``UTF-8`` also matches the shape, so
# the dashboard treats jira_key as a hint (a trace chip the operator can act on),
# never as a trusted key. Refine with a project-key allowlist if false positives
# become noisy.
_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def _clean(value) -> str | None:
    """Coerce to a trimmed non-empty string, or None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_jira_key(*texts: str) -> str | None:
    """First Jira-style key found across the given texts, or None."""
    for text in texts:
        if not text:
            continue
        match = _JIRA_KEY_RE.search(text)
        if match:
            return match.group(1)
    return None


def slack_thread_key(workspace: str, channel: str, thread_ts: str) -> str | None:
    """The canonical composite identity for a Slack conversation thread.

    Returns workspace#channel#thread_ts when all three parts are present,
    None otherwise. Used by enrichment (trace ref), notify (ops-thread unit),
    and the router (runtime session hash input) — ONE derivation point so a
    normalization change can't split what should be one conversation."""
    ws = _clean(workspace)
    ch = _clean(channel)
    ts = _clean(thread_ts)
    if not (ws and ch and ts):
        return None
    return f"{ws}#{ch}#{ts}"


def derive_trace_refs(source: str, source_context: dict, instruction: str = "") -> dict:
    """Derive the structured trace-reference map for a dispatch.

    Extensible by design: to trace by a new dimension, emit a new key here
    (or have the agent add it via ``update_trace_refs``). Returns only the keys
    whose values are present, so downstream ``trace_refs`` never carries empty
    slots.

    :param source: dispatch source (``github`` | ``asana`` | ``slack``).
    :param source_context: the router's ``source_context`` dict.
    :param instruction: the triggering instruction text (scanned for a Jira key).
    :returns: a flat ``{ref_name: value}`` map (only non-empty entries).
    """
    ctx = source_context or {}
    refs: dict[str, str] = {}

    if source == "github":
        repo = _clean(ctx.get("repo"))
        issue_number = _clean(ctx.get("issue_number"))
        is_pr = str(ctx.get("is_pr", "")).lower() == "true"
        if repo:
            refs["repo"] = repo
        if issue_number:
            # For a PR the GitHub dispatch payload carries the PR number in
            # ``issue_number`` (PRs are issues in the REST API). Surface it under
            # the semantically correct key so the dashboard can distinguish them.
            refs["pr_number" if is_pr else "issue_number"] = issue_number
        jira_key = _extract_jira_key(
            instruction,
            _clean(ctx.get("issue_title")) or "",
            _clean(ctx.get("issue_body")) or "",
        )
        if jira_key:
            refs["jira_key"] = jira_key

    elif source == "asana":
        task_gid = _clean(ctx.get("task_gid"))
        project_gid = _clean(ctx.get("project_gid"))
        project_name = _clean(ctx.get("project_name"))
        if task_gid:
            refs["asana_task_gid"] = task_gid
        if project_gid:
            refs["project_gid"] = project_gid
        if project_name:
            refs["project_name"] = project_name
        jira_key = _extract_jira_key(
            instruction,
            _clean(ctx.get("task_name")) or "",
            _clean(ctx.get("task_notes")) or "",
        )
        if jira_key:
            refs["jira_key"] = jira_key

    elif source == "slack":
        workspace = _clean(ctx.get("workspace"))
        channel = _clean(ctx.get("channel_id"))
        thread_ts = _clean(ctx.get("thread_ts"))
        if workspace:
            refs["slack_workspace"] = workspace
        if channel:
            refs["slack_channel"] = channel
        if thread_ts:
            refs["slack_thread_ts"] = thread_ts
        # Composite thread identity — the END-TO-END trace key for a Slack
        # conversation. thread_ts alone is not unique across channels; this
        # key is. Every dispatch born in the thread (the original, D8 linked
        # follow-ups, distinct agents serving the same thread) carries the
        # same value, so the dashboard trace view shows the whole
        # conversation as one timeline. (A RESUME re-enters the original
        # assignment, so it is inherently part of that run's row.)
        thread_key = slack_thread_key(workspace or "", channel or "", thread_ts or "")
        if thread_key:
            refs["slack_thread"] = thread_key
        jira_key = _extract_jira_key(instruction)
        if jira_key:
            refs["jira_key"] = jira_key

    # Unknown sources contribute no refs today; a future source adds its own
    # branch above. (This is the extension point referenced in the module docstring.)
    return refs


# Matches the GitHub comment *header* emitted by agent-dispatch.yml, which
# formats each comment as ``[<login> at <iso-timestamp>]:\n<body>``. We anchor
# on the start of a line (re.MULTILINE) AND require the trailing ``]:`` so we
# only capture real headers — not ``[label](link)``-style markdown that appears
# inside a comment *body* (matching over the whole blob would inject spurious
# participants from user-controlled comment text). The login charset allows the
# ``[bot]`` suffix so GitHub bot accounts (e.g. ``github-actions[bot]``) are
# captured rather than silently dropped. Logins cannot contain spaces, so the
# `` at `` separator is unambiguous.
_GH_COMMENT_AUTHOR_RE = re.compile(
    r"^\[([\w-]+(?:\[bot\])?) at [^\]]*\]:", re.MULTILINE
)


def _split_csv(value) -> list[str]:
    """Split a comma-joined string field into trimmed non-empty tokens."""
    text = _clean(value)
    if not text:
        return []
    return [tok.strip() for tok in text.split(",") if tok.strip()]


def derive_participants(
    source: str, requester: str, source_context: dict
) -> list[dict]:
    """Distill the humans involved in the work from the trigger event.

    Structured as ``[{id, kind, source}]`` where ``kind`` is one of
    ``requester`` | ``assignee`` | ``commenter``. De-duplicated by ``id`` with
    the strongest role kept (requester > assignee > commenter). Derived purely
    from ``source_context`` the router already has — no external lookups.

    :param source: dispatch source.
    :param requester: the resolved trigger actor (already on the assignment).
    :param source_context: the router's ``source_context`` dict.
    :returns: an ordered list of participant records.
    """
    ctx = source_context or {}
    # Rank so a person who is both requester and commenter is recorded as the
    # stronger role only.
    rank = {"requester": 0, "assignee": 1, "commenter": 2}
    best: dict[str, str] = {}

    def add(pid: str, kind: str) -> None:
        pid = _clean(pid)
        if not pid or pid in {"unknown", "asana_rule"}:
            return
        if pid not in best or rank[kind] < rank[best[pid]]:
            best[pid] = kind

    add(requester, "requester")

    if source == "github":
        for login in _split_csv(ctx.get("issue_assignees")):
            add(login, "assignee")
        comments = _clean(ctx.get("issue_comments")) or ""
        for match in _GH_COMMENT_AUTHOR_RE.finditer(comments):
            add(match.group(1), "commenter")
    elif source == "asana":
        add(ctx.get("task_assignee_gid"), "assignee")

    # Preserve insertion order (requester first) while emitting the resolved kind.
    return [{"id": pid, "kind": kind, "source": source} for pid, kind in best.items()]
