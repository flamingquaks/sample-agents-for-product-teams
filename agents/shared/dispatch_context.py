"""Shared dispatch-context builder.

Every agent injects a "## Current Dispatch" block into its system prompt so it
knows which task/issue/thread triggered it and where to reply. This logic used
to be hand-copied into each agent's invoke() as a 4-way if/elif on `source`,
which drifted across agents and shipped a real bug (a `project_item` branch
placed AFTER the generic `github` branch never executed, because the first
matching elif wins).

Centralizing it here gives one correct implementation for all agents:
- the `github` + `project_item` case is checked BEFORE the generic `github`
  case, by construction, so the ordering hazard can't recur;
- a board item may lack a resolvable issue number (the webhook delivers a
  content_node_id, not always an issue number) and the board can span repos, so
  those fields render conditionally;
- the block is deliberately injected into the SYSTEM prompt, not the user
  message — a "[Dispatch Context] ... [User Request]" wrapper in the user turn
  mirrors the canonical prompt-injection shape and trips Bedrock Guardrails'
  PROMPT_ATTACK filter.

Agents that don't use a given source simply never receive that source — the
builder handles all sources uniformly, and unknown/empty inputs yield "".
"""


def build_dispatch_context(source: str, source_context: dict | None) -> str:
    """Return the "## Current Dispatch" block for a trigger, or "" if none.

    Args:
        source: originating platform — "asana", "github", or "slack".
        source_context: the per-source context dict from the dispatch payload.

    Returns:
        A system-prompt-ready block (leading blank lines included), or "" when
        there's no context to render.
    """
    if not source_context:
        return ""

    if source == "asana":
        return _asana(source_context)
    if source == "github":
        # project_item MUST be checked before the generic github case — both
        # match a github source. This ordering is the whole reason the builder
        # is centralized; do not reorder.
        if source_context.get("trigger_type") == "project_item":
            return _github_project_item(source_context)
        return _github_issue(source_context)
    if source == "slack":
        return _slack(source_context)
    return ""


def _asana(sc: dict) -> str:
    return (
        "\n\n## Current Dispatch\n\n"
        "Source: asana\n"
        f"Task GID: {sc.get('task_gid', 'unknown')}\n"
        f"Task: {sc.get('task_name', 'unknown')}\n"
        f"Task Notes: {sc.get('task_notes', '')}\n"
        f"Project: {sc.get('project_name', 'unknown')} ({sc.get('project_gid', '')})\n"
        f"Reply to: Asana task {sc.get('task_gid', 'unknown')}\n"
    )


def _github_issue(sc: dict) -> str:
    """Generic GitHub issue/PR comment dispatch — one block for all agents.

    Consolidating four hand-written blocks onto one renderer intentionally
    NORMALIZES the prompt text across agents (it is not a byte-for-byte copy of
    any single agent's old block):

    - The number line is uniformly `Issue: #N` / `PR: #N` (workitems/researcher
      previously said `Issue Title:`/`Issue Body:`; the value the LLM reads is
      unchanged — only the field label).
    - `Labels:` / `State:` render whenever the payload carries them
      (`issue_labels` / `issue_state`). The GitHub Actions dispatch payload
      always includes these, so every github-triggered agent now sees them; the
      PM-webhook payloads omit them. This is additive context (more signal for
      the LLM), never less — a deliberate harmonization, not a regression.
    - PR-reviewing agents may send pr_title/pr_body and `comments` instead of the
      issue_* keys; we prefer the PR-specific value, then the issue value, so
      docwriter's and adr's PR payloads keep every field they had.
    """
    repo = sc.get("repo", "unknown")
    is_pr = sc.get("is_pr") == "true" or bool(sc.get("pr_number"))
    target_type = "PR" if is_pr else "issue"
    label = "PR" if is_pr else "Issue"
    number = sc.get("issue_number") or sc.get("pr_number", "unknown")
    title = sc.get("pr_title") or sc.get("issue_title", "unknown")
    body = sc.get("pr_body") or sc.get("issue_body", "")
    comments = sc.get("issue_comments") or sc.get("comments", "(not loaded)")

    block = (
        "\n\n## Current Dispatch\n\n"
        "Source: github\n"
        f"Repository: {repo}\n"
        f"{label}: #{number}\n"
        f"Title: {title}\n"
        f"Body:\n{body}\n"
    )
    if "issue_labels" in sc:
        block += f"Labels: {sc.get('issue_labels', '')}\n"
    if "issue_state" in sc:
        block += f"State: {sc.get('issue_state', '')}\n"
    block += (
        f"Comments:\n{comments}\n"
        f"Reply to: GitHub {target_type} #{number} on {repo}\n"
    )
    return block


def _github_project_item(sc: dict) -> str:
    """Projects V2 board-item event (from the GitHub PM webhook).

    A board item may not carry a resolvable issue number, and the board can span
    repos, so the linked-issue line and reply target render conditionally.
    """
    issue_number = sc.get("issue_number", "")
    content_node_id = sc.get("content_node_id", "")
    repo = sc.get("repo", "")
    project_number = sc.get("project_number", "unknown")
    item_id = sc.get("item_id", "unknown")

    if issue_number:
        linked = f"Linked issue: #{issue_number}\n"
    else:
        linked = (
            f"Linked content node: {content_node_id or 'unknown'} "
            "(resolve the issue via the board item if you need to comment)\n"
        )

    if issue_number and repo:
        reply_target = (
            f"Reply to: GitHub issue #{issue_number} on {repo}, and update "
            f"board item {item_id} on project #{project_number}\n"
        )
    else:
        reply_target = (
            f"Reply to: post a project status update on project #{project_number} "
            "(and comment on the linked issue if you can resolve it from the board item)\n"
        )

    return (
        "\n\n## Current Dispatch\n\n"
        "Source: github (Projects V2 board)\n"
        f"Repository: {repo or '(board spans repos / not specified)'}\n"
        f"Project: #{project_number}\n"
        f"Board item: {item_id}\n"
        f"{linked}"
        f"Status change: {sc.get('status_change', 'unknown')}\n"
        f"{reply_target}"
    )


def _slack(sc: dict) -> str:
    thread_ts = sc.get("thread_ts")
    reply_thread = f" thread {thread_ts}" if thread_ts else ""
    return (
        "\n\n## Current Dispatch\n\n"
        "Source: slack\n"
        f"Channel: {sc.get('channel_id', 'unknown')}\n"
        f"Thread: {sc.get('thread_ts', 'none')}\n"
        f"Team: {sc.get('team_id', 'unknown')}\n"
        f"Reply to: Slack channel {sc.get('channel_id', 'unknown')}{reply_thread}\n"
    )
