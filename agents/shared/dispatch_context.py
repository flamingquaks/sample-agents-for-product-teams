"""Build the per-source "Current Dispatch" block appended to agent system prompts.

Each agent gets a small per-invocation header describing where the request
came from and where to reply. Centralizing the formatting here keeps the
shape consistent across agents and avoids the prompt-injection-shaped
"[Dispatch Context] ... [User Request] ..." wrappers that trip the
Bedrock Guardrails PROMPT_ATTACK filter.

Callers pass the `source` and `source_context` from the dispatch payload.
Unknown sources return an empty string — the agent runs without a context
header rather than crashing on a new platform.
"""

from typing import Mapping


_HEADER = "\n\n## Current Dispatch\n\n"


def _asana(ctx: Mapping) -> str:
    return (
        _HEADER
        + "Source: asana\n"
        f"Task GID: {ctx.get('task_gid', 'unknown')}\n"
        f"Task: {ctx.get('task_name', 'unknown')}\n"
        f"Task Notes: {ctx.get('task_notes', '')}\n"
        f"Project: {ctx.get('project_name', 'unknown')} ({ctx.get('project_gid', '')})\n"
        f"Reply to: Asana task {ctx.get('task_gid', 'unknown')}\n"
    )


def _github(ctx: Mapping) -> str:
    is_pr = ctx.get("is_pr") == "true" or bool(ctx.get("pr_number"))
    number = ctx.get("issue_number") or ctx.get("pr_number", "unknown")
    target_type = "PR" if is_pr else "issue"

    lines = [
        _HEADER,
        "Source: github\n",
        f"Repository: {ctx.get('repo', 'unknown')}\n",
        f"{target_type}: #{number}\n",
        f"Title: {ctx.get('issue_title', 'unknown')}\n",
        f"Body:\n{ctx.get('issue_body', '')}\n",
    ]
    if ctx.get("issue_labels"):
        lines.append(f"Labels: {ctx['issue_labels']}\n")
    if ctx.get("issue_state"):
        lines.append(f"State: {ctx['issue_state']}\n")
    lines.append(f"Comments:\n{ctx.get('issue_comments', '(not loaded)')}\n")
    lines.append(
        f"Reply to: GitHub {target_type} #{number} "
        f"on {ctx.get('repo', 'unknown')}\n"
    )
    return "".join(lines)


def _slack(ctx: Mapping) -> str:
    thread_ts = ctx.get("thread_ts") or ""
    channel_id = ctx.get("channel_id", "unknown")
    if thread_ts:
        reply_line = (
            f"Reply to: Slack channel {channel_id} in thread {thread_ts}.\n"
            f"Use slack_post_thread with channel='{channel_id}' and thread_ts='{thread_ts}' to post your results.\n"
        )
    else:
        reply_line = (
            f"Reply to: Slack channel {channel_id} (top-level message).\n"
            f"Use slack_post_message with channel='{channel_id}' to post your results.\n"
        )
    return (
        _HEADER
        + "Source: slack\n"
        f"Channel: {channel_id}\n"
        f"Thread: {thread_ts or '(top-level)'}\n"
        f"User: {ctx.get('user_id', 'unknown')}\n"
        + reply_line
    )


def _discord(ctx: Mapping) -> str:
    application_id = ctx.get("application_id", "unknown")
    interaction_token = ctx.get("interaction_token", "")
    channel_id = ctx.get("channel_id", "unknown")
    # The interaction_token is a 15-minute write credential. It is intentionally
    # NOT included in the prompt — emitting it here would echo it into OTel
    # traces and into the LLM's context window. The agent receives it as a
    # tool argument bound at invoke time, not via the system prompt.
    return (
        _HEADER
        + "Source: discord\n"
        f"Guild: {ctx.get('guild_id', '(dm)')}\n"
        f"Channel: {channel_id}\n"
        f"User: {ctx.get('user_id', 'unknown')}\n"
        f"Application ID: {application_id}\n"
        f"Reply to: Discord channel {channel_id}.\n"
        f"Use discord_post_followup with the interaction_token bound by the runtime "
        f"for the first reply, or discord_post_message to channel '{channel_id}' for additional posts.\n"
    )


_BUILDERS = {
    "asana": _asana,
    "github": _github,
    "slack": _slack,
    "discord": _discord,
}


def build_dispatch_context_block(source: str, source_context: Mapping | None) -> str:
    """Return the per-source dispatch context block, or "" if the source is
    unknown or the context is empty."""
    if not source_context or source not in _BUILDERS:
        return ""
    return _BUILDERS[source](source_context)
