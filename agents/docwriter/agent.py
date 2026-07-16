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

from shared.assignment import (
    complete_assignment,
    extract_token_usage,
    extract_trace_refs_from_messages,
    extract_trace_refs_from_result,
    fail_assignment,
    update_trace_refs,
)
from shared.bedrock import build_model
from prompts import SYSTEM_PROMPT
from project_config import build_project_context
from tools.generate_api_docs import generate_api_docs
from tools.generate_release_notes import generate_release_notes
from tools.detect_doc_gaps import detect_doc_gaps
from tools.check_doc_freshness import check_doc_freshness
from tools.post_results import post_results
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
    dispatch_context_block = ""
    if source_context and source == "github":
        is_pr = source_context.get("is_pr") == "true" or source_context.get("pr_number")
        issue_or_pr = source_context.get("issue_number") or source_context.get("pr_number", "unknown")
        target_type = "PR" if is_pr else "issue"
        dispatch_context_block = (
            "\n\n## Current Dispatch\n\n"
            f"Source: github\n"
            f"Repository: {source_context.get('repo', 'unknown')}\n"
            f"{target_type}: #{issue_or_pr}\n"
            f"Title: {source_context.get('issue_title', 'unknown')}\n"
            f"Body:\n{source_context.get('issue_body', '')}\n"
            f"Labels: {source_context.get('issue_labels', '')}\n"
            f"State: {source_context.get('issue_state', '')}\n"
            f"Comments:\n{source_context.get('issue_comments', '(not loaded)')}\n"
            f"Reply to: GitHub {target_type} #{issue_or_pr} "
            f"on {source_context.get('repo', 'unknown')}\n"
        )
    elif source_context and source == "asana":
        dispatch_context_block = (
            "\n\n## Current Dispatch\n\n"
            f"Source: asana\n"
            f"Task GID: {source_context.get('task_gid', 'unknown')}\n"
            f"Task: {source_context.get('task_name', 'unknown')}\n"
            f"Task Notes: {source_context.get('task_notes', '')}\n"
            f"Project: {source_context.get('project_name', 'unknown')} ({source_context.get('project_gid', '')})\n"
            f"Reply to: Asana task {source_context.get('task_gid', 'unknown')}\n"
        )

    # Build system prompt with project context + dispatch context. The repo to
    # act on comes from the dispatch (multi-repo fleet), not a baked env var.
    dispatch_repo = source_context.get("repo") if source == "github" else None
    system_prompt = SYSTEM_PROMPT.format(
        project_context=build_project_context(dispatch_repo),
    ) + dispatch_context_block

    # max_tokens bumped from Claude's default (4K) to 16K so Docwriter can
    # emit multi-file patches without hitting MaxTokensReachedException
    # mid-tool-call on a multi-file README update.
    model = build_model(max_tokens=16000)
    tools = [generate_api_docs, generate_release_notes, detect_doc_gaps, check_doc_freshness, post_results]

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
        complete_assignment(
            assignment_id,
            result_summary=str(result)[:500],
            token_usage=extract_token_usage(result),
        )
        # If the run opened a doc PR, record branch/PR as trace refs so the work
        # is traceable on the dashboard. docwriter opens the PR itself via the
        # GitHub MCP server, so the tool-call transcript (agent.messages) carries
        # the branch and PR — the reliable, structured source. Fall back to the
        # PR url in the final text. Best-effort — never blocks the run.
        refs = {
            **extract_trace_refs_from_result(result),
            **extract_trace_refs_from_messages(getattr(agent, "messages", None)),
        }
        if refs:
            try:
                update_trace_refs(assignment_id, **refs)
            except Exception:
                logger.exception("update_trace_refs failed for %s", assignment_id)

    return {"result": str(result)}


if __name__ == "__main__":
    app.run()
