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

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands_tools.agent_core_memory import AgentCoreMemoryToolProvider

from shared.bedrock import build_model
from shared.dispatch_context import build_dispatch_context
from shared.mcp_clients import run_with_pm_backend
from prompts import get_system_prompt
from project_config import build_project_context, PM_BACKEND
from tools.status_report import generate_status_report
from tools.risk_detection import detect_risks
from tools.sync import reconcile_sync
from shared.tools.post_results import post_results
from shared.tools.slack_post import slack_post_message, slack_add_reaction

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
        get_system_prompt(PM_BACKEND, build_project_context())
        + dispatch_context_block
    )

    # Connect the MCP client set for the PM backend and run. workitems bridges
    # Asana planning + GitHub dev, so in asana mode it ALSO connects GitHub
    # (github_in_asana_mode=True); in github mode a single GitHub client with
    # the Projects V2 toolset covers both planning and dev.
    result = run_with_pm_backend(
        PM_BACKEND,
        model=model,
        system_prompt=system_prompt,
        custom_tools=tools,
        user_input=user_input,
        assignment_id=assignment_id,
        github_in_asana_mode=True,
    )
    return {"result": str(result)}


if __name__ == "__main__":
    app.run()
