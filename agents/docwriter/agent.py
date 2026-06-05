"""Docwriter — Autonomous Technical Writer agent.

Keeps documentation in sync with the codebase, generates API docs from
OpenAPI specs, drafts release notes from merged PRs, and detects doc gaps.

Deployed to Amazon Bedrock AgentCore Runtime.
Uses Claude Opus 4.7 via Bedrock and GitHub's official remote MCP server.
GitHub-only — the Workitems agent owns Asana side of the loop.
"""

import logging
import os
import sys

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands_tools.agent_core_memory import AgentCoreMemoryToolProvider

from shared.bedrock import build_model
from shared.dispatch_context import build_dispatch_context
from shared.mcp_clients import run_single_github_agent
from prompts import SYSTEM_PROMPT
from project_config import build_project_context
from tools.generate_api_docs import generate_api_docs
from tools.generate_release_notes import generate_release_notes
from tools.detect_doc_gaps import detect_doc_gaps
from tools.check_doc_freshness import check_doc_freshness
from shared.tools.post_results import post_results
from shared.tools.slack_post import slack_post_message, slack_add_reaction

# Configure root logger to stdout so AgentCore's OTel sidecar captures app logs
# (matches the other agents; docwriter previously omitted this).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
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
    # Built by the shared helper so all agents share one correct implementation
    # (see agents/shared/dispatch_context.py). The github branch there handles
    # PRs (is_pr / pr_number + labels/state), which docwriter relies on.
    dispatch_context_block = build_dispatch_context(source, source_context)

    # Build system prompt with project context + dispatch context.
    system_prompt = SYSTEM_PROMPT.format(
        project_context=build_project_context(),
    ) + dispatch_context_block

    # max_tokens bumped from Claude's default (4K) to 16K so Docwriter can
    # emit multi-file patches without hitting MaxTokensReachedException
    # mid-tool-call on a multi-file README update.
    model = build_model(max_tokens=16000)
    tools = [
        generate_api_docs,
        generate_release_notes,
        detect_doc_gaps,
        check_doc_freshness,
        post_results,
        slack_post_message,
        slack_add_reaction,
    ]

    # Memory — optional until Memory resource is created
    if MEMORY_ID:
        memory_provider = AgentCoreMemoryToolProvider(
            memory_id=MEMORY_ID,
            actor_id=ACTOR_ID,
            session_id=session_id,
            namespace=f"/agents/docwriter/{session_id}",
        )
        tools.extend(memory_provider.tools)

    # Docwriter is GitHub-native (reads issues/PRs, opens doc PRs). Slack posting
    # is handled by direct Web API tools already in `tools` — no live MCP
    # connection needed. The shared helper connects one GitHub MCP client and
    # runs the agent through the shared assignment lifecycle.
    result = run_single_github_agent(
        model=model,
        system_prompt=system_prompt,
        custom_tools=tools,
        user_input=user_input,
        assignment_id=assignment_id,
    )
    return {"result": str(result)}


if __name__ == "__main__":
    app.run()
