"""Researcher — Autonomous Business Analyst agent.

Performs research synthesis, competitive intelligence, requirements drafting,
backlog analysis, and impact estimation.

The PM backend is selectable via PM_BACKEND:
- "asana"  (default): input/output through Asana tasks + comments.
- "github": input/output through GitHub issues + a Projects V2 board (comments
  for analysis/briefs; new issues for drafted user stories). No Asana connection.

Deployed to Amazon Bedrock AgentCore Runtime. Uses Claude Opus via Bedrock and
the official Asana or GitHub remote MCP server depending on the backend.
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
from prompts import get_system_prompt
from project_config import build_project_context, PM_BACKEND
from tools.synthesize_research import synthesize_research
from tools.competitive_scan import competitive_scan
from tools.review_spec import review_spec
from tools.analyze_backlog import analyze_backlog
from tools.draft_user_stories import draft_user_stories
from shared.tools.post_results import post_results
from tools.web_search import web_search
from shared.tools.asana_mcp import get_access_token, ASANA_MCP_URL
from shared.tools.github_mcp import get_github_token, GITHUB_MCP_URL
from shared.tools.slack_post import slack_post_message, slack_add_reaction

# In github mode the agent needs the (opt-in) Projects V2 toolset in addition
# to the default issues/repos tools — selected via this header on the remote
# GitHub MCP server. (See workitems/agent.py for the same pattern.)
GITHUB_PM_TOOLSETS = "default,projects"

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

    # Dispatch context lives in the system prompt, not the user message (the
    # injection-shape concern). Built by the shared helper so all agents share
    # one correct implementation (see agents/shared/dispatch_context.py).
    dispatch_context_block = build_dispatch_context(source, source_context)

    # Build system prompt with project context + dispatch context.
    system_prompt = (
        get_system_prompt(PM_BACKEND, build_project_context())
        + dispatch_context_block
    )

    model = build_model()
    tools = [
        synthesize_research,
        competitive_scan,
        review_spec,
        analyze_backlog,
        draft_user_stories,
        post_results,
        web_search,
        slack_post_message,
        slack_add_reaction,
    ]

    # Memory — optional until Memory resource is created
    if MEMORY_ID:
        memory_provider = AgentCoreMemoryToolProvider(
            memory_id=MEMORY_ID,
            actor_id=ACTOR_ID,
            session_id=session_id,
            namespace=f"/agents/researcher/{session_id}",
        )
        tools.extend(memory_provider.tools)

    # Connect the MCP server for the selected PM backend:
    # - github: a single GitHub MCP client with the projects toolset (read
    #   issues + board, create issues, post comments).
    # - asana:  the Asana MCP client (read tasks, post comments, create tasks).
    if PM_BACKEND == "github":
        github_token = get_github_token()
        github_client = MCPClient(
            lambda: streamablehttp_client(
                GITHUB_MCP_URL,
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "X-MCP-Toolsets": GITHUB_PM_TOOLSETS,
                },
            )
        )
        with github_client:
            all_tools = [*github_client.list_tools_sync(), *tools]
            result = _run_agent(model, system_prompt, all_tools, user_input, assignment_id)
        return {"result": str(result)}

    # asana mode (default)
    asana_token = get_access_token()
    asana_client = MCPClient(
        lambda: streamablehttp_client(
            ASANA_MCP_URL,
            headers={"Authorization": f"Bearer {asana_token}"},
        )
    )
    with asana_client:
        all_tools = [*asana_client.list_tools_sync(), *tools]
        result = _run_agent(model, system_prompt, all_tools, user_input, assignment_id)

    return {"result": str(result)}


def _run_agent(model, system_prompt, all_tools, user_input, assignment_id):
    """Construct and run the agent, recording assignment completion/failure."""
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
    return result


if __name__ == "__main__":
    app.run()
