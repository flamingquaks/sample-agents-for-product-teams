"""Generic base agent for config-driven (custom) capabilities (spec §4).

Reads its identity, prompt, and skill configuration from the runtime environment
rather than hardcoded imports — so a new agent is a capability row + this base
image, no code checkin. The tool set comes from the Gateway (policy-filtered per
agent-id by the Cedar engine, §3.5); skills are loaded natively via AgentSkills.

This file is the ONLY runtime-coupled piece; it sits behind a thin RUNTIME seam
so a future alternative (Codex / Kiro / Claude Code) can be introduced by
replacing this agent.py without touching the schema, deployer, or UI.
"""

import json
import logging
import os
import sys
from contextlib import ExitStack
from pathlib import Path

from strands import Agent
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from shared.assignment import complete_assignment, extract_token_usage, fail_assignment
from shared.bedrock import build_model
from shared.tools import gateway

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

AGENT_ID = os.environ.get("AGENT_ID", "custom-agent")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "You are a helpful assistant.")
SKILLS_DIR = os.environ.get("SKILLS_DIR", "/app/skills")

GATEWAY_MCP_URL = os.environ.get("GATEWAY_MCP_URL")
if not GATEWAY_MCP_URL:
    logger.critical("GATEWAY_MCP_URL is required — this agent routes ALL tool calls through the Gateway")
    sys.exit(1)


def _load_skills_plugin():
    """Wire AgentSkills from the resolved SKILLS_DIR (§6). Returns None if no
    skills are present or the plugin can't be loaded (degraded: agent runs
    prompt-only without skills rather than failing to start)."""
    skills_path = Path(SKILLS_DIR)
    if not skills_path.is_dir():
        return None
    skill_dirs = [d for d in skills_path.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]
    if not skill_dirs:
        return None
    try:
        from strands_agents_tools.plugins import AgentSkills
        return AgentSkills(skills=[str(d) for d in skill_dirs])
    except ImportError:
        logger.warning("strands AgentSkills plugin not available; skills will not load")
        return None


app = BedrockAgentCoreApp()


@app.handler
def invoke(payload, context=None):
    user_input = ""
    source_context = {}
    assignment_id = ""
    if isinstance(payload, dict):
        try:
            body = json.loads(payload.get("body", "{}")) if isinstance(payload.get("body"), str) else payload
        except (json.JSONDecodeError, TypeError):
            body = payload
        user_input = body.get("instruction", "") or body.get("body", "")
        source_context = body.get("source_context", {})
        assignment_id = body.get("assignment_id", "")

    if not user_input:
        logger.error("no instruction in payload")
        return {"statusCode": 400, "body": "no instruction"}

    dispatch_repo = source_context.get("repo", "")

    with ExitStack() as stack:
        gw = stack.enter_context(
            gateway.build_gateway_client(agent=AGENT_ID)
        )
        all_tools = list(gw.list_tools_sync())

        plugins = []
        skills_plugin = _load_skills_plugin()
        if skills_plugin:
            plugins.append(skills_plugin)

        prompt = SYSTEM_PROMPT
        if dispatch_repo:
            prompt += f"\n\nCurrent Dispatch:\n- Repository: {dispatch_repo}\n"

        agent = Agent(
            model=build_model(),
            system_prompt=prompt,
            tools=all_tools,
            plugins=plugins,
        )

        try:
            result = agent(user_input)
            output = str(result)
            if assignment_id:
                complete_assignment(
                    assignment_id,
                    result_summary=output[:500],
                    token_usage=extract_token_usage(result),
                )
        except Exception as exc:
            logger.exception("agent run failed")
            if assignment_id:
                fail_assignment(assignment_id, error=str(exc))
            return {"statusCode": 500, "body": f"agent error: {exc}"}

    body = output if len(output) <= 5000 else output[:5000] + "\n\n…[output truncated]"
    return {"statusCode": 200, "body": body}


if __name__ == "__main__":
    app.run()
