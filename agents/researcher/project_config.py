"""Project configuration for the Researcher agent.

Maps project resources so the agent always knows where to find things.
Loaded at startup and injected into the system prompt.
"""

import os

# Asana — Researcher's primary workspace. Optional: a UI-onboarded capability
# may carry no Asana env (the fleet's capability row `env` map is often empty),
# and the agent must still boot — a missing project just means no Asana scope.
ASANA_PROJECT_GID = os.environ.get("ASANA_PROJECT_GID", "")
ASANA_PROJECT_NAME = os.environ.get("ASANA_PROJECT_NAME", "")
ASANA_WORKSPACE_GID = os.environ.get("ASANA_WORKSPACE_GID", "")


def build_project_context() -> str:
    """Build a project context block for injection into the system prompt."""
    if not ASANA_PROJECT_GID:
        return """\

## Project Resources

No Asana project is configured for this deployment. Do the research and
respond in the channel/thread you were dispatched from; do not attempt to
read or create Asana tasks unless the request names a project explicitly.
"""
    project_line = f"- Project: {ASANA_PROJECT_NAME}\n" if ASANA_PROJECT_NAME else ""
    return f"""\

## Project Resources

These are YOUR project resources. Use them directly — never ask the user
for project IDs.

Asana:
{project_line}- Project GID: {ASANA_PROJECT_GID}
- Workspace GID: {ASANA_WORKSPACE_GID}

When reading or creating tasks, ALWAYS use project "{ASANA_PROJECT_GID}".
Do NOT ask the user for these — you already have them.
"""
