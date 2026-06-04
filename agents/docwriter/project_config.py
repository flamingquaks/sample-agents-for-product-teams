"""Project configuration for the Docwriter agent.

Maps project resources so the agent always knows where to find things.
Loaded at startup and injected into the system prompt.

Docwriter is source-control-native: GitHub is required (it reads code and opens
doc PRs). Asana is OPTIONAL — it only provides extra "feature context" for docs.
A GitHub-PM-only or Slack-only deployment has no Asana, so the Asana GIDs are
read lazily and the Asana block is omitted from the context when they're absent.
Reading them at import time (as this module used to) crashed the agent at
startup whenever Asana wasn't configured.
"""

import os

# GitHub — Docwriter's primary workspace (reads code, opens doc PRs). Required.
GITHUB_REPO = os.environ["GITHUB_REPO"]
GITHUB_REPO_OWNER, GITHUB_REPO_NAME = GITHUB_REPO.split("/", 1)


def build_project_context() -> str:
    """Build a project context block for injection into the system prompt.

    The Asana section is included only when Asana is configured; otherwise
    Docwriter operates purely against GitHub.
    """
    block = f"""\

## Project Resources

These are YOUR project resources. Use them directly — never ask the user
for repo URLs or project IDs.

GitHub:
- Repository: {GITHUB_REPO}
- Owner: {GITHUB_REPO_OWNER}
- Repo name: {GITHUB_REPO_NAME}

When opening doc PRs, ALWAYS target repo "{GITHUB_REPO}".
"""

    # Asana is optional supplementary context. Read lazily so a no-Asana deploy
    # doesn't crash; include the block only when a project GID is present.
    asana_project_gid = os.environ.get("ASANA_PROJECT_GID", "")
    asana_workspace_gid = os.environ.get("ASANA_WORKSPACE_GID", "")
    asana_project_name = os.environ.get("ASANA_PROJECT_NAME", "")
    if asana_project_gid:
        project_line = f"- Project: {asana_project_name}\n" if asana_project_name else ""
        block += f"""
Asana (optional feature context):
{project_line}- Project GID: {asana_project_gid}
- Workspace GID: {asana_workspace_gid}

When reading Asana tasks for feature context, use project "{asana_project_gid}".
"""

    block += "Do NOT ask the user for these — you already have them.\n"
    return block
