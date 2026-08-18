"""Researcher — Autonomous Business Analyst agent.

Performs research synthesis, competitive intelligence, requirements drafting,
backlog analysis, and impact estimation. Writes through Asana; has READ-ONLY
GitHub access (issues, PRs, files, history) so analysis is grounded in the
real codebase and issue tracker.

Deployed to Amazon Bedrock AgentCore Runtime.
Uses Claude Opus 4.7 via Bedrock and Asana's official MCP server.
"""

import logging
import os
import sys

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
from shared.memory import memory_tools
from shared.tools import gateway
from strands import Agent
from tools.analyze_backlog import analyze_backlog
from tools.competitive_scan import competitive_scan
from tools.draft_user_stories import draft_user_stories
from tools.post_results import post_results
from tools.review_spec import review_spec
from tools.synthesize_research import synthesize_research
from tools.web_search import web_search

# --- Logging -----------------------------------------------------------------
# Configure root logger to emit to stdout so AgentCore's OTel sidecar captures
# application logs alongside traces and metrics.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# --- Configuration -----------------------------------------------------------

MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID")
ACTOR_ID = "researcher"

# --- App ---------------------------------------------------------------------

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload, context=None):
    """Main agent entry point. Receives instruction from Dispatch or schedule."""

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
    source = payload.get("source", "unknown")
    source_context = payload.get("source_context", {})

    # Dispatch context lives in the system prompt, not in the user message —
    # the "[Dispatch Context] ... [User Request]" wrapper trips Bedrock
    # Guardrails' PROMPT_ATTACK filter because it mirrors the canonical
    # injection shape.
    dispatch_context_block = ""
    if source_context and source == "asana":
        dispatch_context_block = (
            "\n\n## Current Dispatch\n\n"
            f"Source: asana\n"
            f"Task GID: {source_context.get('task_gid', 'unknown')}\n"
            f"Task: {source_context.get('task_name', 'unknown')}\n"
            f"Task Notes: {source_context.get('task_notes', '')}\n"
            f"Project: {source_context.get('project_name', 'unknown')} ({source_context.get('project_gid', '')})\n"
            f"Reply to: Asana task {source_context.get('task_gid', 'unknown')}\n"
        )
    elif source_context and source == "slack":
        # The codebase bridge for chat dispatches: which repos this channel is
        # approved for, and when to reach for them (shared/dispatch_context.py).
        dispatch_context_block = slack_dispatch_block(source_context)
    elif source_context and source == "jira":
        dispatch_context_block = jira_dispatch_block(source_context)
    elif source_context and source == "confluence":
        dispatch_context_block = confluence_dispatch_block(source_context)

    # Build system prompt with project context + dispatch context.
    system_prompt = SYSTEM_PROMPT.format(
        project_context=build_project_context(),
    ) + dispatch_context_block

    # Cost attribution uses the fleet's shared Mantle project (MANTLE_PROJECT_ID
    # runtime env, read inside build_model) — dispatches may span repos.
    model = build_model()
    tools = [
        synthesize_research,
        competitive_scan,
        review_spec,
        analyze_backlog,
        draft_user_stories,
        post_results,
        web_search,
    ]

    # Memory — optional until Memory resource is created
    if MEMORY_ID:
        tools.extend(memory_tools(
            memory_id=MEMORY_ID,
            actor_id=ACTOR_ID,
            session_id=session_id,
            namespace=f"/agents/researcher/{session_id}",
        ))

    # The origin repo anchors co-repo enforcement: a Slack dispatch carries the
    # first channel-approved repo, and the interceptor scopes researcher's
    # READ-ONLY GitHub reach from it. No repo (e.g. Asana dispatch) => no
    # GitHub reads possible, exactly as before.
    dispatch_repo = source_context.get("repo") or None

    # Gateway-only: route through the AgentCore Gateway (Cedar-enforced, SigV4).
    # Origin + agent headers let the gateway enforce co-repo grouping and
    # researcher's read-only GitHub tier.
    with gateway.build_gateway_client(
        dispatch_origin=dispatch_repo, agent=ACTOR_ID
    ) as gw:
        all_tools = [*gw.list_tools_sync(), *tools]

        # Durable session + ask_user (durable-repo-work spec). Researcher's
        # GitHub access is READ-ONLY (API reads via the gateway) — no git
        # workspace, so repo_capable stays False.
        session, durable_tools, hooks, durable_prompt = durable.durable_kit(
            assignment_id, agent_id=ACTOR_ID, origin=dispatch_repo or "",
            repo_capable=False, source=source, source_context=source_context,
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
