"""Shared MCP-client wiring + agent run loop for PM-capable agents.

workitems and researcher duplicated: the `_run_agent` helper, the
GITHUB_PM_TOOLSETS constant, and the github-vs-asana MCP client construction
(incl. the tool-name collision filter). This centralizes all of it.

The one legitimate per-agent difference in asana mode — workitems also connects
GitHub (it bridges Asana planning + GitHub dev), while researcher connects only
Asana — is expressed by the `github_in_asana_mode` flag, not by duplicating the
whole block.

Tool precedence / collision rule: when two MCP servers expose the same tool name
(e.g. both Asana and GitHub expose `get_me`), the earlier list wins; we drop the
colliding later-server tools so the Agent sees one tool per name.
"""

import logging

from strands import Agent
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client

from shared.assignment import complete_assignment, fail_assignment
from shared.tools.asana_mcp import get_access_token, ASANA_MCP_URL
from shared.tools.github_mcp import get_github_token, GITHUB_MCP_URL

logger = logging.getLogger(__name__)

# In github PM mode the agent needs the opt-in Projects V2 toolset in addition
# to the default issues/repos/pull_requests tools. The remote GitHub MCP server
# selects toolsets via this header; the comma list keeps the default tools AND
# adds projects. (The /x/projects URL path form is single-toolset only and would
# drop the issues tools, so we use the header.)
GITHUB_PM_TOOLSETS = "default,projects"


def _github_client(*, projects_toolset: bool) -> MCPClient:
    token = get_github_token()
    headers = {"Authorization": f"Bearer {token}"}
    if projects_toolset:
        headers["X-MCP-Toolsets"] = GITHUB_PM_TOOLSETS
    return MCPClient(lambda: streamablehttp_client(GITHUB_MCP_URL, headers=headers))


def _asana_client() -> MCPClient:
    token = get_access_token()
    return MCPClient(
        lambda: streamablehttp_client(
            ASANA_MCP_URL, headers={"Authorization": f"Bearer {token}"}
        )
    )


def _dedupe(primary_tools, secondary_tools):
    """Drop secondary tools whose name collides with a primary tool (primary wins)."""
    primary_names = {t.tool_name for t in primary_tools}
    return [t for t in secondary_tools if t.tool_name not in primary_names]


def run_agent(model, system_prompt, all_tools, user_input, assignment_id):
    """Construct and run the agent, recording assignment completion/failure."""
    agent = Agent(model=model, system_prompt=system_prompt, tools=all_tools)
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


def run_single_github_agent(
    *,
    model,
    system_prompt,
    custom_tools,
    user_input,
    assignment_id,
):
    """Connect a single GitHub MCP client (default toolset) and run the agent.

    For GitHub-native agents (docwriter, adr) that have no PM backend, no Asana,
    and no Projects V2 needs — they just read issues/PRs and write comments.
    Reuses the shared run_agent loop so the assignment lifecycle isn't reimplemented.
    """
    client = _github_client(projects_toolset=False)
    with client:
        all_tools = [*client.list_tools_sync(), *custom_tools]
        return run_agent(model, system_prompt, all_tools, user_input, assignment_id)


def run_with_pm_backend(
    pm_backend,
    *,
    model,
    system_prompt,
    custom_tools,
    user_input,
    assignment_id,
    github_in_asana_mode,
):
    """Connect the MCP client set for the PM backend, then run the agent.

    Args:
        pm_backend: "github" or "asana".
        model: the Bedrock model.
        system_prompt: assembled system prompt.
        custom_tools: the agent's own @tool functions (post_results, slack, etc.).
        user_input / assignment_id: passed through to run_agent.
        github_in_asana_mode: in asana mode, also connect the GitHub MCP server
            (default toolset) and merge its tools. workitems=True (bridges Asana
            + GitHub); researcher=False (Asana-only).

    Returns:
        The agent result (str-able).
    """
    if pm_backend == "github":
        # Single GitHub client with the Projects V2 toolset enabled.
        client = _github_client(projects_toolset=True)
        with client:
            all_tools = [*client.list_tools_sync(), *custom_tools]
            return run_agent(model, system_prompt, all_tools, user_input, assignment_id)

    # asana mode (default)
    asana = _asana_client()
    if not github_in_asana_mode:
        with asana:
            all_tools = [*asana.list_tools_sync(), *custom_tools]
            return run_agent(model, system_prompt, all_tools, user_input, assignment_id)

    github = _github_client(projects_toolset=False)
    with asana, github:
        asana_tools = asana.list_tools_sync()
        # Asana wins name collisions (e.g. get_me) — drop the GitHub duplicates.
        github_tools = _dedupe(asana_tools, github.list_tools_sync())
        all_tools = [*asana_tools, *github_tools, *custom_tools]
        return run_agent(model, system_prompt, all_tools, user_input, assignment_id)
