"""Reviewer — Autonomous pull-request review agent.

Reads a PR diff and posts one COMMENT review with inline findings (correctness,
safety, soundness) — a second pair of eyes on every diff. GitHub-only; no other
integrations. Reviews for ordinary-reviewer concerns, NOT architecture-decision
conformance (that's Adr).

Deployed to Amazon Bedrock AgentCore Runtime, pattern-identical to `adr`.
Uses the fleet's default model via Bedrock Mantle and the Gateway GitHub tools.
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
from shared.dispatch_context import (
    confluence_dispatch_block,
    jira_dispatch_block,
    slack_dispatch_block,
)
from shared.tools import gateway
from strands import Agent
from strands_tools.agent_core_memory import AgentCoreMemoryToolProvider
from tools.format_review import format_review
from tools.plan_review import plan_review
from tools.review_state import get_review_state, record_review

# --- Logging -----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID")
ACTOR_ID = "reviewer"

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload, context=None):
    """Main entry. Reviews the dispatched PR."""
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

    # Reviewer operates on GitHub PRs. Context tells it the PR, its head SHA, the
    # trigger (mention vs. automation opened/synchronize), and whether a prior
    # review exists. Dispatch context lives in the system prompt, not the user
    # message — the "[Dispatch Context] ... [User Request]" wrapper trips Bedrock
    # Guardrails' PROMPT_ATTACK filter because it mirrors the injection shape.
    dispatch_context_block = ""
    if source_context and source == "github":
        pr_number = source_context.get("pr_number", source_context.get("issue_number", "unknown"))
        auto_event = source_context.get("automation_event", "")
        trigger_line = (
            f"Auto-trigger: {auto_event} (automation rule)\n"
            if auto_event else "Trigger: @reviewer mention (run REVIEW_FULL)\n"
        )
        dispatch_context_block = (
            "\n\n## Current Dispatch\n\n"
            f"Source: github\n"
            f"Repository: {source_context.get('repo', 'unknown')}\n"
            f"Pull request: #{pr_number}\n"
            f"Head SHA: {source_context.get('head_sha', '(read via get_pull_request)')}\n"
            f"Base branch: {source_context.get('base_branch', 'unknown')}\n"
            f"Draft: {source_context.get('draft', 'unknown')}\n"
            f"{trigger_line}"
            f"Title: {source_context.get('pr_title') or source_context.get('issue_title', 'unknown')}\n"
            f"Body:\n{source_context.get('pr_body') or source_context.get('issue_body', '')}\n"
            f"Comments:\n{source_context.get('comments', '(not loaded — read via GitHub tools)')}\n"
            f"Reply to: GitHub PR #{pr_number} on {source_context.get('repo', 'unknown')}\n"
        )
    elif source_context and source == "slack":
        dispatch_context_block = slack_dispatch_block(source_context)
    elif source_context and source == "jira":
        dispatch_context_block = jira_dispatch_block(source_context)
    elif source_context and source == "confluence":
        dispatch_context_block = confluence_dispatch_block(source_context)

    # The repo to act on comes from the dispatch (multi-repo fleet), not a baked
    # env var. Reviewer is GitHub-only, so the dispatch always carries the repo.
    dispatch_repo = source_context.get("repo") or None
    system_prompt = (
        SYSTEM_PROMPT.format(project_context=build_project_context(dispatch_repo))
        + dispatch_context_block
    )

    # Cost attribution uses the fleet's shared Mantle project (MANTLE_PROJECT_ID
    # runtime env, read inside build_model) — dispatches may span repos.
    model = build_model()
    tools = [
        get_review_state,
        plan_review,
        format_review,
        record_review,
    ]

    if MEMORY_ID:
        memory_provider = AgentCoreMemoryToolProvider(
            memory_id=MEMORY_ID,
            actor_id=ACTOR_ID,
            session_id=session_id,
            namespace=f"/agents/reviewer/{session_id}",
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

        # Durable session + ask_user (durable-repo-work spec). Reviewer reads and
        # comments — no repo workspace (its tier is contents:read).
        session, durable_tools, hooks, durable_prompt = durable.durable_kit(
            assignment_id,
            agent_id=ACTOR_ID,
            origin=dispatch_repo or "",
            repo_capable=False,
            source=source,
            source_context=source_context,
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
