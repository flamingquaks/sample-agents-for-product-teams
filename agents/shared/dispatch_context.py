"""Slack dispatch-context rendering, shared by every agent.

GitHub and Asana dispatches carry rich, source-specific context each agent
renders itself (issue bodies, task notes). Slack dispatches were rendering
NOTHING — the agent got the instruction text but no "Current Dispatch" block,
so it didn't know which channel/thread it was serving or, critically, which
CODEBASES it was authorized to work with. This module is the bridge: it turns
the Slack ``source_context`` (workspace/channel/thread + the channel's approved
repo scope, attached by the webhook) into the system-prompt block that tells
the agent when and where to reach for a repo.
"""


def slack_dispatch_block(source_context: dict | None) -> str:
    """The ``## Current Dispatch`` system-prompt block for a Slack dispatch.

    Renders the origin (workspace/channel/thread) and the repo scope. The repo
    guidance is explicit about the gateway contract: only the listed repos are
    reachable (co-repo enforcement fails anything else closed), and a repo-less
    dispatch means GitHub tools will refuse — answer from knowledge instead of
    burning tool calls. Returns "" for a non-Slack/empty context."""
    if not source_context:
        return ""
    ctx = source_context
    repos = [str(r) for r in (ctx.get("repos") or []) if r]
    origin = str(ctx.get("repo", "") or "")
    if origin and origin not in repos:
        repos.insert(0, origin)
    if repos:
        primary = origin or repos[0]
        repo_lines = (
            f"Repositories you may work with (approved for the requesting channel):\n"
            + "\n".join(f"- {r}" + (" (primary)" if r == primary else "") for r in repos)
            + "\n\nWhen the request involves code, files, issues, or PRs, use your "
            "GitHub tools against these repositories — start with the primary "
            "unless the request names another listed repo. Repositories NOT "
            "listed are not reachable: the gateway refuses calls to them, so "
            "don't attempt or promise work there.\n"
        )
    else:
        repo_lines = (
            "No repositories are approved for this channel — GitHub tool calls "
            "will be refused. Answer from the conversation and your knowledge; "
            "if the request needs repository access, say the channel must be "
            "onboarded for a repo first (an admin approves that in the fleet "
            "dashboard).\n"
        )
    return (
        "\n\n## Current Dispatch\n\n"
        f"Source: slack\n"
        f"Workspace: {ctx.get('workspace', 'unknown')}\n"
        f"Channel: {ctx.get('channel_id', 'unknown')}\n"
        f"Requester: {ctx.get('sender_name') or 'unknown'}\n"
        f"Reply to: the originating Slack thread (your final response is "
        f"delivered there automatically — do not call a tool to post it)\n\n"
        f"{repo_lines}"
    )
