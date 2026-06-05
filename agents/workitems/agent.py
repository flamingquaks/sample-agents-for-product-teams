"""Workitems — PO/PM autonomous agent.

The PM (planning) backend is selectable via PM_BACKEND:
- "asana"  (default): bridges Asana (planning) and GitHub (development).
- "github": GitHub is both surfaces — Issues are work items and a Projects V2
  board is the planning/roadmap surface. No Asana connection is made.

Assigned work via the planning surface, reasons about decomposition, proposes
plans for approval, then creates GitHub issues on approval.

Deployed to Amazon Bedrock AgentCore Runtime.
Uses Claude Opus via Bedrock, GitHub's official remote MCP server, and
(in asana mode) Asana's official MCP server.
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
from tools.status_report import generate_status_report
from tools.risk_detection import detect_risks
from tools.sync import reconcile_sync
from shared.tools.post_results import post_results
from shared.tools.asana_mcp import get_access_token, ASANA_MCP_URL
from shared.tools.github_mcp import get_github_token, GITHUB_MCP_URL
from shared.tools.slack_post import slack_post_message, slack_add_reaction

# When GitHub is the PM backend we need the (opt-in) Projects V2 toolset in
# addition to the default issues/repos/pull_requests tools. The remote GitHub
# MCP server selects toolsets via this header; a comma-separated list keeps the
# default tools AND adds projects. (The /x/projects URL path form is
# single-toolset only and would drop the issues tools, so we use the header.)
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
ACTOR_ID = "workitems"

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
    source_context = payload.get("source_context", {})

    # Dispatch context lives in the system prompt, not the user message — a
    # "[Dispatch Context] ... [User Request]" wrapper in the user turn mirrors
    # the canonical prompt-injection shape and trips Bedrock Guardrails'
    # PROMPT_ATTACK filter. Built by the shared helper so all agents share one
    # correct implementation (see agents/shared/dispatch_context.py).
    source = payload.get("source", "unknown")
    dispatch_context_block = build_dispatch_context(source, source_context)

    model = build_model()
    # Slack posting is a pair of cheap function tools (direct Web API), so they
    # are always available regardless of trigger source — no live connection.
    tools = [
        generate_status_report,
        detect_risks,
        post_results,
        slack_post_message,
        slack_add_reaction,
    ]
    # reconcile_sync reconciles Asana <-> GitHub drift; it's meaningless when
    # GitHub is the single source of truth, so only load it in asana mode.
    if PM_BACKEND == "asana":
        tools.append(reconcile_sync)

    # Memory — optional until Memory resource is created
    if MEMORY_ID:
        memory_provider = AgentCoreMemoryToolProvider(
            memory_id=MEMORY_ID,
            actor_id=ACTOR_ID,
            session_id=session_id,
            namespace=f"/agents/workitems/{session_id}",
        )
        tools.extend(memory_provider.tools)

    system_prompt = (
        get_system_prompt(PM_BACKEND).format(project_context=build_project_context())
        + dispatch_context_block
    )

    # Build the MCP client set for the selected PM backend.
    #
    # - asana mode:  Asana MCP (planning) + GitHub MCP default toolset (dev).
    # - github mode: a single GitHub MCP client with the projects toolset added
    #   so the agent can read/write the Projects V2 board AND issues/PRs.
    github_token = get_github_token()

    if PM_BACKEND == "github":
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
            github_tools = github_client.list_tools_sync()
            all_tools = [*github_tools, *tools]
            result = _run_agent(
                model, system_prompt, all_tools, user_input, assignment_id
            )
        return {"result": str(result)}

    # asana mode (default)
    asana_token = get_access_token()
    asana_client = MCPClient(
        lambda: streamablehttp_client(
            ASANA_MCP_URL,
            headers={"Authorization": f"Bearer {asana_token}"},
        )
    )
    github_client = MCPClient(
        lambda: streamablehttp_client(
            GITHUB_MCP_URL,
            headers={"Authorization": f"Bearer {github_token}"},
        )
    )

    with asana_client, github_client:
        asana_tools = asana_client.list_tools_sync()
        github_tools = github_client.list_tools_sync()

        # Drop tools that collide with earlier-priority tool names
        # (e.g. both servers expose get_me — keep the Asana version)
        asana_names = {t.tool_name for t in asana_tools}
        github_tools = [gt for gt in github_tools if gt.tool_name not in asana_names]

        all_tools = [*asana_tools, *github_tools, *tools]
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
