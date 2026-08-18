"""Workitems — PO/PM autonomous agent.

Bridges Asana (planning) and GitHub (development). Assigned work via Asana
tasks, reasons about decomposition, proposes plans for approval, then creates
GitHub issues on approval.

Deployed to Amazon Bedrock AgentCore Runtime.
Uses Claude Opus 4.6 via Bedrock, Asana's official MCP server, and
GitHub's official remote MCP server.
"""

import logging
import os
import sys
from contextlib import ExitStack

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from project_config import build_project_context
from prompts import SYSTEM_PROMPT
from shared import durable
from shared.assignment import (
    complete_assignment,
    extract_trace_refs_from_result,
    extract_usage,
    fail_assignment,
    update_trace_refs,
)
from shared.bedrock import build_model
from shared.dispatch_context import (
    confluence_dispatch_block,
    jira_dispatch_block,
    slack_dispatch_block,
)
from shared.memory import memory_tools
from shared.tools import gateway
from strands import Agent
from tools.post_results import post_results
from tools.risk_detection import detect_risks
from tools.status_report import generate_status_report
from tools.sync import reconcile_sync

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
    # Resume dispatch (durable-repo-work spec): saved interrupt id + the
    # human's reply; the S3 session restores the conversation.
    resume = payload.get("resume") if isinstance(payload.get("resume"), dict) else None

    # Dispatch context lives in the system prompt, not in the user message.
    # Structured "[Dispatch Context] ... [User Request] ..." wrappers look
    # like the canonical prompt-injection shape and trip Bedrock Guardrails'
    # PROMPT_ATTACK filter even at MEDIUM strength. Keeping the user-supplied
    # content as the only user-role message lets the guardrail evaluate what
    # actually came from outside the trust boundary.
    source = payload.get("source", "unknown")
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
    elif source_context and source == "github":
        dispatch_context_block = (
            "\n\n## Current Dispatch\n\n"
            f"Source: github\n"
            f"Repository: {source_context.get('repo', 'unknown')}\n"
            f"Issue: #{source_context.get('issue_number', 'unknown')}\n"
            f"Issue Title: {source_context.get('issue_title', 'unknown')}\n"
            f"Issue Body:\n{source_context.get('issue_body', '')}\n"
            f"Comments:\n{source_context.get('issue_comments', '(not loaded)')}\n"
            f"Reply to: GitHub issue #{source_context.get('issue_number', 'unknown')} "
            f"on {source_context.get('repo', 'unknown')}\n"
        )
    elif source_context and source == "slack":
        # The codebase bridge for chat dispatches: which repos this channel is
        # approved for, and when to reach for them (shared/dispatch_context.py).
        dispatch_context_block = slack_dispatch_block(source_context)
    elif source_context and source == "jira":
        dispatch_context_block = jira_dispatch_block(source_context)
    elif source_context and source == "confluence":
        dispatch_context_block = confluence_dispatch_block(source_context)

    # Cost attribution uses the fleet's shared Mantle project (MANTLE_PROJECT_ID
    # runtime env, read inside build_model) — a dispatch may span repos, so there
    # is no per-repo project to pass through.
    model = build_model()
    tools = [generate_status_report, detect_risks, reconcile_sync, post_results]

    # Memory — optional until Memory resource is created
    if MEMORY_ID:
        tools.extend(memory_tools(
            memory_id=MEMORY_ID,
            actor_id=ACTOR_ID,
            session_id=session_id,
            namespace=f"/agents/workitems/{session_id}",
        ))

    # The repo to act on comes from the dispatch (multi-repo fleet), not a
    # baked env var. Absent for non-GitHub dispatches — the project context
    # then defers to the Current Dispatch block. Resolved before the MCP client
    # (and before the model) so it can be stamped as the trusted dispatch origin
    # the gateway interceptor/broker use to scope GitHub access to this owner.
    # The origin repo anchors co-repo enforcement for ANY source that names a
    # repo: GitHub mentions carry the repo they came from; a Slack
    # /sdlc-message-agent dispatch carries the first channel-approved repo the
    # user selected. No repo (e.g. Asana) => no GitHub work possible.
    dispatch_repo = source_context.get("repo") or None

    # MCP connectivity: one gateway client (SigV4, Cedar-enforced) — the fleet is
    # gateway-only, so ALL tool calls route through the AgentCore Gateway (policy
    # engine + SCM interceptor + observability). No direct-to-vendor path. The
    # origin + agent headers let the gateway enforce co-repo grouping and
    # per-agent GitHub access. See agents/shared/tools/gateway.py.
    with ExitStack() as stack:
        # Single gateway client — targets are aggregated + per-principal filtered
        # at the gateway, so no client-side dedup is needed.
        gw = stack.enter_context(
            gateway.build_gateway_client(
                dispatch_origin=dispatch_repo, agent=ACTOR_ID
            )
        )
        all_tools = [*gw.list_tools_sync(), *tools]

        system_prompt = (
            SYSTEM_PROMPT.format(project_context=build_project_context(dispatch_repo))
            + dispatch_context_block
        )

        # Durable session + ask_user (durable-repo-work spec). workitems does
        # issue/task work, not repo work — no workspace tools (repo_capable=False;
        # its GitHub tier has no contents grant anyway).
        session, durable_tools, hooks, durable_prompt = durable.durable_kit(
            assignment_id,
            agent_id=ACTOR_ID,
            origin=dispatch_repo or "",
            repo_capable=False,
            source=source,
            source_context=source_context,
        )
        all_tools.extend(durable_tools)
        system_prompt += durable_prompt

        agent = Agent(
            model=model,
            system_prompt=system_prompt,
            tools=all_tools,
            hooks=hooks,
            session_manager=session,
        )
        try:
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
                fail_assignment(
                    assignment_id,
                    error=f"pause checkpoint failed: {pause_error}",
                )
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
        # If the run opened a PR, record it as a trace ref so the work is
        # traceable by PR on the dashboard. Best-effort — never blocks the run.
        refs = extract_trace_refs_from_result(result)
        if refs:
            try:
                update_trace_refs(assignment_id, **refs)
            except Exception:
                logger.exception("update_trace_refs failed for %s", assignment_id)

    return {"result": str(result)}


if __name__ == "__main__":
    app.run()
