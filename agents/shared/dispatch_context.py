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


def _atlassian_repo_lines(ctx: dict) -> str:
    """Shared repo-scope guidance for Jira/Confluence dispatches — the linked
    repos of the origin container (attached by the receiver's context). Mirrors
    the Slack block: only listed repos are reachable (co-repo enforcement)."""
    repos = [str(r) for r in (ctx.get("repos") or []) if r]
    origin = str(ctx.get("repo", "") or "")
    if origin and origin not in repos:
        repos.insert(0, origin)
    if not repos:
        return (
            "No repositories are linked to this project/space — GitHub tool calls "
            "will be refused. Work from the Atlassian content and your knowledge.\n"
        )
    primary = origin or repos[0]
    return (
        "Repositories linked to this project/space (work against these; the "
        "first is your primary):\n"
        + "\n".join(f"- {r}" + (" (primary)" if r == primary else "") for r in repos)
        + "\n\nRepositories NOT listed are not reachable — the gateway refuses "
        "calls to them.\n"
    )


def jira_dispatch_block(source_context: dict | None) -> str:
    """The ``## Current Dispatch`` block for a Jira dispatch (§B1.3). Tells the
    agent the site/project/issue/status + comment history + the project's repo
    scope, and to answer via the JiraTarget add_comment tool (the gateway-only
    reply chokepoint)."""
    if not source_context:
        return ""
    ctx = source_context
    history = ctx.get("comment_history")
    block = (
        "\n\n## Current Dispatch\n\n"
        "Source: jira\n"
        f"Site: {ctx.get('workspace', 'unknown')}\n"
        f"Project: {ctx.get('project_key', 'unknown')}\n"
        f"Issue: {ctx.get('issue_key', 'unknown')} — {ctx.get('issue_summary', '')}\n"
        f"Status: {ctx.get('issue_status', 'unknown')}\n"
        "Reply to: post your answer with the JiraTarget add_comment tool on this "
        "issue (that is the ONLY reply channel — the gateway enforces it). NEVER "
        "transition an issue to Done/Closed without explicit human approval on the "
        "issue.\n\n"
        f"{_atlassian_repo_lines(ctx)}"
    )
    if history:
        block += f"\n### Comment history\n{history}\n"
    return block


def confluence_dispatch_block(source_context: dict | None) -> str:
    """The ``## Current Dispatch`` block for a Confluence dispatch (§C1.3). Tells
    the agent the site/space/page/version, the inline selection (if any), and the
    space's write mode — so it knows up front whether to apply or propose."""
    if not source_context:
        return ""
    ctx = source_context
    write_mode = ctx.get("write_mode", "propose")
    block = (
        "\n\n## Current Dispatch\n\n"
        "Source: confluence\n"
        f"Site: {ctx.get('workspace', 'unknown')}\n"
        f"Space: {ctx.get('space_key', 'unknown')}\n"
        f"Page: {ctx.get('page_id', 'unknown')} — {ctx.get('page_title', '')} "
        f"(version {ctx.get('page_version', '?')})\n"
        f"Write mode: {write_mode} — "
        + ("in PROPOSE mode, post proposed content with ConfluenceTarget "
           "add_comment for a human to apply; direct page writes are refused.\n"
           if write_mode == "propose"
           else "in DIRECT mode, page writes land immediately (version-guarded). "
           "ALWAYS get_page first and pass its version as base_version.\n")
        + "Reply to: post your answer with the ConfluenceTarget add_comment tool "
        "on this page (the gateway-only reply channel).\n\n"
        f"{_atlassian_repo_lines(ctx)}"
    )
    sel = ctx.get("inline_selection")
    if sel:
        block += f"\n### Inline selection (the anchored text the comment refers to)\n{sel}\n"
    return block
