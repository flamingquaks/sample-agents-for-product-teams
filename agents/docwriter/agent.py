"""Docwriter — Autonomous Technical Writer agent.

Keeps documentation in sync with the codebase, generates API docs from
OpenAPI specs, drafts release notes from merged PRs, and detects doc gaps.

Deployed to Amazon Bedrock AgentCore Runtime.
Uses Claude Opus 4.7 via Bedrock and GitHub's official remote MCP server.
GitHub-only — the Workitems agent owns Asana side of the loop.
"""

import logging
import os

from strands import Agent
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands_tools.agent_core_memory import AgentCoreMemoryToolProvider

from shared.assignment import complete_assignment, fail_assignment
from shared.bedrock import build_model
from shared.dispatch_context import build_dispatch_context_block
from prompts import SYSTEM_PROMPT
from project_config import build_project_context
from tools.generate_api_docs import generate_api_docs
from tools.generate_release_notes import generate_release_notes
from tools.detect_doc_gaps import detect_doc_gaps
from tools.check_doc_freshness import check_doc_freshness
from tools.post_results import post_results
from shared.tools.slack_post import slack_post_message, slack_post_thread
from shared.discord_post import discord_post_message, make_discord_followup_tool
from tools.github_mcp import get_github_token, GITHUB_MCP_URL

logger = logging.getLogger(__name__)

# --- Configuration -----------------------------------------------------------

MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID")
ACTOR_ID = "docwriter"

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
    source_context = payload.get("source_context", {}) or payload.get("context", {}) or {}

    # Dispatch context lives in the system prompt, not in the user message —
    # the "[Dispatch Context] ... [User Request]" wrapper trips Bedrock
    # Guardrails' PROMPT_ATTACK filter because it mirrors the canonical
    # injection shape. The agent still needs this context to know which
    # PR/issue triggered it; we just deliver it via the system slot.
    dispatch_context_block = build_dispatch_context_block(source, source_context)

    # Build system prompt with project context + dispatch context.
    system_prompt = SYSTEM_PROMPT.format(
        project_context=build_project_context(),
    ) + dispatch_context_block

    # max_tokens bumped from Claude's default (4K) to 16K so Docwriter can
    # emit multi-file patches without hitting MaxTokensReachedException
    # mid-tool-call on a multi-file README update.
    model = build_model(max_tokens=16000)
    tools = [generate_api_docs, generate_release_notes, detect_doc_gaps, check_doc_freshness, post_results, slack_post_message, slack_post_thread]
    if source == "discord":
        tools.append(make_discord_followup_tool(
            application_id=source_context.get("application_id", ""),
            interaction_token=source_context.get("interaction_token", ""),
        ))
        tools.append(discord_post_message)

    # Memory — optional until Memory resource is created
    if MEMORY_ID:
        memory_provider = AgentCoreMemoryToolProvider(
            memory_id=MEMORY_ID,
            actor_id=ACTOR_ID,
            session_id=session_id,
            namespace=f"/agents/docwriter/{session_id}",
        )
        tools.extend(memory_provider.tools)

    # GitHub MCP — official remote server (primary for Docwriter)
    github_token = get_github_token()
    github_client = MCPClient(
        lambda: streamablehttp_client(
            GITHUB_MCP_URL,
            headers={"Authorization": f"Bearer {github_token}"},
        )
    )

    with github_client:
        github_tools = github_client.list_tools_sync()
        all_tools = [*github_tools, *tools]

        agent = Agent(
            model=model,
            system_prompt=system_prompt,
            tools=all_tools,
        )
        try:
            result = agent(user_input)
        except Exception as agent_error:
            try:
                fail_assignment(assignment_id, error=str(agent_error))
            except Exception:
                logger.exception("fail_assignment also failed for %s", assignment_id)
            raise
        complete_assignment(assignment_id, result_summary=str(result)[:500])

    return {"result": str(result)}


if __name__ == "__main__":
    app.run()
