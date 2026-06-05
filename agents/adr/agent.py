"""Adr — Autonomous ADR-linking agent.

Reads a repo's ADR library and links GitHub issues/PRs to the architecture
decisions that govern them. GitHub-only; no other integrations.

Deployed to Amazon Bedrock AgentCore Runtime.
Uses Claude Opus 4.7 via Bedrock and GitHub's official remote MCP server.
"""

import logging
import os
import sys

from strands import Agent
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands_tools.agent_core_memory import AgentCoreMemoryToolProvider

from shared.assignment import complete_assignment, fail_assignment
from shared.bedrock import build_model
from shared.dispatch_context import build_dispatch_context
from prompts import SYSTEM_PROMPT
from project_config import build_project_context
from tools.index_adrs import index_adrs
from tools.match_adrs import match_issue_to_adrs, match_pr_to_adrs
from tools.find_linked_issues import find_linked_issues
from tools.format_rationale import format_tag_issue_comment, format_pr_review_summary
from shared.tools.github_mcp import get_github_token, GITHUB_MCP_URL
from shared.tools.slack_post import slack_post_message, slack_add_reaction

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
    # Built by the shared helper so all agents share one correct implementation
    # (see agents/shared/dispatch_context.py). Its github branch handles PRs
    # (pr_title/pr_body/comments fallbacks) which adr's PR reviews rely on.
    dispatch_context_block = build_dispatch_context(source, source_context)

    system_prompt = SYSTEM_PROMPT.format(project_context=build_project_context()) + dispatch_context_block

    model = build_model()
    tools = [
        index_adrs,
        match_issue_to_adrs,
        match_pr_to_adrs,
        find_linked_issues,
        format_tag_issue_comment,
        format_pr_review_summary,
        slack_post_message,
        slack_add_reaction,
    ]

    if MEMORY_ID:
        memory_provider = AgentCoreMemoryToolProvider(
            memory_id=MEMORY_ID,
            actor_id=ACTOR_ID,
            session_id=session_id,
            namespace=f"/agents/adr/{session_id}",
        )
        tools.extend(memory_provider.tools)

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
