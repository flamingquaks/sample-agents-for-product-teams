"""Adr — Autonomous ADR-linking agent.

Reads a repo's ADR library and links GitHub issues/PRs to the architecture
decisions that govern them. GitHub-only; no other integrations.

Deployed to Amazon Bedrock AgentCore Runtime.
Uses Claude Opus 4.7 via Bedrock and GitHub's official remote MCP server.
"""

import logging
import os
import sys
from contextlib import ExitStack

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from project_config import build_project_context
from prompts import SYSTEM_PROMPT
from shared import durable
from shared.assignment import complete_assignment, extract_usage, fail_assignment
from shared.bedrock import build_model
from shared.dispatch_context import slack_dispatch_block
from shared.tools import gateway
from strands import Agent
from strands_tools.agent_core_memory import AgentCoreMemoryToolProvider
from tools.find_linked_issues import find_linked_issues
from tools.format_rationale import format_pr_review_summary, format_tag_issue_comment
from tools.index_adrs import index_adrs
from tools.match_adrs import match_issue_to_adrs, match_pr_to_adrs

# --- Logging -----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID")
ACTOR_ID = "adr"

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload, context=None):
    """Main entry. Dispatches based on trigger source (issue vs PR)."""
    if isinstance(payload, str):
        import json
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            payload = {"prompt": payload}
    if not isinstance(payload, dict):
        payload = {"prompt": str(payload)}

    user_input = payload.get("prompt", payload.get("user_input", ""))
    ctx = context if isinstance(context, dict) else {}
    session_id = payload.get("session_id", ctx.get("session_id", "default"))
    assignment_id = payload.get("assignment_id", "")
    source_context = payload.get("source_context", {})
    source = payload.get("source", "unknown")

    # Adr only operates on GitHub events. Context tells it whether the
    # mention was on an issue, on a linked PR, or on an unlinked PR.
    # Dispatch context lives in the system prompt, not in the user message —
    # the "[Dispatch Context] ... [User Request]" wrapper trips Bedrock
    # Guardrails' PROMPT_ATTACK filter because it mirrors the canonical
    # injection shape.
    dispatch_context_block = ""
    if source_context and source == "github":
        is_pr = bool(source_context.get("pr_number"))
        pr_body = source_context.get("pr_body", "")
        issue_or_pr_number = (
            source_context.get("pr_number") if is_pr
            else source_context.get("issue_number", "unknown")
        )
        dispatch_context_block = (
            "\n\n## Current Dispatch\n\n"
            f"Source: github\n"
            f"Repository: {source_context.get('repo', 'unknown')}\n"
            f"Trigger target: {'PR' if is_pr else 'Issue'} #{issue_or_pr_number}\n"
            f"Title: {source_context.get('pr_title') or source_context.get('issue_title', 'unknown')}\n"
            f"Body:\n{pr_body or source_context.get('issue_body', '')}\n"
            f"Comments:\n{source_context.get('comments', '(not loaded)')}\n"
            f"Reply to: GitHub {'PR' if is_pr else 'issue'} #{issue_or_pr_number} "
            f"on {source_context.get('repo', 'unknown')}\n"
        )
    elif source_context and source == "slack":
        # The codebase bridge for chat dispatches: which repos this channel is
        # approved for, and when to reach for them (shared/dispatch_context.py).
        dispatch_context_block = slack_dispatch_block(source_context)

    # The repo to act on comes from the dispatch (multi-repo fleet), not a baked
    # env var. Adr is GitHub-only, so the dispatch always carries the repo.
    # The origin repo anchors co-repo enforcement for ANY source that names a
    # repo: GitHub mentions carry the repo they came from; a Slack
    # /sdlc-message-agent dispatch carries the first channel-approved repo the
    # user selected. No repo (e.g. Asana) => no GitHub work possible.
    dispatch_repo = source_context.get("repo") or None
    system_prompt = (
        SYSTEM_PROMPT.format(project_context=build_project_context(dispatch_repo))
        + dispatch_context_block
    )

    # Cost attribution uses the fleet's shared Mantle project (MANTLE_PROJECT_ID
    # runtime env, read inside build_model) — dispatches may span repos.
    model = build_model()
    tools = [
        index_adrs,
        match_issue_to_adrs,
        match_pr_to_adrs,
        find_linked_issues,
        format_tag_issue_comment,
        format_pr_review_summary,
    ]

    if MEMORY_ID:
        memory_provider = AgentCoreMemoryToolProvider(
            memory_id=MEMORY_ID,
            actor_id=ACTOR_ID,
            session_id=session_id,
            namespace=f"/agents/adr/{session_id}",
        )
        tools.extend(memory_provider.tools)

    # MCP connectivity: one gateway client (SigV4, Cedar-enforced) — the fleet is
    # gateway-only, so all tool calls route through the AgentCore Gateway (policy
    # engine + SCM interceptor + observability). The origin + agent headers let
    # the gateway enforce co-repo grouping + per-agent access. See
    # agents/shared/tools/gateway.py.
    with ExitStack() as stack:
        gw = stack.enter_context(
            gateway.build_gateway_client(
                dispatch_origin=dispatch_repo, agent=ACTOR_ID
            )
        )
        all_tools = [*gw.list_tools_sync(), *tools]

        # Durable session + ask_user (durable-repo-work spec). adr reviews and
        # comments — no repo workspace (its tier is contents:read).
        session, durable_tools, hooks, durable_prompt = durable.durable_kit(
            assignment_id,
            agent_id=ACTOR_ID,
            origin=dispatch_repo or "",
            repo_capable=False,
            source=source,
        )
        all_tools.extend(durable_tools)

        agent = Agent(
            model=model,
            system_prompt=system_prompt + durable_prompt,
            tools=all_tools,
            hooks=hooks,
            session_manager=session,
        )
        try:
            resume = payload.get("resume") if isinstance(payload.get("resume"), dict) else None
            if resume:
                durable.mark_resumed(assignment_id)
                result = agent(
                    durable.resume_payload(
                        str(resume.get("interrupt_id", "")),
                        str(resume.get("response", "")),
                    )
                )
            else:
                result = agent(user_input)
        except Exception as agent_error:
            try:
                fail_assignment(assignment_id, error=str(agent_error))
            except Exception:
                logger.exception("fail_assignment also failed for %s", assignment_id)
            raise
        try:
            pause = durable.handle_agent_result(result, assignment_id)
        except Exception as pause_error:
            # D7: a pause whose checkpoint can't land must FAIL LOUD.
            try:
                fail_assignment(assignment_id, error=f"pause checkpoint failed: {pause_error}")
            except Exception:
                logger.exception("fail_assignment also failed for %s", assignment_id)
            raise
        if pause:
            return {"result": f"PAUSED: {pause['question']}"}
        complete_assignment(
            assignment_id,
            result_summary=str(result)[:3500],
            token_usage=extract_usage(result),
        )

    return {"result": str(result)}


if __name__ == "__main__":
    app.run()
