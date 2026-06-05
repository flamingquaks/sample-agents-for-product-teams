"""Project configuration for the Researcher agent.

Maps project resources so the agent always knows where to find things. Loaded at
startup and injected into the system prompt.

The PM backend is selectable via PM_BACKEND (shared scaffold in
agents/shared/project_config.py):
- "asana"  (default): Asana is the research workspace — input from Asana tasks,
  output as Asana comments / new tasks. Requires ASANA_*.
- "github": GitHub is the workspace — input from GitHub issues + a Projects V2
  board, output as issue comments / new issues on the board. Requires
  GITHUB_REPO and GITHUB_PROJECT_NUMBER (+ optional GITHUB_PROJECT_OWNER).

Only the selected backend's prose builder runs, so only that backend's env vars
are read (lazy, to avoid an import-time crash on a single-backend deploy).
"""

import os

from shared.project_config import PM_BACKEND, build_project_context as _dispatch, require_env

ACTOR_ID = "researcher"  # used in the require_env error messages


def _build_asana_context() -> str:
    project_gid = require_env("ASANA_PROJECT_GID", ACTOR_ID)
    workspace_gid = require_env("ASANA_WORKSPACE_GID", ACTOR_ID)
    project_name = os.environ.get("ASANA_PROJECT_NAME", "")

    project_line = f"- Project: {project_name}\n" if project_name else ""
    return f"""\

## Project Resources

These are YOUR project resources. Use them directly — never ask the user
for project IDs.

Asana:
{project_line}- Project GID: {project_gid}
- Workspace GID: {workspace_gid}

When reading or creating tasks, ALWAYS use project "{project_gid}".
Do NOT ask the user for these — you already have them.
"""


def _build_github_context() -> str:
    github_repo = require_env("GITHUB_REPO", ACTOR_ID)
    owner, repo_name = github_repo.split("/", 1)
    project_number = require_env("GITHUB_PROJECT_NUMBER", ACTOR_ID)
    project_owner = os.environ.get("GITHUB_PROJECT_OWNER", owner)

    return f"""\

## Project Resources

These are YOUR project resources. Use them directly — never ask the user
for repo URLs or project numbers.

GitHub:
- Repository: {github_repo}
- Owner: {owner}
- Repo name: {repo_name}
- Projects V2 board: project #{project_number} owned by "{project_owner}"

Research input comes from GitHub issues and the Projects V2 board (#{project_number}).
Output goes back as issue comments, and — when you draft user stories — as new
GitHub issues added to that board.

When creating issues, ALWAYS use repo "{github_repo}".
When reading or updating the board, use project #{project_number} owned by "{project_owner}".
Do NOT ask the user for these — you already have them.
"""


def build_project_context() -> str:
    """Build this agent's project-context block for the selected PM backend."""
    return _dispatch(_build_asana_context, _build_github_context)


__all__ = ["PM_BACKEND", "build_project_context"]
